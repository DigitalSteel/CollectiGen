from __future__ import annotations

from .help_content import workspace_help

import copy
from contextlib import contextmanager
from dataclasses import dataclass,replace
from pathlib import Path
import sqlite3
import time
import uuid

from core.evidence_freshness import current_candidate_findings
from core.image_knowledge_history import HistoryWriteInterrupted
from core.history_versions import HistoryError
from .history_workflow import capture_operation, open_history, open_organization_history, history_dialogs_busy, close_history_dialogs
from core.organization_history_scope import OrganizationHistoryScope

from PySide6.QtCore import QEvent,QSignalBlocker,QThreadPool,QTimer,QUrl,Signal,Qt
from PySide6.QtGui import QAction,QDesktopServices,QImage
from PySide6.QtWidgets import (
    QCheckBox,QComboBox,QDialog,QDialogButtonBox,QFileDialog,QFrame,QGridLayout,QGroupBox,QHBoxLayout,QInputDialog,QLabel,QLineEdit,
    QHeaderView,QMenu,QMessageBox,QProgressBar,QPushButton,QScrollArea,QSizePolicy,QSplitter,QTabBar,QTabWidget,QTextEdit,QToolButton,QTreeWidget,QTreeWidgetItem,QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core import CategoryRegistry,CharacterDatabase,ConfirmedTagIdentityError,ConfirmedTagIdentityService,ConflictRuleEngine,DEFAULT_EXPORT_PROFILE,DatasetCaptionExportService,DownstreamUpdateStore,ExportProfileStore,ExternalEvidenceImportCheckpointService,ExternalWriteDecision,ExternalWritePolicy,FolderAuditAnalyzer,ImageAuthorityInitializationService,ImageEvidenceInspector,ImageFindingDecisionService,ImageKnowledgeReconciliationService,ImageRecord,ImageTagBatchService,ImageWorkflowState,ImageWorkflowStateService,LIBRARY_NAMING_KEEP,LibraryMovePlanningService,LibraryMoveProtectionService,LibraryPrivacyPolicy,LibraryProfile,LibraryRole,ManagedMetadataCorrectionService,MetadataBackupService,MetadataSyncService,MissingTagExceptionStore,MissingTagSearch,OperationJournal,OrderImpact,OrganizationRouter,PAIRED_IMAGE_KNOWLEDGE,PROJECTION_EXCLUSION,PreferredCaptionPreparationService,SourceCorrectionService,TagCollection,TagOrderCandidate,TagOrderingService,TrainingCandidateSelectionService,TrainingPackageResult,TrainingPackageService,TrainingPlanDraftStore,TrainingProjectionExclusionService,TrainingReadinessService,TransferRecoveryRequired,UnifiedPreparationService,active_automation_engine,affected_order_projection_paths,configured_plain_language_backend,configured_wd_backends,image_tag_identity,normalize_plain_language_exclusion,split_plain_language,status_from_severity,tag_addition_decision_issues,typed_tag_sources,unavailable_confirmed_tag_identities
from core.paths import CATEGORIES_PATH,DEFAULTS_DIR,EXPORT_PROFILES_PATH,MODEL_RESOURCES_PATH,TRAINING_PLAN_DRAFTS_PATH,USER_DATA_DIR
from core.model_resource_registry import ModelResourceRegistry
from core.tag_collection import normalize_tag
from core.dataset_search_projection import (
    DatasetSearchProjectionStore,DatasetSearchSpec,
)
from core.ingest_gallery_projection import RevisionDict
from .ai_run_dialog import RunAIDialog
from .components import DisclosureButton,EmptyStatePanel,PageHeader,RangeCheckTreeWidget,compact_button,counted_noun,show_operation_in_progress,ui_setting_bool
from .dependency_dialog import ensure_capability
from .deferred_editor import defer_editor_delivery
from .export_profile_dialog import ExportProfileManagerDialog
from .external_evidence_import_dialog import ExternalEvidenceImportDialog
from .gallery import IMAGE_BADGE_HELP,GalleryScaleControl,ImageCardGrid,content_safety_policy,filter_content_paths
from .image_data_editor import (
    ImageDataEditor,REQUEST_SOURCE_CORRECTION,prepare_image_data_editor_context,
)
from .image_identity_correction_widget import ImageIdentityCorrectionWidget
from .image_tag_batch_dialog import ImageTagBatchDialog
from .library_move_dialog import LibraryMovePreviewDialog
from .library_profile_dialog import LibraryDestinationDialog
from .metadata_backup_dialog import (
    MetadataBackupPlanDialog,MetadataBackupProtectionDialog,
    MetadataBackupResultDialog,
)
from .preferred_caption_preparation_dialog import PreferredCaptionPreparationDialog
from .preparation_controls import (
    PreparationControls,ai_tag_runtime_recovery,show_ai_tag_runtime_recovery,
)
from .preparation_result_dialog import PreparationResultDialog,should_show_preparation_results
from .modal_feedback import FeedbackEnabledDialog
from .problems_dialog import ProblemsDialog
from .recycle_bin_dialog import RecycleBinDialog
from .duplicate_finder_dialog import DuplicateFinderDialog
from .source_correction_dialog import (
    SourceCorrectionPlanDialog,SourceCorrectionProtectionDialog,
    SourceCorrectionResultDialog,
)
from .tag_relationship_editor import TagReferencePickerDialog
from .tag_identity_review import review_removed_identities
from .training_caption_projection_widget import TrainingCaptionProjectionWidget
from .training_candidate_plan_dialog import (
    TrainingCandidateGoalDialog,TrainingCandidatePlanDialog,
    TrainingPlanDraftPickerDialog,TrainingScopeChoice,TrainingScopeKind,
)
from .image_knowledge_summary import ImageKnowledgeSummaryWidget
from .training_candidate_review import TrainingCandidateReviewSession,training_duplicate_provider
from .training_package_dialog import (
    TrainingPackageComparisonDialog,TrainingPackageDestinationDialog,
    TrainingPackagePlanDialog,TrainingPackageResultDialog,
)
from .training_txt_export_dialog import (
    TrainingTXTExportDestinationDialog,TrainingTXTExportPlanDialog,
)
from .workers import AsyncCallTask,CancelableProgressCallTask,CoordinatedCallTask,ProgressCallTask,start_qt_delivery_task
from .resolution_filter import ResolutionFilter
from .gallery_filters import GalleryFilters


def _dataset_preparation_token(state) -> tuple:
    """Cheap stale guard for cold-page inputs, never image/library contents."""
    from core.paths import RELATIONSHIPS_PATH

    knowledge=state.knowledge
    paths=(
        knowledge.path,knowledge.path.with_name("relationships.json"),
        CATEGORIES_PATH,RELATIONSHIPS_PATH,
        *(USER_DATA_DIR/name for name in (
            "series.json","characters.json","outfits.json",
            "relationships.json","automation_rules.json",
        )),
        *(DEFAULTS_DIR/name for name in (
            "categories.json","relationships.json","automation_rules.json",
            "conflicts.json",
        )),
        USER_DATA_DIR.parent/"defaults"/"relationships.json",
    )
    snapshots=[]
    for path in paths:
        try:
            stat=path.stat()
            snapshots.append((str(path),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns))
        except OSError:
            snapshots.append((str(path),None))
    rules=tuple(sorted((
        rule.id,tuple(rule.source_tags),rule.target_tag,bool(rule.enabled),
        bool(rule.require_review),str(rule.extras.get("intent") or ""),
    ) for rule in getattr(knowledge,"merge_rules",{}).values()))
    return (
        id(knowledge),getattr(knowledge,"_loaded_bytes",None),
        getattr(knowledge,"_relationship_signature_revision",0),rules,
        tuple(sorted(normalize_plain_language_exclusion(value) for value in
                     state.settings.ignored_plain_language_sentences)),
        tuple(snapshots),
    )


@dataclass
class DatasetPagePreparation:
    """Exclusively owned services handed from a worker to one Dataset page."""
    characters: object
    categories: object
    workflow_states: object
    context_fingerprint: str
    source_token: tuple

    def is_current(self,state) -> bool:
        return self.source_token==_dataset_preparation_token(state)


class _DatasetPreparationChanged(RuntimeError):
    pass


def prepare_dataset_page(state,job=None) -> DatasetPagePreparation:
    # A fresh profile or locked character recovery can legitimately create its
    # initial files. Rebuild once from that resulting authority; never relabel
    # an older context as current or retry indefinitely under active edits.
    try:
        return _prepare_dataset_page_once(state,job)
    except _DatasetPreparationChanged:
        return _prepare_dataset_page_once(state,job)


def _prepare_dataset_page_once(state,job=None) -> DatasetPagePreparation:
    def check_interrupt():
        if job is not None and not job.checkpoint():
            raise InterruptedError("Dataset preparation cancelled")

    def checkpoint():
        check_interrupt()
        # Yield the interpreter between CPU-heavy projections so Qt timers and
        # cancellation can run even while the core-only worker is active.
        # A 1 ms yield can let Windows resume this worker before Qt's 10 ms
        # heartbeat is dispatched, joining adjacent native JSON operations
        # into one visible pause. Give the event loop a full scheduling turn.
        time.sleep(.01)

    def progress(current,message):
        checkpoint()
        if job is not None:
            job.update(current,2,message)

    progress(0,"Loading character knowledge")
    source_token=_dataset_preparation_token(state)
    # Admit only this short background load/recovery phase for the same root.
    from core.character_storage_transaction import character_transaction_lock
    with character_transaction_lock(USER_DATA_DIR,check_interrupt=check_interrupt if job is not None else None):
        characters=CharacterDatabase(USER_DATA_DIR)
    categories=CategoryRegistry(DEFAULTS_DIR/"categories.json",CATEGORIES_PATH)
    progress(1,"Preparing Dataset readiness")
    workflow_states=ImageWorkflowStateService(
        state.knowledge,characters,categories=categories,
        ignored_plain_language=state.settings.ignored_plain_language_sentences,
        inspect_sources=True,
    )
    context=workflow_states.context_fingerprint(checkpoint=checkpoint)
    progress(2,"Dataset ready")
    preparation=DatasetPagePreparation(
        characters,categories,workflow_states,context,source_token,
    )
    if not preparation.is_current(state):
        raise _DatasetPreparationChanged("Dataset knowledge changed while the page was opening. Select Dataset to retry.")
    return preparation


def _labeled_filter(
    label_text: str,options,accessible_name: str,accessible_description: str,
):
    group=QFrame(); layout=QVBoxLayout(group)
    layout.setContentsMargins(0,0,0,0); layout.setSpacing(3)
    control=QComboBox()
    for label,key in options:
        control.addItem(label,key)
    control.setAccessibleName(accessible_name)
    control.setAccessibleDescription(accessible_description)
    label=QLabel(label_text); label.setBuddy(control)
    layout.addWidget(label); layout.addWidget(control)
    return group,label,control


class _TrainingProjectionDetails(str):
    """Structured projection copy with a plain-text compatibility surface."""

    def __new__(
        cls,plain_text: str,*,profile_name: str,profile_version: int,
        profile_id: str,counts,decisions: str,health_label: str,
        health_detail: str,
    ):
        value=super().__new__(cls,plain_text)
        value.profile_name=str(profile_name)
        value.profile_version=int(profile_version)
        value.profile_id=str(profile_id)
        value.counts=dict(counts)
        value.decisions=str(decisions)
        value.decision_count=sum(int(count) for count in value.counts.values())
        value.health_label=str(health_label or "Unavailable")
        value.health_detail=str(health_detail or "")
        return value


class _TrainingInspectorWidget(QWidget):
    """Compact training state with the exact audit trace on demand."""

    def __init__(self,parent=None):
        super().__init__(parent); self._plain_text=""
        layout=QVBoxLayout(self); layout.setContentsMargins(6,6,6,6); layout.setSpacing(8)
        self.state=QLabel(); self.state.setWordWrap(True)
        self.state.setTextInteractionFlags(Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard)
        self.state.setAccessibleName("Training projection state")
        layout.addWidget(self.state)

        self.summary_card=QFrame(); self.summary_card.setObjectName("sectionCard")
        self.summary_card.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Maximum)
        summary_layout=QVBoxLayout(self.summary_card); summary_layout.setContentsMargins(10,9,10,9); summary_layout.setSpacing(4)
        summary_layout.setAlignment(Qt.AlignTop)
        self.profile=QLabel(); profile_font=self.profile.font(); profile_font.setBold(True); self.profile.setFont(profile_font)
        self.profile.setWordWrap(True); self.profile.setTextInteractionFlags(Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard)
        self.counts=QLabel(); self.counts.setWordWrap(True)
        self.health=QLabel(); self.health.setWordWrap(True); self.health.setObjectName("mutedLabel")
        self.caption_hint=QLabel("Export Training TXT… previews the complete caption.")
        self.caption_hint.setWordWrap(True); self.caption_hint.setObjectName("mutedLabel")
        for widget in (self.profile,self.counts,self.health,self.caption_hint):
            summary_layout.addWidget(widget)
        layout.addWidget(self.summary_card)

        self.decisions=QTextEdit(); self.decisions.setReadOnly(True)
        self.decisions.setAccessibleName("Projection decisions for the current image")
        self.decisions_toggle=DisclosureButton(
            "Show Projection Decisions","Hide Projection Decisions",self.decisions,
        )
        self.decisions_toggle.setAccessibleName("Show or hide projection decisions")
        toggle_widget=QWidget(); toggle_widget.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Maximum)
        toggle_row=QHBoxLayout(toggle_widget); toggle_row.setContentsMargins(0,0,0,0)
        toggle_row.addWidget(self.decisions_toggle); toggle_row.addStretch(1)
        layout.addWidget(toggle_widget); layout.addWidget(self.decisions,1)
        self.compact_filler=QWidget(); self.compact_filler.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Expanding)
        layout.addWidget(self.compact_filler,1)
        self.decisions_toggle.toggled.connect(
            lambda checked:self.compact_filler.setVisible(not checked)
        )
        self.summary_card.hide(); self.decisions_toggle.hide()

    def set_projection(self,details: _TrainingProjectionDetails):
        self._plain_text=str(details); self.state.hide(); self.summary_card.show()
        self.profile.setText(f"{details.profile_name} v{details.profile_version}")
        profile_help=f"Projection profile ID: {details.profile_id}"
        self.profile.setToolTip(profile_help); self.profile.setAccessibleDescription(profile_help)
        counts=details.counts
        count_text=(
            f"{counts.get('emitted',0)} emitted • "
            f"{counts.get('combined',0)} combined • "
            f"{counts.get('omitted',0)} omitted"
        )
        self.counts.setText(count_text)
        self.health.setText(f"Training Export Health: {details.health_label}")
        self.health.setToolTip(details.health_detail)
        self.health.setAccessibleDescription(details.health_detail)
        self.decisions.setPlainText(details.decisions)
        decision_count=details.decision_count
        self.decisions.setAccessibleDescription(
            f"Exact emitted, combined, and omitted reasons for {decision_count} projection decisions."
        )
        self.decisions_toggle.show_text=f"Show {decision_count} Projection Decisions"
        self.decisions_toggle.hide_text="Hide Projection Decisions"
        self.decisions_toggle.setText(
            self.decisions_toggle.hide_text
            if self.decisions_toggle.isChecked()
            else self.decisions_toggle.show_text
        )
        self.decisions_toggle.setAccessibleDescription(
            f"Shows the exact reason for each of {decision_count} projection decisions."
        )
        self.decisions_toggle.setVisible(decision_count>0)
        self.decisions.setVisible(decision_count>0 and self.decisions_toggle.isChecked())
        self.compact_filler.setVisible(not self.decisions_toggle.isChecked())
        self.decisions.verticalScrollBar().setValue(0)

    def setPlainText(self,text: str):
        self._plain_text=str(text); self.state.setText(self._plain_text)
        self.state.setVisible(bool(self._plain_text)); self.summary_card.hide()
        self.decisions_toggle.hide(); self.decisions.hide(); self.decisions.clear()
        self.compact_filler.show()

    def toPlainText(self) -> str:
        return self._plain_text

    def clear(self):
        self.setPlainText("")

    def verticalScrollBar(self):
        return self.decisions.verticalScrollBar()


class _ImageTagMaintenanceDialog(FeedbackEnabledDialog):
    """Keep the shared maintenance preview outside ordinary Dataset navigation."""

    feedback_workflow="Update image Tags"

    def __init__(self,correction,parent=None):
        super().__init__(parent)
        self.setWindowTitle("Update image Tags from Tag Encyclopedia")
        self.resize(1180,760)
        self.correction=correction
        layout=QVBoxLayout(self)
        layout.addWidget(correction)
        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def reject(self):
        if self.correction.has_active_background_work():
            QMessageBox.information(
                self,"Tag update is running",
                "Wait for the current operation to finish, or use Cancel Safely "
                "before closing this preview.",
            )
            return
        super().reject()

    def closeEvent(self,event):
        if self.correction.has_active_background_work():
            event.ignore()
            self.reject()
            return
        super().closeEvent(event)


class _DeferredImageIdentityCorrectionWidget(QWidget):
    """Keep Dataset usable while its hidden identity index prepares."""

    refresh_requested=Signal()
    edit_image_data=Signal(str)
    history_restored=Signal(object)
    knowledge_changed=Signal()
    open_tag_encyclopedia=Signal(str,object)
    downstream_updates_changed=Signal()
    background_work_finished=Signal()

    def __init__(self,state,parent=None):
        super().__init__(parent); self.state=state
        self._task=None; self._child=None; self._pending_scope=None
        self._knowledge=None; self._knowledge_bytes_id=0
        layout=QVBoxLayout(self); layout.setContentsMargins(16,16,16,16)
        self.status=QLabel(
            "Open the Dashboard's pending Tag updates to review changes from "
            "Tag Encyclopedia."
        )
        self.status.setWordWrap(True); self.status.setObjectName("mutedLabel")
        layout.addWidget(self.status)
        self.progress=QProgressBar(); self.progress.setRange(0,0); self.progress.hide()
        self.progress.setAccessibleName("Image identity correction preparation")
        layout.addWidget(self.progress)
        self.retry=QPushButton("Try Again"); self.retry.setProperty("compact",True)
        self.retry.clicked.connect(self.activate); self.retry.hide()
        layout.addWidget(self.retry,0,Qt.AlignLeft); layout.addStretch(1)
        from .components import bind_status_footer
        bind_status_footer(self,self.status,self.progress,self.retry,title="Image identity")

    @property
    def prepared(self) -> bool:
        return self._child is not None

    def activate(self):
        if self._child is not None or self._task is not None:
            return
        self.retry.hide(); self.progress.show()
        self.status.setText(
            "Preparing exact System Knowledge identity links in the background…"
        )
        self._knowledge=self.state.knowledge
        self._knowledge_bytes_id=id(getattr(self._knowledge,"_loaded_bytes",None))
        state=self.state
        task=CoordinatedCallTask(
            self.state.jobs,"Preparing image correction tools",
            lambda _job:ImageIdentityCorrectionWidget.prepare_service(
                state,
            ),
            resource="cpu",key="dataset:image-identity-correction",
        )
        self._task=task
        task.signals.complete.connect(
            lambda service,owner=task:self._preparation_complete(owner,service)
        )
        task.signals.failed.connect(
            lambda message,owner=task:self._preparation_failed(owner,message)
        )
        task.signals.cancelled.connect(
            lambda owner=task:self._preparation_failed(
                owner,"Preparation was cancelled safely.",
            )
        )
        task.start()


    def _preparation_complete(self,task,service):
        if task is not self._task:
            return
        self._task=None
        if (
            self._knowledge is not self.state.knowledge
            or self._knowledge_bytes_id
            != id(getattr(self.state.knowledge,"_loaded_bytes",None))
        ):
            self._preparation_failed(
                None,"System Knowledge changed while image correction was preparing.",
            )
            return
        child=ImageIdentityCorrectionWidget(
            self.state,self,matching_service=service,
        )
        child.refresh_requested.connect(self.refresh_requested.emit)
        child.edit_image_data.connect(self.edit_image_data.emit)
        child.history_restored.connect(self.history_restored.emit)
        child.knowledge_changed.connect(self.knowledge_changed.emit)
        child.open_tag_encyclopedia.connect(self.open_tag_encyclopedia.emit)
        child.downstream_updates_changed.connect(
            self.downstream_updates_changed.emit
        )
        child.background_work_finished.connect(
            self.background_work_finished.emit
        )
        self._child=child
        self.status.hide(); self.progress.hide(); self.retry.hide()
        self.layout().insertWidget(0,child,1)
        if self._pending_scope is not None:
            scope=self._pending_scope; self._pending_scope=None
            child.open_scope(scope)
        self.background_work_finished.emit()

    def _preparation_failed(self,task,message):
        if task is not None and task is not self._task:
            return
        self._task=None; self.progress.hide(); self.retry.show()
        self.status.setText(
            "Image correction tools could not be prepared. "
            +str(message or "Try again after restoring System Knowledge access.")
        )
        self.background_work_finished.emit()

    def open_scope(self,scope=None):
        if self._child is not None:
            self._child.open_scope(scope); return
        self._pending_scope=scope
        self.activate()

    def refresh_system_knowledge(self):
        if self._child is not None:
            self._child.refresh_system_knowledge()
            return
        active=self._task is not None
        if active:
            self._task.cancel(); self._task=None
            self.activate()

    def has_active_background_work(self) -> bool:
        return bool(
            self._task is not None
            or self._child is not None
            and self._child.has_active_background_work()
        )

    def request_background_shutdown(self) -> bool:
        if self._task is not None:
            self._task.cancel(); self._task=None
            QTimer.singleShot(0,self.background_work_finished.emit)
            return True
        if self._child is not None:
            return self._child.request_background_shutdown()
        return False


class _DeferredTrainingCaptionProjectionWidget(QWidget):
    """Materialize the hidden training workflow only when it is requested."""

    edit_image_data=Signal(str)
    export_requested=Signal(object)
    projection_exclusion_requested=Signal(object)
    return_requested=Signal()
    background_work_finished=Signal()

    def __init__(self,state,parent=None):
        super().__init__(parent); self.state=state; self._child=None
        self._scheduled=False; self._pending_invalidation=()
        self._layout=QVBoxLayout(self); self._layout.setContentsMargins(16,16,16,16)
        self.status=QLabel(
            "Open this tab to build a bounded Training TXT preview from current "
            "Image Knowledge. No caption file changes until export is confirmed."
        )
        self.status.setWordWrap(True); self.status.setObjectName("mutedLabel")
        self._layout.addWidget(self.status); self._layout.addStretch(1)

    def activate(self):
        if self._child is not None or self._scheduled:
            return
        self._scheduled=True
        self.status.setText("Opening the Training TXT projection workspace…")
        QTimer.singleShot(0,self._ensure_child)

    def _ensure_child(self):
        if self._child is not None:
            return self._child
        self._scheduled=False
        child=TrainingCaptionProjectionWidget(self.state,self)
        child.edit_image_data.connect(self.edit_image_data)
        child.export_requested.connect(self.export_requested)
        child.projection_exclusion_requested.connect(
            self.projection_exclusion_requested
        )
        child.return_requested.connect(self.return_requested)
        child.background_work_finished.connect(self.background_work_finished)
        self.status.hide(); self._layout.takeAt(self._layout.count()-1)
        self._layout.addWidget(child); self._child=child
        if self._pending_invalidation:
            child.invalidate_after_authority_refresh(self._pending_invalidation)
            self._pending_invalidation=()
        return child

    def open_scope(self,*args,**kwargs):
        return self._ensure_child().open_scope(*args,**kwargs)

    def has_active_background_work(self):
        return bool(self._child and self._child.has_active_background_work())

    def request_background_shutdown(self):
        return bool(self._child and self._child.request_background_shutdown())

    def derived_results_changed(self,paths=(),*,scope="images"):
        if self._child is not None:
            self._child.derived_results_changed(paths,scope=scope)

    def invalidate_after_authority_refresh(self,paths):
        if self._child is not None:
            self._child.invalidate_after_authority_refresh(paths)
        else:
            self._pending_invalidation=tuple(paths)

    def refresh_system_knowledge(self):
        if self._child is not None:
            self._child.refresh_system_knowledge()

    def clear_contextual_scope(self):
        if self._child is not None:
            self._child.clear_contextual_scope()

    def update_projection_exclusion(self,*args,**kwargs):
        if self._child is not None:
            return self._child.update_projection_exclusion(*args,**kwargs)
        return None


class _ExceptionDialog(FeedbackEnabledDialog):
    feedback_workflow = "Remembered image exceptions"

    def __init__(self,entries,parent=None):
        super().__init__(parent); self.entries=list(entries)
        self.setWindowTitle("Manage remembered exceptions"); self.resize(760,430)
        layout=QVBoxLayout(self)
        intro=QLabel("Check decisions CollectiGen should forget for this image. Unchecked decisions remain in effect.")
        intro.setWordWrap(True); layout.addWidget(intro)
        authority=QLabel("Saved Image Knowledge history covers image-local decisions. Hidden search results are remembered separately and are not restored by that history.")
        authority.setWordWrap(True); layout.addWidget(authority)
        self.tree=RangeCheckTreeWidget(); self.tree.setHeaderLabels(("Forget","Remembered decision","Kind")); self.tree.setRootIsDecorated(False)
        kinds={"rejected_tag":"SDXL Tag not needed","conflict_rule":"Valid conflict","missing_search":"Hidden search result"}
        for kind,key,label in self.entries:
            item=QTreeWidgetItem(("",label,kinds.get(kind,kind))); item.setData(0,Qt.UserRole,(kind,key)); item.setCheckState(0,Qt.Unchecked)
            self.tree.addTopLevelItem(item)
        self.tree.resizeColumnToContents(0); layout.addWidget(self.tree,1)
        buttons=QDialogButtonBox(QDialogButtonBox.Apply|QDialogButtonBox.Cancel)
        buttons.button(QDialogButtonBox.Apply).setText("Forget Checked Decisions")
        buttons.button(QDialogButtonBox.Apply).clicked.connect(self.accept)
        buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def selected(self):
        return [self.tree.topLevelItem(index).data(0,Qt.UserRole) for index in range(self.tree.topLevelItemCount())
                if self.tree.topLevelItem(index).checkState(0)==Qt.Checked]


class _TrainingTXTExportDialog(FeedbackEnabledDialog):
    feedback_workflow = "Training TXT export"

    def __init__(
        self,record,characters,knowledge,maximum_content_rating,parent=None,current_profile_id=None,
        profile_store=None,
    ):
        super().__init__(parent)
        self.record=record; self.characters=characters; self.knowledge=knowledge
        self.maximum_content_rating=maximum_content_rating
        self.profile_store=profile_store or ExportProfileStore(EXPORT_PROFILES_PATH)
        self.setWindowTitle("Export Training TXT"); self.resize(960,700)
        layout=QVBoxLayout(self)
        intro=QLabel(
            "Choose the named projection strategy that will turn confirmed Image Knowledge "
            "into training TXT. You will choose separate-folder or adjacent TXT next."
        )
        intro.setWordWrap(True); layout.addWidget(intro)
        profile_row=QHBoxLayout()
        self.profile_choice=QComboBox()
        profile_row.addWidget(self.profile_choice,1)
        self.manage_profiles_button=QPushButton("Manage Profiles…")
        self.manage_profiles_button.clicked.connect(self._manage_profiles)
        profile_row.addWidget(self.manage_profiles_button)
        layout.addLayout(profile_row)
        self._reload_profiles(current_profile_id)
        self.description=QLabel(); self.description.setWordWrap(True)
        self.description.setObjectName("mutedLabel"); layout.addWidget(self.description)
        self.identity=QLabel(); self.identity.setWordWrap(True); layout.addWidget(self.identity)
        layout.addWidget(QLabel("Exact preview for the first selected image"))
        self.preview=QTextEdit(); self.preview.setReadOnly(True)
        self.preview.setMaximumHeight(130); layout.addWidget(self.preview)
        layout.addWidget(QLabel("Why each confirmed fact was emitted, combined, or omitted"))
        self.explanations=QTreeWidget()
        self.explanations.setHeaderLabels(
            ("Result","Source fact","Final output","Image Knowledge source","Reason")
        )
        self.explanations.setRootIsDecorated(False)
        self.explanations.setAlternatingRowColors(True)
        self.explanations.header().setSectionResizeMode(0,QHeaderView.ResizeToContents)
        self.explanations.header().setSectionResizeMode(1,QHeaderView.Interactive)
        self.explanations.header().setSectionResizeMode(2,QHeaderView.Interactive)
        self.explanations.header().setSectionResizeMode(3,QHeaderView.Interactive)
        self.explanations.header().setSectionResizeMode(4,QHeaderView.Stretch)
        self.explanations.setColumnWidth(1,190)
        self.explanations.setColumnWidth(2,170)
        self.explanations.setColumnWidth(3,190)
        layout.addWidget(self.explanations,1)
        consequence=QLabel(
            "The next step chooses a separate training folder or source-adjacent TXT, then "
            "shows the exact aggregate plan and shared backup policy before any write. This "
            "does not change Image Knowledge, image Metadata, or source images."
        )
        consequence.setWordWrap(True); consequence.setObjectName("mutedLabel")
        layout.addWidget(consequence)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        export=buttons.addButton("Continue…",QDialogButtonBox.AcceptRole)
        export.setProperty("primary",True)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.profile_choice.currentIndexChanged.connect(self._update_preview)
        self._update_preview()

    def selected_profile(self):
        return self.profile_store.resolve(
            self.profile_choice.currentData()
        ).with_maximum_content_rating(self.maximum_content_rating)

    def _reload_profiles(self,current_profile_id=None):
        selected=current_profile_id or self.profile_choice.currentData()
        self.profile_choice.blockSignals(True)
        self.profile_choice.clear()
        for profile in self.profile_store.all_profiles():
            kind = (
                "My profile"
                if profile.profile_id in self.profile_store.profiles_by_id
                else "CollectiGen profile"
            )
            self.profile_choice.addItem(
                f"{profile.name} — v{profile.version} — {kind}",profile.profile_id
            )
        current=self.profile_choice.findData(selected)
        self.profile_choice.setCurrentIndex(current if current>=0 else 0)
        self.profile_choice.blockSignals(False)

    def _manage_profiles(self):
        current_id=self.profile_choice.currentData()
        dialog=ExportProfileManagerDialog(
            self.profile_store,self.record,self.characters,self.knowledge,
            self.maximum_content_rating,self,current_profile_id=current_id,
        )
        dialog.exec()
        current_id=dialog.selected_profile_id()
        self.profile_store.load()
        self._reload_profiles(current_id)
        self._update_preview()

    def _update_preview(self):
        profile=self.selected_profile()
        target=profile.target_model_family
        if profile.target_model_version:
            target=f"{target} {profile.target_model_version}"
        self.description.setText(profile.description)
        self.identity.setText(
            f"Stable ID: {profile.profile_id} • Version {profile.version} • "
            f"Target: {target} • Strategy: {profile.caption_strategy}"
        )
        try:
            projection=profile.project(
                self.record.annotation,self.characters,self.knowledge
            )
            value=projection.render(profile.separator)
            self.preview.setPlainText(
                value if value.strip() else "(This profile produces no training TXT for this image.)"
            )
            self.explanations.clear()
            for decision in projection.decisions:
                item=QTreeWidgetItem((
                    decision.action.title(),
                    decision.source_value,
                    decision.output or "—",
                    decision.source,
                    decision.reason,
                ))
                item.setToolTip(1,decision.source_value)
                item.setToolTip(3,decision.source)
                item.setToolTip(4,decision.reason)
                self.explanations.addTopLevelItem(item)
        except Exception as exc:
            self.preview.setPlainText(f"Preview unavailable: {exc}")
            self.explanations.clear()


class _ProjectionBatchDialog(FeedbackEnabledDialog):
    feedback_workflow = "Confirmed data projection"

    def __init__(self,previews,skipped,errors,parent=None,preferred_targets=None):
        super().__init__(parent); self.previews=list(previews)
        self.setWindowTitle("Preview confirmed-data synchronization"); self.resize(900,580)
        layout=QVBoxLayout(self)
        intro=QLabel(
            f"Previewed {counted_noun(len(self.previews),'authoritative record')} • skipped {len(skipped):,} without usable records • {counted_noun(len(errors),'preview error')}."
        )
        intro.setWordWrap(True); intro.setObjectName("sectionTitle"); layout.addWidget(intro)
        note=QLabel("This writes confirmed Image Knowledge outward. It does not import external evidence or confirm pending findings. Every changed target is backed up, read back, and automatically restored if verification fails.")
        note.setWordWrap(True); note.setObjectName("mutedLabel"); layout.addWidget(note)
        tree=QTreeWidget(); tree.setHeaderLabels(("Image","TXT projection","Metadata projection")); tree.setRootIsDecorated(False); tree.setAlternatingRowColors(True)
        tree.header().setSectionResizeMode(0,QHeaderView.Stretch); tree.header().setSectionResizeMode(1,QHeaderView.ResizeToContents); tree.header().setSectionResizeMode(2,QHeaderView.ResizeToContents)
        for preview in self.previews:
            labels=[]
            for target in ("txt","metadata"):
                difference=preview.difference(target)
                if difference is None: label="Not inspected"
                elif difference.error: label="Unavailable"
                elif difference.will_write: label=difference.summary.replace(f"{target}: ","")
                else: label="Current"
                labels.append(label)
            row=QTreeWidgetItem((preview.image.name,*labels)); row.setToolTip(0,str(preview.image)); tree.addTopLevelItem(row)
        layout.addWidget(tree,1)
        txt_changes=sum(bool(preview.difference("txt") and preview.difference("txt").will_write) for preview in self.previews)
        metadata_differences=[preview.difference("metadata") for preview in self.previews]
        metadata_changes=sum(bool(item and item.will_write) for item in metadata_differences)
        metadata_available=any(bool(item and item.available and not item.error) for item in metadata_differences)
        preferred=set(preferred_targets or ("txt","metadata"))
        self.txt=QCheckBox(f"TXT captions ({txt_changes:,} will change)"); self.txt.setChecked("txt" in preferred); self.txt.setVisible("txt" in preferred); layout.addWidget(self.txt)
        self.metadata=QCheckBox(f"Embedded Metadata ({metadata_changes:,} will change; unavailable targets are skipped)"); self.metadata.setChecked(metadata_available and "metadata" in preferred); self.metadata.setEnabled(metadata_available); self.metadata.setVisible("metadata" in preferred); layout.addWidget(self.metadata)
        if errors:
            detail=QLabel("Preview problems: "+" • ".join(errors[:4])); detail.setWordWrap(True); detail.setObjectName("mutedLabel"); detail.setToolTip("\n".join(errors)); layout.addWidget(detail)
        buttons=QDialogButtonBox(QDialogButtonBox.Apply|QDialogButtonBox.Cancel); buttons.button(QDialogButtonBox.Apply).setText("Sync Selected Targets"); buttons.button(QDialogButtonBox.Apply).clicked.connect(self.accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def targets(self):
        result=[]
        if self.txt.isChecked(): result.append("txt")
        if self.metadata.isChecked(): result.append("metadata")
        return tuple(result)


class DatasetPage(QWidget):
    refresh_requested=Signal(); library_paths_changed=Signal(object); change_library=Signal(); open_ingest_requested=Signal(); content_ratings_requested=Signal(object); edit_tag_requested=Signal(str,object); create_tag_requested=Signal(str,object)
    knowledge_changed=Signal(); open_tag_encyclopedia=Signal(str,object)
    refresh_external_evidence=Signal(object)
    downstream_updates_changed=Signal()
    background_work_finished=Signal()
    open_automation=Signal(str,object)
    ingest_source_correction_return_requested=Signal(object)

    def __init__(
        self,state,parent=None,*,defer_expensive_initialization: bool=False,
        ui_settings=None,preparation=None,
    ):
        if preparation is not None and not preparation.is_current(state):
            raise RuntimeError("Dataset knowledge changed before the page opened.")
        super().__init__(parent); self.state=state; self.settings=state.settings; self.dataset=None
        self._initial_preparation=preparation
        self.ui_settings=ui_settings
        self._summary_visible=ui_setting_bool(
            ui_settings,"dataset/summary_visible",True,
        )
        self._defer_expensive_initialization=bool(defer_expensive_initialization)
        self._conflict_task=None
        self._conflicts_ready=not self._defer_expensive_initialization
        self._conflict_error=""
        self.filtered=[]; self.page=0; self.generation=0; self.cards={}
        self._dataset_search_store=DatasetSearchProjectionStore()
        self._dataset_search_task=None; self._dataset_search_generation=0
        self._dataset_search_source_generation=0
        self._dataset_search_selection_generation=0
        self._dataset_search_requested_spec=None
        self._dataset_search_requested_source_generation=0
        self._dataset_search_requested_selection_generation=0
        self._dataset_search_accepted_spec=None
        self._deferred_dataset_filter=None
        self._dataset_filter_retry_timer=QTimer(self)
        self._dataset_filter_retry_timer.setSingleShot(True)
        self._dataset_filter_retry_timer.setInterval(500)
        self._dataset_filter_retry_timer.timeout.connect(self._retry_dataset_filter)
        self._projection_stage_counts=None; self._projection_unknown_count=0
        self.current_record=None; self.current_report=None; self.undo_annotation=None
        self.batch_mode=""; self.batch_search=None; self.batch_selected=set(); self.batch_undo=[]
        self._batch_task=None; self._batch_task_kind=""
        self._editor_queue=[]; self._editor_queue_index=0; self._ai_batch_paths=[]
        self._preparation_review_scope=set()
        self._preparation_task=None; self._preparation_paths=[]
        self._unified_preparation_service=None
        self._unified_preparation_operation_id=""
        self._unified_preparation_paths=[]
        self._edit_history_target=None
        self._batch_history_target=None
        self._preparation_history_target=None
        self._history_refresh_tasks=[]
        self._shutdown_requested=False
        self._batch_conflict_context={}
        self._active_editor=None
        self._preparation_editor=None
        self._editor_load_task=None; self._editor_load_generation=0
        self._projection_task=None; self._projection_service=None; self._projection_paths=[]; self._library_move_task=None; self._library_move_planner=None; self._library_move_protection_service=None; self._folder_audit_task=None; self._folder_audit_downstream_context=None; self._filter_state_task=None; self._filter_state_automatic=False; self._filter_generation=0
        self._metadata_backup_task=None; self._metadata_backup_stage=""; self._metadata_backup_plan=None; self._metadata_backup_service=None; self._metadata_backup_paths=[]
        self.downstream_updates=DownstreamUpdateStore(
            USER_DATA_DIR/"downstream_updates.json"
        )
        self._projection_downstream_context=None
        self._projection_exclusion_scope={}
        self._projection_exclusion_guard=None
        self._projection_exclusion_return_scope=None
        self._metadata_backup_downstream_context=None
        self._training_export_task=None; self._training_export_stage=""; self._training_export_plan=None; self._training_export_service=None
        self._training_candidate_task=None; self._training_candidate_scope=()
        self._training_candidate_plan=None
        self._training_candidate_draft=None
        self._training_candidate_guard_selection=True
        self._training_package_task=None; self._training_package_stage=""
        self._training_package_plan=None; self._training_package_service=None
        self._record_inspection_generation=0; self._record_inspection_task=None; self._record_inspection_error=""
        self._inspection_generation=0; self._inspection_task=None; self._inspection_detail_cache={}; self._inspection_details_path=None; self._inspection_error=""
        self._pending_content_rating_paths=()
        self._pending_workflow_state_paths=()
        self._problems_dialog=None; self._recycle_dialog=None; self._duplicate_dialog=None
        self._organization_transfer_task=None; self._organization_transfer_context=None
        self._move_history_scope=None
        self._organization_recycle_ids=()
        self.export_profiles=ExportProfileStore(EXPORT_PROFILES_PATH)
        self.training_plan_drafts=TrainingPlanDraftStore(TRAINING_PLAN_DRAFTS_PATH)
        self._training_export_profile_id=DEFAULT_EXPORT_PROFILE.profile_id
        self._search_timer=QTimer(self); self._search_timer.setSingleShot(True); self._search_timer.setInterval(160)
        self._search_timer.timeout.connect(self.apply_filters)
        self._responsive_layout_timer=QTimer(self)
        self._responsive_layout_timer.setSingleShot(True)
        self._responsive_layout_timer.setInterval(16)
        self._responsive_layout_timer.timeout.connect(self.update_responsive_layout)
        self._visible_background_timer=QTimer(self)
        self._visible_background_timer.setSingleShot(True)
        self._visible_background_timer.setInterval(300)
        self._visible_background_timer.timeout.connect(
            self._start_visible_background_work
        )
        self.audit_issues={}; self._readiness_cache=RevisionDict()
        self.exceptions=MissingTagExceptionStore(USER_DATA_DIR/"missing_tag_exceptions.json")
        self._exception_storage_error=""
        self.characters=(preparation.characters if preparation is not None
                         else CharacterDatabase(USER_DATA_DIR))
        self.categories=(preparation.categories if preparation is not None
                         else CategoryRegistry(DEFAULTS_DIR/"categories.json",CATEGORIES_PATH))
        self.conflicts=(
            ConflictRuleEngine()
            if self._defer_expensive_initialization else
            ConflictRuleEngine.load(
                DEFAULTS_DIR/"conflicts.json",tags=self.state.knowledge,
            )
        )
        self.pool=QThreadPool.globalInstance()
        self.workflow_states=preparation.workflow_states if preparation is not None else ImageWorkflowStateService(
            self.state.knowledge,self.characters,
            ignored_plain_language=self.settings.ignored_plain_language_sentences,categories=self.categories,
            inspect_sources=True,
        )
        root=QVBoxLayout(self); root.setContentsMargins(22,18,22,18); root.setSpacing(12)

        self.header=PageHeader(
            "Dataset","",
            workspace_help("dataset")+IMAGE_BADGE_HELP,
        )
        refresh_button=self.header.add_action("Refresh",self.refresh_requested); refresh_button.setProperty("compact",True)
        self.summary_visibility_button=QToolButton()
        self.summary_visibility_button.setCheckable(True)
        self.summary_visibility_button.setFocusPolicy(Qt.StrongFocus)
        self.summary_visibility_button.setProperty("compact",True)
        self.summary_visibility_button.setFixedSize(40,32)
        self.summary_visibility_button.clicked.connect(self.toggle_summary)
        self.header.actions.addWidget(self.summary_visibility_button)
        self.media_library_button=QPushButton("Review Media…")
        self.media_library_button.setProperty("compact",True)
        self.media_library_button.setToolTip("Inspect images, videos and unexpected media in a library")
        self.media_library_button.clicked.connect(self.open_media_library)
        self.header.actions.addWidget(self.media_library_button)
        self.summary_visibility_button.setAccessibleName("Show or hide Dataset summary")
        self._update_summary_visibility_button()
        self.library_actions=QToolButton(); self.library_actions.setText("Library Actions"); self.library_actions.setPopupMode(QToolButton.InstantPopup); self.library_actions.setProperty("compact",True)
        self.library_actions.setAccessibleName("Dataset library actions")
        self.library_actions.setAccessibleDescription(
            "Import external evidence with an exact scope, audit folder "
            "organization, or open the separate Problems "
            "storage folder."
        )
        self.library_actions.setToolTip(
            "Current-library maintenance and recovery actions."
        )
        self.library_actions.setMinimumWidth(
            self.library_actions.fontMetrics().horizontalAdvance(
                self.library_actions.text()
            )+24
        )
        library_menu=QMenu(self.library_actions)
        self.plan_training_scope_action=QAction("Plan Training Dataset…",self)
        self.plan_training_scope_action.setToolTip(
            "Choose the selected images or the exact current matching results across all pages."
        )
        self.plan_training_scope_action.triggered.connect(self.plan_training_dataset_scope)
        self.training_projection_action=QAction("Review Caption Rules…",self)
        self.training_projection_action.setToolTip(
            "Open a non-writing preview of automatic caption-rule effects for "
            "the current Dataset result, then continue to the exact profile export."
        )
        self.training_projection_action.triggered.connect(
            lambda:self.open_function(
                "dataset.training_captions.preview",
                {"origin":"dataset_training_preparation","scope":"current_filter"},
            )
        )
        self.import_external_evidence_action=library_menu.addAction(
            "Import External Evidence…"
        )
        self.import_external_evidence_action.setToolTip(
            "Choose selected images, the current filter, a folder, one named "
            "library, or all included libraries; preview changed or deliberate "
            "rereads before importing reviewable evidence."
        )
        self.import_external_evidence_action.triggered.connect(
            self.open_external_evidence_import
        )
        self.find_duplicates_action=library_menu.addAction(
            "Find Duplicate and Similar Images…"
        )
        self.find_duplicates_action.setToolTip(
            "Compare exact file bytes, decoded pixels, and visual similarity in "
            "the current filter or another explicit local scope."
        )
        self.find_duplicates_action.triggered.connect(self.open_duplicate_finder)
        library_menu.addSeparator()
        audit_action=library_menu.addAction("Audit Folder Organization…"); audit_action.triggered.connect(self.audit_folders)
        problems_action=library_menu.addAction("Open Problems Folder…"); problems_action.triggered.connect(self.browse_problems)
        problems_action.setToolTip("Open the separately configured Problems storage folder; this does not filter the current library.")
        recycle_action=library_menu.addAction("Open Recycle Bin and Orphan Recovery…")
        recycle_action.triggered.connect(self.open_recycle_bin)
        self.library_actions.setMenu(library_menu); self.header.actions.addWidget(self.library_actions)
        root.addWidget(self.header)

        splitter=QSplitter(Qt.Horizontal); root.addWidget(splitter,1)
        splitter.splitterMoved.connect(lambda *_:self._queue_responsive_layout())
        browser=QWidget(); self.browser=browser; browser.setMinimumWidth(0)
        browser_layout=QVBoxLayout(browser); self.browser_layout=browser_layout; browser_layout.setContentsMargins(0,0,8,0)
        workflow_tabs=QTabWidget(); self.workflow_tabs=workflow_tabs
        browse_workflow=QWidget(); self.browse_workflow=browse_workflow; browse_workflow_layout=QVBoxLayout(browse_workflow); browse_workflow_layout.setContentsMargins(8,8,8,8); browse_workflow_layout.setAlignment(Qt.AlignTop)
        self.stage_tabs=QTabBar()
        self.stage_tabs.setObjectName("datasetStageTabs")
        self.stage_tabs.setAccessibleName("Dataset preparation stage")
        self.stage_tabs.setAccessibleDescription(
            "Separate library images into unprepared, partially prepared, and complete stages."
        )
        self.stage_tabs.setDrawBase(False)
        self.stage_tabs.setExpanding(True)
        for label,key,tip in (
            ("Unprepared","unprepared","Images with no saved or pending Tags or Descriptions."),
            ("Partially Prepared","partial","Images with saved work that still has a blocking completeness issue."),
            ("Complete","complete","Images whose Image Knowledge has no blocking completeness issue."),
        ):
            index=self.stage_tabs.addTab(label)
            self.stage_tabs.setTabData(index,key)
            self.stage_tabs.setTabToolTip(index,tip)
        self.stage_tabs.setCurrentIndex(2)
        self.stage_tabs.currentChanged.connect(self._dataset_stage_changed)
        self.search=QLineEdit(); self.search.setPlaceholderText("Search filenames, SDXL Tags, or Descriptions…")
        self.search.setAccessibleName("Search Dataset images")
        self.search.setAccessibleDescription(
            "Search filenames, folder paths, indexed SDXL Tags, and confirmed "
            "Plain Language Descriptions in the included libraries."
        )
        state_group,self.state_filter_label,self.state_filter=_labeled_filter(
            "Image Knowledge detail",
            (("All in this stage","all"),("Image Knowledge complete","ready"),("Needs fact review","review"),("Image Knowledge incomplete","incomplete"),("Images with Problems","problem")),
            "Image Knowledge status filter",
            "Filter images in the current working library by the same Image Knowledge state shown on cards and in the inspector.",
        )
        self.state_filter_group=state_group
        state_group.setObjectName("galleryStateFilter")
        state_group.setProperty("active",False)
        projection_group,self.projection_filter_label,self.projection_filter=_labeled_filter(
            "Training TXT",
            (("All TXT projections","all"),("TXT current","current"),("TXT needs synchronization","needs_sync"),("Image Knowledge record missing","record_missing")),
            "Training TXT projection filter",
            "Filter by the separate Training TXT projection or missing Image Knowledge record state.",
        )
        self.projection_filter_group=projection_group
        self.record_filter=self.state_filter; self.caption_filter=self.projection_filter
        filters=GalleryFilters(self.stage_tabs,self.search,self.clear_filters)
        self.filter_panel=self.browse_filters=filters
        filters.filters_button.toggled.connect(
            lambda *_:QTimer.singleShot(0,self._sync_workflow_geometry)
        )
        filters.add_filter("Image Knowledge",state_group,self.state_filter)
        filters.add_filter("Training TXT",projection_group,self.projection_filter)
        self.search.textChanged.connect(lambda *_:self._search_timer.start())
        self.search.returnPressed.connect(self.apply_filters)
        self.state_filter.currentIndexChanged.connect(self._sync_dataset_filter_emphasis)
        self.state_filter.currentIndexChanged.connect(self.apply_filters)
        self.projection_filter.currentIndexChanged.connect(self.apply_filters)
        browse_workflow_layout.addWidget(filters)
        self.retry_search_row=QWidget()
        self.retry_search_row.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Maximum)
        retry_search_layout=QHBoxLayout(self.retry_search_row)
        retry_search_layout.setContentsMargins(0,0,0,0)
        self.retry_search_hint=QLabel()
        self.retry_search_hint.setObjectName("warningLabel")
        self.retry_search_hint.setWordWrap(True)
        retry_search_layout.addWidget(self.retry_search_hint,1)
        self.retry_search_button=compact_button(QPushButton("Retry Search"))
        self.retry_search_button.setAccessibleDescription(
            "Retry the current Dataset search with the same libraries, stage, and filters. "
            "This does not refresh libraries or change images."
        )
        self.retry_search_button.clicked.connect(
            lambda:self.apply_filters(preserve_page=True)
        )
        retry_search_layout.addWidget(self.retry_search_button)
        self.retry_search_row.hide()
        browse_workflow_layout.addWidget(self.retry_search_row)
        workflow_tabs.addTab(browse_workflow,"Browse library")
        batch_workflow=QWidget(); self.batch_workflow=batch_workflow; batch_workflow_layout=QVBoxLayout(batch_workflow); batch_workflow_layout.setContentsMargins(8,8,8,8); batch_workflow_layout.setAlignment(Qt.AlignTop)
        batch=QWidget(); self.batch_controls=batch; batch.setSizePolicy(QSizePolicy.Expanding,QSizePolicy.Maximum); batch_grid=QGridLayout(batch); batch_grid.setContentsMargins(0,0,0,0)
        self.batch_kind=QComboBox(); self.batch_kind.addItems(("Verify an existing SDXL Tag","Find images missing an SDXL Tag","Open conflict checks in Review Center"))
        self.batch_target=QLineEdit(); self.batch_target.setPlaceholderText("SDXL Tag to check, add, or remove")
        self.conflict_rule=QComboBox()
        for rule in self.conflicts.rules:
            self.conflict_rule.addItem(
                rule.label or ", ".join(self.conflicts.effective_tags(rule)),rule.id,
            )
        self.conflict_rule.hide()
        self.batch_required=QLineEdit(); self.batch_required.setPlaceholderText("Optional: must also contain these SDXL Tags (comma-separated)")
        self.batch_excluded=QLineEdit(); self.batch_excluded.setPlaceholderText("Optional: exclude images with these SDXL Tags (comma-separated)")
        self.ignore_memory=QCheckBox("Include images previously marked as not needing this SDXL Tag")
        find_batch=compact_button(QPushButton("Start Batch Review"),primary=True); find_batch.clicked.connect(self.start_batch_review); self.start_batch_review_button=find_batch
        batch_grid.addWidget(self.batch_kind,0,0); batch_grid.addWidget(self.batch_target,0,1); batch_grid.addWidget(self.conflict_rule,0,1)
        batch_grid.addWidget(self.batch_required,1,0); batch_grid.addWidget(self.batch_excluded,1,1)
        batch_grid.addWidget(self.ignore_memory,2,0); batch_grid.addWidget(find_batch,2,1)
        self.batch_kind.currentIndexChanged.connect(self._batch_kind_changed)
        batch_workflow_layout.addWidget(batch)
        self.exception_storage_warning=QLabel()
        self.exception_storage_warning.setObjectName("warningLabel")
        self.exception_storage_warning.setWordWrap(True)
        self.exception_storage_warning.hide()
        batch_workflow_layout.addWidget(self.exception_storage_warning)
        workflow_tabs.addTab(batch_workflow,"Focused review")
        self.image_identity_correction=(
            _DeferredImageIdentityCorrectionWidget(self.state,self)
            if self._defer_expensive_initialization else
            ImageIdentityCorrectionWidget(self.state,self)
        )
        self.image_identity_correction.background_work_finished.connect(
            self._notify_background_work_finished
        )
        self.image_identity_correction.refresh_requested.connect(
            self.refresh_requested.emit
        )
        self.image_identity_correction.edit_image_data.connect(
            self.edit_gallery_image
        )
        self.image_identity_correction.history_restored.connect(self._history_restored)
        self.image_identity_correction.knowledge_changed.connect(
            self.knowledge_changed.emit
        )
        self.image_identity_correction.open_tag_encyclopedia.connect(
            self.open_tag_encyclopedia.emit
        )
        self.image_identity_correction.downstream_updates_changed.connect(
            self.downstream_updates_changed.emit
        )
        self.image_identity_correction.hide()
        self._image_tag_maintenance_dialog=None
        self._build_goal_workflow_pages(workflow_tabs)
        self.training_caption_projection=(
            _DeferredTrainingCaptionProjectionWidget(self.state,self)
            if self._defer_expensive_initialization else
            TrainingCaptionProjectionWidget(self.state,self)
        )
        self.training_caption_projection.edit_image_data.connect(
            self.edit_gallery_image
        )
        self.training_caption_projection.export_requested.connect(
            self._export_training_captions
        )
        self.training_caption_projection.projection_exclusion_requested.connect(
            self._apply_projection_exclusion
        )
        self.training_caption_projection.return_requested.connect(
            self._return_from_training_projection
        )
        self.training_caption_projection.background_work_finished.connect(
            self._notify_background_work_finished
        )
        projection_index=workflow_tabs.addTab(
            self.training_caption_projection,"Review Caption Rules",
        )
        workflow_tabs.setTabVisible(projection_index,False)
        self._compact_workflow_pages=(browse_workflow,batch_workflow,self.training_goal_page,self.generation_goal_page)
        self._unbounded_workflow_maximum_height=workflow_tabs.maximumHeight()
        browser_layout.addWidget(workflow_tabs)
        self.result_label=QLabel("Load a library to browse images."); self.result_label.setObjectName("mutedLabel")
        self.search_activity_status=QLabel(); self.search_activity_status.hide()
        from .components import bind_status_footer
        bind_status_footer(self,self.search_activity_status,title="Dataset results",follow_visibility=True)
        gallery_toolbar=QFrame(); self.gallery_toolbar=gallery_toolbar; gallery_toolbar.setObjectName("galleryToolbar"); gallery_toolbar_layout=QVBoxLayout(gallery_toolbar); gallery_toolbar_layout.setContentsMargins(9,7,9,7)
        result_row=QHBoxLayout(); self.gallery_primary_row=result_row; result_row.setContentsMargins(0,0,0,0); result_row.addWidget(self.result_label,1)
        self.resolution_filter=ResolutionFilter(self.state,self)
        self.resolution_filter.set_review_scope_provider(self._resolution_review_paths)
        self.resolution_filter.changed.connect(self._resolution_filter_changed)
        self.resolution_filter.status_changed.connect(self._resolution_status_changed)
        from .resolution_dialog import resolution_events
        resolution_events().policy_changed.connect(self.resolution_filter.invalidate_policy)
        self.filter_panel.add_filter("Resolution",self.resolution_filter,self.resolution_filter.combo,"")
        self.resolution_filter.changed.connect(self.filter_panel.sync)
        self.inspect_current_button=compact_button(QPushButton("Inspect Image"))
        self.inspect_current_button.setAccessibleDescription(
            "Open the inspector for the currently selected Dataset image."
        )
        self.inspect_current_button.clicked.connect(self.show_current_inspector)
        self.inspect_current_button.hide(); result_row.addWidget(self.inspect_current_button)
        self.selection_header=QWidget(); header_selection=QHBoxLayout(self.selection_header); header_selection.setContentsMargins(0,0,0,0); header_selection.setSpacing(5)
        self.selection_label=QLabel("Batch selection: none"); self.selection_label.setObjectName("mutedLabel")
        self.selection_label.setAccessibleDescription(
            "Images marked for batch actions; the image open in the inspector may not be marked."
        )
        self.select_page_button=QPushButton("Select Page"); self.select_page_button.clicked.connect(self.select_page)
        self.select_all_button=QPushButton("Select All"); self.select_all_button.clicked.connect(self.select_all_results)
        clear_selection=QPushButton("Clear Selection"); clear_selection.clicked.connect(self.clear_selection)
        for button in (self.select_page_button,self.select_all_button,clear_selection): button.setProperty("compact",True)
        header_selection.addWidget(self.selection_label); header_selection.addWidget(self.select_page_button); header_selection.addWidget(self.select_all_button); header_selection.addWidget(clear_selection)
        result_row.addWidget(self.selection_header)
        self.gallery_scale=GalleryScaleControl(self.settings.gallery_page_size,self.settings.gallery_card_width)
        self.gallery_scale.changed.connect(self.gallery_density_changed); self.gallery_density=self.gallery_scale
        result_row.addWidget(self.gallery_density); gallery_toolbar_layout.addLayout(result_row)
        self.gallery_narrow_row=QHBoxLayout(); self.gallery_narrow_row.setContentsMargins(0,0,0,0)
        self.gallery_narrow_row.setSpacing(5); self.gallery_narrow_row.addStretch(1)
        gallery_toolbar_layout.addLayout(self.gallery_narrow_row)
        selection=QWidget(); selection_layout=QVBoxLayout(selection); selection_layout.setContentsMargins(0,0,0,0); selection_layout.setSpacing(4)
        self.selection_group=selection
        self.cancel_projection_button=QPushButton("Cancel Safely"); self.cancel_projection_button.clicked.connect(self.cancel_data_task); self.cancel_projection_button.hide()
        self.cancel_projection_button.setProperty("compact",True)
        bind_status_footer(self,self.cancel_projection_button,title="Dataset task")
        self.resume_training_plan_action=QAction("Resume Training Dataset Draft…",self)
        self.resume_training_plan_action.triggered.connect(self.resume_training_dataset_draft)
        self.preparation_controls=PreparationControls(
            self.settings,inline_options=False,
        )
        from .preparation_impact_dialog import preparation_impact_context
        self.preparation_controls.impact_context_provider=lambda:preparation_impact_context(self)
        self.preparation_controls.requested.connect(self.prepare_images)
        self.preparation_controls.undo_requested.connect(
            self.undo_preparation
        )
        self.history_button=QPushButton("History & Versions…",self)
        self.history_button.setToolTip("Compare recorded saved data for the selected images or the current image.")
        self.history_button.clicked.connect(lambda:self.open_saved_history())
        self.history_button.hide()  # Source command for the shared Dataset ribbon.
        selection_layout.addWidget(self.preparation_controls)
        self.run_ai_selected_button=compact_button(QPushButton("Run AI…"))
        self.run_ai_selected_button.setToolTip(
            "Choose AI Tags, AI Descriptions, or both for the exact checked images. "
            "Unlike Prepare, this AI-only review does not read TXT or Metadata."
        )
        self.run_ai_selected_button.setAccessibleDescription(
            "Opens the shared AI-only review for explicitly checked Dataset images; opening it starts no AI work."
        )
        self.run_ai_selected_button.clicked.connect(self.choose_batch_ai)
        self.run_ai_selected_button.hide()
        self.preparation_controls.action_row.insertWidget(1,self.run_ai_selected_button)
        action_row=QHBoxLayout()
        # Cancellation is presented by the shared task footer.

        self.organization_result_bar=QWidget()
        organization_result_layout=QHBoxLayout(self.organization_result_bar)
        organization_result_layout.setContentsMargins(0,0,0,0)
        self.organization_result_label=QLabel()
        self.organization_result_label.setWordWrap(True)
        self.organization_result_button=compact_button(QPushButton("Review Move History…"))
        self.organization_result_button.clicked.connect(self.undo_file_move)
        organization_result_layout.addWidget(self.organization_result_label,1)
        organization_result_layout.addWidget(self.organization_result_button)
        self.organization_result_bar.hide()
        browser_layout.addWidget(self.organization_result_bar)
        browser_layout.addWidget(gallery_toolbar)
        browser_layout.addWidget(selection)
        self.gallery_scroll=QScrollArea(); self.gallery_scroll.setWidgetResizable(True); self.gallery_scroll.setFrameShape(QFrame.NoFrame)
        self._gallery_viewport=self.gallery_scroll.viewport()
        self._gallery_viewport.installEventFilter(self)
        self.gallery=ImageCardGrid(); self.gallery.chosen.connect(self.inspect_image); self.gallery.marked.connect(self.mark_batch_image)
        self.gallery.selection_replaced.connect(self.replace_batch_selection)
        self.gallery.edit_requested.connect(self.edit_gallery_image)
        self.gallery.force_ai_requested.connect(self.force_ai_from_gallery)
        self.gallery.add_tag_requested.connect(self.add_tag_to_images)
        self.gallery.file_action_requested.connect(self._gallery_file_action)
        self.gallery_scroll.setWidget(self.gallery); browser_layout.addWidget(self.gallery_scroll,1)
        self.empty_results_panel=EmptyStatePanel(
            "Checking which images are available in this preparation stage.",
            "No images in this view",minimum_height=240,
        )
        self.clear_results_filters_button=self.empty_results_panel.add_action(
            "Clear Search and Filters",self.clear_filters,
        )
        self.browse_stage_button=self.empty_results_panel.add_action(
            "Browse Another Stage",self._browse_recovery_stage,
        )
        self.refresh_results_button=self.empty_results_panel.add_action(
            "Refresh Libraries",lambda:self.refresh_requested.emit(),primary=False,
        )
        self._recovery_stage=None
        self.empty_results_panel.hide()
        browser_layout.addWidget(self.empty_results_panel,1)
        self.no_library_panel=EmptyStatePanel(
            "Choose an image library to browse and review images here.",
            "No image libraries loaded",minimum_height=240,
        )
        self.choose_libraries_button=self.no_library_panel.add_action(
            "Choose Libraries…",lambda:self.change_library.emit(),
        )
        self.no_library_panel.hide()
        browser_layout.addWidget(self.no_library_panel,1)
        self.empty_library_panel=EmptyStatePanel(
            "Add and review images in Ingest, then move them into a library to see them here.",
            "No images in the included libraries",minimum_height=240,
        )
        self.open_ingest_button=self.empty_library_panel.add_action(
            "Open Ingest",lambda:self.open_ingest_requested.emit(),
        )
        self.refresh_empty_libraries_button=self.empty_library_panel.add_action(
            "Refresh Libraries",lambda:self.refresh_requested.emit(),
        )
        self.empty_choose_libraries_button=self.empty_library_panel.add_action(
            "Choose Libraries…",lambda:self.change_library.emit(),primary=False,
        )
        self.empty_library_panel.hide()
        browser_layout.addWidget(self.empty_library_panel,1)
        self.batch_actions=QGroupBox("Review this page"); batch_actions_layout=QHBoxLayout(self.batch_actions)
        self.batch_instruction=QLabel(); self.batch_instruction.setWordWrap(True)
        self.apply_batch_button=QPushButton("Apply Page Decisions"); self.apply_batch_button.setProperty("primary",True); self.apply_batch_button.clicked.connect(self.apply_batch_page)
        clear_batch=QPushButton("Exit Batch Review"); clear_batch.clicked.connect(self.end_batch_review)
        self.undo_batch_button=QPushButton("Review Batch Changes…"); self.undo_batch_button.clicked.connect(self.undo_batch); self.undo_batch_button.setEnabled(False)
        batch_actions_layout.addWidget(self.batch_instruction,1); batch_actions_layout.addWidget(self.apply_batch_button); batch_actions_layout.addWidget(self.undo_batch_button); batch_actions_layout.addWidget(clear_batch)
        self.batch_actions.hide(); browser_layout.addWidget(self.batch_actions)
        pager=QHBoxLayout(); self.previous=QPushButton("Previous Page"); self.next=QPushButton("Next Page")
        self.page_label=QLabel("No results"); self.page_label.setAlignment(Qt.AlignCenter)
        self.previous.clicked.connect(lambda:self.change_page(-1)); self.next.clicked.connect(lambda:self.change_page(1))
        self.previous.setProperty("compact",True); self.next.setProperty("compact",True)
        pager.addWidget(self.previous); pager.addWidget(self.page_label,1); pager.addWidget(self.next); browser_layout.addLayout(pager)
        splitter.addWidget(browser)

        inspector=QWidget(); self.inspector=inspector; inspector.setMinimumWidth(0)
        inspect_layout=QVBoxLayout(inspector); inspect_layout.setContentsMargins(8,0,0,0)
        inspect_header=QHBoxLayout(); inspect_header.setContentsMargins(0,0,0,0)
        inspect_title=QLabel("Inspect image"); inspect_title.setObjectName("sectionTitle"); inspect_header.addWidget(inspect_title)
        inspect_header.addStretch(1)
        self.inspector_back_button=compact_button(QPushButton("Back to Images"))
        self.inspector_back_button.setAccessibleDescription(
            "Return to the Dataset gallery without changing the current image or selection."
        )
        self.inspector_back_button.clicked.connect(self.show_dataset_browser)
        self.inspector_back_button.hide(); inspect_header.addWidget(self.inspector_back_button)
        inspect_layout.addLayout(inspect_header)
        self.image_name=QLabel("Select an image from the gallery"); self.image_name.setWordWrap(True); inspect_layout.addWidget(self.image_name)
        self.tabs=QTabWidget(); self.overview=ImageKnowledgeSummaryWidget()
        self.overview.edit_requested.connect(
            lambda:self.edit_gallery_image(str(self.current_path))
            if self.current_path is not None else None
        )
        self.quick_review=QWidget(); quick_layout=QVBoxLayout(self.quick_review); quick_layout.setContentsMargins(6,6,6,6)
        self.quick_review_status=QLabel("No findings to review"); self.quick_review_status.setObjectName("mutedLabel"); quick_layout.addWidget(self.quick_review_status)
        self.quick_review_items=QTreeWidget(); self.quick_review_items.setHeaderLabels(("Information","Kind","Found in")); self.quick_review_items.setRootIsDecorated(False); self.quick_review_items.setAlternatingRowColors(True)
        quick_header=self.quick_review_items.header(); quick_header.setSectionResizeMode(0,QHeaderView.Stretch); quick_header.setSectionResizeMode(1,QHeaderView.ResizeToContents); quick_header.setSectionResizeMode(2,QHeaderView.ResizeToContents)
        quick_layout.addWidget(self.quick_review_items,1)
        self.quick_review_open=QPushButton("Open Quick Review"); self.quick_review_open.setProperty("primary",True); self.quick_review_open.clicked.connect(self.open_data_editor); self.quick_review_open.setEnabled(False); quick_layout.addWidget(self.quick_review_open,0,Qt.AlignRight)
        self.training_preview=_TrainingInspectorWidget()
        self.issue_tab=QWidget(); issue_tab_layout=QVBoxLayout(self.issue_tab); issue_tab_layout.setContentsMargins(0,0,0,0)
        self.issue_details=self._text_tab(); issue_tab_layout.addWidget(self.issue_details,1)
        self.issue_actions=QWidget(); self.issue_actions_layout=QVBoxLayout(self.issue_actions); self.issue_actions_layout.setContentsMargins(0,6,0,0)
        self.issue_action_summary=QLabel(); self.issue_action_summary.setWordWrap(True); self.issue_action_summary.setObjectName("mutedLabel")
        self.review_all_issues_button=QPushButton("Review All Issues…"); self.review_all_issues_button.setProperty("primary",True); self.review_all_issues_button.clicked.connect(self.review_all_issues)
        self.issue_actions_layout.addWidget(self.issue_action_summary)
        self.issue_actions_layout.addWidget(self.review_all_issues_button,0,Qt.AlignRight)
        self.issue_actions_scroll=QScrollArea(); self.issue_actions_scroll.setWidgetResizable(True); self.issue_actions_scroll.setFrameShape(QFrame.NoFrame)
        self.issue_actions_scroll.setMaximumHeight(360); self.issue_actions_scroll.setWidget(self.issue_actions)
        issue_tab_layout.addWidget(self.issue_actions_scroll)
        self._issue_action_rows=[]; self._issue_action_buttons=[]; self.issue_actions.hide()
        self.issue_actions_scroll.hide()
        self.tabs.addTab(self.overview,"Summary")
        self.tabs.addTab(self.quick_review,"Quick Review")
        self.tabs.addTab(self.training_preview,"Training"); self.tabs.addTab(self.issue_tab,"Issues")
        inspect_layout.addWidget(self.tabs,2)
        editor_hint=QLabel("Double-click the card, press Enter, or use Image Actions to edit image data."); self.editor_hint=editor_hint; editor_hint.setObjectName("mutedLabel"); editor_hint.setWordWrap(True)
        inspect_layout.addWidget(editor_hint)
        image_actions=QToolButton(); self.inspector_actions=image_actions
        image_actions.setText("Image Actions"); image_actions.setPopupMode(QToolButton.InstantPopup)
        image_actions.setProperty("compact",True)
        image_actions.setAccessibleName("Actions for the inspected image")
        image_actions.setAccessibleDescription(
            "Edit, resize, back up, open, organize, or recover the inspected Dataset image."
        )
        actions=QMenu(image_actions)
        actions.setToolTipsVisible(True)
        guided=actions.addAction("Choose How to Change This Image…")
        guided.setToolTip(
            "Compare faithful enlargement, AI detail enlargement, a close variation, and a substantial change before opening a separate review."
        )
        guided.triggered.connect(
            lambda:self._gallery_file_action((self.current_path,),"change_image")
            if self.current_path is not None else None
        )
        actions.addSection("Direct actions")
        edit=actions.addAction("Edit Image Data…")
        edit.triggered.connect(
            lambda:self.edit_gallery_image(str(self.current_path))
            if self.current_path is not None else None
        )
        resize_current=actions.addAction("Resize This Image…")
        resize_current.setToolTip(
            "Compare faithful resize, local AI upscale and a separate generative-upscale draft. "
            "The original image stays unchanged."
        )
        resize_current.triggered.connect(
            lambda:self._gallery_file_action((self.current_path,),"resize")
            if self.current_path is not None else None
        )
        remix_current=actions.addAction("Remix This Image…")
        remix_current.setToolTip(
            "Open a separate source-bound Generate draft to change this image. "
            "The original stays unchanged; nothing runs until you review and launch it."
        )
        remix_current.triggered.connect(
            lambda:self._gallery_file_action((self.current_path,),"remix")
            if self.current_path is not None else None
        )
        animate_current=actions.addAction("Animate This Image…")
        animate_current.setToolTip(
            "Open a separate video draft using this image as its source. "
            "The original stays unchanged; nothing runs until you review and launch it."
        )
        animate_current.triggered.connect(
            lambda:self._gallery_file_action((self.current_path,),"animate_this_image")
            if self.current_path is not None else None
        )
        image_setup=actions.addAction("Create Generation Setup from Image…")
        image_setup.setToolTip("Reuse selected saved Tags or Descriptions and recorded generator settings without copying this image.")
        image_setup.triggered.connect(
            lambda:self._open_image_generation_setup(self.current_path)
        )
        undo=actions.addAction("Review Saved Edit…"); undo.triggered.connect(self.undo_edit); self.undo_button=undo; undo.setEnabled(False)
        actions.addSeparator()
        sync_current=actions.addAction("Back Up Tags to Metadata…"); sync_current.triggered.connect(lambda:self.sync_selected_data([self.current_path] if self.current_path else [],preferred_targets=("metadata",)))
        exceptions=actions.addAction("Manage Exceptions…"); exceptions.triggered.connect(self.manage_exceptions); self.manage_exceptions_button=exceptions
        actions.addSeparator()
        external=actions.addAction("Open Externally"); external.triggered.connect(self.open_external)
        reorganize=actions.addAction("Reorganize Image…"); reorganize.triggered.connect(self.reorganize_current)
        problems=actions.addAction("Move to Problems…"); problems.triggered.connect(self.move_to_problems)
        recycle=actions.addAction("Move to Recycle Bin…"); recycle.triggered.connect(self.move_current_to_recycle)
        actions.addSeparator()
        undo_move=actions.addAction("Review Move History…"); undo_move.triggered.connect(self.undo_file_move); self.undo_move_button=undo_move
        undo_move.setEnabled(False)
        image_actions.setMenu(actions)
        self.inspector_external_button=external
        self.inspector_write_buttons=(reorganize,problems,recycle,exceptions,sync_current)
        inspect_layout.addWidget(image_actions,0,Qt.AlignLeft)
        splitter.addWidget(inspector)
        splitter.setStretchFactor(0,7); splitter.setStretchFactor(1,3); splitter.setSizes((1200,520))
        self.splitter=splitter; self.current_path=None; self.inspector.hide(); self.selection_group.hide(); self.selection_header.hide()
        self._dataset_narrow_layout=False; self._narrow_inspector_open=False
        self._gallery_toolbar_narrow=False
        self._pre_full_workflow_visibility=None
        self._training_projection_return_widget=None
        self.workflow_tabs.currentChanged.connect(self._workflow_changed)
        DatasetPage._sync_workflow_geometry(self)
        self._sync_exception_storage_state()
        if getattr(self.state,"dataset_preparation_work",None):
            self._restore_preparation_request_queue()
        if getattr(self,"_defer_expensive_initialization",False):
            QTimer.singleShot(0,self._start_conflict_preparation)

    def _restore_preparation_request_queue(self):
        from .dataset_preparation_queue import DatasetPreparationQueue
        if getattr(self,"_preparation_request_queue",None) is None:
            queue=DatasetPreparationQueue(self)
            self._preparation_request_queue=queue
            self.header.actions.addWidget(queue.button)
            self.update_selection_controls()

    def _start_conflict_preparation(self):
        if self._conflict_task is not None:
            return
        self._conflicts_ready=False
        knowledge=self.state.knowledge
        task=CoordinatedCallTask(
            self.state.jobs,"Preparing conflict checks",
            lambda _job:ConflictRuleEngine.load(
                DEFAULTS_DIR/"conflicts.json",tags=knowledge,
            ),
            resource="cpu",key="system-knowledge:conflict-rules",
        )
        self._conflict_task=task
        task.signals.complete.connect(
            lambda result,owner=task:self._conflict_preparation_complete(
                owner,result,knowledge,
            )
        )
        task.signals.failed.connect(
            lambda message,owner=task:self._conflict_preparation_failed(
                owner,message,
            )
        )
        task.signals.cancelled.connect(
            lambda owner=task:self._conflict_preparation_failed(
                owner,"Conflict preparation was cancelled safely.",
            )
        )
        task.start()

    def _conflict_preparation_complete(self,task,result,knowledge):
        if task is not self._conflict_task:
            return
        self._conflict_task=None
        if knowledge is not self.state.knowledge:
            self._conflict_preparation_failed(
                None,"System Knowledge changed while conflict checks were preparing.",
            )
            return
        self.conflicts=result; self._conflict_error=""; self._conflicts_ready=True
        selected=self.conflict_rule.currentData()
        blocker=self.conflict_rule.blockSignals(True)
        self.conflict_rule.clear()
        for rule in self.conflicts.rules:
            self.conflict_rule.addItem(
                rule.label or ", ".join(self.conflicts.effective_tags(rule)),
                rule.id,
            )
        if selected is not None:
            self.conflict_rule.setCurrentIndex(max(
                0,self.conflict_rule.findData(selected),
            ))
        self.conflict_rule.blockSignals(blocker)
        if self.current_record is not None and self.current_path is not None:
            self._populate_image_inspector(
                self.current_record,self.current_path,
                self._record_inspection_generation,refresh_training=False,
            )
        if self._shutdown_requested:
            self._notify_background_work_finished()

    def _conflict_preparation_failed(self,task,message):
        if task is not None and task is not self._conflict_task:
            return
        self._conflict_task=None
        self._conflict_error=str(message or "Conflict checks are unavailable.")
        self._conflicts_ready=False
        if self._shutdown_requested:
            self._notify_background_work_finished()

    def _build_goal_workflow_pages(self,tabs):
        """Goal entry points share the existing planners and their exact scopes."""
        self.training_goal_page=QWidget()
        training=QVBoxLayout(self.training_goal_page); training.setContentsMargins(8,8,8,8); training.setAlignment(Qt.AlignTop)
        intro=QLabel("Choose images and a target, review captions, build a dataset, then configure training. Captions describe existing images so a model can learn from them.")
        intro.setWordWrap(True); training.addWidget(intro)
        self.training_goal_scope=QLabel("Choose a library to prepare training data.")
        self.training_goal_scope.setWordWrap(True); training.addWidget(self.training_goal_scope)
        actions=QHBoxLayout()
        self.training_plan_button=compact_button(QPushButton("Plan Training Dataset…"),primary=True)
        self.training_plan_button.clicked.connect(self.plan_training_dataset_scope)
        self.training_resume_button=compact_button(QPushButton("Open Training"))
        self.training_resume_button.clicked.connect(self.open_training_section)
        self.training_rules_button=compact_button(QPushButton("Review Caption Rules…"))
        self.training_rules_button.setEnabled(False)
        self.training_rules_button.clicked.connect(self.review_training_caption_rules)
        for button in (self.training_plan_button,self.training_resume_button,self.training_rules_button):
            actions.addWidget(button)
        actions.addStretch(1); training.addLayout(actions)
        concepts=QHBoxLayout()
        self.concept_groups_button=compact_button(QPushButton("Training Collections…"),primary=True)
        self.concept_groups_button.clicked.connect(self.open_concept_groups)
        self.concept_add_button=compact_button(QPushButton("Add to Collection…"))
        self.concept_add_button.setEnabled(False)
        self.concept_add_button.clicked.connect(self.add_selected_to_concept)
        concepts.addWidget(self.concept_groups_button); concepts.addWidget(self.concept_add_button); concepts.addStretch(1)
        training.addLayout(concepts)
        concept_note=QLabel("Create a collection, add Dataset images, then build training data from that collection. Each plan keeps its own frozen image list.")
        concept_note.setWordWrap(True); concept_note.setObjectName("mutedLabel"); training.addWidget(concept_note)
        tabs.addTab(self.training_goal_page,"Prepare for training")

        self.generation_goal_page=QWidget()
        generation=QVBoxLayout(self.generation_goal_page); generation.setContentsMargins(8,8,8,8); generation.setAlignment(Qt.AlignTop)
        intro=QLabel("Create an image from a new goal, or use selected images' recorded settings and reviewed knowledge. No training is required. Selecting images does not send their pixels as image-to-image input.")
        intro.setWordWrap(True); generation.addWidget(intro)
        self.generation_goal_scope=QLabel("Start a new goal without selecting an image.")
        self.generation_goal_scope.setWordWrap(True); generation.addWidget(self.generation_goal_scope)
        actions=QHBoxLayout()
        self.generation_selection_button=compact_button(QPushButton("Use Image Settings…"),primary=True)
        self.generation_selection_button.setEnabled(False)
        self.generation_selection_button.clicked.connect(self.plan_generation_from_selection)
        self.generation_new_button=compact_button(QPushButton("Open Generate"))
        self.generation_new_button.clicked.connect(self.plan_new_generation)
        actions.addWidget(self.generation_selection_button); actions.addWidget(self.generation_new_button); actions.addStretch(1)
        generation.addLayout(actions)
        note=QLabel("Plan, save and export offline. Submission needs a supported running local generator; verified results return through Ingest.")
        note.setWordWrap(True); note.setObjectName("mutedLabel"); generation.addWidget(note)
        tabs.addTab(self.generation_goal_page,"Generate images")

    def review_training_caption_rules(self):
        scope="selected" if self.selected_paths() else "current_filter"
        return self.open_function("dataset.training_captions.preview",{
            "origin":"dataset_training_preparation","scope":scope,
        })

    def _show_caption_rule_page(self):
        tabs=self.workflow_tabs
        if isinstance(tabs,QTabWidget):
            tabs.setTabVisible(tabs.indexOf(self.training_caption_projection),True)
        tabs.setCurrentWidget(self.training_caption_projection)

    def _sync_workflow_geometry(self):
        """Give spare height to image results, not compact workflow controls."""
        current=self.workflow_tabs.currentWidget()
        compact=current in getattr(self,"_compact_workflow_pages",())
        if not compact:
            self.workflow_tabs.setMaximumHeight(
                getattr(self,"_unbounded_workflow_maximum_height",16777215),
            )
            self.workflow_tabs.setSizePolicy(
                QSizePolicy.Expanding,QSizePolicy.Expanding,
            )
            return
        layout=current.layout()
        content_height=(
            layout.sizeHint().height() if layout is not None
            else max(0,current.sizeHint().height())
        )
        if (layout is not None and layout.hasHeightForWidth()
                and current in (getattr(self,"training_goal_page",None),getattr(self,"generation_goal_page",None))):
            content_height=layout.totalHeightForWidth(current.width())
        tab_height=max(0,self.workflow_tabs.tabBar().sizeHint().height())
        self.workflow_tabs.setMaximumHeight(content_height+tab_height+10)
        self.workflow_tabs.setSizePolicy(
            QSizePolicy.Expanding,QSizePolicy.Maximum,
        )

    def _workflow_changed(self,*_):
        current=self.workflow_tabs.currentWidget()
        # The detailed diagnostic is a contextual training step, not a default goal.
        if current is not self.training_caption_projection and isinstance(self.workflow_tabs,QTabWidget):
            self.workflow_tabs.setTabVisible(self.workflow_tabs.indexOf(self.training_caption_projection),False)
        if isinstance(current,(
            _DeferredImageIdentityCorrectionWidget,
            _DeferredTrainingCaptionProjectionWidget,
        )):
            current.activate()
        full_workflow=(
            current in (
                self.image_identity_correction,
                self.training_caption_projection,
            )
        )
        DatasetPage._sync_workflow_geometry(self)
        self.browser_layout.setStretchFactor(
            self.workflow_tabs,1 if full_workflow else 0,
        )
        self.browser_layout.setStretchFactor(
            self.gallery_scroll,0 if full_workflow else 1,
        )
        self.browser_layout.setStretchFactor(
            self.no_library_panel,0 if full_workflow else 1,
        )
        self.browser_layout.setStretchFactor(
            self.empty_library_panel,0 if full_workflow else 1,
        )
        browse_widgets=(
            self.selection_group,self.gallery_toolbar,self.gallery_scroll,
            self.no_library_panel,self.empty_library_panel,
            self.batch_actions,self.previous,self.page_label,self.next,
            self.inspector,
        )
        if full_workflow:
            self.workflow_tabs.show()
            if self._pre_full_workflow_visibility is None:
                self._pre_full_workflow_visibility={
                    widget:not widget.isHidden() for widget in browse_widgets
                }
            for widget in browse_widgets:
                widget.hide()
            return
        previous=self._pre_full_workflow_visibility
        self._pre_full_workflow_visibility=None
        if previous is not None:
            for widget,visible in previous.items():
                widget.setVisible(visible)
        self.render_page()

    def _text_tab(self):
        widget=QTextEdit(); widget.setReadOnly(True); return widget

    def show_help(self):
        self.header.show_help()

    def browse_problems(self):
        if self._problems_dialog is not None:
            self._problems_dialog.show(); self._problems_dialog.raise_(); self._problems_dialog.activateWindow(); return
        dialog=ProblemsDialog(self.state,self); self._problems_dialog=dialog; dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.changed.connect(self.refresh_requested); dialog.force_ai_requested.connect(self.force_ai_from_gallery)
        dialog.library_paths_changed.connect(self.library_paths_changed.emit)
        dialog.content_ratings_requested.connect(self.content_ratings_requested.emit)
        dialog.edit_tag_requested.connect(self.edit_tag_requested)
        dialog.create_tag_requested.connect(self.create_tag_requested)
        dialog.destroyed.connect(lambda *_:setattr(self,"_problems_dialog",None)); dialog.show()

    def open_recycle_bin(self, *, record_ids=()):
        if self._recycle_dialog is not None:
            if record_ids:self._recycle_dialog.focus_record_ids(record_ids)
            self._recycle_dialog.show(); self._recycle_dialog.raise_(); self._recycle_dialog.activateWindow(); return
        dialog=RecycleBinDialog(self.state,self); self._recycle_dialog=dialog
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.changed.connect(self.refresh_requested)
        dialog.library_paths_changed.connect(self.library_paths_changed.emit)
        dialog.destroyed.connect(lambda *_:setattr(self,"_recycle_dialog",None))
        if record_ids:dialog.focus_record_ids(record_ids)
        dialog.show()

    def open_duplicate_finder(self,scope_kind="current_filter"):
        if not isinstance(scope_kind,str):scope_kind="current_filter"
        if scope_kind=="current_filter" and not DatasetPage._dataset_search_results_ready(self):
            return
        if self._duplicate_dialog is not None:
            self._duplicate_dialog.show(); self._duplicate_dialog.raise_()
            self._duplicate_dialog.activateWindow(); return
        dialog=DuplicateFinderDialog(
            self.state,self,current_paths=self.filtered,
            selected_paths=self.selected_paths(),
            current_library_id=(
                str(getattr(self.state,"selected_library_id","") or "")
                or str(getattr(self.settings,"active_library_id","") or "")
            ),
        )
        index=dialog.scope.findData(scope_kind)
        if index>=0:dialog.scope.setCurrentIndex(index)
        self._duplicate_dialog=dialog; dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.edit_requested.connect(self.edit_gallery_image)
        dialog.organize_requested.connect(self._organize_duplicate_image)
        dialog.changed.connect(self._duplicate_files_changed)
        dialog.destroyed.connect(lambda *_:setattr(self,"_duplicate_dialog",None))
        dialog.show()

    def _organize_duplicate_image(self,value: str):
        path=Path(value)
        if self.settings.library_registry().profile_for_path(path) is None:
            QMessageBox.information(
                self,"Open the owning image workspace",
                "This image is outside a named Dataset library. Use its Ingest "
                "or folder workflow to organize it; no file changed.",
            )
            return
        try:record=ImageRecord(path)
        except Exception as error:
            QMessageBox.warning(self,"Image data unavailable",str(error)); return
        self.current_path=path; self.current_record=record; self.reorganize_current()

    def _duplicate_files_changed(self,values):
        moved={str(Path(value)) for value in values}
        if not moved:return
        self.batch_selected.difference_update(moved)
        self.filtered=[path for path in self.filtered if str(path) not in moved]
        if self.current_path is not None and str(self.current_path) in moved:
            self.current_path=None; self.current_record=None; self.inspector.hide()
        self.render_page(); self.refresh_requested.emit()

    def current_stage(self) -> str:
        tabs=getattr(self,"stage_tabs",None)
        if tabs is None:
            return "all"
        return str(tabs.tabData(tabs.currentIndex()) or "complete")

    @staticmethod
    def _stage_filter_options(stage: str):
        if stage=="partial":
            return (
                ("All partially prepared","all"),
                ("Image Knowledge incomplete","incomplete"),
                ("AI findings to review","ai"),
                ("Needs fact review","review"),
                ("Images with Problems","problem"),
            )
        if stage=="complete":
            return (("All complete images","all"),)
        return (("All unprepared images","all"),)

    def _sync_dataset_filter_options(self) -> None:
        stage=DatasetPage.current_stage(self)
        options=DatasetPage._stage_filter_options(stage)
        current=str(self.state_filter.currentData() or "all")
        blocker=QSignalBlocker(self.state_filter)
        self.state_filter.clear()
        for label,key in options:
            self.state_filter.addItem(label,key)
        index=self.state_filter.findData(current)
        self.state_filter.setCurrentIndex(max(0,index))
        del blocker
        self.state_filter_group.setVisible(
            bool(getattr(self.dataset,"images",()) if self.dataset else ())
            and len(options)>1
        )
        has_images=bool(
            getattr(self.dataset,"images",()) if self.dataset else ()
        )
        projection_group=getattr(self,"projection_filter_group",None)
        if projection_group is not None:
            projection_group.setVisible(
                has_images and stage in {"complete","all"}
            )
        if stage not in {"complete","all"} and str(
            self.projection_filter.currentData() or "all"
        )!="all":
            projection_blocker=QSignalBlocker(self.projection_filter)
            self.projection_filter.setCurrentIndex(0)
            del projection_blocker
        self._sync_dataset_filter_emphasis()
        if hasattr(self,"filter_panel"):
            self.filter_panel.sync()

    def _dataset_stage_changed(self,*_) -> None:
        self.page=0
        self._sync_dataset_filter_options()
        self.apply_filters()

    def _sync_dataset_filter_emphasis(self,*_) -> None:
        group=getattr(self,"state_filter_group",None)
        if group is None:
            return
        active=str(self.state_filter.currentData() or "all")!="all"
        if bool(group.property("active"))==active:
            return
        group.setProperty("active",active)
        style=group.style(); style.unpolish(group); style.polish(group)

    def _workflow_index_managers(self):
        dataset=getattr(self,"dataset",None)
        if dataset is None:
            return ()
        if hasattr(dataset,"scoped_profile_managers"):
            managers=[
                manager for _profile,manager in dataset.scoped_profile_managers()
                if getattr(manager,"index",None) is not None
            ]
        else:
            managers=[dataset] if getattr(dataset,"index",None) is not None else []
        return tuple({id(manager):manager for manager in managers}.values())

    def open_media_library(self):
        from .media_library_dialog import open_media_library
        dataset=self.dataset
        if hasattr(dataset,"scoped_profile_managers"):
            roots=[(profile.name,manager.root) for profile,manager in dataset.scoped_profile_managers()
                   if manager.root is not None]
        else:
            root=getattr(dataset,"root",None)
            roots=[("Dataset",root)] if root is not None else []
        return open_media_library(self,roots,operation_owner="dataset")

    def _restore_persisted_workflow_states(self,*,current_context=None):
        managers=self._workflow_index_managers()
        self._readiness_context_fingerprint=(
            self.workflow_states.context_fingerprint()
            if current_context is None else current_context
        )
        self._workflow_cache_notice=""
        if not managers:
            return {}
        wanted=set(getattr(self.dataset,"images",()))
        restored={}; counts={}; compatible_contexts=None
        for manager in managers:
            payloads=manager.index.cached_workflow_states(
                context_fingerprint=self._readiness_context_fingerprint,
            )
            summary=getattr(manager.index,"workflow_cache_summary",{})
            manager_paths=wanted & set(getattr(manager,"images",wanted))
            if manager_paths-set(payloads):
                if compatible_contexts is None:
                    compatible=getattr(self.workflow_states,"compatible_context_fingerprints",None)
                    compatible_contexts=tuple(compatible()) if callable(compatible) else ()
                for old_context in compatible_contexts:
                    legacy=manager.index.cached_workflow_states(context_fingerprint=old_context)
                    payloads={**legacy,**payloads}
            if isinstance(summary,dict):
                for key,count in summary.items():
                    counts[key]=counts.get(key,0)+count
            rating_validator=getattr(self.workflow_states,"cached_rating_matches",None)
            input_reader=getattr(manager.index,"workflow_confirmed_tags",None)
            legacy=[path for path,payload in payloads.items() if "rating_tags" not in payload]
            rating_inputs=input_reader(legacy) if legacy and callable(input_reader) else {}
            for path,payload in payloads.items():
                if callable(rating_validator) and not rating_validator(payload,rating_inputs.get(path)):
                    continue
                workflow=ImageWorkflowState.from_cache_dict(payload)
                if workflow is not None and path in wanted:
                    restored[path]=((None,None),workflow)
        for path,value in restored.items():
            self._readiness_cache.setdefault(path,value)
        if counts.get("unavailable"):
            self._workflow_cache_notice="The saved status cache is unavailable."
        elif counts.get("context_changed"):
            self._workflow_cache_notice="Checking saved status against current rules and knowledge."
        elif counts.get("file_changed"):
            self._workflow_cache_notice="Image files or their saved data changed."
        elif counts.get("missing"):
            self._workflow_cache_notice="Some images do not have saved status yet."
        return restored

    @staticmethod
    def _workflow_family_fingerprint(path: Path) -> tuple[int,...]:
        values=[]
        for candidate in (
            Path(path),Path(path).with_suffix(".txt"),
            Path(path).with_suffix(".tags.json"),
        ):
            try:
                stat=candidate.stat(); values.extend((stat.st_size,stat.st_mtime_ns))
            except OSError:
                values.extend((-1,-1))
        return tuple(values)

    def _persist_workflow_states(
        self,values,*,expected_fingerprints=None,
    ) -> bool:
        if not values:
            return True
        context=str(getattr(self,"_readiness_context_fingerprint","") or "")
        if not context:
            context=self.workflow_states.context_fingerprint()
            self._readiness_context_fingerprint=context
        grouped={}
        for path,(_signatures,workflow) in values.items():
            manager=(
                self.dataset.manager_for_path(path)
                if hasattr(self.dataset,"manager_for_path") else self.dataset
            )
            index=getattr(manager,"index",None)
            if index is not None:
                grouped.setdefault(index,{})[Path(path)]=workflow.to_cache_dict()
        try:
            for index,payloads in grouped.items():
                index.record_workflow_states(
                    payloads,context_fingerprint=context,
                    expected_fingerprints={
                        path:expected_fingerprints[path]
                        for path in payloads
                        if expected_fingerprints and path in expected_fingerprints
                    },
                )
        except (OSError,sqlite3.DatabaseError):
            # This database is only a rebuildable performance cache. Keep the
            # correct in-memory result and allow the next refresh to retry.
            return False
        return True

    def _invalidate_workflow_work(self) -> None:
        self._filter_generation+=1
        task=getattr(self,"_filter_state_task",None)
        if task is not None:
            task.cancel()
            self._filter_state_task=None
            self._filter_state_automatic=False
            self.state.finish_long_operation("dataset")
            self.cancel_projection_button.hide()

    def _invalidate_editor_load(self) -> None:
        self._editor_load_generation+=1
        task=getattr(self,"_editor_load_task",None)
        if task is not None:
            task.cancel()
            self._editor_load_task=None

    def _workflow_persistence_targets(self,paths):
        """Freeze index ownership before a background classification starts."""
        dataset=self.dataset
        targets={}
        for path in paths:
            manager=(
                dataset.manager_for_path(path)
                if hasattr(dataset,"manager_for_path") else dataset
            )
            index=getattr(manager,"index",None)
            if index is not None:
                targets[Path(path)]=index
        return targets

    @staticmethod
    def _dataset_binding_scope(dataset):
        if dataset is None:
            return None
        provider=getattr(dataset,"scoped_profile_managers",None)
        roots=(
            tuple(sorted((str(profile.stable_id),str(manager.root))
                         for profile,manager in provider()))
            if callable(provider) else (("",str(getattr(dataset,"root",None))),)
        )
        return roots,tuple(getattr(dataset,"explicit_scope_library_ids",()))

    def set_dataset(self,dataset):
        scope=DatasetPage._dataset_binding_scope(dataset)
        preparation=getattr(self,"_initial_preparation",None)
        self._initial_preparation=None
        current_context=(
            preparation.context_fingerprint
            if preparation is not None and preparation.is_current(self.state)
            else self.workflow_states.context_fingerprint()
        )
        same_binding=(
            dataset is not None and dataset is getattr(self,"dataset",None)
            and scope==getattr(self,"_bound_dataset_scope",None)
            and current_context==getattr(self,"_readiness_context_fingerprint","")
        )
        images=set(getattr(dataset,"images",()))
        changed=set(getattr(dataset,"changed_images",())) if dataset else set()
        previous_images=getattr(self,"_bound_dataset_images",set())
        self._bound_dataset_scope=scope
        self._bound_dataset_images=images
        DatasetPage._invalidate_dataset_search(self)
        if same_binding:
            # Inventory has already validated file families. A stage change is
            # not a new library; retain in-flight and memory-only work unless
            # its actual inputs or membership changed.
            removed=previous_images-images
            restored=self._restore_persisted_workflow_states(
                current_context=current_context,
            )
            # Derived index rebuilds also mark rows changed. Reuse matching
            # current projections; the dirty hint alone is not invalidity.
            invalid=removed | {
                path for path in changed
                if path not in restored or path not in self._readiness_cache
                or restored[path][1]!=self._readiness_cache[path][1]
            }
            if invalid or images!=previous_images:
                self._invalidate_workflow_work()
            if getattr(self,"current_path",None) in invalid:
                self._invalidate_editor_load()
            for cache in (self.audit_issues,self._readiness_cache,self._inspection_detail_cache):
                for path in invalid:
                    cache.pop(path,None)
            self.batch_selected.intersection_update(str(path) for path in images)
        else:
            self._dataset_scope_generation=getattr(self,"_dataset_scope_generation",0)+1
            self._invalidate_workflow_work()
            self._invalidate_editor_load()
            self.audit_issues={}; self._readiness_cache=RevisionDict(); self._inspection_detail_cache={}
            self.batch_selected.clear()
        self.dataset=dataset
        projection=getattr(self,"training_caption_projection",None)
        if projection is not None:
            projection.invalidate_after_authority_refresh(
                (changed | (images-previous_images)) if same_binding else images
            )
        exceptions=getattr(self,"exceptions",None)
        if exceptions is not None:
            exceptions.load()
            self._exception_storage_error=exceptions.source_error
            try:
                exceptions.bind_legacy_library(
                    str(getattr(dataset,"preferred_library_id","") or "")
                )
            except (OSError,RuntimeError,TypeError,ValueError) as exc:
                self._exception_storage_error=str(exc)
        profiles=(
            [profile for profile,_manager in dataset.scoped_profile_managers()]
            if dataset is not None and hasattr(dataset,"scoped_profile_managers") else []
        )
        if not profiles and dataset is not None and getattr(dataset,"root",None):
            legacy_profile=self.settings.library_profile_for_path(dataset.root)
            if legacy_profile is not None:
                profiles=[legacy_profile]
        explicit=tuple(getattr(dataset,"explicit_scope_library_ids",())) if dataset else ()
        if not profiles:
            context=""
        elif not explicit:
            collection_label="collection" if len(profiles)==1 else "collections"
            context=(
                f"All Libraries — {len(profiles):,} peer {collection_label} — "
                f"{len(getattr(dataset,'images',())):,} image(s)"
            )
        elif len(profiles)==1:
            profile=profiles[0]
            context=(
                f"{profile.name} — {profile.display_role} — "
                f"{len(getattr(dataset,'images',())):,} image(s)"
            )
        else:
            collection_label="collection" if len(profiles)==1 else "collections"
            context=(
                f"Filtered Libraries — {len(profiles):,} peer {collection_label} — "
                f"{len(getattr(dataset,'images',())):,} image(s)\n"
                +", ".join(profile.name for profile in profiles)
            )
        self.header.set_context(context)
        if same_binding:
            for path,value in restored.items():
                self._readiness_cache.setdefault(path,value)
        elif dataset is not None and DatasetPage._has_library_roots(dataset):
            self._restore_persisted_workflow_states(
                current_context=current_context,
            )
        self._sync_dataset_filter_options()
        self.apply_filters(preserve_page=same_binding)
        self._sync_exception_storage_state()

    def _cache_record(
        self,record: ImageRecord,*,publish_content_safety: bool=True,
    ) -> None:
        if not self.dataset or not self.dataset.index:
            return
        self.dataset.cache_managed_record(record.image_path,record)
        DatasetPage._invalidate_dataset_search(self)
        if publish_content_safety:
            policy=content_safety_policy()
            if policy is not None: policy.publish_saved_record(record)

    def _publish_editor_image_data_changed(
        self,path: str | Path,*,evidence_changed: bool,
        source: str="universal_editor",
    ) -> None:
        events=getattr(self.state,"events",None)
        if events is not None:
            events.publish(
                "image_data_changed",image=str(Path(path)),
                source=str(source or "universal_editor"),
                evidence_changed=bool(evidence_changed),
            )

    def _library_root_for(self,path: str | Path) -> Path | None:
        if self.dataset is None:
            return None
        manager=self.dataset.manager_for_path(path)
        return manager.root if manager is not None else None

    def _storage_availability(self,path: str | Path):
        dataset=getattr(self,"dataset",None)
        if dataset is None or not hasattr(dataset,"storage_availability"):
            return None
        return dataset.storage_availability(path)

    def _storage_readable(self,path: str | Path) -> bool:
        availability=DatasetPage._storage_availability(self,path)
        return availability is None or bool(getattr(availability,"readable",True))

    def _storage_writable(self,path: str | Path) -> bool:
        availability=DatasetPage._storage_availability(self,path)
        return availability is None or bool(getattr(availability,"ready_for_write",True))

    def _storage_scope_ready(
        self,paths,*,write: bool,action: str,feedback_parent=None,
    ) -> bool:
        values=tuple(dict.fromkeys(Path(value) for value in paths if value))
        check=(DatasetPage._storage_writable if write else DatasetPage._storage_readable)
        held=[
            (path,DatasetPage._storage_availability(self,path))
            for path in values if not check(self,path)
        ]
        if not held:
            return True
        examples="\n".join(
            f"• {path.name}: {getattr(availability,'label','Unavailable')}"
            for path,availability in held[:6]
        )
        QMessageBox.information(
            feedback_parent or self,f"Complete selection is not ready to {action}",
            f"{len(held):,} of {len(values):,} selected image(s) are online-only, "
            "conflicted, read-only, or unavailable. Nothing was changed.\n\n"
            +examples
            +"\n\nRestore local access or resolve the conflict, refresh the library, then select the complete group again.",
        )
        return False

    def _update_inspector_storage_actions(self,availability) -> None:
        readable=(
            availability is None
            or bool(getattr(availability,"readable",True))
        )
        writable=(
            availability is None
            or bool(getattr(availability,"ready_for_write",True))
        )
        external=getattr(self,"inspector_external_button",None)
        if external is not None:
            external.setEnabled(readable)
        for button in getattr(self,"inspector_write_buttons",()):
            button.setEnabled(writable)
        if hasattr(self,"undo_button"):
            self.undo_button.setEnabled(bool(getattr(self,"_edit_history_target",None)))
        editor_hint=getattr(self,"editor_hint",None)
        if editor_hint is None:
            return
        if not readable:
            editor_hint.setText(
                "Image actions are unavailable until this file is available locally and the library is refreshed."
            )
        elif not writable:
            editor_hint.setText(
                "This image can be viewed, but edits and file moves are held until the storage issue is resolved and the library is refreshed."
            )
        else:
            editor_hint.setText(
                "Double-click the card or press Enter to edit image data."
            )

    def _library_context_for(self,path: str | Path):
        root=self._library_root_for(path)
        identity=(
            self.dataset.identity_for_path(path)
            if self.dataset is not None and hasattr(self.dataset,"identity_for_path")
            else None
        )
        return root,str(getattr(identity,"library_id","") or "")

    @staticmethod
    def _has_library_roots(dataset) -> bool:
        roots=getattr(dataset,"roots",None)
        if roots is not None:
            return bool(roots)
        return bool(getattr(dataset,"root",None))

    def _sync_exception_storage_state(self) -> None:
        error=str(
            getattr(self,"_exception_storage_error","")
            or getattr(getattr(self,"exceptions",None),"source_error","")
            or ""
        )
        warning=getattr(self,"exception_storage_warning",None)
        if warning is not None:
            warning.setText(error)
            warning.setVisible(bool(error))
        button=getattr(self,"manage_exceptions_button",None)
        if button is not None:
            button.setEnabled(not error)
            button.setToolTip(
                error or "Review remembered image-specific exceptions."
            )
        if error and getattr(self,"header",None) is not None:
            self.header.set_status(
                "Remembered missing-tag decisions need recovery • nothing changed"
            )

    def _exception_storage_ready(self, *, current: bool=True) -> bool:
        try:
            if getattr(self,"_exception_storage_error",""):
                raise RuntimeError(self._exception_storage_error)
            if current:
                self.exceptions.validate_current()
            else:
                self.exceptions.require_readable()
        except (OSError,RuntimeError,TypeError,ValueError) as exc:
            self._sync_exception_storage_state()
            self.header.set_status(
                "Remembered missing-tag decisions need recovery • nothing changed"
            )
            QMessageBox.critical(
                self,"Remembered decisions unavailable",str(exc),
            )
            return False
        self._sync_exception_storage_state()
        return True

    def _search_exception_keys(self,paths) -> list[str]:
        keys=[]
        for path in paths:
            root,library_id=self._library_context_for(path)
            if root is not None:
                keys.append(self.exceptions.image_key(path,root,library_id))
        return keys

    def _update_search_exceptions(self,search,*,hide=(),show=()) -> None:
        self.exceptions.update_keys(
            search,
            hide=self._search_exception_keys(hide),
            show=self._search_exception_keys(show),
        )

    def clear_filters(self):
        self._search_timer.stop()
        self.filter_panel.reset_values()
        self._sync_dataset_filter_emphasis()
        self.apply_filters()

    def show_tag(self,tag: str):
        """Open normal browsing filtered to a tag selected elsewhere."""
        self.end_batch_review(); self.caption_filter.setCurrentIndex(0); self.record_filter.setCurrentIndex(0)
        self.search.setText(tag); self.apply_filters()

    def open_function(self,function_id: str,scope=None) -> bool:
        """Open one stable Dataset destination without starting its work."""
        function_id=str(function_id or "")
        if function_id=="dataset.source_correction.preview":
            return self._open_source_correction_scope(scope)
        if function_id in {
            "dataset.image_facts.update_matching",
            "image_facts.update_matching",  # Saved pre-relocation handoff.
        }:
            return self._open_image_tag_maintenance(scope)
        if function_id=="dataset.image_data.sync_order_projections":
            return self._open_order_projection_scope(scope)
        if function_id!="dataset.training_captions.preview":
            return False
        if self.training_caption_projection.has_active_background_work():
            QMessageBox.information(
                self,"Training projection preview is running",
                "Wait for the current preview to finish or cancel it safely "
                "before opening another Dataset projection scope.",
            )
            return False
        scope=dict(scope) if isinstance(scope,dict) else {}
        mode=str(scope.get("scope") or "current_filter")
        if mode not in {"current_filter","selected","current_library"}:
            QMessageBox.information(
                self,"Dataset projection scope is unavailable",
                "This saved handoff uses an unsupported Dataset scope. The "
                "scope was not broadened; return to Dataset and choose it again.",
            )
            return False
        current=tuple(getattr(self.dataset,"images",()) or ())
        by_key={str(Path(path)).casefold():Path(path) for path in current}

        def canonical_scope(values):
            requested=tuple(Path(value) for value in values if value)
            missing=[
                path for path in requested
                if str(path).casefold() not in by_key
            ]
            return (
                tuple(
                    by_key[str(path).casefold()] for path in requested
                    if str(path).casefold() in by_key
                ),
                missing,
            )

        if "paths" in scope:
            raw_paths=scope.get("paths",())
            if isinstance(raw_paths,(str,Path)):
                raw_paths=(raw_paths,)
            paths,missing=canonical_scope(raw_paths)
            label=str(scope.get("scope_label") or "saved Dataset selection")
        elif mode=="selected":
            paths,missing=canonical_scope(self.selected_paths())
            label="explicitly selected Dataset images"
        elif mode=="current_library":
            writable=getattr(self.state.dataset,"writable_images",None)
            paths,missing=canonical_scope(
                current if writable is None else writable
            )
            label="current writable Dataset library"
        else:
            if not DatasetPage._dataset_search_results_ready(self):
                return False
            paths,missing=canonical_scope(self.filtered)
            label="current Dataset search and filters"
        if missing:
            QMessageBox.information(
                self,"Dataset projection scope changed",
                "One or more images in this handoff are no longer in the "
                "current Dataset. The scope was not broadened; return to "
                "Dataset and choose it again.",
            )
            return False
        current_widget=self.workflow_tabs.currentWidget()
        if current_widget is not self.training_caption_projection:
            self._training_projection_return_widget=current_widget
        self.training_caption_projection.open_scope(paths,label=label)
        DatasetPage._show_caption_rule_page(self)
        self.workflow_tabs.show()
        return True

    def _open_image_tag_maintenance(self,scope=None) -> bool:
        correction=self.image_identity_correction
        dialog=self._image_tag_maintenance_dialog
        if dialog is None:
            dialog=_ImageTagMaintenanceDialog(correction,self.window())
            self._image_tag_maintenance_dialog=dialog
            correction.open_tag_encyclopedia.connect(dialog.hide)
        dialog.show()
        correction.show()
        dialog.raise_(); dialog.activateWindow()
        # A repeated Dashboard click must not replace an active preview scope.
        if not correction.has_active_background_work():
            correction.open_scope(scope)
        return True

    def open_ingest_source_correction(self,scope,scope_guard) -> bool:
        """Open one Ingest-authorized correction without changing Dataset scope."""
        exact_scope=dict(scope) if isinstance(scope,dict) else {}
        if (
            str(exact_scope.get("origin") or "")
            not in {
                "ingest.universal_editor.tag_manager",
                "ingest.universal_editor.descriptions",
            }
            or not callable(scope_guard)
        ):
            QMessageBox.information(
                self,"Ingest source correction scope is unavailable",
                "This handoff has no current one-shot Ingest authorization. "
                "Nothing was selected or changed; return to Ingest and choose "
                "the correction again.",
            )
            return False
        return self._open_source_correction_scope(
            exact_scope,contextual_guard=scope_guard,
            refresh_callback=lambda:None,
        )

    def _open_source_correction_scope(
        self,scope,*,contextual_guard=None,refresh_callback=None,
    ) -> bool:
        """Preview one exact source or paired Image Knowledge correction."""
        exact_scope=dict(scope) if isinstance(scope,dict) else {}
        image_value=str(exact_scope.get("image") or "").strip()
        source_kind=str(exact_scope.get("source_kind") or "").strip()
        value_kind=str(exact_scope.get("value_kind") or "").strip()
        source_field=str(exact_scope.get("source_field") or "").strip()
        value=str(exact_scope.get("value") or "").strip()
        meaning=str(exact_scope.get("meaning") or "source_only").strip()
        metadata=source_kind=="managed_metadata"
        valid_metadata_field=(
            (value_kind=="tag" and source_field=="XMP:Subject")
            or (
                value_kind=="description"
                and source_field=="XMP:Description"
            )
        )
        if (
            not image_value or not value
            or source_kind not in {"training_txt","managed_metadata"}
            or value_kind not in {"tag","description"}
            or (metadata and not valid_metadata_field)
            or (not metadata and source_field)
            or meaning!="source_only"
        ):
            QMessageBox.information(
                self,"Source correction scope is unavailable",
                "This handoff does not identify one Dataset image, one exact "
                "supported source field and value, and the source-only meaning. "
                "Nothing was selected or changed; choose the correction again.",
            )
            return False

        current=tuple(getattr(self.dataset,"images",()) or ())
        by_key={
            str(Path(path).resolve()).casefold():Path(path).resolve()
            for path in current
        }
        try:
            requested=Path(image_value).resolve()
        except (OSError,RuntimeError,TypeError,ValueError):
            requested=None
        contextual=callable(contextual_guard)
        if contextual:
            image=(
                requested
                if requested is not None and contextual_guard(requested)
                else None
            )
        else:
            image=(
                by_key.get(str(requested).casefold())
                if requested is not None else None
            )
        if image is None:
            owner="current Ingest inventory" if contextual else "current Dataset"
            QMessageBox.information(
                self,"Source correction scope changed",
                "The exact image in this handoff is no longer in the "
                f"{owner}. The scope was not broadened and nothing was changed.",
            )
            return False
        dataset_owned=str(image).casefold() in by_key
        source_label="managed Metadata" if metadata else "Training TXT"
        if dataset_owned and not DatasetPage._storage_scope_ready(
            self,(image,),write=True,action=f"correct {source_label}",
        ):
            return False

        policy=ExternalWritePolicy.from_settings(self.settings)
        registry=self.settings.library_registry()
        protection=SourceCorrectionProtectionDialog(
            policy,registry,self,source_kind=source_kind,
            value_kind=value_kind,
        )
        if protection.exec()!=QDialog.Accepted:
            return True
        if (
            protection.action==PROJECTION_EXCLUSION
            and not metadata and value_kind=="tag"
        ):
            return self._open_projection_exclusion_scope(
                image,value,contextual_guard=contextual_guard,
                return_scope=(exact_scope if contextual else None),
            )
        if protection.action=="configure":
            self.open_automation.emit(
                "automation.settings",
                {"origin":"dataset.source_correction","return_scope":exact_scope},
            )
            return True

        source_path=image if metadata else image.with_suffix(".txt")
        backup_enabled=protection.backup_enabled
        boundary=policy.boundary_for_pairs(
            registry,((image,source_path),),
            backup_enabled=backup_enabled,
            artifact_label=f"{source_label} correction",
            workflow_label=f"{source_label} source correction",
        )
        decision=ExternalWriteDecision.from_policy(
            policy,boundary,
            ask_choice=(backup_enabled if policy.asks_each_time else None),
        )
        service=(
            ManagedMetadataCorrectionService(
                MetadataSyncService(self.settings.exiftool_path or None),
                knowledge=self.state.knowledge,
                history_operation_factory=lambda images:capture_operation(self,images,
                    operation_kind="managed_metadata_correction",reason="Correct exact managed Metadata evidence"),
            )
            if metadata else
            SourceCorrectionService(knowledge=self.state.knowledge,
                history_operation_factory=lambda images:capture_operation(self,images,
                    operation_kind="training_txt_correction",reason="Correct exact Training TXT evidence"))
        )
        if metadata:
            plan=service.plan(
                image,value,value_kind=value_kind,source_field=source_field,
                write_decision=decision,
            )
        elif value_kind=="description":
            plan=service.plan_txt_description_removal(
                image,value,write_decision=decision,
            )
        else:
            plan=service.plan_txt_tag_removal(
                image,value,write_decision=decision,
            )
        preview=SourceCorrectionPlanDialog(plan,self)
        if preview.exec()!=QDialog.Accepted:
            return True
        if not metadata and preview.action==PROJECTION_EXCLUSION:
            return self._open_projection_exclusion_scope(
                image,value,contextual_guard=contextual_guard,
                return_scope=(exact_scope if contextual else None),
            )
        if (
            preview.action not in {"apply",PAIRED_IMAGE_KNOWLEDGE}
        ):
            return True

        if contextual:
            current_scope=bool(contextual_guard(image))
            owner="current Ingest inventory"
        else:
            current_scope=str(image).casefold() in {
                str(Path(path).resolve()).casefold()
                for path in tuple(getattr(self.dataset,"images",()) or ())
            }
            owner="current Dataset"
        if not current_scope:
            QMessageBox.information(
                self,"Source correction scope changed",
                f"The exact image is no longer in the {owner}. Nothing was "
                "changed; return to its owning workflow and choose the correction again.",
            )
            return True
        if not contextual and not DatasetPage._storage_scope_ready(
            self,(image,),write=True,action=(
                f"correct {source_label} and Image Knowledge"
                if preview.action==PAIRED_IMAGE_KNOWLEDGE else
                f"correct {source_label}"
            ),
        ):
            return True

        current_policy=ExternalWritePolicy.from_settings(self.settings)
        current_registry=self.settings.library_registry()
        current_boundary=current_policy.boundary_for_pairs(
            current_registry,((image,source_path),),
            backup_enabled=plan.backup_enabled,
            artifact_label=f"{source_label} correction",
            workflow_label=f"{source_label} source correction",
        )
        current_decision=ExternalWriteDecision.from_policy(
            current_policy,current_boundary,
            ask_choice=(
                plan.backup_enabled if current_policy.asks_each_time else None
            ),
        )
        protection_issues=current_policy.backup_destination_issues(
            current_registry,enabled=plan.backup_enabled,
        )
        if (
            protection_issues
            or current_decision.fingerprint()!=plan.write_decision.fingerprint()
        ):
            detail=(
                " ".join(protection_issues)
                if protection_issues else
                "The external-write policy or declared storage boundary changed "
                "after preview."
            )
            QMessageBox.information(
                self,"Source correction protection changed",
                detail+" Nothing was changed; preview the exact correction again.",
            )
            return True

        execution=plan.write_decision.execution(
                current_policy_fingerprint=current_policy.fingerprint(),
                privacy_confirmed=bool(
                    preview.privacy_confirmation is not None
                    and preview.privacy_confirmation.isChecked()
                ),
            )
        if metadata:
            result=(
                service.apply(
                    plan,execution=execution,meaning=PAIRED_IMAGE_KNOWLEDGE,
                )
                if preview.action==PAIRED_IMAGE_KNOWLEDGE else
                service.apply(plan,execution=execution)
            )
        else:
            result=service.apply(
                plan,execution=execution,meaning=(
                PAIRED_IMAGE_KNOWLEDGE
                if preview.action==PAIRED_IMAGE_KNOWLEDGE else "source_only"
            ),
            )
        outcome=SourceCorrectionResultDialog(result,self)
        outcome.exec()
        refresh_needed=result.status in {"complete","held","failed"}
        if outcome.action=="restore" and result.recovery_manifest is not None:
            restored=(
                service.restore_paired(plan,result.recovery_manifest)
                if result.meaning==PAIRED_IMAGE_KNOWLEDGE else
                service.restore(plan,result.recovery_manifest)
            )
            restore_subject=(
                (
                    "Managed Metadata and Image Knowledge"
                    if metadata else "Training TXT and Image Knowledge"
                )
                if result.meaning==PAIRED_IMAGE_KNOWLEDGE else
                ("Managed Metadata" if metadata else "Training TXT")
            )
            if restored.status in {"restored","already_restored"}:
                QMessageBox.information(
                    self,restore_subject+" restored",restored.detail,
                )
            elif restored.status=="held":
                QMessageBox.information(
                    self,restore_subject+" restore held",restored.detail,
                )
            else:
                QMessageBox.warning(
                    self,restore_subject+" restore failed",restored.detail,
                )
        if refresh_needed:
            if callable(refresh_callback):
                refresh_callback()
            else:
                self.refresh_requested.emit()
        return True

    def _open_projection_exclusion_scope(
        self,image,value,*,contextual_guard=None,return_scope=None,
    ) -> bool:
        """Open one exact confirmed fact in the versioned projection workflow."""
        if self.training_caption_projection.has_active_background_work():
            QMessageBox.information(
                self,"Training projection preview is running",
                "Wait for the current preview to finish or cancel it safely "
                "before opening this exact training decision.",
            )
            return False
        try:
            requested=Path(image).resolve()
        except (OSError,RuntimeError,TypeError,ValueError):
            requested=None
        current=tuple(getattr(self.dataset,"images",()) or ())
        by_key={
            str(Path(path).resolve()).casefold():Path(path).resolve()
            for path in current
        }
        contextual=callable(contextual_guard)
        if contextual:
            exact=(
                requested
                if requested is not None and contextual_guard(requested)
                else None
            )
        else:
            exact=(
                by_key.get(str(requested).casefold())
                if requested is not None else None
            )
        if exact is None:
            owner="current Ingest inventory" if contextual else "current Dataset"
            QMessageBox.information(
                self,"Training projection scope changed",
                "The exact image is no longer in the "
                f"{owner}. The scope was not broadened and nothing was changed.",
            )
            return False
        dataset_owned=str(exact).casefold() in by_key
        if dataset_owned and not DatasetPage._storage_scope_ready(
            self,(exact,),write=True,action="save a training exclusion",
        ):
            return False
        service=TrainingProjectionExclusionService(
            knowledge=self.state.knowledge,
            history_operation_factory=lambda images:capture_operation(self,images,
                operation_kind="training_projection_exclusion",reason="Save an image-local training exclusion"),
        )
        plan=service.plan(exact,value,exclude=True)
        if plan.disposition in {"unsupported","held"}:
            QMessageBox.information(
                self,"Training exclusion is unavailable",plan.detail,
            )
            return False
        current_widget=self.workflow_tabs.currentWidget()
        if current_widget is not self.training_caption_projection:
            self._training_projection_return_widget=current_widget
        self._projection_exclusion_scope={
            "image":str(plan.image),"value":plan.value,
            "contextual":contextual,
        }
        self._projection_exclusion_guard=(
            contextual_guard if contextual else None
        )
        self._projection_exclusion_return_scope=(
            dict(return_scope) if contextual and isinstance(return_scope,dict)
            else None
        )
        opened=self.training_caption_projection.open_scope(
            (plan.image,),label="exact source-correction handoff",
            focus={
                **self._projection_exclusion_scope,
                "excluded":plan.currently_excluded,
                "available":True,"detail":plan.detail,
            },
        )
        if not opened:
            self._projection_exclusion_scope={}
            self._projection_exclusion_guard=None
            self._projection_exclusion_return_scope=None
            return False
        DatasetPage._show_caption_rule_page(self)
        self.workflow_tabs.show()
        return True

    def has_contextual_projection_handoff(self) -> bool:
        return bool(self._projection_exclusion_return_scope)

    def abandon_contextual_projection_handoff(self) -> bool:
        """Drop consumed Ingest authority when navigation abandons its return."""
        if not self._projection_exclusion_return_scope:
            return False
        self._projection_exclusion_return_scope=None
        self._projection_exclusion_scope={}
        self._projection_exclusion_guard=None
        self.training_caption_projection.clear_contextual_scope()
        return True

    def _apply_projection_exclusion(self,request) -> None:
        """Confirm and save only the exact active per-image projection decision."""
        requested=dict(request) if isinstance(request,dict) else {}
        active=dict(self._projection_exclusion_scope)
        if (
            not active
            or str(requested.get("image") or "")!=active.get("image")
            or normalize_tag(str(requested.get("value") or ""))
            !=normalize_tag(str(active.get("value") or ""))
        ):
            QMessageBox.information(
                self,"Training projection scope changed",
                "This exact-value handoff is no longer active. Nothing was changed; "
                "choose the correction again.",
            )
            return
        image=Path(str(active["image"]))
        guard=self._projection_exclusion_guard
        contextual=callable(guard)
        if contextual and not guard(image):
            QMessageBox.information(
                self,"Training projection scope changed",
                "The exact image is no longer in the current Ingest inventory. "
                "Nothing was changed; return to Ingest and choose the correction again.",
            )
            return
        if not contextual:
            current={
                str(Path(path).resolve()).casefold()
                for path in tuple(getattr(self.dataset,"images",()) or ())
            }
            if str(image.resolve()).casefold() not in current:
                QMessageBox.information(
                    self,"Training projection scope changed",
                    "The exact image is no longer in the current Dataset. The "
                    "scope was not broadened and nothing was changed.",
                )
                return
            if not DatasetPage._storage_scope_ready(
                self,(image,),write=True,action="save a training exclusion",
            ):
                return
        exclude=bool(requested.get("exclude"))
        service=TrainingProjectionExclusionService(
            knowledge=self.state.knowledge,
            history_operation_factory=lambda images:capture_operation(self,images,
                operation_kind="training_projection_exclusion",reason="Save an image-local training exclusion"),
        )
        plan=service.plan(image,active["value"],exclude=exclude)
        if plan.disposition=="current":
            self.training_caption_projection.update_projection_exclusion(
                excluded=plan.currently_excluded,detail=plan.detail,
            )
            return
        if not plan.will_write:
            QMessageBox.information(
                self,"Training exclusion was held",plan.detail,
            )
            return
        action=(
            "Omit this valid fact from future training captions?"
            if exclude else
            "Include this valid fact in future training captions again?"
        )
        consequence=(
            "The confirmed fact stays in Image Knowledge. Only this image's "
            "versioned training exclusion is saved. Existing Training TXT, "
            "Metadata, image pixels, and reusable rules remain unchanged. A "
            "verified sidecar recovery checkpoint is created before saving."
        )
        if QMessageBox.question(
            self,"Confirm per-image training decision",
            action+"\n\n"+consequence,
        )!=QMessageBox.Yes:
            return
        if contextual and not guard(image):
            QMessageBox.information(
                self,"Training projection scope changed",
                "The exact image left the current Ingest inventory during review. "
                "Nothing was changed.",
            )
            return
        if not contextual:
            current_after_review={
                str(Path(path).resolve()).casefold()
                for path in tuple(getattr(self.dataset,"images",()) or ())
            }
            if str(image.resolve()).casefold() not in current_after_review:
                QMessageBox.information(
                    self,"Training projection scope changed",
                    "The exact image left the current Dataset during review. "
                    "The scope was not broadened and nothing was changed.",
                )
                return
        result=service.apply(plan)
        if result.status!="complete" or result.record is None:
            QMessageBox.information(
                self,"Training exclusion was held",
                result.detail+" Current data and recovery evidence were retained. Review this exact image's saved history before trying again.",
            )
            return
        if self.dataset and self.dataset.index and not contextual:
            self._cache_record(result.record,publish_content_safety=False)
        if hasattr(self,"_invalidate_inspection_cache"):
            self._invalidate_inspection_cache(result.image)
        if hasattr(self,"_refresh_cached_workflow_state"):
            self._refresh_cached_workflow_state(result.image)
        self._publish_editor_image_data_changed(
            result.image,evidence_changed=False,
            source="training_projection_exclusion",
        )
        self.training_caption_projection.update_projection_exclusion(
            excluded=result.excluded,detail=(
                result.detail+" Saved and verified. Choose the opposite action "
                "here to reverse this projection decision."
            ),
        )
        self.header.set_status(
            "Saved the per-image training exclusion; confirmed facts and external files are unchanged"
            if result.excluded else
            "Removed the per-image training exclusion; confirmed facts and external files are unchanged"
        )

    def _open_order_projection_scope(self,scope) -> bool:
        """Resolve a persisted stable-Tag scope into the current Dataset."""
        scope=dict(scope) if isinstance(scope,dict) else {}
        target=str(scope.get("target") or "")
        if target not in {"captions","metadata","organization"}:
            QMessageBox.information(
                self,"Downstream update unavailable",
                "This saved downstream target has no supported Dataset job. "
                "Nothing was changed and the pending notice was retained.",
            )
            return False
        if (
            not self.dataset or not DatasetPage._has_library_roots(self.dataset)
            or self.dataset.index is None
        ):
            QMessageBox.information(
                self,"Open libraries first",
                "Choose or refresh the image libraries that should be checked. "
                "The saved SDXL Tag scope was retained without broadening it.",
            )
            return False
        stable_ids=tuple(dict.fromkeys(
            str(value) for value in scope.get("stable_ids",()) if str(value)
        ))
        current_tags=getattr(self.state.knowledge,"tags",{})
        missing=[value for value in stable_ids if value not in current_tags]
        if not stable_ids or missing:
            QMessageBox.information(
                self,"Saved downstream scope changed",
                "One or more SDXL Tag identities in this saved caption-order "
                "scope are no longer available. Nothing was selected or "
                "changed; review the newer System Knowledge change first.",
            )
            return False
        current_paths={
            str(Path(path)).casefold():Path(path)
            for path in tuple(getattr(self.dataset,"images",()) or ())
        }
        try:
            records=tuple(
                record for record in self.dataset.index.all_records()
                if str(Path(record.path)).casefold() in current_paths
            )
            matched=affected_order_projection_paths(
                OrderImpact(changed_tags=stable_ids),current_tags,records,
                aliases_for=self.state.knowledge.effective_aliases,
            )
        except Exception as error:
            QMessageBox.warning(
                self,"Could not resolve downstream scope",
                "The current Dataset index could not resolve the saved SDXL "
                f"Tag scope. Nothing was changed.\n\n{error}",
            )
            return False
        paths=[current_paths[str(Path(value)).casefold()] for value in matched]
        context={
            "target":target,
            "notice_updated_at":str(scope.get("notice_updated_at") or ""),
        }
        if not paths:
            completed=self._complete_downstream_projection(context)
            QMessageBox.information(
                self,
                "No matching image projections" if completed
                else "A newer downstream update is pending",
                (
                    "The current included-library index has no image using the "
                    "saved SDXL Tag scope. This downstream target is current for "
                    "the included libraries; no file was opened or changed."
                    if completed else
                    "The current included-library index has no image using this "
                    "older saved SDXL Tag scope, but a newer System Knowledge "
                    "change is pending. Its notice was retained without opening "
                    "or changing a file."
                ),
            )
            return True
        if target=="organization":
            return DatasetPage._start_folder_audit(
                self,paths,downstream_context=context,
            )
        self.sync_selected_data(
            paths,preferred_targets=(
                ("txt",) if target=="captions" else ("metadata",)
            ),downstream_context=context,
        )
        return True

    def _return_from_training_projection(self):
        contextual_return=getattr(
            self,"_projection_exclusion_return_scope",None,
        )
        if contextual_return is not None:
            self._projection_exclusion_return_scope=None
            self._projection_exclusion_scope={}
            self._projection_exclusion_guard=None
            clear_scope=getattr(
                self.training_caption_projection,"clear_contextual_scope",None,
            )
            if callable(clear_scope):
                clear_scope()
        target=self._training_projection_return_widget
        self._training_projection_return_widget=None
        if target in (
            self.training_caption_projection,self.image_identity_correction,
        ) or target is None:
            target=self.workflow_tabs.widget(0)
        self.workflow_tabs.setCurrentWidget(target)
        if contextual_return is not None:
            self.ingest_source_correction_return_requested.emit(
                dict(contextual_return)
            )

    def _dataset_search_spec(self):
        state_control=getattr(self,"state_filter",self.record_filter)
        projection_control=getattr(self,"projection_filter",self.caption_filter)
        state_mode=(
            str(state_control.currentData() or "all")
            if hasattr(state_control,"currentData")
            else ("all" if state_control.currentIndex()==0 else "problem")
        )
        stage_mode=DatasetPage.current_stage(self)
        projection_mode=(
            str(projection_control.currentData() or "all")
            if stage_mode in {"complete","all"}
            and hasattr(projection_control,"currentData")
            else (
                "all" if stage_mode not in {"complete","all"}
                or projection_control.currentIndex()==0 else "needs_sync"
            )
        )
        return DatasetSearchSpec(
            query=self.search.text(),stage_mode=stage_mode,state_mode=state_mode,
            projection_mode=projection_mode,
        )

    def _invalidate_dataset_search(self) -> None:
        DatasetPage._clear_deferred_dataset_filter(self)
        store=getattr(self,"_dataset_search_store",None)
        if store is not None:
            store.invalidate_projections()
        self._dataset_search_accepted_spec=None
        self._dataset_search_generation=getattr(self,"_dataset_search_generation",0)+1
        self._dataset_search_source_generation=(
            getattr(self,"_dataset_search_source_generation",0)+1
        )
        task=getattr(self,"_dataset_search_task",None)
        if task is not None:
            task.cancel()
            self._dataset_search_task=None

    def _dataset_search_selection_changed(self) -> None:
        self._dataset_search_selection_generation=(
            getattr(self,"_dataset_search_selection_generation",0)+1
        )
        if getattr(self,"_dataset_search_task",None) is not None:
            self.apply_filters(preserve_page=True)

    def _dataset_search_results_ready(self) -> bool:
        """Do not apply a new filter label to the prior accepted result scope."""
        if getattr(self,"batch_mode",""):
            return True
        if hasattr(self,"dataset"):
            dataset=self.dataset
            if dataset is None or not DatasetPage._has_library_roots(dataset):
                return True
        if not hasattr(self,"_dataset_search_accepted_spec"):
            # Direct non-Qt consumers have no asynchronous search lifecycle.
            return True
        if (
            getattr(self,"_dataset_search_task",None) is None
            and getattr(self,"_dataset_search_accepted_spec",None)
            ==DatasetPage._dataset_search_spec(self)
            and getattr(self,"_dataset_search_accepted_memory",None)
            ==DatasetSearchProjectionStore.memory_token(self.dataset)
        ):
            return True
        if DatasetPage._dataset_search_pending_text(self):
            return False
        self.header.set_status(
            "Dataset search needs a retry. Choose Retry Search to refresh matching results."
        )
        return False

    def _clear_deferred_dataset_filter(self) -> None:
        self._deferred_dataset_filter=None
        timer=getattr(self,"_dataset_filter_retry_timer",None)
        if timer is not None: timer.stop()

    def _dataset_filter_token(self):
        return (
            DatasetPage._dataset_search_spec(self),
            DatasetSearchProjectionStore.memory_token(self.dataset),
            getattr(self,"_dataset_scope_generation",0),
            getattr(self,"_dataset_search_source_generation",0),
            getattr(self,"_dataset_search_selection_generation",0),
        )

    def _retry_dataset_filter(self) -> None:
        pending=getattr(self,"_deferred_dataset_filter",None)
        if pending is None or getattr(self,"_shutdown_requested",False):
            DatasetPage._clear_deferred_dataset_filter(self)
            return
        # Full-filter classification retains its exclusive boundary. Wait
        # without rereading the index or probing every deferred image each tick.
        if self.state.jobs.active():
            self._dataset_filter_retry_timer.start()
            return
        token,paths=pending
        if token!=DatasetPage._dataset_filter_token(self):
            DatasetPage._clear_deferred_dataset_filter(self)
            self.apply_filters(preserve_page=True)
            return
        if self._start_workflow_filter_scan(paths,defer_if_busy=True):
            DatasetPage._clear_deferred_dataset_filter(self)
            self.render_page()
        else:
            self._dataset_filter_retry_timer.start()

    def _dataset_search_pending_text(self) -> str:
        dataset=getattr(self,"dataset",None)
        if dataset is None or not DatasetPage._has_library_roots(dataset):
            return ""
        if getattr(self,"_deferred_dataset_filter",None) is not None:
            return "Image status will refresh when background work finishes."
        if getattr(self,"_filter_state_task",None) is not None:
            notice=str(getattr(self,"_workflow_cache_notice","") or "")
            return "Checking image status for these filters…"+(f" {notice}" if notice else "")
        if getattr(self,"_dataset_search_task",None) is not None:
            return "Searching images in the background…"
        if DatasetPage._dataset_search_retry_available(self):
            return "Search results need a retry. Choose Retry Search, or press Enter in Search."
        return ""

    def _dataset_search_retry_available(self) -> bool:
        dataset=getattr(self,"dataset",None)
        if (dataset is None or not DatasetPage._has_library_roots(dataset)
                or getattr(self,"_shutdown_requested",False)
                or getattr(self,"_dataset_search_task",None) is not None
                or getattr(self,"_filter_state_task",None) is not None
                or getattr(self,"_deferred_dataset_filter",None) is not None
                or not hasattr(self,"_dataset_search_accepted_spec")):
            return False
        scoped=getattr(dataset,"scoped_profile_managers",None)
        if callable(scoped):
            availability=tuple(
                dataset.library_availability(profile.stable_id)
                for profile,_manager in scoped()
            )
            if not availability or any(
                value not in {"current","validated_checking"}
                for value in availability
            ):
                return False
        accepted=self._dataset_search_accepted_spec
        if accepted is None:
            return True
        return (
            accepted!=DatasetPage._dataset_search_spec(self)
            or getattr(self,"_dataset_search_accepted_memory",None)
            !=DatasetSearchProjectionStore.memory_token(dataset)
        )

    def _update_dataset_search_retry_action(self) -> None:
        button=getattr(self,"retry_search_button",None)
        if button is not None:
            available=DatasetPage._dataset_search_retry_available(self)
            hint=getattr(self,"retry_search_hint",None)
            if hint is not None:
                hint.setText(
                    "Search did not finish. Shown images are previous results, not matches for the current search."
                    if getattr(self,"filtered",()) else
                    "Search did not finish. No current results are available."
                )
            row=getattr(self,"retry_search_row",None)
            if row is not None:row.setVisible(available)
            else:button.setVisible(available)

    def _apply_dataset_search_projection(
        self,projection,*,spec,requested_page: int,compatibility_list: bool=False,
    ) -> None:
        self._filter_records=projection.records
        self._projection_stage_counts=dict(projection.stage_counts)
        self._projection_unknown_count=int(projection.unknown_count)
        self._projection_paused_count=int(getattr(projection,"paused_count",0))
        if projection.stale_paths:
            self._dataset_search_accepted_spec=None
            if not self._start_workflow_filter_scan(
                projection.stale_paths,defer_if_busy=True,
            ):
                self._deferred_dataset_filter=(
                    DatasetPage._dataset_filter_token(self),projection.stale_paths,
                )
                timer=getattr(self,"_dataset_filter_retry_timer",None)
                if timer is not None: timer.start()
            self.render_page()
            return
        DatasetPage._clear_deferred_dataset_filter(self)
        self._dataset_search_accepted_spec=spec
        self._dataset_search_accepted_memory=projection.memory_token
        header=getattr(self,"header",None)
        if header is not None:
            header.set_status(
                "Library checks paused; showing known results. Resume library checks to classify remaining images."
                if self._projection_paused_count else ""
            )
        # Result-path construction and selection trimming both ran in the worker;
        # keep the existing path-based action semantics without a second Qt scan.
        self.filtered=list(projection.paths) if compatibility_list else projection.paths
        if self.batch_selected:
            self.batch_selected.intersection_update(projection.retained_selected)
        self.page=requested_page
        self.render_page()

    def _dataset_search_complete(self,task,result) -> None:
        if task is not getattr(self,"_dataset_search_task",None):
            return
        self._dataset_search_task=None
        generation,scope_generation,source_generation,selection_generation,spec,page,projection=result
        if (
            getattr(self,"_shutdown_requested",False)
            or generation!=getattr(self,"_dataset_search_generation",0)
            or scope_generation!=getattr(self,"_dataset_scope_generation",0)
            or source_generation!=getattr(self,"_dataset_search_source_generation",0)
            or selection_generation!=getattr(self,"_dataset_search_selection_generation",0)
            or spec!=DatasetPage._dataset_search_spec(self)
            or projection.memory_token!=DatasetSearchProjectionStore.memory_token(
                self.dataset,
            )
        ):
            if not getattr(self,"_shutdown_requested",False):
                self.header.set_status("Dataset changed during search. Choose Retry Search to refresh results.")
                DatasetPage._update_dataset_search_retry_action(self)
                self.render_page()
            self._notify_background_work_finished()
            return
        DatasetPage._apply_dataset_search_projection(
            self,projection,spec=spec,requested_page=page,
        )
        self._notify_background_work_finished()

    def _dataset_search_failed(self,task,message: str) -> None:
        if task is not getattr(self,"_dataset_search_task",None):
            return
        self._dataset_search_task=None
        if not getattr(self,"_shutdown_requested",False):
            self.header.set_status(f"Dataset search could not be updated: {message}. Choose Retry Search to try again.")
            DatasetPage._update_dataset_search_retry_action(self)
            self.render_page()
        self._notify_background_work_finished()

    def _dataset_search_cancelled(self,task) -> None:
        if task is not getattr(self,"_dataset_search_task",None):
            return
        self._dataset_search_task=None
        if not getattr(self,"_shutdown_requested",False):
            self.header.set_status("Dataset search stopped. Choose Retry Search to try again.")
            DatasetPage._update_dataset_search_retry_action(self)
            self.render_page()
        self._notify_background_work_finished()

    def apply_filters(self,*_,preserve_page: bool=False):
        requested_page=self.page if preserve_page else 0
        if not self.dataset or not DatasetPage._has_library_roots(self.dataset):
            DatasetPage._invalidate_dataset_search(self)
            header=getattr(self,"header",None)
            if header is not None: header.set_status("")
            self.filtered=[]; self._projection_stage_counts=None; self.render_page(); return
        spec=DatasetPage._dataset_search_spec(self)
        pending=getattr(self,"_deferred_dataset_filter",None)
        if pending is not None:
            if pending[0]==DatasetPage._dataset_filter_token(self):
                return
            DatasetPage._clear_deferred_dataset_filter(self)
        active_search=getattr(self,"_dataset_search_task",None)
        if (
            active_search is not None
            and getattr(self,"_dataset_search_requested_spec",None)!=spec
        ):
            active_search.cancel()
            self._dataset_search_task=None
            self._dataset_search_generation=(
                getattr(self,"_dataset_search_generation",0)+1
            )
        if (
            not spec.query and spec.state_mode=="all"
            and spec.projection_mode=="all" and spec.stage_mode=="all"
        ):
            # Keep the documented instant open path: no search/index projection
            # is needed until the user asks Dataset to narrow the catalog.
            self.filtered=getattr(self.dataset,"images",())
            self._filter_records={}; self._projection_stage_counts={
                "unprepared":len(self.filtered),"partial":0,"complete":0,
            }
            self._projection_unknown_count=0
            self._dataset_search_accepted_spec=spec
            self._dataset_search_accepted_memory=DatasetSearchProjectionStore.memory_token(self.dataset)
            header=getattr(self,"header",None)
            if header is not None: header.set_status("")
            if self.batch_selected:
                self.batch_selected.intersection_update(str(path) for path in self.filtered)
            self.page=requested_page; self.render_page(); return
        if (
            spec.stage_mode=="complete"
            and not getattr(self.dataset,"index",None)
            and not DatasetPage._workflow_index_managers(self)
        ):
            if active_search is not None:
                active_search.cancel(); self._dataset_search_task=None
            self.filtered=[]; self.batch_selected.clear(); self.page=requested_page
            self._filter_records={}
            self._dataset_search_accepted_spec=spec
            self._dataset_search_accepted_memory=DatasetSearchProjectionStore.memory_token(self.dataset)
            self._projection_stage_counts={"unprepared":len(self.dataset.images),"partial":0,"complete":0}
            header=getattr(self,"header",None)
            if header is not None: header.set_status("")
            self._projection_unknown_count=0; self.render_page(); return
        store=getattr(self,"_dataset_search_store",None)
        if store is None:
            store=DatasetSearchProjectionStore(); self._dataset_search_store=store
        jobs=getattr(getattr(self,"state",None),"jobs",None)
        if jobs is None:
            # Small direct-call fixtures have no application coordinator.  Keep
            # this explicit compatibility seam out of normal Qt interaction.
            projection=store.project(
                self.dataset,spec,readiness_cache=getattr(self,"_readiness_cache",{}),
                selected_paths=getattr(self,"batch_selected",()),
            )
            DatasetPage._apply_dataset_search_projection(
                self,projection,spec=spec,requested_page=requested_page,
                compatibility_list=True,
            )
            return
        previous=getattr(self,"_dataset_search_task",None)
        if (
            previous is not None
            and getattr(self,"_dataset_search_requested_spec",None)==spec
            and getattr(self,"_dataset_search_requested_source_generation",0)
            ==getattr(self,"_dataset_search_source_generation",0)
            and getattr(self,"_dataset_search_requested_selection_generation",0)
            ==getattr(self,"_dataset_search_selection_generation",0)
        ):
            return
        if previous is not None:
            previous.cancel()
        self._dataset_search_generation=getattr(self,"_dataset_search_generation",0)+1
        generation=self._dataset_search_generation
        scope_generation=getattr(self,"_dataset_scope_generation",0)
        source_generation=getattr(self,"_dataset_search_source_generation",0)
        selection_generation=getattr(self,"_dataset_search_selection_generation",0)
        dataset=self.dataset
        readiness_cache=getattr(self,"_readiness_cache",{})
        selected=frozenset(getattr(self,"batch_selected",()))
        self._dataset_search_requested_spec=spec
        self._dataset_search_accepted_spec=None
        self._dataset_search_requested_source_generation=source_generation
        self._dataset_search_requested_selection_generation=selection_generation

        def work(job):
            # Resolve the exact disk-backed source inside the worker before an
            # O(N) readiness copy. Revisited filters can reuse the immutable
            # result while external index changes still force a miss.
            cached,cache_context=store.cached_projection(
                dataset,spec,readiness_owner=readiness_cache,
                source_generation=source_generation,selected_paths=selected,
                should_continue=job.checkpoint,
            )
            if cached is not None:
                if (
                    getattr(readiness_cache,"revision",None)
                    !=cache_context.readiness_revision
                    or not job.checkpoint()
                ):
                    raise InterruptedError("Dataset search source changed")
                projection=cached
                return (
                    generation,scope_generation,source_generation,selection_generation,
                    spec,requested_page,projection,
                )
            readiness_revision=getattr(readiness_cache,"revision",None)
            readiness_snapshot=dict(readiness_cache.items())
            if (
                readiness_revision is not None
                and getattr(readiness_cache,"revision",None)!=readiness_revision
            ):
                raise InterruptedError("Dataset readiness changed")
            projection=store.project(
                dataset,spec,readiness_cache=readiness_snapshot,
                selected_paths=selected,should_continue=job.checkpoint,
                cache_context=cache_context,
                cache_guard=lambda:(
                    getattr(readiness_cache,"revision",None)==readiness_revision
                    and getattr(self,"_dataset_search_source_generation",0)
                    ==source_generation
                    and not getattr(self,"_shutdown_requested",False)
                ),
            )
            if (
                readiness_revision is not None
                and getattr(readiness_cache,"revision",None)!=readiness_revision
            ):
                raise InterruptedError("Dataset readiness changed")
            return (
                generation,scope_generation,source_generation,selection_generation,
                spec,requested_page,projection,
            )

        task=CoordinatedCallTask(
            jobs,"Searching Dataset",work,resource="io",
            key=(
                f"dataset-search:{id(self)}:{scope_generation}:"
                f"{source_generation}:{selection_generation}:{generation}:{hash(spec)}"
            ),
        )
        self._dataset_search_task=task
        DatasetPage._update_dataset_search_retry_action(self)
        task.signals.complete.connect(
            lambda result,owner=task:DatasetPage._dataset_search_complete(self,owner,result)
        )
        task.signals.failed.connect(
            lambda message,owner=task:DatasetPage._dataset_search_failed(self,owner,message)
        )
        task.signals.cancelled.connect(
            lambda owner=task:DatasetPage._dataset_search_cancelled(self,owner)
        )
        self.header.set_status("Searching the saved Dataset index in the background…")
        task.start()
        if not self.filtered: self.render_page()

    @staticmethod
    def _workflow_signatures(path: Path):
        values=[]
        for candidate in (path.with_suffix(".tags.json"),path.with_suffix(".txt")):
            try:
                stat=candidate.stat(); values.append((stat.st_mtime_ns,stat.st_size))
            except OSError: values.append(None)
        return tuple(values)

    def _workflow_cache_current(self,path: Path) -> bool:
        # Dataset refresh owns external-change detection and clears this cache.
        # Filtering therefore remains an in-memory lookup instead of issuing
        # two synchronous filesystem probes for every image in a large library.
        return path in self._readiness_cache

    def _refresh_cached_workflow_state(self,path: Path,*,workflow=None,expected=None):
        """Reassess one changed image without blanking its visible status."""
        path=Path(path)
        if workflow is None:
            # Source reads belong to the bounded worker, never an editor-save
            # or derived-result callback on the Qt thread. The normal filter
            # projection queues unknown rows and retains its busy/retry route.
            self._readiness_cache.pop(path,None)
            DatasetPage._invalidate_dataset_search(self)
            if not getattr(self,"_workflow_refresh_queued",False):
                self._workflow_refresh_queued=True
                def refresh():
                    self._workflow_refresh_queued=False
                    if getattr(self,"_shutdown_requested",False):return
                    # An outer batch may already have requested the final view.
                    if getattr(self,"_dataset_search_task",None) is not None:return
                    if getattr(self,"_dataset_search_accepted_spec",None) is not None:return
                    self.apply_filters(preserve_page=True)
                QTimer.singleShot(0,refresh)
            return None
        expected=(expected if expected is not None
                  else DatasetPage._workflow_family_fingerprint(path))
        if expected!=DatasetPage._workflow_family_fingerprint(path):
            self._readiness_cache.pop(path,None)
            return None
        value=(self._workflow_signatures(path),workflow)
        self._readiness_cache[path]=value
        persist=getattr(self,"_persist_workflow_states",None)
        if callable(persist):
            persisted=persist(
                {path:value},expected_fingerprints={path:expected},
            )
            if not persisted:
                self.header.set_status(
                    "Image status is current; its performance cache will retry later."
                )
        if expected!=DatasetPage._workflow_family_fingerprint(path):
            self._readiness_cache.pop(path,None)
            return None
        card=self.cards.get(str(path))
        if card is not None: self._configure_gallery_card(path,card)
        return workflow

    def derived_results_changed(
        self,paths=(),*,scope: str="images",cause: str="Image Knowledge changed",
        workflow_proofs=None,
    ) -> None:
        """Publish current derived cache results without losing gallery context."""
        DatasetPage._invalidate_dataset_search(self)
        affected=tuple(dict.fromkeys(Path(value) for value in paths if value))
        projection=getattr(self,"training_caption_projection",None)
        if projection is not None:
            projection.derived_results_changed(affected,scope=scope)
        broad=str(scope or "images")=="all"
        affected_count=len(affected)
        from core.workflow_state_cache import workflow_state_digest

        context=str(getattr(self,"_readiness_context_fingerprint","") or "")
        context_reader=getattr(getattr(self,"workflow_states",None),"context_fingerprint",None)
        current_context=context_reader() if callable(context_reader) else ""
        context_changed=bool(current_context and current_context!=context)
        if current_context:
            self._readiness_context_fingerprint=current_context
        if context_changed or (broad and workflow_proofs is None):
            # Real shared-input changes still reject old projections. A broad
            # check or a long notification is not itself such a change.
            self._readiness_cache.clear(); self._inspection_detail_cache.clear()
            restore=getattr(self,"_restore_persisted_workflow_states",None)
            if callable(restore):restore()
        else:
            invalidated=[]
            contexts={current_context}
            compatible=getattr(getattr(self,"workflow_states",None),"compatible_context_fingerprints",None)
            if callable(compatible):contexts.update(compatible())
            for path in affected:
                cached=self._readiness_cache.get(path)
                proof=(workflow_proofs or {}).get(str(path))
                reusable=False
                if cached is not None and current_context and proof:
                    try:
                        reusable=(len(proof)==2 and proof[0] in contexts and
                                  proof[1]==workflow_state_digest(cached[1].to_cache_dict()))
                    except (AttributeError,TypeError,ValueError):
                        pass
                self._inspection_detail_cache.pop(path,None)
                if reusable:
                    continue
                self._readiness_cache.pop(path,None)
                invalidated.append(path)
            affected=tuple(invalidated)
            # Small editor saves should update visible completeness immediately.
            # Larger event batches retain responsive UI and let the existing
            # bounded workflow-state worker fill only the visible/filter scope.
            if len(affected)<=8:
                for path in affected:
                    if DatasetPage._storage_readable(self,path):
                        self._refresh_cached_workflow_state(path)
        self.apply_filters(preserve_page=True)
        reason=str(cause or "current image data changed").strip().rstrip(".")
        if broad:
            message=f"Current image results refreshed because {reason.lower()}."
        else:
            count=affected_count
            message=(
                f"Current results for {count:,} image{'s' if count!=1 else ''} "
                f"refreshed because {reason.lower()}."
            )
        self.header.set_status(message)

    def _start_workflow_filter_scan(
        self,paths,*,automatic: bool=False,defer_if_busy: bool=False,
    ):
        if self._filter_state_task is not None:
            if defer_if_busy:
                self.header.set_status("Checking image status for these filters…")
            return False
        availability=getattr(self.dataset,"availability_for_path",lambda _path:"current")
        paths=[Path(path) for path in paths
               if DatasetPage._storage_readable(self,path)
               and availability(path)!="validated_cancelled"]
        if not paths:
            if not automatic:self.apply_filters()
            return True
        # Inventory validation establishes source freshness before classification.
        # This dependency is narrower than excluding every foreground operation.
        library_id=getattr(self.state,"_operation_gate",{}).get("library")
        if library_id:
            self.header.set_status("Image status will refresh after library validation finishes")
            return False
        started,active=self.state.begin_long_operation(
            "dataset",f"Checking image status for {len(paths):,} image(s)",
            resource="io",exclusive=False,
        )
        if not started:
            if automatic or defer_if_busy: self.header.set_status(f"Image status will refresh after {active} finishes")
            else: show_operation_in_progress(self,active)
            return False
        self.state.update_long_operation("dataset",0,len(paths),"Preparing image-status checks")
        self._filter_generation+=1; generation=self._filter_generation
        self.header.set_status(f"Classifying {len(paths):,} image(s) for the selected filter…")
        service=ImageWorkflowStateService(
            self.state.knowledge,self.characters,
            ignored_plain_language=self.settings.ignored_plain_language_sentences,categories=self.categories,
            inspect_sources=True,
        )
        context=str(getattr(self,"_readiness_context_fingerprint","") or "")
        if not context:
            context=service.context_fingerprint()
            self._readiness_context_fingerprint=context
        persistence_targets=self._workflow_persistence_targets(paths)
        self._workflow_scan_paths=tuple(paths)
        read_job=self.state.jobs.get(getattr(self.state,"_operation_gate",{}).get("dataset",""))

        def work(progress,should_cancel):
            from core.workflow_state_cache import WorkflowStateCheckpoint

            values={}; expected={}
            checkpoint=WorkflowStateCheckpoint(
                context_fingerprint=context,targets=persistence_targets,
            )
            if read_job is not None:
                self.state.jobs.allow_foreground_yield(read_job.id,before_yield=checkpoint.flush)
            cancelled=False
            try:
                reusable={};reuse_fingerprints={}
                validator=service.semantic_validator()
                def validate(payload):
                    if should_cancel() or (read_job is not None and not read_job.checkpoint()):
                        raise InterruptedError("Saved status validation cancelled")
                    return validator(payload)
                grouped={}
                for path,target in persistence_targets.items():grouped.setdefault(target,[]).append(path)
                for target,selected in grouped.items():
                    reusable.update(target.validated_workflow_states(selected,
                        context_fingerprint=context,context_validator=validate,
                        source_fingerprints=reuse_fingerprints,validate_sources=False,
                        progress=lambda current,total:progress(current,total,"Checking saved image status"),
                        should_continue=lambda:not should_cancel() and (read_job is None or read_job.checkpoint())))
                for index,path in enumerate(paths,1):
                    if should_cancel() or (read_job is not None and not read_job.checkpoint()):
                        cancelled=True; break
                    before=DatasetPage._workflow_family_fingerprint(path)
                    saved=reusable.get(path) if reuse_fingerprints.get(path)==before else None
                    restored=ImageWorkflowState.from_cache_dict(saved) if saved else None
                    if restored is not None and not service.cached_rating_matches(saved):restored=None
                    value=(self._workflow_signatures(path),restored or service.assess(path))
                    if before==DatasetPage._workflow_family_fingerprint(path):
                        values[path]=value; expected[path]=before
                        checkpoint.add(path,value[1].to_cache_dict(),before)
                    progress(index,len(paths),"Reusing saved image status" if restored is not None else path.name)
            except InterruptedError:
                cancelled=True
            finally:
                try:
                    checkpoint.flush()
                finally:
                    service.close()
            stable={
                path for path in checkpoint.current_recorded()
                if path not in expected
                or DatasetPage._workflow_family_fingerprint(path)==expected[path]
            }
            unstable_paths=tuple(
                path for path in paths
                if path not in values
                or path in persistence_targets and path not in stable
            )
            return generation,{path:values[path] for path in stable},cancelled,checkpoint.cache_warning,unstable_paths
        task=CancelableProgressCallTask(work); self._filter_state_task=task; self._filter_state_automatic=bool(automatic); self.cancel_projection_button.setText("Cancel Safely"); self.cancel_projection_button.setEnabled(True); self.cancel_projection_button.show()
        cache_notice=str(getattr(self,"_workflow_cache_notice","") or "")
        def report_progress(index,total,name):
            detail=f"Image data filter: {index:,} of {total:,} • {name}"
            if cache_notice:
                detail+=f" • {cache_notice}"
            self.state.update_long_operation("dataset",index,total,detail)
            self.header.set_status(detail)
        task.signals.progress.connect(report_progress)
        task.signals.complete.connect(
            lambda result,owner=task:self._workflow_filter_complete(owner,result)
        )
        task.signals.failed.connect(
            lambda message,owner=task:self._workflow_filter_failed(owner,message)
        )
        self.pool.start(task)
        return True

    def _workflow_filter_complete(self,task,result):
        if task is not self._filter_state_task:
            return
        automatic=self._filter_state_automatic
        self.state.finish_long_operation("dataset"); self._filter_state_task=None; self._filter_state_automatic=False; self.cancel_projection_button.hide()
        generation,values,cancelled,*details=result
        cache_warning=bool(details[0]) if details else False
        unstable=tuple(details[1]) if len(details)>1 and details[1] else ()
        if getattr(self,"_shutdown_requested",False):
            self._notify_background_work_finished()
            return
        if generation==self._filter_generation:
            # A selected-image inspection may have read newer source evidence
            # while this broader classification was running.
            selected=getattr(self,"current_path",None)
            expected=getattr(self,"_current_report_fingerprint",None)
            if (selected in values and expected is not None
                    and expected==DatasetPage._workflow_family_fingerprint(selected)
                    and selected in self._readiness_cache):
                values[selected]=self._readiness_cache[selected]
                self._refresh_cached_workflow_state(
                    selected,workflow=values[selected][1],expected=expected,
                )
            self._readiness_cache.update(values)
            DatasetPage._invalidate_dataset_search(self)
            if not cancelled and not cache_warning and not unstable:
                self._workflow_cache_notice=""
        if cancelled:
            self.header.set_status("Image-data classification cancelled safely")
            if not self.filtered: self.render_page()
            self._notify_background_work_finished()
            return
        self.header.set_status(
            "Image status is current; its performance cache will retry later."
            if cache_warning else
            f"{len(unstable):,} image(s) changed during the check and will be checked again."
            if unstable else ""
        )
        if automatic:
            for path in values:
                card=self.cards.get(str(path))
                if card is not None: self._configure_gallery_card(path,card)
            # Accepted filters depend on this readiness snapshot too.
            if generation==self._filter_generation:
                self.apply_filters(preserve_page=True)
        else: self.apply_filters()
        if automatic and unstable:
            QTimer.singleShot(
                0,lambda values=unstable:self._start_workflow_filter_scan(
                    values,automatic=True,
                ),
            )
        self._notify_background_work_finished()

    def _workflow_filter_failed(self,task,message: str):
        if task is not self._filter_state_task:
            return
        DatasetPage._clear_deferred_dataset_filter(self)
        automatic=self._filter_state_automatic
        self.state.finish_long_operation("dataset"); self._filter_state_task=None; self._filter_state_automatic=False; self.cancel_projection_button.hide(); self.header.set_status("Image-data status could not be checked" if automatic else "Image-data filtering could not be completed")
        if not getattr(self,"_shutdown_requested",False) and not self.filtered:
            self.render_page()
        if not automatic and not getattr(self,"_shutdown_requested",False):
            QMessageBox.warning(self,"Could not apply image-data filter",message)
        self._notify_background_work_finished()

    def audit_folders(self,*_):
        return DatasetPage._start_folder_audit(self)

    def _start_folder_audit(self,paths=None,*,downstream_context=None) -> bool:
        if getattr(self,"_folder_audit_task",None) is not None:return False
        if not self.dataset or not DatasetPage._has_library_roots(self.dataset):
            QMessageBox.information(self,"Load libraries","Choose or refresh image libraries first."); return False
        scoped_paths=(
            tuple(dict.fromkeys(Path(value) for value in paths if value))
            if paths is not None else ()
        )
        scoped_managers=[]
        if paths is not None:
            current={
                str(Path(value)).casefold():Path(value)
                for value in tuple(getattr(self.dataset,"images",()) or ())
            }
            if not scoped_paths or any(
                str(path).casefold() not in current for path in scoped_paths
            ):
                QMessageBox.information(
                    self,"Saved organization scope changed",
                    "One or more images in the saved System Knowledge scope "
                    "are no longer available in the included libraries. "
                    "Nothing was changed and the pending notice was retained.",
                )
                return False
            if not DatasetPage._storage_scope_ready(
                self,scoped_paths,write=False,
                action="audit saved organization scope",
            ):
                return False
            for path in scoped_paths:
                manager=self.dataset.manager_for_path(path)
                if manager is None or manager.root is None:
                    QMessageBox.information(
                        self,"Saved organization scope changed",
                        "The current library profiles could not resolve every "
                        "image in the saved System Knowledge scope. Nothing was "
                        "changed and the pending notice was retained.",
                    )
                    return False
                scoped_managers.append((path,manager))
        started,active=self.state.begin_long_operation("dataset","Auditing folder organization")
        if not started:show_operation_in_progress(self,active); return False
        self.header.set_status("Auditing folder organization in the background…")
        rules=[rule for rule in self.state.knowledge.merge_rules.values() if rule.enabled]
        def work():
            result={}
            if scoped_managers:
                analyzers={}
                for path,manager in scoped_managers:
                    root=Path(manager.root)
                    root_key=str(root).casefold()
                    analyzer=analyzers.get(root_key)
                    if analyzer is None:
                        analyzer=FolderAuditAnalyzer(root,rules)
                        analyzers[root_key]=analyzer
                    if issues:=analyzer.audit(path):result[path]=issues
            else:
                for _profile,manager in self.dataset.scoped_profile_managers():
                    if manager.root is None:continue
                    analyzer=FolderAuditAnalyzer(manager.root,rules)
                    result.update({
                        path:issues for path in manager.images
                        if (issues:=analyzer.audit(path))
                    })
            return result
        self._folder_audit_downstream_context=(
            dict(downstream_context) if isinstance(downstream_context,dict)
            else None
        )
        task=AsyncCallTask(work); self._folder_audit_task=task
        task.signals.complete.connect(self.folder_audit_complete); task.signals.failed.connect(self.folder_audit_failed)
        self.pool.start(task)
        return True

    def folder_audit_complete(self,issues):
        context=getattr(self,"_folder_audit_downstream_context",None)
        self._folder_audit_downstream_context=None
        self.state.finish_long_operation("dataset"); self._folder_audit_task=None
        if getattr(self,"_shutdown_requested",False):
            self._notify_background_work_finished()
            return
        DatasetPage._invalidate_dataset_search(self)
        self._dataset_search_accepted_spec=DatasetPage._dataset_search_spec(self)
        self._dataset_search_accepted_memory=DatasetSearchProjectionStore.memory_token(self.dataset)
        self.audit_issues=dict(issues); self.filtered=list(self.audit_issues); self.page=0
        self.batch_mode=""; self.batch_search=None; self.batch_selected=set(); self.batch_actions.hide()
        status=f"{len(self.audit_issues):,} image(s) have folder or caption issues"
        if context is not None:
            completed=self._complete_downstream_projection(context)
            status+=(
                " • saved organization scope reviewed"
                if completed else
                " • a newer organization review remains pending"
            )
        self.header.set_status(status)
        self.render_page()
        self._notify_background_work_finished()

    def folder_audit_failed(self,message: str):
        self._folder_audit_downstream_context=None
        self.state.finish_long_operation("dataset"); self._folder_audit_task=None; self.header.set_status("")
        if not getattr(self,"_shutdown_requested",False):
            QMessageBox.critical(self,"Folder audit failed",message)
        self._notify_background_work_finished()

    def _update_stage_tab_counts(self) -> None:
        from core.image_workflow_state import image_preparation_stage
        tabs=getattr(self,"stage_tabs",None)
        if tabs is None:
            return
        projection_counts=getattr(self,"_projection_stage_counts",None)
        if projection_counts is not None:
            counts=dict(projection_counts)
            unknown=int(getattr(self,"_projection_unknown_count",0))
        else:
            records=getattr(self,"_filter_records",{})
            counts={"unprepared":0,"partial":0,"complete":0}
            unknown=0
            images=getattr(self.dataset,"images",()) if self.dataset else ()
            if not records:
                counts["unprepared"]=len(images)
            for path in images if records else ():
                record=records.get(path)
                if record is None or not record.sidecar_exists:
                    counts["unprepared"]+=1
                    continue
                cached=self._readiness_cache.get(path)
                if cached is None:
                    unknown+=1
                else:
                    counts[image_preparation_stage(cached[1],has_record=True)]+=1
        labels={
            "unprepared":"Unprepared","partial":"Partially Prepared",
            "complete":"Complete",
        }
        for index in range(tabs.count()):
            key=str(tabs.tabData(index))
            pending=(
                " + unchecked (paused)" if getattr(self,"_projection_paused_count",0)
                else " + checking"
            ) if unknown and key!="unprepared" else ""
            tabs.setTabText(
                index,f"{labels[key]} ({counts[key]:,}{pending})",
            )

    def _browse_recovery_stage(self) -> None:
        target=self._recovery_stage
        if target is None:
            return
        for index in range(self.stage_tabs.count()):
            if self.stage_tabs.tabData(index)==target:
                self.stage_tabs.setCurrentIndex(index)
                return

    def _configure_empty_results(self,pending_text: str) -> None:
        stage=DatasetPage.current_stage(self)
        label={"unprepared":"Unprepared","partial":"Partially Prepared",
               "complete":"Complete"}.get(stage,"current")
        self._recovery_stage=None
        self.clear_results_filters_button.hide()
        self.browse_stage_button.hide()
        self.refresh_results_button.hide()
        self.empty_results_panel.setTitle(f"No images shown in {label}")
        if pending_text:
            self.empty_results_panel.setText(pending_text)
            return
        availability=tuple(
            self.dataset.library_availability(profile.stable_id)
            for profile,_manager in self.dataset.scoped_profile_managers()
        )
        if any(value in {"checking","saved_checking"} for value in availability):
            self.empty_results_panel.setText(
                "Included libraries are still checking. Results will update when that finishes."
            )
            return
        if not availability or any(value not in {"current","validated_checking"}
                                   for value in availability):
            self.empty_results_panel.setText(
                "An included library is unavailable or needs a fresh check. "
                "Refresh before assuming this stage has no images."
            )
            self.refresh_results_button.show()
            return
        if self.filter_panel.clear_button.isEnabled():
            self.empty_results_panel.setText(
                f"No {label.lower()} images match the current search or detail filters. "
                "Clear them to see this stage; the preparation stage and library scope stay selected."
            )
            self.clear_results_filters_button.show()
            return
        counts=getattr(self,"_projection_stage_counts",None)
        unknown=int(getattr(self,"_projection_unknown_count",0))
        paused=int(getattr(self,"_projection_paused_count",0))
        if counts is None or unknown or paused:
            self.empty_results_panel.setText(
                "Image stages are still being checked. Choose another stage above if you "
                "know where your image belongs, or wait for current counts."
            )
            return
        alternatives=[key for key in ("unprepared","partial","complete")
                      if key!=stage and int(counts.get(key,0))>0]
        if alternatives and int(counts.get(stage,0))==0:
            target=alternatives[0]
            target_label={"unprepared":"Unprepared","partial":"Partially Prepared",
                          "complete":"Complete"}[target]
            count=int(counts[target])
            self.empty_results_panel.setText(
                f"There are no {label.lower()} images in the included libraries. "
                f"{count:,} {'image is' if count==1 else 'images are'} in {target_label}. "
                "Choose that stage to browse; nothing is moved or changed."
            )
            self._recovery_stage=target
            self.browse_stage_button.setText(f"Browse {target_label} Images")
            self.browse_stage_button.show()
            return
        self.empty_results_panel.setText(
            f"No {label.lower()} images are shown right now. "
            "Choose another stage above or refresh the included libraries."
        )
        self.refresh_results_button.show()

    def _resolution_filter_changed(self):
        self.page=0
        self.render_page()

    def _resolution_review_paths(self):
        selected=self.selected_paths()
        return selected or DatasetPage._resolution_filtered_paths(self,self.filtered)

    @staticmethod
    def _resolution_filtered_paths(owner,paths):
        filter_widget=getattr(owner,"resolution_filter",None)
        return filter_widget.filter_paths(paths) if filter_widget is not None else paths

    def _resolution_status_changed(self,message: str):
        if message:
            self.header.set_status(message)
        elif self.header.status.text().startswith("Checking image dimensions:"):
            self.header.set_status("")

    def render_page(self):
        page_size=max(12,int(getattr(self.settings,"gallery_page_size",24)))
        # Content safety is presentation-only, so slice the saved projection
        # before the gallery's bounded blur/card configuration.
        filter_widget=getattr(self,"resolution_filter",None)
        if filter_widget is not None:filter_widget.observe_population(self.filtered)
        resolution_paths=DatasetPage._resolution_filtered_paths(self,self.filtered)
        total=len(resolution_paths); pages=max(1,(total+page_size-1)//page_size); self.page=min(self.page,pages-1)
        pending_text=DatasetPage._dataset_search_pending_text(self)
        DatasetPage._update_dataset_search_retry_action(self)
        no_library=(
            not pending_text
            and (not self.dataset or not DatasetPage._has_library_roots(self.dataset))
        )
        scoped_managers=getattr(self.dataset,"scoped_managers",None)
        managers=(scoped_managers() if callable(scoped_managers) else (self.dataset,))
        self._has_dataset_images=any(
            bool(getattr(manager,"images",())) for manager in managers
        )
        activity=getattr(self,"search_activity_status",None)
        if activity is not None:
            activity.setText(pending_text); activity.setVisible(bool(pending_text))
        visible=resolution_paths[self.page*page_size:(self.page+1)*page_size]
        policy=content_safety_policy(); missing_ratings=[]
        if self.dataset is not None and policy is not None and (
            self.settings.gallery_hidden_ratings or self.settings.gallery_content_level!="explicit"
            or getattr(self.settings,"gallery_blur_unknown",True)
        ):
            # Ratings affect only preview protection, never paging membership.
            # Request the bounded visible page; startup validation will publish
            # every other exact cached result without a gallery-wide rescan.
            missing_ratings=policy.unclassified([
                path for path in visible
                if DatasetPage._storage_readable(self,path)
            ])
        # An exact image opened while the first status/search projection is
        # still loading may be absent from the temporary empty result. Keep
        # its inspector until the settled filter can make the real decision.
        pending_exact_image=(
            bool(getattr(self,"_filter_state_task",None)
                 or getattr(self,"_dataset_search_task",None)
                 or getattr(self,"_deferred_dataset_filter",None))
            and self.current_path is not None
            and self.dataset is not None
            and self.current_path in self.dataset.images
        )
        if self.current_path not in resolution_paths and not pending_exact_image:
            self.current_path=None; self.current_record=None; self.inspector.hide()
            self._queue_responsive_layout()
        columns=self.gallery_columns(); self._last_gallery_columns=columns
        self.gallery.set_paths(
            visible,columns=columns,card_width=self.settings.gallery_card_width,show_marks=True,allow_edit=True,selected=self.batch_selected,
            active=self.current_path,configure=self._configure_gallery_card,
            incremental=getattr(self,"_defer_expensive_initialization",False),
            batch_size=2,
            empty_text=(
                "No image results available yet." if pending_text else
                "Unknown previews remain protected."
                if missing_ratings else
                "No images in the included libraries. Add images in Ingest or choose another library."
                if self.dataset and DatasetPage._has_library_roots(self.dataset) and not self._has_dataset_images else
                "No images match these filters. Clear the filters or choose another workflow."
                if self.dataset and DatasetPage._has_library_roots(self.dataset) else
                "Choose Libraries on Dashboard, then refresh the included image libraries."
            ),
        )
        self.cards=self.gallery.cards
        self._pending_content_rating_paths=tuple(missing_ratings)
        records=getattr(self,"_filter_records",{})
        missing_states=[
            path for path in visible
            if DatasetPage._storage_readable(self,path)
            and bool(records.get(path) and records[path].sidecar_exists)
            and path not in self._readiness_cache
        ]
        self._pending_workflow_state_paths=(
            tuple(missing_states) if self._filter_state_task is None else ()
        )
        if missing_ratings or missing_states:
            resume=getattr(self,"resume_visible_background_work",None)
            if callable(resume):
                resume()
        if total:
            result=(
                f"Showing {total:,} previous {'result' if total==1 else 'results'}; current search needs retry."
                if DatasetPage._dataset_search_retry_available(self) else
                f"{total:,} matching image(s)"
            )
        elif not self.dataset or not DatasetPage._has_library_roots(self.dataset):
            result="No image library is open. Choose or refresh libraries to begin."
        elif pending_text:
            result="No image results available yet."
        elif missing_ratings:
            result="Unknown previews remain protected."
        else:
            result="No images match these filters. Clear the filters or choose another workflow."
        self.result_label.setText(result)
        update_stage_counts=getattr(self,"_update_stage_tab_counts",None)
        if callable(update_stage_counts):
            update_stage_counts()
        self.page_label.setText(f"Page {self.page+1} of {pages}" if total else "No matching images")
        self.previous.setEnabled(self.page>0); self.next.setEnabled(self.page+1<pages)
        has_library=bool(
            (self.dataset and DatasetPage._has_library_roots(self.dataset)) or total
        )
        current_workflow=self.workflow_tabs.currentWidget()
        full_workflow=current_workflow in tuple(
            widget for widget in (
                getattr(self,"image_identity_correction",None),
                getattr(self,"training_caption_projection",None),
            ) if widget is not None
        )
        empty_library=bool(has_library and not self._has_dataset_images and not pending_text)
        if empty_library:
            availability=tuple(
                self.dataset.library_availability(profile.stable_id)
                for profile,_manager in self.dataset.scoped_profile_managers()
            )
            ready=bool(availability) and all(value in {
                "current","validated_checking",
            } for value in availability)
            checking=not ready and any(value in {
                "checking","saved_checking",
            } for value in availability)
            if checking:
                self.empty_library_panel.setTitle("Checking included libraries")
                self.empty_library_panel.setText(
                    "The library scan is still running. Images and next actions will appear when it finishes."
                )
            elif not ready:
                self.empty_library_panel.setTitle("Image libraries need attention")
                self.empty_library_panel.setText(
                    "No images are available yet. Refresh the included libraries or choose a different library."
                )
            else:
                self.empty_library_panel.setTitle("No images in the included libraries")
                self.empty_library_panel.setText(
                    "Add and review images in Ingest, then move them into a library to see them here."
                )
            self.open_ingest_button.setVisible(ready)
            self.refresh_empty_libraries_button.setVisible(not ready and not checking)
            self.empty_choose_libraries_button.setVisible(not checking)
        empty_results=bool(has_library and self._has_dataset_images and not total
                           and not full_workflow)
        if empty_results:
            self._configure_empty_results(pending_text)
        self.workflow_tabs.setVisible((has_library and not empty_library) or full_workflow)
        self.gallery_toolbar.setVisible(has_library and bool(total) and not full_workflow)
        self.result_label.setVisible(has_library and bool(total) and not full_workflow)
        self.gallery_density.setVisible(bool(total) and not full_workflow)
        self.selection_header.setVisible(bool(total) and not full_workflow)
        self.select_page_button.setText(f"Select Page ({len(visible):,})")
        self.select_all_button.setText(f"Select All ({total:,})")
        self.previous.setVisible(pages>1 and not full_workflow); self.next.setVisible(pages>1 and not full_workflow); self.page_label.setVisible(pages>1 and not full_workflow)
        self.selection_group.setVisible(bool(total) and not full_workflow)
        self.gallery_scroll.setVisible(not full_workflow and not no_library
                                       and not empty_library and not empty_results)
        self.empty_results_panel.setVisible(empty_results)
        self.no_library_panel.setVisible(not full_workflow and no_library)
        self.empty_library_panel.setVisible(not full_workflow and empty_library)
        if full_workflow:
            self.batch_actions.hide()
            self.inspector.hide()
        self.update_selection_controls()
        self._queue_responsive_layout()

    def resume_visible_background_work(self):
        """Start bounded visible-page reads only after navigation has settled."""
        self._visible_background_timer.start()

    def _start_visible_background_work(self):
        if not self.isVisible() or not self.window().isVisible():
            return
        ratings=self._pending_content_rating_paths
        self._pending_content_rating_paths=()
        if ratings:
            self.content_ratings_requested.emit(ratings)
        states=self._pending_workflow_state_paths
        if states and self._filter_state_task is None:
            if self._start_workflow_filter_scan(states,automatic=True):
                self._pending_workflow_state_paths=()
            else:
                self._visible_background_timer.start()

    def gallery_columns(self):
        width=max(400,self.gallery_scroll.viewport().width())
        return max(2,min(14,width//(max(112,int(self.settings.gallery_card_width))+12)))

    def gallery_density_changed(self,page_size=None,card_width=None):
        if page_size is None or card_width is None: page_size,card_width=self.gallery_scale.current_data()
        self.settings.gallery_page_size=int(page_size); self.settings.gallery_card_width=int(card_width); self.settings.save()
        self.page=0; self.render_page()

    def show_current_inspector(self):
        if self.current_path is None:
            return
        self._set_summary_visible(True)
        self._narrow_inspector_open=True
        self.update_responsive_layout()

    def show_dataset_browser(self):
        self._narrow_inspector_open=False
        self.update_responsive_layout()

    def toggle_summary(self):
        self._set_summary_visible(not self._summary_visible)

    def _set_summary_visible(self,visible: bool,*,persist: bool=True):
        self._summary_visible=bool(visible)
        if not self._summary_visible:
            self._narrow_inspector_open=False
        elif self._dataset_narrow_layout and self.current_path is not None:
            self._narrow_inspector_open=True
        if persist and self.ui_settings is not None:
            self.ui_settings.setValue(
                "dataset/summary_visible",self._summary_visible,
            )
        self._update_summary_visibility_button()
        self.update_responsive_layout()

    def _update_summary_visibility_button(self):
        button=getattr(self,"summary_visibility_button",None)
        if button is None:
            return
        from .navigation_icons import navigation_mode_icon
        button.setIcon(navigation_mode_icon(not self._summary_visible,right_side=True))
        button.setChecked(not self._summary_visible)
        button.setToolTip(
            "Expand Dataset summary" if not self._summary_visible else
            "Collapse Dataset summary to give the gallery more space"
        )
        button.setAccessibleDescription(button.toolTip())

    def _sync_gallery_toolbar_layout(self,narrow: bool) -> None:
        if narrow==getattr(self,"_gallery_toolbar_narrow",False):
            return
        primary=self.gallery_primary_row; secondary=self.gallery_narrow_row
        if narrow:
            primary.removeWidget(self.selection_header)
            primary.removeWidget(self.gallery_density)
            secondary.insertWidget(0,self.selection_header)
            secondary.insertWidget(1,self.gallery_density)
        else:
            secondary.removeWidget(self.selection_header)
            secondary.removeWidget(self.gallery_density)
            primary.insertWidget(1,self.selection_header)
            primary.insertWidget(2,self.gallery_density)
        self._gallery_toolbar_narrow=narrow

    def _sync_adaptive_panes(self) -> None:
        narrow=self.width()<1100
        was_narrow=getattr(self,"_dataset_narrow_layout",False)
        current=self.workflow_tabs.currentWidget()
        full_workflow=current in (
            self.image_identity_correction,self.training_caption_projection,
        )
        if narrow and not was_narrow:
            self._narrow_inspector_open=bool(
                self.current_path is not None and self.inspector.isVisible()
            )
        self._dataset_narrow_layout=narrow
        self.header.set_compact_mode(narrow)
        self._sync_gallery_toolbar_layout(narrow)
        self.preparation_controls.summary.setWordWrap(narrow)
        show_inspector=bool(
            self._summary_visible and narrow and not full_workflow and self.current_path is not None
            and self._narrow_inspector_open
        )
        has_library=bool(self.dataset and DatasetPage._has_library_roots(self.dataset))
        self.inspector_back_button.setVisible(show_inspector)
        if narrow:
            self.browser.setVisible(not show_inspector)
            self.inspector.setVisible(show_inspector)
        else:
            self.browser.show()
            self.inspector.setVisible(
                self._summary_visible and has_library and not full_workflow
                and getattr(self,"_has_dataset_images",False)
            )
        self.inspect_current_button.setVisible(
            self._summary_visible and narrow and not full_workflow and not show_inspector
            and self.current_path is not None
        )

    def resizeEvent(self,event):
        super().resizeEvent(event); self._queue_responsive_layout()

    def eventFilter(self,watched,event):
        if (watched is getattr(self,"_gallery_viewport",None)
                and event.type()==QEvent.Resize):
            self._queue_responsive_layout()
        return super().eventFilter(watched,event)

    def _queue_responsive_layout(self):
        """Coalesce resize and splitter bursts into one frame-bounded reflow."""
        self._responsive_layout_timer.start()

    def update_responsive_layout(self):
        self._responsive_layout_timer.stop()
        DatasetPage._sync_workflow_geometry(self)
        DatasetPage._sync_adaptive_panes(self)
        columns=self.gallery_columns()
        if self.filtered and getattr(self,"_last_gallery_columns",None)!=columns:
            self._last_gallery_columns=columns; self.gallery.set_columns(columns)

    def _configure_gallery_card(self,path,card):
        card.set_generation_actions(True)
        from core.image_workflow_state import image_knowledge_coverage
        card.set_compact_mode(True,problem_implied=DatasetPage.current_stage(self)=="partial")
        card.set_knowledge_coverage()
        export_actions=(
            (("export_training","Export Training TXT…","Export Selected Training TXT…"),)
            if DatasetPage.current_stage(self) in {"complete","all"} else ()
        )
        card.set_file_actions((
            ("add_to_collection","Add to Collection…","Add Selected to Collection…"),
            ("resize","Resize This Image…","Resize Selected Images…"),
            ("library","Move This Image to Library…","Move Selected Images to Library…"),
            ("reorganize","Reorganize This Image…","Reorganize Selected Images…"),
            ("recycle","Move This Image to Recycle Bin…","Move Selected Images to Recycle Bin…"),
            ("ingest","Move This Image to Ingest…","Move Selected Images to Ingest…"),
            ("backup_metadata","Back Up Tags to Metadata…","Back Up Selected Tags to Metadata…"),
        )+export_actions,read_only_actions=("add_to_collection",))
        availability=DatasetPage._storage_availability(self,path)
        if availability is not None and str(
            getattr(availability.state,"value",availability.state)
        )!="available":
            detail=" ".join(value for value in (
                str(getattr(availability,"detail","") or ""),
                str(getattr(availability,"recovery","") or ""),
            ) if value).strip()
            card.set_visual_status("orange",detail)
            card.set_note(str(getattr(availability,"label","Storage attention required")))
            card.set_ai_scan_history()
            return
        cached=self._readiness_cache.get(path)
        if cached is not None:
            workflow=cached[1]
            card.set_knowledge_coverage(image_knowledge_coverage(workflow))
            card.set_ai_scan_history(
                getattr(workflow,"last_sdxl_ai_scan",""),
                getattr(workflow,"last_plain_language_ai_scan",""),
            )
        else:
            card.set_ai_scan_history()
        issues=self.audit_issues.get(path,[])
        if issues:
            card.set_note(issues[0].message)
            statuses=[status_from_severity(issue.severity) for issue in issues]
            status=next((value for value in ("red","orange","yellow") if value in statuses),"yellow")
            card.set_visual_status(status,issues[0].message)
        else:
            if cached is not None:
                workflow=cached[1]
                card.set_visual_status(workflow.color,workflow.detail)
                card.set_note(f"{workflow.label} • Image Knowledge {workflow.score}/100")
            else:
                card.set_visual_status("neutral","Checking Image Knowledge in the background")
                card.set_note("Checking image data")

    def change_page(self,direction: int):
        self.page=max(0,self.page+direction); self.render_page()

    @staticmethod
    def _tag_values(text: str):
        return [value.strip() for value in text.split(",") if value.strip()]

    def _confirm_focused_identity_creation(self,value: str) -> bool:
        dialog=QMessageBox(self); dialog.setWindowTitle("Create Reusable SDXL Tag?")
        dialog.setIcon(QMessageBox.Question)
        dialog.setText(f'Create “{value}” in reusable System Knowledge?')
        dialog.setInformativeText(
            "The new tag will be saved immediately as Uncategorized with a "
            "recovery backup. The focused review will then open; no image changes "
            "until you apply its page decisions."
        )
        create=dialog.addButton("Create Tag and Start Review",QMessageBox.AcceptRole)
        cancel=dialog.addButton(QMessageBox.Cancel); dialog.setDefaultButton(cancel)
        dialog.exec(); return dialog.clickedButton() is create

    def _resolve_focused_review_target(self,target: str) -> str | None:
        service=ConfirmedTagIdentityService(
            self.state.knowledge,
            evidence_rejected=active_automation_engine(
                self.state.knowledge,
            ).is_evidence_rejected,
        )
        plan=service.plan((target,),confirmation=True)
        outcome=review_removed_identities(
            self,service,plan,source="dataset_focused_review_confirmation",
            continuation=(
                "The focused review will then continue with its filters and "
                "selection; no image changes until you apply page decisions."
            ),
            confirmation=True,
        )
        if outcome.cancelled or outcome.blocked:
            return None
        plan=outcome.plan
        if plan.blockers:
            QMessageBox.warning(
                self,"SDXL Tag Needs Review",
                "The focused review was not started because its target does not "
                "resolve to one reusable System Knowledge identity.\n\n"
                +"\n".join(
                    entry.message or f"{entry.raw_value}: {entry.status}"
                    for entry in plan.blockers
                ),
            )
            return None
        if plan.new_entries and not self._confirm_focused_identity_creation(target):
            return None
        try:
            resolution=(
                service.commit(
                    plan,source="dataset_focused_review_confirmation",
                )
                if plan.new_entries else
                service.require_existing((target,),confirmation=True)
            )
        except (ConfirmedTagIdentityError,OSError,RuntimeError,ValueError) as error:
            QMessageBox.critical(
                self,"SDXL Tag Was Not Resolved",
                "The focused review was not changed. "+str(error),
            )
            return None
        return resolution.values[0] if resolution.values else None

    def start_batch_review(self):
        if not self.dataset or not DatasetPage._has_library_roots(self.dataset):
            QMessageBox.information(self,"Load libraries","Load one or more image libraries before starting batch review."); return
        DatasetPage._invalidate_dataset_search(self)
        mode=("verify","missing","conflict")[self.batch_kind.currentIndex()]
        if mode=="conflict":
            if not getattr(self,"_conflicts_ready",True):
                QMessageBox.information(
                    self,"Conflict checks are still preparing",
                    self._conflict_error or
                    "Wait for the current System Knowledge conflict check to finish, then try again.",
                )
                return
            rule_id=str(self.conflict_rule.currentData() or "")
            rule=next(
                (item for item in self.conflicts.rules if rule_id in item.accepted_ids),
                None,
            )
            if rule is None:
                QMessageBox.information(
                    self,"Choose a conflict",
                    "Choose the conflict relationship you want to review.",
                )
                return
            self.open_automation.emit(
                "review.conflicts.guided_checks",
                {
                    "rule_id":rule.id,
                    "relationship_id":rule.relationship_id or rule.id,
                    "origin":"dataset_batch",
                    "scope_kind":"library",
                },
            )
            return
        if not self._exception_storage_ready():
            return
        target=self.batch_target.text().strip()
        if not target:
            QMessageBox.information(self,"Enter an SDXL Tag","Enter the SDXL Tag you want to verify or add."); return
        if mode=="missing":
            target=self._resolve_focused_review_target(target)
            if not target:return
            self.batch_target.setText(target)
        required=self._tag_values(self.batch_required.text()); excluded=self._tag_values(self.batch_excluded.text())
        search=MissingTagSearch(target,required,excluded,mode=mode); target_key=normalize_tag(target)
        required_keys={normalize_tag(value) for value in required}; excluded_keys={normalize_tag(value) for value in excluded}
        records=self.dataset.index.all_records() if self.dataset.index else []
        tag_map={Path(item.path):{normalize_tag(tag) for tag in item.tags} for item in records}
        matches=[]
        for path in self.dataset.images:
            if not DatasetPage._storage_writable(self,path):
                continue
            tags=tag_map.get(path,set())
            if required_keys and not required_keys.issubset(tags): continue
            if excluded_keys and excluded_keys.intersection(tags): continue
            if mode=="verify" and target_key not in tags: continue
            if mode=="missing" and target_key in tags: continue
            if not self.ignore_memory.isChecked() and (
                (context:=self._library_context_for(path))[0] is not None
                and self.exceptions.hidden(search,path,*context)
            ): continue
            matches.append(path)
        self.batch_mode=mode; self.batch_search=search; self.batch_selected=set(); self.filtered=matches; self.page=0
        self.workflow_tabs.setCurrentIndex(1)
        self.batch_actions.show()
        if mode=="verify":
            self.batch_instruction.setText(f'Check images where “{target}” is incorrect. Checked images will lose the SDXL Tag; unchecked images keep it.')
        elif mode=="missing":
            self.batch_instruction.setText(f'Check images that need “{target}”. Unchecked images will be remembered as intentional exceptions for this exact search.')
        self.apply_batch_button.setText("Apply Page Decisions")
        self.render_page()

    def _batch_kind_changed(self,index: int):
        conflict=index==2
        self.batch_target.setVisible(not conflict); self.conflict_rule.setVisible(conflict)
        self.ignore_memory.setVisible(not conflict)
        self.batch_required.setVisible(not conflict)
        self.batch_excluded.setVisible(not conflict)

    def end_batch_review(self):
        self.batch_mode=""; self.batch_search=None; self.batch_selected=set(); self.batch_actions.hide(); self.apply_filters()

    def mark_batch_image(self,path: str,selected: bool):
        if selected: self.batch_selected.add(path)
        else: self.batch_selected.discard(path)
        DatasetPage._dataset_search_selection_changed(self)
        self.update_selection_controls()

    def replace_batch_selection(self,values):
        self.batch_selected={str(value) for value in values}; self.update_selection_controls()
        DatasetPage._dataset_search_selection_changed(self)

    def _update_preparation_emphasis(self,stage=None):
        stage=stage or DatasetPage.current_stage(self)
        inspecting_one_selected=(
            len(self.batch_selected)==1
            and str(getattr(self,"current_path",None)) in self.batch_selected
        )
        self.preparation_controls.set_context(
            refresh=stage=="complete",
            secondary=stage=="complete" or inspecting_one_selected,
        )

    def update_selection_controls(self):
        # Selection is already constrained to gallery results. Do not stat every
        # path in a large/network-backed Dataset merely to update this label.
        count=len(self.batch_selected)
        selected_paths=[Path(value) for value in self.batch_selected]
        held=sum(not DatasetPage._storage_writable(self,path) for path in selected_paths)
        self.selection_label.setText(
            (f"Batch selection: {count:,}"+(f" • {held:,} unavailable, conflicted, or read-only" if held else ""))
            if count else "Batch selection: none"
        )
        busy=(self._projection_task is not None
              or self._library_move_task is not None or self._preparation_task is not None
              or getattr(self,"_batch_task",None) is not None
              or getattr(self,"_organization_transfer_task",None) is not None
              or getattr(self,"_training_export_task",None) is not None
              or getattr(self,"_training_candidate_task",None) is not None
              or getattr(self,"_metadata_backup_task",None) is not None
              or history_dialogs_busy(self))
        if hasattr(self,"history_button"):
            self.history_button.setEnabled(not busy and bool(count or getattr(self,"current_path",None)))
        if hasattr(self,"undo_move_button"):
            scope=getattr(self,"_move_history_scope",None)
            self.undo_move_button.setEnabled(not busy and ((scope is not None and scope.available)
                or bool(getattr(self,"_organization_recycle_ids",()))))
            if hasattr(self,"organization_result_button"):
                self.organization_result_button.setEnabled(self.undo_move_button.isEnabled())
        stage=DatasetPage.current_stage(self)
        plan_scope=getattr(self,"plan_training_scope_action",None)
        if plan_scope is not None:
            plan_scope.setEnabled(
                bool(self.filtered) and self.dataset is not None
                and not self.batch_mode and not busy
            )
        resume_training=getattr(self,"resume_training_plan_action",None)
        if resume_training is not None:
            resume_training.setEnabled(
                not self.batch_mode and not busy
                and self.dataset is not None
                and bool(self.training_plan_drafts.all_drafts())
            )
        if hasattr(self,"training_goal_scope"):
            DatasetPage._update_goal_scope_controls(self,count,busy)
        preparation_paths=self.preparation_paths()
        queue_busy=(getattr(self,"_preparation_request_queue",None) is not None
                    and self._preparation_task is self._preparation_request_queue)
        queue_busy=queue_busy and not any((self._projection_task,self._library_move_task,
            getattr(self,"_batch_task",None),getattr(self,"_organization_transfer_task",None),
            getattr(self,"_training_export_task",None),getattr(self,"_training_candidate_task",None),
            getattr(self,"_metadata_backup_task",None),history_dialogs_busy(self)))
        preparation_enabled=not self.batch_mode and (not busy or queue_busy)
        if count and held:
            preparation_paths=[]
            button_text=f"Prepare Selected ({count:,})"
            description="all selected images must be locally available and writable"
            preparation_enabled=False
        elif count:
            button_text=f"Prepare Selected ({len(preparation_paths):,})"
            description=f"{len(preparation_paths):,} explicitly selected image(s)"
        elif preparation_paths:
            button_text=(
                f"Prepare Unprepared Images ({len(preparation_paths):,})"
                if stage=="unprepared" else
                f"Continue Preparation ({len(preparation_paths):,})"
            )
            description=(
                f"all {len(preparation_paths):,} image(s) in the "
                f"{stage.replace('_',' ')} stage"
            )
        else:
            button_text=(
                "Re-run Preparation…"
                if stage=="complete" else
                "Prepare Images"
            )
            description=(
                "select complete images to intentionally re-run preparation"
                if stage=="complete" else
                "no writable images are available in this stage"
            )
        if stage=="complete" and count:
            button_text=f"Re-run Preparation ({count:,})…"
        self.preparation_controls.set_scope(
            len(preparation_paths),button_text=button_text,
            description=description,enabled=preparation_enabled,
        )
        self._update_preparation_emphasis(stage)
        self.preparation_controls.setVisible(
            stage in {"unprepared","partial"} or bool(count)
        )
        self.preparation_controls.setEnabled(not self.batch_mode and (not busy or queue_busy))
        if hasattr(self,"run_ai_selected_button"):
            self.run_ai_selected_button.setText("AI-Only Review...")
            self.run_ai_selected_button.setVisible(bool(count) and not self.batch_mode)
            self.run_ai_selected_button.setEnabled(not busy and not held)

    def export_selected_captions(self):
        self._export_training_captions(self.selected_paths())

    @staticmethod
    def _training_candidate_scope_key(paths):
        return tuple(str(Path(path)).casefold() for path in paths)

    def plan_selected_training_dataset(self):
        if not self.selected_paths():
            QMessageBox.information(
                self,"Select images",
                "Select one or more visible Dataset images to plan from first.",
            )
            return
        DatasetPage.plan_training_dataset_scope(self)

    def add_selected_to_concept(self,*,paths=None):
        if not DatasetPage._dataset_search_results_ready(self): return
        selected=tuple(self.selected_paths() if paths is None else paths)
        if not selected:
            QMessageBox.information(self,"Select collection images","Select images in Dataset, then add them to a named collection.")
            return
        DatasetPage.open_concept_groups(self,include_selection=True,selected_paths=selected)

    def open_concept_groups(self,checked=False,*,include_selection=False,selected_paths=None,selected_group_id=""):
        from core.concept_groups import ConceptGroupStore
        from .concept_group_dialog import ConceptGroupDialog
        selection_at_open=tuple(self.selected_paths())
        selected=tuple(dict.fromkeys(Path(value) for value in (
            (selection_at_open if selected_paths is None else selected_paths)
            if include_selection else ()
        )))
        dataset=self.dataset; generation=getattr(self,"_dataset_scope_generation",0)
        if selected and not DatasetPage._storage_scope_ready(self,selected,write=False,action="add collection membership"):
            return
        def dataset_current():
            return self.dataset is dataset and getattr(self,"_dataset_scope_generation",0)==generation
        def current():
            return dataset_current() and (not selected or tuple(self.selected_paths())==selection_at_open)
        existing=getattr(self,"_concept_group_dialog",None)
        if (existing is not None and not existing._closed
                and not existing.has_active_background_work()
                and (existing.dataset is not dataset
                     or getattr(existing,"_dataset_scope_generation",None)!=generation)):
            if existing._details_dirty or existing._rules_dirty:
                existing.status.setText(
                    "The Dataset library scope changed. Save or discard the current draft, "
                    "then close and reopen Training Collections to use the current libraries."
                )
                existing.show(); existing.raise_(); existing.activateWindow()
                return
            existing.close()
            existing=None
        if existing is not None and not existing._closed:
            if not existing.has_active_background_work():
                existing.dataset=dataset
                existing.selected_paths=selected
                existing.scope_current=current
                existing.dataset_current=dataset_current
                existing._update_actions()
                if selected_group_id and not existing._details_dirty and not existing._rules_dirty:
                    existing.reload(select_id=selected_group_id)
                elif selected_group_id:
                    existing.status.setText("Save or discard the current collection draft, then reopen the collection selected in Training.")
            existing.show(); existing.raise_(); existing.activateWindow()
            return
        from core.structured_history_workflow import collection_history_factory
        from .history_workflow import structured_history_configuration, open_collection_history
        configuration=structured_history_configuration(self)
        collection_store=ConceptGroupStore(USER_DATA_DIR/"concept_groups.json",
            history_factory=collection_history_factory(configuration))
        dialog=ConceptGroupDialog(self.state.jobs,collection_store,dataset,
            selected_paths=selected,scope_current=current,dataset_current=dataset_current,
            history_opener=lambda group_id, callback, **options: open_collection_history(self, configuration,
                collection_store, group_id, dataset, scope_current=dataset_current, on_complete=callback, **options),
            selected_group_id=selected_group_id,parent=self)
        dialog.training_requested.connect(
            lambda group, dialog=dialog: self._complete_concept_training_handoff(
                dialog, group
            )
        )
        dialog.image_requested.connect(self._preview_collection_image)
        dialog.background_work_finished.connect(self._notify_background_work_finished)
        dialog._dataset_scope_generation=generation
        self._concept_group_dialog=dialog
        dialog.show()

    def _complete_concept_training_handoff(self, dialog, group) -> None:
        """Close a collection only when its synchronous goal decision proceeds."""
        if getattr(self, "_shutdown_requested", False):
            return
        window=self.window() if callable(getattr(self,"window",None)) else None
        open_training=getattr(window,"open_training_workspace",None)
        accepted=(open_training(group=group,collection_reviewed=True)
                  if callable(open_training) else self.plan_concept_training(group))
        if accepted:
            dialog.complete_training_handoff()

    def _preview_collection_image(self,member):
        from core.federated_dataset import LibraryImageIdentity
        from .gallery import ImageCard
        try:
            identity=LibraryImageIdentity(member["library_id"],member["relative_path"])
            path=Path(self.dataset.path_for_identity(identity))
            if self.dataset.identity_for_path(path)!=identity:
                raise ValueError("The image no longer belongs to this library.")
        except (AttributeError,KeyError,ValueError,OSError) as error:
            QMessageBox.information(self,"Collection image unavailable",str(error)); return
        if not DatasetPage._storage_scope_ready(self,[path],write=False,action="view collection image"):
            return
        card=ImageCard(path,self)
        try:
            card.show_large_preview()
        finally:
            card.deleteLater()

    def plan_concept_training(self,group) -> bool:
        from core.concept_groups import group_fingerprint
        from core.federated_dataset import LibraryImageIdentity
        if getattr(self, "_shutdown_requested", False):
            return False
        if self._training_candidate_task is not None:
            show_operation_in_progress(self,"Training dataset planning"); return False
        frozen=copy.deepcopy(group)
        if self.dataset is None or not frozen["members"]:
            QMessageBox.information(self,"Collection needs images","Add images from a named library before building training data.")
            return False
        dataset=self.dataset; generation=getattr(self,"_dataset_scope_generation",0)
        try:
            identities={Path(dataset.path_for_identity(LibraryImageIdentity(row["library_id"],row["relative_path"]))):
                LibraryImageIdentity(row["library_id"],row["relative_path"]) for row in frozen["members"]}
        except (KeyError,ValueError,OSError):
            QMessageBox.information(self,"Collection library unavailable","Restore the collection's named libraries before planning. No substitute images were selected.")
            return False
        paths=tuple(identities)
        dialog=TrainingCandidateGoalDialog(len(paths),self.export_profiles,self.settings.export_content_level,self,
            current_profile_id=self._training_export_profile_id,
            scope_choices=(TrainingScopeChoice(TrainingScopeKind.SELECTED,len(paths),f"Collection: {frozen['name']}"),),
            collection_name=frozen["name"],settings=self.settings,jobs=self.state.jobs)
        dialog.goal_text.setText((f"Teach {frozen['name']}. "+frozen["description"])[:2000])
        dialog.goal_kind.setCurrentIndex(dialog.goal_kind.findData("concept"))
        if dialog.exec()!=QDialog.Accepted: return False
        if getattr(self, "_shutdown_requested", False):
            return False
        if self.dataset is not dataset or getattr(self,"_dataset_scope_generation",0)!=generation:
            QMessageBox.information(self,"Collection library changed","Reopen the collection to review its current library configuration.")
            return False
        self._start_training_candidate_plan(paths,dialog.training_goal(),dialog.selected_profile(),identities=identities,
            configuration=dialog.selected_configuration(),guard_selection=False,concept_group=frozen,
            source_label=f"Collection {frozen['name']} [{frozen['id']}] snapshot {group_fingerprint(frozen)}")
        return True

    def _training_view_signature(self):
        def value(name):
            control=getattr(self,name,None)
            return str(control.currentData() or "all") if control is not None else "all"
        search=getattr(self,"search",None)
        return (
            DatasetPage.current_stage(self),value("state_filter"),value("projection_filter"),
            search.text().strip() if search is not None else "",
            tuple(getattr(self.dataset,"explicit_scope_library_ids",())),
        )

    def _training_scope_is_current(self,context):
        if context is None:
            return True
        if (
            self.dataset is not context["dataset"]
            or getattr(self,"_dataset_scope_generation",0)!=context["generation"]
            or DatasetPage._training_view_signature(self)!=context["view"]
        ):
            return False
        paths=(
            self.selected_paths() if context["kind"]==TrainingScopeKind.SELECTED
            else getattr(self,"filtered",())
        )
        return DatasetPage._training_candidate_scope_key(paths)==context["paths"]

    def plan_training_dataset_scope(self):
        if self._training_candidate_task is not None:
            show_operation_in_progress(self,"Training dataset planning")
            return
        if self.dataset is None:
            QMessageBox.information(
                self,"Dataset library unavailable",
                "Open a named library before planning training images.",
            )
            return
        if not DatasetPage._dataset_search_results_ready(self):
            return
        selected=tuple(Path(path) for path in self.selected_paths())
        matching=tuple(Path(path) for path in getattr(self,"filtered",()))
        if not selected and not matching:
            QMessageBox.information(self,"No matching images","Adjust the current Dataset search or stage before planning.")
            return
        view=DatasetPage._training_view_signature(self)
        stage={"partial":"Partially Prepared","complete":"Complete","unprepared":"Unprepared","all":"All stages"}.get(view[0],view[0])
        description=f"{stage} • Search: {view[3][:240]}" if view[3] else f"{stage} • Current filters"
        choices=(
            TrainingScopeChoice(TrainingScopeKind.SELECTED,len(selected),"Only the images explicitly selected in Dataset."),
            TrainingScopeChoice(TrainingScopeKind.MATCHING,len(matching),description),
        )
        contexts={kind:{
            "kind":kind,"dataset":self.dataset,
            "generation":getattr(self,"_dataset_scope_generation",0),"view":view,
            "paths":DatasetPage._training_candidate_scope_key(paths),
        } for kind,paths in ((TrainingScopeKind.SELECTED,selected),(TrainingScopeKind.MATCHING,matching))}
        dialog=TrainingCandidateGoalDialog(
            len(selected or matching),self.export_profiles,self.settings.export_content_level,
            self,current_profile_id=self._training_export_profile_id,
            scope_choices=choices,
            settings=self.settings,jobs=getattr(getattr(self,"state",None),"jobs",None),
        )
        if dialog.exec()!=QDialog.Accepted:
            return
        kind=dialog.selected_scope_kind()
        context=contexts[kind]
        paths=selected if kind==TrainingScopeKind.SELECTED else matching
        if not paths or not DatasetPage._training_scope_is_current(self,context):
            QMessageBox.information(self,"Training scope changed","The library, search or selection changed. Reopen the scope choice to review its current exact images.")
            return
        if not DatasetPage._storage_scope_ready(self,paths,write=False,action="plan a training dataset"):
            return
        goal=dialog.training_goal()
        profile=dialog.selected_profile()
        if getattr(dialog,"_guided_profile",None) is None:
            self._training_export_profile_id=profile.profile_id
        self._start_training_candidate_plan(
            paths,goal,profile,
            configuration=dialog.selected_configuration() if hasattr(dialog,"selected_configuration") else None,
            source_label="Explicit Dataset selection" if kind==TrainingScopeKind.SELECTED else "Explicit Dataset matching results",
            guard_selection=kind==TrainingScopeKind.SELECTED,scope_context=context,
        )

    def resume_training_dataset_draft(self,*,draft=None):
        if self._training_candidate_task is not None:
            show_operation_in_progress(self,"Training dataset planning")
            return
        if self.dataset is None:
            QMessageBox.information(
                self,"Dataset library unavailable",
                "Open a named library before resolving a saved training draft.",
            )
            return
        if draft is None:
            self.training_plan_drafts.load()
            drafts=self.training_plan_drafts.all_drafts()
            if not drafts:
                detail=(self.training_plan_drafts.load_error
                        or "Save a completed candidate preview before resuming a draft.")
                QMessageBox.information(self,"No training draft available",detail)
                return
            picker=TrainingPlanDraftPickerDialog(drafts,self)
            if picker.exec()!=QDialog.Accepted:
                return
            draft=picker.selected_draft()
        if draft is None:
            return
        resolution=draft.resolve_selection(self.dataset)
        if not resolution.ready:
            QMessageBox.warning(
                self,"Training draft scope unavailable",
                "The saved selection could not be resolved exactly, so CollectiGen "
                "did not substitute other images or build a partial plan.\n\n"
                +"\n".join(resolution.unresolved[:8]),
            )
            return
        if not DatasetPage._storage_scope_ready(
            self,resolution.paths,write=False,action="resume a training dataset draft",
        ):
            return
        self._training_export_profile_id=draft.profile.profile_id
        self._start_training_candidate_plan(
            resolution.paths,draft.goal,draft.profile,
            identities=resolution.identities,overrides=draft.overrides(),
            permission_disposition=draft.permission_disposition(),
            configuration=draft.configuration(),
            source_label=str(
                draft.plan_manifest["selection_scope"]["source_label"]
            ),
            guard_selection=False,draft=draft,
        )

    def resume_training_package_recovery_draft(self) -> bool:
        """Match exact saved work while retaining the selected package receipt."""
        context=getattr(self,"_training_package_recovery_return",None)
        if context is None:
            # Rolled-back staging has no recovery authority; use ordinary drafts.
            self.resume_training_dataset_draft()
            return False
        if (not isinstance(context,tuple) or len(context)!=3
                or not isinstance(context[2],dict)):
            self.header.set_status("Reopen the exact package receipt before finding its draft.")
            return False
        if self._training_candidate_task is not None:
            show_operation_in_progress(self,"Training dataset planning")
            return False
        self.training_plan_drafts.load()
        if self.training_plan_drafts.load_error:
            self.header.set_status("Saved drafts are unavailable; the exact package receipt remains held.")
            return False
        matches=tuple(draft for draft in self.training_plan_drafts.all_drafts()
                      if draft.plan_manifest==context[2])
        if not matches:
            self.header.set_status("No exact saved draft matches this package plan. The receipt remains held.")
            return False
        picker=TrainingPlanDraftPickerDialog(matches,self)
        if picker.exec()!=QDialog.Accepted:
            self.header.set_status("Draft selection cancelled. The exact package receipt remains held.")
            return False
        draft=picker.selected_draft()
        if (getattr(self,"_training_package_recovery_return",None) is not context
                or draft not in matches or draft.plan_manifest!=context[2]):
            self.header.set_status("The selected draft or package context changed. Reopen the exact receipt.")
            return False
        self.resume_training_dataset_draft(draft=draft)
        return True

    def _start_training_candidate_plan(
        self,paths,goal,profile,*,identities=None,overrides=None,
        permission_disposition=None,configuration=None,
        source_label="Explicit Dataset selection",
        guard_selection=True,draft=None,scope_context=None,concept_group=None,
    ):
        window=self.window() if callable(getattr(self,"window",None)) else None
        training_page=getattr(window,"training_page",None)
        if training_page is not None and training_page._review is not None:
            window.open_training_workspace()
            return
        paths=tuple(Path(path) for path in paths)
        dataset=self.dataset
        self._training_candidate_dataset_context=(dataset,getattr(self,"_dataset_scope_generation",0))
        self._training_candidate_scope_context=scope_context
        service=TrainingCandidateSelectionService(
            self.state.knowledge,self.characters,self.categories,
            duplicate_evidence_provider=training_duplicate_provider(self.state),
        )
        started,active=self.state.begin_long_operation(
            "dataset",
            f"Planning a training dataset from {len(paths):,} frozen image(s)",
        )
        if not started:
            show_operation_in_progress(self,active)
            return
        self._training_candidate_scope=DatasetPage._training_candidate_scope_key(
            paths
        )
        self._training_candidate_guard_selection=bool(guard_selection)
        self._training_candidate_draft=draft
        self.header.set_status(
            f"Inspecting {len(paths):,} frozen training candidate(s)…"
        )

        def build_plan(progress,cancelled):
            resolved_identities=dict(identities or {})
            if concept_group is not None:
                from core.concept_groups import resolve_members,verify_plan_membership
                resolution=resolve_members(concept_group,dataset,progress=progress,should_cancel=cancelled)
                if resolution["issues"]:
                    raise ValueError("Collection membership needs review; no partial plan was created.\n"+"\n".join(resolution["issues"][:8]))
                if DatasetPage._training_candidate_scope_key(resolution["paths"])!=DatasetPage._training_candidate_scope_key(paths):
                    raise ValueError("Collection library locations changed before planning.")
            if identities is None and dataset is not None:
                for path in paths:
                    identity=dataset.identity_for_path(path)
                    if identity is not None:
                        resolved_identities[path]=identity
            plan=service.plan(
                paths,goal,profile,identities=resolved_identities,
                overrides=overrides,source_label=source_label,
                permission_disposition=permission_disposition,
                configuration=configuration,
                progress=progress,should_cancel=cancelled,
            )
            if concept_group is not None: verify_plan_membership(concept_group,plan)
            return plan

        task=CancelableProgressCallTask(build_plan)
        self._training_candidate_task=task
        if training_page is not None:
            DatasetPage._sync_training_workspace(self)
            training_page.set_planning(True,"Inspecting the frozen training scope…")
        self.cancel_projection_button.setText("Cancel Safely")
        self.cancel_projection_button.setEnabled(True)
        self.cancel_projection_button.show()
        self.update_selection_controls()
        task.signals.progress.connect(self._training_candidate_progress)
        task.signals.complete.connect(self._training_candidate_complete)
        task.signals.cancelled.connect(self._training_candidate_cancelled)
        task.signals.failed.connect(self._training_candidate_failed)
        start_qt_delivery_task(self.pool,task)

    def _training_candidate_progress(self,index,total,name):
        self.state.update_long_operation("dataset",index,total,name)
        self.header.set_status(
            f"Inspecting training candidates: {index:,} of {total:,} • {name}"
        )
        window=self.window() if callable(getattr(self,"window",None)) else None
        if getattr(window,"training_page",None) is not None:
            from .training_workspace import sync_training_progress
            sync_training_progress(window)

    def _finish_training_candidate_task(self):
        self.state.finish_long_operation("dataset")
        self._training_candidate_task=None
        self.cancel_projection_button.hide()
        self.update_selection_controls()

    def _training_candidate_complete(self,plan):
        task=getattr(self,"_training_candidate_task",None)
        if task is not None and task.is_cancelled():
            DatasetPage._training_candidate_cancelled(self)
            return
        expected=self._training_candidate_scope
        self._finish_training_candidate_task()
        if plan.cancelled:
            self._training_candidate_plan=None
            self._training_candidate_draft=None
            self.header.set_status(
                "Training dataset planning cancelled safely. No files were changed."
            )
            self._notify_background_work_finished()
            return
        current=DatasetPage._training_candidate_scope_key(self.selected_paths())
        dataset_context=getattr(self,"_training_candidate_dataset_context",None)
        dataset_changed=bool(dataset_context is not None and (
            getattr(self,"dataset",None) is not dataset_context[0]
            or getattr(self,"_dataset_scope_generation",0)!=dataset_context[1]
        ))
        context_changed=not DatasetPage._training_scope_is_current(
            self,getattr(self,"_training_candidate_scope_context",None),
        )
        if dataset_changed or context_changed or (self._training_candidate_guard_selection and current!=expected):
            self._training_candidate_plan=None
            self._training_candidate_draft=None
            self.header.set_status("Training dataset scope changed")
            if not self._shutdown_requested:
                QMessageBox.information(
                    self,"Training scope changed",
                    "The Dataset library, search or selection changed while the read-only plan was built. "
                    "Nothing was written; select the intended images and build the plan again.",
                )
            self._notify_background_work_finished()
            return
        if self._shutdown_requested:
            self._training_candidate_draft=None
            self._notify_background_work_finished()
            return
        self._training_candidate_plan=plan
        self.header.set_status(
            f"Training plan ready • {plan.count('include'):,} included • "
            f"{plan.count('exclude'):,} excluded"
        )
        dialog=TrainingCandidatePlanDialog(
            plan,self,draft_store=self.training_plan_drafts,
            draft=self._training_candidate_draft,task_pool=self.pool,
            can_fix=True,resource_registry=ModelResourceRegistry(MODEL_RESOURCES_PATH),jobs=self.state.jobs,
        )
        session=TrainingCandidateReviewSession(self,dialog)
        window=self.window() if callable(getattr(self,"window",None)) else None
        retain=getattr(window,"retain_training_review",None)
        package_destination=[None]
        def request_package():
            if self._shutdown_requested:
                return
            self._training_candidate_plan=dialog.plan
            destination=self._choose_training_package_destination(dialog.plan,review=dialog)
            if destination is not None:
                package_destination[0]=destination
                dialog.action="package"
                dialog.accept()
        dialog.package_requested.connect(request_package)
        def remember_package():
            if package_destination[0] is not None and getattr(window,"training_page",None) is not None:
                from .training_workspace import remember_training_package_preview
                remember_training_package_preview(window,dialog.plan,package_destination[0])
        if callable(retain):
            def finished(_result):
                self._training_candidate_plan=None if dialog._refresh_required else dialog.plan
                self._training_candidate_draft=None
                if not self._shutdown_requested and dialog.action=="package":
                    remember_package()
                else:
                    self._notify_background_work_finished()
            if retain(dialog,finished):
                return
        dialog.exec()
        self._training_candidate_plan=None if dialog._refresh_required else dialog.plan
        self._training_candidate_draft=None
        if dialog.action=="package":
            remember_package()
            return
        self._notify_background_work_finished()

    def _choose_training_package_destination(self,candidate_plan,*,review=None):
        configuration=candidate_plan.configuration
        output_name=(
            configuration.output_name if configuration is not None else
            "training-output"
        )
        recovery_return=getattr(self,"_training_package_recovery_return",None)
        if recovery_return is not None and candidate_plan.to_manifest_dict()!=recovery_return[2]:
            self._training_package_recovery_return=None
            self.header.set_status(
                "The rebuilt plan differs from the interrupted package. No recovery was started."
            )
            QMessageBox.information(
                self,"Interrupted package needs its exact plan",
                "The saved draft or current image evidence no longer rebuilds the exact staged "
                "plan. Nothing was published. The stage remains held. Review the saved plan "
                "and exact receipt again; to build different training data, start a separate "
                "package in a new folder.",
            )
            self._notify_background_work_finished()
            return
        dialog=TrainingPackageDestinationDialog(
            output_name,review if review is not None else self,
            initial_parent=recovery_return[1].parent if recovery_return else None,
        )
        if recovery_return is not None:
            dialog.package_name.setText(recovery_return[1].name)
        if dialog.exec()!=QDialog.Accepted or dialog.action!="preview":
            message=("Destination selection cancelled. Your review choices are unchanged; choose Continue to Package to try again."
                     if review is not None else "Training package cancelled before preview.")
            self.header.set_status(message)
            if review is not None:
                review.show_package_notice(message)
            self._notify_background_work_finished()
            return
        if self._start_training_package_preview(candidate_plan,dialog.destination):
            return dialog.destination
        if review is not None:
            review.show_package_notice(getattr(self,"_training_package_start_error",None) or "Package preview could not start. Your review choices are unchanged; try Continue to Package again when the blocker is resolved.")

    def _training_package_write_decision(self,candidate_plan,destination):
        policy=ExternalWritePolicy.from_settings(self.settings)
        registry=self.settings.library_registry()
        included=(
            candidate for candidate in candidate_plan.candidates
            if candidate.decision=="include"
        )
        boundary=policy.boundary_for_pairs(
            registry,
            ((candidate.image,destination) for candidate in included),
            backup_enabled=False,artifact_label="training package",
            workflow_label="Training package export",
        )
        return ExternalWriteDecision(
            policy.fingerprint(),False,None,boundary,
        ),tuple(
            Path(profile.root).expanduser().absolute()
            for profile in registry.profiles if profile.root
        )

    def _start_training_package_preview(self,candidate_plan,destination):
        self._training_package_start_error=None
        if self._shutdown_requested:
            self._training_package_start_error="CollectiGen is closing. No package preview started; review choices are unchanged."
            return False
        started,active=self.state.begin_long_operation(
            "dataset",
            f"Previewing a training package for {candidate_plan.count('include'):,} image(s)",
        )
        if not started:
            self._training_package_start_error="Finish the active operation, then try Continue to Package again. Your review choices are unchanged."
            show_operation_in_progress(self,active)
            self._notify_background_work_finished()
            return False
        try:
            service=TrainingPackageService(
                self.state.knowledge,self.characters,self.categories,
            )
            decision,forbidden_roots=self._training_package_write_decision(
                candidate_plan,destination,
            )
            def preview(progress,cancelled):
                return service.preview(
                    candidate_plan,destination,decision=decision,
                    forbidden_roots=forbidden_roots,progress=progress,
                    should_cancel=cancelled,
                )
            task=CancelableProgressCallTask(preview)
        except Exception as exc:
            self.state.finish_long_operation("dataset")
            self._training_package_start_error=f"Package preview could not start: {exc}. Review choices are unchanged."
            self.header.set_status(self._training_package_start_error)
            self._notify_background_work_finished()
            return False
        self._training_package_service=service
        self._training_package_stage="preview"
        self.header.set_status("Rechecking sources and previewing exact package artifacts…")

        self._training_package_task=task
        window=self.window() if callable(getattr(self,"window",None)) else None
        if getattr(window,"training_page",None) is not None and window.training_page._review is None:
            from .training_workspace import remember_training_package_preview
            remember_training_package_preview(window,candidate_plan,destination)
        DatasetPage._sync_training_workspace(self)
        self.cancel_projection_button.setText("Cancel Safely")
        self.cancel_projection_button.setEnabled(True)
        self.cancel_projection_button.show()
        self.update_selection_controls()
        task.signals.progress.connect(self._training_package_progress)
        task.signals.complete.connect(self._training_package_preview_complete)
        task.signals.cancelled.connect(self._training_package_cancelled)
        task.signals.failed.connect(self._training_package_failed)
        try:
            start_qt_delivery_task(self.pool,task)
        except Exception as exc:
            task.cancel()
            self._finish_training_package_task()
            self._training_package_start_error=f"Package preview could not start: {exc}. Review choices are unchanged; retry when background capacity is available."
            self.header.set_status(self._training_package_start_error)
            self._notify_background_work_finished()
            return False
        return True

    def _training_package_progress(self,index,total,name):
        self.state.update_long_operation("dataset",index,total,name)
        verb=(
            "Previewing training package"
            if self._training_package_stage=="preview" else
            "Building and verifying training package"
        )
        self.header.set_status(f"{verb}: {index:,} of {total:,} • {name}")
        DatasetPage._sync_training_workspace(self)

    def _finish_training_package_task(self):
        self.state.finish_long_operation("dataset")
        self._training_package_task=None
        self.cancel_projection_button.hide()
        self.update_selection_controls()

    def _training_package_preview_complete(self,plan):
        task=getattr(self,"_training_package_task",None)
        if task is not None and getattr(task,"is_cancelled",lambda:False)():
            DatasetPage._training_package_cancelled(self)
            return
        self._finish_training_package_task()
        if plan.cancelled:
            self._training_package_plan=None
            self.header.set_status(
                "Training package preview cancelled safely. No files were changed."
            )
            self._notify_background_work_finished()
            return
        self._training_package_plan=plan
        if self._shutdown_requested:
            self._notify_background_work_finished()
            return
        service=self._training_package_service
        try:
            receipts=service.recovery_receipts(
                plan.destination.parent,destination_name=plan.destination.name,
            )
        except Exception as exc:
            QMessageBox.warning(
                self,"Training package recovery unavailable",
                "CollectiGen could not safely inspect recovery records for this "
                f"destination. Nothing was written.\n\n{exc}",
            )
            self._notify_background_work_finished()
            return
        recovery=None
        recovery_return=getattr(self,"_training_package_recovery_return",None)
        if recovery_return is not None:
            receipt,destination,manifest=recovery_return
            if plan.destination!=destination or plan.candidate_manifest!=manifest:
                self._training_package_recovery_return=None
                QMessageBox.information(
                    self,"Interrupted package does not match",
                    "The destination or reviewed plan changed. The staged package was not "
                    "published. Find a related saved draft again, or choose a new package destination.",
                )
                self._notify_background_work_finished()
                return
            if receipt not in receipts:
                self._training_package_recovery_return=None
                QMessageBox.information(
                    self,"Interrupted package changed",
                    "The selected recovery receipt is no longer active at this destination. "
                    "Nothing was published. Review the interrupted package again.",
                )
                self._notify_background_work_finished()
                return
            recovery=receipt
        elif receipts:
            QMessageBox.information(
                self,"Review the interrupted package first",
                "An interrupted package already owns this destination. Nothing was written. "
                "In Training, choose Recover Interrupted Package, select this output "
                "location, and review the exact saved package before continuing.",
            )
            self._notify_background_work_finished()
            return
        dialog=TrainingPackagePlanDialog(
            plan,self,recovery_receipt=recovery,
        )
        dialog.exec()
        if dialog.action=="recover" and recovery is not None:
            DatasetPage._clear_training_package_retry(self)
            self._training_package_recovery_return=None
            self._start_training_package_recovery(recovery,expected_phase="staged_complete")
            return
        if dialog.action!="build":
            DatasetPage._clear_training_package_retry(self)
            self.header.set_status("Training package cancelled before writing.")
            self._notify_background_work_finished()
            return
        current_decision,_=self._training_package_write_decision(
            plan.candidate_plan,plan.destination,
        )
        if current_decision.fingerprint()!=plan.decision.fingerprint():
            QMessageBox.information(
                self,"Training package protection changed",
                "The shared external-write policy or declared storage boundary "
                "changed after preview. Nothing was written; preview again.",
            )
            self._notify_background_work_finished()
            return
        execution=plan.decision.execution(
            current_policy_fingerprint=current_decision.policy_fingerprint,
            privacy_confirmed=dialog.privacy_confirmed,
        )
        DatasetPage._clear_training_package_retry(self)
        self._start_training_package_apply(plan,execution)

    def _start_training_package_apply(self,plan,execution):
        started,active=self.state.begin_long_operation(
            "dataset",
            f"Building and verifying {len(plan.items):,} training package image(s)",
        )
        if not started:
            show_operation_in_progress(self,active)
            self._notify_background_work_finished()
            return
        self._training_package_stage="apply"
        self.header.set_status(
            "Building in a private staging folder; source files remain unchanged…"
        )
        task=CancelableProgressCallTask(
            lambda progress,cancelled:self._training_package_service.apply(
                plan,execution,progress=progress,should_cancel=cancelled,
            )
        )
        self._training_package_task=task
        DatasetPage._sync_training_workspace(self)
        self.cancel_projection_button.setText("Cancel Safely")
        self.cancel_projection_button.setEnabled(True)
        self.cancel_projection_button.show()
        self.update_selection_controls()
        task.signals.progress.connect(self._training_package_progress)
        task.signals.complete.connect(self._training_package_result_complete)
        task.signals.cancelled.connect(self._training_package_cancelled)
        task.signals.failed.connect(self._training_package_failed)
        start_qt_delivery_task(self.pool,task)

    def _start_training_package_recovery(self,receipt,*,expected_phase=None):
        started,active=self.state.begin_long_operation(
            "dataset","Recovering and verifying an interrupted training package",
        )
        if not started:
            show_operation_in_progress(self,active)
            self._notify_background_work_finished()
            return
        self._training_package_stage="recover"
        self.header.set_status("Checking the exact training package recovery receipt…")
        candidate_plan=(
            self._training_package_plan.candidate_plan
            if self._training_package_plan is not None else None
        )
        registry=self.settings.library_registry()
        forbidden_roots=tuple(
            Path(profile.root).expanduser().absolute()
            for profile in registry.profiles if profile.root
        )
        task=ProgressCallTask(
            lambda _progress:self._training_package_service.recover(
                receipt,candidate_plan=candidate_plan,
                forbidden_roots=forbidden_roots,
                expected_phase=expected_phase,
            )
        )
        self._training_package_task=task
        DatasetPage._sync_training_workspace(self)
        task.signals.complete.connect(self._training_package_result_complete)
        task.signals.failed.connect(self._training_package_failed)
        start_qt_delivery_task(self.pool,task)

    def _start_training_package_verify(self,destination):
        started,active=self.state.begin_long_operation(
            "dataset","Verifying every training package artifact",
        )
        if not started:
            show_operation_in_progress(self,active)
            self._notify_background_work_finished()
            return
        self._training_package_stage="verify"
        if self._training_package_service is None:
            self._training_package_service=TrainingPackageService()
        self.header.set_status("Verifying manifest, hashes, images, captions, and splits…")

        def verify(_progress,cancelled):
            verification=self._training_package_service.verify(destination,should_cancel=cancelled)
            if cancelled():
                raise InterruptedError("Training package verification cancelled.")
            return TrainingPackageResult(
                "current" if verification.valid else "failed",
                verification.destination,verification.artifact_count,
                verification.destination/"collectigen-training-manifest.json",
                verification=verification,issues=verification.issues,
            )

        task=CancelableProgressCallTask(verify)
        self._training_package_task=task
        DatasetPage._sync_training_workspace(self)
        task.signals.complete.connect(self._training_package_result_complete)
        task.signals.cancelled.connect(self._training_package_cancelled)
        task.signals.failed.connect(self._training_package_failed)
        start_qt_delivery_task(self.pool,task)

    def _start_training_package_recovery_preview(self,receipt):
        started,active=self.state.begin_long_operation(
            "dataset","Reviewing an interrupted training package",
        )
        if not started:
            show_operation_in_progress(self,active)
            self._notify_background_work_finished()
            return
        self._training_package_stage="recovery_preview"
        if self._training_package_service is None:
            self._training_package_service=TrainingPackageService()
        self.header.set_status("Reading the selected recovery receipt; no files are being changed…")
        def preview(_progress,cancelled):
            result=self._training_package_service.preview_recovery(receipt,should_cancel=cancelled)
            if cancelled():
                raise InterruptedError("Training package recovery review cancelled.")
            return result
        task=CancelableProgressCallTask(preview)
        self._training_package_task=task
        DatasetPage._sync_training_workspace(self)
        task.signals.complete.connect(self._training_package_result_complete)
        task.signals.cancelled.connect(self._training_package_cancelled)
        task.signals.failed.connect(self._training_package_failed)
        start_qt_delivery_task(self.pool,task)

    def _start_training_package_compare(self,first):
        second=QFileDialog.getExistingDirectory(
            self,"Choose a Training Package to Compare",str(Path(first).parent),
        )
        if not second:
            self._notify_background_work_finished()
            return
        started,active=self.state.begin_long_operation(
            "dataset","Comparing two verified training packages",
        )
        if not started:
            show_operation_in_progress(self,active)
            self._notify_background_work_finished()
            return
        self._training_package_stage="compare"
        self.header.set_status("Verifying and comparing package fingerprints…")
        task=CancelableProgressCallTask(
            lambda _progress,cancelled:self._training_package_service.compare(first,second,should_cancel=cancelled)
        )
        self._training_package_task=task
        DatasetPage._sync_training_workspace(self)
        task.signals.complete.connect(self._training_package_comparison_complete)
        task.signals.cancelled.connect(self._training_package_cancelled)
        task.signals.failed.connect(self._training_package_failed)
        start_qt_delivery_task(self.pool,task)

    def _training_package_result_complete(self,result):
        task=getattr(self,"_training_package_task",None)
        if (getattr(self,"_training_package_stage","") in ("verify","recovery_preview")
                and task is not None and getattr(task,"is_cancelled",lambda:False)()):
            DatasetPage._training_package_cancelled(self)
            return
        self._finish_training_package_task()
        if self._shutdown_requested:
            self._notify_background_work_finished()
            return
        verification=result.verification
        self.header.set_status(
            f"Training package {result.status.replace('_',' ')}"
            +(f" • {verification.artifact_count:,} artifacts verified"
              if verification and verification.valid else "")
        )
        dialog=TrainingPackageResultDialog(
            result,self,candidate_plan_available=getattr(self,"_training_package_plan",None) is not None,
        )
        dialog.exec()
        if dialog.action=="saved_drafts" and result.status=="rolled_back":
            self._training_package_recovery_return=None
            self._notify_background_work_finished()
            window=self.window() if callable(getattr(self,"window",None)) else None
            data_recovery=getattr(window,"open_dataset_package_recovery",None)
            if callable(data_recovery):
                data_recovery()
                return
            if window is not None and callable(getattr(window,"open_training_workspace",None)) and window.open_training_workspace():
                window.training_page.show_package_recovery_drafts(None)
                window.training_page.status.setText("Incomplete staging removed. Choose a saved draft and preview a fresh package; nothing starts automatically.")
            return
        if dialog.action=="recovery_preview" and result.recovery_receipt is not None:
            self._start_training_package_recovery_preview(result.recovery_receipt)
            return
        if dialog.action=="find_draft" and result.recovery_receipt is not None:
            verification=result.verification
            manifest=(verification.manifest.get("candidate_plan")
                      if verification is not None and verification.valid else None)
            window=self.window() if callable(getattr(self,"window",None)) else None
            if not isinstance(manifest,dict) or window is None or not (
                callable(getattr(window,"open_dataset_package_recovery",None))
                or callable(getattr(window,"open_training_workspace",None))
            ):
                self.header.set_status(
                    "The saved plan could not be matched. Reopen the exact recovery receipt to review it again."
                )
                self._notify_background_work_finished()
                return
            self._training_package_recovery_return=(
                result.recovery_receipt,result.destination,copy.deepcopy(manifest),
            )
            self._notify_background_work_finished()
            data_recovery=getattr(window,"open_dataset_package_recovery",None)
            if callable(data_recovery):
                data_recovery()
                return
            if not window.open_training_workspace():
                self._training_package_recovery_return=None
                self.header.set_status(
                    "Finish the current Training review, then reopen this recovery receipt."
                )
                return
            window.training_page.show_package_recovery_drafts(manifest)
            return
        if dialog.action=="trainer_preview" and result.verification is not None and result.verification.valid:
            family=result.verification.manifest.get("training_configuration",{}).get("target",{}).get("model_family","")
            if str(family).casefold()=="anima":
                from .training_workflow_controller import shared_training_workflow
                workflow=shared_training_workflow(self.state,self.window())
                workflow.prepare(result.verification,ModelResourceRegistry(MODEL_RESOURCES_PATH),None)
                self._notify_background_work_finished()
                return
            from .training_adapter_preview_dialog import TrainingAdapterPreviewDialog
            preview=TrainingAdapterPreviewDialog(
                result.verification,ModelResourceRegistry(MODEL_RESOURCES_PATH),self.state.jobs,self,
            )
            preview.exec()
            if preview.action=="local_training":
                workflow=getattr(self,"training_execution_workflow",None)
                if workflow is None:
                    from .training_workflow_controller import shared_training_workflow
                    workflow=shared_training_workflow(self.state,self.window())
                    self.training_execution_workflow=workflow
                    workflow.backgroundWorkFinished.connect(self._notify_background_work_finished)
                    workflow.packageReviewRequested.connect(
                        lambda package:self._start_training_package_verify(package.destination))
                    workflow.resourceRegistered.connect(
                        lambda _identity:self.header.set_status("Trained LoRA verified and registered with its lineage"))
                workflow.prepare(result.verification,preview.registry,preview.selected_recipe)
            self._notify_background_work_finished()
            return
        if dialog.action=="recover" and result.recovery_receipt is not None:
            self._start_training_package_recovery(result.recovery_receipt,expected_phase=result.recovery_phase)
            return
        if dialog.action=="verify":
            self._start_training_package_verify(result.destination)
            return
        if dialog.action=="compare":
            self._start_training_package_compare(result.destination)
            return
        self._notify_background_work_finished()

    def _training_package_comparison_complete(self,comparison):
        task=getattr(self,"_training_package_task",None)
        if task is not None and getattr(task,"is_cancelled",lambda:False)():
            DatasetPage._training_package_cancelled(self)
            return
        self._finish_training_package_task()
        if not self._shutdown_requested:
            self.header.set_status(
                "Training packages match exactly"
                if comparison.identical else
                "Training package comparison found differences"
            )
            TrainingPackageComparisonDialog(comparison,self).exec()
        self._notify_background_work_finished()

    def _training_package_cancelled(self):
        stage=getattr(self,"_training_package_stage","")
        self._finish_training_package_task()
        if stage=="preview":
            self._training_package_plan=None
        self.header.set_status(
            "Training package preview cancelled safely. No files were changed."
            if stage=="preview" else
            "Training package work cancelled safely. Source files were unchanged."
        )
        self._notify_background_work_finished()

    def _training_package_failed(self,message):
        task=getattr(self,"_training_package_task",None)
        if (getattr(self,"_training_package_stage","") in
                ("preview","verify","recovery_preview","compare")
                and task is not None and getattr(task,"is_cancelled",lambda:False)()):
            DatasetPage._training_package_cancelled(self)
            return
        self._finish_training_package_task()
        self.header.set_status("Training package work stopped")
        if not self._shutdown_requested:
            QMessageBox.warning(
                self,"Training package unavailable",
                f"{message}\n\nSource images and Image Knowledge were not changed.",
            )
        self._notify_background_work_finished()

    def _training_candidate_cancelled(self):
        self._finish_training_candidate_task()
        self._training_candidate_plan=None
        self._training_candidate_draft=None
        self.header.set_status(
            "Training dataset planning cancelled safely. No files were changed."
        )
        self._notify_background_work_finished()

    def _training_candidate_failed(self,message):
        task=getattr(self,"_training_candidate_task",None)
        if task is not None and task.is_cancelled():
            DatasetPage._training_candidate_cancelled(self)
            return
        self._finish_training_candidate_task()
        self._training_candidate_plan=None
        self._training_candidate_draft=None
        self.header.set_status("Training dataset planning stopped")
        if not self._shutdown_requested:
            QMessageBox.warning(
                self,"Training dataset plan unavailable",
                f"{message}\n\nNo Image Knowledge, captions, Metadata, images, or "
                "training package files were changed.",
            )
        self._notify_background_work_finished()

    def _training_export_destination_is_separate(self,paths,target) -> bool:
        target_path=Path(target).resolve()
        roots=tuple(dict.fromkeys(
            (self._library_root_for(path) or Path(path).resolve().parent).resolve()
            for path in paths
        ))
        return not any(
            target_path==root
            or target_path.is_relative_to(root)
            or root.is_relative_to(target_path)
            for root in roots
        )

    def _export_training_captions(self,paths):
        paths=[Path(path) for path in paths if path]
        if not paths:
            QMessageBox.information(self,"Select images","Select one or more visible images to export first."); return
        if self._training_export_task is not None:
            show_operation_in_progress(self,"Training TXT export")
            return
        if not DatasetPage._storage_scope_ready(self,
            paths,write=False,action="export training TXT",
        ):
            return
        try:
            first_record=ImageRecord(paths[0])
        except Exception as exc:
            QMessageBox.warning(
                self,"Training TXT preview unavailable",
                f"CollectiGen could not prepare the first selected image for preview.\n\n{exc}",
            ); return
        dialog=_TrainingTXTExportDialog(
            first_record,self.characters,self.state.knowledge,self.settings.export_content_level,
            self,current_profile_id=self._training_export_profile_id,
            profile_store=self.export_profiles,
        )
        if dialog.exec()!=QDialog.Accepted: return
        profile=dialog.selected_profile()
        self._training_export_profile_id=profile.profile_id
        policy=ExternalWritePolicy.from_settings(self.settings)
        registry=self.settings.library_registry()
        destination_dialog=TrainingTXTExportDestinationDialog(
            policy,registry,self,
        )
        if destination_dialog.exec()!=QDialog.Accepted:
            return
        if destination_dialog.action=="configure":
            self.open_automation.emit(
                "automation.settings",
                {"origin":"dataset.training_txt_export"},
            )
            return
        destination_mode=destination_dialog.destination_mode
        backup_enabled=destination_dialog.backup_enabled
        first_root=self._library_root_for(paths[0])
        target_path=None
        if destination_mode=="separate":
            initial=str(first_root or Path.cwd())
            target=QFileDialog.getExistingDirectory(
                self,"Choose Training TXT export folder",initial,
            )
            if not target:
                return
            target_path=Path(target).resolve()
            if not DatasetPage._training_export_destination_is_separate(
                self,paths,target_path,
            ):
                QMessageBox.warning(
                    self,"Choose a separate export folder",
                    "The Training TXT export folder must not be the same as, "
                    "inside, or contain a selected image library. Choose "
                    "Adjacent TXT explicitly if that is the intended workflow.",
                )
                return
        service=DatasetCaptionExportService(
            self.state.knowledge,self.characters,self.categories,
        )
        identities={
            path:self.dataset.identity_for_path(path)
            for path in paths
            if self.dataset is not None and self.dataset.identity_for_path(path) is not None
        }
        started,active=self.state.begin_long_operation(
            "dataset",f"Previewing Training TXT export for {len(paths):,} image(s)",
        )
        if not started:
            show_operation_in_progress(self,active)
            return
        self._training_export_service=service
        self._training_export_stage="preview"
        self.header.set_status(
            f"Previewing exact Training TXT targets for {len(paths):,} image(s)…"
        )

        def prepare_preview(progress,cancelled):
            plan=service.preview(
                paths,target_path,dataset_root=first_root,profile=profile,
                image_identities=identities,
                destination_mode=destination_mode,
                backup_enabled=backup_enabled,
                backup_directory=policy.configured_backup_directory,
                external_policy_fingerprint=policy.fingerprint(),
                progress=progress,should_cancel=cancelled,
            )
            boundary=policy.boundary_for_pairs(
                registry,
                (
                    (item.image,item.target) for item in plan.items
                    if item.target is not None
                ),
                backup_enabled=backup_enabled,
            )
            plan.privacy_confirmation_required=(
                boundary.confirmation_required
            )
            plan.privacy_message=boundary.message
            return plan

        task=CancelableProgressCallTask(
            prepare_preview
        )
        self._training_export_task=task
        self.cancel_projection_button.setText("Cancel Safely")
        self.cancel_projection_button.setEnabled(True)
        self.cancel_projection_button.show()
        self.update_selection_controls()
        task.signals.progress.connect(self._training_export_progress)
        task.signals.complete.connect(self._training_export_preview_complete)
        task.signals.cancelled.connect(self._training_export_cancelled)
        task.signals.failed.connect(self._training_export_failed)
        self.pool.start(task)

    def _training_export_progress(self,index,total,name):
        self.state.update_long_operation("dataset",index,total,name)
        verb=(
            "Previewing Training TXT" if self._training_export_stage=="preview"
            else "Exporting Training TXT" if self._training_export_stage=="apply"
            else "Restoring Training TXT"
        )
        self.header.set_status(
            f"{verb}: {index:,} of {total:,} • {name}"
        )

    def _finish_training_export_task(self):
        self.state.finish_long_operation("dataset")
        self._training_export_task=None
        self.cancel_projection_button.hide()
        self.update_selection_controls()

    def _training_export_preview_complete(self,plan):
        self._finish_training_export_task()
        if plan.cancelled:
            self.header.set_status(
                "Training TXT preview cancelled safely. No output was changed."
            )
            self._training_export_plan=None
            self._notify_background_work_finished()
            return
        if self._shutdown_requested:
            self._notify_background_work_finished()
            return
        self._training_export_plan=plan
        service=self._training_export_service
        try:
            interrupted=service.latest_recovery_for_plan(
                plan,statuses=("applying",),
            )
            restorable=service.latest_recovery_for_plan(
                plan,
                statuses=("applying","complete","cancelled","failed"),
            )
        except Exception as exc:
            QMessageBox.warning(
                self,"Training TXT recovery unavailable",
                "CollectiGen could not safely inspect the recovery records for "
                f"this exact export plan. Nothing was written.\n\n{exc}",
            )
            self._notify_background_work_finished()
            return
        dialog=TrainingTXTExportPlanDialog(
            plan,self,restore_available=restorable is not None,
            recovery_required=interrupted is not None,
        )
        if dialog.exec()!=QDialog.Accepted:
            self.header.set_status(
                "Training TXT export cancelled before writing."
            )
            self._notify_background_work_finished()
            return
        if dialog.action=="restore":
            self._start_training_export_restore(plan)
            return
        try:
            current=self.export_profiles.resolve(plan.profile.profile_id)
            current=current.with_maximum_content_rating(
                self.settings.export_content_level
            )
        except Exception:
            current=None
        if current is None or current.fingerprint()!=plan.profile.fingerprint():
            QMessageBox.information(
                self,"Training profile changed",
                "The selected Training TXT profile changed after preview. "
                "Nothing was written; preview the exact scope again.",
            )
            self._notify_background_work_finished()
            return
        current_policy=ExternalWritePolicy.from_settings(self.settings)
        if (
            plan.external_policy_fingerprint
            and current_policy.fingerprint()!=plan.external_policy_fingerprint
        ):
            QMessageBox.information(
                self,"External-write protection changed",
                "The shared external-write backup policy changed after preview. "
                "Nothing was written; preview the exact scope again.",
            )
            self._notify_background_work_finished()
            return
        policy_issues=(
            current_policy.backup_destination_issues(
                self.settings.library_registry(),enabled=plan.backup_enabled,
            )
            if plan.external_policy_fingerprint else ()
        )
        if policy_issues:
            QMessageBox.warning(
                self,"External-write protection unavailable",
                " ".join(policy_issues)+" Nothing was written.",
            )
            self._notify_background_work_finished()
            return
        privacy_confirmation=getattr(dialog,"privacy_confirmation",None)
        privacy_confirmed=(
            privacy_confirmation is not None
            and privacy_confirmation.isChecked()
        )
        self._start_training_export_apply(
            plan,privacy_confirmed=privacy_confirmed,
            external_policy_fingerprint=current_policy.fingerprint(),
        )

    def _start_training_export_apply(
        self,plan,*,privacy_confirmed=False,
        external_policy_fingerprint="",
    ):
        started,active=self.state.begin_long_operation(
            "dataset",
            f"Exporting {plan.write_count:,} changed Training TXT file(s)",
        )
        if not started:
            show_operation_in_progress(self,active)
            self._notify_background_work_finished()
            return
        self._training_export_stage="apply"
        task=CancelableProgressCallTask(
            lambda progress,cancelled:self._training_export_service.apply(
                plan,progress=progress,should_cancel=cancelled,
                privacy_confirmed=privacy_confirmed,
                external_policy_fingerprint=external_policy_fingerprint,
            )
        )
        self._training_export_task=task
        self.cancel_projection_button.setText("Cancel Safely")
        self.cancel_projection_button.setEnabled(True)
        self.cancel_projection_button.show()
        self.update_selection_controls()
        task.signals.progress.connect(self._training_export_progress)
        task.signals.complete.connect(self._training_export_apply_complete)
        task.signals.cancelled.connect(self._training_export_cancelled)
        task.signals.failed.connect(self._training_export_failed)
        self.pool.start(task)

    def _start_training_export_restore(self,plan):
        started,active=self.state.begin_long_operation(
            "dataset","Restoring the last Training TXT export",
        )
        if not started:
            show_operation_in_progress(self,active)
            self._notify_background_work_finished()
            return
        self._training_export_stage="restore"
        task=ProgressCallTask(
            lambda progress:self._training_export_service.restore_latest_for_plan(
                plan,progress=progress,
            )
        )
        self._training_export_task=task
        self.update_selection_controls()
        task.signals.progress.connect(self._training_export_progress)
        task.signals.complete.connect(self._training_export_restore_complete)
        task.signals.failed.connect(self._training_export_failed)
        self.pool.start(task)

    def _training_export_apply_complete(self,result):
        self._finish_training_export_task()
        outcomes=(("Created",result.created),("Replaced",result.replaced),
                  ("Already current",result.current),("Excluded",result.excluded_by_rating),
                  ("Empty",result.empty),("Held stale",result.stale),
                  ("Collisions",result.collisions),("Failed",result.failed),
                  ("Not processed",result.not_processed))
        details=[f"{name} {len(values):,}" for name,values in outcomes if values]
        message="Training TXT export • "+" • ".join(details or ["No TXT changes"])
        if result.cancelled:
            message="Cancelled safely after verified files • "+message
        DatasetPage._set_training_export_status(
            self,message,severity="warning" if result.failed or result.stale or result.collisions else "info",
        )
        if (
            result.failed or result.stale or result.collisions
        ) and not self._shutdown_requested:
            plan=self._training_export_plan
            recovery_guidance=(
                "Use this exact destination choice again to inspect or restore "
                "the last protected operation. "
                if plan is not None and plan.backup_enabled else
                "Backup protection was Off, so no automatic restore was created. "
            )
            txt_guidance=(
                "Only the exact adjacent TXT targets in the accepted plan could "
                "change."
                if plan is not None and plan.destination_mode=="adjacent" else
                "Adjacent source TXT were unchanged."
            )
            QMessageBox.warning(
                self,"Training TXT export needs attention",
                message
                +"\n\nVerified completed files were kept. "
                +recovery_guidance
                +"Image Knowledge, source images, and Metadata were unchanged. "
                +txt_guidance,
            )
        self._notify_background_work_finished()

    def _training_export_restore_complete(self,result):
        self._finish_training_export_task()
        message=(
            f"Training TXT restore • Restored {len(result.restored):,} • "
            f"Already restored {len(result.already_restored):,} • "
            f"Held newer output {len(result.held):,}"
        )
        DatasetPage._set_training_export_status(self,message,severity="warning" if result.held else "info")
        if result.held and not self._shutdown_requested:
            QMessageBox.warning(
                self,"Newer Training TXT was not overwritten",
                message+"\n\nRestore stopped before changing any held newer output.",
            )
        self._notify_background_work_finished()

    def _training_export_cancelled(self):
        stage=self._training_export_stage
        self._finish_training_export_task()
        DatasetPage._set_training_export_status(self,
            f"Training TXT {stage} cancelled safely."
        )
        self._notify_background_work_finished()

    def _training_export_failed(self,message):
        stage=self._training_export_stage
        self._finish_training_export_task()
        DatasetPage._set_training_export_status(self,f"Training TXT {stage} stopped",severity="warning")
        if not self._shutdown_requested:
            QMessageBox.warning(
                self,"Training TXT work stopped",
                f"{message}\n\nUse the same export folder to inspect any "
                "durable recovery record. Image Knowledge, source images, "
                "Metadata, and adjacent source TXT were unchanged.",
            )
        self._notify_background_work_finished()

    def _set_training_export_status(self,message,*,severity="info"):
        self.header.set_status(message)
        projection=getattr(self,"training_caption_projection",None)
        if projection is not None and projection.isVisible():
            projection.show_export_outcome(message,severity=severity)

    def _training_export_profile(self):
        return self.export_profiles.resolve(
            self._training_export_profile_id
        ).with_maximum_content_rating(self.settings.export_content_level)

    def _backup_tags_to_metadata(self,paths,*,downstream_context=None):
        paths=[Path(path) for path in paths if path]
        if not paths:
            QMessageBox.information(
                self,"Select images",
                "Select one or more images to back up to managed Metadata first.",
            )
            return
        if (
            self._metadata_backup_task is not None
            or self._projection_task is not None
            or self._training_export_task is not None
        ):
            show_operation_in_progress(self,"Metadata backup")
            return
        if not DatasetPage._storage_scope_ready(
            self,paths,write=True,action="back up Tags to Metadata",
        ):
            return
        policy=ExternalWritePolicy.from_settings(self.settings)
        registry=self.settings.library_registry()
        protection=MetadataBackupProtectionDialog(policy,registry,self)
        if protection.exec()!=QDialog.Accepted:
            return
        if protection.action=="configure":
            self.open_automation.emit(
                "automation.settings",{"origin":"dataset.metadata_backup"},
            )
            return
        backup_enabled=protection.backup_enabled
        reconciliation=ImageKnowledgeReconciliationService(
            MetadataSyncService(self.settings.exiftool_path or None),
            knowledge=self.state.knowledge,characters=self.characters,
        )
        service=MetadataBackupService(
            reconciliation,
            journal_path=USER_DATA_DIR/"metadata-backup-operations.jsonl",
            history_operation_factory=lambda images:capture_operation(
                self,images,operation_kind="managed_metadata_backup",
                reason="Back up or restore managed image Metadata"),
        )
        started,active=self.state.begin_long_operation(
            "dataset",f"Previewing managed Metadata for {len(paths):,} image(s)",
        )
        if not started:
            show_operation_in_progress(self,active)
            return
        self._metadata_backup_service=service
        self._metadata_backup_stage="preview"
        self._metadata_backup_paths=list(paths)
        self._metadata_backup_downstream_context=(
            dict(downstream_context) if isinstance(downstream_context,dict)
            else None
        )
        self.header.set_status(
            f"Previewing exact managed Metadata targets for {len(paths):,} image(s)…"
        )

        def prepare_preview(progress,cancelled):
            plan=service.preview(
                paths,backup_enabled=backup_enabled,
                backup_directory=policy.configured_backup_directory,
                external_policy_fingerprint=policy.fingerprint(),
                progress=progress,should_cancel=cancelled,
            )
            boundary=policy.boundary_for_pairs(
                registry,((item.image,item.image) for item in plan.items),
                backup_enabled=backup_enabled,
                artifact_label="managed Metadata",workflow_label="Metadata backup",
            )
            plan.privacy_confirmation_required=boundary.confirmation_required
            plan.privacy_message=boundary.message
            return plan

        task=CancelableProgressCallTask(prepare_preview)
        self._metadata_backup_task=task
        self.cancel_projection_button.setText("Cancel Safely")
        self.cancel_projection_button.setEnabled(True)
        self.cancel_projection_button.show(); self.update_selection_controls()
        task.signals.progress.connect(self._metadata_backup_progress)
        task.signals.complete.connect(self._metadata_backup_preview_complete)
        task.signals.cancelled.connect(self._metadata_backup_cancelled)
        task.signals.failed.connect(self._metadata_backup_failed)
        self.pool.start(task)

    def _metadata_backup_progress(self,index,total,name):
        self.state.update_long_operation("dataset",index,total,name)
        verb=(
            "Previewing managed Metadata"
            if self._metadata_backup_stage=="preview" else
            "Backing up Tags to Metadata"
            if self._metadata_backup_stage=="apply" else
            "Restoring managed Metadata"
        )
        self.header.set_status(f"{verb}: {index:,} of {total:,} • {name}")

    def _finish_metadata_backup_task(self):
        self.state.finish_long_operation("dataset")
        self._metadata_backup_task=None
        self.cancel_projection_button.hide(); self.update_selection_controls()

    def _complete_downstream_projection(self,context) -> bool:
        if not isinstance(context,dict):return False
        try:
            before=self.downstream_updates.load_projection()
            after=self.downstream_updates.complete_projection_target(
                str(context.get("target") or ""),
                expected_updated_at=str(
                    context.get("notice_updated_at") or ""
                ),
            )
        except OSError:
            return False
        if after!=before:self.downstream_updates_changed.emit()
        return str(context.get("target") or "") not in after.pending_targets

    def _discard_metadata_downstream_context(self) -> None:
        self._metadata_backup_downstream_context=None

    def _metadata_backup_preview_complete(self,plan):
        self._finish_metadata_backup_task()
        if plan.cancelled:
            self.header.set_status(
                "Managed Metadata preview cancelled safely. No image was changed."
            )
            self._metadata_backup_plan=None
            self._discard_metadata_downstream_context()
            self._notify_background_work_finished()
            return
        if self._shutdown_requested:
            self._discard_metadata_downstream_context()
            self._notify_background_work_finished()
            return
        self._metadata_backup_plan=plan
        service=self._metadata_backup_service
        try:
            interrupted=service.latest_recovery_for_plan(
                plan,statuses=("applying",),
            )
            restorable=service.latest_recovery_for_plan(
                plan,statuses=("applying","complete","cancelled","failed"),
            )
        except Exception as exc:
            QMessageBox.warning(
                self,"Metadata recovery unavailable",
                "CollectiGen could not safely inspect protected Metadata records "
                f"for this exact image scope. Nothing was written.\n\n{exc}",
            )
            self._discard_metadata_downstream_context()
            self._notify_background_work_finished()
            return
        if (
            getattr(self,"_metadata_backup_downstream_context",None)
            and not interrupted and plan.items
            and len(plan.items)==len(self._metadata_backup_paths)
            and all(item.disposition=="current" for item in plan.items)
        ):
            completed=self._complete_downstream_projection(
                self._metadata_backup_downstream_context,
            )
            self._discard_metadata_downstream_context()
            self.header.set_status(
                "Metadata is already current. This saved update is complete."
                if completed else
                "Metadata is current for this check; a newer saved update still needs review."
            )
            self._notify_background_work_finished()
            return
        dialog=MetadataBackupPlanDialog(
            plan,self,restore_available=restorable is not None,
            recovery_required=interrupted is not None,
        )
        if dialog.exec()!=QDialog.Accepted:
            self.header.set_status("Metadata backup cancelled before writing.")
            self._discard_metadata_downstream_context()
            self._notify_background_work_finished()
            return
        if dialog.action=="restore":
            self._discard_metadata_downstream_context()
            self._start_metadata_backup_restore(plan)
            return
        current_policy=ExternalWritePolicy.from_settings(self.settings)
        if current_policy.fingerprint()!=plan.external_policy_fingerprint:
            QMessageBox.information(
                self,"External-write protection changed",
                "The shared external-write backup policy changed after preview. "
                "Nothing was written; preview the exact scope again.",
            )
            self._discard_metadata_downstream_context()
            self._notify_background_work_finished()
            return
        issues=current_policy.backup_destination_issues(
            self.settings.library_registry(),enabled=plan.backup_enabled,
        )
        if issues:
            QMessageBox.warning(
                self,"External-write protection unavailable",
                " ".join(issues)+" Nothing was written.",
            )
            self._discard_metadata_downstream_context()
            self._notify_background_work_finished()
            return
        privacy_confirmed=(
            dialog.privacy_confirmation is not None
            and dialog.privacy_confirmation.isChecked()
        )
        self._start_metadata_backup_apply(
            plan,privacy_confirmed=privacy_confirmed,
            external_policy_fingerprint=current_policy.fingerprint(),
        )

    def _start_metadata_backup_apply(
        self,plan,*,privacy_confirmed=False,external_policy_fingerprint="",
    ):
        started,active=self.state.begin_long_operation(
            "dataset",f"Backing up Tags for {plan.write_count:,} image(s)",
        )
        if not started:
            show_operation_in_progress(self,active)
            self._discard_metadata_downstream_context()
            self._notify_background_work_finished()
            return
        self._metadata_backup_stage="apply"
        task=CancelableProgressCallTask(
            lambda progress,cancelled:self._metadata_backup_service.apply(
                plan,progress=progress,should_cancel=cancelled,
                privacy_confirmed=privacy_confirmed,
                external_policy_fingerprint=external_policy_fingerprint,
            )
        )
        self._metadata_backup_task=task
        self.cancel_projection_button.setText("Cancel Safely")
        self.cancel_projection_button.setEnabled(True)
        self.cancel_projection_button.show(); self.update_selection_controls()
        task.signals.progress.connect(self._metadata_backup_progress)
        task.signals.complete.connect(self._metadata_backup_apply_complete)
        task.signals.cancelled.connect(self._metadata_backup_cancelled)
        task.signals.failed.connect(self._metadata_backup_failed)
        self.pool.start(task)

    def _start_metadata_backup_restore(self,plan):
        started,active=self.state.begin_long_operation(
            "dataset","Restoring the last protected managed Metadata operation",
        )
        if not started:
            show_operation_in_progress(self,active)
            self._notify_background_work_finished()
            return
        self._metadata_backup_stage="restore"
        task=ProgressCallTask(
            lambda progress:self._metadata_backup_service.restore_latest_for_plan(
                plan,progress=progress,
            )
        )
        self._metadata_backup_task=task; self.update_selection_controls()
        task.signals.progress.connect(self._metadata_backup_progress)
        task.signals.complete.connect(self._metadata_backup_restore_complete)
        task.signals.failed.connect(self._metadata_backup_failed)
        self.pool.start(task)

    def _refresh_metadata_backup_paths(self):
        for path in self._metadata_backup_paths:
            self._readiness_cache.pop(path,None); self._invalidate_inspection_cache(path)
            if self.dataset and self.dataset.index and path.exists():
                try:self._cache_record(ImageRecord(path))
                except Exception:pass
        self.update_selection_controls(); self.render_page()
        if (
            self.current_path and self.current_path in self._metadata_backup_paths
            and self.current_path.exists()
        ):
            self.inspect_image(str(self.current_path))

    def _metadata_backup_apply_complete(self,result):
        self._finish_metadata_backup_task(); self._refresh_metadata_backup_paths()
        context=self._metadata_backup_downstream_context
        self._discard_metadata_downstream_context()
        incomplete=bool(
            result.cancelled or result.failed or result.recovered
            or result.count("unsupported") or result.count("blocked")
            or result.count("missing") or result.count("not_processed")
        )
        if not incomplete:self._complete_downstream_projection(context)
        message=(
            ("Cancelled safely after completed images • " if result.cancelled else "")
            +f"Metadata backup • Changed and verified {result.changed:,} • "
            f"Already current {result.current:,} • Unsupported {result.count('unsupported'):,} • "
            f"Blocked {result.count('blocked')+result.count('missing'):,} • "
            f"Failed {result.failed:,} • Recovered {result.recovered:,} • "
            f"Not processed {result.count('not_processed'):,}"
        )
        self.header.set_status(message)
        if not self._shutdown_requested:
            MetadataBackupResultDialog(result,self).exec()
        self._notify_background_work_finished()

    def _metadata_backup_restore_complete(self,result):
        self._discard_metadata_downstream_context()
        self._finish_metadata_backup_task(); self._refresh_metadata_backup_paths()
        message=(
            f"Metadata restore • Restored {len(result.restored):,} • "
            f"Already restored {len(result.already_restored):,} • "
            f"Held newer images {len(result.held):,}"
        )
        self.header.set_status(message)
        if result.held and not self._shutdown_requested:
            QMessageBox.warning(
                self,"Newer Metadata was not overwritten",
                message+"\n\n"+("Completed images were retained; remaining images need exact recovery."
                    if result.restored else "Restore stopped; current data and recovery evidence were retained.")
                +("\n\n"+"\n".join(result.errors) if getattr(result,"errors",None) else ""),
            )
        self._notify_background_work_finished()

    def _metadata_backup_cancelled(self):
        stage=self._metadata_backup_stage
        self._discard_metadata_downstream_context()
        self._finish_metadata_backup_task()
        self.header.set_status(f"Metadata backup {stage} cancelled safely.")
        self._notify_background_work_finished()

    def _metadata_backup_failed(self,message):
        stage=self._metadata_backup_stage
        self._discard_metadata_downstream_context()
        self._finish_metadata_backup_task()
        self.header.set_status(f"Metadata backup {stage} stopped")
        if not self._shutdown_requested:
            QMessageBox.warning(
                self,"Metadata backup stopped",
                f"{message}\n\nNo unverified Metadata result was reported as complete. "
                "Use Back Up Tags to Metadata again with the same scope to inspect recovery.",
            )
        self._notify_background_work_finished()

    def sync_selected_data(
        self,paths=None,preferred_targets=None,*,downstream_context=None,
    ):
        if paths is None or isinstance(paths,bool): paths=self.selected_paths()
        paths=[Path(path) for path in paths if path]
        preferred=tuple(preferred_targets or ("txt","metadata"))
        if preferred==("metadata",):
            if downstream_context is None:
                self._backup_tags_to_metadata(paths)
            else:
                self._backup_tags_to_metadata(
                    paths,downstream_context=downstream_context,
                )
            return
        if not paths:
            QMessageBox.information(self,"Select images","Select one or more images to synchronize first."); return
        if not DatasetPage._storage_scope_ready(self,
            paths,write=True,action="synchronize",
        ):
            return
        if self._projection_task is not None:
            QMessageBox.information(
                self,"Caption update in progress",
                "Wait for the current caption preview or update to finish, then "
                "check this saved step again. Its scope has been retained.",
            )
            return
        started,active=self.state.begin_long_operation("dataset",f"Previewing confirmed-data synchronization for {len(paths):,} image(s)")
        if not started: show_operation_in_progress(self,active); return
        self._projection_service=ImageKnowledgeReconciliationService(
            MetadataSyncService(self.settings.exiftool_path or None),
            knowledge=self.state.knowledge,characters=self.characters,
        )
        self._projection_paths=list(paths); self._projection_preferred_targets=tuple(preferred_targets or ("txt","metadata")); self._projection_downstream_context=(dict(downstream_context) if isinstance(downstream_context,dict) else None); self.header.set_status(f"Previewing confirmed-data synchronization for {len(paths):,} image(s)…")
        def work(progress,should_cancel):
            previews=[]; skipped=[]; errors=[]
            for index,path in enumerate(paths,1):
                if should_cancel(): return previews,skipped,errors,True
                try:
                    if not path.with_suffix(".tags.json").is_file(): skipped.append(path.name)
                    else:
                        preview=self._projection_service.preview(path,include_metadata=True)
                        previews.append(preview)
                        errors.extend(f"{path.name}: {message}" for message in preview.errors)
                except Exception as exc: errors.append(f"{path.name}: {exc}")
                progress(index,len(paths),path.name)
            return previews,skipped,errors,False
        task=CancelableProgressCallTask(work); self._projection_task=task; self.cancel_projection_button.setText("Cancel Safely"); self.cancel_projection_button.setEnabled(True); self.cancel_projection_button.show(); self.update_selection_controls()
        task.signals.progress.connect(lambda index,total,name:self.state.update_long_operation("dataset",index,total,name))
        task.signals.progress.connect(lambda index,total,name:self.header.set_status(f"Projection preview: {index:,} of {total:,} • {name}"))
        task.signals.complete.connect(self._projection_preview_complete); task.signals.failed.connect(self._projection_failed); self.pool.start(task)

    def _projection_preview_complete(self,result):
        self.state.finish_long_operation("dataset"); self._projection_task=None; self.cancel_projection_button.hide(); self.update_selection_controls()
        previews,skipped,errors,cancelled=result
        if getattr(self,"_shutdown_requested",False):
            self._projection_downstream_context=None
            self._notify_background_work_finished()
            return
        if cancelled:
            self._projection_downstream_context=None
            self.header.set_status("Confirmed-data preview cancelled safely before writing")
            self._notify_background_work_finished()
            return
        if not previews:
            self._projection_downstream_context=None
            self.header.set_status("No authoritative Image Knowledge records were available to synchronize")
            QMessageBox.information(self,"Nothing to synchronize","The selected images do not have usable authoritative Image Knowledge records yet.")
            self._notify_background_work_finished()
            return
        context=self._projection_downstream_context
        target="txt" if context and context.get("target")=="captions" else ""
        differences=[preview.difference(target) for preview in previews] if target else []
        if (
            differences and not skipped
            and len(previews)==len(self._projection_paths)
            and all(item is not None and item.available and not item.error
                    and not item.will_write for item in differences)
        ):
            completed=self._complete_downstream_projection(context)
            self._projection_downstream_context=None
            self.header.set_status(
                "Training captions are already current. This saved update is complete."
                if completed else
                "Training captions are current for this check; a newer saved update still needs review."
            )
            self._notify_background_work_finished()
            return
        dialog=_ProjectionBatchDialog(previews,skipped,errors,self,preferred_targets=self._projection_preferred_targets)
        if dialog.exec()!=QDialog.Accepted:
            self._projection_downstream_context=None
            self.header.set_status("Confirmed-data synchronization cancelled before writing")
            self._notify_background_work_finished()
            return
        targets=dialog.targets()
        if not targets:
            self._projection_downstream_context=None
            self.header.set_status("No projection target selected")
            self._notify_background_work_finished()
            return
        history_by_image={}
        try:
            for start in range(0,len(previews),64):
                images=tuple(item.image for item in previews[start:start+64])
                operation=capture_operation(self,images,operation_kind="image_projection_bookkeeping",
                    reason="Synchronize selected confirmed-data projections")
                history_by_image.update((image,operation) for image in images)
        except Exception as exc:
            self._history_admission_failed(exc)
            self._notify_background_work_finished()
            return
        started,active=self.state.begin_long_operation("dataset",f"Synchronizing confirmed data for {len(previews):,} image(s)")
        if not started:
            self._projection_downstream_context=None
            show_operation_in_progress(self,active)
            self._notify_background_work_finished()
            return
        self.header.set_status(f"Synchronizing confirmed data for {len(previews):,} image(s)…")
        def work(progress,should_cancel):
            changed=unchanged=unavailable=0; apply_errors=[]; cancelled=False
            for index,preview in enumerate(previews,1):
                if should_cancel(): cancelled=True; break
                selected=[]
                for target in targets:
                    difference=preview.difference(target)
                    if difference and difference.available and not difference.error: selected.append(target)
                    else: unavailable+=1
                if selected:
                    history=history_by_image[preview.image]
                    result=self._projection_service.apply(preview,targets=selected,
                        **({"history_operation":history} if history is not None else {}))
                    if result.errors: apply_errors.append(f"{preview.image.name}: "+"; ".join(result.errors))
                    elif result.changed_targets: changed+=1
                    else: unchanged+=1
                    if getattr(result,"history_interrupted",False) or getattr(result,"history_held",False):
                        apply_errors.append(f"Stopped for saved-history review; {len(previews)-index:,} later image(s) were not processed.")
                        break
                else: unchanged+=1
                progress(index,len(previews),preview.image.name)
            return {
                "changed":changed,"unchanged":unchanged,
                "unavailable":unavailable,"errors":apply_errors,
                "cancelled":cancelled,"skipped":len(skipped),
                "preview_errors":len(errors),"targets":tuple(targets),
                "expected":len(self._projection_paths),
            }
        task=CancelableProgressCallTask(work); self._projection_task=task; self.cancel_projection_button.setText("Cancel Safely"); self.cancel_projection_button.setEnabled(True); self.cancel_projection_button.show(); self.update_selection_controls()
        task.signals.progress.connect(lambda index,total,name:self.state.update_long_operation("dataset",index,total,name))
        task.signals.progress.connect(lambda index,total,name:self.header.set_status(f"Confirmed-data synchronization: {index:,} of {total:,} • {name}"))
        task.signals.complete.connect(self._projection_apply_complete); task.signals.failed.connect(self._projection_failed); self.pool.start(task)

    def _projection_apply_complete(self,result):
        self.state.finish_long_operation("dataset"); self._projection_task=None; self.cancel_projection_button.hide()
        context=self._projection_downstream_context
        self._projection_downstream_context=None
        for path in self._projection_paths:
            self._readiness_cache.pop(path,None); self._invalidate_inspection_cache(path)
            if self.dataset and self.dataset.index and path.exists():
                try: self._cache_record(ImageRecord(path))
                except Exception: pass
        self.update_selection_controls(); self.render_page()
        if self.current_path and self.current_path in self._projection_paths and self.current_path.exists(): self.inspect_image(str(self.current_path))
        errors=list(result.get("errors",[]))
        complete=bool(
            context
            and not result.get("cancelled") and not errors
            and not int(result.get("unavailable",0))
            and not int(result.get("skipped",0))
            and not int(result.get("preview_errors",0))
            and int(result.get("changed",0))+int(result.get("unchanged",0))
                ==int(result.get("expected",0))
        )
        if complete:self._complete_downstream_projection(context)
        message=(("Paused safely after the current image • " if result.get("cancelled") else "")+f"Synchronized and verified: {int(result.get('changed',0)):,} • Already current: {int(result.get('unchanged',0)):,} • Unavailable optional targets skipped: {int(result.get('unavailable',0)):,} • Failed: {len(errors):,}")
        self.header.set_status(message)
        if errors and not getattr(self,"_shutdown_requested",False):
            QMessageBox.warning(self,"Some confirmed data was not synchronized",message+"\n\n"+"\n".join(errors[:8]))
        self._notify_background_work_finished()

    def _projection_failed(self,message: str):
        self._projection_downstream_context=None
        self.state.finish_long_operation("dataset"); self._projection_task=None; self.cancel_projection_button.hide(); self.update_selection_controls(); self.header.set_status("Confirmed-data synchronization stopped before completion")
        if not getattr(self,"_shutdown_requested",False):
            QMessageBox.warning(self,"Confirmed data could not be synchronized",message)
        self._notify_background_work_finished()

    def cancel_data_task(self):
        if getattr(self,"_deferred_dataset_filter",None) is not None:
            DatasetPage._clear_deferred_dataset_filter(self)
            self.cancel_projection_button.hide()
            self.header.set_status("Deferred image-status check cancelled.")
        task=(
            getattr(self,"_preparation_task",None)
            or getattr(self,"_batch_task",None) or self._projection_task
            or getattr(self,"_metadata_backup_task",None)
            or getattr(self,"_training_candidate_task",None)
            or getattr(self,"_training_package_task",None)
            or getattr(self,"_training_export_task",None) or self._library_move_task
            or getattr(self,"_organization_transfer_task",None)
            or self._filter_state_task or getattr(self,"_dataset_search_task",None)
        )
        if task is None or not hasattr(task,"cancel"): return
        if task is getattr(self,"_dataset_search_task",None):
            task.cancel(); self._dataset_search_task=None
            self._dataset_search_accepted_spec=None
            self.header.set_status("Dataset search cancelled. Press Enter in Search to retry.")
            self._notify_background_work_finished()
            return
        self.state.request_long_operation_cancel("dataset")
        task.cancel(); self.cancel_projection_button.setText("Cancelling…"); self.cancel_projection_button.setEnabled(False)

    def has_active_background_work(self) -> bool:
        impact=getattr(self,"_preparation_impact_dialog",None)
        media=getattr(self,"_media_library_dialog",None)
        if media is not None and media.has_active_background_work():return True
        local_tasks=(
            getattr(impact,"_task",None),
            getattr(self,"_preparation_task",None),
            getattr(self,"_library_move_task",None),
            getattr(self,"_projection_task",None),
            getattr(self,"_metadata_backup_task",None),
            getattr(self,"_filter_state_task",None),
            getattr(self,"_dataset_search_task",None),
            getattr(self,"_folder_audit_task",None),
            getattr(self,"_training_candidate_task",None),
            getattr(self,"_training_package_task",None),
            getattr(self,"_training_export_task",None),
            getattr(self,"_organization_transfer_task",None),
            getattr(self,"_batch_task",None),
            getattr(self,"_conflict_task",None),
            getattr(getattr(self,"resolution_filter",None),"_task",None),
            getattr(self,"_automatic_resize_task",None),
        )
        matching=getattr(self,"image_identity_correction",None)
        training=getattr(self,"training_caption_projection",None)
        execution=getattr(self,"training_execution_workflow",None)
        concepts=getattr(self,"_concept_group_dialog",None)
        return (
            any(task is not None for task in local_tasks)
            or bool(getattr(self,"_history_refresh_tasks",()))
            or history_dialogs_busy(self)
            or bool(matching and matching.has_active_background_work())
            or bool(training and training.has_active_background_work())
            or bool(execution and execution.has_active_background_work())
            or bool(concepts and concepts.has_active_background_work())
        )

    def request_background_shutdown(self) -> bool:
        """Stop cancellable Dataset work and wait for every safe boundary."""
        self._shutdown_requested=True
        queue=getattr(self,"_preparation_request_queue",None)
        if queue is not None:queue.cancel_all()
        active=close_history_dialogs(self) or bool(getattr(self,"_history_refresh_tasks",()))
        resolution_filter=getattr(self,"resolution_filter",None)
        if resolution_filter is not None:
            resolution_filter.shutdown()
        automatic_resize=getattr(self,"_automatic_resize_task",None)
        if automatic_resize is not None:
            active=True
            automatic_resize.cancel()
            self._automatic_resize_task=None
        # Coordinated readers detach on cancellation and intentionally emit no
        # later Qt callback. Clear their owner now so shutdown cannot wait on a
        # completion signal that will never arrive; the coordinator owns drain.
        DatasetPage._invalidate_dataset_search(self)
        impact=getattr(self,"_preparation_impact_dialog",None)
        if impact is not None:
            active=active or impact._task is not None
            impact.reject()
        for child_name in (
            "image_identity_correction","training_caption_projection","training_execution_workflow","_concept_group_dialog","_media_library_dialog",
        ):
            child=getattr(self,child_name,None)
            if child is not None and child.has_active_background_work():
                child.request_background_shutdown()
                active=True
        for task in (
            getattr(self,"_preparation_task",None),
            getattr(self,"_library_move_task",None),
            getattr(self,"_projection_task",None),
            getattr(self,"_metadata_backup_task",None),
            getattr(self,"_filter_state_task",None),
            getattr(self,"_dataset_search_task",None),
            getattr(self,"_folder_audit_task",None),
            getattr(self,"_training_candidate_task",None),
            getattr(self,"_training_package_task",None),
            getattr(self,"_training_export_task",None),
            getattr(self,"_organization_transfer_task",None),
            getattr(self,"_batch_task",None),
        ):
            if task is None:
                continue
            active=True
            signals=getattr(task,"signals",None)
            if (
                signals is not None
                and hasattr(signals,"cancelled")
                and hasattr(task,"cancel")
            ):
                self.state.request_long_operation_cancel("dataset")
                task.cancel()
        conflict_task=getattr(self,"_conflict_task",None)
        if conflict_task is not None:
            active=True; conflict_task.cancel(); self._conflict_task=None
            QTimer.singleShot(0,self._notify_background_work_finished)
        elif not self.has_active_background_work():
            # Detached coordinated readers emit no later completion signal.
            # Publish their cleared ownership on the next GUI turn so the
            # shell's deferred Close can proceed through its existing drains.
            QTimer.singleShot(0,self._notify_background_work_finished)
        return active

    def _sync_training_workspace(self):
        window=self.window() if callable(getattr(self,"window",None)) else None
        if getattr(window,"training_page",None) is not None:
            from .training_workspace import sync_training_progress
            sync_training_progress(window)

    def _clear_training_package_retry(self):
        window=self.window() if callable(getattr(self,"window",None)) else None
        page=getattr(window,"training_page",None)
        if page is not None:
            page.clear_package_retry()

    def _notify_background_work_finished(self):
        DatasetPage._sync_training_workspace(self)
        if not self.has_active_background_work():
            self.background_work_finished.emit()

    def select_page(self):
        if not DatasetPage._dataset_search_results_ready(self):
            return
        for path in self._visible_batch_paths(): self.batch_selected.add(str(path))
        DatasetPage._dataset_search_selection_changed(self)
        self.render_page()

    def select_all_results(self):
        if not DatasetPage._dataset_search_results_ready(self):
            return
        available=DatasetPage._resolution_filtered_paths(self,self.filtered)
        if len(available)>max(12,int(getattr(self.settings,"gallery_page_size",24))) and QMessageBox.question(
            self,"Check all results",f"Check all {len(available):,} visible matching images across every page?"
        )!=QMessageBox.Yes: return
        self.batch_selected.update(str(path) for path in available)
        DatasetPage._dataset_search_selection_changed(self)
        self.render_page()

    def clear_selection(self):
        self.batch_selected.clear()
        DatasetPage._dataset_search_selection_changed(self)
        self.render_page()

    def _update_goal_scope_controls(self,count,busy):
        pending=getattr(self,"_dataset_search_task",None) is not None
        matching=len(self.filtered)
        self.training_goal_scope.setText(
            "Updating search results; review the scope when the search finishes."
            if pending else
            f"Selected images: {count:,} • Matching results: {matching:,}. Choose the exact scope in the training plan."
        )
        self.training_plan_button.setEnabled(self.plan_training_scope_action.isEnabled() and not pending)
        self.training_plan_button.setToolTip(self.training_goal_scope.text()+" Review captions and build a dataset before configuring training.")
        self.training_resume_button.setEnabled(True)
        self.concept_add_button.setEnabled(0<count<=5000 and self.dataset is not None and not busy and not pending and not self.batch_mode)
        self.training_rules_button.setEnabled(bool(matching) and self.dataset is not None and not busy and not pending and not self.batch_mode)
        self.generation_goal_scope.setText(
            "Updating search results; the selected-image scope is not ready."
            if pending else
            ("No images selected. Open Generate, or select images in the gallery."
             if not count else
             f"Use exactly {count:,} selected image(s) as planning references. "
             +( "Choose up to 100 images for one plan." if count>100 else
                "One image may supply recorded settings; a collection supplies selectable knowledge."))
        )
        self.generation_selection_button.setEnabled(0<count<=100 and self.dataset is not None and not pending and not busy and not self.batch_mode)
        self.generation_selection_button.setToolTip(self.generation_goal_scope.text()+" Selection supplies recorded settings and reviewed knowledge, not image-to-image pixels.")

    def open_training_section(self):
        window=self.window()
        open_training=getattr(window,"open_training_workspace",None)
        if callable(open_training):
            return open_training()
        return self.resume_training_dataset_draft()

    def plan_new_generation(self):
        window=self.window()
        open_generate=getattr(window,"open_generation_workspace",None)
        if callable(open_generate):
            return open_generate()
        from .generation_workflow import open_generation_workspace
        open_generation_workspace(self)

    def plan_generation_from_selection(self):
        if not DatasetPage._dataset_search_results_ready(self):
            return
        paths=tuple(self.selected_paths())
        if not paths:
            QMessageBox.information(self,"Choose generation references","Select images in the gallery, or use Open Generate without image references.")
            return
        from .generation_workflow import open_generation_workspace
        open_generation_workspace(self, paths=paths)

    def selected_paths(self):
        # Availability comes from the completed inventory. Avoid a fresh path
        # probe here because a disconnected or online-only library must not be
        # touched merely to update selection state.
        return [
            Path(path) for path in self.filtered if str(path) in self.batch_selected
        ]

    def _history_admission_failed(self,error,parent=None):
        QMessageBox.warning(parent or self,"Saved history needs attention",
            f"{error}\n\nReview saved-data history storage in Settings → Folder Locations, or narrow the selection to images with a usable configuration. Your unsaved choices were kept.")

    @contextmanager
    def _history_record(self,path,operation,record=None):
        """One immediate mutation only; never retain this lock across a dialog."""
        if operation is None:
            current=record if record is not None else ImageRecord(path)
            current.history_context=None
            yield current
            return
        context=operation.context_for(path)
        if context is None or context.operation_id!=operation.operation_id:
            raise HistoryError("This saved edit lacks its exact admitted history operation.")
        with context.locked(Path(path)):
            if context.store.has_pending(context.entity):
                raise HistoryWriteInterrupted("This image needs saved-history recovery. Open its image data editor before making another saved change.")
            current=record if record is not None else ImageRecord(path,history_context=context)
            current.history_context=context
            yield current

    def _remember_history_target(self,kind,paths,operation_id):
        target=(tuple(dict.fromkeys(Path(path) for path in paths)),str(operation_id)) if operation_id else None
        setattr(self,f"_{kind}_history_target",target)
        if kind=="edit":self.undo_button.setEnabled(target is not None)
        elif kind=="batch":self.undo_batch_button.setEnabled(target is not None)
        elif kind=="preparation":self.preparation_controls.set_undo_available(target is not None)

    def open_saved_history(self,*,paths=None,operation_id=None):
        if getattr(self,"_shutdown_requested",False) or any(getattr(self,name,None) is not None for name in (
            "_active_editor","_editor_load_task","_preparation_task","_batch_task","_projection_task","_metadata_backup_task",
            "_library_move_task","_organization_transfer_task",
        )) or bool(getattr(self,"_history_refresh_tasks",())):
            show_operation_in_progress(self,"Finish the current Dataset edit or preparation before opening saved history")
            return
        exact=tuple(dict.fromkeys(Path(path) for path in (paths if paths is not None else (
            self.selected_paths() or ((self.current_path,) if self.current_path is not None else ())))))
        if not exact:
            QMessageBox.information(self,"Select images","Select image cards or open one image before reviewing saved history.")
            return
        try:
            return open_history(self,exact,operation_target=operation_id,
                on_complete=self._history_restored,
                on_organization_outcomes=lambda outcomes:DatasetPage._organization_history_restored(self, None, exact, outcomes),
                recovery_handler=lambda path:self._queue_data_editor_load(Path(path)))
        except Exception as exc:
            self._history_admission_failed(exc)

    def _review_history_target(self,kind):
        target=getattr(self,f"_{kind}_history_target",None)
        if target is None:
            QMessageBox.information(self,"Saved change unavailable",
                "This action has no proven saved-history operation. Open History & Versions for explicit recorded versions. Older untracked changes cannot be restored from an in-memory snapshot.")
            return
        paths,operation_id=target
        return self.open_saved_history(paths=paths,operation_id=operation_id)

    def _history_restored(self,paths):
        exact=tuple(dict.fromkeys(Path(path) for path in paths))
        dataset=self.dataset
        task=AsyncCallTask(lambda:tuple(ImageRecord(path) for path in exact))
        self._history_refresh_tasks.append(task)
        def complete(records):
            if task in self._history_refresh_tasks:self._history_refresh_tasks.remove(task)
            if getattr(self,"_shutdown_requested",False):
                self._notify_background_work_finished(); return
            if self.dataset is dataset:
                for record in records:
                    self._invalidate_inspection_cache(record.image_path)
                    self._readiness_cache.pop(record.image_path,None)
                    if self.dataset and self.dataset.index:self._cache_record(record,publish_content_safety=False)
                self.render_page()
                current=next((record for record in records if record.image_path==self.current_path),None)
                if current is not None:self.inspect_image(str(current.image_path),current)
            events=getattr(self.state,"events",None)
            if events is not None:events.publish("image_data_changed",images=tuple(str(path) for path in exact),source="saved_history")
            self._notify_background_work_finished()
        def failed(message):
            if task in self._history_refresh_tasks:self._history_refresh_tasks.remove(task)
            if not getattr(self,"_shutdown_requested",False):
                self.header.set_status(f"Saved restoration completed; Dataset refresh needs attention: {message}")
            self._notify_background_work_finished()
        task.signals.complete.connect(complete)
        task.signals.failed.connect(failed)
        start_qt_delivery_task(self.pool,task)

    def open_external_evidence_import(self) -> None:
        if self.dataset is None or not tuple(getattr(self.dataset,"images",()) or ()):
            QMessageBox.information(
                self,"No Dataset images",
                "Open one or more image libraries before importing external evidence.",
            )
            return
        scoped=getattr(self.dataset,"scoped_profile_managers",None)
        named_scopes=tuple(
            (profile.stable_id,profile.name,tuple(getattr(manager,"images",()) or ()))
            for profile,manager in (scoped() if callable(scoped) else ())
        )
        def availability(path):
            status=self.dataset.storage_availability(path)
            reason=" ".join(
                value for value in (
                    str(getattr(status,"detail","") or ""),
                    str(getattr(status,"recovery","") or ""),
                ) if value
            )
            return bool(getattr(status,"ready_for_write",False)),reason
        matching_ready=DatasetPage._dataset_search_results_ready(self)
        dialog=ExternalEvidenceImportDialog(
            self.state,self,context_label="Dataset",
            all_paths=self.dataset.images,
            roots=(
                getattr(self.dataset,"roots",())
                or ((self.dataset.root,) if getattr(self.dataset,"root",None) else ())
            ),
            availability=availability,selected=self.selected_paths(),
            filtered=self.filtered if matching_ready else (),named_scopes=named_scopes,
            whole_scope_label="All included libraries",
            checkpoint_service=ExternalEvidenceImportCheckpointService(
                OperationJournal(USER_DATA_DIR/"operation_journal.jsonl"),
                rules_fingerprint_provider=lambda:
                    ImageAuthorityInitializationService(
                        knowledge=self.state.knowledge,
                    ).current_rules_fingerprint(),
            ),
        )
        if not matching_ready:
            index=dialog.scope.findData(("current_filter",""))
            if index>=0:
                dialog.scope.setItemText(index,"Current search unavailable — refresh Dataset search first")
                dialog.scope.model().item(index).setEnabled(False)
                if dialog.scope.currentIndex()==index:
                    # Keep all-images scope a deliberate choice.
                    dialog.scope.setCurrentIndex(0)
        if dialog.exec()!=QDialog.Accepted or dialog.plan is None:
            return
        self.refresh_external_evidence.emit(dialog.plan)

    def preparation_paths(self):
        selected=self.selected_paths()
        if selected:
            return [path for path in selected if DatasetPage._storage_writable(self,path)]
        if not DatasetPage._dataset_search_results_ready(self):
            return []
        if self.batch_mode or DatasetPage.current_stage(self)=="complete":
            return []
        return [Path(path) for path in self.filtered if DatasetPage._storage_writable(self,path)]

    def _visible_batch_paths(self):
        size=max(12,int(getattr(self.settings,"gallery_page_size",24)))
        paths=DatasetPage._resolution_filtered_paths(self,self.filtered)
        return paths[self.page*size:(self.page+1)*size]

    def apply_batch_page(self):
        if getattr(self,"_batch_task",None) is not None: return
        if not self.batch_mode or not self.batch_search: return
        if self.batch_mode not in {"verify","missing"}: return
        if not self._exception_storage_ready(): return
        if self.batch_mode=="missing":
            target=self._resolve_focused_review_target(self.batch_search.target)
            if not target:return
            if normalize_tag(target)!=normalize_tag(self.batch_search.target):
                self.batch_search=MissingTagSearch(
                    target,list(self.batch_search.required),list(self.batch_search.absent),
                    mode=self.batch_search.mode,
                )
        visible=self._visible_batch_paths(); selected=[path for path in visible if str(path) in self.batch_selected]
        unselected=[path for path in visible if str(path) not in self.batch_selected]
        action="remove the SDXL Tag from" if self.batch_mode=="verify" else "add the SDXL Tag to"
        note=(f"This will {action} {len(selected)} selected image(s). "
              f"CollectiGen will remember {len(unselected)} unselected image(s) for this exact review search. "
              "Saved Image Knowledge history does not restore those separate search-exception decisions.")
        if QMessageBox.question(self,"Apply page decisions",note)!=QMessageBox.Yes: return
        try:
            history_operation=capture_operation(self,selected,operation_kind="dataset_page_decisions",reason="Apply selected Dataset review decisions") if selected else None
        except Exception as exc:
            self._history_admission_failed(exc); return
        started,active=self.state.begin_long_operation(
            "dataset",f"Applying {len(selected):,} Dataset review decision(s)",
        )
        if not started:
            show_operation_in_progress(self,active); return
        mode=self.batch_mode; search=self.batch_search; target=search.target
        knowledge=self.state.knowledge; characters=self.characters

        def work(progress,should_cancel):
            snapshots=[]; changed_records=[]
            def result(*,cancelled=False,error="",interrupted=False):
                return {"snapshots":snapshots,"records":changed_records,"cancelled":cancelled,"error":error,
                        "history_operation_id":history_operation.operation_id if history_operation is not None else "",
                        "history_paths":tuple(selected),"history_interrupted":interrupted}
            automation=active_automation_engine(knowledge)
            for index,path in enumerate(selected,1):
                if should_cancel():
                    return result(cancelled=True)
                try:
                    with self._history_record(path,history_operation) as record:
                        before=copy.deepcopy(record.annotation); changed=False
                        if mode=="verify":
                            record.remove_tag(target)
                            changed=ImageFindingDecisionService(
                                record.annotation,knowledge=knowledge,
                            ).ignore_for_image(tags=(target,)) or changed
                        elif mode=="missing":
                            issues=tag_addition_decision_issues(
                                record.annotation,(target,),knowledge=knowledge,
                                automation_engine=automation,
                            )
                            if issues:
                                raise RuntimeError(
                                    f"{path.name} requires explicit review in the image data editor: "
                                    + "; ".join(issue.message for issue in issues)
                                )
                            before_tags=record.annotation.all_structured_tags()
                            after_tags=TagCollection([*before_tags,target]).to_list()
                            warnings=automation.conflict_warnings(path,before_tags,after_tags)
                            if warnings:
                                raise RuntimeError(
                                    f"{path.name} would introduce a diagnostic conflict; review it in the image data editor: "
                                    + "; ".join(warnings)
                                )
                            changed=record.confirm_sdxl_tags([target],knowledge=knowledge)
                        if changed:
                            record.modified=True
                            record.save_sidecar(force=True,knowledge=knowledge)
                            snapshots.append((path,before)); changed_records.append(record)
                            record.export_caption(character_db=characters,knowledge=knowledge)
                except Exception as exc:
                    return result(error=str(exc),interrupted=isinstance(exc,HistoryWriteInterrupted))
                progress(index,len(selected),path.name)
            try:
                self._update_search_exceptions(search,hide=unselected,show=selected)
            except Exception as exc:
                return result(error=str(exc))
            return result()

        task=CancelableProgressCallTask(work)
        self._batch_task=task; self._batch_task_kind="apply"
        self.apply_batch_button.setEnabled(False); self.undo_batch_button.setEnabled(False)
        self.cancel_projection_button.setText("Cancel Safely")
        self.cancel_projection_button.setEnabled(True); self.cancel_projection_button.show()
        self.header.set_status(f"Applying Dataset review decisions for {len(selected):,} image(s)…")
        self.update_selection_controls()
        task.signals.progress.connect(
            lambda index,total,name:self.state.update_long_operation("dataset",index,total,name)
        )
        task.signals.complete.connect(
            lambda result,visible=tuple(visible),remembered=len(unselected):
            self._batch_apply_complete(result,visible,remembered)
        )
        task.signals.cancelled.connect(self._batch_task_cancelled)
        task.signals.failed.connect(self._batch_task_failed)
        start_qt_delivery_task(self.pool,task)

    def undo_batch(self):
        if getattr(self,"_batch_task",None) is None:
            return self._review_history_target("batch")

    def _finish_batch_task(self):
        self.state.finish_long_operation("dataset")
        self._batch_task=None; self._batch_task_kind=""
        self.cancel_projection_button.hide()
        self.apply_batch_button.setEnabled(bool(self.batch_mode))
        self.update_selection_controls()

    def _cache_batch_records(self,records):
        if not self.dataset or not self.dataset.index: return
        for record in records: self._cache_record(record)

    def _batch_apply_complete(self,result,visible,remembered):
        self._finish_batch_task()
        snapshots=list(result.get("snapshots",()))
        self.batch_undo=[]
        self._remember_history_target("batch",result.get("history_paths",()),result.get("history_operation_id",""))
        self._cache_batch_records(result.get("records",()))
        if getattr(self,"_shutdown_requested",False):
            self._notify_background_work_finished(); return
        error=str(result.get("error","") or "")
        if error:
            QMessageBox.critical(
                self,"Batch review failed",
                error+("\n\nReview Batch Changes opens the exact captured operation, including any recovery needs." if result.get("history_operation_id") else ""),
            )
            self._notify_background_work_finished(); return
        if result.get("cancelled"):
            self.header.set_status(
                f"Stopped safely after {len(snapshots):,} image change(s)"
                +(" • Review Batch Changes is available" if result.get("history_operation_id") else "")
            )
            self._notify_background_work_finished(); return
        for path in visible: self.batch_selected.discard(str(path))
        self.header.set_status(
            f"Saved {len(snapshots)} image change(s) • remembered {remembered} review decision(s)"
        )
        self.start_batch_review(); self._notify_background_work_finished()

    def _batch_task_cancelled(self):
        self._finish_batch_task()
        if not getattr(self,"_shutdown_requested",False):
            self.header.set_status("Dataset review operation cancelled safely")
        self._notify_background_work_finished()

    def _batch_task_failed(self,message: str):
        self._finish_batch_task()
        if not getattr(self,"_shutdown_requested",False):
            QMessageBox.critical(self,"Batch review failed",message)
        self._notify_background_work_finished()

    def inspect_image(self,value: str,record: ImageRecord|None=None):
        self._cancel_transient_inspection("_record_inspection_task")
        self._cancel_transient_inspection("_inspection_task")
        path=Path(value); self.current_path=path
        self._update_preparation_emphasis()
        DatasetPage._render_issue_actions(self,())
        dataset=getattr(self,"dataset",None)
        profile=(
            dataset.profile(dataset.identity_for_path(path).library_id)
            if dataset is not None
            and hasattr(dataset,"identity_for_path")
            and dataset.identity_for_path(path) is not None else None
        )
        self.image_name.setText(
            f"{path.name}\n{profile.name} — {profile.display_role}"
            if profile is not None else path.name
        )
        self._queue_responsive_layout()
        for card_path,card in self.cards.items(): card.set_active_selected(card_path==str(path))
        self._record_inspection_generation+=1; generation=self._record_inspection_generation
        self._record_inspection_error=""; self._inspection_error=""; self._inspection_details_path=None
        availability=DatasetPage._storage_availability(self,path)
        DatasetPage._update_inspector_storage_actions(self,availability)
        if availability is not None and not bool(getattr(availability,"readable",True)):
            self.current_record=None; self._record_inspection_task=None
            message=(
                f"{getattr(availability,'label','Image unavailable')}\n\n"
                f"{getattr(availability,'detail','')}\n\n"
                f"Recovery\n{getattr(availability,'recovery','Refresh the library after restoring access.')}"
            ).strip()
            self.overview.setPlainText(message)
            self.training_preview.setPlainText(
                "Training information remains in the saved catalog, but current files must be available locally before it can be recalculated."
            )
            self.issue_details.setPlainText(message)
            DatasetPage._render_issue_actions(self,({
                "group":"Folder and Sources",
                "title":str(getattr(availability,"label","Image unavailable")),
                "detail":str(getattr(availability,"recovery","Refresh the library after restoring access.")),
                "action_id":"review_problems","action_label":"Review Dataset Problems…",
            },))
            return
        self.current_report=None
        self._current_report_fingerprint=None
        if record is not None:
            self._record_inspection_task=None
            self._populate_image_inspector(
                record,path,generation,quick_review_ready=False,refresh_training=False,
            )
        else:
            self.current_record=None
            self.overview.setPlainText("Loading this image's information in the background…")
            if hasattr(self,"quick_review_items"):
                self.quick_review_items.clear(); self.quick_review_status.setText("Checking current findings…"); self.quick_review_open.setEnabled(False); self._set_quick_review_count(0)
            self.training_preview.setPlainText("The training preview will appear after the image information loads.")
            self.issue_details.setPlainText("Image Knowledge checks will appear after the image information loads.")

        def load():
            expected=DatasetPage._workflow_family_fingerprint(path)
            loaded=record if record is not None else ImageRecord(path)
            if task.is_cancelled():return
            try:
                report=ImageEvidenceInspector(
                    knowledge=self.state.knowledge,
                ).inspect(path)
            except Exception:
                report=None
            if task.is_cancelled():return
            candidates=(current_candidate_findings(
                loaded.annotation.source.get("candidate_findings",[]),report,record=loaded,
            ) if report is not None else None)
            from core.source_inspection import workflow_source_lookup_names
            workflow=self.workflow_states.assess_record(
                loaded,derived_result={"candidate_findings":candidates,
                    "workflow_source_lookup_names":workflow_source_lookup_names(report)},
            )
            return generation,str(path),loaded,report,True,expected,workflow

        task=AsyncCallTask(load); self._record_inspection_task=task
        task.signals.complete.connect(self._record_inspection_loaded)
        task.signals.failed.connect(lambda message,g=generation,p=str(path):self._record_inspection_failed(g,p,message))
        self.pool.start(task,1)

    def _record_inspection_loaded(self,result):
        generation,value,record,*details=result; path=Path(value)
        if generation!=self._record_inspection_generation or self.current_path!=path: return
        self._record_inspection_task=None
        report=details[0] if details else None
        refresh_training=bool(details[1]) if len(details)>1 else True
        if len(details)>2 and details[2]!=DatasetPage._workflow_family_fingerprint(path):
            self.inspect_image(str(path))
            return
        self._current_report_fingerprint=details[2] if len(details)>2 else None
        if len(details)>3:
            self._refresh_cached_workflow_state(path,workflow=details[3],expected=details[2])
        self._populate_image_inspector(
            record,path,generation,report=report,
            refresh_training=refresh_training,
        )

    def _populate_image_inspector(
        self,record: ImageRecord,path: Path,generation: int,*,report=None,
        refresh_training=True,quick_review_ready=True,
    ):
        if generation!=self._record_inspection_generation or self.current_path!=path: return
        try:
            self.current_record=record; self.current_report=report
            annotation=record.annotation
            raw_tags=ConfirmedTagIdentityService(
                self.state.knowledge
            ).canonical_displays(
                annotation.all_structured_tags(),
                stored_ids=annotation.tag_identities,
            )
            tags=TagOrderingService(self.state.knowledge,self.characters).order(
                [TagOrderCandidate(tag,sequence=index) for index,tag in enumerate(raw_tags)],
                canonicalize=False,deterministic=True,
            )
            status=("Caption file: available" if record.caption_path.is_file() else "Caption file: missing")
            organized=("Organized image data: available" if record.sidecar_path.is_file() else "Organized image data: not created yet")
            sources=[]
            identity=(
                self.dataset.identity_for_path(path)
                if self.dataset is not None and hasattr(self.dataset,"identity_for_path")
                else None
            )
            profile=(
                self.dataset.profile(identity.library_id)
                if identity is not None else None
            )
            if profile is not None:
                sources.append(
                    f"Source library: {profile.name} — {profile.display_role}"
                )
                sources.append(
                    f"Stable image reference: {identity.library_id}:{identity.relative_path}"
                )
            if record.caption_path.is_file(): sources.append(f"Caption file: {record.caption_path}")
            if record.sidecar_path.is_file(): sources.append(f"Organized data: {record.sidecar_path}")
            sources.append(f"Image file: {path}")
            cached=self._readiness_cache.get(path)
            workflow=cached[1] if cached is not None else None
            current_state=DatasetPage._inspector_summary_status(
                workflow,DatasetPage.current_stage(self),
            )
            issues=self.audit_issues.get(path,[])
            character_names=[]
            for character in annotation.characters:
                known=getattr(self.characters,"characters",{}).get(character.character_id)
                name=known.name if known is not None else character.character_id or "unnamed"
                character_names.append(f"Character {character.slot}: {name}")
            self.overview.set_summary(
                image_key=identity.key if identity is not None else str(path),
                status=current_state,tags=tags,descriptions=annotation.plain_language,
                characters=character_names,
                source_overview=f"{status}\n{organized}",source_details=sources,
                issues=tuple(item.message for item in issues),
            )
            if quick_review_ready:
                self._populate_quick_review(record,report)
            elif hasattr(self,"quick_review_items"):
                self.quick_review_items.clear()
                self.quick_review_status.setText("Checking current findings…")
                self.quick_review_open.setEnabled(False)
                self._set_quick_review_count(0)
            if refresh_training:
                self._populate_training_details(record)
        except Exception as exc:
            self._record_inspection_failed(generation,str(path),str(exc))

    @staticmethod
    def _inspector_summary_status(workflow,stage: str) -> str:
        if workflow is None:
            return "Image Knowledge loaded • completeness assessment pending"
        if workflow.ready and stage=="complete":
            return ""
        return f"{workflow.label}\n{workflow.detail}"

    def begin_sensitivity_refresh(self,paths):
        """Retire intersecting workers while retaining unrelated ready results."""
        affected=set(map(Path,paths))
        if affected.intersection(getattr(self,"_workflow_scan_paths",())):
            self._invalidate_workflow_work()
        if getattr(self,"current_path",None) in affected:self._invalidate_editor_load()
        self.categories.load()
        self.workflow_states.knowledge=self.state.knowledge
        self._readiness_context_fingerprint=self.workflow_states.context_fingerprint()

    def sensitivity_refresh_incomplete(self,groups):
        from core.image_workflow_state import sensitivity_cached_paths
        inputs={path:getattr(cached[1],"rating_tags",None)
                for path,cached in self._readiness_cache.items()}
        for path in getattr(self,"_workflow_scan_paths",()):inputs.setdefault(path,None)
        affected=sensitivity_cached_paths(groups,inputs)
        if not affected:return
        self.begin_sensitivity_refresh(affected)
        for path in affected:
            self._readiness_cache.pop(path,None)
            self._inspection_detail_cache.pop(path,None)
            self.audit_issues.pop(path,None)
        DatasetPage._invalidate_dataset_search(self)
        self.render_page()
        self.header.set_status("Content-rating checks are incomplete; affected image status will refresh when needed")

    def sensitivity_results_changed(self,paths):
        affected=set(map(Path,paths)) & set(getattr(self.dataset,"images",()))
        for path in affected:
            self._readiness_cache.pop(path,None)
            self._inspection_detail_cache.pop(path,None)
            self.audit_issues.pop(path,None)
        if affected:
            self._restore_persisted_workflow_states()
            self.apply_filters(preserve_page=True)

    def refresh_system_knowledge(self,*,preserve_workflow=False):
        """Refresh canonical names and Quick Review without rereading files."""
        resume_paths=(tuple(getattr(self,"_workflow_scan_paths",()))
                      if preserve_workflow and getattr(self,"_filter_state_task",None) is not None else ())
        resume_automatic=bool(getattr(self,"_filter_state_automatic",False))
        previous_context=str(
            getattr(self,"_readiness_context_fingerprint","") or ""
        )
        self.workflow_states.knowledge=self.state.knowledge
        self.workflow_states.categories=self.categories
        self.workflow_states.characters=self.characters
        current_context=self.workflow_states.context_fingerprint()
        context_changed=current_context!=previous_context
        if context_changed:
            self._invalidate_workflow_work()
            self._invalidate_editor_load()
            self._readiness_context_fingerprint=current_context
            if not preserve_workflow:
                self._readiness_cache.clear()
                self._inspection_detail_cache.clear()
                self._restore_persisted_workflow_states()
        projection=getattr(self,"training_caption_projection",None)
        dataset=getattr(self,"dataset",None)
        if projection is not None and not preserve_workflow:
            projection.invalidate_after_authority_refresh(
                getattr(dataset,"images",()) if dataset is not None else ()
            )
        correction=getattr(self,"image_identity_correction",None)
        if correction is not None:
            correction.refresh_system_knowledge()
        if getattr(self,"_defer_expensive_initialization",False):
            if self._conflict_task is not None:
                self._conflict_task.cancel(); self._conflict_task=None
            self._start_conflict_preparation()
        else:
            selected_rule_id=str(self.conflict_rule.currentData() or "")
            self.conflicts=ConflictRuleEngine.load(
                DEFAULTS_DIR/"conflicts.json",tags=self.state.knowledge,
            )
            self.conflict_rule.clear()
            for rule in self.conflicts.rules:
                self.conflict_rule.addItem(
                    rule.label or ", ".join(self.conflicts.effective_tags(rule)),rule.id,
                )
            selected_index=self.conflict_rule.findData(selected_rule_id)
            if selected_index>=0:self.conflict_rule.setCurrentIndex(selected_index)
        if context_changed and not preserve_workflow:
            self.apply_filters(preserve_page=True)
        elif context_changed and resume_paths:
            if not self._start_workflow_filter_scan(resume_paths,automatic=resume_automatic,defer_if_busy=True):
                self._deferred_dataset_filter=(DatasetPage._dataset_filter_token(self),resume_paths)
                self._dataset_filter_retry_timer.start()
                self.cancel_projection_button.setText("Cancel Safely")
                self.cancel_projection_button.setEnabled(True); self.cancel_projection_button.show()
        if self.current_record is not None and self.current_path is not None:
            if (getattr(self,"_current_report_fingerprint",None)
                    !=DatasetPage._workflow_family_fingerprint(self.current_path)):
                self.inspect_image(str(self.current_path))
                return
            self._populate_image_inspector(
                self.current_record,self.current_path,
                self._record_inspection_generation,
                report=getattr(self,"current_report",None),
                refresh_training=False,
            )

    def _record_inspection_failed(self,generation: int,value: str,message: str):
        path=Path(value)
        if generation!=self._record_inspection_generation or self.current_path!=path: return
        self._record_inspection_task=None; self._record_inspection_error=message
        self.current_record=None; self.current_report=None
        self.overview.setPlainText(f"CollectiGen could not read this image's information.\n\n{message}")
        self.quick_review_items.clear(); self.quick_review_status.setText(message); self.quick_review_open.setEnabled(False); self._set_quick_review_count(0)
        self.training_preview.clear(); self.issue_details.setPlainText(message)
        DatasetPage._render_issue_actions(self,({
            "group":"Assessment","title":"Image information unavailable",
            "detail":"Retry reading this image after restoring access or repairing its saved record.",
            "action_id":"retry_assessment","action_label":"Retry Assessment",
        },))

    def _set_quick_review_count(self,count: int) -> None:
        index=self.tabs.indexOf(self.quick_review)
        if index>=0:
            self.tabs.setTabText(
                index,"Quick Review"+(f" ({int(count):,})" if count else ""),
            )

    def _populate_quick_review(self,record: ImageRecord,report=None) -> None:
        if not hasattr(self,"quick_review_items"):
            return
        self.quick_review_items.clear()
        annotation=record.annotation
        confirmed=annotation.all_structured_tags()
        candidates=[]
        source_fingerprints={}
        for finding in current_candidate_findings(
            annotation.source.get("candidate_findings",[]),report,record=record,
        ):
            sources=tuple(finding["sources"])
            source_fingerprints.update(finding.get("source_fingerprints",{}))
            candidates.append((
                finding["value"],
                "SDXL Tag" if finding["kind"]=="tag" else "Plain Language Description",
                ", ".join(sources),sources,"",
            ))
        for evidence in annotation.ai_evidence:
            for prediction in evidence.typed_tags():
                value=str(prediction.get("tag") or "").strip()
                if evidence.decisions.get(normalize_tag(value),"") not in {
                    "accepted","rejected",*ImageFindingDecisionService.DEFERRED_VALUES,
                }:
                    source=evidence.model_id or "Unknown model"
                    candidates.append((value,"AI Tag",source,(source,),evidence.id))
            prose=evidence.typed_plain_language()
            if prose and evidence.decisions.get("plain_language","") not in {
                "accepted","rejected",*ImageFindingDecisionService.DEFERRED_VALUES,
            }:
                candidates.extend(
                    (
                        value,"AI Description",evidence.model_id or "Unknown model",
                        (evidence.model_id or "Unknown model",),evidence.id,
                    )
                    for value in split_plain_language(prose)
                )
        tag_values=[value for value,kind,_source,_sources,_evidence_id in candidates if "Tag" in kind]
        plan=ConfirmedTagIdentityService(self.state.knowledge).plan(
            [*confirmed,*tag_values],
            stored_ids=annotation.tag_identities,
            confirmation=False,
        )
        identities={entry.normalized_value:entry for entry in plan.entries}
        confirmed_ids={
            entry.stable_id for value in confirmed
            if (entry:=identities.get(normalize_tag(value))) is not None
            and entry.stable_id
        }
        confirmed_keys={normalize_tag(value) for value in confirmed}
        confirmed_prose={
            normalize_plain_language_exclusion(sentence)
            for value in annotation.plain_language for sentence in split_plain_language(value)
        }
        decisions=ImageFindingDecisionService(annotation,knowledge=self.state.knowledge)
        unavailable_tag_ids=unavailable_confirmed_tag_identities(
            annotation,self.state.knowledge,
        )
        unavailable_prose=(
            decisions.ignored_description_keys()
            | {
                normalize_plain_language_exclusion(value)
                for value in getattr(
                    self.settings,"ignored_plain_language_sentences",[],
                )
                if normalize_plain_language_exclusion(value)
            }
        )
        automation=active_automation_engine(self.state.knowledge)
        seen=set()
        for raw,kind,source,sources,evidence_id in candidates:
            value=raw
            if not value:
                continue
            if "Tag" in kind:
                identity=identities.get(normalize_tag(value))
                if identity is not None and identity.stable_id:
                    if identity.stable_id in confirmed_ids:
                        continue
                    value=identity.canonical_display or value
                key=("tag",normalize_tag(value))
                decision_identity=image_tag_identity(value,self.state.knowledge)
                if (
                    key[1] in confirmed_keys
                    or decision_identity in unavailable_tag_ids
                    or all(
                        decisions.source_is_deferred("tag",value,item,evidence_id=evidence_id)
                        for item in (sources or ("Unknown",))
                    )
                    or automation.is_evidence_rejected(value)
                ):
                    continue
            else:
                key=("description",normalize_plain_language_exclusion(value))
                if (
                    key[1] in confirmed_prose|unavailable_prose
                    or all(
                        decisions.source_is_deferred("description",value,item,evidence_id=evidence_id)
                        or decisions.source_was_used_as_edited(
                            "description",value,item,evidence_id=evidence_id,
                            source_fingerprint=source_fingerprints.get(item,"") if not evidence_id else "",
                        )
                        for item in (sources or ("Unknown",))
                    )
                ):
                    continue
            if not key[1] or key in seen:
                continue
            seen.add(key)
            self.quick_review_items.addTopLevelItem(
                QTreeWidgetItem((value,kind,source or "Unknown"))
            )
        count=self.quick_review_items.topLevelItemCount()
        self.quick_review_status.setText(
            f"{count:,} finding{'s' if count!=1 else ''} ready for review."
            if count else "No findings to review."
        )
        self.quick_review_open.setEnabled(bool(count))
        self._set_quick_review_count(count)

    def _render_issue_actions(self,actions) -> None:
        if not hasattr(self,"issue_actions"):
            return
        for row in self._issue_action_rows:
            self.issue_actions_layout.removeWidget(row)
            row.hide(); row.setEnabled(False); row.deleteLater()
        self._issue_action_rows=[]; self._issue_action_buttons=[]
        actions=[dict(item) for item in actions]
        if not actions:
            self.issue_action_summary.clear(); self.issue_actions.hide()
            if hasattr(self,"issue_actions_scroll"):self.issue_actions_scroll.hide()
            return
        groups={}
        for item in actions:
            groups.setdefault(item.get("group") or "Next Steps",[]).append(item)
        insertion=0
        for group_name,items in groups.items():
            group=QGroupBox(group_name); group_layout=QVBoxLayout(group)
            for item in items:
                row=QWidget(); row_layout=QHBoxLayout(row); row_layout.setContentsMargins(0,0,0,0)
                title=str(item.get("title") or "Issue needs review")
                detail=str(item.get("detail") or "")
                explanation=QLabel(title+(f"\n{detail}" if detail else "")); explanation.setWordWrap(True)
                action_id=str(item.get("action_id") or "")
                button=compact_button(QPushButton(str(item.get("action_label") or "Review…")))
                button.setAccessibleName(f"{button.text()} for {title}")
                button.clicked.connect(lambda _checked=False,value=action_id:self._run_issue_action(value))
                row_layout.addWidget(explanation,1); row_layout.addWidget(button,0,Qt.AlignTop)
                group_layout.addWidget(row); self._issue_action_buttons.append(button)
            self.issue_actions_layout.insertWidget(insertion,group); insertion+=1
            self._issue_action_rows.append(group)
        first=actions[0]
        self.issue_action_summary.setText(
            f"{len(actions):,} actionable issue{'s' if len(actions)!=1 else ''}. "
            f"Review All Issues starts with {first.get('action_label','the highest-consequence issue')}."
        )
        self.review_all_issues_button.setText(f"Review All Issues… ({len(actions):,})")
        self.review_all_issues_button.setEnabled(True); self.issue_actions.show()
        if hasattr(self,"issue_actions_scroll"):
            self.issue_actions_scroll.show()
            self.issue_actions_scroll.verticalScrollBar().setValue(0)

    def review_all_issues(self):
        if self._issue_action_buttons:
            self._issue_action_buttons[0].click()

    def _run_issue_action(self,action_id: str):
        if action_id in {"edit_image_data","review_findings"}:
            result=self.open_data_editor()
            if result!=QDialog.Accepted:self._reassess_current_issue_state()
        elif action_id=="export_training_txt":
            self._export_training_captions([self.current_path] if self.current_path else [])
            self._reassess_current_issue_state()
        elif action_id=="backup_metadata":
            self.sync_selected_data(
                [self.current_path] if self.current_path else [],
                preferred_targets=("metadata",),
            )
        elif action_id=="setup_metadata":
            ensure_capability(
                self,self.settings,"metadata","Backing up confirmed Tags to Metadata",
            )
            self._reassess_current_issue_state()
        elif action_id=="review_problems":
            self.browse_problems()
        elif action_id.startswith("review_conflict:"):
            self._review_current_conflict(action_id.split(":",1)[1])
        elif action_id=="retry_assessment":
            self._reassess_current_issue_state()

    def _reassess_current_issue_state(self):
        if self.current_path is None:
            return
        self._invalidate_inspection_cache(self.current_path)
        self._refresh_cached_workflow_state(self.current_path)
        if self.current_record is not None:self._populate_training_details(self.current_record)
        else:self.inspect_image(str(self.current_path))

    def _review_current_conflict(self,rule_id: str):
        if self.current_record is None:
            QMessageBox.information(self,"Select an image","Select an image from the gallery first."); return
        rule=next(
            (item for item in self.conflicts.rules if rule_id in item.accepted_ids),
            None,
        )
        if rule is None:
            self._populate_training_details(self.current_record)
            return
        self.open_automation.emit(
            "review.conflicts.guided_checks",
            {
                "image":str(self.current_record.image_path),
                "rule_id":rule.id,
                "relationship_id":rule.relationship_id or rule.id,
                "origin":"dataset_inspector",
            },
        )

    def _populate_training_details(self,record: ImageRecord):
        self._cancel_transient_inspection("_inspection_task")
        path=record.image_path; self._inspection_generation+=1; generation=self._inspection_generation
        self._inspection_details_path=None; self._inspection_error=""
        audit=tuple(self.audit_issues.get(path,[]))
        profile=self._training_export_profile()
        report=(getattr(self,"current_report",None)
                if getattr(self,"_current_report_fingerprint",None)
                ==DatasetPage._workflow_family_fingerprint(path) else None)
        candidates=(current_candidate_findings(
            record.annotation.source.get("candidate_findings",[]),report,record=record,
        ) if report is not None else None)
        key=(str(path),profile.fingerprint(),tuple((item.severity,item.message) for item in audit),
             DatasetPage._workflow_family_fingerprint(path),
             repr(candidates))
        cached=self._inspection_detail_cache.get(key)
        if cached is not None:
            self._training_details_ready(cached,str(path),generation,key); return
        self.training_preview.setPlainText("Calculating the training preview in the background…")
        self.issue_details.setPlainText("Checking Image Knowledge completeness and possible issues in the background…")
        DatasetPage._render_issue_actions(self,())
        snapshot=copy.copy(record)
        snapshot.annotation=copy.deepcopy(record.annotation)
        def calculate():
            return {
                "path":str(path),"generation":generation,"key":key,
                "details":self._build_training_details(
                    path,audit,profile,record=snapshot,candidate_findings=candidates,
                ),
            }
        task=AsyncCallTask(calculate); self._inspection_task=task
        task.signals.complete.connect(self._training_details_result)
        task.signals.failed.connect(
            lambda message,p=str(path),g=generation:self._training_details_failed(p,g,message)
        )
        self.pool.start(task,-1)

    def _build_training_details(self,path: Path,audit,profile=None,*,record=None,candidate_findings=None):
        record=record if record is not None else ImageRecord(path)
        profile=profile or self._training_export_profile()
        readiness=TrainingReadinessService(
            self.characters,self.state.knowledge,categories=self.categories,
            ignored_plain_language=getattr(
                getattr(self,"settings",None),
                "ignored_plain_language_sentences",(),
            ),
        ).assess_record(record,profile,candidate_findings=candidate_findings)
        projection=profile.project(
            record.annotation,
            self.characters,
            self.state.knowledge,
        )
        counts=projection.decision_counts()
        explanations="\n".join(projection.explanation_lines()[1:])
        plain_preview=(
            f"{profile.name} v{profile.version}\n{profile.profile_id}"
            "\n\nProjection summary\n"
            f"Emitted {counts['emitted']} • Combined {counts['combined']} • "
            f"Omitted {counts['omitted']}\n"
            "Export Training TXT… previews the complete caption."
        )
        training_health=readiness.training_export_health
        metadata_health=readiness.metadata_backup_health
        if training_health is not None:
            plain_preview+=(
                f"\n\nTraining Export Health: {training_health.label}\n"
                f"{training_health.detail}"
            )
        decision_text=explanations or "No confirmed facts were projected."
        plain_preview+=f"\n\nProjection decisions\n{decision_text}"
        preview=_TrainingProjectionDetails(
            plain_preview,profile_name=profile.name,
            profile_version=profile.version,profile_id=profile.profile_id,
            counts=counts,decisions=decision_text,
            health_label=(training_health.label if training_health is not None else "Unavailable"),
            health_detail=(training_health.detail if training_health is not None else ""),
        )
        lines=[
            f"Image Knowledge completeness: {readiness.score}/100",
            "Image Knowledge is complete enough for this workflow"
            if readiness.completeness and readiness.completeness.ready
            else "Image Knowledge needs attention",
            "Next Image Knowledge action: "
            +(
                readiness.completeness.quickest_action
                if readiness.completeness is not None
                else readiness.quickest_action
            ),
        ]
        actions=[]

        def add_action(group,title,detail,action_id,action_label,priority):
            actions.append({
                "group":group,"title":str(title),"detail":str(detail or ""),
                "action_id":action_id,"action_label":action_label,
                "priority":int(priority),
            })
        if readiness.components:
            lines.extend(("","Explainable components"))
            for component in readiness.components:
                lines.append(f"• {component.name.replace('_',' ').title()}: {component.score:.1f}/{component.maximum:.0f}")
        image_knowledge_issues=[
            issue for issue in readiness.issues
            if issue.component!="selected_export"
        ]
        selected_profile_issues=[
            issue for issue in readiness.issues
            if issue.component=="selected_export"
        ]
        if image_knowledge_issues:
            lines.extend(("","Image Knowledge issues"))
            for issue in image_knowledge_issues:
                deduction=f" (−{issue.deduction:.1f})" if issue.deduction else ""
                lines.append(f"• {issue.severity.upper()}: {issue.label}{deduction}")
                lines.append(f"  Recommended: {issue.suggested_action}")
                review_findings=issue.code=="unresolved_evidence"
                add_action(
                    "Image Knowledge",issue.label,issue.suggested_action,
                    "review_findings" if review_findings else "edit_image_data",
                    "Review Findings…" if review_findings else "Edit Image Data…",
                    10 if issue.severity=="high" else 20 if issue.severity=="medium" else 30,
                )
        if training_health is not None:
            lines.extend((
                "",
                f"Training Export Health — {training_health.label}",
                (
                    f"Profile: {training_health.profile_name} v{training_health.profile_version} "
                    f"({training_health.profile_id})"
                ),
                training_health.detail,
                f"Recommended: {training_health.suggested_action}",
                "This separate output state does not lower Image Knowledge completeness.",
            ))
            if training_health.issues:
                add_action(
                    "Training Export",training_health.label,
                    training_health.suggested_action,"export_training_txt",
                    "Export Training TXT…",40,
                )
        if metadata_health is not None:
            lines.extend((
                "",
                f"Metadata Backup Health — {metadata_health.label}",
                f"Container: {metadata_health.container}",
                metadata_health.detail,
                f"Recommended: {metadata_health.suggested_action}",
                "This separate backup state does not lower Image Knowledge completeness.",
            ))
            if metadata_health.issues:
                setup_required=metadata_health.state in {"unavailable","unreadable"}
                add_action(
                    "Metadata Backup",metadata_health.label,
                    metadata_health.suggested_action,
                    "setup_metadata" if setup_required else "backup_metadata",
                    "Set Up Metadata…" if setup_required else "Back Up Tags to Metadata…",
                    18 if setup_required else 50,
                )
        if selected_profile_issues:
            lines.extend(("","Selected-profile readiness"))
            for issue in selected_profile_issues:
                lines.append(f"• {issue.severity.upper()}: {issue.label}")
                lines.append(f"  Recommended: {issue.suggested_action}")
                add_action(
                    "Training Export",issue.label,issue.suggested_action,
                    "export_training_txt","Review Training TXT Export…",15,
                )
        if audit:
            lines.extend(("","Folder and source issues"))
            lines.extend(f"• {item.severity.upper()}: {item.message}" for item in audit)
            for item in audit:
                add_action(
                    "Folder and Sources",item.message,
                    "Open the existing Problems review for the affected library files.",
                    "review_problems","Review Folder Problems…",12,
                )
        conflicts=self.conflicts.evaluate(record)
        if conflicts:
            lines.extend(("","Active SDXL Tag conflicts"))
            lines.extend(f"• {match.message}" for match in conflicts)
            for match in conflicts:
                add_action(
                    "Image Knowledge",match.message,
                    "Choose the correct confirmed Tag or deliberately remember this exact rule as a valid exception.",
                    f"review_conflict:{match.rule.id}","Review Conflict…",5,
                )
        if not image_knowledge_issues and not selected_profile_issues and not audit and not conflicts:
            lines.extend(("","No active Image Knowledge, selected-profile, folder, or SDXL Tag conflict issues were found."))
        actions.sort(key=lambda item:(item["priority"],item["group"],item["title"]))
        return preview,"\n".join(lines),actions

    def _training_details_result(self,result):
        path=str(result.get("path") or ""); generation=int(result.get("generation",-1))
        self._training_details_ready(result.get("details"),path,generation,result.get("key"))

    def _training_details_ready(self,result,path: str,generation: int,key):
        self._inspection_detail_cache[key]=result
        if generation!=self._inspection_generation or not self.current_path or path!=str(self.current_path): return
        self._inspection_task=None; self._inspection_details_path=path; self._inspection_error=""
        preview,issues,actions=result
        if (
            isinstance(self.training_preview,_TrainingInspectorWidget)
            and isinstance(preview,_TrainingProjectionDetails)
        ):
            self.training_preview.set_projection(preview)
        else:
            self.training_preview.setPlainText(str(preview))
        self.issue_details.setPlainText(issues)
        self.training_preview.verticalScrollBar().setValue(0)
        self.issue_details.verticalScrollBar().setValue(0)
        DatasetPage._render_issue_actions(self,actions)

    def _training_details_failed(self,path: str,generation: int,message: str):
        if generation!=self._inspection_generation or not self.current_path or path!=str(self.current_path): return
        self._inspection_task=None; self._inspection_details_path=path; self._inspection_error=message
        self.training_preview.setPlainText("Assessment unavailable. The training preview could not be calculated.")
        self.issue_details.setPlainText(
            "Assessment unavailable\n\nCollectiGen could not calculate the current image details. "
            "Retry the assessment; no Image Knowledge, training TXT, Metadata, or image file was changed."
            f"\n\nTechnical details\n{message}"
        )
        DatasetPage._render_issue_actions(self,({
            "group":"Assessment","title":"Assessment unavailable",
            "detail":"Retry the same current-image assessment safely.",
            "action_id":"retry_assessment","action_label":"Retry Assessment",
        },))

    def _cancel_transient_inspection(self,name: str):
        task=getattr(self,name,None)
        if task is None:
            return
        if hasattr(task,"cancel"):
            task.cancel()
        try:
            if hasattr(self.pool,"tryTake"):
                self.pool.tryTake(task)
        except RuntimeError:
            pass
        if getattr(self,name,None) is task:
            setattr(self,name,None)

    def inspection_background_state(self):
        """Return bounded diagnostics for the active selected-image inspection."""
        path=str(self.current_path) if self.current_path is not None else ""
        record_task=self._record_inspection_task
        details_task=self._inspection_task
        record_state=str(getattr(record_task,"state","idle"))
        details_state=str(getattr(details_task,"state","idle"))
        error=self._record_inspection_error or self._inspection_error
        if path and self._inspection_details_path==path:
            state="failed" if error else "ready"
        elif error:
            state="failed"
        elif details_task is not None:
            state=f"details_{details_state}"
        elif record_task is not None:
            state=f"record_{record_state}"
        else:
            state="idle"
        return {
            "state":state,
            "path":path,
            "details_path":str(self._inspection_details_path or ""),
            "record_task":record_state,
            "details_task":details_state,
            "error":error,
            "pool_active":int(self.pool.activeThreadCount()) if hasattr(self.pool,"activeThreadCount") else -1,
            "pool_limit":int(self.pool.maxThreadCount()) if hasattr(self.pool,"maxThreadCount") else -1,
        }

    def _invalidate_inspection_cache(self,path: Path):
        value=str(path)
        self._inspection_detail_cache={key:details for key,details in self._inspection_detail_cache.items() if key[0]!=value}

    def _finish_edit(self,message: str):
        record=self.current_record; record.modified=True
        operation=None
        try:
            operation=capture_operation(self,(record.image_path,),operation_kind="dataset_saved_edit",reason=message)
            with self._history_record(record.image_path,operation,record) as record:
                record.save_sidecar(force=True)
                record.export_caption(character_db=self.characters,knowledge=self.state.knowledge)
        except Exception as exc:
            if isinstance(exc,HistoryWriteInterrupted) and operation is not None:
                self._remember_history_target("edit",(record.image_path,),operation.operation_id)
            self._history_admission_failed(exc); return
        self._invalidate_inspection_cache(record.image_path)
        if self.dataset and self.dataset.index:
            self._cache_record(record)
        self._refresh_cached_workflow_state(record.image_path)
        self._remember_history_target("edit",(record.image_path,),operation.operation_id if operation is not None else "")
        self.inspect_image(str(record.image_path),record)
        self.overview.append(f"\nSaved: {message}")

    def open_data_editor(self):
        if self.current_record is None:
            QMessageBox.information(self,"Select an image","Select an image from the gallery first."); return
        self._batch_conflict_context={}
        return self._queue_data_editor_load(
            self.current_record.image_path,
            record=self.current_record,report=self.current_report,
        )

    def _run_data_editor_dialog(
        self,record,report,preview_image=None,prepared_context=None,
        *,review_parent=None,initial_tab_index: int=0,_context_envelope=None,
    ):
        if _context_envelope is not None:prepared_context=_context_envelope.pop()
        if review_parent is None:
            self.current_record=record; self.undo_annotation=copy.deepcopy(record.annotation)
        cached=getattr(self,"_readiness_cache",{}).get(
            Path(record.image_path),
        )
        preparation_complete=bool(
            cached and getattr(cached[1],"ready",False)
        ) or (review_parent is None and DatasetPage.current_stage(self)=="complete")
        maintenance=getattr(self,"_image_tag_maintenance_dialog",None)
        editor_parent=(
            maintenance if maintenance is not None and maintenance.isVisible()
            else self
        )
        dialog=ImageDataEditor(
            record,report,self.settings,review_parent or editor_parent,knowledge=self.state.knowledge,
            characters=self.characters,preview_image=preview_image,
            content_safety=content_safety_policy(),
            source_correction_available=review_parent is None,
            image_knowledge_only=review_parent is not None,
            prepared_context=prepared_context,
            preparation_complete=preparation_complete,
            initial_tab_index=initial_tab_index,
        )
        prepared_context=None  # Release the construction snapshot before nested edits.
        dialog.ai_requested.connect(lambda kind,current=dialog:self.run_image_ai(kind,current))
        if hasattr(dialog,"preparation_requested"):
            dialog.preparation_requested.connect(
                lambda options,current=dialog:
                    self.run_image_preparation(options,current)
            )
        if hasattr(dialog,"edit_tag_requested"):dialog.edit_tag_requested.connect(self.edit_tag_requested)
        if hasattr(dialog,"create_tag_requested"):dialog.create_tag_requested.connect(self.create_tag_requested)
        self._active_editor=dialog
        result=dialog.exec()
        if self._active_editor is dialog: self._active_editor=None
        if result in {
            QDialog.Accepted,REQUEST_SOURCE_CORRECTION,
        } and dialog.persisted_changes:
            self._invalidate_inspection_cache(record.image_path)
            evidence_changed=False
            if self.dataset and self.dataset.index:
                self._cache_record(record,publish_content_safety=False)
                evidence_changed=True
            self._refresh_cached_workflow_state(record.image_path)
            self._publish_editor_image_data_changed(
                record.image_path,evidence_changed=evidence_changed,
            )
            if review_parent is None:
                context=getattr(record,"history_context",None)
                self._remember_history_target("edit",(record.image_path,),context.operation_id if context is not None else "")
                self.inspect_image(str(record.image_path),record)
        if result==REQUEST_SOURCE_CORRECTION:
            self._open_source_correction_scope(dialog.source_correction_scope)
        return result

    def edit_gallery_image(self,value: str):
        self._batch_conflict_context={}
        path=Path(value)
        current=getattr(self,"current_record",None)
        if (
            current is not None
            and Path(current.image_path)==path
        ):
            return self._queue_data_editor_load(
                path,record=current,report=self.current_report,
            )
        return self._queue_data_editor_load(path)

    def _editor_view_token(self):
        return (
            id(self.dataset),
            str(getattr(self,"_readiness_context_fingerprint","") or ""),
            DatasetPage.current_stage(self),
            str(self.state_filter.currentData() or "all"),
            str(self.projection_filter.currentData() or "all"),
            int(self.workflow_tabs.currentIndex()),
            bool(self.isVisible()),
        )

    def _queue_data_editor_load(
        self,path,*,record=None,report=None,queue: bool=False,
    ):
        path=Path(path)
        availability=DatasetPage._storage_availability(self,path)
        if availability is not None and not bool(getattr(availability,"ready_for_write",True)):
            QMessageBox.information(
                self,str(getattr(availability,"label","Image unavailable")),
                "\n\n".join(value for value in (
                    str(getattr(availability,"detail","") or ""),
                    str(getattr(availability,"recovery","") or ""),
                ) if value),
            )
            return
        try:
            history_operation=capture_operation(self,(path,),operation_kind="dataset_image_edit",reason="Edit saved Dataset Image Knowledge")
        except Exception as exc:
            self._history_admission_failed(exc); return
        self._invalidate_editor_load()
        generation=self._editor_load_generation
        queue_index=self._editor_queue_index if queue else -1
        view_token=self._editor_view_token()
        if not queue:
            self.current_path=path
            for card_path,card in self.cards.items():
                card.set_active_selected(card_path==str(path))
        self.header.set_status(
            f"Opening {'review' if queue else 'image data'} for {path.name} in the background…"
        )
        def load():
            context=history_operation.context_for(path) if history_operation is not None else None
            loaded_record=record if record is not None else ImageRecord(path,history_context=context)
            loaded_record.history_context=context  # Editor owns explicit pending recovery.
            loaded_report=report
            if loaded_report is None:
                try:
                    loaded_report=ImageEvidenceInspector(
                        knowledge=self.state.knowledge,
                    ).inspect(path)
                except Exception:
                    loaded_report=None
            preview_image=QImage(str(path))
            prepared_context=prepare_image_data_editor_context(
                self.state.knowledge,self.characters,
            )
            return (
                generation,path,queue,queue_index,view_token,loaded_record,
                loaded_report,preview_image,prepared_context,
            )
        task=AsyncCallTask(load); self._editor_load_task=task
        task.signals.complete.connect(self._gallery_editor_loaded)
        task.signals.failed.connect(
            lambda message,g=generation,p=path,q=queue,i=queue_index:
            self._gallery_editor_failed(g,p,message,queue=q,queue_index=i)
        )
        self.pool.start(task,-1)

    def _editor_request_current(
        self,path: Path,*,queue: bool,queue_index: int,view_token,
    ) -> bool:
        if view_token[:-1]!=self._editor_view_token()[:-1]:
            return False
        if view_token[-1] and not self.isVisible():
            return False
        if queue:
            return bool(
                queue_index==self._editor_queue_index
                and 0<=queue_index<len(self._editor_queue)
                and Path(self._editor_queue[queue_index])==path
            )
        return self.current_path==path

    def _gallery_editor_loaded(self,result):
        generation,path,queue,queue_index,view_token=result[:5]
        if (generation!=self._editor_load_generation or not self._editor_request_current(
                path,queue=queue,queue_index=queue_index,view_token=view_token)):
            return
        self._editor_load_task=None; self.header.set_status("")
        defer_editor_delivery(self,DatasetPage._deliver_gallery_editor,result)

    def _deliver_gallery_editor(self,envelope):
        if not envelope:return
        generation,path,queue,queue_index,view_token,record,report,preview_image=envelope[:-1]
        if (getattr(self,"_shutdown_requested",False)
                or generation!=self._editor_load_generation or not self._editor_request_current(
                    path,queue=queue,queue_index=queue_index,view_token=view_token)):
            envelope.clear();return
        context_owner=[envelope.pop()]
        envelope.clear()
        if queue:
            self._run_editor_queue_dialog(path,record,report,preview_image,_context_envelope=context_owner)
        else:
            self._run_data_editor_dialog(record,report,preview_image,_context_envelope=context_owner)

    def _gallery_editor_failed(
        self,generation,path,message,*,queue: bool=False,queue_index: int=-1,
    ):
        if generation!=self._editor_load_generation: return
        self._editor_load_task=None; self.header.set_status("Image data could not be opened")
        QMessageBox.warning(
            self,"Could not review image" if queue else "Could not open image data",
            f"{path.name}\n\n{message}",
        )
        if (
            queue and queue_index==self._editor_queue_index
            and 0<=queue_index<len(self._editor_queue)
        ):
            self._advance_editor_queue(1)

    @staticmethod
    def _search_exception_label(key: str) -> str:
        values={part.split("=",1)[0]:part.split("=",1)[1] for part in key.split("|") if "=" in part}
        target=values.get("target") or "unknown SDXL Tag"; mode=values.get("mode") or "missing"
        details=[]
        if values.get("required"): details.append("requires "+values["required"])
        if values.get("absent"): details.append("excludes "+values["absent"])
        return f'{mode.title()} “{target}”'+(f" ({'; '.join(details)})" if details else "")

    def manage_exceptions(self):
        if self.current_record is None or not self.dataset:
            QMessageBox.information(self,"Select an image","Select an image from the gallery first."); return
        if not DatasetPage._storage_scope_ready(self,
            (self.current_record.image_path,),write=True,action="edit",
        ):
            return
        if not self._exception_storage_ready():
            return
        record=self.current_record; entries=[]
        root,library_id=self._library_context_for(record.image_path)
        if root is None:
            QMessageBox.information(self,"Library unavailable","The source library for this image is not currently available."); return
        for tag in record.annotation.source.get("rejected_tags",[]):
            entries.append(("rejected_tag",str(tag),f'Previously marked as not needing SDXL Tag “{tag}”'))
        rule_labels={
            rule.id:(rule.label or ", ".join(self.conflicts.effective_tags(rule)))
            for rule in self.conflicts.rules
        }
        for rule_id in record.annotation.source.get("accepted_conflict_rules",[]):
            entries.append(("conflict_rule",str(rule_id),rule_labels.get(str(rule_id),str(rule_id))))
        for key in self.exceptions.searches_for_image(record.image_path,root,library_id):
            entries.append(("missing_search",key,self._search_exception_label(key)))
        if not entries:
            QMessageBox.information(self,"No remembered exceptions","CollectiGen has no SDXL Tag or conflict exceptions saved for this image."); return
        dialog=_ExceptionDialog(entries,self)
        if dialog.exec()!=QDialog.Accepted: return
        selected=dialog.selected()
        if not selected:
            QMessageBox.information(self,"Nothing selected","No remembered decisions were changed."); return
        if not self._exception_storage_ready():
            return
        path=record.image_path
        operation=None
        image_decisions=[(kind,key) for kind,key in selected if kind in {"rejected_tag","conflict_rule"}]
        search_keys=[key for kind,key in selected if kind=="missing_search"]
        try:
            if image_decisions:
                operation=capture_operation(self,(path,),operation_kind="dataset_image_exceptions",reason="Forget selected image-local decisions")
                with self._history_record(path,operation) as record:
                    accepted_rules=TagCollection(record.annotation.source.get("accepted_conflict_rules",[]))
                    for kind,key in image_decisions:
                        if kind=="rejected_tag":
                            ImageFindingDecisionService(
                                record.annotation,knowledge=self.state.knowledge,
                            ).restore_image_ignores(tags=(key,))
                        elif kind=="conflict_rule": accepted_rules.remove(key)
                    record.annotation.source["accepted_conflict_rules"]=accepted_rules.to_list()
                    record.modified=True; record.save_sidecar(force=True)
                    record.export_caption(character_db=self.characters,knowledge=self.state.knowledge)
        except Exception as exc:
            if isinstance(exc,HistoryWriteInterrupted) and operation is not None:
                self._remember_history_target("edit",(path,),operation.operation_id)
            self._history_admission_failed(exc); return
        if image_decisions:
            self._remember_history_target("edit",(path,),operation.operation_id if operation is not None else "")
        if search_keys:
            try:
                self.exceptions.forget_image(record.image_path,root,search_keys,library_id)
            except Exception as exc:
                saved="Image-local decisions were saved. " if image_decisions else ""
                QMessageBox.warning(self,"Search exceptions need attention",f"{saved}The separate hidden-search changes could not finish: {exc}")
                self._invalidate_inspection_cache(record.image_path)
                self.inspect_image(str(record.image_path),record)
                return
        self._invalidate_inspection_cache(record.image_path); self._refresh_cached_workflow_state(record.image_path)
        self.inspect_image(str(record.image_path),record)
        self.header.set_status(f"Forgot {len(selected):,} remembered decision(s) for this image")

    def review_selected_data(self):
        paths=self.selected_paths()
        if not paths:
            QMessageBox.information(self,"Select images","Check one or more image cards to review first."); return
        if not DatasetPage._storage_scope_ready(self,
            paths,write=True,action="review",
        ):
            return
        self._batch_conflict_context={}
        self._editor_queue=paths; self._editor_queue_index=0; self._show_editor_queue()

    def _review_preparation_paths(self,paths):
        current_by_key={
            str(Path(value).absolute()).casefold():Path(value)
            for value in tuple(getattr(self.dataset,"images",()) or ())
        }
        exact=list(dict.fromkeys(
            current_by_key[key]
            for value in paths
            if (key:=str(Path(value).absolute()).casefold()) in current_by_key
        ))
        if not exact:
            QMessageBox.information(
                self,"Prepare review scope changed",
                "None of the exact remaining images from this Prepare run are "
                "still available. Refresh Dataset before retrying.",
            )
            return
        if not DatasetPage._storage_scope_ready(
            self,exact,write=True,action="review prepared findings",
        ):
            return
        self._batch_conflict_context={}
        self._editor_queue=exact
        self._editor_queue_index=0
        self._preparation_review_scope=set(exact)
        self._show_editor_queue()

    def _synchronize_preparation_review_path(self,path: Path) -> bool:
        path=Path(path)
        scope=getattr(self,"_preparation_review_scope",set())
        if path not in scope:
            return False
        service=ImageWorkflowStateService(
            self.state.knowledge,self.characters,
            ignored_plain_language=self.settings.ignored_plain_language_sentences,
            categories=self.categories,
        )
        if service.has_review_findings(path,include_deferred=True):
            self.header.set_status(
                f"Prepare review • {len(scope):,} image(s) still unresolved"
            )
            return False
        scope.discard(path)
        self.header.set_status(
            f"Prepare review • {len(scope):,} image(s) still unresolved"
        )
        return True

    def _show_editor_queue(self):
        if not (0<=self._editor_queue_index<len(self._editor_queue)):
            self._editor_queue=[]; self._batch_conflict_context={}; return
        path=Path(self._editor_queue[self._editor_queue_index])
        self._queue_data_editor_load(path,queue=True)

    def _run_editor_queue_dialog(
        self,path,record,report,preview_image=None,prepared_context=None,*,_context_envelope=None,
    ):
        if _context_envelope is not None:prepared_context=_context_envelope.pop()
        dialog=ImageDataEditor(
            record,report,self.settings,self,allow_ai=False,knowledge=self.state.knowledge,
            characters=self.characters,
            queue_index=self._editor_queue_index,queue_count=len(self._editor_queue),
            batch_conflict_context=self._batch_conflict_context.get(str(path)),
            image_knowledge_only=str(path) in self._batch_conflict_context,
            content_safety=content_safety_policy(),
            source_correction_available=True,
            preview_image=preview_image,prepared_context=prepared_context,
        )
        prepared_context=None  # Release the construction snapshot before nested edits.
        if hasattr(dialog,"edit_tag_requested"):dialog.edit_tag_requested.connect(self.edit_tag_requested)
        if hasattr(dialog,"create_tag_requested"):dialog.create_tag_requested.connect(self.create_tag_requested)
        self._active_editor=dialog
        result=dialog.exec()
        if self._active_editor is dialog:self._active_editor=None
        if (
            result in {
                QDialog.Accepted,ImageDataEditor.SAVE_AND_NEXT,
                REQUEST_SOURCE_CORRECTION,
            }
            and dialog.persisted_changes
        ):
            context=getattr(record,"history_context",None)
            self._remember_history_target("edit",(path,),context.operation_id if context is not None else "")
            self._invalidate_inspection_cache(path)
            evidence_changed=False
            if self.dataset and self.dataset.index:
                self._cache_record(record,publish_content_safety=False)
                evidence_changed=True
            self._refresh_cached_workflow_state(path)
            self._publish_editor_image_data_changed(
                path,evidence_changed=evidence_changed,
            )
            if path==self.current_path: self.inspect_image(str(path),record)
        resolved_preparation_path=(
            dialog.persisted_changes
            and self._synchronize_preparation_review_path(path)
        )
        if result in {ImageDataEditor.SAVE_AND_NEXT,ImageDataEditor.REQUEST_SKIP_NEXT}:
            if result==ImageDataEditor.SAVE_AND_NEXT and resolved_preparation_path:
                self._editor_queue.pop(self._editor_queue_index)
                self._editor_queue_index-=1
            self._advance_editor_queue(1)
        elif result==ImageDataEditor.REQUEST_PREVIOUS:
            if resolved_preparation_path:
                self._editor_queue.pop(self._editor_queue_index)
            self._advance_editor_queue(-1)
        elif result==REQUEST_SOURCE_CORRECTION:
            self._editor_queue=[]; self._batch_conflict_context={}
            self._open_source_correction_scope(dialog.source_correction_scope)
        else:
            self._editor_queue=[]; self._batch_conflict_context={}
        return result

    def add_tag_to_selected_images(self):
        self.add_tag_to_images(self.selected_paths())

    def prepare_preferred_descriptions(self):
        paths=tuple(dict.fromkeys(self.selected_paths()))
        if not paths:
            QMessageBox.information(
                self,"Select images","Select one or more available images first."
            )
            return
        if not DatasetPage._storage_scope_ready(self,
            paths,write=True,action="prepare",
        ):
            return

        def identity(_record):
            return configured_plain_language_backend(
                self.settings,
            ).evidence_identity()

        service=PreferredCaptionPreparationService(
            OperationJournal(USER_DATA_DIR/"operation_journal.jsonl"),
            identity,
            ignored_plain_language=self.settings.ignored_plain_language_sentences,
        )
        dialog=PreferredCaptionPreparationDialog(
            service,paths,pool=self.pool,parent=self,
            history_operation_factory=lambda images:capture_operation(self,images,operation_kind="preferred_caption_preparation",reason="Prepare preferred Dataset Descriptions"),
            history_opener=lambda images,operation_id:self.open_saved_history(paths=images,operation_id=operation_id),
        )
        dialog.exec()
        changed=dialog.changed_paths()
        if changed:
            for path in changed:
                self._invalidate_inspection_cache(path)
                self._refresh_cached_workflow_state(path)
            self.render_page()
            if self.current_path in changed:
                self.inspect_image(str(self.current_path))

    def add_tag_to_images(self,paths):
        paths=tuple(dict.fromkeys(Path(value) for value in paths))
        if not paths:
            QMessageBox.information(
                self,"Select images","Select one or more available images first."
            )
            return
        if not getattr(self,"_conflicts_ready",True):
            QMessageBox.information(
                self,"Conflict checks are still preparing",
                self._conflict_error or
                "Wait for the current System Knowledge conflict check to finish, then try again.",
            )
            return
        if not DatasetPage._storage_scope_ready(self,
            paths,write=True,action="add an SDXL Tag",
        ):
            return
        picker=TagReferencePickerDialog(
            self.state.knowledge,self.categories,
            title=f"Add SDXL Tag to {len(paths):,} Image(s)",
            allow_identity_creation=True,
            identity_source="selected_image_batch_confirmation",
            parent=self,
        )
        if picker.exec()!=QDialog.Accepted:
            return
        tag_id=picker.selected_tag_id()
        if not tag_id:
            return
        service=ImageTagBatchService(
            self.state.knowledge,self.conflicts,
            OperationJournal(USER_DATA_DIR/"operation_journal.jsonl"),
            categories=self.categories,characters=self.characters,
        )
        dialog=ImageTagBatchDialog(
            service,paths,tag_id,pool=self.pool,parent=self,
            history_operation_factory=lambda images:capture_operation(self,images,operation_kind="image_tag_batch",reason="Add selected Dataset Tags"),
            history_opener=lambda images,operation_id:self.open_saved_history(paths=images,operation_id=operation_id),
        )
        dialog.exec()
        changed=dialog.changed_paths()
        if changed:
            for path in changed:
                self._invalidate_inspection_cache(path)
                self._refresh_cached_workflow_state(path)
            self.render_page()
            if self.current_path in changed:
                self.inspect_image(str(self.current_path))
        if dialog.review_requested:
            context=dialog.conflict_review_context()
            review_paths=[Path(value) for value in context]
            if review_paths:
                self._batch_conflict_context=context
                self._editor_queue=review_paths
                self._editor_queue_index=0
                self._show_editor_queue()

    def _advance_editor_queue(self,direction: int):
        self._editor_queue_index+=1 if direction>=0 else -1
        if 0<=self._editor_queue_index<len(self._editor_queue):
            QTimer.singleShot(0,self._show_editor_queue)
        else:
            self._editor_queue=[]
            self._batch_conflict_context={}

    def prepare_images(self,*,options=None,paths=None,editor=None,fingerprint_retry=None):
        if paths is None and editor is None:
            paths=self.preparation_controls.take_submitted_paths()
        feedback_parent=editor or self

        def fail_editor(message: str=""):
            if editor is not None:
                unavailable=getattr(editor,"preparation_unavailable",None)
                if message and callable(unavailable):
                    unavailable(message)
                else:
                    editor.ai_failed()

        if self._preparation_task is not None and not hasattr(getattr(self,"state",None),"preparation_requests"):
            fail_editor(
                "Preparation did not start because another Dataset preparation "
                "is still running. Wait for it to finish or cancel it, then try again."
            )
            return
        options=options or getattr(self.preparation_controls,"take_submitted_options",self.preparation_controls.options)()
        explicit_paths=paths is not None
        paths=tuple(dict.fromkeys(Path(value) for value in (
            paths if explicit_paths else self.preparation_paths()
        )))
        selected=paths if explicit_paths else self.selected_paths()
        if selected and not DatasetPage._storage_scope_ready(self,
            selected,write=True,action="prepare",feedback_parent=feedback_parent,
        ):
            fail_editor()
            return
        if not paths:
            QMessageBox.information(
                feedback_parent,"Choose images to prepare",
                "Select images, or open the Unprepared or Partially Prepared "
                "stage to prepare that result.",
            )
            fail_editor()
            return
        fingerprint_controls=(getattr(editor,"preparation_controls",None)
                              if editor is not None else self.preparation_controls)
        take_fingerprints=getattr(fingerprint_controls,"take_submitted_fingerprints",None)
        fingerprint_choice=take_fingerprints() if callable(take_fingerprints) else None
        create_fingerprints=(bool(fingerprint_retry) if fingerprint_retry is not None else (
            bool(getattr(self.settings,"ingest_create_duplicate_fingerprints",True))
            if fingerprint_choice is None else bool(fingerprint_choice)
        ))
        fingerprint_registry=self.settings.library_registry() if create_fingerprints else None
        has_preparation_steps=any((options.read_external_evidence,options.suggest_sdxl_tags,
                    options.suggest_plain_language,options.has_automated_application))
        if not has_preparation_steps and not create_fingerprints:
            QMessageBox.information(feedback_parent,"Choose preparation steps","Choose at least one preparation option first.")
            fail_editor()
            return
        if editor is not None and hasattr(editor,"memory_hold"):
            import copy
            editor.memory_hold.clear()
            editor._memory_preparation_request=(options,paths,create_fingerprints,copy.deepcopy(vars(self.settings)))
        try:
            history_operation=capture_operation(self,paths,operation_kind="unified_image_preparation",reason="Prepare selected Dataset Image Knowledge") if has_preparation_steps else None
        except Exception as exc:
            self._history_admission_failed(exc,feedback_parent); fail_editor(str(exc)); return
        if options.read_external_evidence and not ensure_capability(
            feedback_parent,self.settings,"metadata","Reading TXT and Metadata",allow_without=True,
        ):
            fail_editor(); return
        def ai_capability_ready(capability,task_name):
            ready=ensure_capability(feedback_parent,self.settings,capability,task_name)
            window=self.window()
            if (not ready and editor is None and window is not None
                    and getattr(window,"navigation",None) is not None
                    and window.navigation.currentRow()==6):
                self.preparation_controls.remember_setup_return(
                    options,paths,fingerprint_choice,
                )
                offer=getattr(window,"offer_preparation_setup_return",None)
                if callable(offer):offer(self)
            return ready

        if options.suggest_sdxl_tags and not ai_capability_ready(
            "wd","Generating AI Tags during preparation",
        ):
            fail_editor(); return
        if options.suggest_plain_language and not ai_capability_ready(
            "joy","Generating AI Descriptions during preparation",
        ):
            fail_editor(); return
        if hasattr(self.state,"preparation_requests"):
            return self._queue_preparation_request(options,paths,editor,
                history_operation,create_fingerprints)
        taggers=configured_wd_backends(self.settings) if options.suggest_sdxl_tags else []
        captioner=(configured_plain_language_backend(self.settings) if (
            options.suggest_plain_language
            or options.prepares_preferred_descriptions
        ) else None)
        preferred=None
        journal=OperationJournal(USER_DATA_DIR/"operation_journal.jsonl")
        if options.prepares_preferred_descriptions:
            def identity(_record):
                return captioner.evidence_identity()

            preferred=PreferredCaptionPreparationService(
                journal,identity,
                ignored_plain_language=self.settings.ignored_plain_language_sentences,
            )
        service=UnifiedPreparationService(
            self.state.knowledge,self.state.ai,
            preferred_caption_service=preferred,
            journal=journal,
        )
        self._unified_preparation_service=service
        self._unified_preparation_paths=list(paths)
        self._unified_preparation_operation_id=""
        self.preparation_controls.set_undo_available(False)
        resource="gpu" if (
            options.suggest_sdxl_tags or options.suggest_plain_language
        ) else "io"
        started,active=self.state.begin_long_operation(
            "dataset",f"Preparing {len(paths):,} image(s)",resource=resource,
        )
        if not started:
            if editor is None:
                show_operation_in_progress(self,active)
            fail_editor(
                "Preparation did not start because CollectiGen is already working: "
                f"{str(active or 'another background task').strip().rstrip('.')}. "
                "Wait for it to finish or cancel it, then try again."
            )
            return
        self.header.set_status(f"Starting preparation for {len(paths):,} image(s)…")
        self._preparation_task=True
        self._preparation_editor=editor
        self._preparation_paths=list(paths)
        if editor is None:
            self.preparation_controls.remember_setup_return(
                options,paths,fingerprint_choice,
            )
        self._remember_history_target("preparation",paths,history_operation.operation_id if history_operation is not None else "")
        self.update_selection_controls()

        def work(progress,should_cancel):
            fingerprint_lines=[]; fingerprint_errors=[]
            if create_fingerprints:
                from core.duplicate_preflight import warm_dataset_duplicate_fingerprints
                warmup=warm_dataset_duplicate_fingerprints(
                    self.state.duplicates,paths,fingerprint_registry,
                    progress=progress,should_cancel=should_cancel,
                )
                if warmup.cancelled or should_cancel():
                    return {"processed":0,"cancelled":True,"skip_followup_state_scan":True,"summary_lines":[
                        "Fingerprint creation paused safely. Completed cache entries remain reusable; no preparation changes started."
                    ]}
                fingerprint_lines.append(
                    f"Duplicate fingerprints: {warmup.indexed:,} new or refreshed, {warmup.reused:,} valid entries reused, {warmup.issue_count:,} held."
                )
                fingerprint_errors=[f"{issue.path.name}: {issue.detail}" for issue in warmup.issues]
            if has_preparation_steps:
                result=service.run(
                    paths,options,tag_backends=taggers,description_backend=captioner,
                    minimum_tag_agreement=self.settings.wd_consensus_minimum,
                    progress=progress,should_cancel=should_cancel,
                    **({"history_operation":history_operation} if history_operation is not None else {}),
                ).as_page_result()
            else:
                result={"processed":len(paths),"summary_lines":[],"errors":[],"skip_followup_state_scan":True}
            result["summary_lines"]=fingerprint_lines+list(result.get("summary_lines",()))
            result["errors"]=fingerprint_errors+list(result.get("errors",()))
            return result

        task=CancelableProgressCallTask(work)
        self._preparation_task=task
        self.cancel_projection_button.setText("Cancel Safely")
        self.cancel_projection_button.setEnabled(True)
        self.cancel_projection_button.show()
        task.signals.progress.connect(self.preparation_progress)
        task.signals.complete.connect(self.preparation_complete)
        task.signals.cancelled.connect(self.preparation_cancelled)
        task.signals.failed.connect(self.preparation_failed)
        self.pool.start(task)

    def _queue_preparation_request(self,options,paths,editor,history_operation,create_fingerprints):
        from core.dataset_preparation_request import DatasetPreparationRequestWork
        from .history_workflow import history_configuration
        from .dataset_preparation_queue import DatasetPreparationQueue
        state=self.state
        try:
            work=DatasetPreparationRequestWork(state,self.settings,paths,options,
                history_operation=history_operation,history_configuration=history_configuration(self),
                authority_token=lambda:_dataset_preparation_token(state),
                journal_path=USER_DATA_DIR/"operation_journal.jsonl",
                create_fingerprints=create_fingerprints,
                service_factory=UnifiedPreparationService,tagger_factory=configured_wd_backends,
                captioner_factory=configured_plain_language_backend,
                preferred_factory=PreferredCaptionPreparationService,journal_factory=OperationJournal)
            queue=getattr(self,"_preparation_request_queue",None)
            if queue is None:
                queue=DatasetPreparationQueue(self);self._preparation_request_queue=queue
                self.header.actions.addWidget(queue.button)
            request_id=queue.submit(work,editor)
            return request_id
        except Exception as exc:
            if editor is not None:editor.preparation_unavailable(str(exc))
            else:QMessageBox.warning(self,"Preparation could not be queued",str(exc))
            return None

    def preparation_progress(self,index,total,name):
        self.state.update_long_operation("dataset",index,total,name)
        editor=getattr(self,"_preparation_editor",None)
        update_editor=getattr(editor,"set_preparation_progress",None)
        if callable(update_editor):
            update_editor(index,total,name)
        self.header.set_status(
            f"Preparing image data: work step {index:,} of {total:,} • {name}"
        )

    def _refresh_preparation_paths(self):
        paths=tuple(dict.fromkeys(Path(value) for value in self._preparation_paths))
        self._preparation_paths=[]
        for path in paths:
            self._readiness_cache.pop(path,None)
            self._invalidate_inspection_cache(path)
        if paths:
            self.render_page()
            events=getattr(self.state,"events",None)
            if events is not None:
                events.publish(
                    "image_data_changed",images=tuple(str(path) for path in paths),
                    source="preparation",
                )
        return paths

    def preparation_complete(self,result):
        self.state.finish_long_operation("dataset")
        self._preparation_task=None
        self.cancel_projection_button.hide()
        if result.get("skip_followup_state_scan"):
            self._preparation_paths=[]
            preparation_editor=self._finish_preparation_editor(refresh=False)
        else:
            self._refresh_preparation_paths()
            preparation_editor=self._finish_preparation_editor()
        feedback_parent=preparation_editor or self
        self.update_selection_controls()
        if self._shutdown_requested:
            self._notify_background_work_finished()
            return
        summary=list(result.get("summary_lines",[]))
        errors=list(result.get("errors",[]))
        runtime_recovery=next((
            recovery for error in errors
            if (recovery:=ai_tag_runtime_recovery(error)) is not None
        ),None)
        if runtime_recovery is None:
            self.preparation_controls.discard_setup_return()
        if result.get("preparation_undo_complete"):
            self._unified_preparation_operation_id=""
            self.preparation_controls.set_undo_available(False)
        elif (result.get("preparation_operation_id")
              and int(result.get("preparation_changes_recorded",0))>0):
            self._unified_preparation_operation_id=str(
                result["preparation_operation_id"]
            )
            self.preparation_controls.set_undo_available(True)
        if result.get("history_operation_id"):
            self._remember_history_target("preparation",
                tuple(Path(item["image"]) for item in result.get("history_coverage",())) or tuple(self._unified_preparation_paths),
                result["history_operation_id"])
        self.header.set_status(
            "AI Tags could not start; repair the shared runtime in Setup"
            if runtime_recovery else
            " • ".join(summary[:3]) if summary else "Image preparation complete"
        )
        editor_memory_held=(DatasetPage._show_editor_memory_hold(self,preparation_editor,errors,result=result)
                            if preparation_editor is not None and not result.get("cancelled") else False)
        if errors and runtime_recovery is None and not editor_memory_held:
            QMessageBox.warning(
                feedback_parent,
                "Some images need attention",
                f"{len(errors):,} preparation problem(s) were recorded. Successful work was kept.\n\n"
                +"\n".join(errors[:8]),
            )
        if should_show_preparation_results(result) and not editor_memory_held:
            outcome_dialog=PreparationResultDialog(result,parent=feedback_parent)
            outcome_dialog.exec()
            if outcome_dialog.review_requested:
                self._review_preparation_paths(outcome_dialog.review_paths)
        if runtime_recovery:
            open_setup=getattr(self.window(),"open_preparation_ai_tag_runtime_setup",None)
            show_ai_tag_runtime_recovery(
                feedback_parent,runtime_recovery,
                (lambda:open_setup(self)) if preparation_editor is None
                and callable(open_setup) else None,
            )
        self._notify_background_work_finished()

    def undo_preparation(self):
        if getattr(self,"_preparation_task",None) is None:
            return self._review_history_target("preparation")

    def preparation_cancelled(self):
        self.preparation_controls.discard_setup_return()
        self.state.finish_long_operation("dataset")
        self._preparation_task=None
        self.cancel_projection_button.hide()
        self._refresh_preparation_paths()
        self._finish_preparation_editor()
        self.update_selection_controls()
        self.header.set_status(
            "Preparation cancelled safely; completed image changes were kept."
        )
        self._notify_background_work_finished()

    def preparation_failed(self,message: str):
        runtime_recovery=ai_tag_runtime_recovery(message)
        if runtime_recovery is None:
            self.preparation_controls.discard_setup_return()
        self.state.finish_long_operation("dataset")
        self._preparation_task=None
        self.cancel_projection_button.hide()
        self._refresh_preparation_paths()
        preparation_editor=self._finish_preparation_editor()
        self.update_selection_controls()
        self.header.set_status(
            "AI Tags could not start; repair the shared runtime in Setup"
            if runtime_recovery else "Image preparation stopped before completion"
        )
        if not getattr(self,"_shutdown_requested",False):
            if runtime_recovery:
                open_setup=getattr(self.window(),"open_preparation_ai_tag_runtime_setup",None)
                show_ai_tag_runtime_recovery(
                    preparation_editor or self,runtime_recovery,
                    (lambda:open_setup(self)) if preparation_editor is None
                    and callable(open_setup) else None,
                )
            elif preparation_editor is not None and DatasetPage._show_editor_memory_hold(self,preparation_editor,[message]):
                pass
            else:
                QMessageBox.warning(
                    preparation_editor or self,"Preparation could not finish",message,
                )
        self._notify_background_work_finished()

    def choose_batch_ai(self):
        paths=self.selected_paths()
        if not paths:
            QMessageBox.information(
                self,"Select images",
                "Check one or more image cards before running AI.",
            )
            return
        self._run_ai_dialog(paths,scope="selection")

    def force_ai_from_gallery(self,paths,kind: str):
        paths=list(paths)
        self._run_ai_dialog(
            paths,preselect=kind,scope="active" if len(paths)==1 else "selection",
            active_image=paths[0] if len(paths)==1 else None,
        )

    def run_batch_ai(self,kind: str,paths,*,force_override: bool=False):
        self._run_ai_dialog(
            paths,preselect=kind,force=force_override,scope="selection",
        )

    def _refresh_ai_batch_paths(self):
        """Invalidate only the images touched by the completed AI batch."""
        paths=tuple(dict.fromkeys(Path(value) for value in self._ai_batch_paths))
        self._ai_batch_paths=[]
        for path in paths:
            self._readiness_cache.pop(path,None)
            self._invalidate_inspection_cache(path)
        if paths:
            self.render_page()
            events=getattr(self.state,"events",None)
            if events is not None:
                events.publish(
                    "image_data_changed",
                    images=tuple(str(path) for path in paths),
                    source="batch_ai",
                )
        return paths

    def _show_editor_memory_hold(self,editor,errors,*,result=None):
        from .ai_runtime_controls import show_editor_preparation_memory_hold
        return show_editor_preparation_memory_hold(self,editor,errors,
            lambda options,paths,fingerprints:self.prepare_images(
                options=options,paths=paths,editor=editor,fingerprint_retry=fingerprints,
            ),result=result,
        )

    def _finish_preparation_editor(self,*,refresh=True):
        editor=self._preparation_editor
        self._preparation_editor=None
        if editor is None:
            return None
        if editor is self._active_editor and editor.isVisible():
            if refresh:
                editor.refresh_ai_evidence(ImageRecord(editor.record.image_path))
            else:
                editor.set_ai_busy(False)
            return editor
        else:
            editor.ai_failed()
        return None

    def run_image_preparation(self,options,editor=None):
        record=editor.record if editor is not None else self.current_record
        if record is None:
            if editor is not None:
                editor.ai_failed()
            return
        self.prepare_images(
            options=options,paths=(record.image_path,),editor=editor,
        )

    def run_image_ai(self,kind: str,editor=None):
        record=editor.record if editor is not None else self.current_record
        if record is None: return
        self._run_ai_dialog(
            [record.image_path],preselect=kind,scope="active",
            active_image=record.image_path,editor=editor,
        )

    def _run_ai_dialog(
        self,paths,*,preselect: str="",force: bool=False,scope: str="selection",
        active_image=None,editor=None,
    ):
        if getattr(self,"_preparation_task",None) is not None:
            if editor is not None:
                editor.ai_failed()
            return
        paths=tuple(dict.fromkeys(Path(value) for value in paths))
        if not paths:
            if editor is not None:
                editor.ai_failed()
            return
        if not DatasetPage._storage_scope_ready(self,
            paths,write=True,action="run AI on",
        ):
            if editor is not None:
                editor.ai_failed()
            return
        def retry_scope_ready(images,feedback_parent):
            if (getattr(self,"_preparation_task",None) is not None
                    or getattr(self,"_shutdown_requested",False)):
                show_operation_in_progress(feedback_parent,"Dataset preparation")
                return False
            missing=[image for image in images if not Path(image).is_file()]
            if missing:
                QMessageBox.information(
                    feedback_parent,"Selected-image scope changed",
                    f"{len(missing):,} selected image(s) are no longer available in the current "
                    "Dataset. No AI run started. Refresh the library and review the exact images again.",
                )
                return False
            return DatasetPage._storage_scope_ready(
                self,images,write=True,action="run AI on",feedback_parent=feedback_parent,
            )
        dialog=RunAIDialog(
            self.state,self.settings,paths,owner="dataset",scope=scope,
            active_image=active_image,pool=self.pool,parent=editor or self,
            preselect=preselect,force=force,
            history_operation_factory=lambda images:capture_operation(self,images,operation_kind="dataset_ai",reason="Run AI on selected Dataset images"),
            history_opener=lambda images,operation_id:self.open_saved_history(paths=images,operation_id=operation_id),
            scope_admission=retry_scope_ready,
        )
        refreshed={"done":False}

        def refresh_changed(changed_images):
            self._ai_batch_paths=list(changed_images)
            self._refresh_ai_batch_paths()
            refreshed["done"]=True

        dialog.images_changed.connect(refresh_changed)
        dialog.exec()
        result=getattr(dialog,"latest_retry_result",None) or dialog.result_data
        if result is not None and not refreshed["done"]:
            refresh_changed(paths)
        if result is not None:
            counts=result.outcome_counts()
            from .ai_runtime_controls import ai_result_headline
            self.header.set_status(
                f"{ai_result_headline(result)} • "
                +f"{counts['completed']:,} completed • "
                f"{counts['partial']:,} partial • {counts['skipped']:,} current • "
                f"{counts['conflicted']:,} conflicted • {counts['failed']:,} failed"
            )
        if editor is not None and editor is self._active_editor and editor.isVisible():
            editor.refresh_ai_evidence(ImageRecord(paths[0]))
        elif self.current_record is not None and self.current_record.image_path in paths:
            self.inspect_image(
                str(self.current_record.image_path),ImageRecord(self.current_record.image_path),
            )
        if (
            result is not None and dialog.review_requested and editor is None
            and result.review_paths
        ):
            self._editor_queue=list(getattr(dialog,"review_paths_requested",()) or result.review_paths)
            self._editor_queue_index=0
            self._show_editor_queue()
        elif editor is not None and result is None:
            editor.ai_failed()

    def undo_edit(self):
        return self._review_history_target("edit")

    def move_selected_to_library(self,*,paths=None):
        if getattr(self,"_library_move_task",None) is not None or history_dialogs_busy(self):
            show_operation_in_progress(self,"Named-library move")
            return
        paths=self.selected_paths() if paths is None else list(paths)
        if not paths:
            QMessageBox.information(
                self,"Select images","Check one or more Dataset images to move first.",
            )
            return
        held=[
            (path,DatasetPage._storage_availability(self,path))
            for path in paths if not DatasetPage._storage_writable(self,path)
        ]
        if held:
            examples="\n".join(
                f"• {path.name}: {getattr(availability,'label','Unavailable')}"
                for path,availability in held[:6]
            )
            QMessageBox.information(
                self,"Complete selection is not ready to move",
                f"{len(held):,} of {len(paths):,} selected image(s) are online-only, conflicted, read-only, or unavailable. Nothing was moved.\n\n"
                +examples
                +"\n\nRestore local access or resolve the conflict, refresh the library, then select the complete group again.",
            )
            return
        if not self.dataset or not DatasetPage._has_library_roots(self.dataset):
            QMessageBox.information(
                self,"Open libraries","Open the source libraries before moving images.",
            )
            return
        registry=self.settings.library_registry()
        valid_profiles=[
            profile for profile in registry.profiles
            if not registry.validate(profile,replacing_id=profile.stable_id)
        ]
        if not valid_profiles:
            QMessageBox.information(
                self,"Configure a Named Library",
                "Add or correct a named image library in Setup > Library & Storage > Locations first.",
            )
            return
        destination_dialog=LibraryDestinationDialog(
            registry,self,active_library_id=self.settings.active_library_id,
            source_count=len(paths),
            naming_policy=self.settings.library_move_naming_policy,
        )
        if (
            destination_dialog.exec()!=QDialog.Accepted
            or destination_dialog.profile is None
        ):
            return
        destination_profile=destination_dialog.profile
        naming_policy=destination_dialog.naming_policy
        self._begin_library_move_preview(
            paths,destination_profile,naming_policy,
        )

    def _begin_library_move_preview(
        self,paths,destination_profile,naming_policy,
    ):
        registry=self.settings.library_registry()
        if history_dialogs_busy(self):
            show_operation_in_progress(self,"History restoration"); return
        self._library_move_planner=LibraryMovePlanningService(
            Path(destination_profile.root),self.characters,
            self.settings.organization_profile,knowledge=self.state.knowledge,
            ignored_plain_language=self.settings.ignored_plain_language_sentences,
            destination_profile=destination_profile,library_registry=registry,
            transfers=self.state.moves,
            naming_policy=naming_policy,
        )
        started,active=self.state.begin_long_operation(
            "dataset",f"Previewing named-library destinations for {len(paths):,} image(s)",
        )
        if not started:
            show_operation_in_progress(self,active)
            return
        planner=self._library_move_planner
        self.header.set_status(
            f"Previewing named-library destinations for {len(paths):,} image(s)…"
        )

        def work(progress,should_cancel):
            try:
                plan=planner.preview(
                    paths,include_metadata=False,progress=progress,
                    should_cancel=should_cancel,include_projection_preview=False,
                )
            except InterruptedError:
                return None,True
            return plan,False

        task=CancelableProgressCallTask(work)
        self._library_move_task=task
        self.cancel_projection_button.setText("Cancel Safely")
        self.cancel_projection_button.setEnabled(True)
        self.cancel_projection_button.show()
        self.update_selection_controls()
        task.signals.progress.connect(
            lambda index,total,name:self.state.update_long_operation(
                "dataset",index,total,name,
            )
        )
        task.signals.progress.connect(
            lambda index,total,name:self.header.set_status(
                f"Move preview: {index:,} of {total:,} • {name}"
            )
        )
        task.signals.complete.connect(self._library_move_preview_complete)
        task.signals.failed.connect(self._library_move_failed)
        self.pool.start(task)

    def _library_move_preview_complete(self,result):
        self.state.finish_long_operation("dataset")
        self._library_move_task=None
        self.cancel_projection_button.hide()
        self.update_selection_controls()
        plan,cancelled=result
        if getattr(self,"_shutdown_requested",False):
            self._notify_background_work_finished()
            return
        if cancelled or plan is None:
            self.header.set_status("Named-library move preview cancelled safely before writing")
            self._notify_background_work_finished()
            return
        if not plan.items:
            self.header.set_status("No selected Dataset images were available to preview")
            self._notify_background_work_finished()
            return
        source_profiles={
            item.source_profile.stable_id
            for item in plan.items if item.source_profile is not None
        }
        dialog=LibraryMovePreviewDialog(
            plan,self,source_context=(
                f"{len(source_profiles):,} selected source libraries"
                if len(source_profiles)>1 else "the selected source library"
            ),
            include_projection_options=False,
        )
        if dialog.exec()!=QDialog.Accepted or not dialog.action:
            self.header.set_status("Named-library move cancelled before writing")
            self._notify_background_work_finished()
            return
        if dialog.action=="review":
            self.batch_selected={str(item.image) for item in plan.pending}
            self.render_page()
            if plan.pending:
                self.inspect_image(str(plan.pending[0].image))
            self._notify_background_work_finished()
            return
        items=(
            plan.ready if dialog.action=="ready" else
            [item for item in plan.items if not item.errors and not item.exact_duplicate]
        )
        if not items:
            QMessageBox.information(
                self,"No images can move",
                "Review the listed image-data or destination problems, then preview the move again.",
            )
            self._notify_background_work_finished()
            return
        self._start_library_move_apply(
            items,plan,boundary_confirmed=dialog.boundary_confirmed(),
            external_policy_fingerprint="",
        )

    def _start_library_move_apply(
        self,items,plan,*,boundary_confirmed: bool,
        external_policy_fingerprint: str="",
    ):
        if history_dialogs_busy(self):
            show_operation_in_progress(self,"History restoration"); return
        started,active=self.state.begin_long_operation(
            "dataset",f"Moving {len(items):,} image family(s) to a named library",
        )
        if not started:
            show_operation_in_progress(self,active)
            self._notify_background_work_finished()
            return
        self.header.set_status(f"Moving {len(items):,} verified image family(s)…")

        def work(progress,should_cancel):
            return self._execute_library_move_items(
                items,plan,boundary_confirmed,progress,should_cancel,
                external_policy_fingerprint,
            )

        task=CancelableProgressCallTask(work)
        self._library_move_task=task
        self.cancel_projection_button.setText("Cancel Safely")
        self.cancel_projection_button.setEnabled(True)
        self.cancel_projection_button.show()
        self.update_selection_controls()
        task.signals.progress.connect(
            lambda index,total,name:self.state.update_long_operation(
                "dataset",index,total,name,
            )
        )
        task.signals.progress.connect(
            lambda index,total,name:self.header.set_status(
                f"Named-library move: {index:,} of {total:,} • {name}"
            )
        )
        task.signals.complete.connect(self._library_move_apply_complete)
        task.signals.failed.connect(self._library_move_failed)
        self.pool.start(task)

    def _execute_library_move_items(
        self,items,plan,boundary_confirmed,progress,should_cancel,
        external_policy_fingerprint="",
    ):
        processed=skipped=fallback_moved=0
        errors=[]; moved=[]; cancelled=False; recovery_required=False
        group_id="dataset-move-"+uuid.uuid4().hex
        operation_ids=[]; changed_paths=[]; recovery_operation_id=""
        destination_profile=plan.destination_profile
        protection=plan.protection.subset(item.image for item in items) if plan.protection else None
        protection_session=None
        protection_service=None
        total=len(items); progress_offset=0
        try:
            for item in items:
                if (
                    self._library_move_planner is None
                    or not self._library_move_planner.is_current(item)
                ):
                    raise RuntimeError(
                        f"{item.image.name}: image data or its proposed destination "
                        "changed after preview; nothing was moved."
                    )
            if protection is not None:
                protection_service=(
                    getattr(self,"_library_move_protection_service",None)
                    or LibraryMoveProtectionService(
                        getattr(self.state.moves,"_engine",None),
                    )
                )
                protected_total=total*2 if protection.backup_enabled else total
                protection_session=protection_service.prepare(
                    protection,privacy_confirmed=boundary_confirmed,
                    external_policy_fingerprint=external_policy_fingerprint,
                    progress=(
                        lambda index,_count,name:progress(
                            index,protected_total,"Protecting "+name,
                        )
                        if protection.backup_enabled else None
                    ),
                    should_cancel=should_cancel,
                )
                if protection.backup_enabled:
                    total=protected_total; progress_offset=len(items)
        except InterruptedError:
            return {
                "processed":0,"skipped":0,"errors":[],"moved":[],
                "cancelled":True,"recovery_required":False,
                "fallback_moved":0,"protected":False,
            }
        except Exception as exc:
            return {
                "processed":0,"skipped":0,"errors":[str(exc)],"moved":[],
                "cancelled":False,"recovery_required":False,
                "fallback_moved":0,"protected":False,
            }
        for index,item in enumerate(items,1):
            path=item.image
            if should_cancel():
                cancelled=True
                break
            try:
                if (
                    self._library_move_planner is None
                    or not self._library_move_planner.is_current(item)
                ):
                    raise RuntimeError(
                        "Image data or its proposed destination changed after preview; preview again."
                    )
                if item.transfer is None:
                    skipped+=1
                else:
                    moved_record=self.state.moves.move(
                        path,item.destination,
                        reason="move selected Dataset image to named library after reviewed destination preview",
                        preview=item.transfer,collision_policy="error",
                        should_cancel=should_cancel,
                        boundary_decision=plan.boundary_decision,
                        boundary_confirmed=boundary_confirmed,
                        source_profile_ids=(
                            (item.source_profile.stable_id,)
                            if item.source_profile is not None else ()
                        ),
                        destination_profile_id=(
                            destination_profile.stable_id
                            if destination_profile is not None else ""
                        ),
                        protected_backup_manifest=(
                            protection_session.recovery_manifest
                            if protection_session is not None else None
                        ),
                        protected_scope_fingerprint=(
                            protection.scope_fingerprint if protection else ""
                        ),
                        external_policy_fingerprint=external_policy_fingerprint,
                        group_id=group_id,
                    )
                    operation_ids.append(moved_record.operation_id)
                    changed_paths.extend(moved_record.files[0][key] for key in ("from","to"))
                    moved.append(str(path))
                    processed+=1
                    fallback_moved+=not item.route_ready
            except InterruptedError:
                cancelled=True
                break
            except TransferRecoveryRequired as exc:
                errors.append(f"{path.name}: {exc}")
                recovery_required=True
                recovery_operation_id=getattr(exc,"operation_id","")
                break
            except Exception as exc:
                errors.append(f"{path.name}: {exc}")
            progress(progress_offset+index,total,path.name)
        if protection_session is not None:
            try:
                protection_service.finish(
                    protection_session,
                    status=(
                        "cancelled" if cancelled else "failed" if errors else "complete"
                    ),
                    result={
                        "moved":processed,"skipped":skipped,
                        "failed":len(errors),"cancelled":cancelled,
                    },
                )
            except Exception as exc:
                errors.append(
                    "Protected originals were created, but their completion record "
                    f"needs attention: {exc}"
                )
                recovery_required=True
        return {
            "processed":processed,"skipped":skipped,"errors":errors,
            "moved":moved,"cancelled":cancelled,
            "recovery_required":recovery_required,
            "fallback_moved":fallback_moved,
            "organization_group_id":group_id,"organization_operation_ids":tuple(operation_ids),
            "organization_paths":tuple(dict.fromkeys(changed_paths)),"recovery_operation_id":recovery_operation_id,
            "protected":bool(
                protection_session and protection_session.recovery_manifest
            ),
        }

    def _library_move_apply_complete(self,result):
        self.state.finish_long_operation("dataset")
        self._library_move_task=None
        self.cancel_projection_button.hide()
        moved={str(value) for value in result.get("moved",())}
        self.batch_selected.difference_update(moved)
        for value in moved:
            path=Path(value)
            self._readiness_cache.pop(path,None)
            self._invalidate_inspection_cache(path)
            self.audit_issues.pop(path,None)
        if self.current_path is not None and str(self.current_path) in moved:
            self.current_path=None
            self.current_record=None
            self.inspector.hide()
        DatasetPage._remember_organization_result(self,result)
        self.update_selection_controls()
        self.render_page()
        errors=list(result.get("errors",()))
        prefix="Paused safely • " if result.get("cancelled") else ""
        message=(
            prefix
            +f"Moved {int(result.get('processed',0)):,} image(s) • "
            +f"Already in place {int(result.get('skipped',0)):,} • "
            +f"Failed {len(errors):,}"
        )
        if result.get("fallback_moved"):
            message+=f" • Explicit fallback {int(result['fallback_moved']):,}"
        if result.get("protected"):
            message+=" • Originals protected"
        self.header.set_status(message)
        if result.get("organization_paths"):
            self.library_paths_changed.emit(tuple(Path(value) for value in result["organization_paths"]))
        if errors and not getattr(self,"_shutdown_requested",False):
            title=(
                "Move recovery needs attention"
                if result.get("recovery_required") else
                "Some images were not moved"
            )
            QMessageBox.warning(self,title,message+"\n\n"+"\n".join(errors[:8]))
        self._notify_background_work_finished()

    def _library_move_failed(self,message: str):
        self.state.finish_long_operation("dataset")
        self._library_move_task=None
        self.cancel_projection_button.hide()
        self.update_selection_controls()
        self.header.set_status("Named-library move stopped before completion")
        if not getattr(self,"_shutdown_requested",False):
            QMessageBox.warning(self,"Named-library move could not continue",message)
        self._notify_background_work_finished()

    def reorganize_current(self):
        if self.current_record is None or not self.dataset:
            QMessageBox.information(self,"Select an image","Select an image from the gallery first."); return
        if not DatasetPage._storage_scope_ready(self,
            (self.current_record.image_path,),write=True,action="reorganize",
        ):
            return
        source_root=self._library_root_for(self.current_record.image_path)
        if source_root is None:
            QMessageBox.information(self,"Library unavailable","The source library for this image is not currently available."); return
        registry=self.settings.library_registry()
        destination_profile=registry.profile_for_path(self.current_record.image_path)
        if destination_profile is None:
            QMessageBox.information(
                self,"Named library required",
                "Reorganize Image needs the source to belong to a named library. "
                "Review Library & Storage in Setup first.",
            ); return
        naming_policy=getattr(
            self.settings,"library_move_naming_policy",LIBRARY_NAMING_KEEP,
        )
        if naming_policy==LIBRARY_NAMING_KEEP:
            try:
                suggestion=OrganizationRouter(
                    source_root,self.characters,self.settings.organization_profile,
                ).suggest(self.current_record)
            except Exception as exc:
                QMessageBox.warning(
                    self,"Organization preview unavailable",str(exc),
                ); return
            if suggestion.destination.resolve()==self.current_record.image_path.parent.resolve():
                QMessageBox.information(
                    self,"Already organized",
                    "This image already matches the configured folder and filename rules:\n\n"
                    +str(suggestion.destination),
                ); return
        self._begin_library_move_preview(
            [self.current_record.image_path],destination_profile,
            naming_policy,
        )

    def reorganize_selected(self):
        self._reorganize_paths(self.selected_paths())

    def _reorganize_paths(self,paths):
        paths=tuple(dict.fromkeys(Path(path) for path in paths))
        if not paths:
            QMessageBox.information(
                self,"Select images","Check one or more Dataset images to reorganize first.",
            )
            return
        if not self.dataset:
            QMessageBox.information(
                self,"Open libraries","Open the source libraries before reorganizing images.",
            )
            return
        if not DatasetPage._storage_scope_ready(
            self,paths,write=True,action="reorganize",
        ):
            return
        registry=self.settings.library_registry()
        resolved=[registry.profile_for_path(path) for path in paths]
        profiles={
            profile.stable_id:profile for profile in resolved
            if profile is not None
        }
        if len(profiles)!=1 or any(profile is None for profile in resolved):
            QMessageBox.information(
                self,"Choose one named library",
                "Reorganization keeps each image inside its current named library. "
                "Filter to one library and select its images, then preview again.",
            )
            return
        self._begin_library_move_preview(
            paths,next(iter(profiles.values())),
            getattr(
                self.settings,"library_move_naming_policy",LIBRARY_NAMING_KEEP,
            ),
        )

    @staticmethod
    def _paths_overlap(first: Path,second: Path) -> bool:
        try:first.resolve().relative_to(second.resolve()); return True
        except ValueError:pass
        try:second.resolve().relative_to(first.resolve()); return True
        except ValueError:return False

    def _open_image_generation_setup(self,path):
        if path is None:
            return
        from .generation_workflow import open_generation_workspace
        open_generation_workspace(self,paths=(Path(path),),setup_from_image=True)

    def _gallery_file_action(self,paths,action_id: str):
        if getattr(self,"batch_mode",False) or any(
            getattr(self,name,None) is not None for name in (
                "_projection_task","_library_move_task","_preparation_task",
                "_batch_task","_organization_transfer_task","_training_export_task",
                "_training_candidate_task","_metadata_backup_task",
            )
        ):
            show_operation_in_progress(self,"Dataset task")
            return
        values=[Path(value) for value in paths]
        if action_id=="change_image" and len(values)==1:
            from .image_change_goal_dialog import open_image_change_goal
            open_image_change_goal(self, values[0])
            return
        if action_id=="remix" and values:
            from .generation_workflow import open_generation_workspace
            open_generation_workspace(self,paths=(values[0],),remix=True)
            return
        if action_id=="image_setup" and values:
            self._open_image_generation_setup(values[0])
            return
        if action_id=="animate_this_image" and values:
            from .video_generation_workflow import open_video_generation_workspace
            open_video_generation_workspace(self,source_path=values[0])
            return
        if action_id=="resize":
            from .resolution_dialog import open_resolution_workspace
            open_resolution_workspace(self,paths=tuple(values))
        elif action_id=="add_to_collection":self.add_selected_to_concept(paths=tuple(values))
        elif action_id=="reorganize":self._reorganize_paths(values)
        elif action_id=="library":self.move_selected_to_library(paths=values)
        elif action_id=="backup_metadata":self.sync_selected_data(values,preferred_targets=("metadata",))
        elif action_id=="export_training":self._export_training_captions(values)
        elif action_id=="recycle":self._begin_organization_transfer_preview(values,"recycle")
        elif action_id=="ingest":self._begin_organization_transfer_preview(values,"ingest")

    def move_selected_to_recycle(self):
        self._begin_organization_transfer_preview(self.selected_paths(),"recycle")

    def move_selected_to_ingest(self):
        self._begin_organization_transfer_preview(self.selected_paths(),"ingest")

    def move_current_to_recycle(self):
        if self.current_path is None:
            QMessageBox.information(self,"Select an image","Select an image from the gallery first."); return
        self._begin_organization_transfer_preview((self.current_path,),"recycle")

    def _begin_organization_transfer_preview(self,paths,kind: str):
        paths=tuple(dict.fromkeys(Path(value) for value in paths if value))
        if not paths:
            QMessageBox.information(self,"Select images","Select one or more Dataset images first."); return
        if self._organization_transfer_task is not None or history_dialogs_busy(self):return
        if not DatasetPage._storage_scope_ready(self,paths,write=True,action="move"):
            return
        try:
            if kind=="recycle":destination=self.state.recycle.validate_root()
            else:
                folder=getattr(self.settings,"problems_directory" if kind=="problems" else "ingest_directory","")
                if not str(folder or "").strip():raise ValueError("Choose an available destination folder in Setup first.")
                destination=Path(folder)
                if not destination.is_dir():raise ValueError("Choose an available destination folder in Setup first.")
                for path in paths:
                    root=self._library_root_for(path)
                    if root is not None and DatasetPage._paths_overlap(destination,root):
                        raise ValueError("The destination must be separate from every named image library; the folders cannot be equal or nested.")
        except Exception as error:
            QMessageBox.information(self,"Storage location needs attention",str(error)); return
        destination_name={"recycle":"Recycle Bin","ingest":"Ingest","problems":"Problems"}[kind]
        label=f"Previewing {len(paths):,} image family(s) for {destination_name}"
        started,active=self.state.begin_long_operation("dataset",label,resource="io")
        if not started:
            show_operation_in_progress(self,active); return
        registry=self.settings.library_registry()
        destination_profile=LibraryProfile.create(
            destination_name,
            destination,LibraryRole.LOCAL_ONLY,
            stable_id="collectigen-recycle-bin" if kind=="recycle" else "collectigen-"+kind,
        )
        def work(progress,should_cancel):
            items=[]; errors=[]
            for index,path in enumerate(paths,1):
                if should_cancel():raise InterruptedError("Move preview cancelled safely")
                root=self._library_root_for(path)
                profile=registry.profile_for_path(path)
                try:
                    if root is None:raise ValueError("The source named library is no longer available.")
                    boundary=(
                        LibraryPrivacyPolicy.movement(profile,destination_profile)
                        if profile is not None else None
                    )
                    force_copy=LibraryPrivacyPolicy.requires_verified_copy(
                        profile,destination_profile,
                    )
                    if kind=="recycle":
                        plan=self.state.recycle.preview_recycle(
                            path,source_root=root,
                            source_profile_id=profile.stable_id if profile is not None else "",
                            should_cancel=should_cancel,
                        )
                        transfer=plan.transfer
                    else:
                        transfer=self.state.moves.preview(
                            path,destination,relative_to=root,
                            collision_policy="error",force_copy=force_copy,
                            should_cancel=should_cancel,
                        )
                        plan=transfer
                    if not transfer.ready:
                        errors.extend(f"{path.name}: {value}" for value in transfer.errors)
                    items.append({
                        "path":path,"root":root,"profile":profile,
                        "plan":plan,"transfer":transfer,"boundary":boundary,
                        "force_copy":force_copy,
                    })
                except InterruptedError:raise
                except Exception as error:errors.append(f"{path.name}: {error}")
                progress(index,len(paths),path.name)
            return {"kind":kind,"destination":destination,"items":items,"errors":errors}
        task=CancelableProgressCallTask(work); self._organization_transfer_task=task
        self._organization_transfer_context=(kind,paths)
        self.cancel_projection_button.setText("Cancel Safely"); self.cancel_projection_button.setEnabled(True); self.cancel_projection_button.show()
        self.update_selection_controls(); self.header.set_status(label+"…")
        task.signals.progress.connect(lambda index,total,name:self.state.update_long_operation("dataset",index,total,name))
        task.signals.complete.connect(self._organization_preview_complete)
        task.signals.cancelled.connect(self._organization_transfer_cancelled)
        task.signals.failed.connect(self._organization_transfer_failed)
        self.pool.start(task)

    def _organization_preview_complete(self,result):
        self.state.finish_long_operation("dataset"); self._organization_transfer_task=None
        self.cancel_projection_button.hide(); self.update_selection_controls()
        errors=list(result.get("errors",()))
        items=list(result.get("items",()))
        if errors or len(items)!=len(self._organization_transfer_context[1]):
            self.header.set_status("Move preview needs attention • nothing changed")
            QMessageBox.warning(
                self,"Complete selection is not ready",
                f"The exact {len(self._organization_transfer_context[1]):,}-image scope was held; nothing moved.\n\n"+"\n".join(errors[:12]),
            )
            self._organization_transfer_context=None; self._notify_background_work_finished(); return
        kind=str(result["kind"]); destination=Path(result["destination"])
        members=[member for item in items for member in item["transfer"].members]
        required=sum(item["transfer"].required_bytes for item in items)
        modes={item["transfer"].mode for item in items}
        boundary_required=any(
            item.get("boundary") is not None
            and item["boundary"].requires_explicit_confirmation
            for item in items
        )
        destination_name={"recycle":"Recycle Bin","ingest":"Ingest","problems":"Problems"}[kind]
        box=QMessageBox(QMessageBox.Question,
            "Move Exact Dataset Scope to "+destination_name,
            f"Move exactly {len(items):,} selected image family(s) containing {len(members):,} file(s)?\n\nDestination: {destination}\nTransfer modes: {', '.join(sorted(modes))}\nAdditional space required: {required:,} bytes\n\nImage Knowledge, TXT, XMP, and the source library identity recorded by this preview stay with each complete family.",
            QMessageBox.NoButton,self)
        move=box.addButton("Move to "+destination_name,QMessageBox.AcceptRole)
        cancel=box.addButton(QMessageBox.Cancel); box.setDefaultButton(cancel); box.setEscapeButton(cancel)
        if boundary_required:
            confirmation=QCheckBox(
                "I reviewed the exact files, storage roles, and Local Only boundary crossing."
            )
            confirmation.setAccessibleName("Confirm exact Local Only boundary transfer")
            move.setEnabled(False); confirmation.toggled.connect(move.setEnabled)
            box.setCheckBox(confirmation)
        box.setDetailedText("\n".join(f"{member.source}  →  {member.target}" for member in members))
        box.exec()
        if box.clickedButton() is not move:
            self.header.set_status("Move cancelled before writing"); self._organization_transfer_context=None
            self._notify_background_work_finished(); return
        result["boundary_confirmed"]=boundary_required
        self._start_organization_transfer_apply(result)

    def _start_organization_transfer_apply(self,preview):
        if history_dialogs_busy(self):
            show_operation_in_progress(self,"History restoration"); return
        items=list(preview["items"]); kind=str(preview["kind"])
        destination_name={"recycle":"Recycle Bin","ingest":"Ingest","problems":"Problems"}[kind]
        label=f"Moving {len(items):,} image family(s) to {destination_name}"
        started,active=self.state.begin_long_operation("dataset",label,resource="io")
        if not started:
            show_operation_in_progress(self,active); self._organization_transfer_context=None; return
        destination=Path(preview["destination"])
        boundary_confirmed=bool(preview.get("boundary_confirmed"))
        group_id="dataset-transfer-"+uuid.uuid4().hex
        def work(progress,should_cancel):
            moved=[]; errors=[]; cancelled=False; recovery_required=False
            operation_ids=[]; changed_paths=[]; recycle_ids=[]; recovery_operation_id=""
            for index,item in enumerate(items,1):
                if should_cancel():cancelled=True; break
                path=item["path"]
                try:
                    if kind=="recycle":
                        record=self.state.recycle.apply_recycle(
                            item["plan"],reason="move selected Dataset image to Recycle Bin",
                            boundary_confirmed=boundary_confirmed,
                            should_cancel=should_cancel,
                        )
                        recycle_ids.append(record.id)
                        changed_paths.extend((record.original_path,record.current_path))
                    else:
                        profile=item["profile"]
                        record=self.state.moves.move(
                            path,destination,relative_to=item["root"],
                            reason="move selected Dataset image to "+destination_name,
                            preview=item["transfer"],collision_policy="error",
                            force_copy=bool(item.get("force_copy")),
                            should_cancel=should_cancel,
                            boundary_decision=item.get("boundary"),
                            boundary_confirmed=boundary_confirmed,
                            source_profile_ids=(profile.stable_id,) if profile is not None else (),
                            group_id=group_id,
                        )
                        operation_ids.append(record.operation_id)
                        changed_paths.extend(record.files[0][key] for key in ("from","to"))
                    moved.append(str(path))
                except InterruptedError:cancelled=True; break
                except TransferRecoveryRequired as error:
                    errors.append(f"{path.name}: {error}"); recovery_required=True
                    recovery_operation_id=getattr(error,"operation_id",""); break
                except Exception as error:errors.append(f"{path.name}: {error}")
                progress(index,len(items),path.name)
            return {"kind":kind,"moved":moved,"errors":errors,"cancelled":cancelled,"recovery_required":recovery_required,
                "organization_group_id":group_id,"organization_operation_ids":tuple(operation_ids),
                "organization_paths":tuple(dict.fromkeys(changed_paths)),"recovery_operation_id":recovery_operation_id,
                "recycle_record_ids":tuple(recycle_ids)}
        task=CancelableProgressCallTask(work); self._organization_transfer_task=task
        self.cancel_projection_button.setText("Cancel Safely"); self.cancel_projection_button.setEnabled(True); self.cancel_projection_button.show()
        self.update_selection_controls(); self.header.set_status(label+"…")
        task.signals.progress.connect(lambda index,total,name:self.state.update_long_operation("dataset",index,total,name))
        task.signals.complete.connect(self._organization_apply_complete)
        task.signals.cancelled.connect(self._organization_transfer_cancelled)
        task.signals.failed.connect(self._organization_transfer_failed)
        self.pool.start(task)

    def _organization_apply_complete(self,result):
        self.state.finish_long_operation("dataset"); self._organization_transfer_task=None
        self._organization_transfer_context=None; self.cancel_projection_button.hide()
        moved={str(value) for value in result.get("moved",())}; errors=list(result.get("errors",()))
        DatasetPage._remember_organization_result(self,result)
        self.batch_selected.difference_update(moved)
        self.filtered=[path for path in self.filtered if str(path) not in moved]
        if self.current_path is not None and str(self.current_path) in moved:
            self.current_path=None; self.current_record=None; self.inspector.hide()
        self.render_page()
        label={"recycle":"Recycle Bin","ingest":"Ingest","problems":"Problems"}[result["kind"]]
        self.header.set_status(
            ("Paused safely • " if result.get("cancelled") else "")+
            f"Moved {len(moved):,} image family(s) to {label} • {len(errors):,} need attention"
        )
        if result.get("organization_paths"):
            self.library_paths_changed.emit(tuple(Path(value) for value in result["organization_paths"]))
        if errors and not getattr(self,"_shutdown_requested",False):
            QMessageBox.warning(
                self,"Move recovery needs attention" if result.get("recovery_required") else "Some images were not moved",
                "\n".join(errors[:8]),
            )
        self._notify_background_work_finished()

    def _organization_transfer_cancelled(self):
        self.state.finish_long_operation("dataset"); self._organization_transfer_task=None
        self._organization_transfer_context=None; self.cancel_projection_button.hide()
        self.update_selection_controls(); self.header.set_status("Move cancelled safely")
        self._notify_background_work_finished()

    def _organization_transfer_failed(self,message: str):
        self.state.finish_long_operation("dataset"); self._organization_transfer_task=None
        self._organization_transfer_context=None; self.cancel_projection_button.hide()
        self.update_selection_controls(); self.header.set_status("Move stopped before completion")
        if not getattr(self,"_shutdown_requested",False):
            QMessageBox.warning(self,"Move could not continue",message)
        self._notify_background_work_finished()

    def move_to_problems(self):
        if self.current_record is None or not self.dataset:
            QMessageBox.information(self,"Select an image","Select an image from the gallery first."); return
        if not DatasetPage._storage_scope_ready(self,
            (self.current_record.image_path,),write=True,action="move",
        ):
            return
        self._begin_organization_transfer_preview((self.current_record.image_path,),"problems")

    def _remember_organization_result(self,result):
        self._move_history_scope=OrganizationHistoryScope.from_result(result)
        self._organization_recycle_ids=tuple(result.get("recycle_record_ids",()))
        scope=self._move_history_scope
        self.undo_move_button.setText("Review Recycle Items…" if self._organization_recycle_ids else
            f"Review Move History ({len(scope.operation_ids):,})…" if scope.available else "Review Move History…")
        self.undo_move_button.setEnabled(scope.available or bool(self._organization_recycle_ids))
        if hasattr(self,"organization_result_bar"):
            self.organization_result_button.setText(self.undo_move_button.text())
            self.organization_result_button.setEnabled(self.undo_move_button.isEnabled())
            count=len(scope.operation_ids)+len(self._organization_recycle_ids)
            prefix="Move needs recovery" if result.get("recovery_required") else "Move paused" if result.get("cancelled") else "Move complete"
            self.organization_result_label.setText(f"{prefix} · {count:,} complete families recorded · {len(result.get('errors',())):,} need attention")
            self.organization_result_bar.setVisible(bool(count or result.get("errors") or result.get("cancelled")))

    def _organization_history_restored(self,scope,original_paths,outcomes):
        completed=tuple(value for value in outcomes if value.status=="complete")
        advanced=scope.advanced(outcomes) if scope is not None else None
        if scope is not None and getattr(self,"_move_history_scope",None) is scope:
            self._move_history_scope=advanced
        if not completed:
            return advanced
        paths=tuple(dict.fromkeys(Path(path) for value in completed
            for path in (getattr(value,"previous_path",None),value.current_path) if path is not None))
        # Older callers without departure evidence retain their exact selection.
        if any(getattr(value,"previous_path",None) is None for value in completed):
            paths=tuple(dict.fromkeys((*paths,*(Path(path) for path in original_paths))))
        for path in paths:
            self._readiness_cache.pop(path,None)
            self._invalidate_inspection_cache(path)
            self.audit_issues.pop(path,None)
        departures={str(value.previous_path) for value in completed if getattr(value,"previous_path",None) is not None
            and value.previous_path!=value.current_path}
        self.batch_selected.difference_update(departures)
        self.filtered=[path for path in self.filtered if str(path) not in departures]
        if self.current_path is not None and str(self.current_path) in departures:
            self.current_path=None; self.current_record=None; self.inspector.hide()
        self.render_page()
        self.library_paths_changed.emit(paths)
        self.header.set_status(f"Restored {len(completed):,} complete image family(s); exact affected locations are refreshing.")
        return advanced

    def undo_file_move(self):
        if history_dialogs_busy(self) or any(getattr(self,name,None) is not None for name in (
                "_library_move_task","_organization_transfer_task","_active_editor","_editor_load_task",
                "_preparation_task","_batch_task","_projection_task","_metadata_backup_task")):
            show_operation_in_progress(self,"Finish the current Dataset or History operation first"); return
        records=getattr(self,"_organization_recycle_ids",())
        if records:
            return self.open_recycle_bin(record_ids=records)
        scope=getattr(self,"_move_history_scope",None)
        if scope is None or not scope.available:
            QMessageBox.information(self,"Recorded move unavailable",
                "This completion has no exact recorded operation. Select images and open History & Versions to browse their recorded moves.")
            return
        try:
            dialog_scope=scope
            def restored(outcomes):
                nonlocal dialog_scope
                dialog_scope=DatasetPage._organization_history_restored(self,dialog_scope,dialog_scope.paths,outcomes)
            return open_organization_history(self,**scope.browser_kwargs(),
                on_outcomes=restored)
        except Exception as error:
            QMessageBox.warning(self,"Move history unavailable",str(error))

    def open_external(self):
        if not self.current_path:
            return
        if not DatasetPage._storage_scope_ready(self,
            (self.current_path,),write=False,action="open",
        ):
            return
        QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.current_path)))
