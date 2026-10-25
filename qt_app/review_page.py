from __future__ import annotations

from .help_content import workspace_help

import hashlib
import math
from copy import deepcopy
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path

from PySide6.QtCore import QSignalBlocker,QSize,QThreadPool,Qt,Signal,QTimer,QUrl
from PySide6.QtGui import QDesktopServices,QImage,QImageReader,QKeySequence,QPixmap,QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,QApplication,QBoxLayout,QButtonGroup,QCheckBox,QComboBox,QDialog,QFrame,QGridLayout,QGroupBox,
    QHeaderView,QHBoxLayout,QLabel,QLineEdit,QListWidget,QListWidgetItem,QMenu,QMessageBox,QProgressBar,QPushButton,QRadioButton,
    QSizePolicy,QSpacerItem,QSplitter,QStackedWidget,QTabWidget,QTextEdit,QTreeWidget,QTreeWidgetItem,QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from .accessibility import announce_accessibly

from core import (
    AppState,CategoryRegistry,CharacterCollectionDiscoveryService,
    CharacterCollectionReviewService,CharacterDatabase,DiscoveryRegistry,
    CharacterSourceDiscoveryService,CharacterSourceReviewService,
    SOURCE_ALIAS_TASK_TYPE,SOURCE_LINK_TASK_TYPE,SOURCE_TASK_TYPES,
    DownstreamUpdateStore,
    EvidenceQueryService,FoundationKnowledge,ImageQuestionService,ImageRecord,
    ImageWorkflowStateService,
    PatternRegistry,SuggestionEngine,
    ReviewExampleSet,ReviewSafeBatchService,ReviewSafeKnowledgeItem,
    ReviewTagDecisionBatchService,ReviewTagDecisionRequest,
    TAG_BROADER_PROPOSAL_TASK_TYPE,TAG_MEANING_TASK_TYPE,
    TagDatabase,
    TagBroaderReviewService,TagMeaningReviewService,
    TagAliasProposal,TagAliasRelationshipService,
    find_suggestions,
    build_review_example_set,classify_system_knowledge_change,
    normalize_tag,review_safe_task_fingerprint,
    review_tag_selection_fingerprint,
    status_from_confidence,status_from_severity,
)
from core.paths import CATEGORIES_PATH,DEFAULTS_DIR,DISCOVERED_TAGS_PATH,PATTERNS_PATH,TAG_MEANING_EVIDENCE_PATH,USER_DATA_DIR
from .components import BoundedSearchableComboBox,EmptyStatePanel,PageHeader,RangeCheckListWidget,bind_status_footer,counted_noun,set_progress_bar,show_operation_in_progress
from .gallery import (
    CompactGalleryStatusController,EmbeddedGallery,ProtectedImageView,
    content_safety_policy,
)
from .conflict_review_dialog import ConflictReviewDialog
from .history_workflow import capture_operation, open_history, history_dialogs_busy, close_history_dialogs
from .guided_review_dialog import GuidedTagReviewDialog
from .tag_alias_preflight_dialog import TagAliasPreflightDialog
from .workers import AsyncCallTask,CancelableProgressCallTask,CoordinatedCallTask,start_qt_delivery_task
from .confidence import color_tree_item


DISMISS_QUESTION_LABEL="Dismiss Question"
STATUS_CHOICES=(
    ("Needs my decision","pending"),
    ("Dismissed questions","ignored"),
    ("Completed history","resolved"),
)
KIND_CHOICES=(
    ("All questions",None),
    ("SDXL Tags and meanings",{
        "new_tag","pattern_group",TAG_MEANING_TASK_TYPE,
        TAG_BROADER_PROPOSAL_TASK_TYPE,
    }),
    ("Character collections",{"collection_member"}),
    ("Character sources and aliases",set(SOURCE_TASK_TYPES)),
    ("Image information",{"image_question"}),
    ("Legacy projection questions",{"merge_suggestion"}),
)


def _merge_location_label(value) -> str:
    location=str(value or "").strip()
    if location=="sdxl_tags":
        return "SDXL Tags"
    return location.replace("."," / ").replace("_"," ").title() if location else "Unknown"


def _available_file(value) -> bool:
    try:
        return Path(value).is_file()
    except OSError:
        return False


def review_task_in_library_scope(task,dataset) -> bool:
    current_scope=set(getattr(dataset,"scope_library_ids",()))
    details=getattr(task,"details",{}) or {}
    if task.task_type=="collection_member" or task.task_type in SOURCE_TASK_TYPES:
        source_scope={
            str(value) for value in details.get("source_scope",())
            if str(value)
        }
        if source_scope and current_scope:
            return bool(source_scope.intersection(current_scope))
    image_path=getattr(task,"image_path","")
    if not image_path:
        return True
    library_id=str(details.get("library_id") or "")
    identity_for_path=getattr(dataset,"identity_for_path",None)
    if not library_id and callable(identity_for_path):
        identity=identity_for_path(image_path)
        library_id=str(getattr(identity,"library_id","") or "")
    return not library_id or library_id in current_scope


def _review_source_token(state: AppState) -> tuple:
    """Return a cheap stale guard for the authority used by cold preparation."""

    paths=(
        state.knowledge.path,state.reviews.path,DISCOVERED_TAGS_PATH,
        CATEGORIES_PATH,PATTERNS_PATH,TAG_MEANING_EVIDENCE_PATH,
        state.knowledge.path.with_name("relationships.json"),
        USER_DATA_DIR/"series.json",USER_DATA_DIR/"characters.json",
        USER_DATA_DIR/"outfits.json",
    )
    snapshots=[]
    for value in paths:
        path=Path(value)
        try:
            stat=path.stat()
        except OSError:
            snapshots.append((str(path),False,0,0))
        else:
            snapshots.append((str(path),True,stat.st_size,stat.st_mtime_ns))
    return (id(state.knowledge),id(state.reviews),tuple(snapshots))


@dataclass
class ReviewPagePreparation:
    """Core-only Review Center state prepared outside the Qt interface thread."""

    tags: object
    tasks: object
    discovery: object
    categories: object
    patterns: object
    foundation: object
    suggestions: object
    characters: object
    collection_reviews: object
    collection_recovered: tuple
    collection_recovery_error: str
    source_reviews: object
    source_recovered: tuple
    source_recovery_error: str
    meaning_reviews: object
    meaning_recovered: tuple
    meaning_recovery_error: str
    broader_reviews: object
    broader_recovered: tuple
    broader_recovery_error: str
    downstream_updates: object
    queue_health_counts: dict
    queue_loaded_at: str
    source_token: tuple

    def is_current(self,state: AppState) -> bool:
        return self.source_token==_review_source_token(state)


def prepare_review_page(state: AppState,job=None) -> ReviewPagePreparation:
    """Load and recover Review Center core services behind visible progress."""

    def progress(current: int,message: str) -> None:
        if job is not None:
            if not job.checkpoint():
                raise InterruptedError("Review Center preparation cancelled")
            job.update(current,6,message)

    def recovered(service):
        try:
            return tuple(service.recover_interrupted()),""
        except (OSError,RuntimeError,ValueError) as exc:
            return (),str(exc)

    progress(0,"Loading Review queue")
    tags=state.knowledge; tasks=state.reviews
    discovery=DiscoveryRegistry(DISCOVERED_TAGS_PATH)
    categories=CategoryRegistry(DEFAULTS_DIR/"categories.json",CATEGORIES_PATH)
    patterns=PatternRegistry(DEFAULTS_DIR/"patterns.json",PATTERNS_PATH)
    foundation=FoundationKnowledge(
        DEFAULTS_DIR/"modifiers.json",DEFAULTS_DIR/"concepts.json",
    )
    suggestions=SuggestionEngine(foundation,patterns,tags)
    progress(1,"Loading character knowledge")
    characters=CharacterDatabase(USER_DATA_DIR)
    collection_reviews=CharacterCollectionReviewService(
        characters,tags,categories,tasks,
        snapshot_provider=lambda: _review_collection_snapshot(state),
    )
    collection_recovered,collection_recovery_error=recovered(collection_reviews)
    progress(2,"Checking Character collection recovery")
    source_reviews=CharacterSourceReviewService(
        characters,tags,categories,tasks,
        default_relationship_path=DEFAULTS_DIR/"relationships.json",
        user_relationship_path=tags.path.with_name("relationships.json"),
        snapshot_provider=lambda: _review_collection_snapshot(state),
    )
    source_recovered,source_recovery_error=recovered(source_reviews)
    progress(3,"Checking Character Source recovery")
    meaning_reviews=TagMeaningReviewService(
        tags,tasks,TAG_MEANING_EVIDENCE_PATH,
        backup_root=USER_DATA_DIR/"backups"/"tag-meaning-review",
    )
    meaning_recovered,meaning_recovery_error=recovered(meaning_reviews)
    broader_reviews=TagBroaderReviewService(
        tags,categories,tasks,suggestions,
        default_relationship_path=DEFAULTS_DIR/"relationships.json",
        user_relationship_path=tags.path.with_name("relationships.json"),
        backup_root=USER_DATA_DIR/"backups"/"relationship-tag-semantics",
    )
    broader_recovered,broader_recovery_error=recovered(broader_reviews)
    progress(4,"Checking Knowledge decision recovery")
    queue_health_counts=tasks.triage_pending()
    progress(5,"Preparing Review queue")
    source_token=_review_source_token(state)
    progress(6,"Review Center is ready")
    return ReviewPagePreparation(
        tags=tags,tasks=tasks,discovery=discovery,categories=categories,
        patterns=patterns,foundation=foundation,suggestions=suggestions,
        characters=characters,collection_reviews=collection_reviews,
        collection_recovered=collection_recovered,
        collection_recovery_error=collection_recovery_error,
        source_reviews=source_reviews,source_recovered=source_recovered,
        source_recovery_error=source_recovery_error,
        meaning_reviews=meaning_reviews,meaning_recovered=meaning_recovered,
        meaning_recovery_error=meaning_recovery_error,
        broader_reviews=broader_reviews,broader_recovered=broader_recovered,
        broader_recovery_error=broader_recovery_error,
        downstream_updates=DownstreamUpdateStore(
            USER_DATA_DIR/"downstream_updates.json",
        ),
        queue_health_counts=queue_health_counts,
        queue_loaded_at=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M"),
        source_token=source_token,
    )


def _review_collection_snapshot(state: AppState):
    index=getattr(getattr(state,"dataset",None),"index",None)
    provider=getattr(index,"evidence_snapshot_token",None)
    return provider() if callable(provider) else ("unavailable-index",)


class ReviewCenterPage(QWidget):
    data_changed=Signal()
    background_work_finished=Signal()
    downstream_updates_changed=Signal()
    open_dataset_image=Signal(str)
    edit_image_data=Signal(str); force_ai_requested=Signal(object,str)
    image_data_changed_event=Signal(object)
    merge_tags_requested=Signal(object); removed_tags_requested=Signal(object)
    open_automation=Signal(str,object)
    edit_tag_requested=Signal(str,object)
    choose_libraries_requested=Signal()

    def __init__(
        self,state: AppState,parent=None,*,
        preparation: ReviewPagePreparation | None=None,
    ):
        super().__init__(parent); self.state=state
        self.tasks=state.reviews; self.tags=state.knowledge
        if preparation is None:
            preparation=prepare_review_page(state)
        elif preparation.tags is not self.tags or preparation.tasks is not self.tasks:
            raise ValueError(
                "Prepared Review Center state no longer matches the active queue."
            )
        self.discovery=preparation.discovery
        self.categories=preparation.categories
        self.patterns=preparation.patterns
        self.foundation=preparation.foundation
        self.suggestions=preparation.suggestions
        self.characters=preparation.characters
        self.collection_reviews=preparation.collection_reviews
        self._collection_recovered=preparation.collection_recovered
        self._collection_recovery_error=preparation.collection_recovery_error
        self.source_reviews=preparation.source_reviews
        self._source_recovered=preparation.source_recovered
        self._source_recovery_error=preparation.source_recovery_error
        self.meaning_reviews=preparation.meaning_reviews
        self._meaning_recovered=preparation.meaning_recovered
        self._meaning_recovery_error=preparation.meaning_recovery_error
        self.broader_reviews=preparation.broader_reviews
        self._broader_recovered=preparation.broader_recovered
        self._broader_recovery_error=preparation.broader_recovery_error
        self.downstream_updates=preparation.downstream_updates
        self._record_recovered_source_aliases(self._source_recovered)
        self._pending_alias_downstream=None
        self._tag_save_failure_notice=""
        self._queue_status_failure=None
        self.pool=QThreadPool.globalInstance(); self._scan_task=None; self._scan_kind=""
        self._pending_image_decision=None
        self._background_jobs=getattr(self.state,"jobs",None)
        self._evidence_queries=getattr(self.state,"evidence_queries",None) or EvidenceQueryService()
        settings=getattr(self.state,"settings",None)
        self._ui_settings=settings
        workflow_states=ImageWorkflowStateService(
            self.tags,self.characters,
            ignored_plain_language=getattr(settings,"ignored_plain_language_sentences",()),
            categories=self.categories,
        )
        self.gallery_status=CompactGalleryStatusController(
            workflow_states.assess,pool=self.pool,parent=self,
        )
        self._image_data_event_subscribed=False

        self.queue_page=0; self.queue_page_size=100
        self._queue_health_counts=dict(preparation.queue_health_counts)
        self._visible_task_cache_key=None; self._visible_task_cache=()
        self._related_pattern_cache_key=None; self._related_pattern_index={}
        self._search_timer=QTimer(self); self._search_timer.setSingleShot(True); self._search_timer.setInterval(180); self._search_timer.timeout.connect(self.reset_queue_page)
        self._queue_loaded_at=preparation.queue_loaded_at
        self.current_task_id=""; self.current_category_id="uncategorized"; self.current_suggestion={}
        self._existing_names: tuple[tuple[str,str],...]=()
        self._shared_selection_ids: tuple[str,...]=()
        self._example_generation=0; self._example_key=(); self._example_task=None
        self._current_image_generation=0; self._current_image_task=None
        self._build(); self.refresh(reload=False)
        self.image_data_changed_event.connect(self._image_data_changed)
        self._image_data_event_callback=(
            lambda **payload:self.image_data_changed_event.emit(dict(payload))
        )
        subscribe=getattr(getattr(self.state,"events",None),"subscribe",None)
        self._image_data_event_subscribed=callable(subscribe)
        if self._image_data_event_subscribed:
            subscribe("image_data_changed",self._image_data_event_callback)
        recovery_error=(
            self._collection_recovery_error or self._source_recovery_error
            or self._meaning_recovery_error or self._broader_recovery_error
        )
        if recovery_error:
            self.header.set_status(
                "A previous Knowledge decision still needs recovery • "
                "no new change was attempted"
            )
            self.header.status.setToolTip(recovery_error)
        elif (
            self._collection_recovered or self._source_recovered
            or self._meaning_recovered or self._broader_recovered
        ):
            recovered_count=(
                len(self._collection_recovered)+len(self._source_recovered)
                +len(self._meaning_recovered)+len(self._broader_recovered)
            )
            self.header.set_status(
                f"Recovered {counted_noun(recovered_count,'interrupted decision')}"
            )
        QShortcut(QKeySequence("Ctrl+Return"),self,activated=self.save_current)
        QShortcut(QKeySequence("Alt+S"),self,activated=lambda:self.set_status("ignored"))

    def _image_ready_for_write(self,image: str | Path) -> bool:
        dataset=getattr(self.state,"dataset",None)
        if dataset is None or not hasattr(dataset,"manager_for_path"):
            return _available_file(image)
        if dataset.manager_for_path(image) is None:
            return _available_file(image)
        return bool(dataset.storage_availability(image).ready_for_write)

    def _collection_snapshot_token(self):
        index=getattr(getattr(self.state,"dataset",None),"index",None)
        provider=getattr(index,"evidence_snapshot_token",None)
        if callable(provider):
            return provider()
        return ("unavailable-index",)

    def _build(self):
        root=QVBoxLayout(self); root.setContentsMargins(24,20,24,20); root.setSpacing(12)
        self.header=PageHeader(
            "Review Center","Decide image facts and reusable knowledge. Review generated images in Ingest; check training captions in Training.",
            workspace_help("review"),
        ); root.addWidget(self.header)

        controls=QHBoxLayout(); self.status_filter_label=QLabel("Show"); controls.addWidget(self.status_filter_label); self.status_filter=QComboBox()
        for label,value in STATUS_CHOICES: self.status_filter.addItem(label,value)
        controls.addWidget(self.status_filter); self.kind_filter_label=QLabel("Question type"); controls.addWidget(self.kind_filter_label); self.kind_filter=QComboBox()
        for label,value in KIND_CHOICES: self.kind_filter.addItem(label,value)
        controls.addWidget(self.kind_filter); controls.addStretch(1)
        self.conflict_button=QPushButton("Conflicts && Guided Checks…")
        self.conflict_button.setAccessibleName("Conflicts & Guided Checks")
        self.conflict_button.setProperty("primary",True)
        self.conflict_button.setProperty("compact",True)
        self.conflict_button.setAccessibleDescription(
            "Build a bounded review queue from canonical diagnostic conflict "
            "rules. Opening the workspace does not scan or change any image."
        )
        self.conflict_button.clicked.connect(self.open_conflict_review)
        controls.addWidget(self.conflict_button)
        self.safe_button=QPushButton("Review Safe Tag Changes…")
        self.safe_button.setProperty("compact",True); self.safe_button.setAccessibleDescription("Preview only saved high-confidence SDXL Tag interpretations and Tag cleanup rules explicitly marked safe. Image-information proposals such as character assignments remain in the ordinary Review queue. No changes are made until the exact System Knowledge, Review outcome, image-data, and recovery effects are confirmed."); self.safe_button.clicked.connect(self.confirm_confident); controls.addWidget(self.safe_button)
        self.recheck_button=QPushButton("Recheck Questions…"); self.recheck_button.clicked.connect(self.recheck_questions)
        self.recheck_button.setToolTip(
            "Rebuild current Review questions from saved Image Knowledge and "
            "reviewed System Knowledge. This does not reread TXT or Metadata."
        )
        self.recheck_button.setAccessibleDescription(
            "Explicitly recheck current questions from saved authority. Ordinary "
            "Image Knowledge and reviewed-rule changes refresh them automatically."
        )
        self.recheck_button.setProperty("compact",True)
        controls.addWidget(self.recheck_button); root.addLayout(controls)
        self.scan_progress=QProgressBar(); self.scan_progress.hide()
        self.scan_progress_text=QLabel(); self.scan_progress_text.setObjectName("mutedLabel"); self.scan_progress_text.hide()
        self.cancel_review_button=QPushButton("Cancel Preview"); self.cancel_review_button.clicked.connect(self.cancel_review_operation); self.cancel_review_button.hide()
        bind_status_footer(
            self,self.scan_progress_text,self.scan_progress,
            self.cancel_review_button,title="Review",
        )
        self.queue_freshness=QLabel(); self.queue_freshness.setObjectName("mutedLabel"); self.queue_freshness.setWordWrap(True); root.addWidget(self.queue_freshness)
        self.status_filter.currentIndexChanged.connect(self._status_changed); self.kind_filter.currentIndexChanged.connect(self.reset_queue_page)

        splitter=QSplitter(Qt.Horizontal); root.addWidget(splitter,1)
        left=QWidget(); self.queue_column=left
        left_layout=QVBoxLayout(left); left_layout.setContentsMargins(0,0,8,0)
        queue_filters=QHBoxLayout(); self.queue_search=QLineEdit(); self.queue_search.setPlaceholderText("Filter questions…")
        self.queue_search.setMinimumWidth(170)
        self.health_filter=QComboBox(); self.health_filter.addItem("All Queue Quality","")
        self.health_filter.setAccessibleName("Queue Quality")
        self.health_filter.setToolTip("Filter by queue quality. All Queue Quality includes older noise and cleanup questions.")
        for label,value in (("Meaningful Decisions","meaningful"),("Punctuation Noise","punctuation"),("Caption Fragments","caption_fragment"),("Duplicates","duplicate"),("Invalid Candidates","invalid")): self.health_filter.addItem(label,value)
        self.health_filter.setCurrentIndex(1)
        self.queue_search.textChanged.connect(self.schedule_queue_refresh); self.health_filter.currentIndexChanged.connect(self.reset_queue_page)
        self.clear_filters=QPushButton("Clear Filters")
        self.clear_filters.setProperty("compact",True)
        self.clear_filters.setToolTip("Show all question types and queue qualities in the current status view.")
        self.clear_filters.clicked.connect(self.clear_queue_filters)
        queue_filters.addWidget(self.queue_search,1); queue_filters.addWidget(self.health_filter); queue_filters.addWidget(self.clear_filters); left_layout.addLayout(queue_filters)
        self.questions=QTreeWidget(); self.questions.setHeaderLabels(("Type","Question","Saved confidence"))
        self.questions.setRootIsDecorated(False); self.questions.setAlternatingRowColors(False); self.questions.setSelectionMode(QAbstractItemView.ExtendedSelection)
        header=self.questions.header(); header.setStretchLastSection(False)
        header.setSectionResizeMode(0,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1,QHeaderView.Stretch)
        header.setSectionResizeMode(2,QHeaderView.Fixed); self.questions.setColumnWidth(2,120)
        self.questions.itemSelectionChanged.connect(self._selection_changed); self.questions.setContextMenuPolicy(Qt.CustomContextMenu); self.questions.customContextMenuRequested.connect(self.show_question_context_menu); left_layout.addWidget(self.questions,1)
        self.selection_summary=QLabel("One question selected"); self.selection_summary.setObjectName("mutedLabel"); left_layout.addWidget(self.selection_summary)
        left_actions=QHBoxLayout(); refresh=QPushButton("Reload Queue"); self.reload_queue_button=refresh; refresh.clicked.connect(self.refresh); refresh.setToolTip("Reload saved questions and outcomes. This does not scan images or change image data.")
        self.open_image=QPushButton("Show Image",self); self.open_image.clicked.connect(self.show_image); self.open_image.hide()
        self.dispose_noise=QPushButton("Dismiss Queue Noise…"); self.dispose_noise.setAccessibleDescription("Preview dismissal of pending queue noise across all matching pages. Meaningful questions, image data and System Knowledge stay unchanged."); self.dispose_noise.clicked.connect(self.bulk_dispose_noise)
        self.previous_queue=QPushButton("Previous"); self.previous_queue.clicked.connect(lambda:self.change_queue_page(-1))
        self.next_queue=QPushButton("Next"); self.next_queue.clicked.connect(lambda:self.change_queue_page(1)); self.queue_page_label=QLabel()
        for button in (refresh,self.open_image,self.dispose_noise,self.previous_queue,self.next_queue): button.setProperty("compact",True)
        left_actions.addWidget(refresh); left_actions.addWidget(self.dispose_noise); left_actions.addStretch(1); left_actions.addWidget(self.previous_queue); left_actions.addWidget(self.queue_page_label); left_actions.addWidget(self.next_queue); left_layout.addLayout(left_actions)
        splitter.addWidget(left)

        right=QWidget(); right_layout=QVBoxLayout(right); right_layout.setContentsMargins(8,0,0,0)
        decision_splitter=QSplitter(Qt.Horizontal); self.decision_splitter=decision_splitter
        decision_content=QWidget(); decision_layout=QVBoxLayout(decision_content); decision_layout.setContentsMargins(0,0,8,0)
        self.decision_title=QLabel("Select a question"); self.decision_title.setObjectName("sectionTitle"); self.decision_title.hide(); decision_layout.addWidget(self.decision_title)
        self.suggestion=QLabel("Choose a question on the left to see CollectiGen's suggested answer.")
        self.suggestion.setWordWrap(True); self.suggestion.setObjectName("mutedLabel"); self.suggestion.hide(); decision_layout.addWidget(self.suggestion)
        self.editor=QStackedWidget()
        self.empty_editor=EmptyStatePanel(
            "Change Show, Question Type, Queue Quality, or search to see other questions.",
            "No questions in this view",minimum_height=240,
        )
        self.empty_choose_libraries=self.empty_editor.add_action(
            "Choose Libraries…",self.choose_libraries_requested.emit,
        )
        self.empty_choose_libraries.setAccessibleDescription(
            "Choose which existing image libraries Review Center includes. "
            "Opening the chooser does not change the current scope or any question."
        )
        self.empty_choose_libraries.hide()
        self.editor.addWidget(self.empty_editor)
        self.incompatible_editor=QWidget(); incompatible_layout=QVBoxLayout(self.incompatible_editor); incompatible_layout.addStretch(1)
        self.incompatible_message=QLabel(); self.incompatible_message.setAlignment(Qt.AlignCenter); self.incompatible_message.setWordWrap(True); self.incompatible_message.setObjectName("mutedLabel"); incompatible_layout.addWidget(self.incompatible_message); incompatible_layout.addStretch(1)
        self.editor.addWidget(self.incompatible_editor)
        self.tag_editor=self._build_tag_editor(); self.editor.addWidget(self.tag_editor)
        self.group_editor=self._build_group_editor(); self.editor.addWidget(self.group_editor)
        self.image_question_editor=self._build_image_question_editor(); self.editor.addWidget(self.image_question_editor)
        self.collection_editor=self._build_collection_editor(); self.editor.addWidget(self.collection_editor)
        self.source_editor=self._build_source_editor(); self.editor.addWidget(self.source_editor)
        self.meaning_editor=self._build_meaning_editor(); self.editor.addWidget(self.meaning_editor)
        self.broader_editor=self._build_broader_editor(); self.editor.addWidget(self.broader_editor)
        self.merge_editor=self._build_merge_editor(); self.editor.addWidget(self.merge_editor)
        self.other_editor=self._build_other_editor(); self.editor.addWidget(self.other_editor)
        self.history_editor=self._build_history_editor(); self.editor.addWidget(self.history_editor)
        decision_layout.addWidget(self.editor,1); decision_splitter.addWidget(decision_content)
        self.current_image_panel=QGroupBox("Image in question")
        current_layout=QVBoxLayout(self.current_image_panel); current_layout.setContentsMargins(8,8,8,8)
        self.current_image_name=QLabel(); self.current_image_name.setObjectName("mutedLabel")
        self.current_image_name.setWordWrap(True); current_layout.addWidget(self.current_image_name)
        self.current_image_preview=ProtectedImageView(
            policy=content_safety_policy(),settings=self._ui_settings,
        )
        self.current_image_preview.setMinimumSize(260,300)
        # The protected preview owns the temporary-reveal button.  In the
        # Review Center its companion Image Data action is immediately below,
        # so stack the protection status and reveal action within this narrow
        # panel rather than allowing two independently sized horizontal rows.
        preview_controls=self.current_image_preview.layout().itemAt(1).layout()
        preview_controls.setDirection(QBoxLayout.TopToBottom)
        current_layout.addWidget(self.current_image_preview,1)
        current_actions=QVBoxLayout()
        self.current_image_edit=QPushButton("Edit Image Data…")
        self.current_image_edit.setProperty("compact",True)
        self.current_image_edit.clicked.connect(self.edit_current_image_data)
        current_actions.addWidget(self.current_image_edit); current_layout.addLayout(current_actions)
        self.current_image_path=""
        action_width=max(
            self.current_image_edit.sizeHint().width(),
            self.current_image_preview.reveal_button.sizeHint().width(),
        )
        self.current_image_panel.setMinimumWidth(max(280,action_width+16)); self.current_image_panel.setMaximumWidth(430); self.current_image_panel.hide()
        decision_splitter.addWidget(self.current_image_panel); decision_splitter.setStretchFactor(0,4); decision_splitter.setStretchFactor(1,1)
        right_layout.addWidget(decision_splitter,3)
        self.examples_panel=QWidget(); examples_layout=QVBoxLayout(self.examples_panel); examples_layout.setContentsMargins(0,0,0,0)
        self.examples_help=QLabel("Use these images as evidence for this exact question. Double-click an image to edit its Image Knowledge."); self.examples_help.setWordWrap(True); self.examples_help.setObjectName("mutedLabel"); examples_layout.addWidget(self.examples_help)
        card_width=max(136,min(180,int(getattr(self._ui_settings,"gallery_card_width",150) or 150)))
        self.context_gallery=EmbeddedGallery(page_size=12,card_width=card_width,show_open_dataset=False); self.context_gallery.setMinimumHeight(240)
        self.context_gallery.visible_paths_changed.connect(self.gallery_status.request)
        self.gallery_status.updated.connect(self.context_gallery.refresh_cards)
        self.context_gallery.edit_requested.connect(self.edit_image_data); self.context_gallery.force_ai_requested.connect(self.force_ai_requested); examples_layout.addWidget(self.context_gallery,1); self.examples_panel.hide(); right_layout.addWidget(self.examples_panel,2)
        self.example_gallery=self.context_gallery; self.image_question_gallery=self.context_gallery; self.merge_gallery=self.context_gallery
        splitter.addWidget(right); splitter.setStretchFactor(0,3); splitter.setStretchFactor(1,4)

    def _set_related_gallery_empty_state(self,empty):
        """Let the embedded gallery provide the one empty-result explanation."""
        self.context_gallery.status.setVisible(not empty)
        self.examples_help.setVisible(not empty)

    def open_guided_review(self,parent=None):
        self.characters.load(); self.categories.load(); self.tags.load()
        dialog=GuidedTagReviewDialog(
            self.state,self.categories,self.characters,parent or self,
            history_operation_factory=lambda images:capture_operation(
                self,images,operation_kind="guided_review",reason="Reviewed per-image topic decision"),
            history_opener=self._open_conflict_history,
        )
        if not hasattr(self,"_guided_dialogs"):self._guided_dialogs=[]
        self._guided_dialogs.append(dialog)
        dialog.background_work_finished.connect(self._emit_background_work_finished_if_idle)
        dialog.data_changed.connect(lambda:self.refresh(reload=True))
        dialog.edit_image_data.connect(self.edit_image_data)
        dialog.open_dataset_image.connect(self.open_dataset_image)
        dialog.force_ai_requested.connect(self.force_ai_requested)
        try:
            dialog.exec()
        finally:
            self._guided_dialogs.remove(dialog)
            dialog.deleteLater()
            self._emit_background_work_finished_if_idle()

    def open_function(self,function_id,scope=None):
        function_id=str(function_id or "")
        if function_id=="review.conflicts.guided_checks":
            return self.open_conflict_review(scope=scope)
        if function_id=="review.character_collections.discover":
            self._show_collection_questions()
            self.discover_character_collections()
            return True
        if function_id=="review.character_sources.discover":
            self._show_source_questions()
            self.discover_character_sources()
            return True
        if function_id=="review.knowledge.discovery":
            controls=(
                self.status_filter,self.kind_filter,
                self.health_filter,self.queue_search,
            )
            for control in controls:control.blockSignals(True)
            try:
                status_index=self.status_filter.findData("pending")
                if status_index>=0:self.status_filter.setCurrentIndex(status_index)
                kind_index=self.kind_filter.findText("SDXL Tags and meanings")
                if kind_index>=0:self.kind_filter.setCurrentIndex(kind_index)
                health_index=self.health_filter.findData("meaningful")
                if health_index>=0:self.health_filter.setCurrentIndex(health_index)
                self.queue_search.clear()
            finally:
                for control in controls:control.blockSignals(False)
            self.queue_page=0
            scope=dict(scope) if isinstance(scope,dict) else {}
            self.refresh(
                reload=False,select_id=str(scope.get("select_id") or ""),
            )
            handoff=(
                "This Knowledge handoff uses saved local evidence only; it did "
                "not reread TXT or Metadata or contact an online provider."
            )
            if handoff not in self.queue_freshness.text():
                self.queue_freshness.setText(
                    self.queue_freshness.text()+" • "+handoff
                )
            return True
        return False

    def open_conflict_review(self,*_args,scope=None,parent=None):
        self.characters.load(); self.categories.load(); self.tags.load()
        dialog=ConflictReviewDialog(
            self.state,parent or self,scope=scope,
            history_operation_factory=lambda images:capture_operation(
                self,images,operation_kind="conflict_review",reason="Reviewed per-image conflict decisions"),
            history_opener=self._open_conflict_history,
        )
        if not hasattr(self,"_conflict_dialogs"):self._conflict_dialogs=[]
        self._conflict_dialogs.append(dialog)
        dialog.background_work_finished.connect(self._emit_background_work_finished_if_idle)
        dialog.data_changed.connect(lambda:self.refresh(reload=True))
        dialog.edit_image_data.connect(self.edit_image_data)
        dialog.open_dataset_image.connect(self.open_dataset_image)
        dialog.open_automation.connect(self.open_automation)
        dialog.force_ai_requested.connect(self.force_ai_requested)
        try:
            dialog.exec()
        finally:
            self._conflict_dialogs.remove(dialog)
            dialog.deleteLater()
            self._emit_background_work_finished_if_idle()
        return True

    def _open_conflict_history(self,images,operation_id):
        return open_history(self,tuple(images),operation_target=operation_id,
            on_complete=self._conflict_history_restored,
            recovery_handler=lambda path:self.edit_image_data.emit(str(path)))

    def _conflict_history_restored(self,images):
        dataset=getattr(self.state,"dataset",None)
        events=getattr(self.state,"events",None)
        refresh_errors=[]
        for image in images:
            if dataset is not None and getattr(dataset,"index",None):
                try:dataset.cache_managed_record(image,ImageRecord(image))
                except Exception as exc:refresh_errors.append(str(exc))
            if events is not None:events.publish("image_data_changed",image=str(image),source="saved_history")
        if events is not None:events.publish("reviews_changed")
        self.refresh(reload=True)
        self.data_changed.emit()
        if refresh_errors:
            QMessageBox.warning(self,"Saved restoration completed",
                "Restored data is saved, but the library display needs a refresh: "+refresh_errors[0])

    def _notify_background_work_finished(self):
        self._emit_background_work_finished_if_idle()

    def _dismiss_button(self):
        button=QPushButton(DISMISS_QUESTION_LABEL)
        button.setAccessibleDescription(
            "Remove this exact question from Needs My Decision without changing image data or System Knowledge. Return it manually from Dismissed Questions."
        )
        button.clicked.connect(lambda:self.set_status("ignored"))
        return button

    def _build_tag_editor(self):
        page=QWidget(); layout=QVBoxLayout(page); layout.setContentsMargins(0,4,0,0)
        self.tag_scope=QLabel(); self.tag_scope.setObjectName("sectionTitle"); self.tag_scope.setWordWrap(True); self.tag_scope.hide(); layout.addWidget(self.tag_scope)
        outcomes=QGroupBox("What should CollectiGen do with this SDXL Tag?"); outcome_layout=QVBoxLayout(outcomes)
        self.tag_outcomes_group=outcomes
        self.outcome_group=QButtonGroup(self); self.outcomes={}
        choices=(
            ("keep","Keep it as a recognized SDXL Tag","Use this wording in organized data and training captions."),
            ("existing","Treat it as another name for a known SDXL Tag","Normalize this wording to the existing SDXL Tag selected below."),
            ("exclude","Exclude it from training captions","Remember the SDXL Tag, but omit it from generated training text."),
        )
        for value,label,help_text in choices:
            radio=QRadioButton(label); radio.setProperty("choice",value); self.outcome_group.addButton(radio)
            radio.setToolTip(help_text); self.outcomes[value]=radio; outcome_layout.addWidget(radio)
        self.outcomes["keep"].setChecked(True); self.outcome_group.buttonClicked.connect(self.update_tag_controls)
        layout.addWidget(outcomes)
        self.tag_fields=QGroupBox("Confirm the meaning"); fields=QGridLayout(self.tag_fields)
        fields.addWidget(QLabel("Organize under"),0,0); self.category=BoundedSearchableComboBox(placeholder="Type any part of a category path…",max_visible=12); self.category.setAccessibleName("Organize under")
        for category_id,label in self.categories.choices(): self.category.addItem(label,category_id)
        self.category.currentIndexChanged.connect(self.category_changed); fields.addWidget(self.category,0,1)
        self.meaning=QLabel(); self.meaning.setWordWrap(True); self.meaning.setObjectName("mutedLabel"); fields.addWidget(self.meaning,1,0,1,2)
        layout.addWidget(self.tag_fields)
        self.existing_fields=QGroupBox("Choose the preferred known SDXL Tag"); existing_layout=QVBoxLayout(self.existing_fields)
        self.existing=BoundedSearchableComboBox(placeholder="Type any part of a known name or alias…",max_visible=12); self.existing.setAccessibleName("Preferred known SDXL Tag")
        existing_layout.addWidget(self.existing); layout.addWidget(self.existing_fields); self.existing_fields.hide()
        self.tag_spacer=QSpacerItem(0,0,QSizePolicy.Minimum,QSizePolicy.Expanding); layout.addItem(self.tag_spacer)
        buttons=QHBoxLayout(); self.related_button=QPushButton(); self.related_button.clicked.connect(self.apply_related); self.related_button.hide()
        self.tag_dismiss_button=self._dismiss_button()
        self.tag_save_button=QPushButton("Save Decision"); self.tag_save_button.setProperty("primary",True); self.tag_save_button.clicked.connect(self.save_tag)
        self.shared_apply_button=QPushButton("Apply Shared Decision…"); self.shared_apply_button.setProperty("primary",True); self.shared_apply_button.clicked.connect(self.apply_selected_tag_decision); self.shared_apply_button.hide()
        buttons.addWidget(self.related_button); buttons.addStretch(1); buttons.addWidget(self.tag_dismiss_button); buttons.addWidget(self.tag_save_button); buttons.addWidget(self.shared_apply_button); layout.addLayout(buttons)
        return page

    def _build_group_editor(self):
        page=QWidget(); layout=QVBoxLayout(page); layout.setContentsMargins(0,4,0,0)
        help_text=QLabel("CollectiGen found several SDXL Tags with the same structure. Leave matching SDXL Tags checked, uncheck exceptions, and confirm the shared organization once.")
        help_text.setWordWrap(True); help_text.setObjectName("mutedLabel"); layout.addWidget(help_text)
        self.group_tags=RangeCheckListWidget(); layout.addWidget(self.group_tags,1)
        category_row=QHBoxLayout(); category_row.addWidget(QLabel("Organize checked SDXL Tags under")); self.group_category=BoundedSearchableComboBox(placeholder="Type any part of a category path…",max_visible=12); self.group_category.setAccessibleName("Organize checked SDXL Tags under")
        for category_id,label in self.categories.choices(): self.group_category.addItem(label,category_id)
        category_row.addWidget(self.group_category,1); layout.addLayout(category_row)
        actions=QHBoxLayout(); later=self._dismiss_button()
        save=QPushButton("Save Group Decision"); save.setProperty("primary",True); save.clicked.connect(self.save_group)
        actions.addStretch(1); actions.addWidget(later); actions.addWidget(save); layout.addLayout(actions); return page

    def _build_image_question_editor(self):
        page=QWidget(); layout=QVBoxLayout(page); layout.setContentsMargins(0,4,0,0)
        details=QGroupBox("Proposed image information"); details_layout=QVBoxLayout(details)
        self.image_question_text=QLabel(); self.image_question_text.setWordWrap(True); details_layout.addWidget(self.image_question_text)
        self.image_question_source=QLabel(); self.image_question_source.setObjectName("mutedLabel"); self.image_question_source.setWordWrap(True); details_layout.addWidget(self.image_question_source)
        self.image_question_values=QListWidget(); self.image_question_values.setMaximumHeight(125); details_layout.addWidget(self.image_question_values)
        layout.addWidget(details)
        self.style_fields=QGroupBox("Classify the visual style"); style_layout=QGridLayout(self.style_fields)
        style_layout.addWidget(QLabel("Relationship"),0,0); self.style_relationship=QComboBox()
        for label,value in (
            ("Original source style","original"),("Modified source style","modified"),
            ("Style of another source","cross_source"),("Mixed styles","mixed"),
        ): self.style_relationship.addItem(label,value)
        self.style_relationship.currentIndexChanged.connect(self.update_style_controls); style_layout.addWidget(self.style_relationship,0,1)
        style_layout.addWidget(QLabel("Style source"),1,0); self.style_series=BoundedSearchableComboBox(
            placeholder="Type any part of a Character Source or alias…",
            max_visible=12,
        ); self.style_series.setAccessibleName("Style source"); style_layout.addWidget(self.style_series,1,1)
        layout.addWidget(self.style_fields); self.style_fields.hide(); layout.addStretch(1)
        actions=QHBoxLayout(); open_dataset=QPushButton("Open in Dataset"); open_dataset.clicked.connect(self.open_in_dataset)
        self.image_question_open_dataset=open_dataset
        self.image_question_edit=QPushButton("Edit Image Data…")
        self.image_question_edit.clicked.connect(self.edit_current_image_data)
        reject=QPushButton("Reject Change"); reject.clicked.connect(self.reject_image_question)
        self.image_question_reject=reject
        later=self._dismiss_button()
        apply=QPushButton("Apply Change"); apply.setProperty("primary",True); apply.clicked.connect(self.apply_image_question)
        self.image_question_apply=apply
        actions.addWidget(open_dataset); actions.addWidget(self.image_question_edit); actions.addStretch(1); actions.addWidget(reject); actions.addWidget(later); actions.addWidget(apply); layout.addLayout(actions)
        return page

    def _build_collection_editor(self):
        page=QWidget(); layout=QVBoxLayout(page); layout.setContentsMargins(0,4,0,0)
        details=QGroupBox("Proposed collection member"); grid=QGridLayout(details)
        grid.addWidget(QLabel("Collection"),0,0)
        self.collection_owner=QLabel(); self.collection_owner.setWordWrap(True); grid.addWidget(self.collection_owner,0,1)
        grid.addWidget(QLabel("Candidate"),1,0)
        self.collection_target=QLabel(); self.collection_target.setWordWrap(True); grid.addWidget(self.collection_target,1,1)
        grid.addWidget(QLabel("Current evidence"),2,0)
        self.collection_metrics=QLabel(); self.collection_metrics.setWordWrap(True); grid.addWidget(self.collection_metrics,2,1)
        grid.addWidget(QLabel("Source scope"),3,0)
        self.collection_sources=QLabel(); self.collection_sources.setWordWrap(True); grid.addWidget(self.collection_sources,3,1)
        layout.addWidget(details)
        self.collection_effect=QLabel(
            "Approval updates only this reusable Character Knowledge collection. "
            "Rejection remembers this exact candidate. Neither choice changes "
            "Image Knowledge, captions, TXT, Metadata, or image pixels."
        )
        self.collection_effect.setWordWrap(True); self.collection_effect.setObjectName("mutedLabel")
        layout.addWidget(self.collection_effect); layout.addStretch(1)
        actions=QHBoxLayout(); show=QPushButton("Show Evidence Image"); show.clicked.connect(self.show_image)
        show.setAccessibleDescription("Show one of the saved representative examples supporting this exact collection question.")
        reject=QPushButton("Reject Candidate"); reject.clicked.connect(lambda:self.apply_collection_decision("rejected"))
        reject.setAccessibleDescription("Remember this exact Character or outfit collection candidate as rejected without changing Image Knowledge.")
        accept=QPushButton("Add to Collection"); accept.setProperty("primary",True); accept.clicked.connect(lambda:self.apply_collection_decision("accepted"))
        accept.setAccessibleDescription("Add this exact reviewed Tag to the named Character Knowledge collection with backup and read-back verification.")
        self.collection_reject=reject; self.collection_accept=accept
        actions.addWidget(show); actions.addStretch(1); actions.addWidget(reject); actions.addWidget(accept); layout.addLayout(actions)
        return page

    def _build_source_editor(self):
        page=QWidget(); layout=QVBoxLayout(page); layout.setContentsMargins(0,4,0,0)
        details=QGroupBox("Proposed Character Knowledge relationship"); grid=QGridLayout(details)
        grid.addWidget(QLabel("Character Identity"),0,0)
        self.source_character=QLabel(); self.source_character.setWordWrap(True); grid.addWidget(self.source_character,0,1)
        grid.addWidget(QLabel("Character Source"),1,0)
        self.source_target=QLabel(); self.source_target.setWordWrap(True); grid.addWidget(self.source_target,1,1)
        grid.addWidget(QLabel("Proposed change"),2,0)
        self.source_change=QLabel(); self.source_change.setWordWrap(True); grid.addWidget(self.source_change,2,1)
        grid.addWidget(QLabel("Current evidence"),3,0)
        self.source_evidence=QLabel(); self.source_evidence.setWordWrap(True); grid.addWidget(self.source_evidence,3,1)
        grid.addWidget(QLabel("Source scope"),4,0)
        self.source_scope=QLabel(); self.source_scope.setWordWrap(True); grid.addWidget(self.source_scope,4,1)
        layout.addWidget(details)
        self.source_effect=QLabel(
            "The source association and optional Other Name are separate reviewed "
            "decisions. Neither changes Image Knowledge, captions, TXT, Metadata, "
            "or image pixels."
        )
        self.source_effect.setWordWrap(True); self.source_effect.setObjectName("mutedLabel")
        layout.addWidget(self.source_effect); layout.addStretch(1)
        actions=QHBoxLayout(); show=QPushButton("Show Evidence Image"); show.clicked.connect(self.show_image)
        show.setAccessibleDescription("Show one saved image supporting this exact Character Identity and Character Source pair.")
        reject=QPushButton("Reject Candidate"); reject.clicked.connect(lambda:self.apply_source_decision("rejected"))
        reject.setAccessibleDescription("Remember this exact Character Source or Other Name candidate as rejected without changing Image Knowledge.")
        accept=QPushButton("Save Reviewed Change"); accept.setProperty("primary",True); accept.clicked.connect(lambda:self.apply_source_decision("accepted"))
        accept.setAccessibleDescription("Save only the exact reviewed Character Source relationship or source-qualified Other Name using verified authority writers.")
        self.source_reject=reject; self.source_accept=accept
        actions.addWidget(show); actions.addStretch(1); actions.addWidget(reject); actions.addWidget(accept); layout.addLayout(actions)
        return page

    def _build_other_editor(self):
        page=QWidget(); layout=QVBoxLayout(page); layout.setContentsMargins(0,4,0,0)
        self.other_details=QTextEdit(); self.other_details.setReadOnly(True); layout.addWidget(self.other_details,1)
        actions=QHBoxLayout(); open_dataset=QPushButton("Open in Dataset"); open_dataset.clicked.connect(self.open_in_dataset)
        later=self._dismiss_button()
        self.other_done=QPushButton("Complete Question"); self.other_done.setProperty("primary",True); self.other_done.clicked.connect(lambda:self.set_status("resolved"))
        self.other_done.setAccessibleDescription(
            "Move only this question to Completed history. Image data and whole-image review status remain unchanged."
        )
        actions.addWidget(open_dataset); actions.addStretch(1); actions.addWidget(later); actions.addWidget(self.other_done); layout.addLayout(actions); return page

    def _build_meaning_editor(self):
        page=QWidget(); layout=QVBoxLayout(page); layout.setContentsMargins(0,4,0,0)
        comparison=QGroupBox("Current and proposed Meaning & Usage")
        grid=QGridLayout(comparison)
        grid.addWidget(QLabel("Current reviewed summary"),0,0)
        self.meaning_current=QTextEdit(); self.meaning_current.setReadOnly(True)
        self.meaning_current.setMaximumHeight(105); self.meaning_current.setAccessibleName("Current reviewed Meaning and Usage")
        grid.addWidget(self.meaning_current,0,1)
        grid.addWidget(QLabel("Attributed proposal"),1,0)
        self.meaning_variant=QComboBox(); self.meaning_variant.setAccessibleName("Attributed Meaning and Usage proposal")
        self.meaning_variant.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.meaning_variant.setMinimumContentsLength(24)
        self.meaning_variant.currentIndexChanged.connect(self._show_meaning_variant)
        grid.addWidget(self.meaning_variant,1,1)
        grid.addWidget(QLabel("Proposed summary"),2,0)
        self.meaning_proposed=QTextEdit(); self.meaning_proposed.setReadOnly(True)
        self.meaning_proposed.setMaximumHeight(120); self.meaning_proposed.setAccessibleName("Proposed Meaning and Usage summary")
        grid.addWidget(self.meaning_proposed,2,1)
        layout.addWidget(comparison)
        evidence=QGroupBox("Source attribution and freshness")
        evidence_layout=QVBoxLayout(evidence)
        self.meaning_sources=QTextEdit(); self.meaning_sources.setReadOnly(True)
        self.meaning_sources.setAccessibleName("Meaning and Usage source observations")
        evidence_layout.addWidget(self.meaning_sources)
        layout.addWidget(evidence,1)
        effect=QLabel(
            "Approval replaces only this Tag's reviewed Meaning & Usage summary and records the selected source identities, versions, retrieval times, freshness, and review decision. Rejection remembers this exact evidence set. Neither choice changes Image Knowledge, captions, TXT, Metadata, or pixels."
        )
        effect.setWordWrap(True); effect.setObjectName("mutedLabel"); layout.addWidget(effect)
        actions=QHBoxLayout()
        reject=QPushButton("Reject Evidence Set"); reject.clicked.connect(lambda:self.apply_meaning_decision("rejected"))
        reject.setAccessibleDescription("Remember this exact attributed proposal set as rejected without changing System or Image Knowledge.")
        accept=QPushButton("Save Reviewed Summary"); accept.setProperty("primary",True); accept.clicked.connect(lambda:self.apply_meaning_decision("accepted"))
        accept.setAccessibleDescription("Save only the selected attributed summary through the stale-safe System Knowledge writer.")
        self.meaning_reject=reject; self.meaning_accept=accept
        actions.addStretch(1); actions.addWidget(reject); actions.addWidget(accept); layout.addLayout(actions)
        return page

    def _build_broader_editor(self):
        page=QWidget(); layout=QVBoxLayout(page); layout.setContentsMargins(0,4,0,0)
        comparison=QGroupBox("Current and proposed Broader Tag")
        grid=QGridLayout(comparison)
        grid.addWidget(QLabel("Current relationship"),0,0)
        self.broader_current=QLabel("No active canonical Broader Tag")
        self.broader_current.setWordWrap(True); self.broader_current.setObjectName("mutedLabel")
        grid.addWidget(self.broader_current,0,1)
        grid.addWidget(QLabel("Proposed relationship"),1,0)
        self.broader_proposed=QLabel(); self.broader_proposed.setWordWrap(True)
        self.broader_proposed.setAccessibleName("Proposed canonical Broader Tag relationship")
        grid.addWidget(self.broader_proposed,1,1)
        grid.addWidget(QLabel("Source category"),2,0)
        self.broader_source_category=QLabel(); self.broader_source_category.setWordWrap(True); grid.addWidget(self.broader_source_category,2,1)
        grid.addWidget(QLabel("Target category"),3,0)
        self.broader_target_category=QLabel(); self.broader_target_category.setWordWrap(True); grid.addWidget(self.broader_target_category,3,1)
        layout.addWidget(comparison)
        evidence=QGroupBox("Reusable-pattern evidence and downstream effects")
        evidence_layout=QVBoxLayout(evidence)
        self.broader_evidence=QTextEdit(); self.broader_evidence.setReadOnly(True)
        self.broader_evidence.setAccessibleName("Broader Tag proposal evidence and effects")
        evidence_layout.addWidget(self.broader_evidence)
        layout.addWidget(evidence,1)
        effect=QLabel(
            "Pattern evidence and confidence rank this question only. Approval writes one canonical Broader Tag through the verified relationship transaction. Rejection remembers this exact proposal. Neither choice changes Image Knowledge, captions, TXT, Metadata, or pixels."
        )
        effect.setWordWrap(True); effect.setObjectName("mutedLabel"); layout.addWidget(effect)
        actions=QHBoxLayout()
        reject=QPushButton("Reject Proposal"); reject.clicked.connect(lambda:self.apply_broader_decision("rejected"))
        reject.setAccessibleDescription("Remember this exact Broader Tag proposal as rejected without changing System or Image Knowledge.")
        accept=QPushButton("Save Broader Tag"); accept.setProperty("primary",True); accept.clicked.connect(lambda:self.apply_broader_decision("accepted"))
        accept.setAccessibleDescription("Save this exact canonical Broader Tag through the stale-safe relationship writer.")
        self.broader_reject=reject; self.broader_accept=accept
        actions.addStretch(1); actions.addWidget(reject); actions.addWidget(accept); layout.addLayout(actions)
        return page

    def _build_merge_editor(self):
        page=QWidget(); layout=QVBoxLayout(page); layout.setContentsMargins(0,4,0,0)
        details=QGroupBox("Legacy training-projection suggestion"); details_layout=QGridLayout(details)
        details_layout.addWidget(QLabel("SDXL Tag location"),0,0); self.merge_location=QLabel(); details_layout.addWidget(self.merge_location,0,1)
        details_layout.addWidget(QLabel("Current SDXL Tags"),1,0); self.merge_before=QLabel(); self.merge_before.setWordWrap(True); details_layout.addWidget(self.merge_before,1,1)
        details_layout.addWidget(QLabel("Concise training projection"),2,0); self.merge_after=QLabel(); self.merge_after.setWordWrap(True); details_layout.addWidget(self.merge_after,2,1)
        details_layout.addWidget(QLabel("Rule"),3,0); self.merge_rule=QLabel(); self.merge_rule.setWordWrap(True); self.merge_rule.setObjectName("mutedLabel"); details_layout.addWidget(self.merge_rule,3,1)
        layout.addWidget(details); layout.addStretch(1)
        actions=QHBoxLayout(); open_dataset=QPushButton("Open in Dataset"); open_dataset.clicked.connect(self.open_in_dataset)
        disable=QPushButton("Disable Rule…"); disable.clicked.connect(self.disable_merge_rule)
        disable.setToolTip("Stop this cleanup rule for every matching question. Review the affected count before saving; image data stays unchanged.")
        reject=QPushButton("Reject Suggestion"); reject.clicked.connect(self.reject_merge)
        reject.setToolTip("Keep every confirmed image fact and close only this suggestion. The cleanup rule stays enabled.")
        later=self._dismiss_button()
        apply=QPushButton("Complete Question"); apply.setProperty("primary",True); apply.clicked.connect(self.apply_merge)
        apply.setAccessibleDescription(
            "Complete this legacy training-projection question after checking the preview. Image data, captions, the rule, and whole-image review status remain unchanged."
        )
        actions.addWidget(open_dataset); actions.addWidget(disable); actions.addStretch(1); actions.addWidget(reject); actions.addWidget(later); actions.addWidget(apply); layout.addLayout(actions)
        return page

    def _build_history_editor(self):
        page=QWidget(); layout=QVBoxLayout(page); layout.setContentsMargins(0,4,0,0)
        self.history=QTextEdit(); self.history.setReadOnly(True); layout.addWidget(self.history,1)
        edit_tag=QPushButton("Edit Saved Tag…"); edit_tag.clicked.connect(self.edit_saved_tag)
        self.history_edit_tag=edit_tag; layout.addWidget(edit_tag,0,Qt.AlignRight)
        reopen=QPushButton("Reopen"); reopen.clicked.connect(lambda:self.set_status("pending")); self.history_reopen=reopen; layout.addWidget(reopen,0,Qt.AlignRight)
        return page

    def edit_saved_tag(self):
        task=self.selected_task()
        if task is None or task.task_type!="new_tag" or task.status!="resolved":
            return
        resolution=task.details.get("resolution") or {}
        stable_id=str(resolution.get("stable_id") or "")
        record=self.tags.tags.get(stable_id) if stable_id else None
        if record is None and not stable_id:
            name=str(resolution.get("target") or task.details.get("tag") or "").strip()
            record=self.tags.find(name) if name else None
            if record is not None and resolution.get("target") and record.display!=resolution["target"]:
                record=None
        if record is None:
            QMessageBox.information(
                self,"Saved Tag unavailable",
                "The exact saved Tag is no longer available. This completed Review decision was kept. "
                "Open Tag Encyclopedia to inspect current System Knowledge before making a correction.",
            )
            return
        self.edit_tag_requested.emit(record.id,self)

    @staticmethod
    def friendly_type(task_type: str):
        return {"new_tag":"Unknown SDXL Tag","pattern_group":"Related SDXL Tag family",TAG_MEANING_TASK_TYPE:"Meaning & Usage evidence",TAG_BROADER_PROPOSAL_TASK_TYPE:"Broader Tag proposal","collection_member":"Character collection",SOURCE_LINK_TASK_TYPE:"Character Source",SOURCE_ALIAS_TASK_TYPE:"Character Other Name","merge_suggestion":"Legacy projection","image_question":"Image information"}.get(task_type,"Other question")

    @staticmethod
    def friendly_title(task):
        if task.task_type=="new_tag": return f"What does “{task.details.get('tag','this SDXL Tag')}” mean?"
        if task.task_type=="pattern_group":
            tags=list(task.details.get("tags",[])); return f"Can these {len(tags)} related SDXL Tag(s) share one interpretation?"
        if task.task_type==TAG_MEANING_TASK_TYPE:
            return f"Which attributed Meaning & Usage summary fits “{task.details.get('tag','this SDXL Tag')}”?"
        if task.task_type==TAG_BROADER_PROPOSAL_TASK_TYPE:
            return f"Should “{task.details.get('tag','this SDXL Tag')}” use “{task.details.get('target','this target')}” as its Broader Tag?"
        if task.task_type=="image_question": return task.title or "What information belongs to this image?"
        return task.title or "Review this legacy training-projection suggestion"

    def _task_in_library_scope(self,task) -> bool:
        return review_task_in_library_scope(task,self.state.dataset)

    def _queue_source_token(self):
        path=Path(self.tasks.path)
        try:
            stat=path.stat(); file_token=(stat.st_mtime_ns,stat.st_size)
        except OSError:
            file_token=(0,0)
        return file_token+(id(self.tasks.tasks),len(self.tasks.tasks))

    def _invalidate_queue_indexes(self):
        self._visible_task_cache_key=None; self._visible_task_cache=()
        self._related_pattern_cache_key=None; self._related_pattern_index={}

    def visible_tasks(self):
        wanted=str(self.status_filter.currentData() or "pending")
        kinds=self.kind_filter.currentData()
        kind_key=tuple(sorted(kinds)) if kinds else ()
        health=str(self.health_filter.currentData() or "")
        query=self.queue_search.text().strip().casefold()
        dataset=getattr(self.state,"dataset",None)
        scope_key=(
            id(dataset),
            tuple(sorted(str(value) for value in getattr(dataset,"scope_library_ids",()))),
        )
        key=(self._queue_source_token(),wanted,kind_key,health,query,scope_key)
        if key==self._visible_task_cache_key:
            return list(self._visible_task_cache)
        tasks=[task for task in self.tasks.tasks.values() if task.status==wanted]
        tasks=[task for task in tasks if self._task_in_library_scope(task)]
        if kinds: tasks=[task for task in tasks if task.task_type in kinds]
        if health: tasks=[task for task in tasks if task.details.get("queue_health",{}).get("category")==health]
        if query: tasks=[task for task in tasks if query in (self.friendly_title(task)+" "+self.friendly_type(task.task_type)).casefold()]
        def queue_order(task):
            rerank=task.details.get("related_rerank")
            if not isinstance(rerank,dict):
                rerank=task.details.get("meaning_rerank")
            rerank=rerank if isinstance(rerank,dict) else {}
            try:rank=max(0,int(rerank.get("rank") or 0))
            except (TypeError,ValueError):rank=0
            family=str(rerank.get("family_key") or rerank.get("tag_id") or "")
            return (
                task.task_type,0 if rank else 1,family,rank or 2**31,
                self.friendly_title(task).casefold(),task.id,
            )
        result=tuple(sorted(tasks,key=queue_order))
        self._visible_task_cache_key=key; self._visible_task_cache=result
        return list(result)

    def refresh(
        self,*_,select_id: str="",reload=True,select_first: bool=True,
    ):
        selected_ids={task.id for task in self.selected_tasks()} if not select_id else {select_id}
        current=self.selected_task()
        current_id=select_id or (current.id if current else "")
        if reload:
            self.tags.load(); self.discovery.load(); self.categories.load(); self.tasks.load()
            self._clear_queue_status_failure(reloaded=True)
            self._queue_reconciled_count=self.tasks.reconcile_known_tags(self.tags)
            self._queue_health_counts=None; self._queue_loaded_at=datetime.now().astimezone().strftime("%Y-%m-%d %H:%M")
            self._invalidate_queue_indexes()
        if self._queue_health_counts is None: self._queue_health_counts=self.tasks.triage_pending()
        scoped_tasks=(task for task in self.tasks.tasks.values() if self._task_in_library_scope(task))
        scoped_statuses={}
        for task in scoped_tasks:
            scoped_statuses[task.status]=scoped_statuses.get(task.status,0)+1
        pending_visible=scoped_statuses.get("pending",0)
        has_scoped_questions=bool(scoped_statuses)
        self.empty_choose_libraries.setVisible(
            not has_scoped_questions and bool(self.tasks.tasks)
        )
        for widget in (self.status_filter_label,self.status_filter,self.kind_filter_label,
                       self.kind_filter,self.queue_column):
            widget.setVisible(has_scoped_questions)
        self.header.set_status(
            f"{counted_noun(pending_visible,'pending knowledge question')}"
        )
        reconciled=int(getattr(self,"_queue_reconciled_count",0) or 0)
        reconciled_text=(
            f" • {counted_noun(reconciled,'obsolete question')} cleared"
            if reconciled else ""
        )
        self.queue_freshness.setVisible(has_scoped_questions)
        self.queue_freshness.setText(
            f"Saved queue loaded {self._queue_loaded_at or 'this session'}"
            f"{reconciled_text} • Updates automatically when saved knowledge changes. "
            "The total counts individual suggestions and evidence, not images or errors; "
            "one image may have several items."
            if has_scoped_questions else ""
        )
        blocker=QSignalBlocker(self.questions)
        self.questions.clear(); all_tasks=self.visible_tasks(); total_visible=len(all_tasks)
        target_id=select_id or (current_id if reload else "")
        if target_id:
            target=next((index for index,task in enumerate(all_tasks) if task.id==target_id),None)
            if target is not None:self.queue_page=target//self.queue_page_size
        maximum_page=max(0,(total_visible-1)//self.queue_page_size); self.queue_page=min(self.queue_page,maximum_page)
        start=self.queue_page*self.queue_page_size; tasks=all_tasks[start:start+self.queue_page_size]
        items=[]; selected_item=None
        for task in tasks:
            status,confidence=self._task_visual_status(task)
            confidence_text=f"{confidence:.0%}" if confidence is not None else "No estimate"
            item=QTreeWidgetItem((self.friendly_type(task.task_type),self.friendly_title(task),confidence_text)); item.setData(0,Qt.UserRole,task.id); color_tree_item(item,status)
            if task.status=="resolved":item.setToolTip(0,"Reviewed question")
            elif task.status=="ignored":item.setToolTip(0,"Dismissed question")
            item.setToolTip(2,
                f"Saved evidence confidence: {confidence:.0%}" if confidence is not None
                else "No evidence confidence estimate was saved."
            )
            items.append(item)
            if task.id==current_id: selected_item=item
        self.questions.addTopLevelItems(items)
        if selected_item is not None:self.questions.setCurrentItem(selected_item)
        for item in items:item.setSelected(str(item.data(0,Qt.UserRole)) in selected_ids)
        if (
            select_first and not self.questions.selectedItems()
            and self.questions.topLevelItemCount()
        ):
            self.questions.setCurrentItem(self.questions.topLevelItem(0))
            self.questions.topLevelItem(0).setSelected(True)
        del blocker
        if not tasks:
            status=str(self.status_filter.currentData())
            status_total=scoped_statuses.get(status,0)
            self.empty_editor.setTitle(
                "No questions in included libraries" if not has_scoped_questions and self.tasks.tasks
                else "No matching questions" if status_total else {
                    "pending":"No pending knowledge questions",
                    "ignored":"No dismissed questions",
                    "resolved":"No completed questions",
                }.get(status,"No questions in this view")
            )
            if (
                pending_visible and str(self.status_filter.currentData())=="pending"
                and str(self.health_filter.currentData())=="meaningful"
                and not self.kind_filter.currentData() and not self.queue_search.text().strip()
            ):
                self.empty_editor.setText(
                    "No pending questions match Meaningful Decisions. "
                    "Choose All Queue Quality to see every question."
                )
            elif not has_scoped_questions and self.tasks.tasks:
                self.empty_editor.setText(
                    "Saved questions exist outside the included libraries. Choose which libraries "
                    "to include; no decision or image is changed by opening the chooser."
                )
            elif not status_total:
                self.empty_editor.setText({
                    "pending":"New questions appear as saved knowledge changes. Generated images still need review in Ingest; an empty queue does not establish training readiness.",
                    "ignored":"Questions you dismiss remain here until you deliberately restore them.",
                    "resolved":"Saved decisions will appear here. Completing a question does not mark every related image reviewed.",
                }.get(status,"Choose another status to see saved questions."))
            else:
                self.empty_editor.setText(
                    "Clear Filters to show every question in this status, or change Show to inspect another status."
                )
            self.clear_selected()
        self.safe_button.setVisible(str(self.status_filter.currentData())=="pending")
        has_noise=any(task.status=="pending" and task.details.get("queue_health",{}).get("category") not in {"",None,"meaningful"} for task in all_tasks)
        self.dispose_noise.setVisible(has_noise); self.dispose_noise.setEnabled(has_noise)
        self.previous_queue.setEnabled(self.queue_page>0); self.next_queue.setEnabled(self.queue_page<maximum_page)
        self.queue_page_label.setText(f"{start+1:,}–{start+len(tasks):,} of {total_visible:,}" if tasks else "0 of 0")
        self.clear_filters.setEnabled(bool(self.kind_filter.currentData() or self.health_filter.currentData() or self.queue_search.text()))
        # Retain an unfinished answer only while its exact saved questions and
        # knowledge authority are unchanged. A changed source rebuilds the panel.
        if self._selection_snapshot()!=getattr(self,"_displayed_selection_snapshot",None):
            self._shared_selection_ids=()
            self._selection_changed()
        elif self.selected_tasks():
            selected=self.selected_tasks()
            if len(selected)==1:self._show_current_image(selected[0])
            self._request_examples(selected)

    def clear_queue_filters(self):
        self._search_timer.stop()
        controls=(self.kind_filter,self.health_filter,self.queue_search)
        blockers=[QSignalBlocker(control) for control in controls]
        self.kind_filter.setCurrentIndex(0); self.health_filter.setCurrentIndex(0); self.queue_search.clear()
        del blockers
        self.reset_queue_page()

    def _status_changed(self,*_):
        # Queue-quality triage describes active work, not whether a saved
        # decision belongs in history. Do not carry the default pending filter
        # into Completed or Dismissed and silently hide a just-saved answer.
        with QSignalBlocker(self.health_filter):
            default="meaningful" if self.status_filter.currentData()=="pending" else ""
            self.health_filter.setCurrentIndex(max(0,self.health_filter.findData(default)))
        self.reset_queue_page()

    def reset_queue_page(self,*_):
        self.queue_page=0; self.refresh(reload=False)

    def schedule_queue_refresh(self,*_):
        self._search_timer.start()

    def change_queue_page(self,amount):
        self.queue_page=max(0,self.queue_page+int(amount)); self.refresh(reload=False)

    def bulk_dispose_noise(self):
        if getattr(self,"_scan_kind","") or str(self.status_filter.currentData())!="pending":return
        ids=[task.id for task in self.visible_tasks() if task.status=="pending" and task.details.get("queue_health",{}).get("category") not in {"",None,"meaningful"}]
        if not ids: return
        item_word="item" if len(ids)==1 else "items"
        if QMessageBox.question(self,"Dismiss queue noise",f"Dismiss {len(ids):,} matching queue-noise {item_word} across all pages?\n\nMeaningful questions remain pending. Dismissed items remain available under Dismissed Questions. Image data and System Knowledge will not change.")!=QMessageBox.Yes: return
        try:self.tasks.bulk_set_status(ids,"ignored")
        except (OSError,RuntimeError,ValueError) as exc:
            QMessageBox.critical(self,"Could not dismiss questions",f"The questions remain pending. Image data and System Knowledge were not changed.\n\n{exc}"); return
        self._queue_health_counts=None; self.refresh(reload=False); self._emit_review_changed()

    @staticmethod
    def _task_visual_status(task):
        details=task.details if isinstance(task.details,dict) else {}; raw=details.get("confidence")
        if raw is None and isinstance(details.get("suggestion"),dict): raw=details["suggestion"].get("confidence")
        try: confidence=float(raw) if raw is not None else None
        except (TypeError,ValueError): confidence=None
        if confidence is not None and (not math.isfinite(confidence) or not 0<=confidence<=1):confidence=None
        if task.status=="resolved": return "green",confidence
        if task.status=="ignored": return "orange",confidence
        if confidence is not None: return status_from_confidence(confidence),confidence
        if task.priority and task.priority!="normal": return status_from_severity(task.priority),None
        return "yellow",None

    def selected_task(self):
        item=self.questions.currentItem()
        if item is None or not item.isSelected():
            item=next(iter(self.questions.selectedItems()),None)
        return self.tasks.tasks.get(str(item.data(0,Qt.UserRole))) if item else None

    def selected_tasks(self):
        return [self.tasks.tasks.get(str(item.data(0,Qt.UserRole))) for item in self.questions.selectedItems() if self.tasks.tasks.get(str(item.data(0,Qt.UserRole))) is not None]

    def _selection_snapshot(self):
        # Unrelated queue arrivals must not erase the current answer. Actual
        # selected-task changes and changed reusable authority do invalidate it.
        selected=self.selected_tasks()
        for task in selected:
            if task.status=="pending" and task.task_type=="new_tag":self.current_suggestion_for(task)
        token=_review_source_token(self.state)
        knowledge_token=tuple(value for value in token[2] if value[0]!=str(self.tasks.path))
        return (
            deepcopy([task.to_dict() for task in selected]),
            token[0],knowledge_token,
        )

    @staticmethod
    def _compatible_tag_selection(selected) -> bool:
        return len(selected)>1 and all(task.status=="pending" and task.task_type=="new_tag" for task in selected)

    def _selection_changed(self):
        selected=self.selected_tasks(); count=len(selected)
        compatible=self._compatible_tag_selection(selected)
        if count<=1: self.selection_summary.setText("One question selected" if count else "No questions selected")
        elif compatible: self.selection_summary.setText(f"{count:,} compatible exact SDXL Tag questions selected • Ctrl-click or Shift-click to adjust")
        else: self.selection_summary.setText(f"{count:,} questions selected • Batch decisions require pending SDXL Tag questions with the same answer fields")
        if hasattr(self,"related_button"):
            if count>1: self.related_button.hide()
            elif count==1 and selected[0].status=="pending" and selected[0].task_type=="new_tag":
                suggestion=self.current_suggestion_for(selected[0]); related=self.related_pending(selected[0]); confidence=float(suggestion.get("confidence") or 0)*100
                self.related_button.setText(f"Apply to {len(related)+1} Tags…")
                self.related_button.setVisible(bool(related) and confidence>=80 and suggestion.get("category")!="uncategorized")
        self.show_selected()
        self._displayed_selection_snapshot=self._selection_snapshot()

    def show_question_context_menu(self,position):
        item=self.questions.itemAt(position)
        if not item: return
        self.questions.clearSelection(); item.setSelected(True); self.questions.setCurrentItem(item); task=self.selected_task()
        if not task: return
        menu=QMenu(self); show_image=menu.addAction("Show Related Image"); show_image.setEnabled(any(_available_file(path) for path in self._example_paths(task)))
        status_action=menu.addAction(
            DISMISS_QUESTION_LABEL if task.status=="pending" else
            "Edit Saved Tag…" if task.task_type=="new_tag" and task.status=="resolved" else
            "Return to Active Questions"
        )
        if (
            task.task_type=="collection_member" or task.task_type in SOURCE_TASK_TYPES
            or task.task_type in {
                TAG_MEANING_TASK_TYPE,TAG_BROADER_PROPOSAL_TASK_TYPE,
            }
        ) and task.status=="resolved":
            status_action.setEnabled(False)
        copy_question=menu.addAction("Copy Question Text")
        chosen=menu.exec(self.questions.viewport().mapToGlobal(position))
        if chosen is show_image: self.show_image()
        elif chosen is status_action: self.set_status("ignored" if task.status=="pending" else "pending")
        elif chosen is copy_question: QApplication.clipboard().setText(self.friendly_title(task))

    def clear_selected(self):
        self._cancel_current_image_request()
        self._cancel_example_request()
        self._shared_selection_ids=(); self._example_generation+=1; self._example_key=()
        self.current_task_id=""; self.decision_title.hide(); self.suggestion.hide()
        if self.questions.topLevelItemCount():
            self.empty_editor.setTitle("Select a question")
            self.empty_editor.setText("Choose a question on the left to inspect its evidence and available decisions.")
        self.editor.setCurrentWidget(self.empty_editor); self.open_image.setEnabled(False); self.examples_panel.hide(); self.current_image_panel.hide()
        self.current_image_path=""; self.current_image_preview.clear("Choose a question with an image.")

    def current_suggestion_for(self,task):
        if task.task_type!="new_tag": return dict(task.details.get("suggestion") or {})
        suggestion=self.suggestions.suggest(str(task.details.get("tag") or "")).__dict__
        if task.details.get("acknowledged"):
            # Vocabulary intake has already created this stable identity.  Its
            # category is a deliberate System Knowledge decision, so a later
            # suggestion refresh may improve semantic hints but cannot replace
            # that chosen category in the pending meaning question.
            record=self.tags.find(str(task.details.get("tag") or ""))
            saved_category=str(getattr(record,"category","") or "")
            queued_suggestion=task.details.get("suggestion")
            queued_category=(
                str(queued_suggestion.get("category") or "")
                if isinstance(queued_suggestion,dict) else ""
            )
            suggestion["category"]=saved_category or queued_category or "uncategorized"
        # Keep the queued estimate as saved evidence. The current suggestion
        # drives this decision, but selection must not relabel a newly computed
        # value as saved confidence on the next queue repaint.
        return suggestion

    def show_selected(self,*_):
        selected=self.selected_tasks(); task=self.selected_task()
        if not task: self.clear_selected(); return
        self.decision_title.show(); self.suggestion.show()
        if len(selected)>1:
            self.current_image_panel.hide()
            if self._compatible_tag_selection(selected): self.show_shared_tag_selection(selected)
            else: self.show_incompatible_selection(selected)
            return
        self._shared_selection_ids=()
        self.current_task_id=task.id; self.decision_title.setText(self.friendly_title(task))
        self._show_current_image(task)
        self._request_examples((task,))
        self.open_image.setEnabled(bool(task.image_path or self._example_paths(task)))
        if task.status!="pending": self.show_history(task); return
        if task.task_type=="new_tag": self.show_tag(task)
        elif task.task_type=="pattern_group": self.show_group(task)
        elif task.task_type=="collection_member": self.show_collection(task)
        elif task.task_type in SOURCE_TASK_TYPES: self.show_source(task)
        elif task.task_type==TAG_MEANING_TASK_TYPE: self.show_meaning_proposal(task)
        elif task.task_type==TAG_BROADER_PROPOSAL_TASK_TYPE: self.show_broader_proposal(task)
        elif task.task_type=="image_question": self.show_image_question(task)
        elif task.task_type=="merge_suggestion": self.show_merge(task)
        else: self.show_other(task)

    def _set_tag_mode(self,shared: bool,count: int=1,names=()):
        self.tag_spacer.changeSize(0,0,QSizePolicy.Minimum,QSizePolicy.Fixed if shared else QSizePolicy.Expanding)
        self.tag_editor.layout().invalidate()
        self.tag_scope.setVisible(shared)
        self.tag_save_button.setVisible(not shared); self.tag_dismiss_button.setVisible(not shared)
        self.shared_apply_button.setVisible(shared)
        if shared:
            shown=list(names[:6]); suffix=f" • and {len(names)-len(shown):,} more" if len(names)>len(shown) else ""
            self.tag_scope.setText(f"One shared decision for {count:,} exact SDXL Tags\n"+" • ".join(shown)+suffix)
            self.tag_outcomes_group.setTitle(f"What should CollectiGen do with these {count:,} exact SDXL Tags?")
            self.shared_apply_button.setText(f"Preview {count:,} Decisions…")
            self.shared_apply_button.setAccessibleDescription(
                f"Preview and save one answer as {count:,} separate exact SDXL Tag decisions. No semantic pattern or image-data change is created."
            )
        else:
            self.tag_outcomes_group.setTitle("What should CollectiGen do with this SDXL Tag?")

    def show_shared_tag_selection(self,selected):
        ids=tuple(task.id for task in selected); names=tuple(str(task.details.get("tag") or "") for task in selected)
        entering=not self._shared_selection_ids
        self._shared_selection_ids=ids; self.current_task_id=""
        self.decision_title.setText(f"Shared decision for {len(selected):,} SDXL Tags")
        self.suggestion.setText(
            "Choose one answer for the complete compatible selection. CollectiGen will save a separate exact decision for every named tag; it will not infer a broader pattern."
        )
        self._set_tag_mode(True,len(selected),names)
        if entering:
            suggestions=[self.current_suggestion_for(task) for task in selected]
            categories={str(item.get("category") or "uncategorized") for item in suggestions}
            category_id=next(iter(categories)) if len(categories)==1 else "uncategorized"
            self.current_category_id=category_id
            self.category.setCurrentIndex(max(0,self.category.findData(category_id)))
            for radio in self.outcomes.values(): radio.setChecked(False)
            self.outcomes["keep"].setChecked(True); self.existing.setCurrentIndex(-1); self.existing.clearEditText(); self.update_tag_controls()
        self.editor.setCurrentWidget(self.tag_editor); self._request_examples(tuple(selected))

    def show_incompatible_selection(self,selected):
        self._shared_selection_ids=(); self._example_generation+=1; self._example_key=()
        self.decision_title.setText(f"{len(selected):,} questions need individual decisions")
        self.suggestion.setText("The selected questions do not share the same exact-tag answer fields, so no subset will be changed.")
        self.incompatible_message.setText(
            "Select only pending SDXL Tag meaning questions to make one shared decision.\n\n"
            "Image questions, cleanup suggestions, dismissed questions, and completed history remain individual decisions."
        )
        self.editor.setCurrentWidget(self.incompatible_editor); self.examples_panel.hide(); self.open_image.setEnabled(False)

    def show_tag(self,task):
        self._set_tag_mode(False)
        tag=str(task.details.get("tag") or ""); suggestion=self.current_suggestion_for(task); self.current_suggestion=suggestion
        confidence=float(suggestion.get("confidence") or 0)*100; category_id=str(suggestion.get("category") or "uncategorized")
        label=self.categories.path_label(category_id); reason=str(suggestion.get("reason") or "CollectiGen does not have enough reusable knowledge yet.")
        current_line=(
            f"Current local suggestion: {label}  •  {confidence:.0f}% confidence"
            if confidence>0 else "No current local meaning suggestion"
        )
        lines=[current_line,reason]
        queued=task.details.get("suggestion")
        if isinstance(queued,dict):
            try: saved_confidence=float(queued.get("confidence"))
            except (TypeError,ValueError): saved_confidence=None
            if saved_confidence is not None and math.isfinite(saved_confidence) and 0<=saved_confidence<=1:
                saved_category=str(queued.get("category") or "uncategorized")
                if abs(saved_confidence*100-confidence)>=.5 or saved_category!=category_id:
                    lines.append(
                        f"Saved queue estimate: {self.categories.path_label(saved_category)}  •  "
                        f"{saved_confidence:.0%} confidence. Check the meaning before saving."
                    )
        self.suggestion.setText("\n".join(lines))
        self.current_category_id=category_id; index=self.category.findData(category_id); self.category.setCurrentIndex(max(0,index))
        concept=suggestion.get("concept_id"); properties=suggestion.get("properties") or {}
        bits=[]
        if concept: bits.append(f"Recognized subject: {concept}")
        if properties: bits.append("Details: "+", ".join(f"{key} = {value}" for key,value in properties.items()))
        if suggestion.get("parent_id"): bits.append(f"More general SDXL Tag: {suggestion['parent_id']}")
        elif suggestion.get("generic_tag"): bits.append(f"Possible missing generic SDXL Tag: {suggestion['generic_tag']} (review before adding)")
        self.meaning.setText(". ".join(bits) if bits else "No additional structured meaning is required. You can still complete this decision by confirming the category.")
        self.existing.setCurrentIndex(-1); self.existing.clearEditText()
        for radio in self.outcomes.values(): radio.setChecked(False)
        self.outcomes["keep"].setChecked(True); self.update_tag_controls()
        related=self.related_pending(task); self.related_button.setText(f"Apply to {len(related)+1} Tags…"); self.related_button.setVisible(bool(related) and confidence>=80 and category_id!="uncategorized")
        self.editor.setCurrentWidget(self.tag_editor)

    def show_group(self,task):
        suggestion=dict(task.details.get("suggestion") or {}); category_id=str(suggestion.get("category") or "uncategorized")
        confidence=float(suggestion.get("confidence") or 0)*100
        self.suggestion.setText(f"Suggested organization: {self.categories.path_label(category_id)}  •  {confidence:.0f}% confidence\n{suggestion.get('reason') or ''}")
        self.group_tags.clear()
        for tag in task.details.get("tags",[]):
            item=QListWidgetItem(str(tag)); item.setFlags(item.flags()|Qt.ItemIsUserCheckable); item.setCheckState(Qt.Checked); self.group_tags.addItem(item)
        self.group_category.setCurrentIndex(max(0,self.group_category.findData(category_id))); self.editor.setCurrentWidget(self.group_editor)

    def show_image_question(self,task):
        details=dict(task.details); action=str(details.get("action") or "")
        actionable=action in {
            "create_sidecar","add_sdxl_tags","add_plain_language",
            "assign_character","replace_character","classify_visual_style",
        }
        self.suggestion.setText(
            "Confirm the proposed image-specific change. Nothing is written "
            "unless you choose Apply."
            if actionable else
            "Open Image Data to inspect saved evidence and correct the image facts. "
            "This question does not confirm any facts automatically."
        )
        self.image_question_text.setText(str(details.get("question") or task.description or task.title))
        source=str(details.get("source") or "CollectiGen analysis")
        _,confidence=self._task_visual_status(task)
        confidence_text=f"{confidence:.0%} evidence confidence" if confidence is not None else "No evidence confidence estimate"
        cause=str(details.get("derived_cause") or "").strip()
        self.image_question_source.setText(
            f"Evidence source: {source}  •  {confidence_text}"
            +(f"\nCurrent because: {cause}" if cause else "")
        )
        self.image_question_values.clear()
        for value in self._image_question_value_labels(details):
            self.image_question_values.addItem(value)
        self.image_question_values.setVisible(self.image_question_values.count()>0)
        self.image_question_apply.setVisible(actionable)
        self.image_question_reject.setVisible(actionable)
        self.image_question_edit.setVisible(not actionable)
        self.image_question_edit.setEnabled(bool(task.image_path) and _available_file(task.image_path))
        self.image_question_open_dataset.setEnabled(bool(task.image_path))
        self.style_fields.setVisible(action=="classify_visual_style")
        if action=="classify_visual_style":
            self.characters.load(); self.style_series.clear(); self.style_series.addItem("Not specified","")
            for series in sorted(self.characters.series.values(),key=lambda item:item.name.casefold()):
                self.style_series.addItem(
                    series.name,series.id,search_terms=series.aliases,
                )
            values=[str(value) for value in details.get("values",[])]
            suggested=self.characters.find_series(values[1]) if len(values)>1 and values[1] else None
            self.style_series.setCurrentIndex(max(0,self.style_series.findData(suggested.id if suggested else "")))
            self.style_relationship.setCurrentIndex(0); self.update_style_controls()
        self.editor.setCurrentWidget(self.image_question_editor)

    def _image_question_value_labels(self,details):
        """Present equivalent stored IDs and display names as one evidence line."""
        labels=[]; seen=set()
        for raw in details.get("values",()):
            value=str(raw or "").strip()
            if not value:
                continue
            record=self.tags.find(value)
            label=str(record.display if record is not None else value).strip()
            key=normalize_tag(label) or label.casefold()
            if key in seen:
                continue
            seen.add(key); labels.append(label)
        return tuple(labels)

    def show_collection(self,task):
        details=dict(task.details)
        owner_kind=str(details.get("owner_kind") or "collection").title()
        owner_label=str(details.get("owner_label") or details.get("owner_id") or "Unknown")
        target=str(details.get("target_tag") or details.get("target_tag_id") or "Unknown")
        category=str(details.get("target_category_path") or details.get("target_category_id") or "Unknown")
        field=str(details.get("collection_field") or "collection").replace("_"," ")
        observations=int(details.get("observation_count") or 0)
        samples=int(details.get("sample_count") or 0)
        baseline_count=int(details.get("baseline_count") or 0)
        baseline_samples=int(details.get("baseline_sample_count") or 0)
        rate=float(details.get("rate") or 0)
        baseline_rate=float(details.get("baseline_rate") or 0)
        lift=float(details.get("lift") or 0)
        confidence=float(details.get("confidence") or 0)
        sources=[str(value) for value in details.get("source_scope",()) if str(value)]
        self.suggestion.setText(
            "Current indexed confirmed Image Knowledge repeatedly pairs this "
            f"exact Tag with the reviewed {owner_kind.lower()} representative. "
            "This is ranked evidence for review, not automatic collection truth."
        )
        self.collection_owner.setText(f"{owner_kind}: {owner_label}  •  saved field: {field}")
        self.collection_target.setText(f"{target}  •  {category}")
        self.collection_metrics.setText(
            f"Observed {observations:,} of {samples:,} scoped examples ({rate:.0%})  •  "
            f"baseline {baseline_count:,} of {baseline_samples:,} ({baseline_rate:.0%})\n"
            f"Lift {lift:.2f}×  •  conservative confidence {confidence:.0%}  •  "
            f"rank {int(details.get('rank') or 0):,} within this collection"
        )
        self.collection_sources.setText(", ".join(sources) if sources else "Current unnamed local index scope")
        self.editor.setCurrentWidget(self.collection_editor)

    def show_source(self,task):
        details=dict(task.details)
        character=str(details.get("character_label") or "Unknown Character")
        character_tag=str(details.get("character_tag") or "Unknown Character Identity Tag")
        source=str(details.get("source_label") or "Unknown Character Source")
        source_tag=str(details.get("source_tag") or "Unknown Character Source Tag")
        action=str(details.get("source_action") or "")
        current=str(details.get("current_source_label") or "No reviewed Character Source")
        observations=int(details.get("observation_count") or 0)
        sources=[str(value) for value in details.get("source_scope",()) if str(value)]
        dataset=getattr(self.state,"dataset",None)
        named=[]
        for library_id in sources:
            profile=(dataset.profile(library_id) if callable(getattr(dataset,"profile",None)) else None)
            named.append(str(getattr(profile,"name","") or library_id))
        self.source_character.setText(f"{character}  •  SDXL Tag: {character_tag}")
        self.source_target.setText(f"{source}  •  SDXL Tag: {source_tag}")
        if action=="link":
            self.source_change.setText(
                f"Change Character Source from {current} to {source}."
            )
            self.source_accept.setText("Save Character Source")
            self.source_accept.setEnabled(True)
            self.source_accept.setToolTip("")
        else:
            alias=str(details.get("alias") or "")
            self.source_change.setText(
                f"Add “{alias}” as a canonical reviewed Other Name for {character_tag}."
            )
            self.source_accept.setText("Add Reviewed Other Name")
            current_source_id=self.characters.character_source_id(
                "character",str(details.get("character_id") or ""),
            )
            ready=current_source_id==str(details.get("source_id") or "")
            self.source_accept.setEnabled(ready)
            self.source_accept.setToolTip(
                "" if ready else
                "Review and save the matching Character Source question first."
            )
        self.source_evidence.setText(
            f"{observations:,} indexed image{'s' if observations!=1 else ''} "
            "confirm this exact Character Identity and Character Source Tag pair."
        )
        self.source_scope.setText(
            ", ".join(named) if named else "Current unnamed local index scope"
        )
        self.suggestion.setText(
            "Only exact reviewed Character Identity and Character Source stable "
            "records contributed. Co-occurrence ranks this proposal but does not "
            "save the relationship or Other Name without review."
        )
        self.editor.setCurrentWidget(self.source_editor)

    def edit_current_image_data(self):
        task=self.selected_task()
        if task and task.image_path:
            self.edit_image_data.emit(task.image_path)

    def show_meaning_proposal(self,task):
        variants=[
            dict(item) for item in task.details.get("variants",())
            if isinstance(item,dict) and str(item.get("fingerprint") or "")
        ]
        previous=str(self.meaning_variant.currentData() or "")
        self.meaning_variant.blockSignals(True)
        try:
            self.meaning_variant.clear()
            for index,variant in enumerate(variants,1):
                sources=[
                    str(item.get("source_label") or "Attributed source")
                    for item in variant.get("sources",()) if isinstance(item,dict)
                ]
                label=(
                    f"Proposal {index} • {variant.get('freshness') or 'unavailable'} • "
                    +(", ".join(dict.fromkeys(sources)) or "Attributed source")
                )
                self.meaning_variant.addItem(label,str(variant["fingerprint"]))
            selected=self.meaning_variant.findData(previous)
            self.meaning_variant.setCurrentIndex(selected if selected>=0 else (0 if variants else -1))
        finally:
            self.meaning_variant.blockSignals(False)
        current=str(task.details.get("current_summary") or "")
        self.meaning_current.setPlainText(current or "No reviewed Meaning & Usage summary is saved yet.")
        count=len(variants)
        self.suggestion.setText(
            f"{count:,} distinct attributed proposal{'s' if count!=1 else ''} remain side by side. "
            "Source text, community frequency, cache state, and confidence rank this review only; none is confirmed automatically."
        )
        self.meaning_accept.setEnabled(bool(variants))
        self._show_meaning_variant()
        self.editor.setCurrentWidget(self.meaning_editor)

    def _show_meaning_variant(self,*_):
        task=self.selected_task()
        fingerprint=str(self.meaning_variant.currentData() or "")
        variants=(task.details.get("variants",()) if task is not None else ())
        variant=next((
            item for item in variants
            if isinstance(item,dict) and str(item.get("fingerprint") or "")==fingerprint
        ),{})
        definition=str(variant.get("definition") or "")
        guidance=str(variant.get("community_guidance") or "")
        self.meaning_proposed.setPlainText(
            "Definition: "+definition
            +(("\n\nCommunity usage: "+guidance) if guidance else "")
        )
        lines=[]
        for source in variant.get("sources",()):
            if not isinstance(source,dict):continue
            identity=str(source.get("source_label") or "Attributed source")
            version=str(source.get("source_version") or "")
            page=str(source.get("source_page_id") or "")
            url=str(source.get("source_url") or "")
            origin=" • ".join(value for value in (identity,version,page,url) if value)
            lines.append(
                f"{origin}\nRetrieved: {source.get('retrieved_at') or 'unknown'} • "
                f"Freshness: {source.get('freshness') or 'unavailable'} • "
                f"Ranking confidence: {float(source.get('confidence') or 0):.0%}"
            )
        omitted=max(0,int(variant.get("sources_omitted") or 0))
        if omitted:
            lines.append(
                f"{omitted:,} additional supporting observation{'s' if omitted!=1 else ''} "
                "remain represented by the exact evidence-set fingerprint in the bounded local inbox."
            )
        self.meaning_sources.setPlainText("\n\n".join(lines) or "No complete source attribution is available.")

    def apply_meaning_decision(self,decision):
        task=self.selected_task()
        if not task or task.task_type!=TAG_MEANING_TASK_TYPE:return
        fingerprint=str(self.meaning_variant.currentData() or "") if decision=="accepted" else ""
        try:
            result=self.meaning_reviews.decide(
                task.id,decision,variant_fingerprint=fingerprint,
            )
        except (OSError,RuntimeError,ValueError) as error:
            self.tags.load(); self.tasks.load(); self.refresh(reload=False)
            QMessageBox.critical(self,"Meaning & Usage was not saved",str(error)); return
        self._queue_health_counts=None; self._emit_changed(); self.refresh(reload=False)
        if decision=="accepted":
            self.header.set_status(
                "Saved one reviewed Meaning & Usage summary • related attributed "
                f"questions reranked: {result.related_pending_reranked:,} • image data unchanged"
            )
            self.header.status.setToolTip(f"Recovery files: {result.backup_directory}")
        else:
            self.header.set_status(
                "Rejected one exact attributed evidence set • System and Image Knowledge unchanged"
            )

    def show_broader_proposal(self,task):
        details=task.details if isinstance(task.details,dict) else {}
        tag=str(details.get("tag") or "This SDXL Tag")
        target=str(details.get("target") or "the proposed target")
        boundary=str(details.get("review_boundary") or "ready")
        source_category=str(
            details.get("category_path") or details.get("category_id") or "Unknown"
        )
        proposed_category=str(
            details.get("proposed_category_path")
            or details.get("proposed_category_id") or "Unknown"
        )
        self.broader_current.setText(
            "No active canonical Broader Tag is saved for this exact SDXL Tag."
        )
        self.broader_proposed.setText(f"{tag} → {target}")
        self.broader_source_category.setText(
            f"{source_category} → {proposed_category} (proposed)"
            if boundary=="category_change_required" else source_category
        )
        self.broader_target_category.setText(
            str(details.get("target_category_path") or details.get("target_category_id") or "Unknown")
        )
        effects=[
            str(value) for value in details.get("downstream_effects",())
            if str(value)
        ]
        evidence=(
            f"Pattern: {details.get('pattern_name') or details.get('pattern_id') or 'Unknown'}\n"
            f"Pattern source: {str(details.get('pattern_source') or 'unknown').title()}\n"
            f"Ranking confidence: {float(details.get('confidence') or 0):.0%}\n"
            f"Evidence: {details.get('basis') or 'No explanation is available.'}"
        )
        if boundary=="category_change_required":
            evidence+=(
                "\nReview boundary: the Tag Category must move from "
                f"{source_category} to {proposed_category} in the same verified transaction."
            )
            self.broader_accept.setText("Save Category and Broader Tag…")
            self.broader_accept.setAccessibleDescription(
                "Confirm and atomically save the proposed Tag Category and canonical Broader Tag with stale-state and read-back verification."
            )
        elif boundary=="broader_target_outside_category_path":
            evidence+=(
                "\nReview boundary: the proposed target remains outside the source Tag's current Category path; approval does not move the source Tag."
            )
            self.broader_accept.setText("Save Cross-Category Broader Tag…")
            self.broader_accept.setAccessibleDescription(
                "Confirm and save this exact cross-Category canonical Broader Tag without moving the source Tag."
            )
        else:
            self.broader_accept.setText("Save Broader Tag")
            self.broader_accept.setAccessibleDescription(
                "Save this exact canonical Broader Tag through the stale-safe relationship writer."
            )
        if effects:
            evidence+="\n\nAfter approval:\n• "+"\n• ".join(effects)
        self.broader_evidence.setPlainText(evidence)
        self.suggestion.setText(
            "One current reusable pattern supports this exact source and target. Confidence ranks the question; only your approval creates semantic authority."
        )
        self.editor.setCurrentWidget(self.broader_editor)

    def apply_broader_decision(self,decision):
        task=self.selected_task()
        if not task or task.task_type!=TAG_BROADER_PROPOSAL_TASK_TYPE:return
        details=task.details if isinstance(task.details,dict) else {}
        boundary=str(details.get("review_boundary") or "ready")
        if decision=="accepted" and boundary!="ready":
            tag=str(details.get("tag") or "this SDXL Tag")
            target=str(details.get("target") or "the proposed target")
            source_category=str(
                details.get("category_path") or details.get("category_id") or "Unknown"
            )
            target_category=str(
                details.get("target_category_path")
                or details.get("target_category_id") or "Unknown"
            )
            if boundary=="category_change_required":
                proposed_category=str(
                    details.get("proposed_category_path")
                    or details.get("proposed_category_id") or "Unknown"
                )
                title="Save Category and Broader Tag"
                message=(
                    f"Save “{tag}” → “{target}” and move the Tag Category from "
                    f"{source_category} to {proposed_category}?\n\n"
                    "Both changes use one stale-guarded transaction with backups and read-back verification. Existing reviewed Tag ordering is preserved; unreviewed ordering follows the proposed Category.\n\n"
                    "Image Knowledge, captions, TXT, Metadata, and pixels remain unchanged."
                )
            else:
                title="Save Cross-Category Broader Tag"
                message=(
                    f"Save “{tag}” → “{target}” even though the target Category "
                    f"({target_category}) is outside the source path ({source_category})?\n\n"
                    "Only the canonical Broader Tag changes. The source Tag stays in its current Category, and the save remains stale-guarded with backups and read-back verification.\n\n"
                    "Image Knowledge, captions, TXT, Metadata, and pixels remain unchanged."
                )
            if QMessageBox.question(
                self,title,message,
                QMessageBox.Yes|QMessageBox.No,QMessageBox.No,
            )!=QMessageBox.Yes:
                return
        try:
            result=self.broader_reviews.decide(task.id,decision)
        except (OSError,RuntimeError,ValueError) as error:
            self.tags.load(); self.categories.load(); self.tasks.load(); self.refresh(reload=False)
            QMessageBox.critical(self,"Broader Tag was not saved",str(error)); return
        self._queue_health_counts=None; self._emit_changed(); self.refresh(reload=False)
        if decision=="accepted":
            effect=(
                "Saved one reviewed Category move and canonical Broader Tag"
                if boundary=="category_change_required"
                else "Saved one reviewed cross-Category canonical Broader Tag"
                if boundary=="broader_target_outside_category_path"
                else "Saved one reviewed canonical Broader Tag"
            )
            self.header.set_status(
                f"{effect} • related ordinary questions reranked: "
                f"{result.related_pending_reranked:,} • image data unchanged"
            )
            self.header.status.setToolTip(f"Recovery files: {result.backup_directory}")
        else:
            self.header.set_status(
                "Rejected one exact Broader Tag proposal • System and Image Knowledge unchanged"
            )

    def show_other(self,task):
        self.suggestion.setText(
            "Review this question and open it in Dataset if you need image context or detailed editing. "
            "Complete Question closes only this queue item; it does not mark the image reviewed."
        )
        values=[task.description]
        for key,value in task.details.items():
            if key=="queue_health":
                reason=str(value.get("reason") or "").strip() if isinstance(value,dict) else ""
                if reason and reason!="Needs a human decision":
                    values.append(f"Queue note: {reason}")
                continue
            if key=="related_rerank":
                continue
            values.append(f"{key.replace('_',' ').title()}: {value}")
        self.other_details.setPlainText("\n\n".join(filter(None,values)))
        self.other_done.setVisible(task.task_type!="image_question")
        self.editor.setCurrentWidget(self.other_editor)

    def _merge_candidate(self,task):
        image=Path(task.image_path)
        if not self._image_ready_for_write(image): return None,None
        rule=self.tags.merge_rules.get(str(task.details.get("rule_id") or ""))
        if rule is None or not rule.enabled: return ImageRecord(image),None
        record=ImageRecord(image)
        location=str(task.details.get("location") or "")
        candidate=next((item for item in find_suggestions(record,[rule]) if item.location==location),None)
        return record,candidate

    def show_merge(self,task):
        self.suggestion.setText(
            "This older question previews a concise training projection. "
            "The complete SDXL Tags remain valid Image Knowledge and will not be removed. "
            "Complete Question saves only this queue decision; it does not change the image or caption. "
            "For current training work, review exact captions with the selected profile in Training."
        )
        self.merge_rule.clear()
        try: _,candidate=self._merge_candidate(task)
        except Exception as exc:
            candidate=None; self.merge_rule.setText(f"Could not read the current image data: {exc}")
        self.merge_location.setText(_merge_location_label(task.details.get("location")))
        if candidate:
            self.merge_before.setText(", ".join(candidate.before_tags) or "No SDXL Tags")
            self.merge_after.setText(", ".join(candidate.preview_tags()) or "No SDXL Tags")
            description=candidate.rule.description.strip()
            self.merge_rule.setText(f"Project {', '.join(candidate.source_tags)} as {candidate.target_tag} for concise training output."+(f"\n{description}" if description else ""))
        else:
            self.merge_before.setText("This rule no longer matches the current image data.")
            self.merge_after.setText("No change is needed.")
            if not self.merge_rule.text().startswith("Could not"):
                self.merge_rule.setText("The rule was removed, disabled, or the image was already corrected.")
        self.editor.setCurrentWidget(self.merge_editor)

    def show_history(self,task):
        resolution=task.details.get("resolution") or {}; status="Completed" if task.status=="resolved" else "Dismissed from the active queue"
        lines=[status,self.friendly_title(task),f"Last changed: {task.updated_at}"]
        saved_tag=task.task_type=="new_tag" and task.status=="resolved"
        if saved_tag and resolution:
            decision=resolution.get("decision")
            if decision=="use_existing":
                lines.extend(("",f"Saved decision: Use {resolution.get('target') or 'an existing Tag'} as an Other Name for this exact Tag."))
            elif decision=="excluded_from_training_captions":
                lines.extend(("",f"Saved decision: Keep this Tag in System Knowledge, but exclude it from training captions. Category: {self.categories.path_label(str(resolution.get('category') or 'uncategorized'))}."))
            elif decision=="kept_as_knowledge":
                lines.extend(("",f"Saved decision: Keep this reusable Tag in {self.categories.path_label(str(resolution.get('category') or 'uncategorized'))}."))
            elif decision=="already_recognized":
                lines.extend(("",f"Completed because this exact Tag is already recognized as {resolution.get('display') or 'a saved Tag'}."))
            else:
                lines.extend(("","Saved decision: This exact Tag question was completed."))
        elif task.task_type=="merge_suggestion" and resolution:
            decision=resolution.get("decision")
            if decision=="projection_only_reviewed":
                lines.extend((
                    "", "Saved decision: Completed this legacy training-projection question.",
                    "No image facts, training caption, or rule were changed by this decision.",
                ))
                location=str(resolution.get("location") or "").strip()
                if location:
                    lines.append(f"Tag location: {_merge_location_label(location)}")
                before=resolution.get("before")
                after=resolution.get("after")
                if isinstance(before,list) and all(isinstance(item,str) for item in before):
                    lines.append("Image Knowledge Tags: "+(", ".join(before) or "None"))
                if isinstance(after,list) and all(isinstance(item,str) for item in after):
                    lines.append("Reviewed projection preview: "+(", ".join(after) or "None"))
            elif decision=="complete_knowledge_retained":
                lines.extend((
                    "", "Saved decision: Rejected the concise projection suggestion.",
                    "All separately meaningful Image Knowledge Tags were retained; no caption or rule changed.",
                ))
            elif decision=="rule_disabled":
                lines.extend((
                    "", "Saved decision: Disabled this cleanup rule and closed its pending suggestions.",
                    "Existing image facts and captions were not changed. Manage the rule in Rules & Automation.",
                ))
            elif decision=="no_longer_applicable":
                lines.extend((
                    "", "Saved decision: This projection rule no longer applied when checked.",
                    "No image facts, caption, or rule changed.",
                ))
            else:
                lines.extend(("", "Saved decision: This legacy projection question was completed."))
            rule_id=str(resolution.get("rule_id") or "").strip()
            if rule_id:lines.append(f"Recorded rule ID: {rule_id}")
        elif resolution:
            lines.extend(("", "Saved decision:")); lines.extend(f"• {key.replace('_',' ').title()}: {value}" for key,value in resolution.items())
        elif task.status=="ignored": lines.extend(("","No final decision was made."))
        else: lines.extend(("","This item was marked complete."))
        self.history_edit_tag.setVisible(saved_tag)
        if saved_tag:
            lines.extend(("", "To correct the saved meaning, open this exact Tag in Tag Encyclopedia. The completed decision remains in history."))
        can_reopen=not (saved_tag or resolution.get("decision")=="rule_disabled" or
            (
                task.task_type=="collection_member"
                or task.task_type in SOURCE_TASK_TYPES
                or task.task_type in {
                    TAG_MEANING_TASK_TYPE,TAG_BROADER_PROPOSAL_TASK_TYPE,
                }
            ) and task.status=="resolved"
        )
        self.history_reopen.setVisible(can_reopen)
        if can_reopen:
            lines.extend((
                "", "Reopen returns this question to the active queue. It does not undo a saved change; correct saved facts in their owning workspace.",
            ))
        self.suggestion.setText("This question is no longer in the active review list."); self.history.setPlainText("\n".join(lines)); self.editor.setCurrentWidget(self.history_editor)

    def update_tag_controls(self,*_):
        choice=self.selected_outcome(); self.tag_fields.setVisible(choice in {"keep","exclude"}); self.existing_fields.setVisible(choice=="existing")
        if choice=="existing": self._populate_existing_names()

    def _populate_existing_names(self):
        names=tuple(sorted((
            (
                record.id,
                record.display+(" — Other Names: "+", ".join(aliases) if aliases else ""),
            )
            for record in self.tags.tags.values()
            for aliases in (self.tags.effective_aliases(record),)
        ),key=lambda item:item[1].casefold()))
        if names!=self._existing_names:
            selected_id=str(self.existing.currentData() or ""); self.existing.clear()
            for record_id,label in names:self.existing.addItem(label,record_id)
            if selected_id:self.existing.setCurrentIndex(max(0,self.existing.findData(selected_id)))
            self._existing_names=names
        if self.existing.currentIndex()<0 and names:
            self.existing.setCurrentIndex(0)

    def selected_existing_tag(self):
        record=self.tags.tags.get(str(self.existing.currentData() or ""))
        label=self.existing.currentText()
        for separator in (" — Other Names:"," — aliases:"):
            label=label.split(separator,1)[0]
        return record or self.tags.find(label)

    def selected_outcome(self):
        return next((value for value,button in self.outcomes.items() if button.isChecked()),"keep")

    def save_current(self):
        selected=self.selected_tasks()
        if self._compatible_tag_selection(selected):
            self.apply_selected_tag_decision(); return
        task=self.selected_task()
        if not task or task.status!="pending": return
        if task.task_type=="new_tag": self.save_tag()
        elif task.task_type=="pattern_group": self.save_group()
        elif task.task_type=="collection_member": self.apply_collection_decision("accepted")
        elif task.task_type in SOURCE_TASK_TYPES: self.apply_source_decision("accepted")
        elif task.task_type==TAG_MEANING_TASK_TYPE: self.apply_meaning_decision("accepted")
        elif task.task_type==TAG_BROADER_PROPOSAL_TASK_TYPE: self.apply_broader_decision("accepted")
        elif task.task_type=="image_question": self.apply_image_question()
        elif task.task_type=="merge_suggestion": self.apply_merge()

    def _show_collection_questions(self,select_id: str=""):
        controls=(self.status_filter,self.kind_filter,self.health_filter,self.queue_search)
        for control in controls: control.blockSignals(True)
        try:
            self.status_filter.setCurrentIndex(max(0,self.status_filter.findData("pending")))
            kind_index=self.kind_filter.findText("Character collections")
            if kind_index>=0: self.kind_filter.setCurrentIndex(kind_index)
            health_index=self.health_filter.findData("meaningful")
            if health_index>=0: self.health_filter.setCurrentIndex(health_index)
            self.queue_search.clear()
        finally:
            for control in controls: control.blockSignals(False)
        self.queue_page=0; self._queue_health_counts=None
        self.refresh(reload=False,select_id=select_id)

    def _show_source_questions(self,select_id: str=""):
        controls=(self.status_filter,self.kind_filter,self.health_filter,self.queue_search)
        for control in controls: control.blockSignals(True)
        try:
            self.status_filter.setCurrentIndex(max(0,self.status_filter.findData("pending")))
            kind_index=self.kind_filter.findText("Character sources and aliases")
            if kind_index>=0: self.kind_filter.setCurrentIndex(kind_index)
            health_index=self.health_filter.findData("meaningful")
            if health_index>=0: self.health_filter.setCurrentIndex(health_index)
            self.queue_search.clear()
        finally:
            for control in controls: control.blockSignals(False)
        self.queue_page=0; self._queue_health_counts=None
        self.refresh(reload=False,select_id=select_id)

    def _collection_discovery_work(self,progress,should_cancel):
        dataset=getattr(self.state,"dataset",None)
        index=getattr(dataset,"index",None)
        snapshot_provider=getattr(index,"evidence_snapshot_token",None)
        if index is None or not callable(getattr(index,"all_records",None)) or not callable(snapshot_provider):
            raise RuntimeError(
                "The current named-library Image Knowledge index is unavailable. "
                "Refresh the included libraries, then try again."
            )
        before=snapshot_provider()
        records=index.all_records()
        after=snapshot_provider()
        if before!=after:
            raise RuntimeError(
                "Indexed Image Knowledge changed while collection discovery was "
                "starting. Run Find New Knowledge again."
            )
        library_ids={}
        identity_for_path=getattr(dataset,"identity_for_path",None)
        if callable(identity_for_path):
            for row in records:
                if should_cancel():
                    raise InterruptedError("Character collection discovery cancelled")
                identity=identity_for_path(row.path)
                library_id=str(getattr(identity,"library_id","") or "")
                if library_id: library_ids[str(row.path)]=library_id
        service=CharacterCollectionDiscoveryService(
            self.tags,self.categories,self.characters,self.tasks,
        )
        return service.discover(
            records,snapshot_token=after,library_ids_by_path=library_ids,
            progress=progress,should_cancel=should_cancel,
        )

    def discover_character_collections(self):
        if self._scan_task is not None:
            show_operation_in_progress(self,"Review Center work")
            return
        index=getattr(getattr(self.state,"dataset",None),"index",None)
        if index is None or not callable(getattr(index,"all_records",None)):
            QMessageBox.information(
                self,"Character collection discovery unavailable",
                "Refresh at least one included named library before checking "
                "current indexed Image Knowledge. Nothing was changed.",
            )
            return
        self.tags.load(); self.categories.load(); self.characters.load(); self.tasks.load()
        if not self.set_scan_busy(True,"collection_preview"): return
        self.header.set_status(
            "Checking current indexed confirmed Image Knowledge for Character "
            "and outfit collection candidates…"
        )
        worker=CancelableProgressCallTask(self._collection_discovery_work)
        self._scan_task=worker
        worker.signals.progress.connect(self.collection_discovery_progress)
        worker.signals.complete.connect(self.collection_discovery_complete)
        worker.signals.cancelled.connect(self.collection_discovery_cancelled)
        worker.signals.failed.connect(self.collection_discovery_failed)
        self.pool.start(worker)

    @staticmethod
    def collection_preview_content(report):
        summary=(
            f"{report.candidate_count:,} review candidate(s) across "
            f"{report.family_count:,} Character/outfit collection(s).\n\n"
            f"Indexed records checked: {report.records_scanned:,}\n"
            f"Character-only samples: {report.character_samples:,}\n"
            f"Outfit-specific samples: {report.outfit_samples:,}\n"
            f"Ambiguous owner records kept out: {report.ambiguous_owner_records:,}\n"
            f"Existing members kept out: {report.existing_members_excluded:,}\n"
            f"Durable rejections/in-flight decisions kept out: {report.durable_decisions_excluded:,}\n"
            f"Invalid representative links kept out: {report.invalid_representatives_excluded:,}\n\n"
            "Creating questions changes only the saved Review queue. Every "
            "candidate still requires an exact accept or reject decision."
        )
        details="\n".join(
            f"• {item.owner_label} [{item.owner_kind}] — {item.target_tag}: "
            f"{item.observation_count:,}/{item.sample_count:,}, "
            f"{item.rate:.0%}, {item.lift:.2f}× lift, "
            f"{item.confidence:.0%} confidence"
            for item in report.candidates[:100]
        )
        if report.candidate_count>100:
            details+=f"\n• …and {report.candidate_count-100:,} more"
        return summary,details

    def collection_discovery_progress(self,index,total,name):
        update=getattr(self.state,"update_long_operation",None)
        if callable(update): update("review",index,total,name)
        set_progress_bar(self.scan_progress,index,total,detail=name)
        self.scan_progress_text.setText(f"{index:,} of {total:,}: {name}")

    def collection_discovery_complete(self,report):
        self._scan_task=None; self.set_scan_busy(False)
        if not report.candidates:
            self.header.set_status(
                "Character collection check complete • no new review candidates"
            )
            QMessageBox.information(
                self,"No new collection candidates",
                "Current indexed confirmed Image Knowledge did not meet the "
                "conservative count, rate, lift, and confidence thresholds for "
                "a new Character or outfit collection question. Nothing was changed.",
            )
            return
        summary,details=self.collection_preview_content(report)
        box=QMessageBox(QMessageBox.Question,"Create collection Review questions?",summary,parent=self)
        box.setDetailedText(details)
        confirm=box.addButton(
            f"Create {report.candidate_count:,} Review Question"
            f"{'s' if report.candidate_count!=1 else ''}",QMessageBox.AcceptRole,
        )
        box.addButton("Cancel",QMessageBox.RejectRole); box.setDefaultButton(confirm); box.exec()
        if box.clickedButton() is not confirm:
            self.header.set_status("Collection discovery preview closed • nothing changed")
            return
        try:
            result=CharacterCollectionDiscoveryService.queue_candidates(
                report,self.tasks,
                current_snapshot_token=self._collection_snapshot_token(),
            )
        except (OSError,RuntimeError,ValueError) as exc:
            QMessageBox.warning(
                self,"Collection questions were not created",
                f"{exc}\n\nNo Character Knowledge or Image Knowledge was changed.",
            )
            return
        select_id=result.task_ids[0] if result.task_ids else ""
        self._show_collection_questions(select_id)
        self.state.events.publish("reviews_changed"); self.data_changed.emit()
        self.header.set_status(
            f"Character collection questions current • {result.queued:,} new • "
            f"{result.updated:,} refreshed • {result.skipped_decisions:,} prior decisions preserved"
        )

    def collection_discovery_cancelled(self):
        self._scan_task=None; self.set_scan_busy(False)
        self.header.set_status("Character collection check cancelled • nothing changed")

    def collection_discovery_failed(self,message):
        self._scan_task=None; self.set_scan_busy(False)
        QMessageBox.warning(
            self,"Character collection check could not finish",
            f"{message}\n\nNo Review question, Character Knowledge, or Image Knowledge was changed.",
        )

    def apply_collection_decision(self,decision: str):
        task=self.selected_task()
        if not task or task.task_type!="collection_member" or task.status!="pending": return
        if not self.set_scan_busy(True,"collection_apply"): return
        label="Adding the reviewed collection member" if decision=="accepted" else "Saving the exact rejected candidate"
        self.header.set_status(label+" with stale and recovery checks…")
        worker=AsyncCallTask(lambda:self.collection_reviews.decide(task.id,decision))
        self._scan_task=worker
        worker.signals.complete.connect(self.collection_decision_complete)
        worker.signals.failed.connect(self.collection_decision_failed)
        self.pool.start(worker)

    def collection_decision_complete(self,result):
        self._scan_task=None; self.set_scan_busy(False)
        self._queue_health_counts=None; self.refresh(reload=False)
        self._emit_changed()
        if result.decision in {"accepted","already_present"}:
            self.header.set_status(
                "Saved the reviewed collection member • related pending "
                f"candidates reranked: {result.related_pending_reranked:,} • Image Knowledge unchanged"
            )
            if result.backup_directory and result.backup_directory != Path():
                self.header.status.setToolTip(
                    f"Character Knowledge recovery files: {result.backup_directory}"
                )
        else:
            self.header.set_status(
                "Remembered the exact rejected collection candidate • related "
                f"pending candidates reranked: {result.related_pending_reranked:,} • Image Knowledge unchanged"
            )

    def collection_decision_failed(self,message):
        self._scan_task=None; self.set_scan_busy(False)
        failed_task_id=self.current_task_id
        recovered=()
        try:
            recovered=self.collection_reviews.recover_interrupted()
        except (OSError,RuntimeError,ValueError):
            self.characters.load(); self.tasks.load()
        self._queue_health_counts=None
        self.refresh(reload=False,select_id=failed_task_id)
        accepted=next((
            result for result in recovered
            if result.task_id==failed_task_id
            and result.decision=="accepted"
        ),None)
        if accepted is not None:
            self._emit_changed()
            self.header.set_status(
                "Recovered the reviewed collection member from saved Character "
                "Knowledge • Review state synchronized • Image Knowledge unchanged"
            )
            QMessageBox.information(
                self,"Collection decision recovered",
                "The Character Knowledge change had already been saved. "
                "CollectiGen recovered the interrupted Review state and marked "
                "the collection question complete. No Image Knowledge was changed.",
            )
            return
        returned=next((
            result for result in recovered
            if result.task_id==failed_task_id
            and result.decision=="recovered_for_review"
        ),None)
        if returned is not None:
            self.header.set_status(
                "Collection decision was not saved • question returned for review • "
                "Image Knowledge unchanged"
            )
            QMessageBox.warning(
                self,"Collection decision returned for review",
                f"{message}\n\nThe collection member was not added. The saved "
                "Review question is pending again and can be retried. No Image "
                "Knowledge was changed.",
            )
            return
        QMessageBox.critical(
            self,"Collection decision was not saved",
            f"{message}\n\nSaved Character Knowledge and Review state remain "
            "recoverable. No Image Knowledge was changed.",
        )

    def _source_discovery_work(self,progress,should_cancel):
        dataset=getattr(self.state,"dataset",None)
        index=getattr(dataset,"index",None)
        snapshot_provider=getattr(index,"evidence_snapshot_token",None)
        if index is None or not callable(getattr(index,"all_records",None)) or not callable(snapshot_provider):
            raise RuntimeError(
                "The current named-library Image Knowledge index is unavailable. "
                "Refresh the included libraries, then try again."
            )
        before=snapshot_provider(); records=index.all_records(); after=snapshot_provider()
        if before!=after:
            raise RuntimeError(
                "Indexed Image Knowledge changed while Character Source discovery "
                "was starting. Run Find New Knowledge again."
            )
        library_ids={}; identity_for_path=getattr(dataset,"identity_for_path",None)
        if callable(identity_for_path):
            for row in records:
                if should_cancel():
                    raise InterruptedError("Character Source discovery cancelled")
                identity=identity_for_path(row.path)
                library_id=str(getattr(identity,"library_id","") or "")
                if library_id:library_ids[str(row.path)]=library_id
        return CharacterSourceDiscoveryService(
            self.tags,self.categories,self.characters,self.tasks,
        ).discover(
            records,snapshot_token=after,library_ids_by_path=library_ids,
            progress=progress,should_cancel=should_cancel,
        )

    def discover_character_sources(self):
        if self._scan_task is not None:
            show_operation_in_progress(self,"Review Center work"); return
        index=getattr(getattr(self.state,"dataset",None),"index",None)
        if index is None or not callable(getattr(index,"all_records",None)):
            QMessageBox.information(
                self,"Character Source discovery unavailable",
                "Refresh at least one included named library before checking "
                "current indexed Image Knowledge. Nothing was changed.",
            ); return
        self.tags.load(); self.categories.load(); self.characters.load(); self.tasks.load()
        if not self.set_scan_busy(True,"source_preview"):return
        self.header.set_status(
            "Checking current indexed confirmed Image Knowledge for exact "
            "Character Identity and Character Source pairs…"
        )
        worker=CancelableProgressCallTask(self._source_discovery_work)
        self._scan_task=worker
        worker.signals.progress.connect(self.collection_discovery_progress)
        worker.signals.complete.connect(self.source_discovery_complete)
        worker.signals.cancelled.connect(self.source_discovery_cancelled)
        worker.signals.failed.connect(self.source_discovery_failed)
        self.pool.start(worker)

    @staticmethod
    def source_preview_content(report):
        summary=(
            f"{report.candidate_count:,} review candidate(s): "
            f"{report.link_count:,} Character Source link(s) and "
            f"{report.alias_count:,} optional Other Name(s).\n\n"
            f"Indexed records checked: {report.records_scanned:,}\n"
            f"Exact one-character/one-source records: {report.exact_pair_records:,}\n"
            f"Ambiguous or incomplete pairs kept out: {report.ambiguous_pair_records:,}\n"
            f"Existing reviewed links kept out: {report.existing_links_excluded:,}\n"
            f"Existing Other Names kept out: {report.existing_aliases_excluded:,}\n"
            f"Other Name collisions kept out: {report.alias_collisions_excluded:,}\n"
            f"Durable prior decisions kept out: {report.durable_decisions_excluded:,}\n\n"
            "Creating questions changes only the saved Review queue. The source "
            "link and optional Other Name remain separate decisions."
        )
        details="\n".join(
            f"• {item.character_label} + {item.source_label} — "
            f"{'Character Source link' if item.action=='link' else 'Other Name '+item.alias} "
            f"— {item.observation_count:,} exact image(s)"
            for item in report.candidates[:100]
        )
        if report.candidate_count>100:
            details+=f"\n• …and {report.candidate_count-100:,} more"
        return summary,details

    def source_discovery_complete(self,report):
        self._scan_task=None; self.set_scan_busy(False)
        if not report.candidates:
            self.header.set_status(
                "Character Source check complete • no new review candidates"
            )
            QMessageBox.information(
                self,"No new Character Source candidates",
                "Current indexed confirmed Image Knowledge contained no new exact "
                "one-character/one-source relationship or source-qualified Other "
                "Name candidate. Nothing was changed.",
            ); return
        summary,details=self.source_preview_content(report)
        box=QMessageBox(QMessageBox.Question,"Create Character Source Review questions?",summary,parent=self)
        box.setDetailedText(details)
        confirm=box.addButton(
            f"Create {report.candidate_count:,} Review Question"
            f"{'s' if report.candidate_count!=1 else ''}",QMessageBox.AcceptRole,
        )
        box.addButton("Cancel",QMessageBox.RejectRole); box.setDefaultButton(confirm); box.exec()
        if box.clickedButton() is not confirm:
            self.header.set_status("Character Source preview closed • nothing changed"); return
        try:
            result=CharacterSourceDiscoveryService.queue_candidates(
                report,self.tasks,current_snapshot_token=self._collection_snapshot_token(),
            )
        except (OSError,RuntimeError,ValueError) as exc:
            QMessageBox.warning(
                self,"Character Source questions were not created",
                f"{exc}\n\nNo Character Knowledge, System Knowledge, or Image Knowledge was changed.",
            ); return
        select_id=result.task_ids[0] if result.task_ids else ""
        self._show_source_questions(select_id)
        self.state.events.publish("reviews_changed"); self.data_changed.emit()
        self.header.set_status(
            f"Character Source questions current • {result.queued:,} new • "
            f"{result.updated:,} refreshed • {result.skipped_decisions:,} prior decisions preserved"
        )

    def source_discovery_cancelled(self):
        self._scan_task=None; self.set_scan_busy(False)
        self.header.set_status("Character Source check cancelled • nothing changed")

    def source_discovery_failed(self,message):
        self._scan_task=None; self.set_scan_busy(False)
        QMessageBox.warning(
            self,"Character Source check could not finish",
            f"{message}\n\nNo Review question, Character Knowledge, System Knowledge, or Image Knowledge was changed.",
        )

    def apply_source_decision(self,decision: str):
        task=self.selected_task()
        if not task or task.task_type not in SOURCE_TASK_TYPES or task.status!="pending":return
        if task.task_type==SOURCE_ALIAS_TASK_TYPE and decision=="accepted":
            current=self.characters.character_source_id(
                "character",str(task.details.get("character_id") or ""),
            )
            if current!=str(task.details.get("source_id") or ""):
                QMessageBox.information(
                    self,"Review the Character Source first",
                    "Save the matching Character Source question before adding "
                    "this source-qualified Other Name. Nothing was changed.",
                ); return
        if not self.set_scan_busy(True,"source_apply"):return
        self.header.set_status(
            ("Saving the reviewed Character Source or Other Name" if decision=="accepted" else "Saving the exact rejected Character Source candidate")+
            " with stale and recovery checks…"
        )
        worker=AsyncCallTask(lambda:self.source_reviews.decide(task.id,decision))
        self._scan_task=worker
        worker.signals.complete.connect(self.source_decision_complete)
        worker.signals.failed.connect(self.source_decision_failed)
        self.pool.start(worker)

    def source_decision_complete(self,result):
        self._scan_task=None; self.set_scan_busy(False)
        self._queue_health_counts=None; self.refresh(reload=False)
        self._emit_changed()
        task=self.tasks.tasks.get(result.task_id)
        if result.decision in {"accepted","already_present"}:
            label=(
                "Saved the reviewed Character Source"
                if result.action=="link" else
                "Saved the reviewed Character/Source Other Name"
            )
            downstream=""
            if result.action=="alias" and task is not None:
                downstream=" "+self._record_alias_downstream(
                    str(task.details.get("character_tag_id") or ""),
                    str(task.details.get("character_tag") or ""),
                    (str(task.details.get("alias") or ""),),
                )
            self.header.set_status(
                f"{label} • related pending candidates reranked: "
                f"{result.related_pending_reranked:,} • Image Knowledge unchanged"+downstream
            )
            if result.backup_path and result.backup_path!=Path():
                self.header.status.setToolTip(f"Recovery record: {result.backup_path}")
        else:
            self.header.set_status(
                "Remembered the exact rejected Character Source candidate • "
                f"related pending candidates reranked: {result.related_pending_reranked:,} • Image Knowledge unchanged"
            )

    def source_decision_failed(self,message):
        self._scan_task=None; self.set_scan_busy(False)
        failed_task_id=self.current_task_id; recovered=()
        try:
            recovered=self.source_reviews.recover_interrupted()
        except (OSError,RuntimeError,ValueError):
            self.characters.load(); self.tags.load(); self.tasks.load()
        self._queue_health_counts=None
        self.refresh(reload=False,select_id=failed_task_id)
        result=next((item for item in recovered if item.task_id==failed_task_id),None)
        if result is not None and result.decision=="accepted":
            self._record_recovered_source_aliases((result,))
            self._emit_changed()
            self.header.set_status(
                "Recovered the reviewed Character Knowledge change • Review state synchronized • Image Knowledge unchanged"
            )
            QMessageBox.information(
                self,"Character Knowledge decision recovered",
                "The reviewed Character Source or Other Name had already been saved. "
                "CollectiGen synchronized Review state and marked the question complete. "
                "No Image Knowledge was changed.",
            ); return
        if result is not None and result.decision=="recovered_for_review":
            self.header.set_status(
                "Character Knowledge decision was not saved • question returned for review • Image Knowledge unchanged"
            )
            QMessageBox.warning(
                self,"Character Knowledge decision returned for review",
                f"{message}\n\nThe authority change was not applied. The saved Review "
                "question is pending again and can be retried. No Image Knowledge was changed.",
            ); return
        QMessageBox.critical(
            self,"Character Knowledge decision was not saved",
            f"{message}\n\nSaved Character/System Knowledge and Review state remain "
            "recoverable. No Image Knowledge was changed.",
        )

    def update_style_controls(self,*_):
        relationship=str(self.style_relationship.currentData() or "unknown")
        self.style_series.setEnabled(relationship in {"original","modified","cross_source","mixed"})

    def apply_image_question(self):
        task=self.selected_task()
        if not task or task.task_type!="image_question": return
        if str(task.details.get("action") or "") in {
            "review_in_editor","review_related_tags",
        }:
            self.edit_current_image_data(); return
        image=Path(task.image_path)
        if not self._image_ready_for_write(image):
            QMessageBox.warning(
                self,"Image unavailable or held",
                "This image is online-only, conflicted, read-only, unavailable, or has not been revalidated. Nothing was changed.\n\n"
                "Restore local access or resolve the conflict, refresh the library, then try again.",
            ); return
        details=dict(task.details)
        if str(details.get("action") or "")=="classify_visual_style":
            details["selected_relationship"]=str(self.style_relationship.currentData() or "unknown")
            series_id=str(self.style_series.currentData() or "")
            if not series_id and self.style_series.currentText().strip():
                series=self.characters.find_series(self.style_series.currentText()); series_id=series.id if series else ""
            details["selected_style_series_id"]=series_id
        if not self.set_scan_busy(True,"image_apply"):
            return
        self._pending_image_decision=(task.id,image,details)
        self.header.set_status(
            "Saving the reviewed image change with stale and read-back checks…"
        )
        worker=AsyncCallTask(
            lambda:ImageQuestionService.apply(image,details,self.state.knowledge)
        )
        self._scan_task=worker
        worker.signals.complete.connect(self.image_decision_complete)
        worker.signals.failed.connect(self.image_decision_failed)
        self.pool.start(worker)

    def image_decision_complete(self,changed):
        context=getattr(self,"_pending_image_decision",None)
        self._pending_image_decision=None
        self._scan_task=None; self.set_scan_busy(False)
        if context is None:
            return
        task_id,image,details=context
        resolution={
            "decision":"applied" if changed else "confirmed_already_present",
            "action":details.get("action") or "","values":list(details.get("values",[])),
        }
        if details.get("selected_relationship"):
            resolution["style_relationship"]=details["selected_relationship"]
            resolution["style_series_id"]=details.get("selected_style_series_id") or ""
        try:
            self.tasks.save_status(task_id,"resolved",resolution=resolution)
        except Exception as exc:
            if changed:
                self.state.events.publish("image_data_changed",image=str(image))
            title=(
                "Image changed; Review outcome needs recovery"
                if changed else "Review outcome was not saved"
            )
            explanation=(
                "The reviewed image change was saved, but its Review outcome "
                "could not be recorded. The question remains open and the "
                "automatic recheck will reconcile it without repeating the "
                "image change."
                if changed else
                "The image already matched this suggestion, but CollectiGen "
                "could not record the Review outcome. The question remains "
                "open and can be retried safely."
            )
            QMessageBox.critical(
                self,title,f"{explanation}\n\n{exc}",
            )
            return
        # Move the queue selection first. Publishing the image event while the
        # resolved item is still selected otherwise starts a preview that the
        # refresh immediately cancels.
        self._emit_review_changed(); self.refresh(reload=False)
        self.state.events.publish("image_data_changed",image=str(image))

    def image_decision_failed(self,message):
        self._pending_image_decision=None
        self._scan_task=None; self.set_scan_busy(False)
        QMessageBox.critical(self,"Could not update image data",message)

    def reject_image_question(self):
        task=self.selected_task()
        if getattr(self,"_scan_kind","") or not task or task.status!="pending" or task.task_type!="image_question": return
        resolution={
            "decision":"not_applied","action":task.details.get("action") or "",
            "reason":"The proposed change was reviewed and rejected.",
        }
        try:self.tasks.save_status(task.id,"resolved",resolution=resolution)
        except (OSError,RuntimeError,ValueError) as exc:
            QMessageBox.critical(self,"Could not save Review decision",f"The question remains open. No image facts were changed. Retry Reject Change after resolving the storage problem.\n\n{exc}"); return
        self._emit_review_changed(); self.refresh(reload=False)

    def apply_merge(self):
        task=self.selected_task()
        if not task or task.task_type!="merge_suggestion": return
        try: record,candidate=self._merge_candidate(task)
        except Exception as exc:
            QMessageBox.critical(self,"Could not inspect image data",str(exc)); return
        if record is None:
            QMessageBox.warning(self,"Image unavailable",f"The image could not be found:\n\n{task.image_path}"); return
        if candidate is None:
            self._save_merge_resolution(task,{
                "decision":"no_longer_applicable",
                "rule_id":task.details.get("rule_id") or "",
            })
            return
        before=list(candidate.before_tags); after=candidate.preview_tags()
        resolution={
            "decision":"projection_only_reviewed",
            "rule_id":candidate.rule.id,"location":candidate.location,
            "before":before,"after":after,
            "reason":"Confirmed Image Knowledge was retained; this rule applies only to training projections.",
        }
        self._save_merge_resolution(task,resolution)

    def reject_merge(self):
        task=self.selected_task()
        if not task or task.task_type!="merge_suggestion": return
        resolution={
            "decision":"complete_knowledge_retained","rule_id":task.details.get("rule_id") or "",
            "reason":"All separately meaningful confirmed facts were retained as Image Knowledge.",
        }
        self._save_merge_resolution(task,resolution)

    def _save_merge_resolution(self,task,resolution):
        try:
            saved=self.tasks.save_status(task.id,"resolved",resolution=resolution)
        except (OSError,RuntimeError,ValueError) as exc:
            QMessageBox.critical(
                self,"Could not complete projection question",
                "The completion was not confirmed. Image data, captions, and the rule were not changed. "
                f"Resolve the storage problem, reload the queue, and check the current question before retrying.\n\n{exc}",
            )
            self._remember_queue_status_failure(task.id,"resolved")
            return False
        if not saved:
            QMessageBox.information(
                self,"Question changed",
                "No new decision was saved. Reload the queue and review its current state before deciding again.",
            )
            self.refresh(reload=True)
            return False
        self._clear_queue_status_failure(task_id=task.id,status="resolved")
        self._emit_review_changed(); self.refresh(reload=False)
        return True

    def _remember_queue_status_failure(self,task_id,status):
        status_bar=getattr(getattr(self,"window",lambda:None)(),"statusBar",lambda:None)()
        notice=status_bar.currentMessage() if status_bar is not None else ""
        self._queue_status_failure=(str(task_id),str(status),notice)

    def _clear_queue_status_failure(self,*,task_id=None,status=None,reloaded=False):
        failure=getattr(self,"_queue_status_failure",None)
        if not failure:
            return
        failed_id,desired,notice=failure
        if reloaded:
            current=self.tasks.tasks.get(failed_id)
            if current is None or current.status!=desired:
                return
        elif str(task_id)!=failed_id or str(status)!=desired:
            return
        status_bar=getattr(getattr(self,"window",lambda:None)(),"statusBar",lambda:None)()
        if status_bar is not None and notice and status_bar.currentMessage()==notice:
            status_bar.clearMessage()
        self._queue_status_failure=None

    def disable_merge_rule(self):
        task=self.selected_task()
        if not task or task.task_type!="merge_suggestion": return
        rule_id=str(task.details.get("rule_id") or ""); rule=self.tags.merge_rules.get(rule_id)
        if rule is None:
            QMessageBox.information(self,"Rule unavailable","This cleanup rule has already been removed."); return
        affected=[candidate for candidate in self.tasks.tasks.values()
                  if candidate.status=="pending" and candidate.task_type=="merge_suggestion"
                  and str(candidate.details.get("rule_id") or "")==rule_id]
        if QMessageBox.question(
            self,"Disable cleanup rule",
            f"Disable this rule and close {len(affected):,} pending suggestion(s) created by it?\n\n"
            "Existing image data will not be changed.",
        )!=QMessageBox.Yes: return
        service=self._review_safe_batch_service()
        task_fingerprints={
            candidate.id:review_safe_task_fingerprint(candidate)
            for candidate in affected
        }
        def complete(result):
            self._accept_safe_result(result)
            self.header.set_status(
                f"Disabled cleanup rule • closed {result.knowledge_questions:,} pending suggestion(s) • image data unchanged"
            )
            self.header.status.setToolTip(
                f"Registry recovery files: {result.backup_directory}"
            )
        self._start_review_safe_apply(
            lambda:service.disable_merge_rule(rule_id,task_fingerprints),
            complete,"Cleanup rule was not disabled",
            "Disabling the cleanup rule with backup and read-back verification…",
        )

    def category_changed(self,*_):
        value=self.category.currentData(); self.current_category_id=str(value or "uncategorized")

    def category_from_combo(self,combo: QComboBox,default: str="uncategorized") -> str:
        value=combo.currentData()
        if value: return str(value)
        return str(self.categories.find_id_by_label(combo.currentText()) or default)

    def save_tag(self):
        task=self.selected_task()
        if not task or task.task_type!="new_tag": return
        choice=self.selected_outcome(); self.current_category_id=self.category_from_combo(self.category,self.current_category_id)
        if choice!="existing" and self.current_category_id not in self.categories.categories:
            QMessageBox.warning(self,"Choose a current category","Select a current category before saving this decision."); return
        target=None
        if choice=="existing":
            target=self.selected_existing_tag()
            if not target: QMessageBox.warning(self,"Choose a known SDXL Tag","Select the known SDXL Tag that has the same meaning."); return
        request=ReviewTagDecisionRequest(
            (task.id,),{task.id:review_tag_selection_fingerprint(task)},
            {task.id:dict(self.current_suggestion_for(task))},choice,
            self.current_category_id,target.id if target else "",
        )
        alias_downstream=(
            (target.id,target.display,(str(task.details.get("tag") or ""),))
            if choice=="existing" and target is not None else None
        )
        vocabulary_refresh=(
            choice=="keep" and bool(task.details.get("acknowledged"))
        )
        continue_apply=lambda:ReviewCenterPage._start_tag_decision_apply(
            self,request,alias_downstream,vocabulary_refresh,shared=False,
        )
        if choice=="existing":
            self._start_alias_preflight(
                (TagAliasProposal(
                    str(task.details.get("tag") or ""),target.id,target.display,
                ),),
                continue_apply,
            )
        else:
            continue_apply()

    def _tag_decision_batch_service(self):
        return ReviewTagDecisionBatchService(
            self.tags.path,self.discovery.path,self.tasks.path,
            rule_pair_admission=self.tags.rule_pair_admission,
            default_categories_path=DEFAULTS_DIR/"categories.json",
            user_categories_path=CATEGORIES_PATH,
            backup_root=USER_DATA_DIR/"backups"/"review-decisions",
            journal_path=USER_DATA_DIR/"operation_journal.jsonl",
            excluded_path=USER_DATA_DIR/"excluded_tags.txt",
        )

    def _alias_preflight_service(self,tags):
        return TagAliasRelationshipService(
            tags,default_path=DEFAULTS_DIR/"relationships.json",
            user_path=Path(tags.path).with_name("relationships.json"),
        )

    def _start_alias_preflight(self,proposals,confirmed) -> bool:
        """Read the cached alias-impact scope off the Qt thread before writing."""
        if self._scan_task is not None:
            show_operation_in_progress(self,"Review Center work")
            return False
        selected=tuple(proposals)
        index=getattr(getattr(self.state,"dataset",None),"index",None)
        tags_path=Path(self.tags.path)
        def work(_progress,should_cancel):
            if should_cancel():
                raise InterruptedError("Other Name scope check cancelled")
            service=self._alias_preflight_service(TagDatabase(tags_path))
            records=index.all_records() if index is not None else None
            if should_cancel():
                raise InterruptedError("Other Name scope check cancelled")
            return service.preflight(selected,records=records)
        if not self.set_scan_busy(True,"alias_preflight"):
            return False
        self.header.set_status(
            "Checking the shared Other Name scope from cached Image Knowledge…"
        )
        worker=CancelableProgressCallTask(work); self._scan_task=worker
        worker.signals.complete.connect(
            lambda plan:self._alias_preflight_complete(plan,confirmed)
        )
        worker.signals.cancelled.connect(self._alias_preflight_cancelled)
        worker.signals.failed.connect(self._alias_preflight_failed)
        self.pool.start(worker)
        return True

    def _alias_preflight_complete(self,plan,confirmed):
        self._scan_task=None; self.set_scan_busy(False)
        dialog=TagAliasPreflightDialog(plan,self.tags,parent=self)
        if dialog.exec()==QDialog.Accepted:
            confirmed()
            return
        if dialog.choice=="merge":
            self.merge_tags_requested.emit(dialog.merge_tag_ids())
        elif dialog.choice=="restore":
            self.removed_tags_requested.emit(dialog.removed_tag_ids())
        self.header.set_status("Other Name preview closed • nothing changed")

    def _alias_preflight_cancelled(self):
        self._scan_task=None; self.set_scan_busy(False)
        self.header.set_status("Other Name scope check cancelled • nothing changed")

    def _alias_preflight_failed(self,message):
        self._scan_task=None; self.set_scan_busy(False)
        QMessageBox.warning(
            self,"Other Names need review",
            "CollectiGen could not prepare the shared Other Name review. "
            f"No Review decision was saved.\n\n{message}",
        )

    def _start_tag_decision_apply(
        self,request,alias_downstream,vocabulary_refresh,*,shared: bool,
    ) -> bool:
        if not self.set_scan_busy(True,"shared_apply" if shared else "tag_apply"):
            return False
        self._pending_tag_choices=self._remember_tag_choices(request)
        self._pending_alias_downstream=alias_downstream
        self._pending_vocabulary_refresh=bool(vocabulary_refresh)
        label=(
            f"Saving {len(request.task_ids):,} separate exact SDXL Tag decisions "
            "with backup and read-back verification…"
            if shared else
            "Saving the exact SDXL Tag decision with backup and read-back verification…"
        )
        self.header.set_status(label)
        self._tag_decision_service=self._tag_decision_batch_service()
        worker=AsyncCallTask(
            lambda:self._tag_decision_service.apply_decision(request)
        )
        self._scan_task=worker
        worker.signals.complete.connect(self.shared_decision_complete)
        worker.signals.failed.connect(lambda message:self.shared_decision_failed(message,shared=shared))
        start_qt_delivery_task(self.pool,worker)
        return True

    def _remember_tag_choices(self,request):
        """Retain presentation choices, never a transaction or preflight approval."""
        category=self.categories.categories.get(request.category_id)
        target=self.tags.tags.get(request.target_id)
        return {
            "fingerprints":dict(request.task_fingerprints),
            "choice":request.choice,"category_id":request.category_id,
            "target_id":request.target_id,
            "category":deepcopy(category.to_dict()) if category else None,
            "target":deepcopy(target.to_dict()) if target else None,
        }

    def _restore_tag_choices(self,choices):
        selected=self.selected_tasks()
        if not choices or not selected or any(
            task.task_type!="new_tag" or task.status!="pending" for task in selected
        ) or {
            task.id:review_tag_selection_fingerprint(task) for task in selected
        }!=choices["fingerprints"]:
            return ""
        # Freshly read references; the rollback changes file tokens, not intent.
        categories=CategoryRegistry(self.categories.default_path,self.categories.user_path)
        self.categories=categories
        self.category.blockSignals(True)
        try:
            self.category.clear()
            for category_id,label in categories.choices():
                self.category.addItem(label,category_id)
            category=categories.categories.get(choices["category_id"])
            valid_category=category is not None and category.to_dict()==choices["category"]
            self.current_category_id=choices["category_id"] if valid_category else ""
            self.category.setCurrentIndex(self.category.findData(self.current_category_id))
            if not valid_category:
                self.category.clearEditText()
        finally:
            self.category.blockSignals(False)
        self.outcomes[choices["choice"]].setChecked(True)
        self.update_tag_controls()
        if choices["choice"]=="existing":
            target=self.tags.tags.get(choices["target_id"])
            valid_target=target is not None and target.to_dict()==choices["target"]
            self.existing.setCurrentIndex(self.existing.findData(target.id) if valid_target else -1)
            if not valid_target:
                self.existing.clearEditText()
                return "The known Tag changed or is unavailable. Select it again before retrying."
        elif not valid_category:
            return "The category changed or is unavailable. Select it again before retrying."
        return "Your choices are retained for the same pending question(s)."

    def _record_alias_downstream(self,target_id,target_display,names) -> str:
        classification=classify_system_knowledge_change(
            "tag_aliases",label=(
                f'Saved Review Center Other Names for "{target_display}"'
            ),
            affected_tag_ids=(str(target_id),),source_names=tuple(names),
            matching_images=True,
        )
        try:self.downstream_updates.record_classification(classification)
        except OSError:pass
        else:self.downstream_updates_changed.emit()
        return classification.completion_summary()

    def _record_recovered_source_aliases(self,results) -> None:
        for result in results:
            if result.decision!="accepted" or result.action!="alias":
                continue
            task=self.tasks.tasks.get(result.task_id)
            if task is None:
                continue
            self._record_alias_downstream(
                str(task.details.get("character_tag_id") or ""),
                str(task.details.get("character_tag") or ""),
                (str(task.details.get("alias") or ""),),
            )

    def _accept_tag_decision_result(self,result):
        self.tags.tags=result.knowledge.tags; self.tags.merge_rules=result.knowledge.merge_rules
        self.tags._payload_extras=result.knowledge._payload_extras; self.tags._loaded_bytes=result.knowledge._loaded_bytes
        if result.knowledge._alias_authority_enabled:
            self.tags.configure_alias_authority(
                result.knowledge._canonical_alias_owners,
                result.knowledge._represented_alias_keys,
                alias_labels=result.knowledge._canonical_alias_labels,
            )
        else:self.tags.clear_alias_authority()
        self.discovery.items=result.discovery.items
        self.tasks.load()

    def _reload_tag_decision_state(self):
        self.tags.load()
        try:
            TagAliasRelationshipService(
                self.tags,default_path=DEFAULTS_DIR/"relationships.json",
                user_path=self.tags.path.with_name("relationships.json"),
            )
        except (OSError,ValueError,RuntimeError):
            # TagDatabase.load() deliberately retains the last valid canonical
            # index, which is safer than reviving represented inline aliases
            # when the relationship store is currently unreadable.
            pass
        self.discovery.load(); self.tasks.load()

    def apply_selected_tag_decision(self):
        selected=self.selected_tasks()
        if not self._compatible_tag_selection(selected):
            QMessageBox.information(self,"Questions need individual decisions","Select two or more pending SDXL Tag questions. Image changes, cleanup suggestions, completed items, and questions with different fields remain individual decisions."); return
        choice=self.selected_outcome(); category_id=self.category_from_combo(self.category,self.current_category_id); target=None
        if choice!="existing" and category_id not in self.categories.categories:
            QMessageBox.warning(self,"Choose a current category","Select a current category before saving these decisions."); return
        if choice=="existing":
            target=self.selected_existing_tag()
            if not target: QMessageBox.warning(self,"Choose a known SDXL Tag","Select the known SDXL Tag that has the same meaning for every selected question."); return
        names=[str(task.details.get("tag") or "") for task in selected]
        outcomes={"keep":f"Recognize each exact SDXL Tag under {self.categories.path_label(category_id)}","existing":f"Treat each exact name as an alias of {target.display if target else ''}","exclude":f"Recognize each exact SDXL Tag under {self.categories.path_label(category_id)} and exclude it from training captions"}
        request=ReviewTagDecisionRequest(
            tuple(task.id for task in selected),
            {task.id:review_tag_selection_fingerprint(task) for task in selected},
            {task.id:dict(self.current_suggestion_for(task)) for task in selected},
            choice,category_id,target.id if target else "",
        )
        alias_downstream=(
            (target.id,target.display,tuple(names))
            if choice=="existing" and target is not None else None
        )
        vocabulary_refresh=(
            choice=="keep" and all(
                bool(task.details.get("acknowledged")) for task in selected
            )
        )
        continue_apply=lambda:ReviewCenterPage._start_tag_decision_apply(
            self,request,alias_downstream,vocabulary_refresh,shared=True,
        )
        if choice=="existing":
            self._start_alias_preflight(
                tuple(TagAliasProposal(
                    str(task.details.get("tag") or ""),target.id,target.display,
                ) for task in selected),continue_apply,
            )
        elif not self._confirm_shared_decision(names,outcomes[choice]):return
        else:
            continue_apply()

    def _confirm_shared_decision(self,names,outcome) -> bool:
        box=QMessageBox(QMessageBox.Question,"Preview Shared Decision",f"Apply one answer to {len(names):,} exact SDXL Tags?",parent=self)
        box.setInformativeText(
            f"{outcome}\n\n"
            "CollectiGen will save a separate reviewed outcome for every exact name. It will not create a semantic pattern, edit Image Knowledge, captions, TXT, Metadata, or image pixels. "
            "System Knowledge, discovery, Review outcomes, and any training-caption exclusions are backed up, read back, and restored together if saving fails."
        )
        box.setDetailedText("Exact SDXL Tag questions:\n"+"\n".join(f"• {name}" for name in names))
        confirm=box.addButton(f"Apply to {len(names):,} Exact Tags",QMessageBox.AcceptRole); box.addButton("Cancel",QMessageBox.RejectRole); box.setDefaultButton(confirm)
        box.setAccessibleName(f"Apply one answer to {len(names):,} exact SDXL Tags?")
        box.exec()
        return box.clickedButton() is confirm

    def shared_decision_complete(self,result):
        self._pending_tag_choices=None
        downstream=getattr(self,"_pending_alias_downstream",None)
        self._pending_alias_downstream=None
        vocabulary_refresh=bool(getattr(self,"_pending_vocabulary_refresh",False))
        self._pending_vocabulary_refresh=False
        self._scan_task=None; self.set_scan_busy(False)
        status_bar=getattr(getattr(self,"window",lambda:None)(),"statusBar",lambda:None)()
        if (
            status_bar is not None and getattr(self,"_tag_save_failure_notice","")
            and status_bar.currentMessage()==self._tag_save_failure_notice
        ):
            status_bar.clearMessage()
        self._tag_save_failure_notice=""
        self._accept_tag_decision_result(result)
        self._queue_health_counts=None
        if vocabulary_refresh:
            self.state.refresh_persistent_data(vocabulary_names=result.tag_names)
            self.data_changed.emit()
        else:self._emit_changed()
        self.refresh(reload=False)
        status=(
            f"Saved {result.task_count:,} separate exact SDXL Tag decisions • "
            f"related pending questions reranked: "
            f"{result.related_pending_reranked:,} • no semantic pattern or image-data change"
        )
        if downstream is not None:
            status+=" • "+self._record_alias_downstream(*downstream)
        self.header.set_status(status)
        self.header.status.setToolTip(f"Recovery files: {result.backup_directory}")
        if self.isVisible():announce_accessibly(self.header.status,self.header.status.text())

    def shared_decision_failed(self,message,*,shared=False):
        choices=getattr(self,"_pending_tag_choices",None)
        self._pending_tag_choices=None
        retained=""
        self._pending_alias_downstream=None
        self._pending_vocabulary_refresh=False
        self._scan_task=None; self.set_scan_busy(False)
        self._queue_health_counts=None
        try:
            self._reload_tag_decision_state()
            self.refresh(reload=False)
            retained=self._restore_tag_choices(choices)
        except (OSError,RuntimeError,ValueError) as exc:
            message=(f"{message}\n\nCurrent saved question could not be reloaded: {exc}. "
                     "Resolve the recovery warning before another save.")
        title="Shared decision save needs review" if shared else "Decision save needs review"
        QMessageBox.critical(self,title,
            f"{message}\n\nReview the current question and any recovery record before deciding whether to retry. "
            "Resolve a recovery warning before another save.")
        status_bar=getattr(getattr(self,"window",lambda:None)(),"statusBar",lambda:None)()
        self._tag_save_failure_notice=(
            status_bar.currentMessage() if status_bar is not None else ""
        )
        self.header.set_status(
            "Decision save was not confirmed. "+(retained+" " if retained else "")+
            "Review the error details before retrying."
        )

    def save_group(self):
        task=self.selected_task()
        if not task or task.task_type!="pattern_group": return
        selected=[]; remaining=[]
        for index in range(self.group_tags.count()):
            item=self.group_tags.item(index); (selected if item.checkState()==Qt.Checked else remaining).append(item.text())
        if not selected: QMessageBox.warning(self,"Choose matching SDXL Tags","Leave at least one matching SDXL Tag checked."); return
        suggestion=dict(task.details.get("suggestion") or {}); category_id=self.category_from_combo(self.group_category)
        suggestion["category"]=category_id
        item=ReviewSafeKnowledgeItem(
            task.id,task.task_type,tuple(selected),suggestion,
            review_safe_task_fingerprint(task),tuple(remaining),
        )
        service=self._review_safe_batch_service()
        def complete(result):
            self._accept_safe_result(result,select_id=task.id if remaining else None)
            remaining_status=(
                f"{len(remaining):,} still awaiting review"
                if remaining else "group review completed"
            )
            self.header.set_status(
                f"Saved {len(selected):,} related SDXL Tag interpretation(s) • "
                f"{remaining_status} • image data unchanged"
            )
            self.header.status.setToolTip(
                f"Registry recovery files: {result.backup_directory}"
            )
        self._start_review_safe_apply(
            lambda:service.apply(service.preview_selected((item,))),
            complete,"Related SDXL Tags were not saved",
            "Saving the selected SDXL Tags with backup and read-back verification…",
        )

    def set_status(self,status: str):
        if getattr(self,"_scan_kind",""):return
        if len(self.selected_tasks())>1:
            QMessageBox.information(self,"Choose one question","Dismiss or restore one exact question at a time. The shared editor only saves compatible SDXL Tag decisions."); return
        task=self.selected_task()
        if not task: return
        if task.task_type=="new_tag" and task.status=="resolved" and status=="pending":
            self.edit_saved_tag()
            return
        if status=="ignored" and task.status!="pending":return
        if task.task_type=="collection_member":
            if task.status=="pending" and status=="ignored":
                self.apply_collection_decision("rejected"); return
            if task.status=="resolved":
                QMessageBox.information(
                    self,"Edit the saved collection instead",
                    "This completed approval already changed Character Knowledge. "
                    "Edit that Character or outfit collection to remove the member; "
                    "reopening this old question would not reverse the saved collection.",
                )
                return
        if task.task_type in SOURCE_TASK_TYPES:
            if task.status=="pending" and status=="ignored":
                self.apply_source_decision("rejected"); return
            if task.status=="resolved":
                QMessageBox.information(
                    self,"Edit the saved Character Knowledge instead",
                    "This completed approval already changed Character or System "
                    "Knowledge. Edit the Character Source or the Tag's Other Names "
                    "to change that authority; reopening this old question would "
                    "not reverse the saved decision.",
                )
                return
        if task.task_type==TAG_MEANING_TASK_TYPE:
            if task.status=="pending" and status=="ignored":
                self.apply_meaning_decision("rejected"); return
            if task.status=="resolved":
                QMessageBox.information(
                    self,"Edit the saved Meaning & Usage instead",
                    "This completed approval already changed System Knowledge. "
                    "Edit the Tag's current Meaning & Usage summary to revise it; "
                    "reopening this old evidence question would not undo that save.",
                )
                return
        if task.task_type==TAG_BROADER_PROPOSAL_TASK_TYPE:
            if task.status=="pending" and status=="ignored":
                self.apply_broader_decision("rejected"); return
            if task.status=="resolved":
                QMessageBox.information(
                    self,"Edit the saved Broader Tag instead",
                    "This completed approval already changed canonical System Knowledge. Edit the Tag's current Base override to revise it; reopening the old proposal would not undo that verified save.",
                )
                return
        try:saved=self.tasks.save_status(task.id,status)
        except (OSError,RuntimeError,ValueError) as exc:
            QMessageBox.critical(
                self,"Could not save Review status",
                "The status change could not be confirmed. Image data and System Knowledge "
                "were not changed. Reload Queue to check the saved question before deciding again."
                f"\n\n{exc}",
            )
            self._remember_queue_status_failure(task.id,status)
            return
        if not saved:
            QMessageBox.information(
                self,"Question changed",
                "No new decision was saved. Reloading the queue to show the current question status.",
            )
            self.refresh(reload=True)
            return
        self._clear_queue_status_failure(task_id=task.id,status=status)
        self._emit_review_changed(); self.refresh(reload=False)

    def _emit_changed(self):
        self.state.events.publish("knowledge_changed"); self.state.events.publish("reviews_changed"); self.data_changed.emit()

    def _emit_review_changed(self):
        """Publish a queue/image outcome without reloading System Knowledge."""
        self.state.events.publish("reviews_changed"); self.data_changed.emit()

    def related_pending(self,task):
        key=self.current_suggestion_for(task).get("pattern_id")
        if not key: return []
        token_provider=getattr(self,"_queue_source_token",None)
        token=(
            token_provider() if callable(token_provider)
            else (id(self.tasks.tasks),len(self.tasks.tasks))
        )
        if token!=getattr(self,"_related_pattern_cache_key",None):
            index={}
            for candidate in self.tasks.tasks.values():
                if candidate.status!="pending" or candidate.task_type!="new_tag":
                    continue
                pattern_id=self._cached_related_pattern_id(candidate)
                if pattern_id:index.setdefault(pattern_id,[]).append(candidate.id)
            self._related_pattern_cache_key=token
            self._related_pattern_index={
                pattern_id:tuple(ids) for pattern_id,ids in index.items()
            }
        return [
            self.tasks.tasks[candidate_id]
            for candidate_id in self._related_pattern_index.get(str(key),())
            if candidate_id!=task.id and candidate_id in self.tasks.tasks
        ]

    @staticmethod
    def _cached_related_pattern_id(task):
        """Read already-derived family evidence without scanning on the UI thread."""
        cached=task.details.get("suggestion") if isinstance(task.details,dict) else None
        return str(cached.get("pattern_id") or "") if isinstance(cached,dict) else ""

    def apply_related(self):
        task=self.selected_task()
        if not task: return
        related=self.related_pending(task); all_tasks=[task,*related]
        names=[str(item.details.get("tag") or "") for item in all_tasks]
        if QMessageBox.question(self,"Confirm related SDXL Tags",f"Apply this interpretation to {len(names)} related SDXL Tags?\n\n"+"\n".join(f"• {name}" for name in names[:15]))!=QMessageBox.Yes: return
        service=ReviewSafeBatchService(
            self.tags.path,self.discovery.path,self.tasks.path,
            rule_pair_admission=self.tags.rule_pair_admission,
            default_categories_path=DEFAULTS_DIR/"categories.json",
            user_categories_path=CATEGORIES_PATH,
            backup_root=USER_DATA_DIR/"backups"/"review-safe",
            journal_path=USER_DATA_DIR/"operation_journal.jsonl",
        )
        items=[
            ReviewSafeKnowledgeItem(
                item.id,item.task_type,
                (str(item.details.get("tag") or ""),),
                dict(self.current_suggestion_for(item)),
                review_safe_task_fingerprint(item),
            )
            for item in all_tasks
        ]
        self._start_review_safe_apply(
            lambda:service.apply(service.preview_selected(tuple(items))),
            self.safe_apply_complete,"Related interpretations were not saved",
            "Saving the related SDXL Tag decisions with backup and read-back verification…",
        )

    def confirm_confident(self):
        if not self.set_scan_busy(True,"safe_preview"):
            return
        self.header.set_status("Preparing safe Tag changes from saved Review evidence…")
        self._safe_service=ReviewSafeBatchService(
            self.tags.path,self.discovery.path,self.tasks.path,
            rule_pair_admission=self.tags.rule_pair_admission,
            default_categories_path=DEFAULTS_DIR/"categories.json",
            user_categories_path=CATEGORIES_PATH,
            backup_root=USER_DATA_DIR/"backups"/"review-safe",
            journal_path=USER_DATA_DIR/"operation_journal.jsonl",
        )
        worker=CancelableProgressCallTask(lambda _progress,should_cancel:self._safe_service.preview(should_cancel))
        self._scan_task=worker
        worker.signals.complete.connect(self.safe_preview_complete)
        worker.signals.failed.connect(self.safe_preview_failed)
        self.pool.start(worker)

    @staticmethod
    def safe_confirmation_content(preview):
        names=preview.interpreted_names; cleanups=len(preview.merge_items); images=preview.cleanup_image_count
        if names and not cleanups:
            title=f"Confirm {len(names):,} reusable interpretation{'s' if len(names)!=1 else ''}?"
            action=f"Confirm {len(names):,} Interpretation{'s' if len(names)!=1 else ''}"
        elif cleanups and not names:
            title=f"Apply {cleanups:,} verified cleanup{'s' if cleanups!=1 else ''}?"
            action=f"Apply {cleanups:,} Cleanup{'s' if cleanups!=1 else ''}"
        else:
            title=f"Apply {preview.total_actions:,} safe suggestions?"
            action=f"Apply {preview.total_actions:,} Safe Suggestions"
        sections=[]
        if names:
            sections.extend((
                f"System Knowledge: add or update {len(names):,} reviewed, exact SDXL Tag interpretation{'s' if len(names)!=1 else ''}.",
                f"Review Center: mark {preview.knowledge_question_count:,} matching question{'s' if preview.knowledge_question_count!=1 else ''} completed.",
                "Future behavior: these exact names can be recognized and organized from the saved interpretations. No broader meaning pattern or hidden rule is created.",
            ))
        if cleanups:
            sections.extend((
                f"Image data: recheck {cleanups:,} deterministic cleanup{'s' if cleanups!=1 else ''} across {images:,} image{'s' if images!=1 else ''}; rules that require judgment remain pending.",
                "Recovery: changed Image Knowledge and generated TXT captions are backed up, saved, and read back. A recovery manifest is retained.",
                "Unchanged: original image pixels and embedded Metadata.",
            ))
        else:
            sections.append("Unchanged: image files, Image Knowledge, captions, TXT, and Metadata.")
        sections.append("Registry recovery: System Knowledge and Review outcomes are backed up and read back; a failed or stale save restores their prior state.")
        details=[]
        if names:
            details.append("System Knowledge and Review questions:\n"+"\n".join(f"• {name}" for name in names))
        if preview.merge_items:
            details.append("Image cleanup Review questions:\n"+"\n".join(
                f"• {item.image_path} — rule {item.rule_id} — task {item.task_id}" for item in preview.merge_items
            ))
        return title,"\n\n".join(sections),"\n\n".join(details),action

    def _safe_confirmation_box(self,preview):
        title,summary,details,action=self.safe_confirmation_content(preview)
        box=QMessageBox(QMessageBox.Question,"Review Safe Tag Changes",title,parent=self)
        box.setInformativeText(summary); box.setDetailedText(details)
        box.setAccessibleName(title); box.setAccessibleDescription(summary)
        confirm=box.addButton(action,QMessageBox.AcceptRole); box.addButton("Cancel",QMessageBox.RejectRole)
        box.setDefaultButton(confirm)
        return box,confirm

    def _confirm_safe_preview(self,preview) -> bool:
        box,confirm=self._safe_confirmation_box(preview); box.exec()
        return box.clickedButton() is confirm

    def safe_preview_complete(self,preview):
        self._scan_task=None
        if preview.cancelled:
            self.set_scan_busy(False); self.header.set_status("Safe-suggestion preview cancelled • nothing changed")
            return
        if not preview.knowledge_items and not preview.merge_items:
            self.set_scan_busy(False)
            detail=(
                "No eligible safe Tag changes are available. This tool includes "
                "only saved pending SDXL Tag interpretations at or above 95% and "
                "Tag cleanup rules explicitly marked safe.\n\n"
                "Image-information proposals—such as character assignments—even "
                "when shown at 99%, remain in the ordinary Review queue and are "
                "not evaluated by this batch."
            )
            if preview.uncached_questions:
                detail+=f"\n\n{preview.uncached_questions:,} question(s) have no saved interpretation yet and were not recomputed by this bulk action."
            QMessageBox.information(self,"No safe Tag changes available",detail); return
        self.set_scan_busy(False)
        if not self._confirm_safe_preview(preview):
            self.header.set_status("Safe-suggestion preview closed • nothing changed")
            return
        if not self.set_scan_busy(True,"safe_apply"):
            self.header.set_status("Safe suggestions were not applied • another operation started")
            return
        self.header.set_status(f"Applying {preview.total_actions:,} confirmed safe suggestion(s) with backup and read-back verification…")
        worker=AsyncCallTask(lambda:self._safe_service.apply(preview)); self._scan_task=worker
        worker.signals.complete.connect(
            lambda result:self._finish_review_safe_apply(self.safe_apply_complete,result)
        )
        worker.signals.failed.connect(self.safe_apply_failed); self.pool.start(worker)

    def safe_preview_failed(self,message):
        self._scan_task=None; self.set_scan_busy(False)
        QMessageBox.warning(self,"Could not prepare safe suggestions",message)

    def safe_apply_complete(self,result):
        self._accept_safe_result(result)
        summary=(
            f"Confirmed {result.knowledge_names:,} exact interpretation(s) "
            f"across {result.knowledge_questions:,} Review question(s) • "
            f"related pending questions reranked: "
            f"{result.related_pending_reranked:,} • Image Knowledge unchanged"
        )
        self.header.set_status(summary); self.header.status.setToolTip(f"Registry recovery files: {result.backup_directory}")
        if self.isVisible():announce_accessibly(self.header.status,self.header.status.text())

    def _start_review_safe_apply(self,work,complete,failure_title,status) -> bool:
        """Run one verified Review registry transaction outside the Qt thread."""
        if self._scan_task is not None:
            show_operation_in_progress(self,"Review Center work")
            return False
        if not self.set_scan_busy(True,"safe_apply"):
            return False
        self.header.set_status(status)
        worker=AsyncCallTask(work); self._scan_task=worker
        worker.signals.complete.connect(
            lambda result:self._finish_review_safe_apply(complete,result)
        )
        worker.signals.failed.connect(
            lambda message:self._review_safe_apply_failed(failure_title,message)
        )
        self.pool.start(worker)
        return True

    def _finish_review_safe_apply(self,complete,result):
        self._scan_task=None; self.set_scan_busy(False)
        complete(result)

    def _review_safe_apply_failed(self,title,message):
        self._scan_task=None; self.set_scan_busy(False)
        try:
            self._reload_safe_review_state()
        except (OSError,RuntimeError,ValueError):
            pass
        self._queue_health_counts=None; self.refresh(reload=False)
        QMessageBox.critical(self,title,message)

    def _review_safe_batch_service(self):
        return ReviewSafeBatchService(
            self.tags.path,self.discovery.path,self.tasks.path,
            rule_pair_admission=self.tags.rule_pair_admission,
            default_categories_path=DEFAULTS_DIR/"categories.json",
            user_categories_path=CATEGORIES_PATH,
            backup_root=USER_DATA_DIR/"backups"/"review-safe",
            journal_path=USER_DATA_DIR/"operation_journal.jsonl",
        )

    def _reload_safe_review_state(self):
        self.tags.load(); self.discovery.load(); self.tasks.load()

    def _accept_safe_result(self,result,*,select_id=None):
        self.tags.__dict__.update(result.knowledge.__dict__)
        self.discovery.items=result.discovery.items
        self.tasks.load()
        self._queue_health_counts=None; self.refresh(reload=False,select_id=select_id); self._emit_changed()

    def safe_apply_failed(self,message):
        self._review_safe_apply_failed("Safe suggestions were not applied",message)

    def recheck_questions(self):
        coordinator=getattr(self.state,"derived_refresh",None)
        if coordinator is None:
            self.header.set_status(
                "Automatic Review refresh is unavailable in this session."
            )
            return
        job=coordinator.request(
            None,cause="Review Center recheck requested",
        )
        if job is None:
            self.header.set_status(
                "Current Review questions could not be queued. Try again after "
                "the active background work finishes."
            )
            return
        self._derived_recheck_pending=True
        self.recheck_button.setEnabled(False)
        self.recheck_button.setText("Rechecking Questions…")
        self.header.set_status(
            "Rechecking saved Image Knowledge and reviewed dependencies…"
        )

    def derived_review_changed(self,details):
        details=dict(details or {})
        state=str(details.get("state") or "")
        if state not in {"complete","cancelled","failed"}:
            return
        recheck=bool(getattr(self,"_derived_recheck_pending",False))
        self._derived_recheck_pending=False
        if recheck:
            self.recheck_button.setEnabled(self._scan_task is None)
            self.recheck_button.setText("Recheck Questions…")
        selected=self.current_task_id
        self._queue_health_counts=None
        self.refresh(reload=False,select_id=selected)
        created=int(details.get("review_created") or 0)
        refreshed=int(details.get("review_refreshed") or 0)
        retired=(
            int(details.get("review_retired") or 0)
            +int(details.get("review_legacy_retired") or 0)
        )
        failed=int(details.get("review_failed") or 0)
        cause=str(details.get("cause") or "Saved authority changed")
        if state=="complete":
            summary=(
                f"Review questions current • {created:,} new • "
                f"{refreshed:,} updated • {retired:,} retired"
                +(f" • {failed:,} need Recheck" if failed else "")
            )
        elif state=="cancelled":
            summary="Review question refresh stopped safely; completed items remain current."
        else:
            summary="Review questions could not be refreshed; saved outcomes remain unchanged."
        if recheck or created or refreshed or retired or failed:
            self.header.set_status(summary)
            self.queue_freshness.setToolTip(
                f"{summary}\nCause: {cause}\nNo TXT, Metadata, image, or "
                "Image Knowledge file was written by this refresh."
            )

    def cancel_review_operation(self):
        if hasattr(self._scan_task,"cancel"):
            self.state.request_long_operation_cancel("review")
            self.cancel_review_button.setEnabled(False)
            self.scan_progress_text.setText(
                "Stopping collection discovery safely…"
                if self._scan_kind=="collection_preview" else
                "Stopping Character Source discovery safely…"
                if self._scan_kind=="source_preview" else
                "Cancelling Other Name scope check…"
                if self._scan_kind=="alias_preflight" else
                "Cancelling safe preview…"
            )
            self._scan_task.cancel()

    def set_scan_busy(self,busy: bool,kind: str=""):
        if busy:
            label={"safe_preview":"Preparing safe Tag changes","safe_apply":"Applying verified safe Tag changes","alias_preflight":"Checking shared Other Name scope","tag_apply":"Saving an exact-tag decision","shared_apply":"Saving shared exact-tag decisions","collection_preview":"Checking indexed Character collections","collection_apply":"Saving a Character collection decision","source_preview":"Checking indexed Character Sources","source_apply":"Saving a Character Source decision","image_apply":"Saving a reviewed image change"}.get(kind,"Review Center work")
            started,active=self.state.begin_long_operation("review",label)
            if not started:
                show_operation_in_progress(self,active); return False
        else: self.state.finish_long_operation("review")
        self._scan_kind=kind if busy else ""
        recheck_pending=bool(getattr(self,"_derived_recheck_pending",False))
        self.recheck_button.setEnabled(not busy and not recheck_pending)
        self.recheck_button.setText(
            "Rechecking Questions…" if recheck_pending else "Recheck Questions…"
        )
        self.safe_button.setText("Preparing Safe Tag Changes…" if busy and kind=="safe_preview" else "Applying Safe Tag Changes…" if busy and kind=="safe_apply" else "Review Safe Tag Changes…")
        for control in (self.questions,self.editor,self.status_filter,self.kind_filter,self.health_filter,self.queue_search,self.safe_button,self.conflict_button,self.open_image,self.dispose_noise,self.previous_queue,self.next_queue,self.clear_filters,self.reload_queue_button):
            control.setEnabled(not busy)
        cancellable=busy and kind in {"safe_preview","alias_preflight","collection_preview","source_preview"}
        self.scan_progress.setVisible(cancellable); self.scan_progress_text.setVisible(cancellable); self.cancel_review_button.setVisible(cancellable); self.cancel_review_button.setEnabled(cancellable)
        if cancellable:
            collection=kind=="collection_preview"; source=kind=="source_preview"; alias=kind=="alias_preflight"
            set_progress_bar(
                self.scan_progress,
                unknown_text=("Checking cached Image Knowledge…" if alias else "Checking indexed Image Knowledge…" if collection or source else "Preparing…"),
            )
            self.scan_progress_text.setText(
                "Preparing Character collection evidence…"
                if collection else
                "Preparing Character Identity and Source evidence…"
                if source else "Preparing saved Review suggestions…"
            )
            self.cancel_review_button.setText(
                "Cancel Check" if collection or source or alias else "Cancel Preview"
            )
        if not busy:
            QTimer.singleShot(0,self._emit_background_work_finished_if_idle)
        return True

    def _emit_background_work_finished_if_idle(self):
        if not self.has_active_background_work():
            self.background_work_finished.emit()

    def has_active_background_work(self) -> bool:
        task=self._scan_task
        return bool(
            task is not None
            and not getattr(task,"is_finished",lambda:False)()
        ) or history_dialogs_busy(self) or any(
            dialog._scan_task is not None for dialog in (
                *getattr(self,"_conflict_dialogs",()),*getattr(self,"_guided_dialogs",())))

    def request_background_shutdown(self) -> bool:
        close_history_dialogs(self)
        for dialog in tuple(getattr(self,"_conflict_dialogs",())):dialog.reject()
        for dialog in tuple(getattr(self,"_guided_dialogs",())):dialog.reject()
        if not self.has_active_background_work():
            QTimer.singleShot(0,self.background_work_finished.emit)
            return False
        if self._scan_kind in {
            "safe_preview","alias_preflight","collection_preview","source_preview",
        }:
            self.cancel_review_operation()
        return True

    def _example_paths(self,task,tag=""):
        paths=[]
        if task.image_path: paths.append(task.image_path)
        paths.extend(
            str(value) for value in task.details.get("example_paths",())
            if str(value)
        )
        tag=str(tag or task.details.get("tag") or "").strip()
        observed=self.discovery.items.get(normalize_tag(tag))
        if observed: paths.extend(observed.example_images)
        return list(dict.fromkeys(path for path in paths if path))

    def _task_evidence_tags(self,task):
        if task.task_type=="pattern_group":
            return tuple(dict.fromkeys(
                str(value).strip() for value in task.details.get("tags",())
                if str(value).strip()
            ))
        if task.task_type=="image_question":
            details=dict(task.details); action=str(details.get("action") or "")
            explicit=tuple(dict.fromkeys(
                str(value).strip() for value in details.get("evidence_tags",())
                if str(value).strip()
            ))
            if explicit:return explicit
            values=tuple(
                str(value).strip() for value in details.get("values",())
                if str(value).strip()
            )
            if action=="add_sdxl_tags":return values
            if action in {"assign_character","replace_character"}:
                candidates=values[:1]+(values[2:3] if action=="replace_character" else values[1:2])
                for value in candidates:
                    record=self.tags.find(value)
                    if record is not None:return (record.display,)
            if action=="classify_visual_style":
                for value in values:
                    record=self.tags.find(value)
                    if record is not None:return (record.display,)
            known=tuple(dict.fromkeys(
                record.display for value in values
                if (record:=self.tags.find(value)) is not None
            ))
            if known:return known
        tag=str(task.details.get("tag") or "").strip()
        return (tag,) if tag else ()

    def _show_current_image(self,task):
        path=str(getattr(task,"image_path","") or "").strip()
        if not path:
            self._cancel_current_image_request()
            self.current_image_path=""; self.current_image_panel.hide(); return
        self._cancel_current_image_request()
        self.current_image_path=str(Path(path))
        self.current_image_name.setText(Path(path).name)
        self.current_image_name.setToolTip(str(Path(path)))
        self.current_image_name.setAccessibleName(f"Selected image: {Path(path).name}")
        self.current_image_preview.clear("Loading preview…")
        self.current_image_panel.show()
        self._current_image_generation+=1
        generation=self._current_image_generation
        load_path=self.current_image_path
        worker=AsyncCallTask(
            lambda:(generation,load_path,self._decode_preview_image(load_path))
        )
        self._current_image_task=worker
        worker.signals.complete.connect(self._current_image_ready)
        worker.signals.failed.connect(
            lambda message,g=generation,p=load_path:
            self._current_image_failed(message,g,p)
        )
        self.pool.start(worker)

    @staticmethod
    def _decode_preview_image(path,max_dimension: int=4096):
        reader=QImageReader(str(path)); reader.setAutoTransform(True)
        size=reader.size()
        if size.isValid() and max(size.width(),size.height())>max_dimension:
            size.scale(QSize(max_dimension,max_dimension),Qt.KeepAspectRatio)
            reader.setScaledSize(size)
            return reader.read()
        return QImage(str(path))

    def _cancel_current_image_request(self):
        task=getattr(self,"_current_image_task",None)
        if task is not None and hasattr(task,"cancel"):
            task.cancel()
            try:
                if hasattr(self.pool,"tryTake"):
                    self.pool.tryTake(task)
            except RuntimeError:
                pass
        self._current_image_task=None
        self._current_image_generation+=1

    def _current_image_ready(self,result):
        generation,path,image=result
        if (
            generation!=self._current_image_generation
            or str(Path(path))!=self.current_image_path
        ):
            return
        self._current_image_task=None
        if image.isNull():
            self.current_image_preview.clear("Preview unavailable")
            self.current_image_preview.path=Path(path)
            return
        self.current_image_preview.set_image(QPixmap.fromImage(image),path)

    def _current_image_failed(self,_message,generation,path):
        if (
            generation!=self._current_image_generation
            or str(Path(path))!=self.current_image_path
        ):
            return
        self._current_image_task=None
        self.current_image_preview.clear("Preview unavailable")
        self.current_image_preview.path=Path(path)

    def _evidence_subjects(self,selected):
        collected={}
        for task in selected:
            tags=self._task_evidence_tags(task)
            if not tags:
                label=self.friendly_title(task)
                saved=self._example_paths(task)
                if saved:
                    collected.setdefault((label.casefold(),()),[label,(),[]])[2].extend(saved)
                continue
            for tag in tags:
                record=self.tags.find(tag)
                label=record.display if record is not None else tag
                names=self.tags.identity_names(record) if record is not None else (tag,)
                group=tuple(sorted({
                    normalized for value in names
                    if (normalized:=normalize_tag(value))
                }))
                observed=self.discovery.items.get(normalize_tag(tag))
                saved=list(observed.example_images) if observed else []
                if len(tags)==1 and task.image_path:
                    saved.insert(0,task.image_path)
                entry=collected.setdefault((label.casefold(),group),[label,group,[]])
                entry[2].extend(saved)
            if len(tags)>1 and task.image_path:
                label="Saved question evidence"
                collected.setdefault((label.casefold(),()),[label,(),[]])[2].append(task.image_path)
        return tuple(
            (label,group,tuple(dict.fromkeys(paths)))
            for label,group,paths in collected.values()
        )

    @staticmethod
    def _compose_example_result(subjects,indexed,excluded=()):
        excluded_keys={str(Path(value)).casefold() for value in excluded if str(value)}
        group_labels={}
        for label,group,_saved in subjects:
            if group:
                group_labels.setdefault(group,[]).append(label)
        indexed_by_label={label:[] for label,_group,_saved in subjects}
        if indexed is not None:
            for path in indexed.paths:
                for group in indexed.coverage.get(path,()):
                    for label in group_labels.get(group,()):
                        indexed_by_label.setdefault(label,[]).append(path)
        unavailable=set(); sources=[]
        for label,_group,saved in subjects:
            valid=[]
            for path in saved:
                if str(Path(path)).casefold() in excluded_keys:
                    continue
                if _available_file(path):
                    valid.append(path)
                else:
                    unavailable.add(str(Path(path)))
            valid.extend(
                path for path in indexed_by_label.get(label,())
                if str(Path(path)).casefold() not in excluded_keys
            )
            sources.append((label,tuple(dict.fromkeys(valid))))
        result=build_review_example_set(sources)
        return ReviewExampleSet(
            result.paths,result.coverage,result.unavailable_count+len(unavailable),
        )

    def _cancel_example_request(self):
        task=self._example_task
        if task is not None and hasattr(task,"cancel"):
            task.cancel()
            if isinstance(task,AsyncCallTask):
                try:
                    if hasattr(self.pool,"tryTake"):
                        self.pool.tryTake(task)
                except RuntimeError:
                    pass
        self._example_task=None

    def _request_examples(self,selected):
        subjects=self._evidence_subjects(selected)
        excluded=tuple(
            task.image_path for task in selected if getattr(task,"image_path","")
        )
        groups=tuple(group for _label,group,_saved in subjects if group)
        try:index=getattr(getattr(self.state,"dataset",None),"index",None)
        except Exception:index=None
        index_key=self._evidence_queries.request_key(index,groups,False) if index is not None and groups else ""
        key=(tuple((task.id,task.updated_at) for task in selected),subjects,index_key,excluded)
        relevant=bool(selected) and bool(subjects)
        if not relevant:
            self._cancel_example_request()
            self._example_generation+=1; self._example_key=(); self.examples_panel.hide(); return
        self.examples_panel.show()
        if key==self._example_key:return
        self._cancel_example_request()
        self._example_key=key; self._example_generation+=1; generation=self._example_generation
        context=f"Related images for {len(subjects):,} SDXL Tags" if len(subjects)>1 else "Related images"
        self.context_gallery.set_loading(context)
        self._set_related_gallery_empty_state(False)
        self.examples_help.setText(
            "CollectiGen is searching the cached Image Knowledge index."
        )
        def work(job=None):
            indexed=None
            if index is not None and groups:
                progress=(
                    lambda current,total,message:self._background_jobs.update(
                        job.id,current,total,message,
                    )
                ) if job is not None else None
                indexed=self._evidence_queries.query_result(
                    index,groups,False,
                    should_continue=job.checkpoint if job is not None else None,
                    progress=progress,
                )
            return self._compose_example_result(subjects,indexed,excluded)
        if self._background_jobs is not None and index is not None and groups:
            request_hash=hashlib.sha256(repr(key).encode("utf-8")).hexdigest()
            worker=CoordinatedCallTask(
                self._background_jobs,"Search Review Center evidence",work,
                resource="cpu",key=f"review-evidence:{request_hash}",
            )
            self._example_task=worker
            worker.signals.complete.connect(lambda result,g=generation,k=key,n=len(subjects):self._examples_ready(result,g,k,n))
            worker.signals.failed.connect(lambda message,g=generation,k=key,n=len(subjects):self._examples_failed(message,g,k,n))
            worker.signals.cancelled.connect(lambda g=generation,k=key:self._examples_cancelled(g,k))
            worker.start()
            return
        worker=AsyncCallTask(work); self._example_task=worker
        worker.signals.complete.connect(lambda result,g=generation,k=key,n=len(subjects):self._examples_ready(result,g,k,n))
        worker.signals.failed.connect(lambda message,g=generation,k=key,n=len(subjects):self._examples_failed(message,g,k,n))
        self.pool.start(worker)

    def _examples_ready(self,result,generation,key,tag_count):
        if generation!=self._example_generation or key!=self._example_key:return
        self._example_task=None; multiple=tag_count>1
        def configure(path,card):
            self.gallery_status.configure(path,card)
            labels=result.coverage.get(str(Path(path)),())
            if multiple and labels:
                shown=labels[:2]; suffix=f" +{len(labels)-len(shown):,} more" if len(labels)>len(shown) else ""
                card.set_context_note(
                    "Supports: "+", ".join(shown)+suffix,
                    full_text="Evidence for selected SDXL Tags: "+", ".join(labels),
                )
        context=f"Related images for {tag_count:,} SDXL Tags" if multiple else "Related images"
        self.context_gallery.set_paths(result.paths,total_count=len(result.paths),context=context,configure=configure)
        self._set_related_gallery_empty_state(not result.paths)
        unavailable=f" {result.unavailable_count:,} saved path(s) are currently unavailable." if result.unavailable_count else ""
        if result.paths:
            self.examples_help.setText(
                ("Other database images are fairly interleaved across every selected tag; each card names the tag coverage. " if multiple else "These are other database images that match the selected SDXL Tag or one of its aliases. ")
                +"Double-click an image or focus it and press Enter to edit its Image Knowledge."+unavailable
            )
        self.open_image.setEnabled(bool(result.paths))

    def _examples_failed(self,message,generation,key,tag_count):
        if generation!=self._example_generation or key!=self._example_key:return
        self._example_task=None; context=f"Related images for {tag_count:,} SDXL Tags" if tag_count>1 else "Related images"
        self.context_gallery.set_error(message,context)
        self._set_related_gallery_empty_state(False)
        self.examples_help.setText("Related images could not be loaded from the cached Image Knowledge index. No image data changed.")
        self.open_image.setEnabled(False)

    def _examples_cancelled(self,generation,key):
        if generation!=self._example_generation or key!=self._example_key:return
        self._example_task=None

    def _image_data_changed(self,payload):
        details=dict(payload or {})
        images=details.get("images") or ()
        if isinstance(images,(str,Path)):
            images=(images,)
        affected=tuple(dict.fromkeys(
            str(Path(value))
            for value in (
                ([details.get("image")] if details.get("image") else [])
                +list(images)
            )
            if value
        ))
        if not affected:return
        self.gallery_status.invalidate(affected)
        affected_keys={value.casefold() for value in affected}
        refresh=list(
            value for value in self.context_gallery.visible_paths()
            if str(Path(value)).casefold() in affected_keys
        )
        if refresh:self.gallery_status.request(tuple(dict.fromkeys(refresh)))
        if self.current_image_path.casefold() in affected_keys:
            current=self.selected_task()
            if current is not None:
                self._show_current_image(current)
        if not details.get("evidence_changed") or not self.isVisible():return
        selected=self.selected_tasks()
        if not selected:return
        self._cancel_example_request()
        self._example_generation+=1; self._example_key=()
        self._request_examples(selected)

    def hideEvent(self,event):
        self._cancel_current_image_request()
        self._cancel_example_request()
        self._example_generation+=1
        self._example_key=()
        super().hideEvent(event)

    def showEvent(self,event):
        super().showEvent(event)
        selected=self.selected_tasks()
        if selected:
            path=str(getattr(selected[0],"image_path","") or "").strip()
            if path and self.current_image_preview.path!=Path(path):
                self._show_current_image(selected[0])
        self._request_examples(selected)

    def closeEvent(self,event):
        if self.request_background_shutdown():
            event.ignore()
            return
        self._cancel_current_image_request()
        self._cancel_example_request()
        if self._image_data_event_subscribed:
            unsubscribe=getattr(getattr(self.state,"events",None),"unsubscribe",None)
            if callable(unsubscribe):
                unsubscribe("image_data_changed",self._image_data_event_callback)
            self._image_data_event_subscribed=False
        super().closeEvent(event)

    def show_image(self):
        task=self.selected_task(); paths=self._example_paths(task) if task else []
        available=next((path for path in paths if _available_file(path)),None)
        if available:QDesktopServices.openUrl(QUrl.fromLocalFile(available))
        elif task:QMessageBox.information(self,"Image unavailable","The saved related images are no longer available at their recorded locations. The question and its evidence remain saved.")

    def open_example(self,path):
        path=str(path or "")
        if path: QDesktopServices.openUrl(QUrl.fromLocalFile(path))

    def open_in_dataset(self):
        task=self.selected_task()
        if task and task.image_path: self.open_dataset_image.emit(task.image_path)
