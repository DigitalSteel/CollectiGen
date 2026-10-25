from __future__ import annotations

from .help_content import workspace_help

import copy
from contextlib import contextmanager
from dataclasses import replace
import hashlib
from html import escape
import json
import os
from pathlib import Path
import sqlite3
from threading import Event
from time import monotonic,sleep
from uuid import uuid4

from PySide6.QtCore import QEvent,QObject,QRunnable,QSignalBlocker,QThreadPool,QTimer,Signal,Slot,Qt
from PySide6.QtGui import QImage,QPixmap
from PySide6.QtWidgets import (
    QComboBox,QFileDialog,QFrame,QGridLayout,QGroupBox,QHBoxLayout,QHeaderView,QInputDialog,QLabel,QLineEdit,
    QDialog,QDialogButtonBox,QMenu,QMessageBox,QProgressBar,QPushButton,QScrollArea,QSizePolicy,QSplitter,QTabBar,QTabWidget,QTextEdit,QToolButton,QTreeWidget,
    QTreeWidgetItem,QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from .accessibility import announce_accessibly
from .components import DisclosureButton,ui_setting_bool

from core import (
    AutomaticAIAnalysisService,CategoryRegistry,CharacterDatabase,ConflictRuleEngine,DatasetScope,ExternalEvidenceImportCheckpointService,
    ConfirmedTagIdentityError,ConfirmedTagIdentityService,DatasetIndex,ImageAuthorityInitializationService,ImageEvidenceInspector,ImageFindingDecisionService,ImageKnowledgeReconciliationService,ImageRecord,ImageWorkflowState,ImageWorkflowStateService,LibraryMovePlan,LibraryMovePlanningService,LibraryMoveProtectionService,OperationJournal,
    IngestAttentionItem,IngestAttentionReport,IngestImportPlan,
    IngestImportPlanningService,
    ImageTagBatchService,JoyCaptionBackend,LibraryStorageInspector,MetadataSyncService,OrganizationRouter,PreferredCaptionPreparationService,TagOrderCandidate,TagOrderingService,TransferRecoveryRequired,UnifiedPreparationService,configured_plain_language_backend,configured_wd_backends,
    active_automation_engine,merge_plain_language_in_source_order,normalize_plain_language_exclusion,split_plain_language,
    current_candidate_sources,current_evidence_keys_by_source,
)
from core.paths import CATEGORIES_PATH,DEFAULTS_DIR,USER_DATA_DIR
from core.tag_collection import normalize_tag
from core.ingest_gallery_projection import IngestGalleryProjection,RevisionDict
from core.image_workflow_state import ImageKnowledgeCoverage,image_knowledge_coverage,image_preparation_stage
from core.image_knowledge_history import HistoryWriteInterrupted
from core.external_evidence_import import ExternalEvidenceImportCounts
from core.organization_history_scope import OrganizationHistoryScope, read_interrupted_add_receipt
from core.dataset_scope import IMG_EXTENSIONS
from core.ingest_add_execution import AddImagesBatchExecutor
from core.ingest_add_batch import AddBatchIntentStore,AddBatchState

from .ai_run_dialog import RunAIDialog
from .history_workflow import capture_operation,open_history,open_organization_history,close_history_dialogs,history_dialogs_busy
from .gallery import IMAGE_BADGE_HELP,GalleryScaleControl,ImageCardGrid,content_safety_policy,filter_content_paths
from .dependency_dialog import ensure_capability
from .deferred_editor import defer_editor_delivery
from .image_data_editor import (
    ImageDataEditor,REQUEST_SOURCE_CORRECTION,
    reconcile_embedded_with_preview,prepare_image_data_editor_context,
)
from .image_tag_batch_dialog import ImageTagBatchDialog
from .ingest_import_dialog import IngestImportPreviewDialog
from .ingest_add_batch_dialog import SavedAddBatchDialog
from .external_evidence_import_dialog import ExternalEvidenceImportDialog,external_evidence_result_dialog
from .library_profile_dialog import LibraryDestinationDialog
from .library_move_dialog import LibraryMovePreviewDialog as _MovePreviewDialog
from .preferred_caption_preparation_dialog import PreferredCaptionPreparationDialog
from .preparation_controls import (
    PreparationControls,ai_tag_runtime_recovery,show_ai_tag_runtime_recovery,
)
from .preparation_result_dialog import PreparationResultDialog,should_show_preparation_results
from .modal_feedback import FeedbackEnabledDialog
from .tag_identity_review import review_removed_identities
from .tag_relationship_editor import TagReferencePickerDialog
from .components import EmptyStatePanel,PageHeader,RangeCheckTreeWidget,ai_estimate_short_text,bind_status_footer,compact_button,set_progress_bar,show_operation_in_progress
from .workers import AsyncCallTask,CoordinatedCallTask,start_qt_delivery_task
from .resolution_filter import ResolutionFilter
from .gallery_filters import GalleryFilters


@contextmanager
def _ingest_history_write_guard(context,path):
    """Direct writes must not implicitly reconcile an interrupted saved change."""
    if context is None:
        yield
        return
    with context.locked(path):
        if context.store.has_pending(context.entity):
            raise HistoryWriteInterrupted(
                "Saved history needs recovery. Reopen this exact image in its "
                "data editor before saving another change."
            )
        yield


def _organization_result(group_id,records,*,action="move",recovery_operation_id=""):
    operation_ids=[];paths=[];members=[]
    for record in records:
        identifier=getattr(record,"operation_id","")
        if isinstance(identifier,str) and identifier:operation_ids.append(identifier)
        if record.files:
            paths.extend(str(record.files[0][key]) for key in ("from","to") if record.files[0].get(key))
            if isinstance(identifier,str) and identifier:
                members.append({"operation_id":identifier,**{key:str(record.files[0][key]) for key in ("from","to")}})
    return {"organization_group_id":group_id,"organization_operation_ids":tuple(dict.fromkeys(operation_ids)),
        "organization_paths":tuple(dict.fromkeys(paths)),"organization_action":action,
        "organization_members":tuple(members),
        "recovery_operation_id":str(recovery_operation_id or "")}


def _saved_add_result(result, *, skipped=0):
    records=result.records
    added=[record.files[0]["to"] for record in records if record.files]
    verb="Copied" if result.action=="copy" else "Moved"
    status={
        "complete":"Saved Add Images batch complete.",
        "paused":"Paused after the current image. Review Saved Add to continue the unstarted files.",
        "blocked":"Saved Add Images needs a fresh review before continuing.",
        "held":"One Add Images attempt needs exact recovery; no remaining file will be replayed.",
        "closure_pending":"All transfers finished; review the saved batch to confirm closure.",
    }[result.status]
    return {
        **_organization_result(result.group_id,records,action="add",
                               recovery_operation_id=result.recovery_operation_id),
        "processed":len(records),"skipped":skipped,"errors":list(result.errors),
        "added":added,"cancelled":result.status=="paused",
        "recovery_required":result.status=="held",
        "recovery_not_attempted":result.remaining,
        "add_batch_id":result.parent_id,"add_batch_status":result.status,
        "add_batch_completed":result.completed,
        "add_batch_remaining":result.remaining,"add_batch_held":result.held,
        "summary_lines":[status,
            f"{verb} {len(records):,} image family(s) in this step. "
            f"Saved batch: {result.completed:,} completed, {result.remaining:,} unstarted, "
            f"{result.held:,} need recovery.",
            "Imported images return to ordinary Ingest review; no evidence was confirmed by Add Images."],
    }


def _group_batch_errors(errors) -> list[str]:
    grouped={}
    order=[]
    for value in errors:
        text=str(value)
        name,separator,detail=text.partition(": ")
        key=detail if separator else text
        if key not in grouped:
            grouped[key]=[]
            order.append(key)
        grouped[key].append(name if separator else "")
    lines=[]
    for detail in order:
        names=grouped[detail]
        if len(names)>1:
            lines.append(f"{len(names):,} images: {detail}")
        elif names[0]:
            lines.append(f"{names[0]}: {detail}")
        else:
            lines.append(detail)
    return lines


def _configured_ingest_folder(settings) -> Path | None:
    """Return an explicitly configured Ingest folder, never Path('') == cwd."""
    value=str(getattr(settings,"ingest_directory","") or "").strip()
    return Path(value) if value else None


def _set_ingest_footer_activity(owner,message: str) -> None:
    """Keep transient Ingest work copy with its shared footer progress."""
    summary=getattr(owner,"batch_summary",None)
    if summary is None:return
    summary.setText(message)
    summary.show()


def _clear_ingest_footer_activity(owner,prefix: str) -> None:
    summary=getattr(owner,"batch_summary",None)
    if summary is not None and str(summary.text()).startswith(prefix):
        summary.hide()


class _InspectionSignals(QObject):
    complete=Signal(str,object); failed=Signal(str,str)
    finished=Signal()


class _InspectionTask(QRunnable):
    def __init__(self,image: Path,knowledge=None):
        super().__init__(); self.image=Path(image); self.knowledge=knowledge; self.signals=_InspectionSignals()
        self._cancelled=Event(); self.signature=None

    def cancel(self):
        self._cancelled.set()

    @Slot()
    def run(self):
        try:
            if self._cancelled.is_set():return
            try:
                self.signature=IngestPage._editor_source_signature(self.image)
                record=ImageRecord(self.image)
            except Exception as exc:
                self.signals.failed.emit(str(self.image),"record:"+str(exc)); return
            if self._cancelled.is_set():return
            try:
                report=ImageEvidenceInspector(knowledge=self.knowledge).inspect(self.image)
                result=(record,report)
            except Exception as exc:
                result=(record,None,str(exc))
            if not self._cancelled.is_set():
                self.signals.complete.emit(str(self.image),result)
        finally:
            self.signals.finished.emit()


class _EditorPrefetchSignals(QObject):
    complete=Signal(str,object); failed=Signal(str,object)


class _EditorPrefetchTask(QRunnable):
    """Load exactly one adjacent review record while the current editor is open."""

    def __init__(self,image: Path,knowledge=None,generation: int=0):
        super().__init__(); self.image=Path(image); self.knowledge=knowledge
        self.generation=int(generation)
        self.signals=_EditorPrefetchSignals()

    @Slot()
    def run(self):
        try:
            before=IngestPage._editor_source_signature(self.image)
            record=ImageRecord(self.image)
            report=ImageEvidenceInspector(knowledge=self.knowledge).inspect(self.image)
            preview=QImage(str(self.image))
            after=IngestPage._editor_source_signature(self.image)
            if before!=after:
                raise RuntimeError("Image data changed while the next review item was loading.")
            self.signals.complete.emit(
                str(self.image),(self.generation,after,record,report,preview)
            )
        except Exception as exc:
            self.signals.failed.emit(str(self.image),(self.generation,str(exc)))


class _BatchSignals(QObject):
    progress=Signal(int,int,str); complete=Signal(object); failed=Signal(str)


class _BatchTask(QRunnable):
    def __init__(self,operation,paths):
        super().__init__(); self.operation=operation; self.paths=[Path(path) for path in paths]; self.signals=_BatchSignals(); self._cancelled=Event()

    def cancel(self): self._cancelled.set()

    def is_cancelled(self): return self._cancelled.is_set()

    @Slot()
    def run(self):
        try:
            last_emit=0.0
            last_total=-1
            def report(index,total,name):
                nonlocal last_emit,last_total
                now=monotonic()
                terminal=bool(total and index>=total)
                if terminal or total!=last_total or now-last_emit>=0.20:
                    self.signals.progress.emit(index,total,name)
                    last_emit=now; last_total=total
            result=self.operation(self.paths,report,self.is_cancelled)
            self.signals.complete.emit(result)
        except InterruptedError:
            self.signals.complete.emit({"processed":0,"skipped":0,"errors":[],"summary_lines":["Cancelled safely; completed image changes were kept."]})
        except Exception as exc: self.signals.failed.emit(str(exc))


class _AddRecoveryReviewDialog(QDialog):
    """Keep the exact family operation visible until its worker has drained."""

    recovery_busy=False

    def reject(self):
        if not self.recovery_busy:super().reject()

    def closeEvent(self,event):
        if self.recovery_busy:event.ignore()
        else:super().closeEvent(event)


class IngestPage(QWidget):
    open_recycle_requested=Signal(object)
    @property
    def _state_cache(self):
        return self._revision_state_cache

    @_state_cache.setter
    def _state_cache(self,value):
        self._revision_state_cache=RevisionDict(value)

    def _ordering_projection(self):
        states=self._state_cache
        known=getattr(self,"_known_record_paths",())
        key=(id(self.images),len(self.images),id(states),
             getattr(states,"revision",None),id(self._attention_report),id(known))
        cached=getattr(self,"_gallery_order_projection",None)
        if cached is not None and cached[0]==key and isinstance(states,RevisionDict):
            return cached[1]
        projection=IngestGalleryProjection(self.images,self._state_for,self._stage_for)
        self._gallery_order_projection=(key,projection)
        # Retain the keyed containers so Python cannot recycle their identities
        # for a different same-size inventory or state snapshot.
        self._gallery_order_sources=(self.images,states,self._attention_report,known)
        return projection

    library_changed=Signal(); library_paths_changed=Signal(object); open_setup=Signal(); content_ratings_requested=Signal(object); edit_tag_requested=Signal(str,object); create_tag_requested=Signal(str,object)
    open_dataset_image=Signal(str)
    open_automation=Signal(str,object)
    source_correction_requested=Signal(object)
    background_work_finished=Signal()
    attention_changed=Signal(object,bool)

    def __init__(
        self,state,parent=None,*,defer_expensive_initialization: bool=False,
        ui_settings=None,
    ):
        super().__init__(parent); self.state=state; self.settings=state.settings
        self.ui_settings=ui_settings
        self._summary_visible=ui_setting_bool(
            ui_settings,"ingest/summary_visible",True,
        )
        self.images=[]; self.page=0; self.cards={}; self.generation=0; self.current_path=None; self.selected=set(); self._loaded_folder=""; self._state_cache={}
        self.current_record=None; self.current_report=None; self.rows=[]; self._pending_imports=[]; self.current_pixmap=QPixmap()
        self._reopen_editor_after_inspection=False
        self._editor_prefetch_cache={}; self._editor_prefetch_tasks={}
        self._pending_editor_prefetch=None; self._editor_queue_tab_index=0
        self._editor_prefetch_generation=0
        self._current_editor_preview=None
        self._editor_queue=[]; self._editor_queue_index=0
        self._preparation_review_scope=set()
        self._batch_conflict_context={}
        self._active_editor=None
        self._pending_source_correction_scope=None
        self._preparation_editor=None
        self._duplicate_preflight_dialog=None
        self.pool=QThreadPool.globalInstance(); self.characters=CharacterDatabase(USER_DATA_DIR); self.categories=CategoryRegistry(DEFAULTS_DIR/"categories.json",CATEGORIES_PATH)
        self._defer_expensive_initialization=bool(defer_expensive_initialization)
        self._conflict_task=None
        self._conflicts_ready=not self._defer_expensive_initialization
        self._conflict_error=""
        self.conflicts=(
            ConflictRuleEngine()
            if self._defer_expensive_initialization else
            ConflictRuleEngine.load(
                DEFAULTS_DIR/"conflicts.json",tags=self.state.knowledge,
            )
        )
        self._state_scan_generation=0
        self._scan_task=None; self._state_scan_task=None; self._batch_task=None; self._batch_paths=[]; self._move_planner=None; self._move_protection_service=None
        self._ingest_import_source_kind=""
        self._state_scan_restart_requested=False
        self._state_scan_restart_paths=set()
        self._state_scan_missing_paths=set()
        self._unified_preparation_service=None
        self._unified_preparation_operation_id=""
        self._unified_preparation_paths=[]
        self._unified_preparation_history_id=""
        self._batch_completion_context=""
        self._batch_progress_unit="image"
        self._batch_progress_scope=""
        self._batch_pause_after_current=False
        self._move_undo_context=False
        self._completed_library_paths=()
        self._organization_scope=OrganizationHistoryScope()
        self._organization_members={}
        self._organization_action="move"
        self._recycle_record_ids=()
        startup_results=getattr(self.state.moves,"startup_add_recovery_results",())
        self._startup_add_recovery_results=(tuple(startup_results)
            if isinstance(startup_results,(tuple,list)) else ())
        self._saved_add_executor=AddImagesBatchExecutor(self.state.moves)
        self._saved_add_state=None
        self._saved_add_preview_failed=False
        self._saved_add_error=""
        self._refresh_saved_add_batch_state()
        self._ingest_narrow_layout=False
        self._narrow_inspector_open=False
        self._attention_report=IngestAttentionReport(Path(self.settings.ingest_directory))
        self._shutdown_requested=False
        self.workflow_states=ImageWorkflowStateService(
            self.state.knowledge,self.characters,
            ignored_plain_language=self.settings.ignored_plain_language_sentences,categories=self.categories,
        )
        self._workflow_index=None
        self._trusted_workflow_paths=set()
        self._known_record_paths=set()
        self._readiness_context_fingerprint=""
        self._responsive_layout_timer=QTimer(self)
        self._responsive_layout_timer.setSingleShot(True)
        self._responsive_layout_timer.setInterval(16)
        self._responsive_layout_timer.timeout.connect(self.update_responsive_layout)
        root=QVBoxLayout(self); root.setContentsMargins(22,18,22,18); root.setSpacing(12)

        self.header=PageHeader(
            "Ingest","",
            workspace_help("ingest")+IMAGE_BADGE_HELP,
        )
        add_images=QToolButton(); self.add_images_button=add_images
        add_images.setText("Add Images…")
        add_images.setPopupMode(QToolButton.InstantPopup)
        add_images.setAccessibleName("Add images to Ingest")
        add_images.setAccessibleDescription(
            "Choose image files or a folder, then preview Copy to Ingest or Move to Ingest."
        )
        add_menu=QMenu(add_images)
        self.add_image_files_action=add_menu.addAction("Choose Image Files…")
        self.add_image_files_action.triggered.connect(self.choose_ingest_image_files)
        self.add_image_folder_action=add_menu.addAction("Choose Folder…")
        self.add_image_folder_action.triggered.connect(self.choose_ingest_image_folder)
        add_images.setMenu(add_menu)
        compact_button(add_images)
        self._pending_add_images_request=None
        self._active_add_images_preview_request=None
        self._pending_add_images_verified_versions=()
        self._active_add_images_verified_versions=()
        self._pending_add_images_ordinary=False
        self._active_add_images_ordinary=False
        retry_add_images=compact_button(QPushButton("Retry Add Images Preview",self))
        self.retry_add_images_button=retry_add_images
        retry_add_images.setToolTip(
            "Retry the last selected files or folder after the current work finishes. Nothing has been imported yet."
        )
        retry_add_images.clicked.connect(self._retry_ingest_import_preview)
        retry_add_images.hide()
        choose_ingest_folder=compact_button(QPushButton("Choose Ingest Folder…",self))
        self.choose_ingest_folder_button=choose_ingest_folder
        choose_ingest_folder.setToolTip(
            "Correct the saved Ingest location, then retry the retained selection with a fresh preview."
        )
        choose_ingest_folder.clicked.connect(self._choose_ingest_folder_for_add)
        choose_ingest_folder.hide()
        import_evidence=compact_button(QPushButton("Import External Evidence…"))
        self.import_external_evidence_button=import_evidence
        import_evidence.setAccessibleDescription(
            "Choose an exact incoming-image scope and preview adjacent TXT and Metadata evidence before reading it."
        )
        import_evidence.clicked.connect(self.open_external_evidence_import)
        refresh=compact_button(QPushButton("Scan Again"),primary=True); self.refresh_button=refresh; refresh.clicked.connect(self.scan)
        self.header.actions.addWidget(add_images)
        self.header.actions.addWidget(import_evidence); self.header.actions.addWidget(refresh)
        self.video_ingest_button=compact_button(QPushButton("Review Media…"))
        self.video_ingest_button.setToolTip("Inspect images, videos, playback, lineage and unexpected media in Ingest")
        self.video_ingest_button.clicked.connect(self.open_ingest_videos)
        self.header.actions.addWidget(self.video_ingest_button)
        self.history_button=compact_button(QPushButton("History & Versions…"))
        self.history_button.setToolTip("Review saved Image Knowledge for the selected images")
        self.history_button.clicked.connect(self.open_image_history)
        self.header.actions.addWidget(self.history_button)
        self._video_ingest_dialog=None
        self.summary_visibility_button=QToolButton()
        self.summary_visibility_button.setCheckable(True)
        self.summary_visibility_button.setFocusPolicy(Qt.StrongFocus)
        self.summary_visibility_button.setProperty("compact",True)
        self.summary_visibility_button.setFixedSize(40,32)
        self.summary_visibility_button.clicked.connect(self.toggle_summary)
        self.header.actions.addWidget(self.summary_visibility_button)
        self.summary_visibility_button.setAccessibleName("Show or hide Ingest summary")
        self._update_summary_visibility_button()
        root.addWidget(self.header)
        self.add_images_recovery=QFrame(self)
        self.add_images_recovery.setObjectName("statusCard")
        recovery_layout=QVBoxLayout(self.add_images_recovery)
        recovery_layout.setContentsMargins(12,8,12,8)
        recovery_layout.setSpacing(6)
        self.add_images_location_notice=QLabel(self.add_images_recovery)
        self.add_images_location_notice.setObjectName("warningLabel")
        self.add_images_location_notice.setAccessibleName("Retained Add Images selection and next step")
        self.add_images_location_notice.setWordWrap(True)
        recovery_layout.addWidget(self.add_images_location_notice)
        recovery_actions=QHBoxLayout()
        recovery_actions.setContentsMargins(0,0,0,0)
        recovery_actions.addWidget(retry_add_images)
        recovery_actions.addWidget(choose_ingest_folder)
        recovery_actions.addStretch(1)
        recovery_layout.addLayout(recovery_actions)
        self.add_images_recovery.hide()
        root.addWidget(self.add_images_recovery)

        self.attention_panel=QGroupBox("Ingest Needs Attention")
        self.attention_panel.setAccessibleName("Ingest needs attention")
        attention_layout=QHBoxLayout(self.attention_panel)
        attention_text=QWidget()
        attention_text_layout=QVBoxLayout(attention_text)
        attention_text_layout.setContentsMargins(0,0,0,0)
        attention_text_layout.setSpacing(4)
        self.attention_summary=QLabel()
        self.attention_summary.setWordWrap(True)
        self.attention_summary.setObjectName("errorLabel")
        self.attention_summary.setAccessibleName("Ingest issue summary")
        attention_text_layout.addWidget(self.attention_summary)
        self.attention_review_outcome=QLabel()
        self.attention_review_outcome.setWordWrap(True)
        self.attention_review_outcome.setObjectName("warningLabel")
        self.attention_review_outcome.setAccessibleName("Ingest issue review outcome")
        self.attention_review_outcome.hide()
        attention_text_layout.addWidget(self.attention_review_outcome)
        self.attention_review=compact_button(QPushButton("Review Issues"))
        self.attention_review.setAccessibleDescription(
            "Show the affected incoming images and their safe recovery actions."
        )
        self.attention_review.clicked.connect(self.review_attention)
        attention_layout.addWidget(attention_text,1)
        attention_layout.addWidget(self.attention_review,0,Qt.AlignTop)
        self.attention_panel.hide()
        root.addWidget(self.attention_panel)

        splitter=QSplitter(Qt.Horizontal); self.splitter=splitter; root.addWidget(splitter,1)
        splitter.setChildrenCollapsible(False)
        splitter.splitterMoved.connect(lambda *_:self._queue_responsive_layout())

        gallery_side=QWidget(); self.gallery_side=gallery_side; gallery_side.setMinimumWidth(0)
        gallery_layout=QVBoxLayout(gallery_side); gallery_layout.setContentsMargins(0,0,8,0); gallery_layout.setSpacing(6)
        self.stage_tabs=QTabBar()
        self.stage_tabs.setObjectName("ingestStageTabs")
        self.stage_tabs.setAccessibleName("Ingest preparation stage")
        self.stage_tabs.setAccessibleDescription(
            "Separate incoming images into unprepared, partially prepared, and complete stages."
        )
        self.stage_tabs.setDrawBase(False)
        self.stage_tabs.setExpanding(True)
        for label,key,tip in (
            ("Unprepared","unprepared","Images with no saved or pending Tags or Descriptions."),
            ("Partially Prepared","partial","Images with saved work that still needs preparation or review."),
            ("Complete","complete","Images whose Image Knowledge has no blocking completeness issue."),
        ):
            index=self.stage_tabs.addTab(label)
            self.stage_tabs.setTabData(index,key)
            self.stage_tabs.setTabToolTip(index,tip)
        self.stage_tabs.currentChanged.connect(self._stage_changed)
        self.search=QLineEdit()
        self.search.setPlaceholderText("Search incoming filenames…")
        self.search.setAccessibleName("Search incoming filenames")
        self._filter_search_timer=QTimer(self)
        self._filter_search_timer.setSingleShot(True)
        self._filter_search_timer.setInterval(160)
        self._filter_search_timer.timeout.connect(self._resolution_filter_changed)
        self.search.textChanged.connect(lambda *_:self._filter_search_timer.start())
        self.search.returnPressed.connect(self._resolution_filter_changed)
        self.filter_panel=GalleryFilters(self.stage_tabs,self.search,self.clear_filters)
        gallery_layout.addWidget(self.filter_panel)
        gallery_toolbar=QFrame(); self.gallery_toolbar=gallery_toolbar; gallery_toolbar.setObjectName("galleryToolbar")
        gallery_toolbar.setAccessibleName("Ingest gallery controls")
        toolbar_layout=QVBoxLayout(gallery_toolbar); toolbar_layout.setContentsMargins(9,7,9,7); toolbar_layout.setSpacing(5)
        gallery_status=QHBoxLayout(); self.gallery_primary_row=gallery_status; gallery_status.setSpacing(8)
        self.count=QLabel("Choose an Ingest folder. Incoming images load automatically."); self.count.setObjectName("mutedLabel")
        self.count.setAccessibleName("Ingest gallery result count")
        self.inspect_current_button=compact_button(QPushButton("Inspect Image"))
        self.inspect_current_button.setAccessibleDescription(
            "Open the inspector for the currently selected incoming image."
        )
        self.inspect_current_button.clicked.connect(self.show_current_inspector)
        self.inspect_current_button.hide()
        self.state_filter=QComboBox()
        self.state_filter.setAccessibleName("Ingest image state filter")
        self.state_filter.setAccessibleDescription(
            "Refine the current preparation stage by its detailed Image Knowledge state."
        )
        self.state_filter.setMinimumWidth(210)
        self.state_filter.currentIndexChanged.connect(self.filter_state_changed)
        state_control=QFrame(); self.state_filter_control=state_control
        state_control.setObjectName("galleryStateFilter")
        state_control.setProperty("active",False)
        state_layout=QHBoxLayout(state_control); state_layout.setContentsMargins(0,0,0,0); state_layout.setSpacing(5)
        self.state_filter_label=QLabel("Refine stage")
        self.state_filter_label.setBuddy(self.state_filter)
        state_layout.addWidget(self.state_filter_label); state_layout.addWidget(self.state_filter)
        self.gallery_scale=GalleryScaleControl(self.settings.gallery_page_size,self.settings.gallery_card_width)
        self.gallery_scale.changed.connect(self.gallery_density_changed); self.gallery_density=self.gallery_scale
        gallery_status.addWidget(self.count,1)
        gallery_status.addWidget(self.inspect_current_button)
        self.filter_panel.add_filter("Image Knowledge",state_control,self.state_filter)
        gallery_status.addWidget(self.gallery_density)
        toolbar_layout.addLayout(gallery_status)
        self.gallery_filter_row=QHBoxLayout()
        self.gallery_filter_row.setContentsMargins(0,0,0,0)
        self.gallery_filter_row.setSpacing(5)
        self.resolution_filter=ResolutionFilter(self.state,self)
        self.resolution_filter.set_review_scope_provider(self._resolution_review_paths)
        self.resolution_filter.changed.connect(self._resolution_filter_changed)
        self.resolution_filter.status_changed.connect(self._resolution_status_changed)
        from .resolution_dialog import resolution_events
        resolution_events().policy_changed.connect(self.resolution_filter.invalidate_policy)
        self.filter_panel.add_filter("Resolution",self.resolution_filter,self.resolution_filter.combo,"")
        self.resolution_filter.changed.connect(self.filter_panel.sync)
        self.gallery_filter_row.addStretch(1)
        toolbar_layout.addLayout(self.gallery_filter_row)
        self._recent_ingest_paths=()
        self.recent_import_panel=QFrame(self)
        self.recent_import_panel.setObjectName("statusCard")
        recent_import_layout=QHBoxLayout(self.recent_import_panel)
        recent_import_layout.setContentsMargins(12,8,12,8)
        recent_import_note=QLabel(
            "The latest added images are ready to review, even if your current filter hides them.",
            self.recent_import_panel,
        )
        recent_import_note.setWordWrap(True)
        recent_import_layout.addWidget(recent_import_note,1)
        self.review_added_button=compact_button(QPushButton("Review Added Images…",self.recent_import_panel))
        self.review_added_button.setToolTip(
            "Review the images from the latest successful import in the shared editor. "
            "Gallery filters and selection stay unchanged."
        )
        self.review_added_button.clicked.connect(self.review_added_images)
        recent_import_layout.addWidget(self.review_added_button)
        self.recent_import_panel.hide()
        root.insertWidget(2,self.recent_import_panel)
        self.state_progress=QProgressBar()
        self.state_progress.setAccessibleName("Ingest background check progress")
        self.state_progress.setAccessibleDescription(
            "Reports the exact read-only Ingest inventory or Image Knowledge check currently running."
        )
        self.state_progress.hide()
        selection_header=QWidget(); self.selection_header=selection_header; select_row=QHBoxLayout(selection_header); select_row.setContentsMargins(0,0,0,0); select_row.setSpacing(5)
        selection_header.setAccessibleName("Ingest gallery selection controls")
        self.selection_label=QLabel("Batch selection: none"); self.selection_label.setObjectName("mutedLabel")
        self.selection_label.setAccessibleDescription(
            "Images marked for batch actions; inspecting one image does not mark it for a batch."
        )
        self.select_page_button=QPushButton("Select Page"); self.select_page_button.clicked.connect(self.select_page)
        self.select_all_button=QPushButton("Select All"); self.select_all_button.clicked.connect(self.select_all)
        clear_selection=QPushButton("Clear Selection"); clear_selection.clicked.connect(self.clear_selection)
        for button in (self.select_page_button,self.select_all_button,clear_selection): button.setProperty("compact",True)
        select_row.addWidget(self.selection_label); select_row.addWidget(self.select_page_button); select_row.addWidget(self.select_all_button); select_row.addWidget(clear_selection); select_row.addStretch(1)
        toolbar_layout.addWidget(selection_header)
        batch=QGroupBox("Image actions"); self.batch_actions=batch; batch_layout=QVBoxLayout(batch)
        review_selected=QPushButton("Review Selection…"); self.review_selected_button=review_selected
        review_selected.setToolTip("Open the selected images as a review queue. For one image, double-click its card or focus the card and press Enter.")
        review_selected.clicked.connect(self.review_selected_data)
        review_selected.hide()
        organize=QPushButton("Move to…"); self.organize_button=organize
        organize.setAccessibleName("Move selected images to")
        organize.setAccessibleDescription(
            "Choose the final destination for the selected incoming images."
        )
        organize.setProperty("primary",True)
        organize.setSizePolicy(QSizePolicy.Maximum,QSizePolicy.Fixed)
        move_menu=QMenu(organize)
        self.move_library_action=move_menu.addAction("Named Library…")
        self.move_library_action.triggered.connect(self.batch_organize)
        self.move_problems_action=move_menu.addAction("Problems")
        self.move_problems_action.triggered.connect(self.batch_problems)
        self.move_recycle_action=move_menu.addAction("Recycle Bin…")
        self.move_recycle_action.triggered.connect(self.batch_recycle)
        self.move_folder_action=move_menu.addAction("Choose Folder…")
        self.move_folder_action.triggered.connect(self.move_active_to_folder)
        organize.setMenu(move_menu); organize.setEnabled(False)
        undo_move=QPushButton("Review Move History…"); undo_move.clicked.connect(self.undo_file_move); self.undo_move_button=undo_move
        undo_move.setToolTip("Review the exact completed image families and compare their current locations.")
        undo_move.hide(); undo_move.setEnabled(False)
        for button in (review_selected,undo_move): compact_button(button)
        self.preparation_controls=PreparationControls(
            self.settings,inline_options=False,
        )
        from .preparation_impact_dialog import preparation_impact_context
        self.preparation_controls.impact_context_provider=lambda:preparation_impact_context(self)
        self.preparation_controls.duplicate_preflight_runner=self._run_duplicate_preflight
        self.preparation_controls.requested.connect(self.batch_prepare)
        self.preparation_controls.undo_requested.connect(
            self.undo_preparation
        )
        batch_layout.addWidget(self.preparation_controls)
        finish_row=QHBoxLayout()
        self.run_ai_selected_button=compact_button(QPushButton("Run AI…"))
        self.run_ai_selected_button.setToolTip(
            "Choose AI Tags, AI Descriptions, or both for the exact checked images. "
            "Unlike Prepare, this AI-only review does not read TXT or Metadata."
        )
        self.run_ai_selected_button.setAccessibleDescription(
            "Opens the shared AI-only review for explicitly checked Ingest images; opening it starts no AI work."
        )
        self.run_ai_selected_button.clicked.connect(self.choose_batch_ai)
        self.run_ai_selected_button.hide()
        self.preparation_controls.action_row.insertWidget(1,self.run_ai_selected_button)
        self.preparation_controls.action_row.addWidget(organize)
        self.move_preview_notice=QLabel()
        self.move_preview_notice.setObjectName("mutedLabel")
        self.move_preview_notice.setAccessibleName("Move to Library preview status")
        self.move_preview_notice.setWordWrap(True)
        self.move_preview_notice.hide()
        batch_layout.addWidget(self.move_preview_notice)
        # Retained as a compatibility/state hook; the estimate now belongs in
        # the click-time preparation dialog rather than above the gallery.
        self.ai_time_estimate=QLabel(); self.ai_time_estimate.setObjectName("mutedLabel"); self.ai_time_estimate.setWordWrap(True); self.ai_time_estimate.hide()
        self.batch_progress=QProgressBar(); self.batch_progress.hide()
        self.cancel_batch_button=compact_button(QPushButton("Cancel Safely")); self.cancel_batch_button.clicked.connect(self.cancel_batch); self.cancel_batch_button.hide()
        self.batch_summary=QLabel(); self.batch_summary.setObjectName("mutedLabel"); self.batch_summary.setWordWrap(True); self.batch_summary.hide()
        bind_status_footer(
            self,self.batch_summary,self.state_progress,self.batch_progress,
            self.cancel_batch_button,title="Ingest",
        )
        gallery_layout.addWidget(gallery_toolbar)
        gallery_layout.addWidget(batch)
        self.gallery_scroll=QScrollArea(); self.gallery_scroll.setWidgetResizable(True); self.gallery_scroll.setFrameShape(QScrollArea.NoFrame)
        self._gallery_viewport=self.gallery_scroll.viewport()
        self._gallery_viewport.installEventFilter(self)
        self.gallery=ImageCardGrid(); self.gallery.chosen.connect(self.select_image); self.gallery.marked.connect(self.mark_selected)
        self.gallery.selection_replaced.connect(self.replace_selection)
        self.gallery.edit_requested.connect(self.edit_gallery_image)
        self.gallery.force_ai_requested.connect(self.force_ai_from_gallery)
        self.gallery.add_tag_requested.connect(self.add_tag_to_images)
        self.gallery.file_action_requested.connect(self._gallery_file_action)
        self.gallery.duplicate_check_requested.connect(self.check_duplicates_from_gallery)
        self.gallery_scroll.setWidget(self.gallery); gallery_layout.addWidget(self.gallery_scroll,1)
        self.no_ingest_panel=EmptyStatePanel(
            "Choose where incoming images arrive before adding or reviewing them. Ingest will not scan incoming files until a folder is chosen.",
            "Choose an Ingest folder",minimum_height=240,
        )
        self.choose_first_ingest_folder_button=self.no_ingest_panel.add_action(
            "Choose Ingest Folder…",self._choose_first_ingest_folder,
        )
        self.no_ingest_panel.hide()
        gallery_layout.addWidget(self.no_ingest_panel,1)
        self.empty_ingest_panel=EmptyStatePanel(
            "Add images to prepare and review them before moving them into a library. If you placed files in the Ingest folder yourself, choose Scan Again above.",
            "No incoming images yet",minimum_height=240,
        )
        self.empty_choose_image_files_button=self.empty_ingest_panel.add_action(
            "Choose Image Files…",self.choose_ingest_image_files,
        )
        self.empty_choose_image_folder_button=self.empty_ingest_panel.add_action(
            "Choose Folder…",self.choose_ingest_image_folder,primary=False,
        )
        self.empty_ingest_panel.hide()
        gallery_layout.addWidget(self.empty_ingest_panel,1)
        pager=QHBoxLayout(); self.previous=QPushButton("Previous Page"); self.next=QPushButton("Next Page"); self.page_label=QLabel("No images")
        self.previous.clicked.connect(lambda:self.change_page(-1)); self.next.clicked.connect(lambda:self.change_page(1))
        self.previous.setProperty("compact",True); self.next.setProperty("compact",True)
        pager.addWidget(self.previous); pager.addWidget(self.page_label,1,Qt.AlignCenter); pager.addWidget(self.next); gallery_layout.addLayout(pager)
        self.completion_panel=QFrame(); self.completion_panel.setObjectName("ingestCompletionPanel")
        completion=QHBoxLayout(self.completion_panel); self.completion_layout=completion
        completion.setContentsMargins(10,7,10,7); completion.setSpacing(8)
        completion_note=QLabel("Finish selected images after reviewing their data and destination."); self.completion_note=completion_note
        completion_note.setObjectName("mutedLabel"); completion_note.setWordWrap(True)
        self.view_completed_in_dataset=compact_button(QPushButton("Open in Dataset…"))
        self.view_completed_in_dataset.setAccessibleDescription(
            "Open an exact successfully moved image in the included Dataset library."
        )
        self.view_completed_in_dataset.clicked.connect(self._view_completed_library_image)
        self.view_completed_in_dataset.hide()
        self.review_interrupted_add_button=compact_button(QPushButton("Review Interrupted Add…"))
        self.review_interrupted_add_button.setAccessibleDescription(
            "Read the exact recorded Add Images transfer and restart-recovery guidance; this does not retry or change files."
        )
        self.review_interrupted_add_button.clicked.connect(self._review_interrupted_add)
        self.review_interrupted_add_button.hide()
        self.review_add_recovery_button=compact_button(QPushButton("Review Add Recovery…"))
        self.review_add_recovery_button.setAccessibleDescription(
            "Review exact Add Images families checked during startup recovery; this does not retry imports."
        )
        self.review_add_recovery_button.clicked.connect(self._review_add_recovery)
        self.review_add_recovery_button.hide()
        self.review_saved_add_button=compact_button(QPushButton("Review Saved Add…"))
        self.review_saved_add_button.setAccessibleDescription(
            "Review completed, unstarted and held Add Images families before any deliberate continuation."
        )
        self.review_saved_add_button.clicked.connect(self._review_saved_add)
        self.review_saved_add_button.hide()
        completion.addWidget(completion_note,1)
        completion.addWidget(self.view_completed_in_dataset)
        completion.addWidget(self.review_interrupted_add_button)
        completion.addWidget(self.review_add_recovery_button)
        completion.addWidget(self.review_saved_add_button)
        completion.addWidget(undo_move)
        # Keep the final workflow outcome visible before the gallery instead of
        # hiding it beneath a potentially long page of cards.
        gallery_layout.insertWidget(2,self.completion_panel)
        splitter.addWidget(gallery_side)

        review=QWidget(); review_layout=QVBoxLayout(review); review_layout.setContentsMargins(8,0,0,0)
        review_header=QHBoxLayout(); review_header.setContentsMargins(0,0,0,0)
        self.image_name=QLabel("Select an incoming image"); self.image_name.setObjectName("sectionTitle"); self.image_name.setWordWrap(True); self.image_name.setMinimumWidth(0)
        review_header.addWidget(self.image_name,1)
        self.inspector_back_button=compact_button(QPushButton("Back to Images"))
        self.inspector_back_button.setAccessibleDescription(
            "Return to the Ingest gallery without changing the current image or selection."
        )
        self.inspector_back_button.clicked.connect(self.show_ingest_gallery)
        self.inspector_back_button.hide()
        review_header.addWidget(self.inspector_back_button,0,Qt.AlignTop)
        review_layout.addLayout(review_header)
        self.preview=QLabel("No image selected"); self.preview.hide()
        self.inspect_tabs=QTabWidget()
        self.inspect_tabs.setMinimumSize(0,0)
        tabs_policy=self.inspect_tabs.sizePolicy()
        tabs_policy.setHorizontalPolicy(QSizePolicy.Ignored)
        self.inspect_tabs.setSizePolicy(tabs_policy)
        next_step=QWidget(); self.next_step_tab=next_step; summary_layout=QVBoxLayout(next_step)
        status_group=QGroupBox("Image status and next step"); self.summary_status_group=status_group
        status_layout=QVBoxLayout(status_group); self.recommendation=QLabel("Select an image to begin.")
        self.recommendation.setWordWrap(True); self.recommendation.setObjectName("mutedLabel")
        self.recommendation.setTextInteractionFlags(Qt.TextSelectableByMouse); status_layout.addWidget(self.recommendation)
        self.readiness_details=QLabel()
        self.readiness_details.setWordWrap(True)
        self.readiness_details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.readiness_details_toggle=DisclosureButton(
            "Readiness Details", "Hide Readiness Details", self.readiness_details,
        )
        self.readiness_details_toggle.hide()
        status_layout.addWidget(self.readiness_details_toggle,0,Qt.AlignLeft)
        status_layout.addWidget(self.readiness_details)
        self.summary_action_button=QPushButton("Edit Image Data…"); self.summary_action_button.setProperty("primary",True)
        self.summary_action_button.setAccessibleName("Summary next action")
        self.summary_action_button.clicked.connect(self.run_summary_action); self.summary_action_button.hide()
        self.summary_edit_button=QPushButton("Edit Image Data…")
        self.summary_edit_button.setToolTip("Correct this selected image's saved Tags or Descriptions. Nothing changes until you save in the editor.")
        self.summary_edit_button.setAccessibleDescription(self.summary_edit_button.toolTip())
        self.summary_edit_button.clicked.connect(
            lambda:self.review_selected_data(paths=[self.current_path]) if self.current_path is not None else None
        )
        self.summary_edit_button.hide()
        summary_actions=QHBoxLayout(); summary_actions.addStretch(1)
        summary_actions.addWidget(self.summary_edit_button)
        summary_actions.addWidget(self.summary_action_button)
        status_layout.addLayout(summary_actions); summary_layout.addWidget(status_group)
        tags_group=QGroupBox("Confirmed Tags"); self.confirmed_tags_group=tags_group; tags_layout=QVBoxLayout(tags_group)
        self.sdxl_summary=QLabel("No confirmed Tags")
        self.sdxl_summary.setWordWrap(True); self.sdxl_summary.setTextInteractionFlags(Qt.TextSelectableByMouse); tags_layout.addWidget(self.sdxl_summary)
        summary_layout.addWidget(tags_group)
        descriptions_group=QGroupBox("Confirmed Descriptions"); self.confirmed_descriptions_group=descriptions_group; descriptions_layout=QVBoxLayout(descriptions_group)
        self.plain_summary=QLabel("No confirmed Descriptions")
        self.plain_summary.setWordWrap(True); self.plain_summary.setTextInteractionFlags(Qt.TextSelectableByMouse); descriptions_layout.addWidget(self.plain_summary)
        summary_layout.addWidget(descriptions_group)
        sources_group=QGroupBox("Source overview"); self.source_summary_group=sources_group; sources_layout=QVBoxLayout(sources_group)
        self.source_summary=QLabel("No sources inspected"); self.source_summary.setWordWrap(True); self.source_summary.setObjectName("mutedLabel"); sources_layout.addWidget(self.source_summary)
        summary_layout.addWidget(sources_group)
        summary_layout.addStretch(1)
        self.inspect_tabs.addTab(next_step,"Summary")
        quick_review=QWidget(); self.quick_review_tab=quick_review
        quick_layout=QVBoxLayout(quick_review); quick_layout.setContentsMargins(8,8,8,8)
        self.additional_label=QLabel("No findings to review"); self.additional_label.setObjectName("mutedLabel"); quick_layout.addWidget(self.additional_label)
        self.inline_suggestions=QTreeWidget(); self.inline_suggestions.setHeaderLabels(("Information","Kind","Found in"))
        self.inline_suggestions.setRootIsDecorated(False); self.inline_suggestions.setAlternatingRowColors(True)
        inline_header=self.inline_suggestions.header(); inline_header.setMinimumSectionSize(48); inline_header.setSectionResizeMode(0,QHeaderView.Stretch)
        inline_header.setSectionResizeMode(1,QHeaderView.Fixed); inline_header.setSectionResizeMode(2,QHeaderView.Fixed)
        self.inline_suggestions.setColumnWidth(1,105); self.inline_suggestions.setColumnWidth(2,120); self.inline_suggestions.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.inline_suggestions.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.inline_suggestions.hide(); quick_layout.addWidget(self.inline_suggestions,1)
        self.recommended_button=QPushButton("Select an Image"); self.recommended_button.setProperty("primary",True); self.recommended_button.setEnabled(False)
        self.recommended_button.clicked.connect(self.run_recommended_action); quick_layout.addWidget(self.recommended_button,0,Qt.AlignRight)
        quick_review_index=self.inspect_tabs.addTab(quick_review,"Quick Review")
        self.inspect_tabs.setTabVisible(quick_review_index,False)
        review_layout.addWidget(self.inspect_tabs,1)
        review_actions=QVBoxLayout(); editor_hint=QLabel("Double-click a card, press Enter, or use Image Actions to edit image data."); self.editor_hint=editor_hint; editor_hint.setObjectName("mutedLabel"); editor_hint.setWordWrap(True)
        review_actions.addWidget(editor_hint)
        image_actions=QToolButton(); self.inspector_actions=image_actions
        image_actions.setText("Image Actions"); image_actions.setPopupMode(QToolButton.InstantPopup)
        image_actions.setProperty("compact",True)
        image_actions.setAccessibleName("Actions for the inspected image")
        image_actions.setAccessibleDescription(
            "Edit, resize, remix, animate, or make a reusable setup from this inspected image, not the checked batch selection."
        )
        image_actions.setToolTip(image_actions.accessibleDescription())
        image_actions.setEnabled(False)
        actions=QMenu(image_actions); actions.setToolTipsVisible(True)
        guided=actions.addAction("Choose How to Change This Image…")
        guided.setToolTip(
            "Compare faithful enlargement, AI detail enlargement, a close variation, and a substantial change before opening a separate review."
        )
        guided.triggered.connect(lambda _checked=False:self._inspector_file_action("change_image"))
        actions.addSection("Direct actions")
        for label,action_id,tip in (
            ("Edit Image Data…","review_selection",
             "Correct this image's saved Tags or Descriptions in the shared editor."),
            ("Resize This Image…","resize",
             "Compare faithful resize, local AI upscale and a separate generative-upscale draft. The original stays unchanged."),
            ("Remix This Image…","remix",
             "Open a separate source-bound Generate draft. Nothing runs until you review and launch it."),
            ("Animate This Image…","animate_this_image",
             "Open a separate video draft using this image. Nothing runs until you review and launch it."),
            ("Create Generation Setup from Image…","image_setup",
             "Reuse selected saved Tags or Descriptions and recorded generator settings without changing the image."),
        ):
            action=actions.addAction(label); action.setToolTip(tip)
            action.triggered.connect(lambda _checked=False,kind=action_id:self._inspector_file_action(kind))
            if action_id=="image_setup":self.inspector_setup_action=action
        actions.aboutToShow.connect(self._refresh_inspector_actions)
        image_actions.setMenu(actions)
        review_actions.addWidget(image_actions,0,Qt.AlignLeft)
        self.recommended_action=""; self.summary_action=""
        self.found=RangeCheckTreeWidget(); self.found.hide()
        # Word-wrapped Summary text must yield its preferred width to the
        # splitter. Exact text remains selectable and accessible while Qt
        # computes the additional height needed by the narrower pane.
        for label in review.findChildren(QLabel):
            label.setMinimumWidth(0)
            label_policy=label.sizePolicy()
            label_policy.setHorizontalPolicy(QSizePolicy.Ignored)
            label.setSizePolicy(label_policy)
        review.setMinimumWidth(0)
        review_policy=review.sizePolicy()
        review_policy.setHorizontalPolicy(QSizePolicy.Ignored)
        review.setSizePolicy(review_policy)
        review_scroll=QScrollArea(); self.review_scroll=review_scroll; review_scroll.setWidgetResizable(True); review_scroll.setFrameShape(QScrollArea.NoFrame)
        review_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        review_scroll.setWidget(review)
        review_panel=QWidget(); self.review_panel=review_panel; review_panel.setMinimumWidth(360)
        review_panel_layout=QVBoxLayout(review_panel); review_panel_layout.setContentsMargins(0,0,0,0)
        review_panel_layout.addWidget(review_scroll,1); review_panel_layout.addLayout(review_actions)
        splitter.addWidget(review_panel); review_panel.hide()
        splitter.setStretchFactor(0,3); splitter.setStretchFactor(1,1); splitter.setSizes((1050,400))
        self._sync_state_filter_options()
        self.batch_actions.hide()
        self.render_page()
        IngestPage._set_move_undo_available(self)
        if self._defer_expensive_initialization:
            QTimer.singleShot(0,self._start_conflict_preparation)
        if getattr(self.state,"ingest_preparation_work",None):
            self._restore_preparation_request_queue()

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
        self.conflicts=result
        self._conflict_error=""
        self._conflicts_ready=True
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

    def show_help(self):
        return self.header.show_help()

    def _add_images_start_folder(self) -> str:
        working=Path(self.settings.working_directory)
        if working.is_dir(): return str(working)
        ingest=_configured_ingest_folder(self.settings)
        return str(
            ingest.parent
            if ingest is not None and ingest.parent.is_dir() else
            Path.cwd()
        )

    def choose_ingest_image_files(self):
        if self._batch_task is not None: return
        selected,_filter=QFileDialog.getOpenFileNames(
            self,"Choose images to add",self._add_images_start_folder(),
            "Supported images (*.png *.jpg *.jpeg *.webp);;All files (*)",
        )
        if selected:
            self._start_ingest_import_preview("files",[Path(value) for value in selected],
                                              ordinary_user_selection=True)

    def _hold_ingest_import_selection(self,source_kind,paths,preserve_source,reason,*,
                                      location_required=False,verified_versions=(),
                                      ordinary_user_selection=False):
        self._pending_add_images_request=(source_kind,tuple(paths),preserve_source)
        self._pending_add_images_verified_versions=tuple(verified_versions)
        self._pending_add_images_ordinary=bool(ordinary_user_selection)
        if recovery:=getattr(self,"add_images_recovery",None):recovery.show()
        if button:=getattr(self,"retry_add_images_button",None): button.show()
        if location_required and (button:=getattr(self,"choose_ingest_folder_button",None)):
            button.show()
        if location_required:
            message=(
                f"{reason} Your selection is retained. Choose Ingest Folder, save an available location, "
                "then choose Retry Add Images Preview. No images have been imported."
            )
            if notice:=getattr(self,"add_images_location_notice",None):
                notice.setText(message); notice.show()
            return
        if button:=getattr(self,"choose_ingest_folder_button",None):button.hide()
        if notice:=getattr(self,"add_images_location_notice",None):
            notice.setText(
                f"{reason} Your selection is retained. Choose Retry Add Images Preview "
                "when ready; no images were imported."
            ); notice.show()

    def _choose_ingest_folder_for_add(self):
        if self._pending_add_images_request is None:return
        from .folder_locations import open_folder_locations
        open_folder_locations(
            self,focus="ingest_directory",settings=self.settings,focus_group_only=True,
        )
        if notice:=getattr(self,"add_images_location_notice",None):
            notice.setText(
                "Your selected images are still retained. Choose Retry Add Images Preview to check "
                "the saved Ingest folder and exact files again. No images have been imported."
            )
            notice.show()

    def _choose_first_ingest_folder(self):
        """Use the existing guarded folder editor, then refresh a saved choice."""
        from .folder_locations import open_folder_locations
        previous=str(self.settings.ingest_directory or "").strip()
        open_folder_locations(
            self,focus="ingest_directory",settings=self.settings,focus_group_only=True,
        )
        if str(self.settings.ingest_directory or "").strip()!=previous:
            self.scan()
        else:
            self.render_page()

    def _retry_ingest_import_preview(self):
        pending=self._pending_add_images_request
        if pending is None:return False
        source_kind,paths,preserve_source=pending
        verified_versions=getattr(self,"_pending_add_images_verified_versions",())
        return IngestPage._start_ingest_import_preview(
            self,source_kind,list(paths),preserve_source=preserve_source,
            **({"ordinary_user_selection":True}
               if getattr(self,"_pending_add_images_ordinary",False) is True else {}),
            **({"verified_versions":verified_versions} if verified_versions else {}),
        )

    def _restore_add_images_preview_request(self):
        request=self._active_add_images_preview_request
        self._active_add_images_preview_request=None
        if request is None:
            self._active_add_images_verified_versions=()
            self._active_add_images_ordinary=False
            return False
        self._pending_add_images_request=request
        self._pending_add_images_ordinary=(getattr(self,"_active_add_images_ordinary",False) is True)
        self._active_add_images_ordinary=False
        self._pending_add_images_verified_versions=getattr(
            self,"_active_add_images_verified_versions",(),
        )
        self._active_add_images_verified_versions=()
        if recovery:=getattr(self,"add_images_recovery",None):recovery.show()
        if button:=getattr(self,"retry_add_images_button",None):button.show()
        if notice:=getattr(self,"add_images_location_notice",None):
            notice.setText(
                "Add Images preview stopped. Your exact selection is retained; choose Retry Add Images Preview "
                "for a fresh check. No images were imported."
            ); notice.show()
        return True

    def open_ingest_videos(self):
        return self.open_media_library()

    def open_media_library(self):
        from .media_library_dialog import open_media_library
        root=_configured_ingest_folder(self.settings)
        return open_media_library(self, [("Ingest",root)] if root is not None else [])

    def _start_video_ingest_preview(self,receipt_path,source_path,verified_outputs):
        from core.video_ingest import VideoIngestService
        from core.external_write_policy import ExternalWritePolicy
        service=VideoIngestService(
            verified_outputs,self.state.moves,
            library_registry=self.settings.library_registry(),
            external_write_policy=ExternalWritePolicy.from_settings(self.settings),
        )
        return self._open_video_ingest_dialog(
            service=service,receipt_path=receipt_path,source_path=source_path,
        )

    def _open_video_ingest_dialog(self,**kwargs):
        if getattr(self,"_shutdown_requested",False):return
        current=getattr(self,"_video_ingest_dialog",None)
        if current is not None:
            if kwargs and (
                current.receipt_path!=Path(kwargs["receipt_path"])
                or current.source_path!=Path(kwargs["source_path"])
            ):
                if current.has_active_background_work():
                    current.show(); current.raise_()
                    QMessageBox.information(self,"Video review is busy",
                        "Finish or close the current video operation, then send this verified result to Ingest again.")
                    return
                current.reject()
            else:
                current.show(); current.raise_(); return current
        ingest=_configured_ingest_folder(self.settings)
        if ingest is None or not ingest.is_dir():
            QMessageBox.information(self,"Choose an Ingest folder",
                "Configure an available Ingest folder in Setup before reviewing or adding videos.")
            return
        from .video_ingest_dialog import VideoIngestDialog
        dialog=VideoIngestDialog(self.state,ingest,parent=self,**kwargs)
        self._video_ingest_dialog=dialog
        dialog.background_work_finished.connect(self._notify_background_work_finished)
        def finished(_result):
            if self._video_ingest_dialog is dialog:self._video_ingest_dialog=None
            self._notify_background_work_finished()
        dialog.finished.connect(finished)
        dialog.show()
        return dialog

    def choose_ingest_image_folder(self):
        if self._batch_task is not None: return
        selected=QFileDialog.getExistingDirectory(
            self,"Choose a folder of images to add",self._add_images_start_folder(),
        )
        if selected:
            self._start_ingest_import_preview("folder",[Path(selected)],
                                              ordinary_user_selection=True)

    def _start_ingest_import_preview(self,source_kind: str,paths,*,preserve_source=False,
                                     verified_versions=(),ordinary_user_selection=False) -> bool:
        """Return whether this exact preview was admitted to the Ingest worker."""
        if ordinary_user_selection:
            IngestPage._refresh_saved_add_batch_state(self)
            if self._saved_add_state is not None or self._saved_add_error:
                show_operation_in_progress(self,
                    "Review the saved Add Images batch before choosing another selection")
                return False
        if IngestPage._file_work_blocked(self):
            IngestPage._hold_ingest_import_selection(
                self,source_kind,paths,preserve_source,"Another Ingest action is still running.",
                verified_versions=verified_versions,
                ordinary_user_selection=ordinary_user_selection,
            )
            return False
        ingest=_configured_ingest_folder(self.settings)
        if ingest is None or not ingest.is_dir():
            IngestPage._hold_ingest_import_selection(
                self,source_kind,paths,preserve_source,
                "Choose an available Ingest folder in Setup before adding images.",
                location_required=True,
                verified_versions=verified_versions,
                ordinary_user_selection=ordinary_user_selection,
            )
            return False
        label=(
            f"Previewing {len(paths):,} selected image(s) for Ingest…"
            if source_kind=="files" else
            "Checking the chosen folder for images to add…"
        )
        started,active=self.state.begin_long_operation("ingest",label,resource="io")
        if not started:
            show_operation_in_progress(self,active)
            IngestPage._hold_ingest_import_selection(
                self,source_kind,paths,preserve_source,
                "Another task is using image files right now.",verified_versions=verified_versions,
                ordinary_user_selection=ordinary_user_selection,
            )
            return False
        self._pending_add_images_request=None
        self._pending_add_images_verified_versions=()
        self._pending_add_images_ordinary=False
        if recovery:=getattr(self,"add_images_recovery",None): recovery.hide()
        if button:=getattr(self,"retry_add_images_button",None): button.hide()
        if button:=getattr(self,"choose_ingest_folder_button",None): button.hide()
        if notice:=getattr(self,"add_images_location_notice",None): notice.hide()
        self._active_add_images_preview_request=(source_kind,tuple(Path(path) for path in paths),preserve_source)
        self._active_add_images_verified_versions=tuple(verified_versions)
        self._active_add_images_ordinary=bool(ordinary_user_selection)
        self._batch_completion_context="add_images_preview"
        try:
            registry=self.settings.library_registry()
            planner=IngestImportPlanningService(
                ingest,library_registry=registry,
                destination_profile=registry.profile_for_path(ingest),
                transfers=self.state.moves,
                preserve_source=preserve_source,
                verified_versions=verified_versions,
            )
            def operation(values,progress,should_cancel=lambda:False):
                if source_kind=="folder":
                    return planner.preview_folder(
                        values[0],progress=progress,should_cancel=should_cancel,
                    )
                return planner.preview_files(
                    values,progress=progress,should_cancel=should_cancel,
                )
            self._state_scan_generation+=1
            self.batch_actions.setEnabled(False)
            self.refresh_button.setEnabled(False)
            self.add_images_button.setEnabled(False)
            self.import_external_evidence_button.setEnabled(False)
            self.organize_button.setEnabled(False)
            if controls:=getattr(self,"preparation_controls",None):
                controls.set_busy(True)
            _set_ingest_footer_activity(self,label)
            set_progress_bar(self.batch_progress,detail=label,unknown_text="Starting…")
            self.batch_progress.show()
            self._batch_progress_unit="image"
            self._batch_progress_scope=""
            task=_BatchTask(operation,paths); self._batch_task=task
            task.signals.progress.connect(self._batch_progress)
            task.signals.complete.connect(self._ingest_import_preview_complete)
            task.signals.failed.connect(self._batch_failed)
            self.cancel_batch_button.setText("Cancel Safely")
            self.cancel_batch_button.setEnabled(True)
            self.cancel_batch_button.show()
            self.pool.start(task,-1)
        except Exception as exc:
            self._batch_failed(str(exc))
            return False
        return True

    def _ingest_import_preview_complete(self,result):
        task=self._batch_task
        cancelled=isinstance(task,QRunnable) and task.is_cancelled()
        self._batch_completion_context=""
        self.state.finish_long_operation("ingest")
        self._batch_task=None
        self.batch_actions.setEnabled(True)
        self.refresh_button.setEnabled(True)
        self.add_images_button.setEnabled(True)
        self.import_external_evidence_button.setEnabled(True)
        if controls:=getattr(self,"preparation_controls",None):
            controls.set_busy(False)
        self.batch_progress.hide(); self.cancel_batch_button.hide()
        if getattr(self,"_shutdown_requested",False):
            self._active_add_images_preview_request=None
            self._active_add_images_verified_versions=()
            self._active_add_images_ordinary=False
            self._notify_background_work_finished(); return
        if cancelled or not isinstance(result,IngestImportPlan):
            retained=IngestPage._restore_add_images_preview_request(self)
            self.batch_summary.setText("Add Images preview cancelled safely. Selection retained; choose Retry Add Images Preview for a fresh check."
                if retained else "Add Images preview cancelled safely")
            if retained:self.batch_summary.hide()
            else:self.batch_summary.show()
            self._notify_background_work_finished(); return
        if "The configured Ingest folder is unavailable." in result.errors:
            request=self._active_add_images_preview_request
            self._active_add_images_preview_request=None
            verified_versions=self._active_add_images_verified_versions
            self._active_add_images_verified_versions=()
            ordinary=self._active_add_images_ordinary
            self._active_add_images_ordinary=False
            if request is not None:
                source_kind,paths,preserve_source=request
                IngestPage._hold_ingest_import_selection(
                    self,source_kind,paths,preserve_source,
                    "The Ingest folder became unavailable while checking these images.",
                    location_required=True,
                    verified_versions=verified_versions,
                    ordinary_user_selection=ordinary,
                )
                self.batch_summary.setText(
                    "Ingest folder unavailable. Selection retained; choose Ingest Folder, then Retry Add Images Preview."
                )
                self.batch_summary.hide()
            self._notify_background_work_finished(); return
        request=self._active_add_images_preview_request
        self._active_add_images_preview_request=None
        verified_versions=self._active_add_images_verified_versions
        self._active_add_images_verified_versions=()
        ordinary=self._active_add_images_ordinary
        self._active_add_images_ordinary=False
        dialog=IngestImportPreviewDialog(result,self)
        if dialog.exec()!=QDialog.Accepted:
            if request is not None and request[2]:
                source_kind,paths,preserve_source=request
                IngestPage._hold_ingest_import_selection(
                    self,source_kind,paths,preserve_source,
                    "The saved result was not copied during Ingest review.",
                    verified_versions=verified_versions,
                    ordinary_user_selection=ordinary,
                )
            self._notify_background_work_finished(); return
        self._apply_ingest_import(
            result,dialog.action(),dialog.boundary_confirmed(),
            ordinary_user_selection=ordinary,
        )
        self._notify_background_work_finished()

    def _apply_ingest_import(self,plan,action,boundary_confirmed,*,ordinary_user_selection=False):
        if IngestPage._file_work_blocked(self):return
        items=plan.ready(action)
        if not items: return
        if ordinary_user_selection:
            executor=getattr(self,"_saved_add_executor",None) or AddImagesBatchExecutor(self.state.moves)
            def operation(paths,progress,should_cancel=lambda:False):
                try:
                    result=executor.run_initial(plan,action,
                        origin="ordinary_folder" if plan.preserves_subfolders else "ordinary_files",
                        boundary_confirmed=boundary_confirmed,progress=progress,
                        should_pause=should_cancel)
                except InterruptedError:
                    return {"processed":0,"skipped":0,"errors":[],"added":[],
                        "summary_lines":[
                            "Paused before Add Images began. No images were added or saved in a batch.",
                            "Choose Add Images again to review the files before starting.",
                        ]}
                return _saved_add_result(result,
                    skipped=len(plan.duplicates)+len(plan.unsupported))
            self._run_batch(
                operation,
                "Copying verified image families into Ingest…" if action=="copy"
                else "Moving verified image families into Ingest…",
                [item.source for item in items],
                completion_context="add_images_saved_apply",progress_unit="image",
                pause_after_current=True,
            )
            return
        by_source={item.source:item for item in items}
        destination_profile=plan.destination_profile
        group_id="ingest-add-"+uuid4().hex
        def operation(paths,progress,should_cancel=lambda:False):
            processed=0; errors=[]; added=[]; cancelled=False;records=[];recovery_id="";recovery_required=False
            recovery_not_attempted=0
            for index,path in enumerate(paths,1):
                if should_cancel():
                    cancelled=True; break
                item=by_source[path]
                transfer=item.transfer_for(action)
                try:
                    from core.media_inventory import inspect_media
                    media=inspect_media(path,verify_pixels=True)
                    if not media.image_ready:
                        raise ValueError(f"{media.status}: {media.detail}")
                    arguments=dict(
                        relative_to=None,
                        reason=(
                            "Add Images: Copy to Ingest"
                            if action=="copy" else "Add Images: Move to Ingest"
                        ),
                        preview=transfer,collision_policy="error",
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
                        group_id=group_id,
                    )
                    if action=="copy":
                        record=self.state.moves.copy(path,item.destination,**arguments)
                    else:
                        record=self.state.moves.move(path,item.destination,**arguments)
                    records.append(record);added.append(record.files[0]["to"]); processed+=1
                except InterruptedError:
                    cancelled=True; break
                except TransferRecoveryRequired as exc:
                    recovery_required=True;recovery_id=getattr(exc,"operation_id","")
                    recovery_not_attempted=len(paths)-index
                    errors.append(f"{path.name}: {exc}");break
                except Exception as exc:
                    errors.append(f"{path.name}: {exc}")
                progress(index,len(paths),path.name)
            verb="Copied" if action=="copy" else "Moved"
            return {
                **_organization_result(group_id,records,action="add",recovery_operation_id=recovery_id),
                "processed":processed,
                "skipped":len(plan.duplicates)+len(plan.unsupported),
                "errors":errors,"added":added,"cancelled":cancelled,"recovery_required":recovery_required,
                "recovery_not_attempted":recovery_not_attempted,
                "summary_lines":[
                    *(["Add Images stopped. Check for an interrupted image before retrying."] if cancelled else []),
                    f"{verb} {processed:,} image family(s) into Ingest.",
                    *(["Completed images remain; use a fresh preview for any further Add Images work."] if cancelled else []),
                    f"Skipped {len(plan.duplicates):,} exact duplicate(s) and {len(plan.unsupported):,} unsupported entry or entries.",
                    "Imported images are ready for ordinary Ingest preparation; no evidence was confirmed or rewritten by Add Images.",
                ],
            }
        self._run_batch(
            operation,
            "Copying verified image families into Ingest…"
            if action=="copy" else
            "Moving verified image families into Ingest…",
            [item.source for item in items],
            completion_context="add_images_apply",
            progress_unit="image",
        )

    @staticmethod
    def _evidence_availability(status) -> tuple[bool,str]:
        reason=" ".join(
            value for value in (
                str(getattr(status,"detail","") or ""),
                str(getattr(status,"recovery","") or ""),
            ) if value
        )
        return bool(getattr(status,"ready_for_write",False)),reason

    def open_external_evidence_import(self) -> None:
        if self._scan_task is not None or self._batch_task is not None:
            show_operation_in_progress(self,"Ingest folder scan or image action")
            return
        root=_configured_ingest_folder(self.settings)
        if root is None or not root.is_dir() or not self.images:
            QMessageBox.information(
                self,"No incoming images",
                "Choose and recheck an Ingest folder with one or more images first.",
            )
            return
        filtered=tuple(self._available_paths()[0])
        def availability_many(paths):
            values=LibraryStorageInspector().inspect_images(
                paths,detect_conflicts=True,
            )
            return {
                Path(path):self._evidence_availability(status)
                for path,status in values.items()
            }
        dialog=ExternalEvidenceImportDialog(
            self.state,self,context_label="Ingest",
            all_paths=self.images,roots=(root,),selected=self.selected_paths(),
            filtered=filtered,
            availability_many=availability_many,
            whole_scope_label="All incoming images in the Ingest folder",
            checkpoint_service=ExternalEvidenceImportCheckpointService(
                OperationJournal(USER_DATA_DIR/"operation_journal.jsonl"),
                rules_fingerprint_provider=lambda:
                    ImageAuthorityInitializationService(
                        knowledge=self.state.knowledge,
                    ).current_rules_fingerprint(),
            ),
        )
        if dialog.exec()!=QDialog.Accepted or dialog.plan is None:
            return
        self._start_external_evidence_import(dialog.plan)

    def _start_external_evidence_import(self,plan) -> None:
        current={str(Path(path).resolve()).casefold() for path in self.images}
        missing=[
            path for path in plan.ready_paths
            if str(Path(path).resolve()).casefold() not in current
        ]
        if missing:
            QMessageBox.information(
                self,"External evidence scope is unavailable",
                f"{len(missing):,} previewed image(s) no longer belong to the current "
                "Ingest inventory. Nothing was imported and the scope was not broadened.",
            )
            return
        # Freeze each bounded native operation before any checkpoint or write.
        cancellation={"check":lambda:False}
        operations={}
        try:
            ready=tuple(plan.ready_paths)
            for start in range(0,len(ready),64):
                chunk=ready[start:start+64]
                operations[chunk]=capture_operation(
                    self,chunk,operation_kind="external_evidence_import",
                    cancelled=lambda:cancellation["check"](),
                )
        except (OSError,ValueError,RuntimeError) as exc:
            QMessageBox.warning(self,"Saved history needs attention",str(exc))
            return
        native=any(operation is not None for operation in operations.values())
        if native and any(operation is None for operation in operations.values()):
            QMessageBox.warning(self,"Saved history needs attention","This selection mixes sources with and without saved history. Narrow the import scope or review History locations in Setup.")
            return
        def operation(paths,progress,should_cancel=lambda:False):
            cancellation["check"]=should_cancel
            if not plan.is_current():
                raise RuntimeError(
                    "The frozen external-evidence scope changed before work started. "
                    "Nothing was imported; build the preview again."
                )
            availability=LibraryStorageInspector().inspect_images(
                paths,detect_conflicts=True,
            )
            newly_held=[
                path for path in paths
                if not self._evidence_availability(availability.get(path))[0]
            ]
            if newly_held:
                raise RuntimeError(
                    f"{len(newly_held):,} previewed image(s) became unavailable, "
                    "read-only, or conflicted. Nothing was imported; restore access "
                    "and build the preview again."
                )
            journal=OperationJournal(USER_DATA_DIR/"operation_journal.jsonl")
            checkpoints=ExternalEvidenceImportCheckpointService(
                journal,rules_fingerprint_provider=lambda:
                    ImageAuthorityInitializationService(
                        knowledge=self.state.knowledge,
                    ).current_rules_fingerprint(),
            )
            session=None if native else checkpoints.begin(plan)
            try:
                initialized=ImageAuthorityInitializationService(
                    knowledge=self.state.knowledge,journal=None if native else journal,
                    gather_source_evidence=True,refresh_external_evidence=True,
                    external_evidence_mode=plan.mode,
                    external_evidence_sources=plan.sources,
                    trusted_existing={
                        *session.completed_paths,*session.current_paths,
                    } if session is not None else (),
                ).initialize_many(
                    paths,progress=progress,should_cancel=should_cancel,
                    on_external_evidence_complete=lambda path,_record,counts:
                        checkpoints.checkpoint(
                            session,path,counts=counts,
                        ) if session is not None else None,
                    history_operation_factory=(lambda images:operations[tuple(images)]) if native else None,
                )
            except Exception as exc:
                if session is not None:
                    checkpoints.finish(
                        session,"failed",message=str(exc),
                        result={"resumed":session.resumed},
                    )
                raise
            issues=[
                f"{item.image.name}: {item.message} {item.recovery}".strip()
                for item in initialized.issues
            ]
            paused=(initialized.cancelled or should_cancel())
            checkpoint_status=(
                "cancelled" if paused else
                "failed" if initialized.failed else
                "complete"
            )
            evidence_counts=(
                (session.completed_counts if session is not None else ExternalEvidenceImportCounts())+initialized.evidence_counts
            )
            current_reused=max(
                0,initialized.reused-(len(session.completed_paths) if session is not None else 0)
                -initialized.evidence_counts.unchanged_images,
            )
            result_lines=evidence_counts.summary_lines(
                current_reused=current_reused,
                recovered=len(session.completed_paths) if session is not None else 0,
                held=plan.held_count,failed=initialized.failed,
            )
            if session is not None:checkpoints.finish(
                session,checkpoint_status,
                message=(
                    "Paused safely; rebuild the same exact preview to resume."
                    if paused else "External evidence import completed."
                    if not initialized.failed else
                    "External evidence import finished with one or more image problems."
                ),
                result={
                    "resumed":session.resumed,
                    "resume_count":len(session.completed_paths),
                    "created":initialized.created,
                    "refreshed":initialized.refreshed,
                    "reused":initialized.reused,
                    "failed":initialized.failed,
                    **evidence_counts.to_dict(),
                },
            )
            return {
                "processed":initialized.created+initialized.refreshed,
                "skipped":initialized.reused+plan.held_count,
                "errors":issues,
                "external_evidence_result":{
                    "outcome":checkpoint_status,"scope_label":plan.scope_label,
                    "updated":initialized.created+initialized.refreshed,
                    "counts":evidence_counts,"current_reused":current_reused,
                    "recovered":len(session.completed_paths) if session is not None else 0,
                    "held":plan.held_count,"failed":initialized.failed,
                },
                "summary_lines":[
                    (
                        "Paused safely; completed evidence imports were kept."
                        if paused else
                        f"Imported current external evidence for {initialized.created+initialized.refreshed:,} image(s)."
                    ),
                    *result_lines,
                    "Evidence remains attributed and reviewable; confirmed facts were unchanged, and TXT, Metadata, and image files were not rewritten by this job.",
                ],
            }
        self._run_batch(
            operation,
            f"Importing external evidence for {len(plan.ready_paths):,} incoming image(s)…",
            plan.ready_paths,completion_context="external_evidence",
            progress_unit="image",progress_scope=plan.scope_label,
        )

    def activate(self,*,automatic=False):
        """Populate Ingest on first visit, or after its configured folder changes."""
        path=_configured_ingest_folder(self.settings)
        if path is None:
            if self.images or self._loaded_folder:
                self.images=[]; self.selected.clear(); self.page=0
                self._loaded_folder=""; self._state_cache={}
                self._known_record_paths=set(); self._trusted_workflow_paths=set()
                self._workflow_index=None
                self.render_page()
            return
        key=str(path.resolve()) if path.is_dir() else ""
        if key and key!=self._loaded_folder:
            self.scan(automatic=automatic,full_readiness=False)

    def _workflow_cache_payload(self,workflow):
        payload=workflow.to_cache_dict()
        provider=getattr(self,"_workflow_context_fingerprint",None)
        if callable(provider):
            payload["automatic_policy_fingerprint"]=provider(workflow_context="policy-only")
        return payload

    def _semantic_workflow_options(self):
        factory=getattr(getattr(self,"workflow_states",None),"semantic_validator",None)
        provider=getattr(self,"_workflow_context_fingerprint",None)
        if not callable(factory) or not callable(provider):return {}
        validator=factory();policy=provider(workflow_context="policy-only")
        return {"context_validator":lambda payload:(
            payload.get("automatic_policy_fingerprint")==policy and validator(payload))}

    def _workflow_context_fingerprint(self,*,workflow_context=None) -> str:
        """Bind trusted Ingest state to rules and automatic preparation policy."""
        keys=(
            "auto_import_source_evidence","auto_run_wd_on_managed_images",
            "auto_run_joy_on_managed_images","auto_merge_ai_tags",
            "enabled_wd_taggers","wd_general_threshold",
            "wd_character_threshold","wd_rating_threshold",
            "wd_consensus_minimum","plain_language_model",
            "joycaption_prompt","joycaption_quantization",
            "joycaption_do_sample","joycaption_max_new_tokens",
        )
        payload={
            "workflow":workflow_context or self.workflow_states.context_fingerprint(),
            "automatic_policy":{
                key:getattr(self.settings,key,None) for key in keys
            },
        }
        encoded=json.dumps(
            payload,sort_keys=True,separators=(",",":"),
            ensure_ascii=False,default=str,
        )
        return hashlib.sha256(encoded.encode("utf-8")).hexdigest()

    def scan(self,_checked=False,*,automatic=False,full_readiness=True):
        if self._scan_task is not None or self._batch_task is not None: return
        # A folder recheck is the explicit external-change boundary. Retire the
        # prior projection when the duplicate service is present, including
        # when a previously loaded folder has since become unconfigured.
        duplicates=getattr(getattr(self,"state",None),"duplicates",None)
        if duplicates is not None:
            duplicates.invalidate_badges(getattr(self,"images",()))
        path=_configured_ingest_folder(self.settings)
        if path is None or not path.is_dir():
            if path is None and (self.images or self._loaded_folder):
                self.images=[]; self.selected.clear(); self.page=0
                self._loaded_folder=""; self._state_cache={}
                self._known_record_paths=set(); self._trusted_workflow_paths=set()
                self._workflow_index=None
                self.render_page()
            if not automatic:
                QMessageBox.information(
                    self,"Choose an Ingest folder",
                    "Choose the folder where new images arrive first.",
                )
            return
        if self._state_scan_task is not None:
            self._state_scan_generation+=1
            self._state_scan_restart_requested=False
            self._state_scan_restart_paths.clear()
            self.state.jobs.cancel(self._state_scan_task.job_id)
        self.refresh_button.setEnabled(False)
        if button:=getattr(self,"add_images_button",None): button.setEnabled(False)
        if button:=getattr(self,"import_external_evidence_button",None): button.setEnabled(False)
        _set_ingest_footer_activity(self,"Scanning incoming images…")
        set_progress_bar(self.state_progress,unknown_text="Scanning…"); self.state_progress.show()
        workflow_states=getattr(self,"workflow_states",None)
        context_provider=getattr(self,"_workflow_context_fingerprint",None)
        context_fingerprint=(
            context_provider()
            if callable(context_provider) else
            workflow_states.context_fingerprint()
            if workflow_states is not None else ""
        )
        compatible=getattr(workflow_states,"compatible_context_fingerprints",None)
        legacy_contexts=tuple(
            IngestPage._workflow_context_fingerprint(self,workflow_context=value)
            for value in compatible()
        ) if callable(compatible) else ()
        cache_holder={"index":None,"states":{},"record_paths":set(),"trusted_paths":set()}
        def work(job):
            semantic_options=IngestPage._semantic_workflow_options(self)
            images=[]; record_paths=set()
            for image in DatasetScope(path,include_subfolders=True).iter_images():
                if not job.checkpoint():
                    raise InterruptedError("Ingest folder scan cancelled safely")
                images.append(image)
                if image.with_suffix(".tags.json").is_file():
                    record_paths.add(image)
                if len(images)%25==0:
                    job.update(len(images),0,image.name)
            if context_fingerprint:
                try:
                    workflow_index=DatasetIndex(path)
                    payloads=workflow_index.validated_workflow_states(
                        images,context_fingerprint=context_fingerprint,
                        cache_scope="ingest",should_continue=job.checkpoint,**semantic_options,
                    )
                    for old_context in legacy_contexts:
                        missing=set(images)-set(payloads)
                        if missing:
                            payloads.update(workflow_index.validated_workflow_states(
                                missing,context_fingerprint=old_context,cache_scope="ingest",
                            ))
                    restored={}
                    validator=getattr(workflow_states,"cached_rating_matches",None)
                    reader=getattr(workflow_index,"workflow_confirmed_tags",None)
                    legacy=[path for path,payload in payloads.items() if "rating_tags" not in payload]
                    rating_inputs=reader(legacy,cache_scope="ingest",recover_legacy=True) if legacy and callable(reader) else {}
                    for image,payload in payloads.items():
                        if callable(validator) and not validator(payload,rating_inputs.get(image)):
                            continue
                        workflow=ImageWorkflowState.from_cache_dict(payload)
                        if workflow is not None:
                            restored[image]=IngestPage._state_cache_value(
                                (None,None),workflow,has_record=True,
                            )
                    cache_holder["index"]=workflow_index
                    cache_holder["states"]=restored
                    cache_holder["trusted_paths"]={
                        image for image,payload in payloads.items()
                        if image in restored
                        and payload.get("automatic_preparation_complete") is True
                    }
                except (OSError,sqlite3.DatabaseError):
                    # A disposable cache failure must not prevent Ingest from
                    # opening or change any portable Image Knowledge.
                    pass
            cache_holder["record_paths"]=record_paths
            job.update(len(images),len(images),"Incoming folder scan complete")
            return images
        task=CoordinatedCallTask(
            self.state.jobs,"Scan Ingest folder",work,resource="io",
            key=f"ingest-inventory:{str(path.resolve()).casefold()}",
        ); self._scan_task=task
        task.signals.progress.connect(self._incoming_scan_progress)
        task.signals.complete.connect(
            lambda images,owner=task,root=path,automatic=automatic,full=full_readiness,cache=cache_holder:
            self._incoming_scan_complete(
                owner,root,images,automatic=automatic,full_readiness=full,
                persisted=cache,context_fingerprint=context_fingerprint,
            )
        )
        task.signals.failed.connect(
            lambda message,owner=task,automatic=automatic:
            self._incoming_scan_failed(owner,message,automatic=automatic)
        )
        task.signals.cancelled.connect(
            lambda owner=task:self._incoming_scan_cancelled(owner)
        )
        try:task.start()
        except RuntimeError as error:
            self._incoming_scan_failed(task,str(error),automatic=automatic)

    def _incoming_scan_progress(self,current,total,name):
        detail=(
            str(name) if total else
            f"Found {current:,} incoming image(s)"+(f": {name}" if name else "")
        )
        set_progress_bar(
            self.state_progress,current,total,detail=detail,
            unknown_text=f"Found {current:,}…",
        )
        _set_ingest_footer_activity(
            self,f"Scanning incoming images • {current:,} found",
        )

    def _incoming_scan_complete(
        self,task,root,images,*,automatic=False,full_readiness=True,
        persisted=None,context_fingerprint="",
    ):
        if task is not self._scan_task:return
        self._scan_complete(
            root,images,automatic=automatic,full_readiness=full_readiness,
            persisted=persisted,context_fingerprint=context_fingerprint,
        )

    def _incoming_scan_failed(self,task,message,*,automatic=False):
        if task is not self._scan_task:return
        self._scan_failed(message,automatic=automatic)

    def _incoming_scan_cancelled(self,task):
        if task is not self._scan_task:return
        self._scan_task=None; self.refresh_button.setEnabled(True)
        if button:=getattr(self,"add_images_button",None): button.setEnabled(True)
        if button:=getattr(self,"import_external_evidence_button",None): button.setEnabled(True)
        self.state_progress.hide()
        if not self.state.jobs.shutting_down:
            _set_ingest_footer_activity(self,"Incoming folder scan paused safely.")

    def _scan_complete(
        self,root,images,*,automatic=False,full_readiness=True,
        persisted=None,context_fingerprint="",
    ):
        self._scan_task=None; self.refresh_button.setEnabled(True)
        if button:=getattr(self,"add_images_button",None): button.setEnabled(True)
        if button:=getattr(self,"import_external_evidence_button",None): button.setEnabled(True)
        self.state_progress.hide()
        restored=dict((persisted or {}).get("states",{}))
        self.images=list(images); self._loaded_folder=str(root.resolve())
        self._state_cache={
            path:value for path,value in restored.items() if path in self.images
        }
        self._workflow_index=(persisted or {}).get("index")
        # A read-only status check does not prove configured source/AI work ran.
        # Older rows without this marker remain useful for display, but their
        # preparation eligibility must be checked once through the usual path.
        self._trusted_workflow_paths=(
            set((persisted or {}).get("trusted_paths",())) & set(self._state_cache)
        )
        self._known_record_paths=set((persisted or {}).get("record_paths",()))
        self._readiness_context_fingerprint=str(context_fingerprint or "")
        _clear_ingest_footer_activity(self,"Scanning incoming images")
        self.selected.intersection_update(self.images); self.page=0; self.render_page()
        IngestPage._set_move_undo_available(self)
        if self.images:
            if automatic:
                self._start_managed_ingest(automatic=True)
            elif full_readiness:
                # Recheck is an inventory/readiness action. Full source import,
                # authoritative initialization, and AI belong to the explicit
                # Prepare All/Selected workflow so opening this page cannot silently
                # launch hours of work.
                self._start_state_scan([
                    path for path in self.images if path not in self._state_cache
                ])
        else:
            self._set_attention_report(IngestAttentionReport(Path(root)),automatic)
            self._start_state_scan()

    def _start_managed_ingest(self,*,automatic=False):
        settings=self.settings
        workflow_index=getattr(self,"_workflow_index",None)
        context_fingerprint=str(
            getattr(self,"_readiness_context_fingerprint","") or ""
        )
        trusted=set(getattr(self,"_trusted_workflow_paths",()))
        retained={
            path:value for path,value in getattr(self,"_state_cache",{}).items()
            if path in trusted
        }
        workflow_state_service=getattr(self,"workflow_states",None)
        def operation(paths,progress,should_cancel=lambda:False):
            workflow_states=dict(retained)
            workflow_projections={}
            workflow_expected={}
            automatic_preparation_complete=False

            def persist_projections():
                if (
                    workflow_index is None or not context_fingerprint
                    or not workflow_projections
                ):
                    return
                try:
                    workflow_index.record_workflow_states(
                        {
                            image:{
                                **IngestPage._workflow_cache_payload(self,workflow),
                                "automatic_preparation_complete":automatic_preparation_complete,
                            }
                            for image,workflow in workflow_projections.items()
                        },
                        context_fingerprint=context_fingerprint,
                        expected_fingerprints=workflow_expected,
                        cache_scope="ingest",
                    )
                except (OSError,sqlite3.DatabaseError):
                    # Readiness remains correct in memory; this rebuildable
                    # optimization can safely be repopulated on a later pass.
                    pass
                finally:
                    for image,expected in workflow_expected.items():
                        if DatasetIndex.fingerprint(image)!=expected:
                            workflow_states.pop(image,None)
                            workflow_projections.pop(image,None)

            def remember_workflow_state(image,record,derived):
                if workflow_index is None:
                    workflow_states[image]=self._compute_state_from_record(
                        record,derived_result=derived,
                    )
                    return
                before=DatasetIndex.fingerprint(image)
                workflow=workflow_state_service.assess_record(
                    record,include_output_health=False,derived_result=derived,
                )
                if before!=DatasetIndex.fingerprint(image):
                    return
                workflow_states[image]=IngestPage._state_cache_value(
                    (None,None),workflow,has_record=True,
                )
                workflow_projections[image]=workflow
                workflow_expected[image]=before
            initializer=ImageAuthorityInitializationService(
                knowledge=self.state.knowledge,
                journal=OperationJournal(USER_DATA_DIR/"operation_journal.jsonl"),
                gather_source_evidence=settings.auto_import_source_evidence,
                trusted_existing=trusted,
            )
            initialized=initializer.initialize_many(
                paths,progress=progress,should_cancel=should_cancel,
                on_cache_complete=remember_workflow_state,
                history_operation_factory=lambda images:capture_operation(
                    self,images,operation_kind="image_authority_initialization",cancelled=should_cancel),
            )
            initialization_issues=[
                item
                for item in initialized.issues
                if item.blocking or item.code=="metadata_unavailable"
            ]
            if initialized.cancelled or initialized.history_interrupted:
                persist_projections()
                return {
                    "processed":initialized.created+initialized.refreshed,
                    "skipped":initialized.reused,"errors":[],
                    "attention_report":IngestAttentionReport.build(
                        Path(settings.ingest_directory),initialization_issues,
                    ),
                    "workflow_states":workflow_states,
                    "automatic_prepared_paths":tuple(trusted),
                    "skip_followup_state_scan":True,
                    "preserve_existing_attention":True,
                    "summary_lines":[
                        "Paused safely; completed authoritative image records were kept.",
                        ("Saved history requires recovery. Reopen the affected image in its data editor before scanning again. Automatic AI did not start."
                         if initialized.history_interrupted else
                         "Choose Scan Again to resume the remaining incoming images."),
                    ],
                }
            automatic=AutomaticAIAnalysisService(
                settings,knowledge=self.state.knowledge,ai=self.state.ai,
                ai_work_lease=self.state.ai_work_lease,
            ).analyze_many(
                [path for path in paths if path not in trusted
                 and path not in {item.image for item in initialization_issues if item.blocking}],
                progress=progress,should_cancel=should_cancel,
                history_operation_factory=lambda images:capture_operation(
                    self,images,operation_kind="automatic_image_ai",cancelled=should_cancel),
            )
            # AI writes can change pending-review state. Reclassify only those
            # records; reused AI results retain the state already calculated
            # from the initializer's in-memory record.
            for image in automatic.changed_paths:
                if workflow_index is None:
                    workflow_states[image]=self._compute_state(image)
                else:
                    before=DatasetIndex.fingerprint(image)
                    workflow=workflow_state_service.assess(
                        image,include_output_health=False,
                    )
                    if before!=DatasetIndex.fingerprint(image):
                        workflow_states.pop(image,None)
                        workflow_projections.pop(image,None)
                        workflow_expected.pop(image,None)
                        continue
                    workflow_states[image]=IngestPage._state_cache_value(
                        IngestPage._state_signatures(image),workflow,
                        has_record=True,
                    )
                    workflow_projections[image]=workflow
                    workflow_expected[image]=before
            automatic_preparation_complete=(
                not initialized.cancelled and not initialized.failed
                and not automatic.cancelled and not automatic.errors
                and not should_cancel()
            )
            persist_projections()
            attention_report=IngestAttentionReport.build(
                Path(settings.ingest_directory),initialization_issues,
                automatic_errors=automatic.errors,
            )
            return {
                "processed":initialized.created+initialized.refreshed+automatic.processed,
                "skipped":initialized.reused+automatic.skipped,"errors":[],
                "workflow_states":workflow_states,
                "automatic_prepared_paths":tuple(workflow_states) if automatic_preparation_complete else tuple(trusted),
                "skip_followup_state_scan":True,
                "attention_report":attention_report,
                "summary_lines":[
                    f"Created {initialized.created:,} and refreshed {initialized.refreshed:,} authoritative image record(s).",
                    f"Automatic AI completed {automatic.processed:,} pass(es) and skipped {automatic.skipped:,} current pass(es).",
                    (
                        "Paused safely; completed Metadata and AI findings were kept."
                        if automatic.cancelled else
                        "Metadata and AI findings remain unconfirmed until reviewed."
                    ),
                ],
            }
        self._run_batch(
            operation,"Initializing incoming image data",self.images,
            completion_context="startup_managed" if automatic else "foreground_managed",
        )

    def _scan_failed(self,message,*,automatic=False):
        self._scan_task=None; self.refresh_button.setEnabled(True)
        if button:=getattr(self,"add_images_button",None): button.setEnabled(True)
        if button:=getattr(self,"import_external_evidence_button",None): button.setEnabled(True)
        self.state_progress.hide()
        _set_ingest_footer_activity(self,"Incoming images could not be scanned.")
        if automatic:
            report=IngestAttentionReport(
                Path(self.settings.ingest_directory),
                (IngestAttentionItem(
                    None,"initialization_failed",str(message),
                    "Restore access to the configured Ingest folder, then choose Scan Again.",
                ),),
            )
            self._set_attention_report(report,True)
            return
        QMessageBox.critical(self,"Could not read Ingest folder",message)

    def _set_attention_report(
        self,report: IngestAttentionReport,automatic: bool,
    ) -> None:
        self._attention_report=report
        self._set_attention_review_outcome("")
        if report.items:
            self.attention_review.show()
            summary=report.detailed_summary()
            self.attention_summary.setText(summary)
            self.attention_summary.setAccessibleDescription(summary)
            self.attention_panel.setAccessibleDescription(summary)
            self.attention_panel.show()
        else:
            self.attention_summary.clear()
            self.attention_summary.setAccessibleDescription("")
            self.attention_panel.setAccessibleDescription(
                "No Ingest issues remain."
            )
            self.attention_review.hide()
            self.attention_panel.hide()
        IngestPage._sync_empty_inventory_presentation(self)
        self.attention_changed.emit(report,bool(automatic))

    def _attention_items_for_path(
        self,path: str | Path,
    ) -> tuple[IngestAttentionItem, ...]:
        return self._attention_report.items_for(path)

    def _set_attention_review_outcome(self,message: str) -> None:
        message=str(message or "").strip()
        self.attention_review_outcome.setText(message)
        self.attention_review_outcome.setAccessibleDescription(message)
        self.attention_review_outcome.setVisible(bool(message))
        if not message:
            return
        self.attention_panel.setAccessibleDescription(
            self._attention_report.detailed_summary()+"\n"+message
        )
        self.attention_panel.show()
        if self.attention_review.isVisible():
            self.attention_review.setFocus()
        announce_accessibly(self.attention_review_outcome,message)

    def _attention_work_active(self) -> bool:
        return any(
            getattr(self,name,None) is not None
            for name in ("_scan_task","_batch_task","_state_scan_task")
        )

    def review_attention(self):
        if not self._attention_report.items:
            self.attention_summary.setText("No Ingest issues remain.")
            self.attention_review.hide()
            self._set_attention_review_outcome(
                "A completed recheck cleared the previous attention report. "
                "No image data or raw evidence was changed by opening this review."
            )
            return
        self.attention_review.show()
        stage_tabs=getattr(self,"stage_tabs",None)
        if stage_tabs is not None:
            partial_index=next((
                index for index in range(stage_tabs.count())
                if stage_tabs.tabData(index)=="partial"
            ),-1)
            if partial_index>=0:
                stage_blocker=QSignalBlocker(stage_tabs)
                stage_tabs.setCurrentIndex(partial_index)
                del stage_blocker
        problem_index=self.state_filter.findData("problem")
        if problem_index>=0:
            blocker=QSignalBlocker(self.state_filter)
            self.state_filter.setCurrentIndex(problem_index)
            del blocker
        image_set=set(self.images)
        report_paths=self._attention_report.image_paths
        current_paths=tuple(path for path in report_paths if path in image_set)
        visible_paths,hidden_count=filter_content_paths(self.images)
        visible_set=set(visible_paths)
        reviewable_paths=tuple(
            path for path in current_paths if path in visible_set
        )
        if not reviewable_paths:
            self.page=0
            self.render_page()
            if current_paths and hidden_count:
                message=(
                    f"{len(current_paths):,} affected incoming image"
                    f"{' is' if len(current_paths)==1 else 's are'} not directly "
                    "previewable under the current content-safety presentation. "
                    "The attention report remains available. Adjust the existing "
                    "content-safety view deliberately or use its listed restore/"
                    "recheck action; no image data was changed."
                )
            elif report_paths and self._attention_work_active():
                message=(
                    "Review is waiting for the current Ingest scan or image-state "
                    f"classification to finish. {len(report_paths):,} reported image "
                    f"path{' is' if len(report_paths)==1 else 's are'} not yet in "
                    "the current Ingest view. The report remains available and no "
                    "duplicate scan was started."
                )
            elif report_paths:
                message=(
                    f"{len(report_paths):,} affected image"
                    f"{' is' if len(report_paths)==1 else 's are'} no longer in the "
                    "current Ingest view. The report remains available. Restore "
                    "access or return to the configured Ingest folder, then choose "
                    "Scan Again; no Image Knowledge or raw evidence "
                    "was changed."
                )
            else:
                recovery=next(
                    (
                        group.recovery for group in self._attention_report.groups
                        if group.recovery
                    ),
                    "Restore the affected capability, then choose Scan Again.",
                )
                message=(
                    "This attention report describes an Ingest task failure rather "
                    "than an individual image, so there is no image card to open. "
                    f"Next: {recovery} The report remains available and no duplicate "
                    "work was started."
                )
            self._set_attention_review_outcome(message)
            return
        first=reviewable_paths[0]
        self._set_attention_review_outcome("")
        available,_,_=self._available_paths()
        if first in available:
            size=max(12,int(self.settings.gallery_page_size))
            self.page=available.index(first)//size
        self.render_page()
        self._show_attention_issue(first)

    def _show_attention_issue(self,path: str | Path):
        path=Path(path)
        items=self._attention_items_for_path(path)
        if not items:
            return
        self.current_path=path
        self.current_record=None
        self.current_report=None
        self.review_panel.show()
        display=self._attention_report.display_name_for(path)
        self.image_name.setText(display)
        for card_path,card in self.cards.items():
            card.set_active_selected(card_path==str(path))
        lines=[
            "CollectiGen left this image and its existing data unchanged.",
        ]
        for item in items:
            label=next(
                (
                    group.label for group in self._attention_report.groups
                    if group.code==item.code and group.recovery==item.recovery
                ),
                item.code.replace("_"," ").title(),
            )
            lines.extend((f"{label}: {item.message}",f"Next: {item.recovery}"))
        self.recommendation.setText("\n".join(lines))
        self.sdxl_summary.setText(
            "Unavailable until the existing Image Knowledge record can be read."
        )
        self.plain_summary.setText(
            "Unavailable until the existing Image Knowledge record can be read."
        )
        self.source_summary.setText(
            "No image, TXT, Metadata, raw evidence, or malformed Image Knowledge "
            "was rewritten merely to report this issue."
        )
        self.inline_suggestions.clear()
        self.inline_suggestions.hide()
        self.additional_label.hide()
        self.editor_hint.setText(
            "Repair or restore the named record outside this review, then recheck "
            "the Ingest folder."
        )
        self._set_quick_review_count(0)
        self._set_summary_action()
        self.recommended_action=""
        self.recommended_button.hide()
        self._queue_responsive_layout()

    def current_stage(self) -> str:
        tabs=getattr(self,"stage_tabs",None)
        if tabs is None:
            return "all"
        return str(tabs.tabData(tabs.currentIndex()) or "unprepared")

    @staticmethod
    def _stage_filter_options(stage: str):
        if stage=="partial":
            return (
                ("All partially prepared","all"),
                ("Image Knowledge incomplete","incomplete"),
                ("AI findings to review","ai"),
                ("Needs fact review","review"),
                ("Problems","problem"),
            )
        if stage=="complete":
            return (("All complete images","all"),)
        return (("All unprepared images","all"),)

    def _sync_state_filter_options(self) -> None:
        stage=IngestPage.current_stage(self)
        options=IngestPage._stage_filter_options(stage)
        current=str(self.state_filter.currentData() or "all")
        blocker=QSignalBlocker(self.state_filter)
        self.state_filter.clear()
        for label,key in options:
            self.state_filter.addItem(label,key)
        index=self.state_filter.findData(current)
        self.state_filter.setCurrentIndex(max(0,index))
        del blocker
        self.state_filter_control.setVisible(
            bool(self.images) and len(options)>1
        )
        self._sync_state_filter_emphasis()
        if hasattr(self,"filter_panel"):
            self.filter_panel.sync()

    def _sync_state_filter_emphasis(self) -> None:
        active=str(self.state_filter.currentData() or "all")!="all"
        if bool(self.state_filter_control.property("active"))==active:
            return
        self.state_filter_control.setProperty("active",active)
        style=self.state_filter_control.style()
        style.unpolish(self.state_filter_control)
        style.polish(self.state_filter_control)

    def _stage_changed(self,*_) -> None:
        self.page=0
        self._narrow_inspector_open=False
        self._sync_state_filter_options()
        # Saved records can still be empty; assess unknown rows in every stage.
        missing=[path for path in self.images if path not in self._state_cache]
        if missing:
            IngestPage._queue_missing_state_scan(self,missing)
        self.render_page()

    def filter_state_changed(self,*_):
        self.page=0
        self._sync_state_filter_emphasis()
        selected=str(self.state_filter.currentData() or "all")
        if selected!="all":
            # Detailed filters exist only in Partially Prepared. Do not turn a
            # filter click into a full-folder classification of images that are
            # already known to be Unprepared.
            known_records=set(getattr(self,"_known_record_paths",()))
            missing=[
                path for path in self.images
                if path in known_records and path not in self._state_cache
            ]
            if missing:
                IngestPage._queue_missing_state_scan(self,missing)
        self.render_page()

    def _resolution_filter_changed(self):
        self.page=0
        self.render_page()

    def clear_filters(self):
        self._filter_search_timer.stop()
        self.filter_panel.reset_values()
        self._sync_state_filter_emphasis()
        self._resolution_filter_changed()

    def _resolution_review_paths(self):
        selected=self.selected_paths()
        if selected:return selected
        paths,_,_=self._available_paths()
        return paths

    @staticmethod
    def _resolution_filtered_paths(owner,paths):
        search=getattr(owner,"search",None)
        query=search.text().strip().casefold() if search is not None else ""
        if query:
            paths=[path for path in paths if query in Path(path).name.casefold()]
        filter_widget=getattr(owner,"resolution_filter",None)
        return filter_widget.filter_paths(paths) if filter_widget is not None else paths

    def _resolution_status_changed(self,message: str):
        if message:
            _set_ingest_footer_activity(self,message)
        else:
            _clear_ingest_footer_activity(self,"Checking image dimensions:")

    @staticmethod
    def _state_signatures(path):
        candidates=(path.with_suffix(".tags.json"),path.with_suffix(".txt"))
        values=[]
        for item in candidates:
            try:
                stat=item.stat(); values.append((stat.st_mtime_ns,stat.st_size))
            except OSError:
                values.append(None)
        return tuple(values)

    def _compute_state_projection(self,path):
        signatures=self._state_signatures(path)
        if signatures[0] is None:
            if signatures[1] is not None:
                state=(
                    "incomplete","orange","Existing caption to import",
                    "Existing caption information has not been prepared and reviewed.",
                )
            else:
                state=(
                    "incomplete","orange","No prepared image data",
                    "No authoritative Image Knowledge record is saved yet.",
                )
            coverage=ImageKnowledgeCoverage(
                False,False,"Tags: no saved Image Knowledge.",
                "Descriptions: no saved Image Knowledge.",
                tags_stored=False,descriptions_stored=False,
            )
            return (signatures,state,("",""),"unprepared",coverage,()),None
        workflow=self.workflow_states.assess(
            path,include_output_health=False,
        )
        return self._state_cache_value(
            signatures,workflow,has_record=True,
        ),workflow

    def _compute_state(self,path):
        return IngestPage._compute_state_projection(self,path)[0]

    def _compute_state_from_record(self,record,*,derived_result=None):
        # The managed initializer already validated the files represented by
        # this loaded record. Cache freshness is owned by folder recheck and
        # app-write invalidation, so do not repeat two storage probes here.
        signatures=(None,None)
        workflow=self.workflow_states.assess_record(
            record,include_output_health=False,
            derived_result=derived_result,
        )
        return self._state_cache_value(signatures,workflow,has_record=True)

    @staticmethod
    def _state_cache_value(signatures,workflow,*,has_record: bool|None=None):
        state=(workflow.key,workflow.color,workflow.detail,workflow.label)
        history=(
            getattr(workflow,"last_sdxl_ai_scan",""),
            getattr(workflow,"last_plain_language_ai_scan",""),
        )
        if has_record is None:
            has_record=bool(signatures and signatures[0] is not None)
        stage=image_preparation_stage(workflow,has_record=has_record)
        return signatures,state,history,stage,image_knowledge_coverage(workflow),getattr(workflow,"rating_tags",None)

    def _state_for(self,path):
        if self._attention_items_for_path(path):
            display=self._attention_report.display_name_for(path)
            return (
                "problem","red",f"{display}: Image data needs attention",
                (
                    f"Problem • {display}"
                    if display.casefold()!=Path(path).name.casefold()
                    else "Image data needs attention"
                ),
            )
        cached=self._state_cache.get(path)
        # Folder recheck and every app-owned write invalidate this cache. Do not
        # issue two synchronous network/filesystem probes per image merely to
        # sort or repaint a large gallery.
        if cached: return cached[1]
        return "review","yellow","Checking saved Image Knowledge","Checking image data"

    def _stage_for(self,path) -> str:
        if self._attention_items_for_path(path):
            return "partial"
        cached=self._state_cache.get(path)
        if cached and len(cached)>3 and cached[3] in {
            "unprepared","partial","complete",
        }:
            return cached[3]
        if cached and len(cached)>1:
            key=str(cached[1][0])
            if key=="reviewed":
                return "complete"
            if key!="incomplete":
                return "partial"
        if path in getattr(self,"_known_record_paths",()):
            return "partial"
        return "unprepared"

    def _start_state_scan(self,paths=None):
        # Classification must not depend on content-safety rendering. Unknown
        # ratings stay present with a blurred preview until the separate worker
        # classifies them, and their image-data state can still be prepared.
        if getattr(self,"_shutdown_requested",False) or getattr(
            getattr(getattr(self,"state",None),"jobs",None),"shutting_down",False
        ):
            return
        available=set(self.images)
        requested=list(dict.fromkeys(
            Path(value) for value in (
                self.images if paths is None else paths
            ) if Path(value) in available
        ))
        if self._state_scan_task is not None:
            restart_paths=getattr(self,"_state_scan_restart_paths",set())
            restart_paths.update(requested)
            self._state_scan_restart_paths=restart_paths
            self._state_scan_restart_requested=True
            self.state.jobs.cancel(self._state_scan_task.job_id)
            return
        paths=requested
        self._state_scan_paths=tuple(paths)
        self._state_scan_generation+=1; generation=self._state_scan_generation
        if not paths: self._state_scan_task=None; self.state_progress.hide(); return
        self._state_scan_restart_requested=False
        self._state_scan_restart_paths=set()
        phase="Checking Image Knowledge readiness"
        set_progress_bar(
            self.state_progress,0,len(paths),
            detail=(
                "Read-only check of saved Image Knowledge. This does not run AI "
                "or rewrite images, TXT, Metadata, or authoritative records."
            ),
            scope=phase,
        )
        self.state_progress.show()
        workflow_index=getattr(self,"_workflow_index",None)
        context_fingerprint=str(
            getattr(self,"_readiness_context_fingerprint","") or ""
        )
        projection_provider=getattr(self,"_compute_state_projection",None)
        def work(job):
            from core.workflow_state_cache import WorkflowStateCheckpoint
            semantic_options=IngestPage._semantic_workflow_options(self)
            semantic_validator=semantic_options.get("context_validator")
            if callable(semantic_validator):
                def validate(payload):
                    if not job.checkpoint():raise InterruptedError("Saved status validation cancelled")
                    return semantic_validator(payload)
                semantic_options["context_validator"]=validate

            states={}; projections={}; expected_states={}; unstable=[]
            checkpoint=WorkflowStateCheckpoint(
                context_fingerprint=context_fingerprint,index=workflow_index,
                cache_scope="ingest",paths=paths,
            )
            allow_yield=getattr(self.state.jobs,"allow_foreground_yield",None)
            if callable(allow_yield) and getattr(job,"id",None):allow_yield(job.id,before_yield=checkpoint.flush)
            reusable={};reuse_fingerprints={};trusted_reused=set()
            if workflow_index is not None and context_fingerprint:
                try:
                    reusable=workflow_index.validated_workflow_states(
                        paths,context_fingerprint=context_fingerprint,cache_scope="ingest",
                        source_fingerprints=reuse_fingerprints,should_continue=job.checkpoint,**semantic_options,
                    )
                except (OSError,sqlite3.DatabaseError):pass
            validator=getattr(getattr(self,"workflow_states",None),"cached_rating_matches",None)
            try:
                for index,path in enumerate(paths,1):
                    if not job.checkpoint():
                        raise InterruptedError("State scan cancelled safely")
                    before=DatasetIndex.fingerprint(path)
                    payload=reusable.get(path) if reuse_fingerprints.get(path)==before else None
                    restored=ImageWorkflowState.from_cache_dict(payload) if payload else None
                    if restored is not None and (not callable(validator) or validator(payload)):
                        states[path]=IngestPage._state_cache_value((None,None),restored,has_record=True)
                        projections[path]=payload;expected_states[path]=before
                        checkpoint.add(path,payload,before)
                        if payload.get("automatic_preparation_complete") is True:trusted_reused.add(path)
                        if index==1 or index==len(paths) or index%25==0:
                            job.update(index,len(paths),"Reusing saved image status")
                        continue
                    before=DatasetIndex.fingerprint(path)
                    if callable(projection_provider):
                        state,workflow=projection_provider(path)
                        if workflow is not None:
                            projections[path]=IngestPage._workflow_cache_payload(self,workflow)
                    else:
                        state=self._compute_state(path)
                    if before==DatasetIndex.fingerprint(path):
                        states[path]=state; expected_states[path]=before
                        if path in projections:
                            checkpoint.add(path,projections[path],before)
                    else:
                        projections.pop(path,None); unstable.append(path)
                    if index==1 or index==len(paths) or index%25==0:
                        job.update(index,len(paths),path.name)
                    if index%8==0:
                        sleep(0)
            finally:
                checkpoint.flush()
            skipped=set(projections)-checkpoint.current_recorded()
            for path in skipped:
                states.pop(path,None)
            unstable.extend(skipped)
            for path,fingerprint in tuple(expected_states.items()):
                if DatasetIndex.fingerprint(path)!=fingerprint:
                    states.pop(path,None); unstable.append(path)
            return {
                "generation":generation,"states":states,
                "trusted_paths":tuple(trusted_reused.intersection(states)),
                "unstable":tuple(dict.fromkeys(unstable)),
                "cache_warning":checkpoint.cache_warning,
            }
        task=CoordinatedCallTask(
            self.state.jobs,"Classify Ingest image states",work,resource="io",
            key=f"ingest-state:{str(Path(self.settings.ingest_directory).resolve()).casefold()}",
        ); self._state_scan_task=task
        def progress(index,total,name,owner=task):
            if owner is not self._state_scan_task or getattr(self,"_shutdown_requested",False):
                return
            set_progress_bar(
                self.state_progress,index,total,
                detail=(
                    "Read-only Image Knowledge readiness check. "
                    f"Current record: {name}"
                ),
                scope=phase,
            )
        task.signals.progress.connect(progress)
        task.signals.complete.connect(
            lambda result,owner=task:self._state_scan_complete(owner,result)
        )
        task.signals.cancelled.connect(
            lambda owner=task:self._state_scan_cancelled(owner)
        )
        task.signals.failed.connect(
            lambda message,owner=task:self._state_scan_failed(owner,message)
        )
        try:task.start()
        except RuntimeError as error:self._state_scan_failed(task,str(error))

    def _queue_missing_state_scan(self,paths) -> None:
        """Coalesce discovery behind the active check without cancelling it."""
        if getattr(self,"_shutdown_requested",False) or getattr(
            getattr(getattr(self,"state",None),"jobs",None),"shutting_down",False
        ):
            return
        available=set(self.images)
        availability=getattr(
            getattr(getattr(self,"state",None),"dataset",None),
            "availability_for_path",None,
        )
        missing=set()
        for value in paths or ():
            path=Path(value)
            if path not in available or path in self._state_cache:
                continue
            if callable(availability) and availability(path)=="validated_cancelled":
                continue
            missing.add(path)
        if not missing:
            return
        if getattr(self,"_state_scan_task",None) is None:
            self._start_state_scan(sorted(missing,key=str))
            return
        active=set(getattr(self,"_state_scan_paths",()))
        pending=getattr(self,"_state_scan_missing_paths",set())
        pending.update(missing-active)
        self._state_scan_missing_paths=pending

    def _finish_state_scan_shutdown(self):
        self._state_scan_task=None
        self._state_scan_paths=()
        self._state_scan_restart_requested=False
        self._state_scan_restart_paths=set()
        self._state_scan_missing_paths=set()
        self.state_progress.hide()
        self._notify_background_work_finished()

    def _state_scan_complete(self,task,result):
        if task is not self._state_scan_task:return
        if getattr(self,"_shutdown_requested",False):
            IngestPage._finish_state_scan_shutdown(self); return
        restart=self._state_scan_restart_requested
        restart_paths=list(getattr(self,"_state_scan_restart_paths",()))
        self._state_scan_restart_requested=False
        self._state_scan_restart_paths=set()
        self._state_scan_task=None
        if restart:
            if not self.state.jobs.shutting_down:
                QTimer.singleShot(
                    0,lambda values=restart_paths:self._start_state_scan(values),
                )
            return
        if int(result.get("generation",-1))!=self._state_scan_generation:return
        self._state_cache.update(result.get("states",{}))
        trusted=getattr(self,"_trusted_workflow_paths",None)
        if trusted is not None:trusted.update(result.get("trusted_paths",()))
        self.state_progress.hide()
        self.render_page()
        if result.get("cache_warning"):
            _set_ingest_footer_activity(
                self,"Image status is current; its performance cache will retry later.",
            )
        unstable=list(result.get("unstable",()))
        pending=set(getattr(self,"_state_scan_missing_paths",set()))
        self._state_scan_missing_paths=set()
        followup=set(unstable)
        followup.update(path for path in pending if path not in self._state_cache)
        if followup and not self.state.jobs.shutting_down:
            QTimer.singleShot(
                0,lambda values=tuple(sorted(followup,key=str)):
                self._start_state_scan(values),
            )

    def _state_scan_cancelled(self,task):
        if task is not self._state_scan_task:return
        if getattr(self,"_shutdown_requested",False):
            IngestPage._finish_state_scan_shutdown(self); return
        restart=self._state_scan_restart_requested
        restart_paths=list(getattr(self,"_state_scan_restart_paths",()))
        self._state_scan_restart_requested=False
        self._state_scan_restart_paths=set()
        self._state_scan_task=None
        if restart and not self.state.jobs.shutting_down:
            QTimer.singleShot(
                0,lambda values=restart_paths:self._start_state_scan(values),
            )
        elif not self.state.jobs.shutting_down:
            self._state_scan_missing_paths=set()
            self.state_progress.hide()
        else:
            self._state_scan_missing_paths=set()

    def _state_scan_failed(self,task,message):
        if task is not self._state_scan_task:return
        if getattr(self,"_shutdown_requested",False):
            IngestPage._finish_state_scan_shutdown(self); return
        restart=self._state_scan_restart_requested
        restart_paths=list(getattr(self,"_state_scan_restart_paths",()))
        self._state_scan_restart_requested=False
        self._state_scan_restart_paths=set()
        self._state_scan_task=None
        if restart and not self.state.jobs.shutting_down:
            QTimer.singleShot(
                0,lambda values=restart_paths:self._start_state_scan(values),
            )
            return
        self._state_scan_missing_paths=set()
        self.state_progress.hide()
        _set_ingest_footer_activity(
            self,"Some image states could not be classified.",
        )

    def _available_paths(self):
        selected=str(self.state_filter.currentData() or "all")
        stage=IngestPage.current_stage(self)
        if selected=="problem" and self._attention_report.items:
            # Keep the exact attention-index shortcut: an issue group must not
            # classify unrelated rows just to open its first page.
            visible,hidden=filter_content_paths(self.images); total=len(visible)
            if stage in {"unprepared","partial","complete"}:
                visible=[path for path in visible if self._stage_for(path)==stage]
            visible_set=set(visible)
            known=list(
                dict.fromkeys(
                    path for path in self._attention_report.image_paths
                    if path in visible_set
                )
            )
            known_set=set(known)
            known.extend(
                path for path,cached in self._state_cache.items()
                if path in visible_set and path not in known_set
                and len(cached)>1 and cached[1][0]=="problem"
            )
            return IngestPage._resolution_filtered_paths(self,
                sorted(known,key=lambda path:str(path).casefold())
            ),hidden,total
        projection=IngestPage._ordering_projection(self)
        return IngestPage._resolution_filtered_paths(self,
            projection.paths(stage,selected)
        ),0,len(self.images)

    def visible_paths(self):
        size=max(12,int(self.settings.gallery_page_size))
        visible,_,_=self._available_paths()
        return visible[self.page*size:(self.page+1)*size]

    def selected_paths(self):
        if isinstance(getattr(self,"_state_cache",None),RevisionDict):
            return IngestPage._ordering_projection(self).selected(self.selected)
        visible,_=filter_content_paths(self.images)
        return [path for path in visible if path in self.selected]

    def preparation_paths(self):
        selected=self.selected_paths()
        if selected:
            return selected
        if IngestPage.current_stage(self)=="complete":
            return []
        available_paths=getattr(self,"_available_paths",None)
        if callable(available_paths):
            visible,_,_=available_paths()
        else:
            # Direct consumers without the staged gallery retain the original
            # all-visible preparation scope.
            visible,_=filter_content_paths(self.images)
        return list(visible)

    def update_selection(self):
        # Gallery interactions already enforce the active visibility policy. Keep
        # clicks O(selected) instead of re-reading every image record for a count.
        count=len(self.selected)
        scope=getattr(self,"_gallery_scope_paths",None)
        outside=sum(path not in scope for path in self.selected) if scope is not None else 0
        selection_text=f"Batch selection: {count:,}" if count else "Batch selection: none"
        if outside:
            selection_text+=f" • {outside:,} outside current filters"
        self.selection_label.setText(selection_text)
        preparation_paths=self.preparation_paths()
        preparation_count=len(preparation_paths)
        stage=IngestPage.current_stage(self)
        selected_noun="image" if count==1 else "images"
        if count:
            button_text=(
                "Re-run Preparation…"
                if stage=="complete" else
                "Prepare Selected Images"
            )
            description=f"{count:,} explicitly selected image(s), including selections kept from other stages or filters"
        else:
            button_text=(
                "Prepare Unprepared Images"
                if stage=="unprepared" else
                "Continue Preparation"
                if stage=="partial" else
                "Re-run Preparation…"
                if stage=="complete" else
                "Prepare All Images"
            )
            description=(
                f"all {preparation_count:,} image(s) in the {stage.replace('_',' ')} stage"
                if stage!="all" else
                f"all {preparation_count:,} available incoming image(s)"
            )
        def preparation_detail(options):
            tag_model_ids=(
                [backend.model_id for backend in configured_wd_backends(self.settings)]
                if options.suggest_sdxl_tags else []
            )
            model_ids=list(tag_model_ids)
            if options.suggest_plain_language:
                model_ids.append(self.settings.plain_language_model)
            if not model_ids:
                return "AI analysis: Not selected for this run."
            estimate=ai_estimate_short_text(
                self.settings,preparation_count,model_ids,
            )
            estimate=estimate.replace(
                "AI estimate:","Time:",
            )
            pass_parts=[]
            if tag_model_ids:
                pass_parts.append(
                    f"up to {preparation_count*len(tag_model_ids):,} Tag passes"
                )
            if options.suggest_plain_language:
                pass_parts.append(
                    f"up to {preparation_count:,} Description passes"
                )
            known_states=[
                self._state_cache.get(path) for path in preparation_paths
            ]
            known_states=[
                value for value in known_states
                if isinstance(value,tuple) and len(value)>2
            ]
            known_tag=sum(bool(value[2][0]) for value in known_states)
            known_description=sum(bool(value[2][1]) for value in known_states)
            coverage=(
                f"Prior coverage: Tags {known_tag:,}/{len(known_states):,}; "
                f"Descriptions {known_description:,}/{len(known_states):,} checked images. "
                "Coverage does not prove freshness."
                if known_states else
                "Prior coverage: Not counted yet."
            )
            reuse=(
                "Reuse: Off — Force all scans is selected."
                if options.force_all_ai else
                f"Reuse: Skip exact matching AI results from the last "
                f"{options.skip_ai_value} {options.skip_ai_unit}. "
                "Changed images, models/revisions, prompts or settings are rescanned."
            )
            return (
                estimate+" (full-scope upper bound, not remaining work).\n"
                +"AI work: "+"; ".join(pass_parts)+".\n"
                +coverage+"\n"+reuse
            )

        self.preparation_controls.set_scope(
            preparation_count,button_text=button_text,description=description,
            detail_provider=preparation_detail,
        )
        complete_summary=(
            f"{count:,} {selected_noun} selected • review their readiness before choosing a destination"
            if count else
            "Select completed images to move or refresh"
        )
        self.preparation_controls.set_context(
            refresh=stage=="complete",secondary=stage=="complete",
            summary=complete_summary if stage=="complete" else "",
            summary_tooltip=(
                "Moving is the normal next step for complete images. Re-running "
                "preparation is optional and changes Image Knowledge only."
                if stage=="complete" else ""
            ),
        )
        self.ai_time_estimate.hide()
        self.review_selected_button.hide()
        selected_ready=bool(count) and self._batch_task is None
        if hasattr(self,"run_ai_selected_button"):
            self.run_ai_selected_button.setText("AI-Only Review...")
            self.run_ai_selected_button.setVisible(bool(count))
            self.run_ai_selected_button.setEnabled(selected_ready)
        if button:=getattr(self,"review_added_button",None):
            button.setEnabled(self._batch_task is None)
        self.organize_button.setEnabled(selected_ready)
        for action in filter(None,(
            self.move_library_action,self.move_problems_action,
            getattr(self,"move_recycle_action",None),self.move_folder_action,
        )):
            action.setEnabled(selected_ready)
        sync_completion=getattr(self,"_sync_completion_panel",None)
        if callable(sync_completion):
            sync_completion()
        IngestPage._sync_complete_move_recommendation(self)

    def _sync_complete_move_recommendation(self):
        if not getattr(self,"_complete_inspected_image",False):
            return
        selected=getattr(self,"selected",None)
        if selected is None or self.current_path in selected:
            message="Image Knowledge complete. Choose Move to… to continue."
        elif selected:
            message=(
                "This image is complete but not selected. Select its card to "
                "include it in Move to…, or review the current selection."
            )
        else:
            message=(
                "Image Knowledge complete. Select this image's card, then "
                "choose Move to… to continue."
            )
        self.recommendation.setText(message)

    def _sync_completion_panel(self) -> None:
        if not hasattr(self,"completion_panel"):
            return
        stage=IngestPage.current_stage(self)
        self.organize_button.setVisible(stage=="complete")
        scope=getattr(self,"_organization_scope",OrganizationHistoryScope())
        held_add=bool(scope.recovery_operation_id) and getattr(self,"_organization_action","")=="add"
        startup=getattr(self,"_startup_add_recovery_results",())
        startup=startup if isinstance(startup,tuple) else ()
        self.completion_panel.setVisible(bool(self._move_undo_context or held_add
            or getattr(self,"_saved_add_state",None) is not None
            or getattr(self,"_saved_add_error","")
            or getattr(self,"_completed_library_paths",()) or startup))

    def _refresh_saved_add_batch_state(self) -> None:
        self._saved_add_state=None
        self._saved_add_error=""
        moves=getattr(getattr(self,"state",None),"moves",None)
        if not isinstance(getattr(moves,"journal",None),OperationJournal):
            return
        try:
            executor=getattr(self,"_saved_add_executor",None)
            if not isinstance(executor,AddImagesBatchExecutor):
                executor=AddImagesBatchExecutor(moves)
            active=executor.store.active()
            if len(active)>1:
                raise ValueError("More than one saved Add Images batch needs exact review.")
            if active:
                self._saved_add_state=executor.store.read(active[0])
        except (OSError,ValueError) as exc:
            self._saved_add_error=str(exc)

    def _set_move_undo_available(self,available=None):
        scope=getattr(self,"_organization_scope",OrganizationHistoryScope())
        recycle_ids=getattr(self,"_recycle_record_ids",())
        held_add=bool(scope.recovery_operation_id) and getattr(self,"_organization_action","")=="add"
        startup=getattr(self,"_startup_add_recovery_results",())
        startup=startup if isinstance(startup,tuple) else ()
        held_startup=any(item.status=="held" for item in startup)
        self._move_undo_context=bool(scope.available or recycle_ids)
        button=getattr(self,"undo_move_button",None)
        if button is None:return
        button.setText("Open Recycle…" if recycle_ids else
            "Review Add History…" if getattr(self,"_organization_action","")=="add" else "Review Move History…")
        button.setToolTip("Open these exact Recycle records to review restoration or permanent deletion." if recycle_ids else
            "Review the interrupted transfer and reopen CollectiGen before changing completed Add history."
            if held_add else
            "Compare these recorded image families before restoring location. Originals, current data and privacy boundaries are checked again.")
        button.setVisible(self._move_undo_context)
        button.setEnabled(self._move_undo_context and not held_add
            and getattr(self,"_batch_task",None) is None and not history_dialogs_busy(self))
        count=len(recycle_ids) if recycle_ids else len(scope.operation_ids)
        note=(f"{count:,} exact Recycle record(s) are ready to review." if recycle_ids else
            f"{count:,} completed image {'family' if count==1 else 'families'} {'is' if count==1 else 'are'} recorded." if self._move_undo_context else "No exact completed file operation is selected.")
        if scope.recovery_operation_id:
            remaining=int(getattr(self,"_organization_unattempted",0))
            note=(f"{count:,} completed image {'family' if count==1 else 'families'} recorded. "
                  "One Add Images family needs recovery before more work."
                  +(f" {remaining:,} later {'family was' if remaining==1 else 'families were'} not attempted."
                    if remaining else "")
                  if getattr(self,"_organization_action","")=="add" else
                  note+" One family needs exact recovery before more work.")
        completed=tuple(getattr(self,"_completed_library_paths",()))
        if completed and not scope.recovery_operation_id:
            note=(f"{completed[0].name} moved to a named library. Open it in Dataset or review move history."
                  if len(completed)==1 else
                  f"{len(completed):,} images moved to named libraries. Choose a result to open in Dataset or review move history.")
        if startup and not self._move_undo_context and not scope.recovery_operation_id and not completed:
            note=(f"Startup checked {len(startup):,} interrupted Add Images "
                  f"{'family' if len(startup)==1 else 'families'}. "
                  + ("One or more still need attention; Add Images remains paused. " if held_startup else "")
                  + "Choose Review Add Recovery before adding more images.")
        saved=getattr(self,"_saved_add_state",None)
        saved_error=getattr(self,"_saved_add_error","")
        if saved is not None:
            note=(f"Saved Add Images: {saved.completed:,} completed, "
                  f"{saved.remaining:,} not started, {saved.held:,} need recovery. "
                  +("Choose Review Add Recovery for the startup result before more file work."
                    if saved.held and held_startup else
                    "Choose Review Interrupted Add, then close and reopen CollectiGen for recovery."
                    if saved.held and held_add else
                    "Choose Review Saved Add for exact held details before more file work."
                    if saved.held else
                    "Choose Review Saved Add before another Add Images selection."))
            if self._saved_add_preview_failed and saved.remaining and not saved.held:
                note=(f"Saved Add Images: {saved.completed:,} completed, "
                      f"{saved.remaining:,} not started. Completed files are safe.\n"
                      "Source changed. Restore it, then Review Saved Add; or use Review Saved Add "
                      "to discard all unstarted files before a fresh Add Images selection.")
        elif saved_error:
            note="Saved Add Images evidence needs attention. Review it before more file work."
        self.completion_note.setText(note)
        self.completion_note.setToolTip("Recovery operation: "+scope.recovery_operation_id if scope.recovery_operation_id else note)
        if button:=getattr(self,"review_interrupted_add_button",None):
            button.setVisible(held_add and not held_startup)
            button.setEnabled(getattr(self,"_batch_task",None) is None)
        if button:=getattr(self,"review_add_recovery_button",None):
            button.setVisible(bool(startup))
            button.setEnabled(bool(startup) and getattr(self,"_batch_task",None) is None)
        if button:=getattr(self,"review_saved_add_button",None):
            button.setVisible(bool(saved_error) or (saved is not None and
                (not saved.held or not (held_add or held_startup))))
            button.setEnabled(getattr(self,"_batch_task",None) is None)
        if button:=getattr(self,"add_images_button",None):
            button.setEnabled(not held_add and not held_startup and saved is None
                and not saved_error and getattr(self,"_batch_task",None) is None)
            button.setToolTip("Review the still-held Add Images recovery before more file work."
                if held_startup else "Review the interrupted Add Images transfer and reopen CollectiGen before adding more images."
                if held_add else "Review the saved Add Images batch before choosing another selection."
                if saved is not None or saved_error else "Choose image files or a folder, then preview Copy or Move to Ingest.")
        if (held_add or held_startup) and (actions:=getattr(self,"batch_actions",None)):
            actions.setEnabled(False)
        if button:=getattr(self,"view_completed_in_dataset",None):
            count=len(getattr(self,"_completed_library_paths",()))
            button.setText("Open in Dataset" if count==1 else f"Open {count:,} in Dataset…")
            button.setVisible(bool(count) and not scope.recovery_operation_id)
            button.setEnabled(bool(count) and getattr(self,"_batch_task",None) is None)
        IngestPage._sync_completion_panel(self)

    def _review_saved_add(self):
        if getattr(self,"_batch_task",None) is not None:
            show_operation_in_progress(self,
                "Finish current Ingest work before reviewing the saved Add Images batch")
            return
        IngestPage._refresh_saved_add_batch_state(self)
        state=self._saved_add_state
        if state is None:
            if self._saved_add_error:
                QMessageBox.warning(self,"Saved Add Images needs attention",
                    self._saved_add_error+"\n\nNo saved image family was replayed. Keep the journal for exact recovery.")
            IngestPage._set_move_undo_available(self)
            return
        dialog=SavedAddBatchDialog(state,self)
        if dialog.exec()!=QDialog.Accepted:return
        if dialog.choice=="review":
            self._start_saved_add_preview(state.operation_id)
            return
        if dialog.choice=="discard":
            choice=QMessageBox.question(self,"Discard unstarted Add Images selection?",
                f"Discard only the {state.remaining:,} image families that have not started? "
                f"The {state.completed:,} completed families and their History remain. "
                "No image files will be removed by this choice.",
                QMessageBox.Yes | QMessageBox.No,QMessageBox.No)
            if choice!=QMessageBox.Yes:return
            try:
                self._saved_add_executor.store.discard_remaining(state.operation_id,
                    expected_remaining=tuple(item.item_id for item in state.items
                                             if item.state=="remaining"))
            except (OSError,ValueError) as exc:
                QMessageBox.warning(self,"Saved selection was not discarded",str(exc))
        elif dialog.choice=="finish":
            try:
                self._saved_add_executor.store.close_complete(state.operation_id)
            except (OSError,ValueError) as exc:
                QMessageBox.warning(self,"Saved batch did not close",str(exc))
        IngestPage._refresh_saved_add_batch_state(self)
        IngestPage._set_move_undo_available(self)

    def _start_saved_add_preview(self,parent_id):
        if getattr(self,"_batch_task",None) is not None:
            show_operation_in_progress(self,
                "Finish current Ingest work, then choose Review Saved Add again")
            return False
        try:
            state=self._saved_add_executor.store.read(parent_id)
        except (OSError,ValueError) as exc:
            QMessageBox.warning(self,"Saved Add Images needs attention",str(exc));return False
        if state.held or not state.remaining:
            show_operation_in_progress(self,
                "This saved Add Images batch has no eligible unstarted families to review")
            return False
        self._saved_add_preview_failed=False
        ingest=_configured_ingest_folder(self.settings)
        if ingest is None or ingest!=state.ingest_root:
            QMessageBox.warning(self,"Saved destination changed",
                "Restore the recorded Ingest folder in Setup before reviewing these saved files. "
                "CollectiGen will not redirect the remaining images.")
            return False
        registry=self.settings.library_registry()
        planner=IngestImportPlanningService(ingest,library_registry=registry,
            destination_profile=registry.profile_for_path(ingest),
            transfers=self.state.moves)
        paths=[item.source for item in state.items if item.state=="remaining"]
        started,active=self.state.begin_long_operation("ingest",
            "Checking exact saved Add Images families…",resource="io")
        if not started:
            show_operation_in_progress(self,active);return False
        self._batch_completion_context="saved_add_preview"
        self._saved_add_preview_parent_id=parent_id
        self._state_scan_generation+=1
        self.batch_actions.setEnabled(False);self.refresh_button.setEnabled(False)
        self.add_images_button.setEnabled(False)
        self.review_saved_add_button.setEnabled(False)
        self.import_external_evidence_button.setEnabled(False)
        self.organize_button.setEnabled(False)
        if controls:=getattr(self,"preparation_controls",None):controls.set_busy(True)
        set_progress_bar(self.batch_progress,detail="Checking exact saved Add Images families…",
                         unknown_text="Starting…")
        self.batch_progress.show()
        def operation(values,progress,should_cancel=lambda:False):
            fresh=planner.preview_exact_selection(values,
                source_root=state.source_root,preserves_subfolders=state.preserves_subfolders,
                progress=progress,should_cancel=should_cancel)
            self._saved_add_executor.store.matching_remaining_items(
                parent_id,fresh,should_cancel=should_cancel)
            return fresh,state.action
        task=_BatchTask(operation,paths);self._batch_task=task
        task.signals.progress.connect(self._batch_progress)
        task.signals.complete.connect(self._saved_add_preview_complete)
        task.signals.failed.connect(self._batch_failed)
        self.cancel_batch_button.setText("Cancel Safely")
        self.cancel_batch_button.setEnabled(True);self.cancel_batch_button.show()
        self.pool.start(task,-1)
        return True

    def _saved_add_preview_complete(self,result):
        self._saved_add_preview_failed=False
        parent_id=getattr(self,"_saved_add_preview_parent_id","")
        self._saved_add_preview_parent_id=""
        cancelled=isinstance(self._batch_task,QRunnable) and self._batch_task.is_cancelled()
        self._batch_completion_context=""
        self.state.finish_long_operation("ingest")
        self._batch_task=None
        self.batch_actions.setEnabled(True);self.refresh_button.setEnabled(True)
        self.import_external_evidence_button.setEnabled(True)
        if controls:=getattr(self,"preparation_controls",None):controls.set_busy(False)
        self.batch_progress.hide();self.cancel_batch_button.hide()
        IngestPage._refresh_saved_add_batch_state(self)
        IngestPage._set_move_undo_available(self)
        if getattr(self,"_shutdown_requested",False):
            self._notify_background_work_finished();return
        if cancelled or not (isinstance(result,tuple) and len(result)==2
                             and isinstance(result[0],IngestImportPlan)):
            self.batch_summary.setText("Saved Add Images review stopped. The remaining selection is unchanged.")
            self.batch_summary.show();self._notify_background_work_finished();return
        plan,action=result
        dialog=IngestImportPreviewDialog(plan,self,fixed_action=action)
        if dialog.exec()==QDialog.Accepted:
            self._apply_saved_add_remaining(parent_id,plan,dialog.boundary_confirmed())
        self._notify_background_work_finished()

    def _apply_saved_add_remaining(self,parent_id,plan,boundary_confirmed):
        if IngestPage._file_work_blocked(self):return False
        state=self._saved_add_executor.store.read(parent_id)
        paths=[item.source for item in state.items if item.state=="remaining"]
        def operation(_paths,progress,should_cancel=lambda:False):
            result=self._saved_add_executor.run_remaining(parent_id,plan,
                boundary_confirmed=boundary_confirmed,progress=progress,
                should_pause=should_cancel)
            return _saved_add_result(result)
        return self._run_batch(operation,
            "Continuing verified Add Images families…",paths,
            completion_context="add_images_saved_apply",progress_unit="image",
            pause_after_current=True)

    def _review_interrupted_add(self):
        if getattr(self,"_batch_task",None) is not None:return
        scope=getattr(self,"_organization_scope",OrganizationHistoryScope())
        if not scope.recovery_operation_id or getattr(self,"_organization_action","")!="add":return
        completed=len(scope.operation_ids)
        remaining=int(getattr(self,"_organization_unattempted",0))
        guidance=(
            f"{completed:,} image {'family' if completed==1 else 'families'} completed; one transfer needs recovery."
            +(f" {remaining:,} later {'family was' if remaining==1 else 'families were'} not attempted."
              if remaining else "")+"\n\n"
            "Close CollectiGen normally and reopen it to run the existing interrupted-transfer recovery. "
            "It may complete the recorded family, roll back a partial family, or remain held if files changed. "
            "Do not retry Add Images until you have checked the recovered result."
        )
        try:
            receipt=read_interrupted_add_receipt(self.state.moves.journal,scope)
        except ValueError as exc:
            QMessageBox.warning(self,"Recorded Add Images recovery",guidance+"\n\n"+str(exc))
            return
        details=[f"Operation: {receipt.operation_id}",f"Batch: {receipt.group_id}",
            f"Recorded action: {receipt.mode}","", "Recorded file-family paths and hashes (not a fresh file check):"]
        for source,target,digest in receipt.files:
            details.extend((f"Source: {source}",f"Destination: {target}",f"SHA-256: {digest}",""))
        dialog=QDialog(self)
        dialog.setWindowTitle("Interrupted Add Images transfer")
        dialog.setMinimumWidth(620)
        dialog.resize(760,320)
        layout=QVBoxLayout(dialog)
        summary=QLabel(guidance+f"\n\nRecorded {receipt.mode} family: {receipt.files[0][0].name}.",dialog)
        summary.setWordWrap(True)
        summary.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(summary)
        exact=QTextEdit(dialog)
        exact.setReadOnly(True)
        exact.setAccessibleName("Exact recorded transfer paths and hashes")
        exact.setPlainText("\n".join(details))
        disclosure=DisclosureButton("Show Exact Recorded Paths","Hide Exact Recorded Paths",exact,dialog)
        disclosure.toggled.connect(lambda shown:dialog.resize(960,650) if shown else dialog.resize(760,320))
        layout.addWidget(disclosure,0,Qt.AlignLeft)
        layout.addWidget(exact,1)
        buttons=QDialogButtonBox(QDialogButtonBox.Close,parent=dialog)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()

    def _review_add_recovery(self):
        results=getattr(self,"_startup_add_recovery_results",())
        results=results if isinstance(results,tuple) else ()
        if not results or getattr(self,"_batch_task",None) is not None:return
        held=any(item.status=="held" for item in results)
        dialog=_AddRecoveryReviewDialog(self)
        dialog.setWindowTitle("Add Images recovery after restart")
        dialog.resize(820,460)
        layout=QVBoxLayout(dialog)
        counts={status:sum(item.status==status for item in results)
            for status in ("completed","rolled_back","held")}
        heading=QLabel(
            f"Startup checked {len(results):,} recorded Add Images {'family' if len(results)==1 else 'families'}: "
            f"{counts['completed']:,} added, {counts['rolled_back']:,} rolled back, "
            f"{counts['held']:,} still need attention. Later unattempted images were not resumed."
        )
        heading.setWordWrap(True);layout.addWidget(heading)
        rows=QTreeWidget(dialog)
        rows.setHeaderLabels(("Recovery result","Recorded image family","Action"))
        rows.setRootIsDecorated(False)
        rows.setAlternatingRowColors(True)
        rows.header().setSectionResizeMode(0,QHeaderView.ResizeToContents)
        rows.header().setSectionResizeMode(1,QHeaderView.Stretch)
        rows.header().setSectionResizeMode(2,QHeaderView.ResizeToContents)
        labels={"completed":"Added successfully","rolled_back":"Partial add rolled back",
                "held":"Still needs attention"}
        for index,item in enumerate(results):
            family=item.receipt.files[0][0].name if item.receipt and item.receipt.files else "Recorded family unavailable"
            row=QTreeWidgetItem((labels[item.status],family,item.mode.title()))
            row.setData(0,Qt.UserRole,index)
            rows.addTopLevelItem(row)
        layout.addWidget(rows,1)
        explanation=QLabel(dialog)
        explanation.setWordWrap(True)
        explanation.setTextFormat(Qt.PlainText)
        explanation.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(explanation)
        exact=QTextEdit(dialog)
        exact.setReadOnly(True)
        exact.setAccessibleName("Exact recorded Add Images recovery paths and hashes")
        exact.setMaximumHeight(210)
        disclosure=DisclosureButton("Show Exact Recorded Paths","Hide Exact Recorded Paths",exact,dialog)
        layout.addWidget(disclosure,0,Qt.AlignLeft)
        layout.addWidget(exact)
        buttons=QDialogButtonBox(QDialogButtonBox.Close,parent=dialog)
        history=buttons.addButton("Review This Add History…",QDialogButtonBox.ActionRole)
        check=buttons.addButton("Check This Family",QDialogButtonBox.ActionRole)
        recover=buttons.addButton("Recover This Family…",QDialogButtonBox.ActionRole)
        history.clicked.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        inspection=None
        task_error=""
        task_error_operation_id=""
        reconciled_ids=set()
        def selected():
            row=rows.currentItem()
            index=row.data(0,Qt.UserRole) if row is not None else None
            return results[index] if isinstance(index,int) and 0<=index<len(results) else None
        def update():
            nonlocal inspection
            item=selected()
            if item is None:return
            if item.status=="completed":
                message=("This selected family completed after your recovery check and has saved Add History."
                    if item.operation_id in reconciled_ids else
                    "The recorded family completed during startup recovery and has saved Add History.")
            elif item.status=="rolled_back":
                message=("This selected family was rolled back after your recovery check. The original family was preserved."
                    if item.operation_id in reconciled_ids else
                    "The partial add was rolled back during startup. The original family was preserved.")
            else:
                message=(("The latest exact-family recovery could not safely finish this family. "
                    if item.operation_id in reconciled_ids else
                    "Recovery could not safely finish this family. ")+
                    "Add Images remains paused; "
                    "keep the recorded source and destination files for exact recovery review.")
                if item.reason:
                    if "changed externally" in item.reason:
                        message+=" At startup, a recorded source or destination had changed."
                    else:
                        message+=(" The latest recovery attempt found a blocker; see exact details below."
                            if item.operation_id in reconciled_ids else
                            " The startup check found a blocker; see exact details below.")
                if inspection is not None and inspection.operation_id==item.operation_id:
                    message+=(" A fresh check found the recorded files unchanged. Recover This Family may now "
                        "finish or roll back only this family." if inspection.ready else
                        " A fresh check still found a blocker; no recovery was started. See exact details below.")
                elif task_error and task_error_operation_id==item.operation_id:
                    message+=" The latest check could not finish; the family remains held."
            explanation.setText(message+(
                " The fresh check did not change files." if inspection is not None
                    and inspection.operation_id==item.operation_id else
                " This exact journal result was saved; it is not a fresh check of current file bytes."
                    if item.status!="held" else
                " The journal remains held; this is not a fresh check of current file bytes."
                    if item.operation_id in reconciled_ids else
                " This is the startup result, not a fresh check of current file bytes."))
            receipt=item.receipt
            lines=[f"Operation: {item.operation_id}",f"Batch: {item.group_id}",
                   f"Recorded action: {item.mode}","", "Recorded paths and hashes (not a fresh file check):"]
            if item.status=="held" and item.reason:
                label="Latest recovery blocker" if item.operation_id in reconciled_ids else "Startup blocker"
                lines[3:3]=[f"{label}: {item.reason}",""]
            if inspection is not None and inspection.operation_id==item.operation_id:
                lines[3:3]=[f"Fresh check: {inspection.reason}",""]
                for index,(source_state,target_state) in enumerate(zip(
                    inspection.source_states,inspection.target_states),1):
                    lines.append(f"Current member {index}: source {source_state}; destination {target_state}")
                lines.append("")
            if task_error and task_error_operation_id==item.operation_id:
                lines[3:3]=[f"Latest check error: {task_error}",""]
            if receipt is None:lines.append("The exact recorded file-family snapshot could not be verified.")
            else:
                for source,target,digest in receipt.files:
                    lines.extend((f"Source: {source}",f"Destination: {target}",f"SHA-256: {digest}",""))
            exact.setPlainText("\n".join(lines))
            history.setVisible(item.status=="completed")
            history.setEnabled(item.status=="completed" and not held)
            history.setToolTip("Resolve the still-held Add transfer before changing saved Add History."
                if held else "Open the selected completed family in exact Add History.")
            check.setVisible(item.status=="held")
            check.setEnabled(item.status=="held")
            recover.setVisible(item.status=="held")
            recover.setEnabled(item.status=="held" and inspection is not None
                and inspection.operation_id==item.operation_id and inspection.ready)
            recover.setToolTip("Only the selected recorded family is rechecked before any file change; "
                "the remaining Add Images batch is not resumed.")
        def run_selected(label,operation,complete):
            if (getattr(self,"_batch_task",None) is not None
                    or any(getattr(self,name,None) is not None for name in
                        ("_scan_task","_active_editor"))
                    or history_dialogs_busy(self) or getattr(self,"_shutdown_requested",False)):
                show_operation_in_progress(self,"An Ingest or History action is still active")
                return
            started,active=self.state.begin_long_operation("ingest",label,resource="io",exclusive=True)
            if not started:
                show_operation_in_progress(self,active)
                return
            rows.setEnabled(False)
            dialog.recovery_busy=True
            for button in (check,recover,history,buttons.button(QDialogButtonBox.Close)):
                button.setEnabled(False)
            explanation.setText(label+". Keep CollectiGen open until this exact-family operation finishes.")
            def work(_paths,_progress,should_cancel):
                return operation(should_cancel)
            task=_BatchTask(work,())
            self._batch_task=task
            IngestPage._set_move_undo_available(self)
            def finish():
                self.state.finish_long_operation("ingest")
                self._batch_task=None
                dialog.recovery_busy=False
                rows.setEnabled(True)
                buttons.button(QDialogButtonBox.Close).setEnabled(True)
                IngestPage._set_move_undo_available(self)
            def done(result):
                nonlocal task_error,task_error_operation_id,inspection
                finish()
                if isinstance(result,dict):
                    inspection=None
                    task_error="The operation stopped before a recovery write. Check this family again."
                    task_error_operation_id=selected().operation_id if selected() else ""
                else:
                    complete(result)
                update()
            def failed(message):
                nonlocal task_error,task_error_operation_id,inspection
                finish()
                inspection=None
                task_error=message
                task_error_operation_id=selected().operation_id if selected() else ""
                update()
            task.signals.complete.connect(done)
            task.signals.failed.connect(failed)
            self.pool.start(task,-1)
        def check_selected():
            nonlocal inspection,task_error,task_error_operation_id
            item=selected()
            if item is None or item.status!="held":return
            inspection=None;task_error="";task_error_operation_id=""
            update()
            def complete(result):
                nonlocal inspection
                inspection=result
            run_selected("Checking this recorded Add Images family",
                lambda should_cancel:self.state.moves.inspect_add_recovery(
                    item.operation_id,group_id=item.group_id,should_cancel=should_cancel),complete)
        def recover_selected():
            nonlocal inspection,results,held,task_error,task_error_operation_id
            item=selected()
            if (item is None or item.status!="held" or inspection is None
                    or inspection.operation_id!=item.operation_id or not inspection.ready):return
            choice=QMessageBox.question(self,"Recover this Add Images family?",
                "Only the selected recorded family will be reconciled. CollectiGen will recheck "
                "the exact files and saved receipt, then may finish or roll back the partial transfer. "
                "Other held families and later unattempted images will not be resumed. "
                "Keep the original and destination files in place until the result is shown.",
                QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
            if choice!=QMessageBox.Yes:return
            chosen_inspection=inspection
            task_error="";task_error_operation_id=""
            def complete(result):
                nonlocal inspection,results,held
                inspection=None
                reconciled_ids.add(result.operation_id)
                results=tuple(self.state.moves.startup_add_recovery_results)
                self._startup_add_recovery_results=results
                held=any(value.status=="held" for value in results)
                if not held:self.batch_actions.setEnabled(True)
                IngestPage._refresh_saved_add_batch_state(self)
                IngestPage._set_move_undo_available(self)
                self.update_selection()
                counts={status:sum(value.status==status for value in results)
                    for status in ("completed","rolled_back","held")}
                heading.setText(
                    f"Recovery results for {len(results):,} recorded Add Images "
                    f"{'family' if len(results)==1 else 'families'}: {counts['completed']:,} added, "
                    f"{counts['rolled_back']:,} rolled back, {counts['held']:,} still need attention. "
                    "Later unattempted images were not resumed."
                )
                for index,value in enumerate(results):
                    row=rows.topLevelItem(index)
                    if row is not None:
                        row.setText(0,labels[value.status])
                if result.status!="held":
                    self.scan()
            run_selected("Recovering this recorded Add Images family",
                lambda should_cancel:self.state.moves.recover_add(
                    chosen_inspection,should_cancel=should_cancel),complete)
        check.clicked.connect(check_selected)
        recover.clicked.connect(recover_selected)
        rows.itemSelectionChanged.connect(update)
        first_held=next((index for index,item in enumerate(results) if item.status=="held"),0)
        rows.setCurrentItem(rows.topLevelItem(first_held))
        if dialog.exec()==QDialog.Accepted:
            item=selected()
            if item is not None and item.status=="completed" and not held:
                try:return open_organization_history(self,operation_targets=(item.operation_id,),
                    group_id=item.group_id)
                except (OSError,ValueError,RuntimeError) as exc:
                    QMessageBox.warning(self,"Add History could not open",str(exc))

    def _view_completed_library_image(self):
        if getattr(self,"_batch_task",None) is not None:return
        paths=tuple(getattr(self,"_completed_library_paths",()))
        if not paths:return
        scope=getattr(self,"_organization_scope",OrganizationHistoryScope())
        if scope.recovery_operation_id:return
        if len(paths)==1:
            selected=paths[0]
        else:
            menu=QMenu(self)
            for path in paths:
                action=menu.addAction(f"{path.name} — {path.parent.name}")
                action.setToolTip(str(path))
                action.setData(str(path))
            chosen=menu.exec(self.view_completed_in_dataset.mapToGlobal(
                self.view_completed_in_dataset.rect().bottomLeft()
            ))
            if chosen is None:return
            selected=Path(chosen.data())
        if selected not in paths or not selected.is_file():
            QMessageBox.information(self,"Moved image unavailable",
                "This completed image is no longer at its recorded location. Review Move History or refresh the library before opening it.")
            return
        self.open_dataset_image.emit(str(selected))

    def _file_work_blocked(self):
        saved=getattr(self,"_saved_add_state",None)
        saved_error=getattr(self,"_saved_add_error","")
        if (isinstance(saved_error,str) and saved_error
                or isinstance(saved,AddBatchState) and saved.held):
            startup=getattr(self,"_startup_add_recovery_results",())
            held_startup=isinstance(startup,tuple) and any(item.status=="held" for item in startup)
            scope=getattr(self,"_organization_scope",None)
            held_add=(isinstance(scope,OrganizationHistoryScope)
                and bool(scope.recovery_operation_id)
                and getattr(self,"_organization_action","")=="add")
            show_operation_in_progress(self,
                "The saved Add Images batch needs exact recovery. Choose "
                +( "Review Add Recovery" if held_startup else
                   "Review Interrupted Add" if held_add else "Review Saved Add"))
            return True
        startup=getattr(self,"_startup_add_recovery_results",())
        if isinstance(startup,tuple) and any(item.status=="held" for item in startup):
            show_operation_in_progress(self,
                "An Add Images family still needs exact recovery. Review Add Recovery before more file work")
            return True
        scope=getattr(self,"_organization_scope",None)
        if (isinstance(scope,OrganizationHistoryScope) and scope.recovery_operation_id
                and getattr(self,"_organization_action","")=="add"):
            show_operation_in_progress(self,
                "A recorded image-family transfer needs recovery. Review the interrupted operation and reopen CollectiGen before more file work")
            return True
        if getattr(self,"_batch_task",None) is not None:
            show_operation_in_progress(self,
                "Finish or stop the current Ingest batch before opening image work")
            return True
        busy=any(getattr(self,name,None) is not None for name in ("_scan_task","_active_editor"))
        if busy or history_dialogs_busy(self) or getattr(self,"_shutdown_requested",False):
            show_operation_in_progress(self,"An Ingest operation, image editor or History job is still active")
            return True
        return False

    def _remember_organization_result(self,result):
        if not any(key in result for key in ("organization_operation_ids","recycle_record_ids","moved","added")):return
        try:
            scope=OrganizationHistoryScope.from_result(result)
            members={}
            for member in result.get("organization_members",()):
                if member.get("operation_id") not in scope.operation_ids:continue
                pair=tuple(Path(member[key]) for key in ("from","to"))
                if not all(path.is_absolute() for path in pair):raise ValueError("Recorded placement paths are not absolute.")
                members[member["operation_id"]]=pair
            recycle_ids=tuple(dict.fromkeys(result.get("recycle_record_ids",())))
            if any(not isinstance(value,str) or not value.startswith("recycle_") or len(value)>128 for value in recycle_ids):
                raise ValueError("Recorded Recycle identities are unavailable.")
        except (ValueError,TypeError,KeyError,AttributeError):
            scope=OrganizationHistoryScope();members={};recycle_ids=()
        self._organization_scope=scope;self._organization_members=members;self._recycle_record_ids=recycle_ids
        self._organization_action=result.get("organization_action","move")
        self._organization_unattempted=max(0,int(result.get("recovery_not_attempted",0) or 0))
        IngestPage._set_move_undo_available(self)

    def _organization_restored(self,scope,members,outcomes):
        completed=[item for item in outcomes if item.status=="complete" and item.operation_id in scope.operation_ids and item.current_path is not None]
        changed=set();updated=dict(members)
        for item in completed:
            pair=members.get(item.operation_id)
            previous=getattr(item,"previous_path",None) or (pair[1] if pair else None)
            if previous is not None:changed.add(Path(previous))
            changed.add(Path(item.current_path))
            if item.inverse_operation_id:
                updated.pop(item.operation_id,None)
                updated[item.inverse_operation_id]=(Path(previous) if previous is not None else Path(item.current_path),Path(item.current_path))
        advanced=scope.advanced(outcomes)
        if getattr(self,"_organization_scope",None)==scope:
            self._organization_scope=advanced;self._organization_members=updated
            self._completed_library_paths=()
            IngestPage._set_move_undo_available(self)
        if not changed:return advanced,updated
        root=_configured_ingest_folder(self.settings)
        retained=set()
        for path in changed:
            self._state_cache.pop(path,None);getattr(self,"_editor_prefetch_cache",{}).pop(path,None)
            if root is not None and path.suffix.casefold() in IMG_EXTENSIONS and path.is_file() and path.resolve().is_relative_to(root.resolve()):
                retained.add(path)
        self.images=sorted((set(self.images)-changed)|retained,key=lambda path:str(path).casefold())
        self.selected.intersection_update(self.images)
        if self.current_path in changed and self.current_path not in self.images:
            self.current_path=None;self.current_record=None
            if panel:=getattr(self,"review_panel",None):panel.hide()
            if preview:=getattr(self,"preview",None):preview.clear()
        self.render_page()
        self.library_paths_changed.emit(tuple(sorted(changed,key=str)))
        if retained:self._start_state_scan(tuple(sorted(retained,key=str)))
        if self.current_path in retained:self.select_image(str(self.current_path))
        return advanced,updated

    def mark_selected(self,value: str,checked: bool):
        self.move_preview_notice.hide()
        path=Path(value)
        if checked: self.selected.add(path)
        else: self.selected.discard(path)
        self.update_selection()

    def replace_selection(self,values):
        self.move_preview_notice.hide()
        self.selected={Path(value) for value in values}; self.update_selection()

    def check_duplicates_from_gallery(self,paths):
        from .duplicate_preflight_dialog import run_gallery_duplicate_check
        run_gallery_duplicate_check(self,paths)

    def _gallery_file_action(self,paths,action_id: str):
        if IngestPage._file_work_blocked(self):return
        if action_id=="change_image" and len(paths)==1:
            from .image_change_goal_dialog import open_image_change_goal
            open_image_change_goal(self, Path(paths[0]))
            return
        if action_id=="image_setup":
            if paths: self._open_image_generation_setup(Path(paths[0]))
            return
        if action_id=="remix":
            if not paths:
                return
            from .generation_workflow import open_generation_workspace
            open_generation_workspace(self,paths=(Path(paths[0]),),remix=True)
            return
        if action_id=="animate_this_image":
            if not paths:
                return
            from .video_generation_workflow import open_video_generation_workspace
            open_video_generation_workspace(self,source_path=Path(paths[0]))
            return
        if action_id=="resize":
            from .resolution_dialog import open_resolution_workspace
            open_resolution_workspace(self,paths=tuple(Path(value) for value in paths))
            return
        if action_id=="review_selection":
            self.review_selected_data(paths=[Path(value) for value in paths])
            return
        handler={
            "library":self.batch_organize,
            "problems":self.batch_problems,
            "recycle":self.batch_recycle,
            "folder":self.move_active_to_folder,
        }.get(action_id)
        if handler is not None:
            handler([Path(value) for value in paths])

    def _refresh_inspector_actions(self):
        has_image=self.current_path is not None
        self.inspector_actions.setEnabled(has_image)
        self.inspector_setup_action.setEnabled(has_image and self.current_record is not None)
        self.inspector_setup_action.setToolTip(
            "Reuse selected saved Tags or Descriptions and recorded generator settings without changing the image."
            if self.current_record is not None else
            "Wait for this image's information, or repair its record, before creating a setup."
        )

    def _inspector_file_action(self,action_id: str):
        path=self.current_path
        if path is None:return
        if action_id=="image_setup" and self.current_record is None:
            QMessageBox.information(self,"Image information unavailable",
                "Wait for this image's information, or repair its record, before creating a generation setup.")
            return
        if not path.is_file():
            QMessageBox.information(self,"Inspected image unavailable",
                "This image is no longer at its inspected location. Refresh Ingest and choose an available image before opening Image Actions.")
            return
        self._gallery_file_action((path,),action_id)

    def _open_image_generation_setup(self,path):
        if path is None or IngestPage._file_work_blocked(self): return
        from .generation_workflow import open_generation_workspace
        open_generation_workspace(self,paths=(Path(path),),setup_from_image=True)

    def select_page(self):
        self.move_preview_notice.hide()
        self.selected.update(self.visible_paths()); self.render_page()

    def select_all(self):
        self.move_preview_notice.hide()
        visible,_,_=self._available_paths(); self.selected.update(visible); self.render_page()

    def clear_selection(self):
        self.move_preview_notice.hide()
        self.selected.clear(); self.render_page()

    def _confirmed_empty_inventory(self) -> bool:
        """Only a successful, idle folder scan can simplify first-use chrome."""
        settings=getattr(self,"settings",None)
        if settings is None:return False
        path=_configured_ingest_folder(settings)
        report=getattr(self,"_attention_report",None)
        return bool(
            path is not None and path.is_dir()
            and getattr(self,"_loaded_folder","")==str(path.resolve())
            and not self.images and getattr(self,"_scan_task",None) is None
            and not getattr(report,"items",())
        )

    def _sync_empty_inventory_presentation(self) -> None:
        empty=IngestPage._confirmed_empty_inventory(self)
        no_folder=bool(
            _configured_ingest_folder(self.settings) is None and not self.images
            and not any(item.status=="held" for item in getattr(self,"_startup_add_recovery_results",()))
            and not getattr(getattr(self,"_organization_scope",None),"recovery_operation_id","")
        )
        self._no_ingest_folder=no_folder
        if panel:=getattr(self,"filter_panel",None):panel.setVisible(not empty and not no_folder)
        if panel:=getattr(self,"no_ingest_panel",None):panel.setVisible(no_folder)
        if panel:=getattr(self,"empty_ingest_panel",None):panel.setVisible(empty)
        if gallery:=getattr(self,"gallery_scroll",None):gallery.setVisible(not no_folder and not empty)
        if toolbar:=getattr(self,"gallery_toolbar",None):toolbar.setVisible(not no_folder and not empty)
        if button:=getattr(self,"summary_visibility_button",None):
            button.setEnabled(not empty and not no_folder)
            if no_folder:
                button.setToolTip("An image summary is available after an Ingest folder is chosen and an image is found.")
            elif empty:
                button.setToolTip("An image summary is available after an incoming image is found.")
            else:
                IngestPage._update_summary_visibility_button(self)
        queue=getattr(self,"_queue_responsive_layout",None)
        if callable(queue):queue()

    def render_page(self):
        filter_widget=getattr(self,"resolution_filter",None)
        if filter_widget is not None:filter_widget.observe_population(self.images)
        available,_hidden,total=self._available_paths(); size=max(12,int(self.settings.gallery_page_size)); pages=max(1,(len(available)+size-1)//size); self.page=min(self.page,pages-1)
        visible=available[self.page*size:(self.page+1)*size]
        policy=content_safety_policy(); missing_ratings=[]
        if policy is not None and (
            self.settings.gallery_hidden_ratings or self.settings.gallery_content_level!="explicit"
            or getattr(self.settings,"gallery_blur_unknown",True)
        ):
            missing_ratings=policy.unclassified(visible)
        if self.current_path not in available:
            self.current_path=None; self.current_record=None; self.review_panel.hide()
            self._refresh_inspector_actions()
            self._queue_responsive_layout()
        columns=self.gallery_columns(); self._last_gallery_columns=columns
        configured_ingest=_configured_ingest_folder(self.settings) is not None
        self.gallery.set_paths(
            visible,columns=columns,card_width=self.settings.gallery_card_width,show_marks=True,allow_edit=True,selected=self.selected,
            active=self.current_path,configure=self._configure_gallery_card,
            empty_text=(
                "Protected unknown previews remain blurred until their content rating is available."
                if missing_ratings else
                "No incoming images match this state. Choose another state above."
                if self.images else
                "No incoming images while Add Images recovery is pending."
                if (any(item.status=="held" for item in getattr(self,"_startup_add_recovery_results",()))
                    or (getattr(self,"_organization_scope",OrganizationHistoryScope()).recovery_operation_id
                        and getattr(self,"_organization_action","")=="add")) else
                "Choose Ingest Folder… on this page before adding images."
                if not configured_ingest else
                "Add images to prepare and review them before moving them into your library. Choose Add Images… above. If you placed files in the Ingest folder yourself, choose Scan Again."
            ),
        )
        self.cards=self.gallery.cards
        if missing_ratings: QTimer.singleShot(0,lambda values=missing_ratings:self.content_ratings_requested.emit(values))
        # Tabs own stage totals; repeat a gallery count only when filtering
        # changes that scope. Reuse the same cached projection as the tabs.
        stage=IngestPage.current_stage(self)
        stage_count=(
            IngestPage._ordering_projection(self).stage_counts[stage]
            if stage!="all" else total
        )
        gallery_summary=f"{total:,} incoming"
        if len(available)!=stage_count:
            gallery_summary=f"{len(available):,} matching • "+gallery_summary
        self.count.setText(
            gallery_summary
            if self.images else
            "Choose an Ingest folder • no application files are scanned"
            if not configured_ingest else
            "0 incoming"
        )
        self.count.setVisible(True)
        self.page_label.setText(
            f"Page {self.page+1} of {pages}"
            if available else "No images on this page"
        )
        self.previous.setEnabled(self.page>0); self.next.setEnabled(self.page+1<pages)
        self.previous.setVisible(pages>1); self.next.setVisible(pages>1); self.page_label.setVisible(pages>1)
        self.gallery_density.setVisible(bool(total))
        state_filter=getattr(self,"state_filter",None)
        self.state_filter_control.setVisible(
            bool(self.images) and state_filter is not None
            and state_filter.count()>1
        )
        if hasattr(self,"filter_panel"):
            self.filter_panel.sync()
        IngestPage._sync_empty_inventory_presentation(self)
        self.select_page_button.setText(f"Select Page ({len(visible):,})")
        self.select_all_button.setText("Select All")
        selection_scope=f"Select all {len(available):,} matching images across all pages."
        self.select_all_button.setToolTip(selection_scope)
        self.select_all_button.setAccessibleDescription(selection_scope)
        self._gallery_scope_paths=frozenset(available)
        self.selection_header.setVisible(bool(available or self.selected))
        self.batch_actions.setVisible(bool(available or self.selected))
        self.organize_button.setVisible(bool(total))
        update_stage_counts=getattr(self,"_update_stage_tab_counts",None)
        if callable(update_stage_counts):
            update_stage_counts()
        self.update_selection()
        self._queue_visible_state_scan()

    def _queue_visible_state_scan(self):
        if getattr(self,"_shutdown_requested",False):return
        # The deferred callback belongs to this Qt page, not a process-wide timer.
        QTimer.singleShot(0,self,self._scan_visible_states_if_idle)

    def _scan_visible_states_if_idle(self):
        if getattr(self,"_shutdown_requested",False):return
        if (
            self._state_scan_task is not None or self._scan_task is not None
            or self._batch_task is not None
        ):
            return
        missing=[path for path in self.visible_paths() if path not in self._state_cache]
        if missing:
            IngestPage._queue_missing_state_scan(self,missing)

    def _configure_gallery_card(self,path,card):
        card.set_generation_actions(True)
        card.allow_duplicate_check=True
        duplicate=self.state.duplicates.badge_for_path(path)
        card.set_duplicate_candidate(
            duplicate.kind if duplicate is not None else "",
            match_count=duplicate.match_count if duplicate is not None else 0,
            scope_label=duplicate.scope_label if duplicate is not None else "",
            exact_relations=(
                duplicate.exact_relation_count if duplicate is not None else 0
            ),
            visual_relations=(
                duplicate.visual_relation_count if duplicate is not None else 0
            ),
            truncated=bool(duplicate is not None and duplicate.truncated),
        )
        card.set_compact_mode(
            True,review_implied=IngestPage.current_stage(self)=="unprepared",
            problem_implied=IngestPage.current_stage(self)=="partial",
        )
        review_actions=(
            (("review_selection","Review Image…","Open Selected as Review Queue…"),)
            if len(self.selected)>1 and path in self.selected else ()
        )
        card.set_file_actions((
            ("resize","Resize This Image…","Resize Selected Images…"),
            ("library","Move This Image to Named Library…","Move Selected Images to Named Library…"),
            ("problems","Move This Image to Problems…","Move Selected Images to Problems…"),
            ("recycle","Move This Image to Recycle Bin…","Move Selected Images to Recycle Bin…"),
            ("folder","Move This Image to Folder…","Move Selected Images to Folder…"),
        )+review_actions)
        attention_display=self._attention_report.display_name_for(path)
        if (
            self._attention_items_for_path(path)
            and attention_display.casefold()!=Path(path).name.casefold()
        ):
            card.set_context_note(
                attention_display,
                full_text=f"Incoming image location: {attention_display}",
            )
        else:
            card.set_context_note("")
        _,status,detail,note=self._state_for(path); card.set_visual_status(status,detail); card.set_note(note.replace("Saved — ","").capitalize())
        cached=self._state_cache.get(path)
        history=cached[2] if cached and len(cached)>2 else (None,None)
        card.set_ai_scan_history(*history)
        card.set_knowledge_coverage(cached[4] if cached and len(cached)>4 else None)

    def change_page(self,direction): self.page=max(0,self.page+direction); self.render_page()

    def gallery_columns(self):
        width=max(400,self.gallery_scroll.viewport().width())
        return max(2,min(14,width//(max(112,int(self.settings.gallery_card_width))+12)))

    def gallery_density_changed(self,page_size=None,card_width=None):
        if page_size is None or card_width is None: page_size,card_width=self.gallery_scale.current_data()
        self.settings.gallery_page_size=int(page_size); self.settings.gallery_card_width=int(card_width); self.settings.save()
        self.page=0; self.render_page()

    def _update_stage_tab_counts(self) -> None:
        tabs=getattr(self,"stage_tabs",None)
        if tabs is None:
            return
        counts=IngestPage._ordering_projection(self).stage_counts
        labels={
            "unprepared":"Unprepared","partial":"Partially Prepared",
            "complete":"Complete",
        }
        for index in range(tabs.count()):
            key=str(tabs.tabData(index))
            tabs.setTabText(index,f"{labels[key]} ({counts[key]:,})")

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

    def show_current_inspector(self):
        if self.current_path is None:
            return
        self._set_summary_visible(True)
        self._narrow_inspector_open=True
        self.update_responsive_layout()

    def show_ingest_gallery(self):
        self._narrow_inspector_open=False
        self.update_responsive_layout()

    def toggle_summary(self):
        self._set_summary_visible(not self._summary_visible)

    def _set_summary_visible(self,visible: bool,*,persist: bool=True):
        self._summary_visible=bool(visible)
        if not self._summary_visible:
            self._narrow_inspector_open=False
        elif self._ingest_narrow_layout and self.current_path is not None:
            self._narrow_inspector_open=True
        if persist and self.ui_settings is not None:
            self.ui_settings.setValue(
                "ingest/summary_visible",self._summary_visible,
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
            "Expand Ingest summary" if not self._summary_visible else
            "Collapse Ingest summary to give the gallery more space"
        )
        button.setAccessibleDescription(button.toolTip())

    def _sync_gallery_toolbar_layout(self,narrow: bool) -> None:
        if narrow==getattr(self,"_gallery_toolbar_narrow",False):
            return
        primary=self.gallery_primary_row
        secondary=self.gallery_filter_row
        if narrow:
            primary.removeWidget(self.gallery_density)
            secondary.insertWidget(0,self.gallery_density)
        else:
            secondary.removeWidget(self.gallery_density)
            primary.addWidget(self.gallery_density)
        self._gallery_toolbar_narrow=narrow

    def _sync_adaptive_panes(self) -> None:
        narrow=self.width()<1050
        was_narrow=getattr(self,"_ingest_narrow_layout",False)
        if narrow and not was_narrow:
            self._narrow_inspector_open=bool(
                self.current_path is not None
                and not self.review_panel.isHidden()
            )
        self._ingest_narrow_layout=narrow
        self._sync_gallery_toolbar_layout(narrow)
        self.preparation_controls.summary.setWordWrap(narrow)
        show_inspector=bool(
            self._summary_visible and narrow and self.current_path is not None
            and self._narrow_inspector_open
        )
        self.inspector_back_button.setVisible(show_inspector)
        if narrow:
            self.gallery_side.setVisible(not show_inspector)
            self.review_panel.setVisible(show_inspector)
        else:
            self.gallery_side.show()
            self.review_panel.setVisible(
                self._summary_visible and not IngestPage._confirmed_empty_inventory(self)
                and not getattr(self,"_no_ingest_folder",False)
            )
        self.inspect_current_button.setVisible(
            self._summary_visible and narrow and not show_inspector and self.current_path is not None
        )
        self._sync_review_added_button()

    def _sync_review_added_button(self):
        button=getattr(self,"review_added_button",None)
        if button is None:return
        paths=tuple(getattr(self,"_recent_ingest_paths",()))
        inspector_open=bool(
            getattr(self,"_summary_visible",False)
            and (not getattr(self,"_ingest_narrow_layout",False)
                 or getattr(self,"_narrow_inspector_open",False))
        )
        exact_action_visible=bool(
            len(paths)==1 and self.current_path==paths[0]
            and getattr(self,"current_record",None) is not None
            and getattr(self,"current_report",None) is not None
            and inspector_open
        )
        show=bool(paths) and not exact_action_visible
        button.setVisible(show)
        if panel:=getattr(self,"recent_import_panel",None):panel.setVisible(show)

    def update_responsive_layout(self):
        self._responsive_layout_timer.stop()
        self._sync_adaptive_panes()
        self.update_preview_pixmap()
        columns=self.gallery_columns()
        if self.images and getattr(self,"_last_gallery_columns",None)!=columns:
            self._last_gallery_columns=columns; self.gallery.set_columns(columns)

    def update_preview_pixmap(self):
        if self.current_pixmap.isNull(): return
        target=self.preview.size(); width=max(40,target.width()-12); height=max(40,target.height()-12)
        self.preview.setPixmap(self.current_pixmap.scaled(width,height,Qt.KeepAspectRatio,Qt.SmoothTransformation))

    @staticmethod
    def _editor_source_signature(path: Path):
        path=Path(path)
        values=[]
        for item in (path,path.with_suffix(".tags.json"),path.with_suffix(".txt")):
            try:
                stat=item.stat()
                values.append((stat.st_mtime_ns,stat.st_size))
            except FileNotFoundError:
                values.append(None)
        return tuple(values)

    def select_image(self,value: str,*,prefetched=None):
        self._image_selection_generation=getattr(self,"_image_selection_generation",0)+1
        self._complete_inspected_image=False
        active=getattr(self,"_image_inspection_task",None)
        if active is not None:active.cancel()
        self._pending_image_inspection=None
        path=Path(value); self.current_path=path
        self.image_name.setText(path.name)
        self.editor_hint.setText(
            "Double-click a card, press Enter, or use Image Actions to edit image data."
        )
        self._queue_responsive_layout()
        for card_path,card in self.cards.items(): card.set_active_selected(card_path==str(path))
        # The main Ingest surface intentionally uses the gallery instead of a large
        # duplicate preview. Avoid decoding the full-resolution image on every click.
        self.current_pixmap=QPixmap()
        self.current_record=prefetched[0] if prefetched is not None else None
        self.current_report=prefetched[1] if prefetched is not None else None
        self._refresh_inspector_actions()
        self._current_editor_preview=prefetched[2] if prefetched is not None else None
        self._current_editor_source_signature=(
            self._editor_source_signature(path) if prefetched is not None else None
        )
        self.found.clear(); self.rows=[]
        self.inline_suggestions.clear(); self.inline_suggestions.hide(); self.additional_label.hide(); self._pending_imports=[]
        self._set_quick_review_count(0)
        self._set_summary_action()
        self.recommended_action=""; self.recommended_button.setVisible(True); self.recommended_button.setText("Reading…"); self.recommended_button.setEnabled(False)
        self.update_summary()
        if prefetched is not None:
            self.populate_found()
            if self._reopen_editor_after_inspection:
                self._reopen_editor_after_inspection=False; QTimer.singleShot(0,self.open_data_editor)
            return
        _set_ingest_footer_activity(
            self,"Gathering TXT, organized image data, and Metadata…",
        )
        self._pending_image_inspection=(path,self._image_selection_generation)
        self._start_pending_image_inspection()

    def _start_pending_image_inspection(self):
        if getattr(self,"_image_inspection_task",None) is not None:return
        pending=getattr(self,"_pending_image_inspection",None)
        self._pending_image_inspection=None
        if pending is None or getattr(self,"_shutdown_requested",False):return
        path,generation=pending
        if path!=self.current_path or generation!=self._image_selection_generation:return
        task=_InspectionTask(path,self.state.knowledge)
        self._image_inspection_task=task
        context=self._editor_context_token()
        task.signals.complete.connect(
            lambda value,result,owner=task,g=generation,c=context:
                self._selected_image_inspected(owner,g,c,value,result)
        )
        task.signals.failed.connect(
            lambda value,message,owner=task,g=generation,c=context:
                self._selected_image_inspection_failed(owner,g,c,value,message)
        )
        task.signals.finished.connect(
            lambda owner=task:self._selected_image_inspection_finished(owner)
        )
        # One active read plus one latest selection; obsolete clicks never
        # queue an unbounded series of Metadata reads behind thumbnails.
        self.pool.start(task,1)

    def _selected_image_inspected(self,task,generation,context,value,result):
        if (getattr(self,"_shutdown_requested",False)
            or generation!=self._image_selection_generation or Path(value)!=self.current_path):return
        if context!=self._editor_context_token() or task.signature!=self._editor_source_signature(Path(value)):
            self._pending_image_inspection=(Path(value),generation)
            return
        self._current_editor_source_signature=task.signature
        self._inspection_complete(value,result)

    def _selected_image_inspection_finished(self,task):
        if task is self._image_inspection_task:
            self._image_inspection_task=None
            self._start_pending_image_inspection()

    def _selected_image_inspection_failed(self,task,generation,context,value,message):
        if (getattr(self,"_shutdown_requested",False)
            or task is not getattr(self,"_image_inspection_task",None)
            or generation!=self._image_selection_generation or Path(value)!=self.current_path):return
        if context!=self._editor_context_token() or task.signature!=self._editor_source_signature(Path(value)):
            self._pending_image_inspection=(Path(value),generation)
            return
        self._inspection_failed(value,message)

    def _inspection_complete(self,path: str,result):
        if not self.current_path or str(self.current_path)!=path: return
        _clear_ingest_footer_activity(
            self,"Gathering TXT, organized image data, and Metadata",
        )
        self.current_record,self.current_report=result[:2]
        self.populate_found()
        if len(result)>2:
            self._complete_inspected_image=False
            self.recommendation.setText(
                "Some information sources could not be read. You can still add information manually."
            )
            self._set_summary_action(
                "editor","Edit Image Data…",
                "Open the image-data editor without changing TXT or Metadata merely because a source read failed.",
            )
            QMessageBox.warning(
                self,"Some information could not be read",str(result[2]),
            )
        if self._reopen_editor_after_inspection:
            self._reopen_editor_after_inspection=False; QTimer.singleShot(0,self.open_data_editor)

    def _inspection_failed(self,path: str,message: str):
        if not self.current_path or str(self.current_path)!=path: return
        self._complete_inspected_image=False
        _clear_ingest_footer_activity(
            self,"Gathering TXT, organized image data, and Metadata",
        )
        kind,separator,detail=message.partition(":")
        if kind=="record" and separator:
            if self._attention_items_for_path(Path(path)):
                self._show_attention_issue(Path(path)); return
            QMessageBox.critical(self,"Could not inspect image",detail); return
        message=detail if kind=="evidence" and separator else message
        self.recommendation.setText("Some information sources could not be read. You can still add information manually.")
        self._set_summary_action(
            "editor","Edit Image Data…",
            "Open the image-data editor without changing TXT or Metadata merely because a source read failed.",
        )
        QMessageBox.warning(self,"Some information could not be read",message)
        if self._reopen_editor_after_inspection:
            self._reopen_editor_after_inspection=False; QTimer.singleShot(0,self.open_data_editor)

    def populate_found(self):
        self._complete_inspected_image=False
        IngestPage._set_readiness_details(self)
        self.found.clear(); self.rows=[]; record=self.current_record; report=self.current_report
        edit_button=getattr(self,"summary_edit_button",None)
        if edit_button is not None:edit_button.hide()
        if record is None or report is None:
            IngestPage._sync_review_added_button(self)
            return
        self.update_summary()
        new_items=0
        sidecar=record.sidecar_path.is_file(); existing={normalize_tag(tag) for tag in record.annotation.all_structured_tags()}
        existing_flat={normalize_tag(tag) for tag in record.annotation.sdxl_tags}
        for tag,sources in report.tag_sources().items():
            saved=sidecar and normalize_tag(tag) in existing; suggested=not saved
            if suggested: new_items+=1
            checked=saved or "TXT caption" in sources or len(sources)>1
            self._row(tag,"SDXL Tag",", ".join(sources),"Already saved" if saved else "Ready to review",checked,not saved or normalize_tag(tag) in existing_flat)
        descriptions={}
        for source in report.sources:
            for text in source.plain_language: descriptions.setdefault(text,[]).append(source.name)
        for text,sources in descriptions.items():
            saved=sidecar and text.casefold() in {value.casefold() for value in record.annotation.plain_language}
            if not saved: new_items+=1
            self._row(text,"Plain Language Description",", ".join(sources),"Already saved" if saved else "Ready to review",saved or "TXT caption" in sources or len(sources)>1,True)
        for evidence in record.annotation.ai_evidence:
            for prediction in evidence.typed_tags():
                tag=str(prediction.get("tag") or ""); confidence=float(prediction.get("confidence",0))
                decision=evidence.decisions.get(normalize_tag(tag),"")
                if decision!="accepted": new_items+=1
                self._row(tag,"AI Tag",evidence.model_id,f"{confidence*100:.0f}% confidence",decision=="accepted" or (not decision and confidence>=.8),decision!="accepted")
            if evidence.typed_plain_language():
                if evidence.decisions.get("plain_language")!="accepted": new_items+=1
                self._row(evidence.typed_plain_language(),"AI Description",evidence.model_id,"Suggested",evidence.decisions.get("plain_language")=="accepted",True)
        new_items=self.populate_inline_suggestions()
        if new_items:
            self.recommendation.setText(
                f"{new_items:,} additional item(s) were found in the sources "
                "listed below. Review them in the image data editor to add, "
                "ignore for this image, or decide later."
            )
            IngestPage._set_summary_action(self)
            self.recommended_action="editor"
            self.recommended_button.setVisible(True)
            self.recommended_button.setText(f"Review Findings ({new_items:,})")
            self.recommended_button.setEnabled(True)
        elif record.annotation.all_structured_tags() or record.annotation.plain_language:
            workflow=self.workflow_states.assess(record.image_path)
            if workflow.ready:
                tag_scan=getattr(workflow,"last_sdxl_ai_scan","")
                description_scan=getattr(workflow,"last_plain_language_ai_scan","")
                lines=[
                    "Image Knowledge complete means useful confirmed Tags and Descriptions are present, "
                    "and no blocking Image Knowledge issue was found. "
                    "This image can be included in Move to Library; selected-profile, dataset, "
                    "and AI-coverage checks remain separate.",
                    (
                        "AI scan coverage: "
                        f"AI Tags — {'completed' if tag_scan else 'no completed run'}; "
                        "AI Descriptions — "
                        f"{'completed' if description_scan else 'no completed run'}."
                    ),
                ]
                if not record.annotation.plain_language:
                    IngestPage._set_summary_action(
                        self,
                        "editor","Add Description…",
                        "Open the image-data editor to add a Description. No image data changes until you save there.",
                    )
                else:
                    IngestPage._set_summary_action(self)
                if edit_button is not None and self.summary_action!="editor":edit_button.show()
                self.recommended_action=""
                self.recommended_button.hide()
                IngestPage._set_readiness_details(self,"\n\n".join(lines))
                self._complete_inspected_image=True
                IngestPage._sync_complete_move_recommendation(self)
            else:
                self.recommendation.setText(
                    IngestPage._workflow_recommendation(workflow)
                )
                IngestPage._set_summary_action(
                    self,
                    "editor","Edit Image Data…",
                    "Open the image-data editor to address the listed Image Knowledge issue. No image data changes until you save there.",
                )
                self.recommended_action=""; self.recommended_button.hide()
        else:
            self.recommendation.setText("No Tags or Descriptions were found. Edit the image data manually, or use AI on the selected images.")
            IngestPage._set_summary_action(
                self,
                "editor","Edit Image Data…",
                "Open the image-data editor to add confirmed Tags or Descriptions. No image data changes until you save there.",
            )
            self.recommended_action=""; self.recommended_button.hide()
        IngestPage._sync_review_added_button(self)

    def _set_readiness_details(self,text=""):
        toggle=getattr(self,"readiness_details_toggle",None)
        if toggle is None:return
        toggle.setChecked(False)
        self.readiness_details.hide()
        self.readiness_details.setText(text)
        toggle.setVisible(bool(text))

    @staticmethod
    def _score_value(value: float) -> str:
        return f"{value:.0f}" if float(value).is_integer() else f"{value:.1f}"

    @classmethod
    def _workflow_recommendation(cls, workflow) -> str:
        status="No new source information needs review."
        label=str(workflow.label or "Needs Image Knowledge review").rstrip(".")
        sections=[
            f"<p><b>{escape(status)}</b><br>{escape(label)}.</p>"
        ]
        if workflow.key=="problem" and workflow.detail:
            sections.append(
                f"<p><b>Blocking issue</b><br>{escape(str(workflow.detail))}</p>"
            )
        if workflow.score_components:
            labels={
                "authoritative_record":"Confirmed Image Knowledge",
                "review_resolution":"Review score",
            }

            def component_text(components):
                return " &nbsp;•&nbsp; ".join(
                    f"{labels.get(component.key,component.key.replace('_',' ').title())} "
                    f"{cls._score_value(component.score)}/{cls._score_value(component.maximum)}"
                    for component in components
                )

            score_line=f"Image Knowledge completeness: {workflow.score}/100."
            sections.append(
                f"<p><b>{escape(score_line)}</b><br>"
                f"{escape('Components: ')}{component_text(workflow.score_components)}.</p>"
            )
        elif workflow.score:
            sections.append(
                f"<p><b>Current completeness score: {workflow.score}/100.</b></p>"
            )
        else:
            sections.append(f"<p>{escape(str(workflow.detail))}</p>")

        deductions=sorted(
            (issue for issue in workflow.score_issues if issue.deduction>0),
            key=lambda issue:(-issue.deduction,issue.code),
        )
        review_warnings=[
            issue for issue in workflow.score_issues
            if issue.deduction==0 and issue.code in {
                "typed_tag_boundary_review","typed_description_boundary_review",
            }
        ]
        if review_warnings:
            sections.append(
                "<p><b>Review required despite the completeness score</b><br>"
                "These warnings deduct no points, but need a decision before this image is complete.</p><ul>"
                +"".join(f"<li>{escape(str(issue.label))}</li>" for issue in review_warnings)
                +"</ul>"
            )
        if deductions:
            items=[]
            for issue in deductions:
                area=(
                    "Image record"
                    if issue.component in {"authoritative_record","review_resolution"}
                    else "File/source check"
                )
                items.append(
                    "<li>"
                    f"{escape(area)}: {escape(str(issue.label))} "
                    f"(−{cls._score_value(issue.deduction)} points)"
                    "</li>"
                )
            sections.append(
                "<p><b>Why it needs attention</b></p><ul>"
                +"".join(items)+"</ul>"
            )
        if workflow.next_image_knowledge_action:
            sections.append(
                "<p><b>Next step</b><br>"
                f"{escape(str(workflow.next_image_knowledge_action))}</p>"
            )
        if workflow.training_export_health_label:
            health_line=(
                f"{workflow.training_export_profile_id}: "
                f"{workflow.training_export_health_label}."
            )
            sections.append(
                "<p><b>Training Export Health</b><br>"
                f"{escape(health_line)}<br>"
                "This optional output does not lower Image Knowledge completeness.</p>"
            )
        sections.append(
            "<p>Double-click the card or press Enter to review or correct this image.</p>"
        )
        return "".join(sections)

    def populate_inline_suggestions(self):
        self.inline_suggestions.clear(); self._pending_imports=[]; record=self.current_record; report=self.current_report
        if record is None or report is None:
            self.inline_suggestions.hide(); self.additional_label.hide(); return 0
        confirmed_tag_values=record.annotation.all_structured_tags()
        identity_values=list(confirmed_tag_values)
        identity_values.extend(
            value for source in report.sources for value in source.tags
        )
        findings=record.annotation.source.get("candidate_findings",[])
        if isinstance(findings,list):
            identity_values.extend(
                str(finding.get("value") or "").strip()
                for finding in findings
                if isinstance(finding,dict) and finding.get("kind")=="tag"
            )
        identity_values.extend(
            str(prediction.get("tag") or "").strip()
            for evidence in record.annotation.ai_evidence
            for prediction in evidence.typed_tags()
        )
        identity_plan=ConfirmedTagIdentityService(
            self.state.knowledge
        ).plan(
            identity_values,
            stored_ids=record.annotation.tag_identities,
            confirmation=False,
        )
        identity_entries={
            entry.normalized_value:entry for entry in identity_plan.entries
        }
        confirmed_identity_ids={
            entry.stable_id
            for value in confirmed_tag_values
            if (
                entry:=identity_entries.get(normalize_tag(value))
            ) is not None and entry.stable_id
        }
        confirmed_tags={normalize_tag(value) for value in confirmed_tag_values}
        automation=active_automation_engine(self.state.knowledge)
        ignored_tags={normalize_tag(value) for value in record.annotation.source.get("rejected_tags",[]) if normalize_tag(value)}
        ignored_tags.update(
            normalize_tag(value)
            for value in record.annotation.confirmed_absent
            if normalize_tag(value)
        )
        ignored_tags.update(
            normalize_tag(tag) for evidence in record.annotation.ai_evidence
            for tag,decision in evidence.decisions.items()
            if evidence.kind=="sdxl_tags" and decision=="rejected" and normalize_tag(tag)
        )
        confirmed_prose={
            normalize_plain_language_exclusion(value)
            for value in record.annotation.plain_language
            if normalize_plain_language_exclusion(value)
        }
        ignored_prose={
            normalize_plain_language_exclusion(sentence)
            for value in record.annotation.source.get("rejected_descriptions",[])
            for sentence in split_plain_language(value)
            if normalize_plain_language_exclusion(sentence)
        }
        ignored_prose.update(
            normalize_plain_language_exclusion(sentence)
            for evidence in record.annotation.ai_evidence
            if evidence.typed_plain_language() and evidence.decisions.get("plain_language")=="rejected"
            for sentence in split_plain_language(evidence.typed_plain_language())
            if normalize_plain_language_exclusion(sentence)
        )
        seen={}
        globally_ignored={
            normalize_plain_language_exclusion(value)
            for value in getattr(self.settings,"ignored_plain_language_sentences",[])
            if normalize_plain_language_exclusion(value)
        }
        current_source_keys=current_evidence_keys_by_source(report)

        def add(value,kind,source):
            value=str(value or "").strip()
            if not value: return
            is_tag="tag" in kind.casefold()
            normalized=normalize_tag(value) if is_tag else normalize_plain_language_exclusion(value)
            if is_tag:
                identity=identity_entries.get(normalized)
                if identity is not None and identity.stable_id:
                    if identity.stable_id in confirmed_identity_ids:
                        return
                    value=identity.canonical_display or value
                    normalized=normalize_tag(value)
            key=("tag" if is_tag else "description",normalized)
            if key in seen:
                item=seen[key]
                sources=[]
                for raw_source in (item.data(2,Qt.UserRole) or item.text(2),source):
                    for raw in str(raw_source or "Unknown").split(", "):
                        clean=raw.strip()
                        if clean and clean not in sources: sources.append(clean)
                merged=", ".join(sources or ["Unknown"])
                item.setData(2,Qt.UserRole,merged); item.setText(2,merged); item.setToolTip(2,merged)
                return
            if ((is_tag and (
                        normalized in confirmed_tags|ignored_tags
                        or automation.is_evidence_rejected(value)
                    ))
                    or (not is_tag and (normalized in confirmed_prose
                        or normalized in ignored_prose|globally_ignored))): return
            self._pending_imports.append((value,kind,source))
            item=QTreeWidgetItem((value,kind,source)); item.setFlags(item.flags()&~Qt.ItemIsUserCheckable)
            item.setData(2,Qt.UserRole,source); item.setToolTip(0,value); item.setToolTip(2,source)
            seen[key]=item; self.inline_suggestions.addTopLevelItem(item)

        for source in report.sources:
            for value in source.tags: add(value,"SDXL Tag",source.name)
            for raw_value in source.plain_language:
                for value in split_plain_language(raw_value): add(value,"Plain Language Description",source.name)
        for finding in record.annotation.source.get("candidate_findings",[]):
            if not isinstance(finding,dict): continue
            source_values=[str(source) for source in finding.get("sources",[]) if str(source).strip()]
            if finding.get("kind")=="tag":
                value=finding.get("value")
                active_sources=current_candidate_sources(
                    value,"tag",source_values,current_source_keys,
                )
                if source_values and not active_sources:
                    continue
                sources=", ".join(active_sources) or "Cached source"
                add(value,"SDXL Tag",sources)
            elif finding.get("kind")=="description":
                for value in split_plain_language(finding.get("value")):
                    active_sources=current_candidate_sources(
                        value,"description",source_values,current_source_keys,
                    )
                    if source_values and not active_sources:
                        continue
                    sources=", ".join(active_sources) or "Cached source"
                    add(value,"Plain Language Description",sources)
        for evidence in record.annotation.ai_evidence:
            for prediction in evidence.typed_tags():
                value=prediction.get("tag"); decision=evidence.decisions.get(normalize_tag(str(value or "")),"")
                if decision not in {
                    "accepted","rejected",
                    *ImageFindingDecisionService.DEFERRED_VALUES,
                }:
                    add(value,"AI Tag",evidence.model_id)
            if (
                evidence.typed_plain_language()
                and evidence.decisions.get("plain_language") not in {
                    "accepted","rejected",
                    *ImageFindingDecisionService.DEFERRED_VALUES,
                }
            ):
                for value in split_plain_language(evidence.typed_plain_language()): add(value,"AI Description",evidence.model_id)
        count=self.inline_suggestions.topLevelItemCount(); self.inline_suggestions.setVisible(count>0)
        self.additional_label.setText(f"Additional information ({count:,})"); self.additional_label.setVisible(count>0)
        IngestPage._set_quick_review_count(self,count)
        return count

    def _set_quick_review_count(self,count: int) -> None:
        if not hasattr(self,"inspect_tabs"):
            return
        index=self.inspect_tabs.indexOf(self.quick_review_tab)
        if index>=0:
            visible=int(count)>0
            if not visible and self.inspect_tabs.currentWidget() is self.quick_review_tab:
                self.inspect_tabs.setCurrentWidget(self.next_step_tab)
            self.inspect_tabs.setTabVisible(index,visible)
            self.inspect_tabs.setTabText(
                index,"Quick Review"+(f" ({int(count):,})" if count else ""),
            )

    def _set_summary_action(
        self, action: str="", text: str="", tooltip: str="",
    ) -> None:
        self.summary_action=str(action or "")
        button=getattr(self,"summary_action_button",None)
        if button is None:
            return
        button.setVisible(bool(self.summary_action))
        button.setEnabled(bool(self.summary_action))
        if text:
            button.setText(text)
        button.setToolTip(tooltip)
        button.setAccessibleDescription(tooltip)

    def add_inline_suggestions(self):
        # Compatibility entry point for older callers. Candidate decisions now
        # always use the universal editor and never bulk-accept raw evidence.
        self.open_data_editor()

    def _description_source_sequences(self):
        sequences=[]
        if self.current_report:
            for source in self.current_report.sources:
                values=[sentence for raw in source.plain_language for sentence in split_plain_language(raw)]
                if values: sequences.append(values)
        if self.current_record:
            findings=self.current_record.annotation.source.get("candidate_findings",[])
            values=[
                sentence for finding in findings if isinstance(finding,dict) and finding.get("kind")=="description"
                for sentence in split_plain_language(finding.get("value"))
            ] if isinstance(findings,list) else []
            if values: sequences.append(values)
            for evidence in self.current_record.annotation.ai_evidence:
                values=split_plain_language(evidence.typed_plain_language())
                if values: sequences.append(values)
        return sequences

    def update_summary(self):
        IngestPage._set_readiness_details(self)
        record=self.current_record; report=self.current_report
        self._refresh_inspector_actions()
        if record is None:
            self.confirmed_tags_group.setTitle("Confirmed Tags")
            self.confirmed_descriptions_group.setTitle("Confirmed Descriptions")
            self.sdxl_summary.setText("No confirmed Tags")
            self.plain_summary.setText("No confirmed Descriptions")
            self.source_summary.setText("No sources inspected")
            return
        raw_tags=ConfirmedTagIdentityService(
            self.state.knowledge
        ).canonical_displays(
            record.annotation.all_structured_tags(),
            stored_ids=record.annotation.tag_identities,
        )
        ordering=TagOrderingService(self.state.knowledge,self.characters)
        tags=ordering.order(
            [TagOrderCandidate(value,sequence=index) for index,value in enumerate(raw_tags)],
            canonicalize=False,deterministic=True,
        )
        self.confirmed_tags_group.setTitle(f"Confirmed Tags ({len(tags):,})")
        self.sdxl_summary.setText("  •  ".join(tags) if tags else "No confirmed Tags")
        prose=record.annotation.plain_language
        self.confirmed_descriptions_group.setTitle(
            f"Confirmed Descriptions ({len(prose):,})"
        )
        self.plain_summary.setText(
            "\n\n".join(f"• {value}" for value in prose)
            if prose else "No confirmed Descriptions"
        )
        if not report:
            self.source_summary.setText("Reading TXT, Image Knowledge record, and Metadata…")
            return
        parts=[]
        for source in report.sources:
            if not source.available: continue
            details=[]
            if source.tags: details.append(f"{len(source.tags):,} Tag(s)")
            if source.plain_language: details.append(f"{len(source.plain_language):,} Description(s)")
            if source.details: details.append("generation reference")
            parts.append(f"{source.name}: "+(", ".join(details) or "available"))
        if record.annotation.ai_evidence: parts.append(f"AI suggestions: {len(record.annotation.ai_evidence):,} result set(s)")
        self.source_summary.setText(
            "\n".join(f"• {value}" for value in parts)
            if parts else "No additional sources found"
        )

    def begin_sensitivity_refresh(self,paths):
        affected=set(map(Path,paths))
        active=set(getattr(self,"_state_scan_paths",()))
        if affected.intersection(active):
            self._state_scan_generation+=1
            self._sensitivity_resume_paths=active
        else:self._sensitivity_resume_paths=set()
        self.categories.load()
        self.workflow_states.knowledge=self.state.knowledge
        self._readiness_context_fingerprint=self._workflow_context_fingerprint()
        self._prepared_editor_context=None
        self._clear_editor_prefetch()

    def sensitivity_refresh_incomplete(self,groups):
        from core.image_workflow_state import sensitivity_cached_paths
        inputs={path:(cached[5] if len(cached)>5 else None)
                for path,cached in self._state_cache.items()}
        for path in getattr(self,"_state_scan_paths",()):inputs.setdefault(path,None)
        affected=sensitivity_cached_paths(groups,inputs)
        if not affected:return
        self.begin_sensitivity_refresh(affected)
        self._sensitivity_resume_paths=set()
        for path in affected:self._state_cache.pop(path,None)
        self._trusted_workflow_paths.difference_update(affected)
        # Rendering schedules checks for the visible page only. Cancelling the
        # dependency job must not silently restart a complete-library scan.
        self.render_page()

    def sensitivity_results_changed(self,paths):
        affected=set(map(Path,paths)) & set(self.images)
        for path in affected:self._state_cache.pop(path,None)
        self._trusted_workflow_paths.difference_update(affected)
        if affected:
            self.render_page()
            resumed=affected | getattr(self,"_sensitivity_resume_paths",set())
            self._sensitivity_resume_paths=set()
            self._start_state_scan(sorted(resumed,key=str))

    def derived_results_changed(self,paths=(),*,scope="images",cause="Image Knowledge changed"):
        """Recheck only the completed refresh's Ingest members."""
        affected=set(map(Path,paths)) & set(self.images)
        if scope=="all" and not paths:affected=set(self.images)
        for path in affected:self._state_cache.pop(path,None)
        self._trusted_workflow_paths.difference_update(affected)
        if affected:
            self._start_state_scan(sorted(affected,key=str))
            self.render_page()

    def refresh_system_knowledge(self,*,preserve_workflow=False):
        """Refresh shared rules and invalidate only their derived projections."""
        self._prepared_editor_context=None
        self._clear_editor_prefetch()
        if self._defer_expensive_initialization:
            if self._conflict_task is not None:
                self._conflict_task.cancel(); self._conflict_task=None
            self._start_conflict_preparation()
        else:
            self.conflicts=ConflictRuleEngine.load(
                DEFAULTS_DIR/"conflicts.json",tags=self.state.knowledge,
            )
        self.workflow_states.knowledge=self.state.knowledge
        current_context=self._workflow_context_fingerprint()
        if current_context!=getattr(
            self,"_readiness_context_fingerprint","",
        ):
            self._readiness_context_fingerprint=current_context
            if not preserve_workflow:
                self._state_cache.clear()
                self._trusted_workflow_paths.clear()
                known=list(getattr(self,"_known_record_paths",()))
                self.render_page()
                if known:self._start_state_scan(known)
            elif self._state_scan_task is not None:
                # Keep completed entries, but don't publish an old worker result.
                self._state_scan_generation+=1
                self._start_state_scan(tuple(getattr(self,"_state_scan_paths",())))
        if self.current_record is None:
            return
        self.update_summary()
        self.populate_inline_suggestions()

    def _start_editor_queue(self):
        selected=self.selected_paths()
        if self.current_path in selected and len(selected)>1:
            self._editor_queue=selected
            self._editor_queue_index=selected.index(self.current_path)
        else:
            self._editor_queue=[self.current_path]
            self._editor_queue_index=0

    def review_added_images(self):
        """Open the exact import result without changing the gallery's scope."""
        if self._batch_task is not None:
            show_operation_in_progress(self,"Ingest")
            return
        paths=tuple(getattr(self,"_recent_ingest_paths",()))
        inventory=set(self.images)
        if not paths or any(path not in inventory for path in paths):
            QMessageBox.information(
                self,"Added images changed",
                "Some added images are no longer in this Ingest inventory. "
                "Use the gallery or Dataset to review their current location.",
            )
            return
        self.review_selected_data(paths=paths)

    def review_selected_data(self,*,paths=None):
        paths=self.selected_paths() if paths is None else list(paths)
        if not paths:
            QMessageBox.information(self,"Select incoming images","Check one or more image cards to review first."); return
        self._clear_editor_prefetch()
        self._batch_conflict_context={}
        self._editor_queue=paths; self._editor_queue_index=0; target=paths[0]
        if self.current_path==target and self.current_report is not None:
            self.open_data_editor(); return
        self._reopen_editor_after_inspection=True
        self.select_image(str(target))

    def _review_preparation_paths(self,paths):
        current_by_key={
            str(Path(value).absolute()).casefold():Path(value)
            for value in tuple(self.images)
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
                "still in the current Ingest inventory. Recheck the folder before retrying.",
            )
            return
        self._clear_editor_prefetch()
        self._batch_conflict_context={}
        self._editor_queue=exact
        self._editor_queue_index=0
        self._preparation_review_scope=set(exact)
        target=exact[0]
        if self.current_path==target and self.current_report is not None:
            self.open_data_editor()
        else:
            self._reopen_editor_after_inspection=True
            self.select_image(str(target))

    def edit_gallery_image(self,value: str):
        self._clear_editor_prefetch()
        path=Path(value); self._batch_conflict_context={}; self._editor_queue=[path]; self._editor_queue_index=0
        if self.current_path==path and self.current_record is not None:
            self.open_data_editor(); return
        self._reopen_editor_after_inspection=True; self.select_image(str(path))

    def _continue_editor_queue(self,offset):
        direction=1 if offset>=0 else -1
        target_index=self._editor_queue_index+direction
        while 0<=target_index<len(self._editor_queue):
            target=self._editor_queue[target_index]
            self._editor_queue_index=target_index
            if target.exists():
                self._reopen_editor_after_inspection=True
                prefetched=self._take_editor_prefetch(target)
                if prefetched is not None:
                    self.select_image(str(target),prefetched=prefetched); return
                self._pending_editor_prefetch=target
                _set_ingest_footer_activity(
                    self,f"Opening {target.name} from the review queue…",
                )
                self._schedule_editor_prefetch(target)
                return
            target_index+=direction
        self._clear_editor_prefetch(); self._editor_queue=[]
        self._batch_conflict_context={}

    def _clear_editor_prefetch(self):
        self._editor_prefetch_generation+=1
        self._editor_prefetch_cache={}
        self._editor_prefetch_tasks={}
        self._pending_editor_prefetch=None

    def _schedule_editor_prefetch(self,path: Path):
        path=Path(path)
        if path in self._editor_prefetch_cache or path in self._editor_prefetch_tasks or not path.exists():
            return
        task=_EditorPrefetchTask(
            path,self.state.knowledge,self._editor_prefetch_generation,
        )
        self._editor_prefetch_tasks[path]=task
        task.signals.complete.connect(self._editor_prefetch_complete)
        task.signals.failed.connect(self._editor_prefetch_failed)
        self.pool.start(task,-1)

    def _schedule_adjacent_editor_prefetch(self):
        next_index=self._editor_queue_index+1
        if 0<=next_index<len(self._editor_queue):
            self._schedule_editor_prefetch(self._editor_queue[next_index])

    def _editor_prefetch_complete(self,value: str,payload):
        path=Path(value)
        generation,signature,record,report,preview=payload
        if generation!=self._editor_prefetch_generation:
            return
        self._editor_prefetch_tasks.pop(path,None)
        if signature!=self._editor_source_signature(path):
            return
        self._editor_prefetch_cache[path]=(signature,record,report,preview)
        if self._pending_editor_prefetch==path:
            self._pending_editor_prefetch=None
            _clear_ingest_footer_activity(self,"Opening ")
            prefetched=self._take_editor_prefetch(path)
            if prefetched is not None:
                self.select_image(str(path),prefetched=prefetched)

    def _editor_prefetch_failed(self,value: str,payload):
        path=Path(value); generation,_message=payload
        if generation!=self._editor_prefetch_generation:
            return
        self._editor_prefetch_tasks.pop(path,None)
        if self._pending_editor_prefetch==path:
            self._pending_editor_prefetch=None
            _clear_ingest_footer_activity(self,"Opening ")
            self.select_image(str(path))

    def _take_editor_prefetch(self,path: Path):
        path=Path(path); payload=self._editor_prefetch_cache.pop(path,None)
        if payload is None:
            return None
        signature,record,report,preview=payload
        if signature!=self._editor_source_signature(path):
            return None
        return record,report,preview

    def _refresh_saved_editor_path(self,path: Path):
        self._state_cache.pop(path,None)
        projection_provider=getattr(self,"_compute_state_projection",None)
        if callable(projection_provider):
            cached,workflow=projection_provider(path)
        else:
            cached=self._compute_state(path); workflow=None
        self._state_cache[path]=cached
        if workflow is not None:
            known=set(getattr(self,"_known_record_paths",()))
            trusted=set(getattr(self,"_trusted_workflow_paths",()))
            known.add(path); trusted.discard(path)
            self._known_record_paths=known
            self._trusted_workflow_paths=trusted
            workflow_index=getattr(self,"_workflow_index",None)
            if workflow_index is not None:
                context=str(
                    getattr(self,"_readiness_context_fingerprint","") or ""
                )
                if not context:
                    context=self.workflow_states.context_fingerprint()
                    self._readiness_context_fingerprint=context
                try:
                    workflow_index.record_workflow_states(
                        {path:IngestPage._workflow_cache_payload(self,workflow)},
                        context_fingerprint=context,
                        cache_scope="ingest",
                    )
                except (OSError,sqlite3.DatabaseError):
                    pass
        card=self.cards.get(str(path))
        if card:
            self._configure_gallery_card(path,card)

    def _synchronize_preparation_review_path(self,path: Path) -> bool:
        path=Path(path)
        scope=getattr(self,"_preparation_review_scope",set())
        if path not in scope:
            return False
        if self.workflow_states.has_review_findings(
            path,include_deferred=True,
        ):
            remaining=len(scope)
            self.batch_summary.setText(
                f"Prepare review • {remaining:,} image(s) still unresolved"
            )
            self.batch_summary.show()
            return False
        scope.discard(path)
        self.batch_summary.setText(
            f"Prepare review • {len(scope):,} image(s) still unresolved"
        )
        self.batch_summary.show()
        return True

    def _current_ingest_image(self,value: str | Path) -> Path | None:
        """Resolve one path only while the current Ingest inventory owns it."""
        try:
            requested=Path(value).resolve()
        except (OSError,RuntimeError,TypeError,ValueError):
            return None
        for current in tuple(self.images):
            try:
                candidate=Path(current).resolve()
            except (OSError,RuntimeError,TypeError,ValueError):
                continue
            if str(candidate).casefold()==str(requested).casefold():
                return candidate
        return None

    def source_correction_image_is_current(self,value: str | Path) -> bool:
        """Recheck contextual ownership without admitting the path to Dataset."""
        return self._current_ingest_image(value) is not None

    def _queue_source_correction_handoff(self,scope) -> bool:
        """Offer one synchronous, one-shot handoff from the active editor."""
        exact_scope=dict(scope) if isinstance(scope,dict) else {}
        image=self._current_ingest_image(exact_scope.get("image") or "")
        if image is None:
            QMessageBox.information(
                self,"Ingest source correction scope changed",
                "The exact image from the image editor is no longer in the current "
                "Ingest inventory. The scope was not broadened and nothing was "
                "changed; recheck Ingest and choose the correction again.",
            )
            return False
        exact_scope["image"]=str(image)
        context=dict(exact_scope.get("return_context") or {})
        workspace=(
            "descriptions"
            if str(context.get("workspace") or "")=="descriptions"
            or str(exact_scope.get("origin") or "").endswith(".descriptions")
            else "tag_manager"
        )
        exact_scope["origin"]="ingest.universal_editor."+workspace
        context.update({
            "image":str(image),"workspace":"ingest."+workspace,
        })
        exact_scope["return_context"]=context
        self._pending_source_correction_scope=exact_scope
        self.source_correction_requested.emit(exact_scope)
        if self._pending_source_correction_scope is exact_scope:
            # A disconnected or queued receiver cannot retain write authority.
            self._pending_source_correction_scope=None
        return True

    def consume_source_correction_handoff(self,scope) -> Path | None:
        """Consume the exact in-memory Ingest authorization once."""
        pending=self._pending_source_correction_scope
        self._pending_source_correction_scope=None
        if pending is None or not isinstance(scope,dict) or scope!=pending:
            return None
        return self._current_ingest_image(pending.get("image") or "")

    def resume_source_correction_return(self,scope) -> bool:
        """Return to a freshly inspected image editor after the Dataset modal."""
        exact_scope=dict(scope) if isinstance(scope,dict) else {}
        image=self._current_ingest_image(exact_scope.get("image") or "")
        if image is None:
            QMessageBox.information(
                self,"Ingest image is no longer available",
                "The source-correction review finished, but its exact image is "
                "no longer in the current Ingest inventory. Recheck Ingest to "
                "continue reviewing it.",
            )
            return False
        self._state_cache.pop(image,None)
        self._clear_editor_prefetch()
        context=dict(exact_scope.get("return_context") or {})
        self._reopen_editor_after_inspection=(
            str(context.get("workspace") or "") in {
                "ingest.tag_manager","ingest.descriptions",
            }
        )
        self.select_image(str(image))
        return True

    def _editor_context_token(self):
        knowledge=self.state.knowledge
        return (
            id(knowledge),getattr(knowledge,"_relationship_signature_revision",0),
            id(getattr(knowledge,"_loaded_bytes",None)),
            id(self.characters),str(getattr(self,"_readiness_context_fingerprint","") or ""),
        )

    def open_data_editor(self):
        if self.current_record is None: return
        path=self.current_record.image_path
        if not self._editor_queue or path not in self._editor_queue:
            self._start_editor_queue()
        token=self._editor_context_token()
        cached=getattr(self,"_prepared_editor_context",None)
        source=self._editor_source_signature(path)
        if getattr(self,"_current_editor_source_signature",source) not in (None,source):
            self._reopen_editor_after_inspection=True
            self.select_image(str(path)); return
        if cached is None or cached[0]!=token or self._current_editor_preview is None:
            request=(path,id(self.current_record),self._editor_queue_index,token)
            if getattr(self,"_editor_context_request",None)==request:return
            previous=getattr(self,"_editor_context_task",None)
            if previous is not None:previous.cancel()
            self._editor_context_request=request
            _set_ingest_footer_activity(self,"Preparing the image data editor…")
            knowledge=self.state.knowledge; characters=self.characters
            preview=self._current_editor_preview
            def prepare():
                context=(cached[1] if cached is not None and cached[0]==token else
                         prepare_image_data_editor_context(knowledge,characters))
                image=preview if preview is not None else QImage(str(path))
                return request,source,context,image
            task=AsyncCallTask(prepare); self._editor_context_task=task
            task.source_signature=source
            task.signals.complete.connect(self._image_editor_context_ready)
            task.signals.failed.connect(
                lambda message,owner=task:self._image_editor_context_failed(owner,message)
            )
            self.pool.start(task,1)
            return
        context_owner=[cached[1]];cached=None
        self._run_data_editor(_context_envelope=context_owner)

    def _image_editor_context_ready(self,result):
        request,signature,context,preview=result
        if request!=getattr(self,"_editor_context_request",None):return
        task=self._editor_context_task
        self._editor_context_request=None; self._editor_context_task=None
        if task is not None:
            for name in ("complete","failed"):
                try:getattr(task.signals,name).disconnect()
                except (RuntimeError,TypeError):pass
        path,record_id,queue_index,token=request
        if (
            getattr(self,"_shutdown_requested",False) or self.current_path!=path
            or id(self.current_record)!=record_id or self._editor_queue_index!=queue_index
        ):return
        if token!=self._editor_context_token():
            current_request=(path,record_id,queue_index,self._editor_context_token())
            defer_editor_delivery(self,IngestPage._deliver_ready_editor,(current_request,signature))
            return
        if signature!=self._editor_source_signature(path):
            self._reopen_editor_after_inspection=True
            self.select_image(str(path)); return
        self._prepared_editor_context=(token,context)
        self._current_editor_preview=preview
        _clear_ingest_footer_activity(self,"Preparing the image data editor")
        defer_editor_delivery(self,IngestPage._deliver_ready_editor,(request,signature))

    def _deliver_ready_editor(self,envelope):
        if not envelope:return
        request,signature=envelope;envelope.clear()
        path,record_id,queue_index,token=request
        if (getattr(self,"_shutdown_requested",False) or self.current_path!=path
                or id(self.current_record)!=record_id or self._editor_queue_index!=queue_index
                or token!=self._editor_context_token()):return
        if signature!=self._editor_source_signature(path):
            self._reopen_editor_after_inspection=True;self.select_image(str(path));return
        self.open_data_editor()

    def _image_editor_context_failed(self,task,message):
        if task is not getattr(self,"_editor_context_task",None):return
        request=self._editor_context_request
        self._editor_context_request=None; self._editor_context_task=None
        _clear_ingest_footer_activity(self,"Preparing the image data editor")
        if request is None:return
        path,record_id,queue_index,token=request
        if (getattr(self,"_shutdown_requested",False) or self.current_path!=path
            or id(self.current_record)!=record_id or self._editor_queue_index!=queue_index):return
        if token!=self._editor_context_token():
            self.open_data_editor(); return
        if task.source_signature!=self._editor_source_signature(path):
            self._reopen_editor_after_inspection=True; self.select_image(str(path)); return
        QMessageBox.warning(self,"Image data editor could not open",message)

    def _run_data_editor(self,prepared_context=None,*,_context_envelope=None):
        if _context_envelope is not None:prepared_context=_context_envelope.pop()
        path=self.current_record.image_path
        try:
            operation=capture_operation(self,(path,),operation_kind="manual_image_edit")
            # Pending history belongs in the editor's exact recovery flow;
            # unlike direct producers, opening the editor must not reject it.
            self.current_record.history_context=(operation.context_for(path) if operation else None)
        except (OSError,ValueError,RuntimeError) as exc:
            QMessageBox.warning(self,"Saved history needs attention",str(exc))
            return
        self._reopen_editor_after_inspection=False
        self._schedule_adjacent_editor_prefetch()
        dialog=ImageDataEditor(
            self.current_record,self.current_report,self.settings,self,knowledge=self.state.knowledge,
            characters=self.characters,
            queue_index=self._editor_queue_index,queue_count=len(self._editor_queue),
            batch_conflict_context=self._batch_conflict_context.get(str(path)),
            image_knowledge_only=str(path) in self._batch_conflict_context,
            preview_image=self._current_editor_preview,
            initial_tab_index=self._editor_queue_tab_index,
            content_safety=content_safety_policy(),
            source_correction_available=(
                self._current_ingest_image(path) is not None
            ),
            preparation_complete=self._stage_for(path)=="complete",
            prepared_context=prepared_context,
        )
        prepared_context=None  # Release the construction snapshot before nested edits.
        dialog.ai_requested.connect(lambda kind,current=dialog:self._editor_ai_requested(current,kind))
        if hasattr(dialog,"preparation_requested"):
            dialog.preparation_requested.connect(
                lambda options,current=dialog:
                    self._editor_preparation_requested(current,options)
            )
        if hasattr(dialog,"edit_tag_requested"):dialog.edit_tag_requested.connect(self.edit_tag_requested)
        if hasattr(dialog,"create_tag_requested"):dialog.create_tag_requested.connect(self.create_tag_requested)
        self._active_editor=dialog
        result=dialog.exec()
        self._editor_queue_tab_index=dialog.tabs.currentIndex()
        if self._active_editor is dialog: self._active_editor=None
        if result in {
            QDialog.Accepted,ImageDataEditor.SAVE_AND_NEXT,
            REQUEST_SOURCE_CORRECTION,
        } and dialog.persisted_changes:
            self._refresh_saved_editor_path(path)
        resolved_preparation_path=(
            dialog.persisted_changes
            and self._synchronize_preparation_review_path(path)
        )
        if result==QDialog.Accepted and path.exists():
            self._clear_editor_prefetch()
            self._editor_queue=[]; self._batch_conflict_context={}; self.select_image(str(path))
        elif result==ImageDataEditor.SAVE_AND_NEXT:
            if resolved_preparation_path:
                self._editor_queue.pop(self._editor_queue_index)
                self._editor_queue_index-=1
            self._continue_editor_queue(1)
        elif result==ImageDataEditor.REQUEST_SKIP_NEXT:
            self._continue_editor_queue(1)
        elif result==ImageDataEditor.REQUEST_PREVIOUS:
            if resolved_preparation_path:
                self._editor_queue.pop(self._editor_queue_index)
            self._continue_editor_queue(-1)
        elif result==REQUEST_SOURCE_CORRECTION:
            self._clear_editor_prefetch()
            if not self._queue_source_correction_handoff(
                dialog.source_correction_scope
            ):
                self._editor_queue=[]; self._batch_conflict_context={}
        else:
            self._clear_editor_prefetch()
            self._editor_queue=[]; self._batch_conflict_context={}

    def add_tag_to_selected_images(self):
        self.add_tag_to_images(self.selected_paths())

    def open_image_history(self):
        paths=tuple(self.selected_paths())
        if not paths and self.current_path is not None:
            paths=(self.current_path,)
        return IngestPage._open_image_history(self,paths)

    def _open_image_history(self,images,operation_id=None):
        paths=tuple(dict.fromkeys(Path(path) for path in images))
        if not paths:
            QMessageBox.information(self,"Select incoming images","Select one or more images to review their saved Image Knowledge history.")
            return
        if any(getattr(self,name,None) is not None for name in (
            "_batch_task","_scan_task","_active_editor",
        )):
            show_operation_in_progress(self,"An Ingest operation or image editor is still open")
            return
        try:
            return open_history(
                self,paths,operation_target=operation_id or None,
                on_complete=lambda completed:IngestPage._history_restored(self,completed),
                recovery_handler=lambda path:IngestPage._recover_history_image(self,path),
            )
        except (OSError,ValueError,RuntimeError) as exc:
            QMessageBox.warning(self,"History could not open",str(exc))

    def _recover_history_image(self,path):
        if any(getattr(self,name,None) is not None for name in (
            "_batch_task","_scan_task","_active_editor",
        )):
            show_operation_in_progress(self,"An Ingest operation or image editor is still open")
            return
        target=self._current_ingest_image(path)
        if target is None:
            QMessageBox.information(self,"Image is no longer in Ingest","Reopen this exact image from its current library to review saved-data recovery.")
            return
        self._editor_queue=[target]; self._editor_queue_index=0
        if self.current_path==target and self.current_report is not None:
            self.open_data_editor()
        else:
            self._reopen_editor_after_inspection=True
            self.select_image(str(target))

    def _history_restored(self,images):
        paths=tuple(dict.fromkeys(Path(path) for path in images))
        for path in paths:
            self._state_cache.pop(path,None)
            getattr(self,"_editor_prefetch_cache",{}).pop(path,None)
            if path in self.images:
                self._refresh_saved_editor_path(path)
        if paths:
            self.library_paths_changed.emit(paths)
        # Updating cards and the inspector does not replace gallery selection.
        if self.current_path in paths and self.current_path in self.images:
            self.select_image(str(self.current_path))

    def prepare_preferred_descriptions(self):
        paths=tuple(dict.fromkeys(self.selected_paths()))
        if not paths:
            QMessageBox.information(
                self,"Select incoming images","Select one or more available images first."
            )
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
            history_operation_factory=lambda images:capture_operation(
                self,images,operation_kind="preferred_description_preparation"),
            history_opener=lambda images,operation_id:IngestPage._open_image_history(self,images,operation_id),
        )
        dialog.exec()
        changed=dialog.changed_paths()
        if changed:
            for path in changed:
                self._state_cache.pop(path,None)
            self.render_page()
            if self.current_path in changed:
                self.select_image(str(self.current_path))

    def add_tag_to_images(self,paths):
        paths=tuple(dict.fromkeys(Path(value) for value in paths))
        if not paths:
            QMessageBox.information(
                self,"Select incoming images","Select one or more available images first."
            )
            return
        if not getattr(self,"_conflicts_ready",True):
            QMessageBox.information(
                self,"Conflict checks are still preparing",
                getattr(self,"_conflict_error","") or
                "Wait for the current System Knowledge conflict check to finish, then try again.",
            )
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
            history_operation_factory=lambda images:capture_operation(
                self,images,operation_kind="selected_image_tag_batch"),
            history_opener=lambda images,operation_id:IngestPage._open_image_history(self,images,operation_id),
        )
        dialog.exec()
        changed=dialog.changed_paths()
        if changed:
            for path in changed:
                self._state_cache.pop(path,None)
            self.render_page()
            if self.current_path in changed:
                self.select_image(str(self.current_path))
        if dialog.review_requested:
            context=dialog.conflict_review_context()
            review_paths=[Path(value) for value in context]
            if review_paths:
                self._batch_conflict_context=context
                self._editor_queue=review_paths
                self._editor_queue_index=0
                target=review_paths[0]
                if self.current_path==target and self.current_report is not None:
                    self.open_data_editor()
                else:
                    self._reopen_editor_after_inspection=True
                    self.select_image(str(target))

    def _editor_ai_requested(self,editor,kind):
        if editor is not self._active_editor: return
        (self.run_wd if kind=="tags" else self.run_joy)(editor)

    def _editor_preparation_requested(self,editor,options):
        if editor is not self._active_editor:
            editor.ai_failed()
            return
        self.batch_prepare(
            options=options,paths=(editor.record.image_path,),editor=editor,
        )

    def _row(self,value,kind,source,status,checked,editable):
        item=QTreeWidgetItem(("",value,kind,source,status)); item.setCheckState(0,Qt.Checked if checked else Qt.Unchecked)
        if not editable: item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
        self.found.addTopLevelItem(item); self.rows.append((item,kind,value))

    def add_manual_tag(self):
        if self.current_record is None: return
        tag,accepted=QInputDialog.getText(self,"Add SDXL Tag","SDXL Tag to add:")
        if accepted and tag.strip():
            self._row(tag.strip(),"Manual SDXL Tag","You","New",True,True)
            self.mark_pending_save("The new SDXL Tag is checked below. Save image data when you are ready.")

    def add_manual_description(self):
        if self.current_record is None: return
        text,accepted=QInputDialog.getMultiLineText(self,"Add Plain Language Description","Plain Language Description of the visible image:")
        if accepted and text.strip():
            self._row(text.strip(),"Plain Language Description","You","New",True,True)
            self.mark_pending_save("The Plain Language Description is checked below. Save image data when you are ready.")

    def selected_information(self):
        item=self.found.currentItem()
        if not item: QMessageBox.information(self,"Select information","Select an SDXL Tag or Plain Language Description in the table first.")
        return item

    def replace_selected_information(self):
        item=self.selected_information()
        if not item: return
        kind=item.text(2); old=item.text(1)
        if "description" in kind.casefold(): value,accepted=QInputDialog.getMultiLineText(self,"Replace Plain Language Description","Confirmed Plain Language Description:",old)
        else: value,accepted=QInputDialog.getText(self,"Replace SDXL Tag","Confirmed SDXL Tag:",text=old)
        if not accepted or not value.strip(): return
        value=value.strip(); item.setText(1,value); item.setCheckState(0,Qt.Checked); item.setText(4,"Changed — save required")
        self.rows=[(row_item,row_kind,value if row_item is item else row_value) for row_item,row_kind,row_value in self.rows]
        self.mark_pending_save("The selected information was changed. Save image data to update every destination.")

    def remove_selected_information(self):
        item=self.selected_information()
        if not item: return
        item.setCheckState(0,Qt.Unchecked); item.setText(4,"Will be removed when saved")
        self.mark_pending_save("The selected information will be removed. Save image data to apply the change.")

    def mark_pending_save(self,message):
        self._complete_inspected_image=False
        self.recommendation.setText(message); self.recommended_action="save"; self.recommended_button.setVisible(True)
        self.recommended_button.setText("Save Image Data"); self.recommended_button.setEnabled(True)

    def run_summary_action(self):
        if self.summary_action=="editor":
            self.open_data_editor()

    def run_recommended_action(self):
        if self.recommended_action in {"save","import_all","editor"}:
            self.open_data_editor()

    def _prepare_confirmed_identities(self,record) -> bool:
        automation=active_automation_engine(self.state.knowledge)
        service=ConfirmedTagIdentityService(
            self.state.knowledge,
            evidence_rejected=automation.is_evidence_rejected,
        )
        plan=service.plan(
            record.annotation.all_structured_tags(),
            stored_ids=record.annotation.tag_identities,
        )
        outcome=review_removed_identities(
            self,service,plan,source="qt_ingest_confirmed_tag",
            continuation=(
                "The restored identity will be linked to this image only after "
                "the remaining quick-save checks succeed."
            ),
            stored_ids=record.annotation.tag_identities,
            confirmation=True,
        )
        if outcome.cancelled or outcome.blocked:
            return False
        plan=outcome.plan
        if plan.blockers:
            QMessageBox.warning(
                self,
                "Confirmed Tag Identity Repair required",
                "\n\n".join(
                    entry.message for entry in plan.blockers if entry.message
                )
                + "\n\nNo System Knowledge or image data was changed. "
                "Open the Image Data Editor to review the blocked identity.",
            )
            return False
        if plan.new_entries:
            names=", ".join(
                entry.raw_value for entry in plan.new_entries[:8]
            )
            remainder=len(plan.new_entries)-8
            if remainder:
                names+=f" and {remainder:,} more"
            message=(
                f"Create {len(plan.new_entries):,} reusable uncategorized "
                "System Knowledge SDXL Tag "
                f"{'identity' if len(plan.new_entries)==1 else 'identities'} "
                f"first?\n\nNew: {names}\n\n"
                "System Knowledge saves independently now. The confirmed image "
                "association is saved only after that verified transaction "
                "succeeds."
            )
            if QMessageBox.question(
                self,
                "Create reusable SDXL Tag identity",
                message,
            )!=QMessageBox.Yes:
                return False
        try:
            resolution=service.commit(
                plan,
                source="qt_ingest_confirmed_tag",
            )
        except (ConfirmedTagIdentityError,OSError,RuntimeError) as error:
            QMessageBox.critical(
                self,
                "Could not resolve confirmed SDXL Tags",
                f"{error}\n\nNo image data was changed.",
            )
            return False
        service.bind_annotation(
            record.annotation,
            resolution,
            values=record.annotation.all_structured_tags(),
        )
        return True

    def persist_confirmed(self,record):
        try:
            operation=capture_operation(self,(record.image_path,),operation_kind="confirm_image_data")
            record.history_context=operation.context_for(record.image_path) if operation else None
            with _ingest_history_write_guard(record.history_context,record.image_path):
                pass
        except (OSError,ValueError,RuntimeError) as exc:
            QMessageBox.warning(self,"Saved history needs attention",str(exc))
            return None
        if not self._prepare_confirmed_identities(record):
            return None
        try:
            with _ingest_history_write_guard(record.history_context,record.image_path):
                record.save_sidecar(force=True,knowledge=self.state.knowledge)
        except (OSError,ValueError,RuntimeError) as exc:
            QMessageBox.warning(self,"Image data save needs attention",str(exc))
            return None
        record.export_caption(character_db=self.characters,knowledge=self.state.knowledge,force=True); destinations="the Image Knowledge record and TXT caption"
        metadata=MetadataSyncService(self.settings.exiftool_path or None)
        if metadata.is_available():
            if reconcile_embedded_with_preview(self,record,metadata,self.state.knowledge,self.characters):
                destinations+=" and image Metadata"
        self._refresh_saved_editor_path(record.image_path)
        return destinations

    def move_active_to_folder(self,paths=None):
        if IngestPage._file_work_blocked(self):return
        paths=list(paths) if paths is not None and not isinstance(paths,bool) else self.selected_paths()
        if not paths:
            QMessageBox.information(self,"Select incoming images","Select one or more image cards first."); return
        missing=[path for path in paths if not path.with_suffix(".tags.json").is_file()]
        if missing:
            QMessageBox.information(
                self,"Review the images first",
                f"Save reviewed image data for {len(missing):,} selected image(s) before moving this selection.",
            ); return
        working=Path(self.settings.working_directory)
        if not working.is_dir():
            QMessageBox.information(self,"Configure the default library","Choose a valid default library in Setup before selecting one of its folders."); return
        selected=QFileDialog.getExistingDirectory(self,"Choose a library folder",str(working))
        if not selected: return
        destination=Path(selected).resolve()
        try: destination.relative_to(working.resolve())
        except ValueError:
            QMessageBox.warning(self,"Choose a library folder","The destination must be inside the default library. Use Move to Library to choose another named collection."); return
        if QMessageBox.question(
            self,"Move selected images to folder",
            f"Move {len(paths):,} selected image group(s) and their related files to:\n\n{destination}",
        )!=QMessageBox.Yes: return
        group_id="ingest-move-"+uuid4().hex
        def operation(items,progress,should_cancel=lambda:False):
            processed=skipped=0; errors=[]; moved=[]; move_destinations=[]; cancelled=False
            records=[];recovery_id="";recovery_required=False
            for index,path in enumerate(items,1):
                if should_cancel():
                    cancelled=True
                    break
                try:
                    if path.parent.resolve()==destination:
                        skipped+=1
                    else:
                        move_record=self.state.moves.move(
                            path,destination,
                            reason="move selected incoming image to chosen library folder",
                            group_id=group_id,should_cancel=should_cancel,
                        )
                        records.append(move_record)
                        move_destinations.append(move_record.files[0]["to"])
                        moved.append(str(path)); processed+=1
                except InterruptedError:cancelled=True;break
                except TransferRecoveryRequired as exc:
                    recovery_required=True;recovery_id=getattr(exc,"operation_id","")
                    errors.append(f"{path.name}: {exc}");break
                except Exception as exc:
                    errors.append(f"{path.name}: {exc}")
                progress(index,len(items),path.name)
            return {
                **_organization_result(group_id,records,recovery_operation_id=recovery_id),
                "processed":processed,"skipped":skipped,"errors":errors,
                "moved":moved,"move_destinations":move_destinations,
                "cancelled":cancelled,"recovery_required":recovery_required,
                "summary_lines":[
                    ("Paused safely after the current image. " if cancelled else "")
                    +f"Moved {processed:,} image(s) to the selected folder; {skipped:,} already there."
                ],
            }
        self._run_batch(operation,"Moving selected images to the chosen folder…",paths)

    def _run_batch(
        self,operation,label,paths=None,*,resource="io",completion_context="",
        progress_unit="",progress_scope="",feedback_parent=None,
        pause_after_current=False,
    ):
        def explain_editor(message: str) -> None:
            unavailable=getattr(feedback_parent,"preparation_unavailable",None)
            if callable(unavailable):
                unavailable(message)

        if history_dialogs_busy(self):
            show_operation_in_progress(self,"A History job is still active")
            explain_editor("Wait for the exact History operation to finish or cancel safely before starting more Ingest work.")
            return False

        if self._batch_task is not None:
            explain_editor(
                "Preparation did not start because another Ingest action is "
                "still running. Wait for it to finish or cancel it, then try again."
            )
            return False
        paths=list(paths) if paths is not None else self.selected_paths()
        if not paths:
            QMessageBox.information(feedback_parent or self,"Select incoming images","Select one or more image cards first."); return False
        started,active=self.state.begin_long_operation("ingest",label,resource=resource)
        if not started:
            if feedback_parent is None:
                show_operation_in_progress(self,active)
            explain_editor(
                "Preparation did not start because CollectiGen is already working: "
                f"{str(active or 'another background task').strip().rstrip('.')}. "
                "Wait for it to finish or cancel it, then try again."
            )
            return False
        self._state_scan_generation+=1
        self.batch_actions.setEnabled(False); self.refresh_button.setEnabled(False); self.organize_button.setEnabled(False)
        if button:=getattr(self,"view_completed_in_dataset",None):button.setEnabled(False)
        if button:=getattr(self,"add_images_button",None): button.setEnabled(False)
        if button:=getattr(self,"review_saved_add_button",None): button.setEnabled(False)
        if button:=getattr(self,"import_external_evidence_button",None): button.setEnabled(False)
        if controls:=getattr(self,"preparation_controls",None):
            controls.set_busy(True)
        _set_ingest_footer_activity(self,label)
        set_progress_bar(self.batch_progress,detail=label,unknown_text="Starting…"); self.batch_progress.show()
        self._batch_progress_unit=str(progress_unit or "")
        self._batch_progress_scope=str(progress_scope or "")
        self._batch_completion_context=str(completion_context or "")
        self._batch_pause_after_current=bool(pause_after_current)
        self._batch_paths=list(paths)
        task=_BatchTask(operation,paths); self._batch_task=task
        task.signals.progress.connect(self._batch_progress); task.signals.complete.connect(self._batch_complete); task.signals.failed.connect(self._batch_failed)
        self.cancel_batch_button.setText("Pause After Current Image" if pause_after_current else "Cancel Safely")
        self.cancel_batch_button.setEnabled(True); self.cancel_batch_button.show(); self.pool.start(task,-1)
        return True

    def cancel_batch(self):
        if self._batch_task is None: return
        self.state.request_long_operation_cancel("ingest")
        self._batch_task.cancel()
        self.cancel_batch_button.setText("Pausing after current image…" if self._batch_pause_after_current else "Cancelling…")
        self.cancel_batch_button.setEnabled(False)

    def has_active_background_work(self) -> bool:
        if history_dialogs_busy(self):return True
        media=getattr(self,"_media_library_dialog",None)
        if media is not None and media.has_active_background_work():return True
        return (
            bool(getattr(self,"_video_ingest_dialog",None)
                 and self._video_ingest_dialog.has_active_background_work())
            or
            getattr(getattr(self,"_preparation_impact_dialog",None),"_task",None) is not None
            or getattr(getattr(self,"_duplicate_preflight_dialog",None),"_task",None) is not None
            or
            getattr(self,"_batch_task",None) is not None
            or getattr(self,"_conflict_task",None) is not None
            or getattr(self,"_state_scan_task",None) is not None
            or getattr(getattr(self,"resolution_filter",None),"_task",None) is not None
            or getattr(self,"_automatic_resize_task",None) is not None
        )

    def request_background_shutdown(self) -> bool:
        """Stop new Ingest work and cooperatively drain active AI/file work."""
        self._shutdown_requested=True
        requested=close_history_dialogs(self)
        queue=getattr(self,"_preparation_request_queue",None)
        if queue is not None:
            queue.cancel_all()
            requested=True
        media=getattr(self,"_media_library_dialog",None)
        if media is not None:
            requested=media.request_background_shutdown() or requested
        video=getattr(self,"_video_ingest_dialog",None)
        if video is not None:
            requested=video.request_background_shutdown() or requested
        resolution_filter=getattr(self,"resolution_filter",None)
        if resolution_filter is not None:
            resolution_filter.shutdown()
        automatic_resize=getattr(self,"_automatic_resize_task",None)
        if automatic_resize is not None:
            requested=True
            automatic_resize.cancel()
            self._automatic_resize_task=None
        self._pending_image_inspection=None
        self._editor_context_request=None
        for attribute in ("_image_inspection_task","_editor_context_task"):
            task=getattr(self,attribute,None)
            if task is not None:task.cancel()
        impact=getattr(self,"_preparation_impact_dialog",None)
        if impact is not None:
            requested=impact._task is not None
            impact.reject()
        preflight=getattr(self,"_duplicate_preflight_dialog",None)
        if preflight is not None:
            requested=preflight._task is not None or requested
            preflight.reject()
        if self._batch_task is not None:
            requested=True
            self.state.request_long_operation_cancel("ingest")
            self._batch_task.cancel()
        conflict_task=getattr(self,"_conflict_task",None)
        if conflict_task is not None:
            requested=True
            conflict_task.cancel()
            self._conflict_task=None
        state_scan=getattr(self,"_state_scan_task",None)
        if state_scan is not None:
            requested=True
            self._state_scan_restart_requested=False
            self._state_scan_restart_paths=set()
            self._state_scan_missing_paths=set()
            self.state.jobs.cancel(state_scan.job_id)
        notify=getattr(self,"_notify_background_work_finished",None)
        if requested and callable(notify):
            QTimer.singleShot(0,notify)
        return requested

    def _notify_background_work_finished(self):
        if not self.has_active_background_work():
            self.background_work_finished.emit()

    def _batch_progress(self,index,total,name):
        self.state.update_long_operation("ingest",index,total,name)
        _set_ingest_footer_activity(self,str(name or "Working…"))
        editor=getattr(self,"_preparation_editor",None)
        update_editor=getattr(editor,"set_preparation_progress",None)
        if callable(update_editor):
            update_editor(index,total,name)
        detail=str(name or "")
        if self._batch_progress_scope:
            explanation=(
                "Overall progress counts file checks and enabled AI model passes "
                f"across {self._batch_progress_scope}."
            )
            detail=f"{detail}\n{explanation}" if detail else explanation
        set_progress_bar(
            self.batch_progress,index,total,detail=detail,
            unit=self._batch_progress_unit,scope=self._batch_progress_scope,
        )

    def _batch_complete(self,result):
        completion_context=self._batch_completion_context
        self._batch_completion_context=""
        self._batch_pause_after_current=False
        batch_paths=list(getattr(self,"_batch_paths",()))
        self._batch_paths=[]
        self.state.finish_long_operation("ingest")
        self._batch_task=None; self.batch_actions.setEnabled(True); self.refresh_button.setEnabled(True)
        IngestPage._refresh_saved_add_batch_state(self)
        if button:=getattr(self,"add_images_button",None): button.setEnabled(True)
        if button:=getattr(self,"import_external_evidence_button",None): button.setEnabled(True)
        if controls:=getattr(self,"preparation_controls",None):
            controls.set_busy(False)
        preparation_editor=self._finish_preparation_editor()
        feedback_parent=preparation_editor or self
        self.batch_progress.hide(); self.cancel_batch_button.hide()
        moved={Path(path) for path in result.get("moved",[])}; added={Path(path) for path in result.get("added",[])}
        affected={Path(path) for path in batch_paths}|added
        for path in affected|moved:
            self._state_cache.pop(path,None)
        workflow_states=result.get("workflow_states",{})
        if isinstance(workflow_states,dict):
            self._state_cache.update(workflow_states)
            prepared={
                Path(path) for path,value in workflow_states.items()
                if isinstance(value,(tuple,list)) and len(value)>3
                and value[3]!="unprepared"
            }
            known=set(getattr(self,"_known_record_paths",()))
            trusted=set(getattr(self,"_trusted_workflow_paths",()))
            known.update(prepared)
            trusted.difference_update(affected|moved)
            trusted.update(
                Path(path) for path in result.get("automatic_prepared_paths",())
                if Path(path) in workflow_states
            )
            self._known_record_paths=known
            self._trusted_workflow_paths=trusted
        if getattr(self,"_shutdown_requested",False):
            self._notify_background_work_finished()
            return
        IngestPage._remember_organization_result(self,result)
        self._completed_library_paths=()
        if completion_context=="move_to_library" and not result.get("recovery_required"):
            recorded={destination for source,destination in getattr(self,"_organization_members",{}).values()}
            self._completed_library_paths=tuple(sorted({
                Path(path) for path in result.get("move_destinations",())
                if Path(path) in recorded
            },key=str))
        IngestPage._set_move_undo_available(self)
        if moved:
            self.images=[path for path in self.images if path not in moved]; self.selected.difference_update(moved)
            self._recent_ingest_paths=tuple(
                path for path in getattr(self,"_recent_ingest_paths",()) if path not in moved
            )
            if result.get("move_destinations") and not result.get("recovery_required"):
                self.library_paths_changed.emit(tuple(sorted(
                    moved|{Path(path) for path in result["move_destinations"]},key=str,
                )))
            else:
                self.library_changed.emit()
            if self.current_path in moved:
                self.current_path=None; self.current_record=None; self.review_panel.hide(); self.preview.clear(); self.preview.setText(result.get("moved_message","Images moved"))
        if added:
            self.images=sorted(
                {path for path in self.images if path.exists()}|{
                    path for path in added if path.exists()
                },
                key=lambda path:str(path).casefold(),
            )
            self.library_changed.emit()
            self._recent_ingest_paths=tuple(sorted(added,key=lambda path:str(path).casefold()))
            if button:=getattr(self,"review_added_button",None):
                button.setText(f"Review Added Images ({len(added):,})…")
        self.render_page()
        IngestPage._sync_review_added_button(self)
        missing_states=[
            path for path in affected
            if path in self.images and path not in self._state_cache
        ]
        if missing_states and not result.get("skip_followup_state_scan"):
            self._start_state_scan(missing_states)
        inspect_path=self.current_path if self.current_path and self.current_path.exists() else next(iter(self.selected_paths()),None)
        if inspect_path: self.select_image(str(inspect_path))
        processed=int(result.get("processed",0)); skipped=int(result.get("skipped",0)); errors=list(result.get("errors",[]))
        summary_lines=list(result.get("summary_lines",[]))
        runtime_recovery=next((
            recovery for error in errors
            if (recovery:=ai_tag_runtime_recovery(error)) is not None
        ),None) if completion_context=="preparation" else None
        if completion_context=="preparation" and runtime_recovery is None:
            self.preparation_controls.discard_setup_return()
        message="\n".join(summary_lines) if summary_lines else f"Completed {processed:,} image(s)."
        if skipped and not summary_lines: message+=f"\nSkipped {skipped:,} image(s)."
        self.batch_summary.setText(
            "AI Tags could not start; repair the shared runtime in Setup"
            if runtime_recovery else message.split("\n",1)[0]
        ); self.batch_summary.setToolTip(message)
        self.batch_summary.show()
        evidence_result=result.get("external_evidence_result")
        if isinstance(evidence_result,dict):
            external_evidence_result_dialog(
                feedback_parent,**evidence_result,
            ).exec()
        if result.get("history_operation_id"):
            self._unified_preparation_history_id=str(result["history_operation_id"])
            self._unified_preparation_paths=list(batch_paths)
            self._unified_preparation_operation_id=""
            if controls:=getattr(self,"preparation_controls",None):
                controls.set_undo_available(True)
        elif (result.get("preparation_operation_id")
              and int(result.get("preparation_changes_recorded",0))>0):
            self._unified_preparation_history_id=""
            self._unified_preparation_paths=list(batch_paths)
            self._unified_preparation_operation_id=str(
                result["preparation_operation_id"]
            )
            if controls:=getattr(self,"preparation_controls",None):
                controls.set_undo_available(True)
        attention_report=result.get("attention_report")
        if isinstance(attention_report,IngestAttentionReport):
            if result.get("preserve_existing_attention"):
                attention_report=self._attention_report.merged(attention_report)
            self._set_attention_report(
                attention_report,completion_context=="startup_managed",
            )
        editor_memory_held=(IngestPage._show_editor_memory_hold(self,preparation_editor,errors,result=result)
                            if preparation_editor is not None and not result.get("cancelled") else False)
        if errors:
            if not runtime_recovery and not editor_memory_held:
                grouped_errors=_group_batch_errors(errors)
                shown=grouped_errors[:8]
                hidden=len(grouped_errors)-len(shown)
                detail=f"{len(errors):,} image(s) could not be completed.\n\n"+"\n".join(shown)
                if hidden:
                    detail+=f"\n\n{hidden:,} more distinct problem(s) are not shown."
                QMessageBox.warning(
                    feedback_parent,
                    "Add Images recovery needs attention"
                    if completion_context in {"add_images_apply","add_images_saved_apply"}
                    and result.get("recovery_required") else
                    "Move recovery needs attention"
                    if result.get("recovery_required") else
                    "Some images need attention",
                    detail,
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

    def _batch_failed(self,message):
        completion_context=self._batch_completion_context
        if completion_context=="saved_add_preview":
            self._saved_add_preview_failed=True
        self._batch_pause_after_current=False
        preview_cancelled=(completion_context=="add_images_preview"
            and isinstance(self._batch_task,QRunnable) and self._batch_task.is_cancelled())
        self._batch_completion_context=""
        preview_retry=(completion_context=="add_images_preview"
            and not getattr(self,"_shutdown_requested",False)
            and IngestPage._restore_add_images_preview_request(self))
        if completion_context=="add_images_preview" and not preview_retry:
            self._active_add_images_preview_request=None
            self._active_add_images_ordinary=False
        self._batch_paths=[]
        self.state.finish_long_operation("ingest")
        self._batch_task=None; self.batch_actions.setEnabled(True); self.refresh_button.setEnabled(True)
        IngestPage._refresh_saved_add_batch_state(self)
        if button:=getattr(self,"add_images_button",None): button.setEnabled(True)
        if button:=getattr(self,"import_external_evidence_button",None): button.setEnabled(True)
        if controls:=getattr(self,"preparation_controls",None):
            controls.set_busy(False)
        preparation_editor=self._finish_preparation_editor()
        self.batch_progress.hide(); self.cancel_batch_button.hide()
        if isinstance(self,IngestPage):
            IngestPage._set_move_undo_available(self)
        runtime_recovery=(
            ai_tag_runtime_recovery(message)
            if completion_context=="preparation" else None
        )
        if completion_context=="preparation" and runtime_recovery is None:
            self.preparation_controls.discard_setup_return()
        self.batch_summary.setText(
            "AI Tags could not start. Repair the shared AI Tag runtime in Setup, "
            "then retry the selected images."
            if runtime_recovery else
            "Add Images preview cancelled safely. Selection retained; choose Retry Add Images Preview for a fresh check."
            if preview_cancelled and preview_retry else
            "Add Images preview stopped. Selection retained; choose Retry Add Images Preview."
            if preview_retry else "Batch stopped before completion"
        )
        self.batch_summary.setToolTip("" if preview_cancelled else message)
        if preview_retry:self.batch_summary.hide()
        else:self.batch_summary.show()
        if getattr(self,"_shutdown_requested",False):
            self._notify_background_work_finished()
            return
        if preview_cancelled:
            self.update_selection()
            self._notify_background_work_finished()
            return
        self.update_selection()
        if completion_context=="startup_managed":
            report=IngestAttentionReport(
                Path(self.settings.ingest_directory),
                (IngestAttentionItem(
                    None,"initialization_failed",str(message),
                    "Restore access or wait for other file activity to finish, then choose Scan Again.",
                ),),
            )
            self._set_attention_report(report,True)
        else:
            if runtime_recovery:
                open_setup=getattr(self.window(),"open_preparation_ai_tag_runtime_setup",None)
                show_ai_tag_runtime_recovery(
                    preparation_editor or self,runtime_recovery,
                    (lambda:open_setup(self)) if preparation_editor is None
                    and callable(open_setup) else None,
                )
            elif preparation_editor is not None and IngestPage._show_editor_memory_hold(self,preparation_editor,[message]):
                pass
            else:
                QMessageBox.critical(
                    preparation_editor or self,
                    "Add Images preview could not finish" if preview_retry else
                    "Saved Add Images review could not finish"
                    if completion_context=="saved_add_preview" else "Batch could not finish",
                    str(message)+"\n\nSaved selection and completed files: unchanged.\n"
                    "Accidental change: Restore the recorded source or destination, then choose Review Saved Add again.\n"
                    "Intentional change: Open Review Saved Add, discard all unstarted families, then choose Add Images for a new selection.\n"
                    "Recovery needed: Use the exact recovery action; never retry a held attempt."
                    if completion_context=="saved_add_preview" else
                    str(message)+"\n\nYour selection is retained. Choose Retry Add Images Preview for a fresh check; no images were imported."
                    if preview_retry else message,
                    summary="Saved Add review stopped. Choose Review Saved Add."
                    if completion_context=="saved_add_preview" else None,
                )
        self._notify_background_work_finished()

    def _show_editor_memory_hold(self,editor,errors,*,result=None):
        from .ai_runtime_controls import show_editor_preparation_memory_hold
        return show_editor_preparation_memory_hold(self,editor,errors,
            lambda options,paths,fingerprints:self.batch_prepare(
                options=options,paths=paths,editor=editor,fingerprint_retry=fingerprints,
            ),result=result,
        )

    def _finish_preparation_editor(self):
        editor=self._preparation_editor
        self._preparation_editor=None
        if editor is None:
            return None
        if editor is self._active_editor and editor.isVisible():
            editor.refresh_ai_evidence(ImageRecord(editor.record.image_path))
            return editor
        else:
            editor.ai_failed()
        return None

    def batch_prepare(self,*,options=None,paths=None,editor=None,fingerprint_retry=None):
        if paths is None and editor is None:
            paths=self.preparation_controls.take_submitted_paths()
        options=options or getattr(self.preparation_controls,"take_submitted_options",self.preparation_controls.options)()
        feedback_parent=editor or self
        explicit_paths=paths is not None
        paths=tuple(dict.fromkeys(Path(value) for value in (
            paths if explicit_paths else self.preparation_paths()
        )))

        def fail_editor():
            if editor is not None:
                editor.ai_failed()

        queue=getattr(self,"_preparation_request_queue",None)
        if (getattr(self,"_shutdown_requested",False) or history_dialogs_busy(self)
                or (getattr(self,"_batch_task",None) is not None and self._batch_task is not queue)):
            if editor is not None:
                editor.preparation_unavailable("Preparation did not start because another Ingest action is still running or shutting down. Your image draft is retained.")
            return
        if not paths:
            if not explicit_paths and IngestPage.current_stage(self)=="complete":
                QMessageBox.information(
                    feedback_parent,"Choose images to refresh",
                    "Select one or more complete images before re-running preparation.",
                )
            else:
                QMessageBox.information(
                    feedback_parent,"No incoming images",
                    "There are no available incoming images to prepare.",
                )
            fail_editor()
            return
        fingerprint_controls=(getattr(editor,"preparation_controls",None)
                              if editor is not None else self.preparation_controls)
        take_fingerprints=getattr(fingerprint_controls,"take_submitted_fingerprints",None)
        fingerprint_choice=take_fingerprints() if callable(take_fingerprints) else None
        create_fingerprints=(
            bool(fingerprint_retry) if fingerprint_retry is not None else (
                bool(getattr(self.settings,"ingest_create_duplicate_fingerprints",True))
                if fingerprint_choice is None else bool(fingerprint_choice)
            )
        )
        ingest_root=None
        if create_fingerprints:
            configured_root=(
                getattr(self.settings,"ingest_directory","")
                or getattr(self,"root","")
            )
            ingest_root=(
                Path(configured_root)
                if configured_root else
                Path(os.path.commonpath([
                    str(path.expanduser().resolve().parent) for path in paths
                ]))
            )
        has_preparation_steps=any((options.read_external_evidence,options.suggest_sdxl_tags,
                    options.suggest_plain_language,
                    options.has_automated_application))
        if not has_preparation_steps and not create_fingerprints:
            QMessageBox.information(feedback_parent,"Choose preparation steps","Choose at least one preparation option first.")
            fail_editor()
            return
        if editor is not None and hasattr(editor,"memory_hold"):
            editor.memory_hold.clear()
            import copy
            editor._memory_preparation_request=(options,paths,create_fingerprints,copy.deepcopy(vars(self.settings)))
        cancellation={"check":lambda:False}
        try:
            history=(capture_operation(
                self,paths,operation_kind="unified_image_preparation",
                cancelled=lambda:cancellation["check"](),
            ) if has_preparation_steps else None)
        except (OSError,ValueError,RuntimeError) as exc:
            QMessageBox.warning(feedback_parent,"Saved history needs attention",str(exc))
            fail_editor()
            return
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
            return self._queue_preparation_request(options,paths,editor,history,create_fingerprints,ingest_root)
        backends=configured_wd_backends(self.settings) if options.suggest_sdxl_tags else []
        joy_backend=(configured_plain_language_backend(self.settings) if (
            options.suggest_plain_language
            or options.prepares_preferred_descriptions
        ) else None)
        preferred=None
        journal=OperationJournal(USER_DATA_DIR/"operation_journal.jsonl")
        if options.prepares_preferred_descriptions:
            def identity(_record):
                return joy_backend.evidence_identity()

            preferred=PreferredCaptionPreparationService(
                journal,identity,
                ignored_plain_language=self.settings.ignored_plain_language_sentences,
            )
        service=UnifiedPreparationService(
            self.state.knowledge,self.state.ai,
            preferred_caption_service=preferred,
            journal=journal,
        )
        def operation(batch_paths,progress,should_cancel=lambda:False):
            cancellation["check"]=should_cancel
            fingerprint_lines=[]; fingerprint_errors=[]
            if create_fingerprints:
                from core.duplicate_preflight import warm_duplicate_fingerprints
                warmup=warm_duplicate_fingerprints(
                    self.state.duplicates,batch_paths,ingest_root,
                    progress=progress,should_cancel=should_cancel,
                )
                if warmup.cancelled or should_cancel():
                    return {
                        "processed":0,"cancelled":True,"skip_followup_state_scan":True,
                        "summary_lines":[
                            "Fingerprint creation paused safely. Completed cache entries "
                            "remain reusable; no preparation changes started."
                        ],
                    }
                fingerprint_lines.append(
                    f"Duplicate fingerprints: {warmup.indexed:,} new or refreshed, "
                    f"{warmup.reused:,} valid entries reused, {warmup.issue_count:,} held."
                )
                fingerprint_errors=[f"{issue.path.name}: {issue.detail}" for issue in warmup.issues]
            if has_preparation_steps:
                result=service.run(
                    batch_paths,options,tag_backends=backends,
                    description_backend=joy_backend,
                    minimum_tag_agreement=self.settings.wd_consensus_minimum,
                    progress=progress,should_cancel=should_cancel,
                    **({"history_operation":history} if history is not None else {}),
                ).as_page_result()
            else:
                result={"processed":len(batch_paths),"summary_lines":[],"errors":[],
                        "skip_followup_state_scan":True}
            result["summary_lines"]=fingerprint_lines+list(result.get("summary_lines",()))
            result["errors"]=fingerprint_errors+list(result.get("errors",()))
            return result

        selected_count=len(self.selected_paths())
        scope=(
            "the active image" if editor is not None else
            f"{len(paths):,} selected image(s)" if explicit_paths else
            f"{selected_count:,} selected image(s)" if selected_count else
            f"all {len(paths):,} incoming image(s)"
        )
        self._preparation_editor=editor
        started=self._run_batch(
            operation,f"Preparing {scope}…",paths,
            completion_context="preparation",
            resource="gpu" if (
                options.suggest_sdxl_tags or options.suggest_plain_language
            ) else "io",
            progress_unit="work steps",progress_scope=scope,
            feedback_parent=editor,
        )
        if not started:
            self._preparation_editor=None
            fail_editor()
        elif editor is None:
            self.preparation_controls.remember_setup_return(
                options,paths,fingerprint_choice,
            )

    def _restore_preparation_request_queue(self):
        from .ingest_preparation_queue import IngestPreparationQueue
        if getattr(self,"_preparation_request_queue",None) is None:
            queue=IngestPreparationQueue(self)
            self._preparation_request_queue=queue
            self.header.actions.addWidget(queue.button)
            self._sync_preparation_queue_controls()

    def _sync_preparation_queue_controls(self):
        queue=getattr(self,"_preparation_request_queue",None)
        task=getattr(self,"_batch_task",None)
        own_queue=queue is not None and task is queue
        other_write=getattr(self,"_automatic_resize_task",None) is not None or history_dialogs_busy(self)
        allowed=(task is None or own_queue) and not other_write and not getattr(self,"_shutdown_requested",False)
        self.batch_actions.setEnabled(task is None or own_queue)
        self.preparation_controls.set_busy(not allowed)
        self.update_selection()
        for name in ("refresh_button","add_images_button","import_external_evidence_button","review_saved_add_button"):
            button=getattr(self,name,None)
            if button is not None:button.setEnabled(task is None and not getattr(self,"_shutdown_requested",False))

    def _queue_preparation_request(self,options,paths,editor,history,create_fingerprints,ingest_root):
        from core.ingest_preparation_request import IngestPreparationRequestWork
        from .history_workflow import history_configuration
        from .ingest_preparation_queue import IngestPreparationQueue
        from .dataset_page import _dataset_preparation_token
        state=self.state
        try:
            work=IngestPreparationRequestWork(state,self.settings,paths,options,
                history_operation=history,history_configuration=history_configuration(self),
                authority_token=lambda:_dataset_preparation_token(state),
                journal_path=USER_DATA_DIR/"operation_journal.jsonl",ingest_root=ingest_root,
                create_fingerprints=create_fingerprints,
                service_factory=UnifiedPreparationService,tagger_factory=configured_wd_backends,
                captioner_factory=configured_plain_language_backend,
                preferred_factory=PreferredCaptionPreparationService,journal_factory=OperationJournal)
            queue=getattr(self,"_preparation_request_queue",None)
            if queue is None:
                queue=IngestPreparationQueue(self);self._preparation_request_queue=queue
                self.header.actions.addWidget(queue.button)
            request_id=queue.submit(work,editor)
            return request_id
        except Exception as exc:
            if editor is not None:editor.preparation_unavailable(str(exc))
            else:QMessageBox.warning(self,"Preparation could not be queued",str(exc))
            return None

    def _run_duplicate_preflight(
        self,parent,impact_context,*,include_similar: bool,
        lower_similarity: float,upper_similarity: float,
    ) -> bool:
        """Hold preparation until this exact optional evidence check continues."""
        from .duplicate_preflight_dialog import (
            DuplicatePreflightDialog,duplicate_preflight_context,
        )

        if impact_context is None or not impact_context.is_current():
            return False
        context=duplicate_preflight_context(self,impact_context)
        dialog=DuplicatePreflightDialog(
            context,include_similar=include_similar,
            lower_similarity=lower_similarity,
            upper_similarity=upper_similarity,parent=parent,
        )
        self._duplicate_preflight_dialog=dialog
        try:
            return dialog.exec()==QDialog.Accepted
        finally:
            self._duplicate_preflight_dialog=None
            dialog.deleteLater()
            self.render_page()

    def undo_preparation(self):
        operation_id=getattr(self,"_unified_preparation_history_id","")
        paths=list(self._unified_preparation_paths)
        if not paths:
            self.preparation_controls.set_undo_available(False)
            return
        if not operation_id:
            QMessageBox.information(
                self,"Preparation history was not recorded",
                "This preparation used legacy recovery without a proven saved-history "
                "before-and-after pair. No automatic restore is available. Configure "
                "History & Versions in Setup before future saved changes.",
            )
            return
        return IngestPage._open_image_history(self,paths,operation_id)

    def choose_batch_ai(self):
        paths=self.selected_paths()
        if not paths:
            QMessageBox.information(
                self,"Select incoming images",
                "Select one or more image cards before running an AI-only action.",
            )
            return
        self._run_ai_dialog(paths,scope="selection")

    def force_ai_from_gallery(self,paths,kind: str):
        paths=list(paths)
        self._run_ai_dialog(
            paths,preselect=kind,scope="active" if len(paths)==1 else "selection",
            active_image=paths[0] if len(paths)==1 else None,
        )

    def batch_wd(self,paths=None,*,force_override: bool=False):
        if isinstance(paths,bool):
            paths=None
        self._run_ai_dialog(
            list(paths) if paths is not None else self.selected_paths(),
            preselect="tags",force=force_override,scope="selection",
        )

    def batch_joy(self,paths=None,*,force_override: bool=False):
        if isinstance(paths,bool):
            paths=None
        self._run_ai_dialog(
            list(paths) if paths is not None else self.selected_paths(),
            preselect="description",force=force_override,scope="selection",
        )

    def batch_organize(self,paths=None):
        if IngestPage._file_work_blocked(self):return
        paths=list(paths) if paths is not None and not isinstance(paths,bool) else self.selected_paths()
        if not paths: QMessageBox.information(self,"Select incoming images","Select one or more image cards first."); return
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
        if destination_dialog.exec()!=QDialog.Accepted or destination_dialog.profile is None:
            return
        destination_profile=destination_dialog.profile
        naming_policy=destination_dialog.naming_policy
        working=Path(destination_profile.root)
        metadata=MetadataSyncService(self.settings.exiftool_path or None)
        reconciliation=ImageKnowledgeReconciliationService(
            metadata,knowledge=self.state.knowledge,characters=self.characters,
        )
        self._move_planner=LibraryMovePlanningService(
            working,self.characters,self.settings.organization_profile,
            knowledge=self.state.knowledge,reconciliation=reconciliation,
            ignored_plain_language=self.settings.ignored_plain_language_sentences,
            destination_profile=destination_profile,library_registry=registry,
            transfers=self.state.moves,
            naming_policy=naming_policy,
        )
        started,active=self.state.begin_long_operation("ingest",f"Previewing destinations for {len(paths):,} image(s)")
        if not started: show_operation_in_progress(self,active); return
        self.move_preview_notice.hide()
        self.batch_actions.setEnabled(False); self.refresh_button.setEnabled(False); self.organize_button.setEnabled(False)
        if button:=getattr(self,"add_images_button",None): button.setEnabled(False)
        _set_ingest_footer_activity(self,"Checking readiness and destinations")
        set_progress_bar(self.batch_progress,0,len(paths),detail="Checking readiness and destinations"); self.batch_progress.show()
        def preview_operation(items,progress,should_cancel):
            def preview_progress(index,total,name):
                if should_cancel(): raise InterruptedError("Move preview cancelled safely")
                progress(index,total,name)
            plan=self._move_planner.preview(
                items,include_metadata=True,progress=preview_progress,
                should_cancel=should_cancel,
            )
            return plan
        task=_BatchTask(preview_operation,paths)
        self._batch_task=task; task.signals.progress.connect(self._batch_progress)
        task.signals.complete.connect(self._move_preview_complete); task.signals.failed.connect(self._move_preview_failed)
        self.cancel_batch_button.setText("Cancel Safely"); self.cancel_batch_button.setEnabled(True); self.cancel_batch_button.show(); self.pool.start(task,-1)

    def _finish_move_preview_task(self):
        self.state.finish_long_operation("ingest"); self._batch_task=None
        self.batch_actions.setEnabled(True); self.refresh_button.setEnabled(True)
        if button:=getattr(self,"add_images_button",None): button.setEnabled(True)
        self.batch_progress.hide(); self.cancel_batch_button.hide(); self.update_selection()

    def _move_preview_failed(self,message: str):
        task=self._batch_task
        cancelled=isinstance(task,QRunnable) and task.is_cancelled()
        self._finish_move_preview_task()
        if getattr(self,"_shutdown_requested",False):
            self._notify_background_work_finished(); return
        if cancelled:
            message="Move preview cancelled safely. Selection retained; choose Move to Library again for a fresh check."
            self.batch_summary.setText(message)
            self.batch_summary.setToolTip(""); self.batch_summary.show()
            self.move_preview_notice.setText(message); self.move_preview_notice.show()
        else:
            self.batch_summary.setText("Move preview could not be completed")
            self.batch_summary.setToolTip(message); self.batch_summary.show()
            self.move_preview_notice.setText("Move preview could not be completed. Selection retained; choose Move to Library again to retry.")
            self.move_preview_notice.show()
            QMessageBox.critical(self,"Could not preview Move to Library",message)
        self._notify_background_work_finished()

    def _move_preview_complete(self,plan):
        task=self._batch_task
        cancelled=isinstance(task,QRunnable) and task.is_cancelled()
        self._finish_move_preview_task()
        if getattr(self,"_shutdown_requested",False):
            self._notify_background_work_finished(); return
        if cancelled or not isinstance(plan,LibraryMovePlan):
            message="Move preview cancelled safely. Selection retained; choose Move to Library again for a fresh check."
            self.batch_summary.setText(message)
            self.move_preview_notice.setText(message); self.move_preview_notice.show()
            self.batch_summary.show(); self._notify_background_work_finished(); return
        dialog=_MovePreviewDialog(plan,self,include_projection_options=False)
        if dialog.exec()!=QDialog.Accepted or not dialog.action:
            self._notify_background_work_finished(); return
        if dialog.action=="review":
            self.selected={item.image for item in plan.pending}; self.render_page()
            if plan.pending:
                self.select_image(str(plan.pending[0].image))
            self._notify_background_work_finished()
            return
        items=(
            plan.ready if dialog.action=="ready" else
            [item for item in plan.items if not item.errors and not item.exact_duplicate]
        )
        if not items:
            QMessageBox.information(self,"No images can move","Review the listed image-data or destination problems, then preview the move again.")
            self._notify_background_work_finished(); return
        self._apply_move_plan(
            items,dialog.targets(),dialog.boundary_confirmed(),
            plan.boundary_decision,plan.destination_profile,plan,
            "",
        )
        self._notify_background_work_finished()

    def _apply_move_plan(
        self,items,targets,boundary_confirmed=True,
        boundary_decision=None,destination_profile=None,plan=None,
        external_policy_fingerprint="",
    ):
        if IngestPage._file_work_blocked(self):return
        planner=self._move_planner; item_by_path={item.image:item for item in items}
        group_id="ingest-move-"+uuid4().hex
        projection_history={}
        if targets:
            try:
                paths=tuple(item.image for item in items if item.reconciliation is not None)
                for offset in range(0,len(paths),64):
                    members=paths[offset:offset+64]
                    history=capture_operation(self,members,
                        operation_kind="ingest_premove_projection",
                        reason="Project saved image data before its reviewed library move")
                    projection_history.update((image,history) for image in members)
            except Exception as exc:
                QMessageBox.warning(self,"Saved history needs attention",str(exc))
                return
        def operation(paths,progress,should_cancel=lambda:False):
            processed=skipped=synchronized=projection_skipped=fallback_moved=0; errors=[]; moved=[]; move_destinations=[]; cancelled=False; recovery_required=False
            records=[];recovery_id=""
            protection=(
                plan.protection.subset(paths)
                if plan is not None and plan.protection is not None else None
            )
            protection_service=None; protection_session=None
            total=len(paths); progress_offset=0
            try:
                for item in items:
                    if planner is None or not planner.is_current(item):
                        raise RuntimeError(
                            f"{item.image.name}: image data or its proposed destination "
                            "changed after preview; nothing was moved."
                        )
                if protection is not None:
                    protection_service=(
                        getattr(self,"_move_protection_service",None)
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
                        total=protected_total; progress_offset=len(paths)
            except InterruptedError:
                return {
                    "processed":0,"skipped":0,"errors":[],"moved":[],
                    "summary_lines":[
                        "Cancelled safely while protecting originals; no image family moved."
                    ],
                }
            except Exception as exc:
                return {
                    "processed":0,"skipped":0,"errors":[str(exc)],"moved":[],
                    "summary_lines":[
                        "Protected organization stopped before the first image changed."
                    ],
                }
            for index,path in enumerate(paths,1):
                if should_cancel():
                    cancelled=True
                    break
                item=item_by_path[path]
                try:
                    if planner is None or not planner.is_current(item):
                        raise RuntimeError("Image data or its proposed destination changed after preview; preview again.")
                    selected_targets=[]
                    for target in targets:
                        difference=item.reconciliation.difference(target) if item.reconciliation else None
                        if difference and difference.available and not difference.error:
                            selected_targets.append(target)
                        else:
                            projection_skipped+=1
                    if selected_targets and item.reconciliation is not None:
                        history=projection_history.get(path)
                        result=planner.reconciliation.apply(
                            item.reconciliation,targets=selected_targets,
                            durable_backup=False,
                            **({"history_operation":history} if history is not None else {}),
                        )
                        if result.errors:
                            if history is not None and (getattr(result,"history_held",False)
                                    or getattr(result,"history_interrupted",False)):
                                errors.append(f"{path.name}: Projection was not completed: "+"; ".join(result.errors))
                                recovery_required=True
                                break
                            raise RuntimeError("Projection was not completed: "+"; ".join(result.errors))
                        synchronized+=bool(result.changed_targets)
                    if item.destination.resolve()==path.parent.resolve():
                        skipped+=1
                    else:
                        transfer=item.transfer
                        if selected_targets:
                            transfer=self.state.moves.preview(
                                path,item.destination,collision_policy="error",
                                naming_policy=(
                                    item.transfer.naming_policy
                                    if item.transfer is not None else planner.naming_policy
                                ),
                                should_cancel=should_cancel,
                            )
                        move_record=self.state.moves.move(
                            path,item.destination,
                            reason="add selected image to named library after reviewed destination preview",
                            preview=transfer,collision_policy="error",
                            should_cancel=should_cancel,
                            boundary_decision=boundary_decision,
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
                        records.append(move_record)
                        move_destinations.append(move_record.files[0]["to"])
                        moved.append(str(path)); processed+=1; fallback_moved+=not item.route_ready
                except InterruptedError:
                    cancelled=True
                    break
                except TransferRecoveryRequired as exc:
                    errors.append(f"{path.name}: {exc}")
                    recovery_id=getattr(exc,"operation_id","")
                    recovery_required=True
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
                        "Protected originals were created, but their completion "
                        f"record needs attention: {exc}"
                    )
            remaining=len(self.selected)-processed
            return {
                **_organization_result(group_id,records,recovery_operation_id=recovery_id),
                "processed":processed,"skipped":skipped,"errors":errors,"moved":moved,
                "move_destinations":move_destinations,
                "recovery_required":recovery_required,"cancelled":cancelled,
                "summary_lines":[
                    ("Paused safely after the current image. " if cancelled else "")+f"Moved {processed:,} image(s); {max(0,remaining):,} selected image(s) remained in Ingest.",
                    f"Synchronized confirmed data for {synchronized:,} image(s); skipped {projection_skipped:,} unavailable optional projection target(s).",
                    f"Moved {fallback_moved:,} image(s) to explicitly previewed fallback route(s)." if fallback_moved else "No fallback route was used.",
                    "Verified original copies were retained before the first change."
                    if protection_session and protection_session.recovery_manifest else
                    "No new protected original copy was created for this operation.",
                ],
            }
        self._run_batch(operation,"Synchronizing and moving previewed images…",[item.image for item in items],completion_context="move_to_library")

    def batch_problems(self,paths=None):
        if IngestPage._file_work_blocked(self):return
        paths=list(paths) if paths is not None and not isinstance(paths,bool) else self.selected_paths()
        problems=Path(self.settings.problems_directory); ingest=Path(self.settings.ingest_directory)
        if not paths: QMessageBox.information(self,"Select incoming images","Select one or more image cards first."); return
        if not problems.is_dir(): QMessageBox.information(self,"Configure Problems storage","Choose a valid Problems folder in Setup first."); return
        if QMessageBox.question(self,"Send checked images to Problems",f"Move {len(paths):,} checked image group(s) to Problems while preserving their incoming subfolders?\n\n{problems}")!=QMessageBox.Yes: return
        group_id="ingest-move-"+uuid4().hex
        def operation(items,progress,should_cancel=lambda:False):
            processed=0; errors=[]; moved=[];records=[];cancelled=False;recovery_id="";recovery_required=False
            for index,path in enumerate(items,1):
                if should_cancel():cancelled=True;break
                try:
                    record=self.state.moves.move(path,problems,relative_to=ingest,reason="send incoming image to Problems",
                        group_id=group_id,should_cancel=should_cancel)
                    records.append(record)
                    moved.append(str(path)); processed+=1
                except InterruptedError:cancelled=True;break
                except TransferRecoveryRequired as exc:
                    errors.append(f"{path.name}: {exc}");recovery_id=getattr(exc,"operation_id","");recovery_required=True;break
                except Exception as exc: errors.append(f"{path.name}: {exc}")
                progress(index,len(items),path.name)
            return {**_organization_result(group_id,records,recovery_operation_id=recovery_id),
                "processed":processed,"errors":errors,"moved":moved,"move_destinations":[record.files[0]["to"] for record in records],
                "cancelled":cancelled,"recovery_required":recovery_required,"moved_message":"Images sent to Problems"}
        self._run_batch(operation,"Sending checked images to Problems…",paths)

    def batch_recycle(self,paths=None):
        if IngestPage._file_work_blocked(self):return
        paths=list(paths) if paths is not None else self.selected_paths()
        if not paths:
            QMessageBox.information(self,"Select incoming images","Select one or more image cards first."); return
        if self._batch_task is not None:return
        try:destination=self.state.recycle.validate_root()
        except Exception as error:
            QMessageBox.information(self,"Configure Recycle Bin",str(error)); return
        ingest=_configured_ingest_folder(self.settings)
        if ingest is None or not ingest.is_dir():
            QMessageBox.information(self,"Configure Ingest","Choose an available Ingest folder in Setup first."); return
        label=f"Previewing {len(paths):,} incoming image family(s) for Recycle Bin"
        started,active=self.state.begin_long_operation("ingest",label,resource="io")
        if not started:show_operation_in_progress(self,active); return
        profile=self.settings.library_registry().profile_for_path(ingest)
        def operation(items,progress,should_cancel=lambda:False):
            plans=[]; errors=[]
            for index,path in enumerate(items,1):
                if should_cancel():raise InterruptedError("Recycle preview cancelled safely")
                try:
                    plan=self.state.recycle.preview_recycle(
                        path,source_root=ingest,
                        source_profile_id=profile.stable_id if profile is not None else "",
                        should_cancel=should_cancel,
                    )
                    if not plan.ready:
                        errors.extend(f"{path.name}: {value}" for value in plan.errors)
                    plans.append(plan)
                except InterruptedError:raise
                except Exception as error:errors.append(f"{path.name}: {error}")
                progress(index,len(items),path.name)
            return {"kind":"recycle_preview","plans":plans,"errors":errors,"destination":destination}
        self._state_scan_generation+=1
        self.batch_actions.setEnabled(False); self.refresh_button.setEnabled(False)
        self.add_images_button.setEnabled(False); self.import_external_evidence_button.setEnabled(False)
        self.organize_button.setEnabled(False)
        if controls:=getattr(self,"preparation_controls",None):controls.set_busy(True)
        _set_ingest_footer_activity(self,label)
        set_progress_bar(self.batch_progress,detail=label,unknown_text="Starting…")
        self.batch_progress.show()
        self._batch_progress_unit="image"; self._batch_progress_scope=""
        self._batch_completion_context="recycle_preview"; self._batch_paths=list(paths)
        task=_BatchTask(operation,paths); self._batch_task=task
        task.signals.progress.connect(self._batch_progress)
        task.signals.complete.connect(self._recycle_preview_complete)
        task.signals.failed.connect(self._batch_failed)
        self.cancel_batch_button.setText("Cancel Safely"); self.cancel_batch_button.setEnabled(True); self.cancel_batch_button.show()
        self.pool.start(task,-1)

    def _recycle_preview_complete(self,result):
        paths=list(self._batch_paths); self._batch_paths=[]; self._batch_completion_context=""
        self.state.finish_long_operation("ingest"); self._batch_task=None
        self.batch_actions.setEnabled(True); self.refresh_button.setEnabled(True)
        self.add_images_button.setEnabled(True); self.import_external_evidence_button.setEnabled(True)
        if controls:=getattr(self,"preparation_controls",None):controls.set_busy(False)
        self.batch_progress.hide(); self.cancel_batch_button.hide(); self.update_selection()
        if getattr(self,"_shutdown_requested",False):self._notify_background_work_finished(); return
        if result.get("kind")!="recycle_preview":
            self.batch_summary.setText("Recycle preview cancelled safely • nothing moved"); self.batch_summary.show()
            self._notify_background_work_finished(); return
        plans=list(result.get("plans",())); errors=list(result.get("errors",()))
        if errors or len(plans)!=len(paths):
            self.batch_summary.setText("Recycle preview needs attention • nothing moved"); self.batch_summary.show()
            QMessageBox.warning(
                self,"Complete selection is not ready",
                f"The exact {len(paths):,}-image Ingest scope was held; nothing moved.\n\n"+"\n".join(errors[:12]),
            )
            self._notify_background_work_finished(); return
        members=[member for plan in plans for member in plan.transfer.members]
        required=sum(plan.transfer.required_bytes for plan in plans)
        modes={plan.transfer.mode for plan in plans}
        box=QMessageBox(QMessageBox.Question,"Move Exact Ingest Scope to Recycle Bin",
            f"Move exactly {len(plans):,} selected incoming image family(s) containing {len(members):,} file(s)?\n\nDestination: {result['destination']}\nTransfer modes: {', '.join(sorted(modes))}\nAdditional space required: {required:,} bytes\n\nEvery recognized Image Knowledge, TXT, and XMP companion moves with its image and keeps a verified original path for Restore.",
            QMessageBox.NoButton,self)
        move=box.addButton("Move to Recycle Bin",QMessageBox.AcceptRole)
        cancel=box.addButton(QMessageBox.Cancel); box.setDefaultButton(cancel); box.setEscapeButton(cancel)
        box.setDetailedText("\n".join(f"{member.source}  →  {member.target}" for member in members))
        box.exec()
        if box.clickedButton() is not move:
            self.batch_summary.setText("Recycle move cancelled before writing"); self.batch_summary.show()
            self._notify_background_work_finished(); return
        by_path={plan.transfer.image:plan for plan in plans}
        def operation(items,progress,should_cancel=lambda:False):
            moved=[]; errors=[]; cancelled=False; recovery_required=False;record_ids=[];anchors=[];recovery_id=""
            for index,path in enumerate(items,1):
                if should_cancel():cancelled=True; break
                try:
                    record=self.state.recycle.apply_recycle(
                        by_path[path],reason="move selected incoming image to Recycle Bin",
                        should_cancel=should_cancel,
                    )
                    record_ids.append(record.id);anchors.extend((record.original_path,record.current_path))
                    moved.append(str(path))
                except InterruptedError:cancelled=True; break
                except TransferRecoveryRequired as error:
                    errors.append(f"{path.name}: {error}"); recovery_required=True
                    record_ids.append(by_path[path].record.id);recovery_id=getattr(error,"operation_id","");break
                except Exception as error:errors.append(f"{path.name}: {error}")
                progress(index,len(items),path.name)
            return {
                "recycle_record_ids":tuple(record_ids),"organization_paths":tuple(dict.fromkeys(anchors)),
                "recovery_operation_id":recovery_id,
                "processed":len(moved),"errors":errors,"moved":moved,
                "cancelled":cancelled,"recovery_required":recovery_required,
                "moved_message":"Images moved to Recycle Bin",
                "summary_lines":[
                    ("Paused safely after the current image. " if cancelled else "")+
                    f"Moved {len(moved):,} image family(s) to Recycle Bin; {len(errors):,} need attention."
                ],
            }
        self._run_batch(
            operation,"Moving verified incoming image families to Recycle Bin…",
            paths,completion_context="recycle_apply",progress_unit="image",
        )

    def undo_file_move(self):
        if IngestPage._file_work_blocked(self):return
        recycle_ids=getattr(self,"_recycle_record_ids",())
        if recycle_ids:
            self.open_recycle_requested.emit(tuple(recycle_ids));return
        scope=getattr(self,"_organization_scope",OrganizationHistoryScope())
        if not scope.available:
            QMessageBox.information(self,"No exact completed operation selected",
                "This action has no recorded completed batch to review. Select an image and open History & Versions for its available coverage.")
            return
        members=dict(getattr(self,"_organization_members",{}))
        def restored(outcomes):
            nonlocal scope,members
            scope,members=IngestPage._organization_restored(self,scope,members,outcomes)
        try:
            dialog=open_organization_history(self,**scope.browser_kwargs(),
                on_outcomes=restored)
            if dialog is not None and hasattr(dialog,"busyChanged"):
                dialog.busyChanged.connect(lambda _busy:IngestPage._set_move_undo_available(self))
            return dialog
        except (OSError,ValueError,RuntimeError) as exc:
            QMessageBox.warning(self,"File history could not open",str(exc))

    def _run_ai_dialog(
        self,paths,*,preselect: str="",force: bool=False,scope: str="selection",
        active_image=None,editor=None,
    ):
        if self._batch_task is not None:
            if editor is not None:
                editor.ai_failed()
            return
        paths=tuple(dict.fromkeys(Path(value) for value in paths))
        if not paths:
            if editor is not None:
                editor.ai_failed()
            return
        def retry_scope_ready(images,feedback_parent):
            if self._batch_task is not None or getattr(self,"_shutdown_requested",False):
                show_operation_in_progress(feedback_parent,"Ingest AI analysis")
                return False
            missing=[image for image in images if
                self._current_ingest_image(image) is None or not image.is_file()]
            if missing:
                QMessageBox.information(
                    feedback_parent,"Selected-image scope changed",
                    f"{len(missing):,} selected image(s) are no longer available in the current "
                    "Ingest folder. No AI run started. Refresh Ingest and review the exact images again.",
                )
                return False
            return True
        dialog=RunAIDialog(
            self.state,self.settings,paths,owner="ingest",scope=scope,
            active_image=active_image,pool=self.pool,parent=editor or self,
            preselect=preselect,force=force,
            history_operation_factory=lambda images:capture_operation(
                self,images,operation_kind="image_ai_analysis"),
            history_opener=lambda images,operation_id:IngestPage._open_image_history(self,images,operation_id),
            scope_admission=retry_scope_ready,
        )
        refreshed={"done":False}

        def refresh_changed(changed_images):
            for path in changed_images:
                self._state_cache.pop(path,None)
            self.render_page()
            refreshed["done"]=True

        dialog.images_changed.connect(refresh_changed)
        dialog.exec()
        result=getattr(dialog,"latest_retry_result",None) or dialog.result_data
        if result is not None and not refreshed["done"]:
            refresh_changed(paths)
        if result is not None:
            counts=result.outcome_counts()
            from .ai_runtime_controls import ai_result_headline
            self.batch_summary.setText(
                f"{ai_result_headline(result)} • {counts['completed']:,} completed • "
                f"{counts['partial']:,} partial • {counts['skipped']:,} current • "
                f"{counts['conflicted']:,} conflicted • {counts['failed']:,} failed"
            )
            self.batch_summary.show()
        if editor is not None and editor is self._active_editor and editor.isVisible():
            editor.refresh_ai_evidence(ImageRecord(paths[0]))
        elif self.current_path in paths:
            self.select_image(str(self.current_path))
        if (
            result is not None and dialog.review_requested and editor is None
            and result.review_paths
        ):
            review_paths=list(getattr(dialog,"review_paths_requested",()) or result.review_paths)
            self._editor_queue=review_paths
            self._editor_queue_index=0
            target=review_paths[0]
            if self.current_path==target and self.current_report is not None:
                self.open_data_editor()
            else:
                self._reopen_editor_after_inspection=True
                self.select_image(str(target))
        elif editor is not None and result is None:
            editor.ai_failed()

    def run_wd(self,editor=None):
        record=editor.record if editor is not None else self.current_record
        if record is None: return
        self._run_ai_dialog(
            [record.image_path],preselect="tags",scope="active",
            active_image=record.image_path,editor=editor,
        )

    def run_joy(self,editor=None):
        record=editor.record if editor is not None else self.current_record
        if record is None: return
        self._run_ai_dialog(
            [record.image_path],preselect="description",scope="active",
            active_image=record.image_path,editor=editor,
        )
