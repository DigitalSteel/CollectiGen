from __future__ import annotations

import os
import time
import weakref
from copy import deepcopy
from dataclasses import dataclass,field
from math import ceil
from pathlib import Path

from PySide6.QtCore import QBuffer,QEventLoop,QIODevice,QRect,QSettings,QSignalBlocker,QSize,QThread,QThreadPool,QTimer,Qt,Signal,Slot,QUrl,QUrlQuery
from PySide6.QtGui import QAction,QCloseEvent,QDesktopServices
from PySide6.QtWidgets import (
    QApplication,QComboBox,QDialog,QDialogButtonBox,QFileDialog,QHBoxLayout,QHeaderView,QListWidget,QListWidgetItem,QMenu,
    QMainWindow,QMessageBox,QLabel,QProgressBar,QProgressDialog,QPushButton,QSizePolicy,QStackedWidget,
    QStatusBar,QTextBrowser,QToolButton,QTreeWidget,QTreeWidgetItem,QVBoxLayout,QWidget,QSplitter,
)
from shiboken6 import isValid
from .status_notifications import FooterMessageBox as QMessageBox

from .accessibility import announce_accessibly

from core import (
    AppState,AutomationRuleRepository,CategoryRegistry,DependencyService,DiscoveryRegistry,FoundationKnowledge,IngestAttentionReport,
    CharacterDatabase,CharacterVocabularyResolver,DownstreamUpdateStore,ExternalEvidenceImportCheckpointService,ExternalEvidenceImportCounts,ExternalEvidenceImportPlan,ImageAuthorityInitializationService,OperationJournal,PatternRegistry,
    SOURCE_TASK_TYPES,TAG_BROADER_PROPOSAL_TASK_TYPE,TAG_MEANING_TASK_TYPE,
    CategoryBaseDiscoveryService,ModifierRoleDecisionService,
    TagBroaderDiscoveryService,TagMeaningDiscoveryService,
    TagMeaningEvidenceStore,SuggestionEngine,VocabularyIntakeService,runtime_context,
    classify_system_knowledge_change,
    sensitivity_dependency_groups,
    selected_image_identity as report_selected_image_identity,
)
from core.paths import CATEGORIES_PATH,DEFAULTS_DIR,DISCOVERED_TAGS_PATH,PATTERNS_PATH,RELATIONSHIPS_PATH,SETTINGS_PATH,TAG_MEANING_EVIDENCE_PATH,USER_DATA_DIR
from core.automation_rule_conversion import RulePairAdmission
from core.onboarding import is_genuinely_new_profile,should_offer_onboarding
from core.settings import AppSettings,SettingsRecoveryState
from core.tag_collection import normalize_tag
from core.release_notes import ReleaseNoteEntry,load_release_history
from core.support_bundle import redact_diagnostic_text
from version import APP_TITLE,KNOWLEDGE_VERSION,PRODUCT_NAME,PRODUCT_TAGLINE,PUBLIC_REPOSITORY_URL,SCHEMA_VERSION,VERSION

from .pages import DashboardPage,SetupPage
from .theme import apply_theme
from .tag_editor import refresh_all_tag_appearance_chips
from .dataset_page import DatasetPage,prepare_dataset_page
from .ingest_page import IngestPage
from .generation_page import GenerationPage
from .review_page import (
    ReviewCenterPage,prepare_review_page,review_task_in_library_scope,
)
from .knowledge_page import KnowledgePage,prepare_knowledge_page
from .automation_page import AutomationPage
from .workers import CancelableProgressCallTask,CoordinatedCallTask,DatasetRefreshWorker
from .content_safety import GalleryContentSafetyPolicy
from .gallery import ImageCard,ProtectedImageView,set_content_safety_policy,set_library_identity_resolver
from .components import GlobalStatusStrip,PageHeader,WindowStatusBar,bind_status_footer,set_progress_bar,show_operation_in_progress
from .components import FooterProgressDialog as QProgressDialog
from .vocabulary_dialog import NewVocabularyDialog
from .tag_alias_preflight_dialog import TagAliasPreflightDialog
from .onboarding_dialog import OnboardingDialog
from .bug_report_dialog import BugReportDialog,FeedbackArchiveDialog
from .library_profile_dialog import LibraryScopeDialog
from .brand import about_mark
from .external_evidence_import_dialog import external_evidence_result_dialog
from .category_base_discovery_dialog import CategoryBaseDiscoveryDialog
from .tag_broader_discovery_dialog import TagBroaderDiscoveryDialog
from .update_dialog import confirm_update_candidate


@dataclass
class _ActivityTiming:
    """Per-owner presentation timing; progress events are samples, not heartbeats."""

    started_at: float
    phase_key: str=""
    phase_started_at: float|None=None
    last_advance_at: float|None=None
    scope_total: int=0
    last_current: int=0
    last_sample_at: float|None=None
    rates: list[tuple[float,float]]=field(default_factory=list)
    smoothed_rate: float|None=None

    def observe(
        self,now: float,phase_key: str,current: int,total: int,
    ) -> None:
        current=max(0,int(current)); total=max(0,int(total))
        changed_scope=(
            phase_key!=self.phase_key
            or total!=self.scope_total
            or current<self.last_current
        )
        if current>self.last_current:
            self.last_advance_at=now
        if changed_scope:
            self.phase_started_at=now
            self.phase_key=phase_key
            self.scope_total=total
            self.last_current=current
            self.last_sample_at=now
            self.rates.clear()
            self.smoothed_rate=None
            return
        if self.last_sample_at is None:
            self.last_current=current
            self.last_sample_at=now
            return
        delta=current-self.last_current
        seconds=now-self.last_sample_at
        if delta<=0 or seconds<=0:
            return
        rate=delta/seconds
        self.rates.append((now,rate))
        del self.rates[:-5]
        self.smoothed_rate=(
            rate if self.smoothed_rate is None
            else (self.smoothed_rate*.65)+(rate*.35)
        )
        self.last_current=current
        self.last_sample_at=now

    def eta_seconds(self,current: int,total: int) -> int|None:
        current=max(0,int(current)); total=max(0,int(total))
        if total<=0 or current<=0 or current>=total or len(self.rates)<3:
            return None
        sample_span=self.rates[-1][0]-self.rates[0][0]
        values=[rate for _sampled_at,rate in self.rates]
        mean=sum(values)/len(values)
        if sample_span<2.0 or mean<=0:
            return None
        # Progress is deliberately throttled. Accept only a narrow rate band so
        # uneven batches or a phase transition withhold the visible estimate
        # instead of presenting false precision from the latest interval.
        if max(abs(rate-mean) for rate in values)/mean>.20:
            return None
        rate=self.smoothed_rate
        if rate is None or rate<=0:
            return None
        return max(1,ceil((total-current)/rate))


def _format_activity_duration(seconds: float) -> str:
    value=max(0,int(seconds))
    if value<60:
        return f"{value:,}s"
    minutes,remainder=divmod(value,60)
    if minutes<60:
        return f"{minutes:,}m {remainder:02d}s"
    hours,minutes=divmod(minutes,60)
    return f"{hours:,}h {minutes:02d}m"


def _dialog_accepted(dialog) -> bool:
    """Read a modal result through QDialog's class enum, not an instance attribute."""
    return dialog.exec() == QDialog.Accepted


def feedback_issue_url(report_id: str, summary: str) -> QUrl:
    url=QUrl(f"{PUBLIC_REPOSITORY_URL}/issues/new")
    query=QUrlQuery()
    query.addQueryItem("template","feedback.yml")
    query.addQueryItem("title",f"[{report_id}] {summary}".strip())
    url.setQuery(query)
    return url


def build_feedback_ready_dialog(parent, report_dialog):
    details=f"Saved locally:\n\n{report_dialog.saved_directory}"
    if report_dialog.duplicate_of_report_id:
        details+=(
            "\n\nThis report has the same content fingerprint as "
            f"{report_dialog.duplicate_of_report_id}; both originals were preserved."
        )
    if report_dialog.zip_path is not None:
        details+=(
            "\n\nShareable report package:\n"
            f"{report_dialog.zip_path}\n\n"
            "Choose Open GitHub Feedback below to copy the readable report and "
            "open the public feedback form. Paste the copied report, then attach "
            "this ZIP if you want to include its reviewed technical context. "
            "CollectiGen does not upload anything automatically."
        )
        if report_dialog.tracking_warning:
            details+=(
                "\n\nThe package is valid, but CollectiGen could not record its "
                "export history:\n"+report_dialog.tracking_warning
            )
    else:
        details+=(
            "\n\nChoose Open GitHub Feedback below to copy the readable report "
            "and open the public feedback form. No ZIP was created, but the "
            "readable report can still be submitted."
        )
    if report_dialog.zip_error:
        details+=(
            "\n\nThe report is safe, but its shareable ZIP could not be created:\n"
            +report_dialog.zip_error
        )
    details+=(
        "\n\nGitHub issues are public. Attach a screenshot with Questionable or "
        "Explicit images only after confirming the exact attachment keeps every "
        "such image fully blurred; never attach unblurred pixels or original image "
        "files. Do not submit private images, Local Only data, credentials, "
        "unredacted diagnostics, or security-vulnerability details. A text-only "
        "report can still describe a content-safety problem."
    )
    box=QMessageBox(
        QMessageBox.Information,
        "Feedback Ready",
        details,
        QMessageBox.NoButton,
        parent,
    )
    open_github=box.addButton("Open GitHub Feedback…",QMessageBox.ActionRole)
    copy_path=(
        box.addButton("Copy Package Path",QMessageBox.ActionRole)
        if report_dialog.zip_path is not None
        else None
    )
    open_folder=box.addButton("Open Folder",QMessageBox.ActionRole)
    close=box.addButton(QMessageBox.Close)
    box.setDefaultButton(close)
    return box,open_github,copy_path,open_folder


def about_text() -> str:
    return (
        f"{APP_TITLE}\n"
        f"{PRODUCT_TAGLINE}\n\n"
        "Available in this alpha\n"
        "• Ingest, duplicate checks, Dataset search and organization.\n"
        "• Image Knowledge, optional local AI and training-data export.\n"
        "• Local SDXL 1.0 LoRA training (supported recipe).\n"
        "• Image/video request drafts, reusable setups and source-bound Remix.\n"
        "• Execution needs a ready supported generator and exact models.\n\n"
        "Planned\n"
        "• More generation/video adapters and training recipes.\n\n"
        f"Knowledge version {KNOWLEDGE_VERSION}\n"
        f"Application data schema {SCHEMA_VERSION}\n\n"
        "Original application code: AGPL-3.0-or-later\n"
        "Original documentation and compiled-knowledge contributions: CC-BY-SA-4.0\n"
        "Optional JoyCaption support is Built with Llama and uses separate Llama 3.1 terms.\n"
        "Optional Florence-2 Base FT support is MIT-licensed."
    )


class ReleaseNotesDialog(QDialog):
    def __init__(self,entries: tuple[ReleaseNoteEntry,...],parent=None):
        super().__init__(parent)
        if not entries:
            raise ValueError("No CollectiGen release history is available.")
        self.entries=entries
        self.setWindowTitle(f"What's New in {PRODUCT_NAME}")
        layout=QVBoxLayout(self)
        chooser_row=QHBoxLayout()
        chooser_row.addWidget(QLabel("Release"))
        self.release_chooser=QComboBox()
        self.release_chooser.setAccessibleName("Release history")
        for entry in entries: self.release_chooser.addItem(entry.label)
        chooser_row.addWidget(self.release_chooser,1)
        layout.addLayout(chooser_row)
        self.viewer=QTextBrowser(); self.viewer.setReadOnly(True)
        layout.addWidget(self.viewer,1)
        self.release_chooser.currentIndexChanged.connect(self._show_entry)
        self._show_entry(0)
        buttons=QDialogButtonBox(QDialogButtonBox.Close); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.resize(760,620)

    def _show_entry(self,index: int) -> None:
        if 0<=index<len(self.entries): self.viewer.setMarkdown(self.entries[index].notes)


class _PageLoadingPlaceholder(QWidget):
    """Small honest shell painted before a page's Qt-only widget construction."""

    def __init__(self,page_name: str,parent=None):
        super().__init__(parent)
        layout=QVBoxLayout(self); layout.setContentsMargins(32,32,32,32)
        layout.addStretch(1)
        self.page_name=page_name
        self.label=QLabel(f"Opening {page_name}…")
        self.label.setObjectName("pageTitle")
        self.label.setAlignment(Qt.AlignCenter)
        self.label.setAccessibleName(f"Opening {page_name}")
        self.progress=QProgressBar(); self.progress.setRange(0,0)
        self.progress.setAccessibleName(f"{page_name} loading progress")
        layout.addWidget(self.label)
        layout.addWidget(self.progress)
        layout.addStretch(1)
        bind_status_footer(self,self.label,self.progress,follow_visibility=True)

    def set_progress(self,current: int,total: int,message: str="") -> None:
        if total>0:
            self.progress.setRange(0,total); self.progress.setValue(current)
        else:
            self.progress.setRange(0,0)
        text=str(message or f"Opening {self.page_name}…")
        self.label.setText(text)
        self.label.setAccessibleName(text)

    def set_error(self,message: str) -> None:
        self.progress.setRange(0,1); self.progress.setValue(0)
        self.label.setObjectName("errorLabel")
        self.label.setText(
            f"{self.page_name} could not be prepared. Select another section "
            f"and retry. {message}"
        )
        self.label.setAccessibleName(f"{self.page_name} could not be prepared")


def authority_issue_dialog(result) -> tuple[str,str]:
    """Describe partial initialization honestly without conflating warnings and failures."""
    metadata=[item for item in result.issues if item.code=="metadata_unavailable"]
    blocking=[item for item in result.issues if item.blocking]
    lines=[]
    shown_blocking=blocking[:6]
    for item in shown_blocking:
        message=item.message
        lines.append(f"- {item.image.name}: {message}\n  Recovery: {item.recovery}")
    if len(blocking)>len(shown_blocking):
        lines.append(f"- {len(blocking)-len(shown_blocking):,} more image(s) could not be initialized.")
    grouped_metadata={}
    for item in metadata:
        message=item.message.replace("Image Knowledge was initialized, but ","",1)
        key=(message,item.recovery)
        grouped_metadata.setdefault(key,[]).append(item.image.name)
    shown_metadata=list(grouped_metadata.items())[:4]
    for (message,recovery),names in shown_metadata:
        examples=", ".join(names[:3])
        suffix=f"; examples: {examples}" if examples else ""
        lines.append(f"- {message} ({len(names):,} image(s){suffix})\n  Recovery: {recovery}")
    hidden_metadata=sum(len(names) for _,names in list(grouped_metadata.items())[4:])
    if hidden_metadata:
        lines.append(f"- {hidden_metadata:,} more image(s) have other Metadata read problems.")
    if metadata and not blocking:
        title="Some Metadata could not be read"
        lead=(
            f"Image Knowledge was created or updated, and the image files were not changed. "
            f"Metadata could not be imported for {len(metadata):,} image(s).\n\n"
        )
    elif blocking and metadata:
        title="Some image data needs attention"
        lead=(
            f"{len(blocking):,} image(s) could not be initialized and were left unchanged. "
            f"Another {len(metadata):,} image(s) have Image Knowledge, but their Metadata could not be read.\n\n"
        )
    else:
        title="Some image data could not be initialized"
        lead=f"{PRODUCT_NAME} left the affected image data unchanged.\n\n"
    return title,lead+"\n".join(lines)


def settings_recovery_dialog_content(settings: AppSettings) -> tuple[str,str,str]:
    """Return bounded startup disclosure without exposing invalid settings content."""
    report=settings.load_report
    unchanged=(
        "No library files, Image Knowledge, System Knowledge, TXT, Metadata, source "
        "images, models, or optional components were changed."
    )
    if report.state is SettingsRecoveryState.RECOVERED:
        return (
            "Settings recovered",
            "CollectiGen could not accept the active settings. It preserved the exact "
            "invalid file and restored the newest completely valid settings history.\n\n"
            +unchanged,
            "Preserved invalid settings:\n"
            f"{report.quarantine_path}\n\nRestored history:\n{report.recovered_from}\n\n"
            "Up to ten verified settings-recovery records are retained locally.",
        )
    if report.state is SettingsRecoveryState.UNAVAILABLE:
        return (
            "Settings are unavailable",
            "CollectiGen could not read the active settings. Safe defaults exist only in "
            "memory; no settings can be saved and no library will be opened. Restore "
            "access to the settings file, then restart CollectiGen.\n\n"+unchanged,
            report.detail,
        )
    return (
        "Settings need recovery",
        "CollectiGen could not accept the active settings and found no completely valid "
        "history it could restore automatically. The original file is unchanged, safe "
        "defaults exist only in memory, and no library will be opened.\n\n"
        "Choose Use Safe Defaults to preserve the exact invalid file before writing a "
        "verified default settings file, or close CollectiGen and repair the file manually.\n\n"
        +unchanged,
        report.detail,
    )


def build_settings_recovery_dialog(settings: AppSettings,parent=None):
    """Build the exact bounded recovery decision used before app-state startup."""
    report=settings.load_report
    title,message,details=settings_recovery_dialog_content(settings)
    if report.state is SettingsRecoveryState.RECOVERED:
        box=QMessageBox(QMessageBox.Information,title,message,QMessageBox.Ok,parent)
        box.setDefaultButton(QMessageBox.Ok)
        box.setAccessibleName("Settings recovery completed")
        box.setAccessibleDescription(message)
        box.setDetailedText(details)
        return box,None,None
    box=QMessageBox(QMessageBox.Warning,title,message,QMessageBox.NoButton,parent)
    use_defaults=box.addButton("Use Safe Defaults",QMessageBox.AcceptRole)
    close=box.addButton("Close CollectiGen",QMessageBox.RejectRole)
    use_defaults.setEnabled(report.can_use_safe_defaults)
    use_defaults.setAccessibleName("Use Safe Defaults")
    use_defaults.setAccessibleDescription(
        "Preserve the exact invalid settings, then write and verify safe defaults."
    )
    close.setAccessibleName("Close CollectiGen without changing settings")
    box.setDefaultButton(close); box.setEscapeButton(close)
    box.setAccessibleName("Settings recovery required")
    box.setAccessibleDescription(message)
    box.setDetailedText(details)
    return box,use_defaults,close


def run_settings_recovery_preflight(
    settings: AppSettings,
    parent,
    *,
    settings_path: Path=SETTINGS_PATH,
) -> bool:
    """Resolve settings authority before any other persistent app state is opened."""
    report=settings.load_report
    if report.state in {SettingsRecoveryState.CLEAN,SettingsRecoveryState.SAFE_DEFAULTS}:
        return True
    box,use_defaults,_close=build_settings_recovery_dialog(settings,parent)
    if report.state is SettingsRecoveryState.RECOVERED:
        box.exec()
        return True
    box.exec()
    if box.clickedButton() is not use_defaults:
        return False
    try:
        completed=settings.use_safe_defaults(settings_path)
    except OSError as exc:
        QMessageBox.critical(
            parent,
            "Settings recovery did not complete",
            f"{exc}\n\nThe library was not opened and no other data was changed.",
        )
        return False
    QMessageBox.information(
        parent,
        "Safe defaults saved",
        "Safe defaults were saved after the exact invalid settings were preserved at:\n\n"
        f"{completed.quarantine_path}\n\n"
        "No library files, Image Knowledge, System Knowledge, TXT, Metadata, source "
        "images, models, or optional components were changed.",
    )
    return True


class TagManagerWindow(QMainWindow):
    long_operation_changed=Signal(bool,str,str,int,int,str)
    derived_refresh_event=Signal(object)
    PAGE_NAMES=("Dashboard","Ingest","Dataset","Review Center","Tag Encyclopedia","Rules & Automation","Setup","Generate","Training")
    NAVIGATION_PAGE_ORDER=(0,1,2,3,4,7,8,5,6)
    NAVIGATION_MODES=("expanded","compact")

    def __init__(
        self,settings: AppSettings | None=None,*,state: AppState | None=None,
        genuinely_new_profile: bool | None=None,
    ):
        super().__init__(); self.setWindowTitle(APP_TITLE)
        self.startup_cancelled=False
        self.ui_test=os.environ.get("TAG_MANAGER_UI_TEST")=="1"
        # Keep the comfortable workstation default while allowing adaptive pages
        # to use a smaller laptop-sized window without an artificial width floor.
        self.setMinimumSize(900,600); self.resize(1600,900)
        genuinely_new=(
            is_genuinely_new_profile(USER_DATA_DIR,SETTINGS_PATH)
            if genuinely_new_profile is None else
            bool(genuinely_new_profile)
        )
        self.settings=settings or AppSettings.load()
        if self.settings.load_report.requires_decision and self.ui_test:
            raise RuntimeError(
                "The isolated visual-test settings require recovery; no application "
                "state was opened."
            )
        if not self.ui_test and not run_settings_recovery_preflight(self.settings,self):
            self.startup_cancelled=True
            return
        self.state=state or AppState(settings=self.settings)
        self.state._bind_knowledge_authority_thread()
        self.settings=self.state.settings
        from .tooltips import apply_tooltip_policy
        apply_tooltip_policy(QApplication.instance(),self.settings.show_tooltips)
        self.downstream_updates=DownstreamUpdateStore(
            USER_DATA_DIR/"downstream_updates.json"
        )
        set_library_identity_resolver(self._gallery_library_identity)
        self._startup_onboarding=should_offer_onboarding(self.settings,genuinely_new=genuinely_new)
        self._onboarding_dialog=None
        self.content_safety=GalleryContentSafetyPolicy(self.settings,self.state.knowledge,self.state.dataset,CategoryRegistry(DEFAULTS_DIR/"categories.json",CATEGORIES_PATH)); set_content_safety_policy(self.content_safety)
        self.qt_settings=QSettings(str(USER_DATA_DIR/"qt_ui.ini"),QSettings.IniFormat)
        self._scan_thread=None; self._scan_worker=None; self._library_scan_active=False
        self._pending_completed_dataset_image=None
        self._library_refresh_queue=[]; self._library_refresh_current=None
        self._library_refresh_failures=[]; self._library_refresh_cancelled=False
        self._library_refresh_paths_by_id={}; self._library_refresh_kind="library"
        self._library_refresh_evidence_mode=""; self._library_refresh_evidence_sources=("txt","metadata")
        self._library_refresh_run_automatic_ai=True; self._library_refresh_scope_label=""
        self._library_refresh_held=0
        self._library_refresh_evidence_checkpoint_service=None
        self._library_refresh_evidence_checkpoint_session=None
        self._scan_cached_ready=False; self._last_library_outcome=""
        self._recommended_action="choose_library"
        self._pending_vocabulary_items=[]
        self._vocabulary_task=None
        self._pending_startup_derived_paths=set()
        self._startup_derived_refresh_scheduled=False
        self._close_after_scan=False
        self._close_after_ingest=False
        self._close_after_dataset=False
        self._close_after_knowledge=False
        self._close_after_automation=False
        self._close_after_review=False
        self._close_with_unsaved_confirmed=False
        self._startup_ingest_refresh_pending=True
        self._ingest_attention_report=None
        self._content_safety_task=None; self._pending_content_safety_paths=set(); self._forced_content_safety_paths=set(); self._content_safety_pool=QThreadPool.globalInstance()
        self._long_operation_events={}; self._displayed_long_operation_owner=""
        self._long_operation_cancel_pending_owner=""
        self._long_operation_timings={}
        self._activity_timing_timer=QTimer(self)
        self._activity_timing_timer.setInterval(1000)
        self._activity_timing_timer.timeout.connect(
            self._refresh_activity_timing
        )
        self._content_sensitivity_scope_task=None; self._pending_content_sensitivity_tag_ids=set(); self._pending_content_sensitivity_category_ids=set(); self._pending_content_sensitivity_identity_groups=set(); self._content_sensitivity_refresh_scheduled=False
        self._startup_update_task=None
        self._pending_startup_update_candidate=None
        self._knowledge_page_task=None
        self._knowledge_page_preparation=None
        self._dataset_page_task=None
        self._dataset_page_preparation=None
        self._dataset_preparation_shutdown=False
        self._pending_dataset_page_actions=[]
        self._review_page_task=None
        self._review_page_preparation=None
        self._pending_review_page_actions=[]
        self._defer_expensive_page_initialization=(
            not self.ui_test
            or os.environ.get("TAG_MANAGER_PROFILE_NAVIGATION")=="1"
        )
        self._defer_knowledge_page_initialization=(
            self._defer_expensive_page_initialization
        )
        # Initial Library navigation can render recommendations during UI construction.
        self.refresh_review_count()
        self._build_ui(); self._build_menu()
        from .workspace_commands import WorkspaceCommandController
        self.command_controller=WorkspaceCommandController(self)
        self._job_status_timer=QTimer(self)
        self._job_status_timer.setInterval(250)
        self._job_status_timer.timeout.connect(self._sync_job_activity)
        self._job_status_timer.start()
        self._ai_runtime_timer=QTimer(self)
        self._ai_runtime_timer.setInterval(2000)
        self._ai_runtime_timer.timeout.connect(self._maintain_ai_runtimes)
        self._ai_runtime_timer.start()
        self.long_operation_changed.connect(self._show_long_operation)
        self.derived_refresh_event.connect(self._derived_refresh_changed)
        self._derived_refresh_event_callback=(
            lambda **payload:self.derived_refresh_event.emit(dict(payload))
        )
        window_ref=weakref.ref(self)
        def deliver_long_operation(active,owner,label,current,total,message,**_extra):
            window=window_ref()
            if window is not None and isValid(window):
                window.long_operation_changed.emit(
                    active,owner,label,current,total,message,
                )
        self._long_operation_event_callback=deliver_long_operation
        self.state.events.subscribe(
            "long_operation_changed",deliver_long_operation,
        )
        self.state.events.subscribe(
            "derived_refresh_changed",self._derived_refresh_event_callback,
        )
        QTimer.singleShot(0,self.state.setup_queue.start_pending)
        if not self.ui_test:
            self._restore_window()
            QTimer.singleShot(0,self._ensure_window_on_screen)
        self.refresh_review_count()
        if not self.ui_test:
            self.statusBar().showMessage("Opening the remembered library in the background…")
            if self._startup_onboarding:
                self.statusBar().showMessage("Guided setup is ready; no library has been scanned")
                QTimer.singleShot(250,lambda:self.open_guided_setup(startup=True))
            else:
                if self.settings.check_dependencies_on_startup:
                    QTimer.singleShot(700,self.check_startup_dependencies)
                if self.settings.check_updates_on_startup:
                    QTimer.singleShot(1400,self.check_startup_updates)
                QTimer.singleShot(250,self.restore_library)
        else: self._update_recommended()

    def open_dashboard_guided_review(self):
        """Open the library-wide guided wizard without changing page context."""
        self._ensure_page(3)
        if hasattr(self,"review_page"):
            self.review_page.open_guided_review(parent=self)
            return
        self._pending_review_page_actions.append(
            lambda:self.review_page.open_guided_review(parent=self)
        )

    def open_guided_setup(self,*,startup=False,initial_step: str=""):
        if self._onboarding_dialog is not None:
            self._onboarding_dialog.raise_(); self._onboarding_dialog.activateWindow(); return
        source_import_was_enabled=bool(
            self.settings.auto_import_source_evidence
        )
        dialog=OnboardingDialog(
            self.settings,self,settings_path=SETTINGS_PATH,reentry=not startup,
            initial_step=initial_step,
        )
        self._onboarding_dialog=dialog
        try: dialog.exec()
        finally: self._onboarding_dialog=None
        if hasattr(self,"setup_page"):
            self.setup_page.refresh_from_settings()
        self.refresh_preparation_defaults()
        if hasattr(self,"automation_page"):
            self.automation_page.sync_reusable_settings()
        update_dashboard=getattr(self,"_update_recommended",None)
        if callable(update_dashboard):
            update_dashboard()
        if (
            not startup
            and not source_import_was_enabled
            and self.settings.auto_import_source_evidence
        ):
            self.apply_source_automation_now()
        if not startup:
            if dialog.open_components_requested:
                self.navigation.setCurrentRow(6)
                self.setup_page.show_section("ai","components")
                self.setup_page.header.set_status("Review optional components; no download starts without confirmation")
            return
        self._startup_onboarding=False
        if dialog.outcome=="completed":
            if dialog.open_components_requested:
                self.navigation.setCurrentRow(6)
                self.setup_page.show_section("ai","components")
                self.setup_page.header.set_status("Review optional components; the library has not been opened yet")
            else:
                self.restore_library()
            if self.settings.check_dependencies_on_startup:
                QTimer.singleShot(700,self.check_startup_dependencies)
        else:
            self.statusBar().showMessage(
                "Ready • guided setup can be continued from Setup; no library was scanned",12000,
            )
            self._update_recommended()

    def check_startup_dependencies(self):
        missing=[
            item for item in DependencyService(self.settings).components(include_sizes=False)
            if item.required and not item.installed
        ]
        if not missing: return
        details="\n".join(f"• {item.name} — {item.purpose}" for item in missing)
        if QMessageBox.question(
            self,f"Finish {PRODUCT_NAME} setup",
            "Required application components are missing:\n\n"+details+"\n\nOpen the native installer now?",
        )==QMessageBox.Yes:
            self.navigation.setCurrentRow(6)
            self.setup_page.open_dependencies("application")

    def check_startup_updates(self):
        """Run the user's opt-in metadata check without downloading anything."""
        if (
            self._startup_update_task is not None
            or not self.settings.check_updates_on_startup
            or self.state.jobs.shutting_down
        ):
            return
        if QApplication.activeModalWidget() is not None:
            QTimer.singleShot(1000,self.check_startup_updates)
            return
        from core.application_update import ApplicationUpdateService
        service=ApplicationUpdateService(Path(__file__).resolve().parents[1],VERSION)
        if not service.installed_package:
            return
        channel=self.settings.update_channel
        self.statusBar().showMessage("Checking for application updates…")
        task=CoordinatedCallTask(
            self.state.jobs,"Checking for application updates",
            lambda _job:service.check(channel),resource="io",
            key="application-update:startup-check",
        )
        self._startup_update_task=task
        task.signals.complete.connect(self._startup_update_check_complete)
        task.signals.failed.connect(self._startup_update_check_failed)
        task.signals.cancelled.connect(
            lambda:self._startup_update_check_failed("The update check was cancelled.")
        )
        task.start()

    def _startup_update_check_complete(self,candidate):
        self._startup_update_task=None
        if candidate is None:
            self.statusBar().showMessage(
                "This installation is current for its update channel",8000,
            )
            return
        version=candidate.release.version
        self.statusBar().showMessage(f"CollectiGen v{version} is available")
        self._pending_startup_update_candidate=candidate
        self._offer_startup_update_candidate()

    def _offer_startup_update_candidate(self):
        candidate=self._pending_startup_update_candidate
        if candidate is None:
            return
        if QApplication.activeModalWidget() is not None:
            QTimer.singleShot(1000,self._offer_startup_update_candidate)
            return
        self._pending_startup_update_candidate=None
        version=candidate.release.version
        if not confirm_update_candidate(candidate,self):
            self.statusBar().showMessage(
                f"CollectiGen v{version} is available; update postponed",12000,
            )
            return
        self.navigation.setCurrentRow(6)
        self.setup_page.show_section("maintenance","updates")
        self.setup_page.prepare_update(candidate)

    def _maintain_ai_runtimes(self):
        maintain=getattr(self.state,"maintain_ai_runtimes",None)
        if not callable(maintain):
            return
        settled=not (
            self.ui_test or getattr(self,"_startup_onboarding",False)
            or getattr(self,"_startup_ingest_refresh_pending",False)
            or getattr(self,"_library_scan_active",False)
            or getattr(self,"_startup_derived_refresh_scheduled",False)
            or getattr(self,"_close_after_scan",False)
        )
        maintain(allow_preload=settled)

    def _startup_update_check_failed(self,_message):
        self._startup_update_task=None
        self.statusBar().showMessage(
            "The startup update check could not finish; use Setup to try again",
            12000,
        )

    def _build_ui(self):
        central=QWidget(); self.setCentralWidget(central)
        layout=QVBoxLayout(central); layout.setContentsMargins(0,0,0,0); layout.setSpacing(0)
        self.navigation_panel=QWidget()
        navigation_panel_layout=QVBoxLayout(self.navigation_panel)
        navigation_panel_layout.setContentsMargins(4,4,4,4)
        navigation_panel_layout.setSpacing(4)
        navigation_control=QWidget(); navigation_control.setFixedHeight(32)
        navigation_control_row=QHBoxLayout(navigation_control); navigation_control_row.setContentsMargins(0,0,0,0)
        self.navigation_mode_button=QToolButton()
        self.navigation_mode_button.setObjectName("navigationModeButton")
        self.navigation_mode_button.setCheckable(True)
        self.navigation_mode_button.setFocusPolicy(Qt.StrongFocus)
        self.navigation_mode_button.setProperty("compact",True)
        self.navigation_mode_button.setFixedSize(40,32)
        self.navigation_mode_button.setAccessibleName("Change navigation display")
        self.navigation_mode_button.clicked.connect(self.cycle_navigation_mode)
        navigation_control_row.addStretch(1)
        navigation_control_row.addWidget(self.navigation_mode_button)
        navigation_control_row.addStretch(1)
        navigation_panel_layout.addWidget(navigation_control)
        from .navigation_icons import NavigationItemDelegate,PageNavigationList,navigation_icon
        self.navigation=PageNavigationList(); self.navigation.setObjectName("navigation")
        self.navigation_delegate=NavigationItemDelegate(self.navigation)
        self.navigation.setItemDelegate(self.navigation_delegate)
        self.navigation.setAccessibleName("Application sections")
        self.navigation.setAccessibleDescription(
            "Choose an application section. Drag the divider beside this list "
            "to adjust the navigation width."
        )
        self.navigation.setMinimumWidth(145)
        self.navigation.setMaximumWidth(230)
        self.navigation.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.navigation.setSpacing(2)
        for page_index in self.NAVIGATION_PAGE_ORDER:
            name=self.PAGE_NAMES[page_index]
            item=QListWidgetItem(navigation_icon(name),name)
            item.setData(Qt.UserRole,name)
            item.setData(PageNavigationList.PAGE_INDEX_ROLE,page_index)
            item.setTextAlignment(Qt.AlignVCenter|Qt.AlignLeft)
            item.setToolTip(name)
            self.navigation.addItem(item)
        navigation_readable_width=(
            max(
                self.navigation.fontMetrics().horizontalAdvance(name)
                for name in self.PAGE_NAMES
            )+80
        )
        self.navigation.setMaximumWidth(max(
            self.navigation.maximumWidth(),navigation_readable_width,
        ))
        self._expanded_navigation_maximum=self.navigation.maximumWidth()
        navigation_panel_layout.addWidget(self.navigation,1)
        self.stack=QStackedWidget()
        self.stack.setMinimumSize(0,0)
        self.stack.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Ignored)
        self.dashboard=DashboardPage(); self.dashboard.recommended.connect(self.run_recommended)
        self.dashboard.vocabulary_requested.connect(self._open_new_vocabulary)
        self.dashboard.downstream_updates_requested.connect(
            self.review_downstream_updates
        )
        self.dashboard.tag_recovery_requested.connect(self.review_tag_update_recovery)
        self.dashboard.guided_review_requested.connect(
            self.open_dashboard_guided_review
        )
        self.dashboard.setup_attention_requested.connect(
            lambda:self.open_guided_setup(initial_step="locations")
        )
        self.dashboard.swarm_setup_requested.connect(self.open_swarm_setup)
        self.dashboard.generation_requested.connect(self.open_generation_workspace)
        self._refresh_downstream_update_notice()
        self._prepare_tag_recovery_notice()
        self.stack.addWidget(self.dashboard)
        # Heavy workflow trees are created only when opened. Their named shells
        # paint first so unavoidable Qt-only construction is never a blank pause.
        for name in self.PAGE_NAMES[1:]:
            self.stack.addWidget(_PageLoadingPlaceholder(name))
        self.shell_splitter=QSplitter(Qt.Horizontal)
        self.shell_splitter.setObjectName("shellSplitter")
        self.shell_splitter.setAccessibleName("Navigation and page divider")
        self.shell_splitter.setAccessibleDescription(
            "Resize the application navigation without changing the current page."
        )
        self.shell_splitter.setChildrenCollapsible(False)
        self.shell_splitter.setHandleWidth(5)
        self.shell_splitter.addWidget(self.navigation_panel)
        self.shell_splitter.addWidget(self.stack)
        self.shell_splitter.setStretchFactor(0,0)
        self.shell_splitter.setStretchFactor(1,1)
        self.shell_splitter.splitterMoved.connect(self._remember_navigation_width)
        navigation_default_width=max(
            158,
            min(self.navigation.maximumWidth(),navigation_readable_width),
        )
        navigation_width=navigation_default_width
        if not self.ui_test:
            try:
                navigation_width=int(self.qt_settings.value(
                    "main/navigation_width",navigation_width,
                ))
            except (TypeError,ValueError):
                navigation_width=navigation_default_width
        navigation_width=max(
            self.navigation.minimumWidth(),
            min(self.navigation.maximumWidth(),navigation_width),
        )
        self._expanded_navigation_width=navigation_width
        self.shell_splitter.setSizes((navigation_width,1442))
        layout.addWidget(self.shell_splitter,1)
        navigation_mode=str(self.qt_settings.value(
            "main/navigation_mode","expanded",
        ) or "expanded").strip().casefold()
        self.set_navigation_mode(
            navigation_mode,persist=navigation_mode.strip().casefold()=="hidden",
        )
        self.navigation.pageChanged.connect(self.change_page); self.navigation.setCurrentRow(0)
        status=WindowStatusBar(); self.setStatusBar(status)
        self.global_status_strip=GlobalStatusStrip()
        self.ingest_attention_button=self.global_status_strip.ingest_attention_button
        self.ingest_attention_button.setAccessibleName("Ingest needs attention")
        self.ingest_attention_button.clicked.connect(self._open_ingest_attention)
        self.library_scope_button=self.global_status_strip.library_scope_button
        self.library_scope_button.setAccessibleName("Image library filter")
        self.library_scope_button.clicked.connect(self.choose_library)
        self.activity_label=self.global_status_strip.activity_label
        self.activity_label.setAccessibleName("Background activity")
        self.progress=self.global_status_strip.progress
        self.progress.setAccessibleName("Background progress")
        self.scan_cancel_button=self.global_status_strip.cancel_button
        self.scan_cancel_button.clicked.connect(self._cancel_library_scan); self.scan_cancel_button.hide()
        status.footer.set_global_activity(self.global_status_strip)
        self.activity_queue=QTreeWidget()
        self.activity_queue.setHeaderLabels(("Task","State","Progress or waiting reason"))
        self.activity_queue.header().setSectionResizeMode(0,QHeaderView.Stretch)
        self.activity_queue.header().setSectionResizeMode(1,QHeaderView.ResizeToContents)
        self.activity_queue.header().setSectionResizeMode(2,QHeaderView.Stretch)
        self.activity_queue.setRootIsDecorated(False)
        self.activity_queue.setMaximumHeight(150); self.activity_queue.hide()
        self.activity_queue.setAccessibleName("Background tasks and queue")
        self.activity_queue_button=QPushButton("Tasks…")
        self.activity_queue_button.setCheckable(True)
        self.activity_queue_button.toggled.connect(self.activity_queue.setVisible)
        self.global_status_strip.button_layout.addWidget(self.activity_queue_button)
        status.footer.add_status_widget(self.activity_queue)
        if hasattr(self,"_refresh_library_scope_label"):
            self._refresh_library_scope_label()

    def _show_long_operation(self,active,owner,label,current,total,message,*,render=True):
        events=getattr(self,"_long_operation_events",None)
        if events is None:
            events={}; self._long_operation_events=events
        timings=getattr(self,"_long_operation_timings",None)
        if timings is None:
            timings={}; self._long_operation_timings=timings
        owner=str(owner or "")
        if active:
            # Updating progress must not change a live owner's display order.
            # New owners still enter first; finished owners leave explicitly.
            event=(str(label),max(0,int(current)),max(0,int(total)),str(message))
            events[owner]=event
            now=time.monotonic()
            timing=timings.get(owner)
            if timing is None:
                timing=_ActivityTiming(now); timings[owner]=timing
            phase_key=(
                str(getattr(self,"_scan_phase_label","") or label)
                if owner=="library" else str(message or label).split(";",1)[0]
            )
            timing.observe(now,phase_key,event[1],event[2])
        elif getattr(self,"_long_operation_cancel_pending_owner","")==owner:
            self._long_operation_cancel_pending_owner=""
        if not active:
            events.pop(owner,None)
            timings.pop(owner,None)
        timer=getattr(self,"_activity_timing_timer",None)
        if timer is not None:
            if events and not timer.isActive():timer.start()
            elif not events and timer.isActive():timer.stop()
        if render:
            TagManagerWindow._render_long_operation(self)
        if not active:
            if self._pending_content_safety_paths and self._content_safety_task is None:
                QTimer.singleShot(0,self._resume_content_safety)

    def _sync_job_activity(self):
        """Include directly coordinated scans and waiting jobs in shell feedback.

        Qt-owned external reservations already publish their owner events. A
        bounded coordinator snapshot fills the formerly invisible direct-job
        lane without submitting work, inspecting images, or changing admission.
        """
        jobs=self.state.jobs.active()
        queue=getattr(self.state,"setup_queue",None)
        operations=queue.snapshot() if queue is not None else ()
        setup_job_ids={getattr(item,"job_id","") for item in operations}
        # Setup already publishes its running phase as the dependencies owner.
        managed={f"job:{job.id}":job for job in jobs
                 if not job.external and job.id not in setup_job_ids}
        previous=getattr(self,"_managed_activity_snapshots",{})
        current={}
        self._queued_activity_owners={
            owner for owner,job in managed.items() if job.state=="queued"
        }
        for owner in previous.keys()-managed.keys():
            self._show_long_operation(False,owner,"",0,0,"",render=False)
        for owner,job in sorted(managed.items(),key=lambda item:item[1].state!="queued"):
            snapshot=(job.name,int(job.current),int(job.total),str(job.message),job.state)
            current[owner]=snapshot
            if previous.get(owner)!=snapshot:
                name,count,total,message,state=snapshot
                if state=="running" and total and count>=total:
                    total=0
                    message=f"Finishing {name}; {count:,} work steps reported"
                self._show_long_operation(
                    True,owner,(f"Queued: {name}" if state=="queued" else name),
                    count,total,message or ("Waiting for background capacity" if state=="queued" else name),
                    render=False,
                )
        self._managed_activity_snapshots=current
        def job_detail(job):
            count=(f"{job.current:,} / {job.total:,}" if job.total else "")
            phase=("Waiting for background capacity" if job.state=="queued"
                   else str(job.message or ""))
            reason=str(getattr(job,"reason","") or "")
            waited=int(getattr(job,"queue_wait_seconds",0))
            if waited:phase+=f"; background queue wait: {waited}s (excluded from inference ETA)"
            return " • ".join(dict.fromkeys(value for value in (count,reason,phase) if value)) or "Total not yet known"
        job_by_id={job.id:job for job in jobs}
        rows=[(f"job:{job.id}",job.name,"Waiting" if job.state=="queued" else job.state.title(),job_detail(job))
              for job in jobs if job.id not in setup_job_ids]
        for item in operations:
            job=job_by_id.get(getattr(item,"job_id",""))
            if item.state not in {"waiting","running"} and job is None:continue
            waiting=item.state=="waiting" or job is not None and job.state=="queued"
            rows.append((f"setup:{item.id}",item.label,"Waiting" if waiting else "Running",
                         item.detail or "Waiting for earlier setup work" if waiting
                         else job_detail(job) if job is not None else item.detail))
        previous_rows=getattr(self,"_activity_queue_rows",None) or ()
        previous_ids={row[0] for row in previous_rows}; current_ids={row[0] for row in rows}
        finished=(0 if not previous_ids and current_ids else
                  getattr(self,"_activity_batch_finished",0)+len(previous_ids-current_ids))
        self._activity_batch_finished=finished
        strip=getattr(self,"global_status_strip",None)
        if strip is not None:
            total=finished+len(rows)
            waiting=sum(row[2]=="Waiting" for row in rows)
            strip.overall_label.setText(f"Background jobs • {len(rows)-waiting} active • {waiting} waiting")
            strip.overall_progress.setRange(0,max(1,total))
            strip.overall_progress.setValue(finished)
            strip.overall_progress.setFormat(f"{finished}/{total} known jobs finished")
            detail=("Counts jobs submitted so far, not all files or future workflow steps. "
                    "The operation window shows its current phase and file progress. "
                    "Finished includes completed, cancelled or failed work. New jobs update the total; "
                    "the count starts again when a new batch begins after the queue is empty.")
            strip.overall_progress.setToolTip(detail)
            strip.overall_progress.setAccessibleDescription(detail)
            strip.overall_row.setVisible(bool(rows))
        if rows!=getattr(self,"_activity_queue_rows",None):
            self._activity_queue_rows=rows
            items=getattr(self,"_activity_queue_items",{})
            retained={row[0] for row in rows}
            for identity in items.keys()-retained:
                item=items.pop(identity)
                self.activity_queue.takeTopLevelItem(self.activity_queue.indexOfTopLevelItem(item))
            for identity,*display in rows:
                item=items.get(identity)
                if item is None:
                    item=QTreeWidgetItem(display); item.setData(0,Qt.UserRole,identity)
                    items[identity]=item; self.activity_queue.addTopLevelItem(item)
                else:
                    for column,value in enumerate(display):
                        if item.text(column)!=value:item.setText(column,value)
                for column,value in enumerate(display):item.setToolTip(column,value)
            self._activity_queue_items=items
            self.activity_queue_button.setText(f"Tasks ({len(rows)})…")
            self.activity_queue_button.setMinimumWidth(self.activity_queue_button.sizeHint().width())
            self.activity_queue_button.setEnabled(bool(rows))
            if not rows:self.activity_queue_button.setChecked(False)
        # Render one complete coordinator snapshot, including the empty state.
        self._render_long_operation()

    def _render_long_operation(self):
        events=getattr(self,"_long_operation_events",{})
        strip=getattr(self,"global_status_strip",None)
        if not events:
            if strip is not None:strip.secondary_row.hide()
            self._displayed_long_operation_owner=""
            self.activity_label.hide(); self.progress.hide()
            button=getattr(self,"scan_cancel_button",None)
            if button is not None:button.hide()
            return
        queued=getattr(self,"_queued_activity_owners",set())
        running=[item for item in events.items() if item[0] not in queued]
        if strip is not None:strip.secondary_row.setVisible(len(running)>1)
        if not running:
            self._displayed_long_operation_owner=""
            self.activity_label.hide(); self.progress.hide()
            button=getattr(self,"scan_cancel_button",None)
            if button is not None:button.hide()
            return
        owner,(label,current,total,message)=running[-1]
        if strip is not None and len(running)>1:
            other_owner,(other_label,other_current,other_total,other_message)=running[-2]
            strip.secondary_label.setText(f"Active task • {other_label}")
            timing=getattr(self,"_long_operation_timings",{}).get(other_owner)
            elapsed=_format_activity_duration(max(0,time.monotonic()-timing.started_at) if timing else 0)
            detail=f"{other_label}\n{other_message}\nElapsed: {elapsed}"
            if other_total:detail+=f"\n{other_current:,} of {other_total:,}"
            strip.secondary_label.setToolTip(detail)
            strip.secondary_label.setAccessibleDescription(detail)
            set_progress_bar(strip.secondary_progress,other_current,other_total,detail=detail,
                             unknown_text=f"Elapsed {elapsed}")
            if other_total:
                percent=round(max(0,min(other_current,other_total))*100/other_total)
                candidates=(f"{percent}% • {other_current:,}/{other_total:,} • Elapsed {elapsed}",
                            f"{percent}% • Elapsed {elapsed}")
                available=strip.secondary_progress.contentsRect().width()-12
                strip.secondary_progress.setFormat(next((
                    text for text in candidates
                    if strip.secondary_progress.fontMetrics().horizontalAdvance(text)<=available
                ),candidates[-1]))
            strip.secondary_progress.setAccessibleDescription(detail)
            strip.secondary_row.show()
        self._displayed_long_operation_owner=owner
        status_getter=getattr(self,"statusBar",None)
        if owner=="library" and callable(status_getter):
            status=status_getter()
            if status.currentMessage() in {
                "Opening the remembered library in the background…",
                "Importing current TXT and Metadata and applying automatic tag rules…",
            }:
                status.clearMessage()
        display_label=label
        if owner=="library":
            display_label=str(getattr(self,"_scan_phase_label","") or label)
        stopping=getattr(self,"_long_operation_cancel_pending_owner","")==owner
        phase=" ".join(str(message or display_label or label or "Background task").split())
        if stopping and message:
            display_label=phase
        timing=getattr(self,"_long_operation_timings",{}).get(owner)
        elapsed_seconds=(
            max(0,time.monotonic()-timing.started_at)
            if timing is not None else 0
        )
        elapsed_text=_format_activity_duration(elapsed_seconds)
        eta_seconds=(timing.eta_seconds(current,total) if timing is not None else None)
        if any(marker in phase for marker in ("WD inference ETA:","JoyCaption inference ETA:","Task ETA:")):
            eta_seconds=None
            # Runtime samples use actual image units, never mixed work steps.
            eta_detail=""
        elif total and current>=total:
            eta_detail=""
        elif total and eta_seconds is not None:
            eta_detail=(
                "Current-phase ETA (estimate): "
                +_format_activity_duration(eta_seconds)
            )
        elif total:
            eta_detail="Current-phase ETA: Estimating…"
        else:
            eta_detail="ETA unavailable until this phase reports a bounded total"
        detail=(
            f"{int(current):,} of {int(total):,}" if total else
            f"{int(current):,} processed" if current else phase
        )
        activity_prefix="Waiting" if owner in queued else "Active task"
        visible_label=phase if owner in {"ingest","dataset"} else display_label
        if any(marker in phase for marker in ("WD inference ETA:","JoyCaption inference ETA:","Task ETA:")):
            from core.inference_timing import compact_inference_progress
            visible_label=compact_inference_progress(phase) or visible_label
        if stopping:
            activity_prefix="Stopping at a safe boundary"
        if owner=="library":
            visible_label=visible_label.rsplit(": ",1)[0]
            visible_label=visible_label.split(" batch ",1)[0]
            visible_label={
                "Opening the remembered library":"Opening library",
                "Checking the library for changes":"Checking files",
                "Validating saved image data":"Validating data",
                "Initializing image data":"Initializing data",
                "Running configured AI analysis":"Running AI analysis",
                "Reading image data":"Reading data",
                "Checking image record formats":"Checking formats",
                "Upgrading legacy image records":"Upgrading records",
                "Saving Image Knowledge":"Saving data",
            }.get(visible_label,visible_label)
        self.activity_label.setText(f"{activity_prefix} • {visible_label}")
        tooltip=display_label
        if message and message!=display_label:tooltip+=f"\n{message}"
        if total or current:tooltip+=f"\n{detail}"
        tooltip+=f"\nElapsed: {elapsed_text}"
        if timing is not None and owner in {"ingest","dataset"}:
            now=time.monotonic()
            phase_age=max(0,now-(timing.phase_started_at if timing.phase_started_at is not None else timing.started_at))
            tooltip+=f"\nPhase elapsed: {_format_activity_duration(phase_age)}"
            if timing.last_advance_at is None:
                tooltip+="\nNo completed work-step reported yet."
            else:
                age=max(0,now-timing.last_advance_at)
                tooltip+=f"\nLast completed work-step: {_format_activity_duration(age)} ago"
            tooltip+="\nElapsed time does not imply additional completed work."
            if stopping:
                tooltip+="\nCancellation requested; waiting for the current safe boundary."
        if eta_detail:tooltip+=f"\n{eta_detail}"
        others=[value[0] for key,value in events.items() if key!=owner]
        if others:tooltip+="\n\nAlso active: "+", ".join(others[:3])
        self.activity_label.setToolTip(tooltip)
        self.activity_label.setAccessibleDescription(tooltip)
        self.activity_label.show(); self.progress.show()
        set_progress_bar(
            self.progress,current,total,detail=tooltip,
            unknown_text=(
                f"Stopping • Elapsed {elapsed_text}"
                if stopping else
                f"{int(current):,} processed • Elapsed {elapsed_text}"
                if current else f"Elapsed {elapsed_text}"
            ),
        )
        self.progress.setAccessibleDescription(tooltip)
        if total:
            percent=round(max(0,min(int(current),int(total)))*100/int(total))
            count_text=f"{int(current):,}/{int(total):,}"
            elapsed=f"Elapsed {elapsed_text}"
            if stopping:
                suffix="Stopping"
            elif current>=total:
                suffix=""
            elif eta_seconds is not None:
                suffix=f"ETA est. {_format_activity_duration(eta_seconds)}"
            else:
                suffix=""
            full=(
                " • ".join(
                    part for part in (f"{percent}%",count_text,elapsed,suffix)
                    if part
                )
            )
            compact_eta=(
                " • ".join(
                    part for part in (f"{percent}%",elapsed,suffix) if part
                )
            )
            candidates=([
                full,compact_eta,f"{percent}% • {count_text} • {elapsed}",
                f"{percent}% • {elapsed}",
            ] if eta_seconds is not None else [
                full,
                f"{percent}% • {count_text} • {elapsed}",
                compact_eta,
                f"{percent}% • {elapsed}",
            ])
            available=min(
                self.progress.contentsRect().width(),
                self.progress.maximumWidth(),
            )-12
            progress_text=next((
                text for text in candidates
                if self.progress.fontMetrics().horizontalAdvance(text)<=available
            ),candidates[-1])
            self.progress.setFormat(progress_text)
        TagManagerWindow._render_long_operation_cancel(self,owner)

    def _refresh_activity_timing(self):
        if getattr(self,"_long_operation_events",{}):
            TagManagerWindow._render_long_operation(self)

    def _render_long_operation_cancel(self,owner):
        button=getattr(self,"scan_cancel_button",None)
        if button is None:return
        available={
            "content_safety":getattr(self,"_content_safety_task",None) is not None,
            "library":getattr(self,"_scan_worker",None) is not None,
        }
        target=owner if available.get(owner) else next((
            candidate for candidate,ready in available.items()
            if ready and candidate in getattr(self,"_long_operation_events",{})
        ),"")
        self._footer_cancel_owner=target
        if not target:
            button.hide(); return
        stopping=getattr(self,"_long_operation_cancel_pending_owner","")==target
        action=("Cancel Safely" if target==owner else
                "Cancel Library Scan" if target=="library" else "Cancel Rating Check")
        button.setText("Stopping Safely…" if stopping else action)
        button.setToolTip("Stop safely after the current "+("image-record checkpoint" if target=="library" else "content-rating check"))
        button.setEnabled(not stopping); button.show()

    def change_page(self,index: int):
        if self.stack.currentIndex()==6 and index!=6 and hasattr(self,"setup_page") and not self.setup_page.confirm_leave():
            blocker=QSignalBlocker(self.navigation); self.navigation.setCurrentRow(6); del blocker
            return
        automation_setup_return=(
            self.stack.currentIndex()==6 and index!=6
            and hasattr(self,"setup_page")
            and not self.setup_page.automation_return_panel.isHidden()
        )
        preparation_return_panel=getattr(
            getattr(self,"setup_page",None),"preparation_return_panel",None,
        )
        preparation_setup_return=(
            self.stack.currentIndex()==6 and index!=6
            and preparation_return_panel is not None
            and not preparation_return_panel.isHidden()
        )
        dataset=getattr(self,"dataset_page",None)
        if index!=2 and dataset is not None:
            abandon=getattr(
                dataset,"abandon_contextual_projection_handoff",None,
            )
            if callable(abandon):
                abandon()
        attributes={1:"ingest_page",2:"dataset_page",3:"review_page",4:"knowledge_page",5:"automation_page",6:"setup_page",7:"generation_page",8:"training_page"}
        attribute=attributes.get(index)
        if attribute is not None and not hasattr(self,attribute):
            # Paint the lightweight named shell before constructing the Qt-only
            # widget tree. Qt widgets must stay on this thread, but the user must
            # never be left looking at the previous page or an unexplained pause.
            self.stack.setCurrentIndex(index)
            application=QApplication.instance()
            if application is not None:
                application.processEvents(QEventLoop.ExcludeUserInputEvents)
        self._ensure_page(index)
        self.stack.setCurrentIndex(index)
        if automation_setup_return:
            self.setup_page.set_automation_return_available(False)
            if hasattr(self,"automation_page"):
                self.automation_page.refresh_behavior_prerequisites()
        if preparation_setup_return:
            self.setup_page.set_preparation_return_available(False)
            self._preparation_return_page=None
        if index==0:
            self._update_recommended()
        if index==1 and not self.ui_test: self.ingest_page.activate()
        if index==2 and hasattr(self,"dataset_page"):
            self.dataset_page.resume_visible_background_work()
        if index==4 and hasattr(self,"knowledge_page"):
            self.knowledge_page.refresh_visible_gallery_statuses()

    def refresh_preparation_defaults(self):
        invalidate=getattr(self,"_invalidate_dataset_page_preparation",None)
        if callable(invalidate):
            invalidate()
        for attribute in ("ingest_page","dataset_page"):
            page=getattr(self,attribute,None)
            controls=getattr(page,"preparation_controls",None)
            if controls is not None:
                controls.refresh_from_settings()

    def automation_behavior_changed(self):
        self.refresh_preparation_defaults()
        setup=getattr(self,"setup_page",None)
        if setup is not None:
            setup.sync_automation_behavior()

    def external_evidence_policy_changed(self,enabled: bool):
        setup=getattr(self,"setup_page",None)
        if setup is not None:
            setup.sync_evidence_intake()
        if enabled:
            self.apply_source_automation_now()

    def _dataset_loading_placeholder(self):
        page=self.stack.widget(2)
        return page if isinstance(page,_PageLoadingPlaceholder) else None

    def _start_dataset_page_preparation(self):
        if (
            self._dataset_page_task is not None
            or getattr(self,"_dataset_preparation_shutdown",False)
        ):
            return
        placeholder=self._dataset_loading_placeholder()
        if placeholder is not None:
            placeholder.set_progress(0,2,"Loading Dataset knowledge…")
        task=CoordinatedCallTask(
            self.state.jobs,"Preparing Dataset",
            lambda job:prepare_dataset_page(self.state,job),
            resource="io",key="dataset:first-open",
        )
        self._dataset_page_task=task
        task.signals.progress.connect(
            lambda current,total,message,owner=task:
            self._dataset_page_preparation_progress(
                owner,current,total,message,
            )
        )
        task.signals.complete.connect(
            lambda preparation,owner=task:
            self._dataset_page_preparation_complete(owner,preparation)
        )
        task.signals.failed.connect(
            lambda message,owner=task:
            self._dataset_page_preparation_failed(owner,message)
        )
        task.signals.cancelled.connect(
            lambda owner=task:self._dataset_page_preparation_failed(
                owner,"Preparation was cancelled safely.",
            )
        )
        task.start()

    def _dataset_page_preparation_progress(
        self,task,current: int,total: int,message: str,
    ):
        if task is not self._dataset_page_task:
            return
        placeholder=self._dataset_loading_placeholder()
        if placeholder is not None:
            placeholder.set_progress(current,total,message)

    def _dataset_page_preparation_complete(self,task,preparation):
        if task is not self._dataset_page_task:
            return
        self._dataset_page_task=None
        if getattr(self,"_dataset_preparation_shutdown",False):
            return
        if not preparation.is_current(self.state):
            self._dataset_page_preparation_failed(
                None,"Dataset knowledge changed while the page was opening.",
            )
            return
        self._dataset_page_preparation=preparation
        QTimer.singleShot(0,self._materialize_prepared_dataset_page)

    def _materialize_prepared_dataset_page(self):
        if getattr(self,"_dataset_preparation_shutdown",False):
            self._dataset_page_preparation=None
            return
        preparation=self._dataset_page_preparation
        if preparation is None:
            return
        if not preparation.is_current(self.state):
            self._dataset_page_preparation=None
            self._start_dataset_page_preparation()
            return
        self._ensure_page(2)
        if not hasattr(self,"dataset_page"):
            return
        if self.navigation.currentRow()==2:
            self.stack.setCurrentIndex(2)
            self.dataset_page.resume_visible_background_work()
        pending_actions=getattr(self,"_pending_dataset_page_actions",[])
        actions=tuple(pending_actions)
        pending_actions.clear()
        for action in actions:
            QTimer.singleShot(
                0,lambda pending=action:
                TagManagerWindow._deliver_pending_dataset_page_action(
                    self,pending,
                ),
            )

    def _dataset_page_preparation_failed(self,task,message: str):
        if task is not None and task is not self._dataset_page_task:
            return
        self._dataset_page_task=None
        self._dataset_page_preparation=None
        getattr(self,"_pending_dataset_page_actions",[]).clear()
        training_page=getattr(self,"training_page",None)
        if training_page is not None and training_page._planning:
            training_page.set_planning(False,"Dataset could not be opened. Retry the selected Training action.")
        placeholder=self._dataset_loading_placeholder()
        if placeholder is not None:
            placeholder.set_error(message)
        self.statusBar().showMessage(
            "Dataset could not be prepared; select it to retry",12000,
        )

    def _invalidate_dataset_page_preparation(self):
        task=self._dataset_page_task
        had_preparation=self._dataset_page_preparation is not None
        self._dataset_page_task=None
        self._dataset_page_preparation=None
        if task is not None:
            task.cancel()
        if (
            (task is not None or had_preparation)
            and not getattr(self,"_dataset_preparation_shutdown",False)
            and (
                self.navigation.currentRow()==2
                or bool(getattr(self,"_pending_dataset_page_actions",()))
            )
            and not hasattr(self,"dataset_page")
        ):
            QTimer.singleShot(0,self._start_dataset_page_preparation)

    def _queue_dataset_page_action(self,action):
        if getattr(self,"_dataset_preparation_shutdown",False):
            return False
        if hasattr(self,"dataset_page"):
            return action()
        pending=getattr(self,"_pending_dataset_page_actions",None)
        if pending is None:
            pending=[]
            self._pending_dataset_page_actions=pending
        pending.append(action)
        self._ensure_page(2)
        if (
            hasattr(self,"dataset_page")
            and action in pending
        ):
            pending.remove(action)
            return action()
        return True

    def _deliver_pending_dataset_page_action(self,action):
        if (
            getattr(self,"_dataset_preparation_shutdown",False)
            or not hasattr(self,"dataset_page")
        ):
            return False
        return action()

    def _knowledge_loading_placeholder(self):
        page=self.stack.widget(4)
        return page if isinstance(page,_PageLoadingPlaceholder) else None

    def _start_knowledge_page_preparation(self):
        if self._knowledge_page_task is not None:
            return
        placeholder=self._knowledge_loading_placeholder()
        if placeholder is not None:
            placeholder.set_progress(0,6,"Loading System Knowledge indexes…")

        task=CoordinatedCallTask(
            self.state.jobs,
            "Preparing Tag Encyclopedia",
            lambda job:prepare_knowledge_page(self.state,job),
            resource="cpu",key="tag-encyclopedia:first-open",
        )
        self._knowledge_page_task=task
        task.signals.progress.connect(
            lambda current,total,message,owner=task:
            self._knowledge_page_preparation_progress(
                owner,current,total,message,
            )
        )
        task.signals.complete.connect(
            lambda preparation,owner=task:
            self._knowledge_page_preparation_complete(owner,preparation)
        )
        task.signals.failed.connect(
            lambda message,owner=task:
            self._knowledge_page_preparation_failed(owner,message)
        )
        task.signals.cancelled.connect(
            lambda owner=task:self._knowledge_page_preparation_failed(
                owner,"Preparation was cancelled safely.",
            )
        )
        task.start()

    def _knowledge_page_preparation_progress(
        self,task,current: int,total: int,message: str,
    ):
        if task is not self._knowledge_page_task:
            return
        placeholder=self._knowledge_loading_placeholder()
        if placeholder is not None:
            placeholder.set_progress(current,total,message)

    def _knowledge_page_preparation_complete(self,task,preparation):
        if task is not self._knowledge_page_task:
            return
        self._knowledge_page_task=None
        if not preparation.is_current(self.state):
            self._knowledge_page_preparation_failed(
                None,"System Knowledge changed while the page was opening.",
            )
            return
        self._knowledge_page_preparation=preparation
        QTimer.singleShot(0,self._materialize_prepared_knowledge_page)

    def _materialize_prepared_knowledge_page(self):
        if self._knowledge_page_preparation is None:
            return
        self._ensure_page(4)
        if self.navigation.currentRow()==4:
            self.stack.setCurrentIndex(4)
            QTimer.singleShot(
                0,self.knowledge_page.refresh_visible_gallery_statuses,
            )
        pending=getattr(self,"_pending_knowledge_editor_action",None)
        self._finish_knowledge_editor_wait()
        if pending is not None:
            self._ready_knowledge_editor_action=pending
            QTimer.singleShot(0,lambda:self._resume_knowledge_editor_action(pending))

    def _knowledge_page_preparation_failed(self,task,message: str):
        if task is not None and task is not self._knowledge_page_task:
            return
        self._knowledge_page_task=None
        self._knowledge_page_preparation=None
        placeholder=self._knowledge_loading_placeholder()
        if placeholder is not None:
            placeholder.set_error(message)
        pending=getattr(self,"_pending_knowledge_editor_action",None)
        self._finish_knowledge_editor_wait(cancelled=True)
        if pending is not None:
            self.statusBar().showMessage("The Tag editor could not open: "+message,12000)

    def _review_loading_placeholder(self):
        page=self.stack.widget(3)
        return page if isinstance(page,_PageLoadingPlaceholder) else None

    def _start_review_page_preparation(self):
        if self._review_page_task is not None:
            return
        placeholder=self._review_loading_placeholder()
        if placeholder is not None:
            placeholder.set_progress(0,6,"Loading Review queue…")
        task=CoordinatedCallTask(
            self.state.jobs,"Preparing Review Center",
            lambda job:prepare_review_page(self.state,job),
            resource="io",key="review-center:first-open",
        )
        self._review_page_task=task
        task.signals.progress.connect(
            lambda current,total,message,owner=task:
            self._review_page_preparation_progress(
                owner,current,total,message,
            )
        )
        task.signals.complete.connect(
            lambda preparation,owner=task:
            self._review_page_preparation_complete(owner,preparation)
        )
        task.signals.failed.connect(
            lambda message,owner=task:
            self._review_page_preparation_failed(owner,message)
        )
        task.signals.cancelled.connect(
            lambda owner=task:self._review_page_preparation_failed(
                owner,"Preparation was cancelled safely.",
            )
        )
        task.start()

    def _review_page_preparation_progress(
        self,task,current: int,total: int,message: str,
    ):
        if task is not self._review_page_task:
            return
        placeholder=self._review_loading_placeholder()
        if placeholder is not None:
            placeholder.set_progress(current,total,message)

    def _review_page_preparation_complete(self,task,preparation):
        if task is not self._review_page_task:
            return
        self._review_page_task=None
        if not preparation.is_current(self.state):
            self._review_page_preparation_failed(
                None,"Saved Review data changed while the page was opening.",
            )
            return
        self._review_page_preparation=preparation
        QTimer.singleShot(0,self._materialize_prepared_review_page)

    def _materialize_prepared_review_page(self):
        if self._review_page_preparation is None:
            return
        self._ensure_page(3)
        if self.navigation.currentRow()==3:
            self.stack.setCurrentIndex(3)
        actions=tuple(self._pending_review_page_actions)
        self._pending_review_page_actions.clear()
        for action in actions:
            QTimer.singleShot(0,action)

    def _review_page_preparation_failed(self,task,message: str):
        if task is not None and task is not self._review_page_task:
            return
        self._review_page_task=None
        self._review_page_preparation=None
        self._pending_review_page_actions.clear()
        placeholder=self._review_loading_placeholder()
        if placeholder is not None:
            placeholder.set_error(message)
        self.statusBar().showMessage(
            "Review Center could not be prepared; select it to retry",12000,
        )

    def _ensure_page(self,index: int):
        attributes={1:"ingest_page",2:"dataset_page",3:"review_page",4:"knowledge_page",5:"automation_page",6:"setup_page",7:"generation_page",8:"training_page"}
        attribute=attributes.get(index)
        if attribute is None or hasattr(self,attribute): return
        if (
            index==2 and self._defer_expensive_page_initialization
            and self._dataset_page_preparation is None
        ):
            self._start_dataset_page_preparation()
            return
        if (
            index==3 and self._defer_expensive_page_initialization
            and self._review_page_preparation is None
        ):
            self._start_review_page_preparation()
            return
        if (
            index==4 and self._defer_knowledge_page_initialization
            and self._knowledge_page_preparation is None
        ):
            self._start_knowledge_page_preparation()
            return
        if index==1:
            page=IngestPage(
                self.state,
                defer_expensive_initialization=(
                    self._defer_expensive_page_initialization
                ),
                ui_settings=self.qt_settings,
            ); page.library_changed.connect(self.refresh_library)
            page.library_paths_changed.connect(self.refresh_moved_images)
            page.open_dataset_image.connect(self.open_completed_dataset_image)
            page.open_recycle_requested.connect(self.open_recycle_records)
            page.open_setup.connect(lambda:self.navigation.setCurrentRow(6))
            page.open_automation.connect(self.open_automation_function)
            page.source_correction_requested.connect(
                self.open_ingest_source_correction
            )
            page.attention_changed.connect(self._handle_ingest_attention)
            page.content_ratings_requested.connect(self.request_content_safety)
            page.edit_tag_requested.connect(self.edit_system_tag)
            page.create_tag_requested.connect(self.create_system_tag)
        elif index==2:
            preparation=self._dataset_page_preparation
            if preparation is not None and not preparation.is_current(self.state):
                self._dataset_page_preparation=None
                self._start_dataset_page_preparation()
                return
            try:
                page=DatasetPage(
                    self.state,
                    defer_expensive_initialization=(
                        self._defer_expensive_page_initialization
                    ),
                    ui_settings=self.qt_settings,
                    preparation=preparation,
                )
            except RuntimeError as error:
                if str(error)!="Dataset knowledge changed before the page opened.":
                    raise
                self._dataset_page_preparation=None
                self._start_dataset_page_preparation()
                return
            self._dataset_page_preparation=None
            page.refresh_requested.connect(self.refresh_library); page.change_library.connect(self.choose_library)
            page.open_ingest_requested.connect(lambda:self.navigation.setCurrentRow(1))
            page.library_paths_changed.connect(self.refresh_moved_images)
            page.content_ratings_requested.connect(self.request_content_safety)
            page.edit_tag_requested.connect(self.edit_system_tag)
            page.create_tag_requested.connect(self.create_system_tag)
            page.knowledge_changed.connect(
                lambda:self.knowledge_data_changed(invalidate_ratings=False)
            )
            page.open_tag_encyclopedia.connect(
                self.open_tag_encyclopedia_review
            )
            page.downstream_updates_changed.connect(
                self._refresh_downstream_update_notice
            )
            page.open_automation.connect(self.open_automation_function)
            page.ingest_source_correction_return_requested.connect(
                self._return_ingest_source_correction
            )
            page.refresh_external_evidence.connect(
                self.force_refresh_external_metadata
            )
        elif index==3:
            page=ReviewCenterPage(
                self.state,preparation=self._review_page_preparation,
            ); self._review_page_preparation=None
            page.data_changed.connect(self.review_data_changed)
            page.choose_libraries_requested.connect(self.choose_library)
            page.open_dataset_image.connect(self.open_dataset_image); page.edit_image_data.connect(self.edit_image_data)
            page.force_ai_requested.connect(self.force_ai_from_anywhere)
            page.edit_tag_requested.connect(self.edit_system_tag)
            page.open_automation.connect(self.open_automation_function)
            page.merge_tags_requested.connect(self.review_duplicate_tags)
            page.removed_tags_requested.connect(self.review_removed_tags)
            page.downstream_updates_changed.connect(
                self._refresh_downstream_update_notice
            )
        elif index==4:
            page=KnowledgePage(
                self.state,ui_settings=self.qt_settings,
                preparation=self._knowledge_page_preparation,
            ); self._knowledge_page_preparation=None
            if hasattr(page, "suggest_generation_setup_requested"):
                page.suggest_generation_setup_requested.connect(self.open_character_outfit_setup)
            page.data_changed.connect(lambda:self.knowledge_data_changed(invalidate_ratings=False))
            page.scoped_data_changed.connect(lambda _scope:self.knowledge_data_changed(
                invalidate_ratings=False,reload_knowledge=False,preserve_workflow=True,
            ))
            page.find_new_knowledge_requested.connect(self.find_new_knowledge)
            page.review_meaning_proposals_requested.connect(
                lambda task_id:self.open_review_function(
                    "review.knowledge.discovery",{
                        "origin":"tag_details_meaning",
                        "select_id":task_id,
                    },
                )
            )
            page.review_broader_proposals_requested.connect(
                lambda task_id:self.open_review_function(
                    "review.knowledge.discovery",{
                        "origin":"tag_details_broader",
                        "select_id":task_id,
                    },
                )
            )
            page.maintenance_applied.connect(
                lambda:self.knowledge_data_changed(
                    invalidate_ratings=False,reload_knowledge=False,
                )
            )
            page.show_dataset_tag.connect(self.open_dataset_tag); page.edit_image_data.connect(self.edit_image_data)
            page.force_ai_requested.connect(self.force_ai_from_anywhere)
            page.content_ratings_requested.connect(self.request_content_safety)
            page.content_sensitivity_changed.connect(self.queue_content_sensitivity_refresh)
            page.open_automation.connect(self.open_automation_function)
            page.open_dataset.connect(self.open_dataset_function)
            page.downstream_updates_changed.connect(
                self._refresh_downstream_update_notice
            )
            page.knowledge_pack_import_requested.connect(
                self.open_knowledge_pack_import
            )
        elif index==5:
            page=AutomationPage(
                self.state,
                defer_expensive_initialization=(
                    self._defer_expensive_page_initialization
                ),
                create_rule_tag=self.create_rule_tag,
            ); page.refresh_requested.connect(self.refresh_library); page.edit_image_data.connect(self.edit_image_data)
            page.knowledge_changed.connect(lambda:self.knowledge_data_changed(invalidate_ratings=False))
            page.behavior_changed.connect(self.automation_behavior_changed)
            page.evidence_policy_changed.connect(
                self.external_evidence_policy_changed
            )
            page.organization_policy_changed.connect(self.change_organization)
            page.ai_setup_requested.connect(self.open_automation_ai_setup)
        elif index==6:
            page=SetupPage(
                self.settings,state=self.state,
                defer_expensive_initialization=(
                    self._defer_expensive_page_initialization
                ),
            ); page.theme_changed.connect(self.change_theme); page.color_blind_changed.connect(self.change_color_blind_mode)
            page.tooltips_changed.connect(self.change_tooltips); page.content_level_changed.connect(self.change_content_level)
            page.library_profile_changed.connect(lambda _stable_id:self.refresh_library())
            page.preparation_defaults_changed.connect(
                self.refresh_preparation_defaults
            )
            page.guided_setup_requested.connect(self.open_guided_setup)
            page.bug_report_requested.connect(self.open_bug_report)
            page.feedback_archive_requested.connect(self.export_collected_feedback)
            page.open_automation.connect(self.open_automation_function)
            page.automation_return_requested.connect(
                lambda:self.navigation.setCurrentRow(5)
            )
            page.preparation_return_requested.connect(self._return_preparation_setup)
            page.swarm_status_changed.connect(self.dashboard.set_swarm_status)
            self.dashboard.set_swarm_status(page.swarm_runtime.manager.snapshot())
        elif index==7:
            page=GenerationPage(self)
            page.new_requested.connect(lambda:self.open_generation_workspace(force_new=True))
            page.create_setup_requested.connect(self.open_generation_setup_source)
            page.use_setup_requested.connect(self.open_saved_generation_setup)
            page.active_changed.connect(
                lambda _dialog:setattr(self,"_generation_dialog",page.retained_workspace("image")))
        elif index==8:
            from .training_workspace import create_training_page
            page=create_training_page(self)
        else: raise RuntimeError(f"No native Qt page is registered for index {index}.")
        setattr(self,attribute,page)
        placeholder=self.stack.widget(index)
        previous_page=self.stack.currentWidget()
        blocker=QSignalBlocker(self.stack)
        try:
            self.stack.removeWidget(placeholder)
            self.stack.insertWidget(index,page)
            self.stack.setCurrentWidget(
                page if previous_page is placeholder else previous_page
            )
        finally:
            del blocker
        placeholder.deleteLater()
        if index==2: page.set_dataset(self.state.dataset)
        if hasattr(self,"command_controller"):
            self.command_controller.refresh()

    def open_training_workspace(self, *, group=None, draft=None, collection_reviewed=False):
        from .training_workspace import open_training_workspace
        return open_training_workspace(self,group=group,draft=draft,collection_reviewed=collection_reviewed)

    def retain_training_review(self, dialog, finished):
        from .training_workspace import retain_training_review
        return retain_training_review(self,dialog,finished)

    def open_swarm_setup(self, action: str="setup"):
        """Open the canonical manager and optionally perform one direct Dashboard action."""
        self.navigation.setCurrentRow(6)
        self._ensure_page(6)
        self.setup_page.show_section("components_downloads","local_generation")
        if action == "update":
            self.setup_page.swarm_runtime.select_task("maintenance")
        elif action in {"reconnect", "open", "compatibility"}:
            self.setup_page.swarm_runtime.select_task("run")
        else:
            self.setup_page.swarm_runtime.select_default_task()
        self.setup_page.swarm_runtime.setFocus(Qt.OtherFocusReason)
        actions={
            "reconnect":self.setup_page.swarm_runtime.refresh_health,
            "open":self.setup_page.swarm_runtime.open_runtime,
            "update":self.setup_page.swarm_runtime.apply_update,
            "compatibility":self.setup_page.swarm_runtime.review_compatibility,
        }
        operation=actions.get(str(action))
        if operation is not None:
            QTimer.singleShot(0,operation)

    def _remember_navigation_width(self,*_args):
        if getattr(self,"_navigation_mode",None)=="expanded":
            self._expanded_navigation_width=max(
                145,self.navigation_panel.width()-8,
            )

    def cycle_navigation_mode(self):
        current=getattr(self,"_navigation_mode","expanded")
        index=self.NAVIGATION_MODES.index(current)
        self.set_navigation_mode(self.NAVIGATION_MODES[(index+1)%len(self.NAVIGATION_MODES)])

    def set_navigation_mode(self,mode: str,*,persist: bool=True):
        mode=str(mode or "expanded").strip().casefold()
        if mode=="hidden":mode="compact"
        if mode not in self.NAVIGATION_MODES:
            mode="expanded"
        previous=getattr(self,"_navigation_mode",None)
        if previous=="expanded" and hasattr(self,"navigation_panel"):
            self._expanded_navigation_width=max(
                145,self.navigation_panel.width()-8,
            )
        self._navigation_mode=mode
        compact=mode=="compact"
        self.navigation_delegate.set_compact(compact)
        self.navigation.setProperty("compactNavigation",compact)
        self.navigation.style().unpolish(self.navigation)
        self.navigation.style().polish(self.navigation)
        self.navigation.setMinimumWidth(0 if compact else 145)
        self.navigation.setMaximumWidth(50 if compact else self._expanded_navigation_maximum)
        for row,page_index in enumerate(self.NAVIGATION_PAGE_ORDER):
            name=self.PAGE_NAMES[page_index]
            item=self.navigation.item(row)
            item.setText("" if compact else name)
            item.setTextAlignment(
                Qt.AlignCenter if compact else Qt.AlignVCenter|Qt.AlignLeft
            )
            item.setSizeHint(QSize(48,52) if compact else QSize())
            item.setData(Qt.AccessibleTextRole,name)
        self.navigation.setIconSize(QSize(26,26) if compact else QSize(20,20))
        self.navigation.setVisible(True)
        if mode=="expanded":
            maximum=self.navigation.maximumWidth()+8
            self.navigation_panel.setMinimumWidth(145)
            self.navigation_panel.setMaximumWidth(maximum)
            target=max(145,min(
                maximum,getattr(self,"_expanded_navigation_width",180)+8,
            ))
        elif mode=="compact":
            self.navigation_panel.setMinimumWidth(58)
            self.navigation_panel.setMaximumWidth(58)
            target=58
        self.shell_splitter.setSizes((target,max(1,self.shell_splitter.width()-target)))
        next_mode=self.NAVIGATION_MODES[
            (self.NAVIGATION_MODES.index(mode)+1)%len(self.NAVIGATION_MODES)
        ]
        next_label={
            "expanded":"expanded labels","compact":"compact icons",
        }[next_mode]
        from .navigation_icons import navigation_mode_icon
        self.navigation_mode_button.setIcon(navigation_mode_icon(compact))
        self.navigation_mode_button.setToolTip(
            f"Navigation is {mode}. Toggle to {next_label}."
        )
        self.navigation_mode_button.setAccessibleDescription(
            f"Navigation is {mode}. Activate this toggle for {next_label}."
        )
        self.navigation_mode_button.setChecked(compact)
        if persist:
            self.qt_settings.setValue("main/navigation_mode",mode)

    def _build_menu(self):
        file_menu=QMenu("File",self)
        self.menuBar().addMenu(file_menu)
        choose=QAction("Filter Image Libraries…",self); choose.triggered.connect(self.choose_library); file_menu.addAction(choose)
        refresh=QAction("Refresh Libraries",self); refresh.triggered.connect(self.refresh_library); file_menu.addAction(refresh)
        generation=QAction("Plan Image Generation…",self)
        generation.triggered.connect(self.open_generation_workspace)
        file_menu.addAction(generation)
        file_menu.addSeparator()
        exit_action=QAction("Exit",self)
        exit_action.triggered.connect(self.close)
        file_menu.addAction(exit_action)
        help_menu=QMenu("Help",self)
        self.menuBar().addMenu(help_menu)
        page_help=QAction("Help for Current Page",self); page_help.setShortcut("F1")
        page_help.triggered.connect(self.show_current_help); help_menu.addAction(page_help)
        report_bug=QAction("Feedback…",self)
        report_bug.triggered.connect(self.open_bug_report); help_menu.addAction(report_bug)
        export_feedback=QAction("Export Collected Feedback…",self)
        export_feedback.triggered.connect(self.export_collected_feedback); help_menu.addAction(export_feedback)
        help_menu.addSeparator()
        whats_new=QAction(f"What's New in {PRODUCT_NAME} v{VERSION}",self)
        whats_new.triggered.connect(self.show_release_notes); help_menu.addAction(whats_new)
        about_action=QAction(f"About {PRODUCT_NAME}",self)
        about_action.triggered.connect(self.show_about)
        help_menu.addAction(about_action)
        # Retain the menu wrappers used by the ribbon. Direct commands above
        # have window-owned actions so their lifetime does not depend on menus.
        self._application_menus=(file_menu,help_menu)

    def open_generation_workspace(self, *, force_new=False):
        from .generation_workflow import open_generation_workspace
        self.navigation.setCurrentRow(7)
        if self.navigation.currentRow()==7:
            open_generation_workspace(self, force_new=force_new)

    def open_generation_setup_composer(self):
        """Author a source-free setup without creating an incidental request draft."""
        from core.model_resource_registry import ModelResourceRegistry
        from core.model_selection import protected_model_roots
        from core.paths import MODEL_RESOURCES_PATH
        from .generation_manual_setup_dialog import ManualGenerationSetupDialog
        dialog = ManualGenerationSetupDialog(self.state.jobs, USER_DATA_DIR / "generation",
            registry=ModelResourceRegistry(MODEL_RESOURCES_PATH), settings=self.settings,
            protected_model_roots=protected_model_roots(self.settings), parent=self)
        if dialog.exec() == QDialog.Accepted and dialog.saved_path is not None:
            self.statusBar().showMessage("Generation Setup saved. Use Saved Setup in Generate to review it in a new image.", 12000)

    def open_saved_generation_setup(self):
        """Choose one local setup before opening a separate reviewed request."""
        from core.generation_recipes import load_recipe
        from core.model_resource_registry import ModelResourceRegistry
        from core.model_selection import protected_model_roots
        from core.paths import MODEL_RESOURCES_PATH
        from .generation_saved_work_dialog import SavedGenerationWorkDialog

        browser = SavedGenerationWorkDialog(
            self.state.jobs, USER_DATA_DIR / "generation", "recipes", parent=self,
            registry=ModelResourceRegistry(MODEL_RESOURCES_PATH), settings=self.settings,
            protected_model_roots=protected_model_roots(self.settings),
            selection_only=True,
        )
        if browser.exec() != QDialog.Accepted:
            return
        if browser.requested_action == "create":
            self.open_generation_setup_source()
            return
        path = browser.selected_path
        if not path:
            return
        try:
            load_recipe(path)
        except (OSError, ValueError):
            QMessageBox.information(
                self, "Saved setup needs attention",
                "The selected setup changed or could not be read. No new draft was opened. "
                "Check the saved file, then choose Use Saved Setup again to review the current item.",
            )
            return
        self._review_saved_generation_setup(path)

    def _review_saved_generation_setup(self, saved_path):
        from .generation_workflow import open_generation_workspace
        self.navigation.setCurrentRow(7)
        if self.navigation.currentRow() == 7:
            def review_in_new_request(target):
                original_digest = target.plan.digest
                def unapplied(error, location_saved):
                    if target.plan.digest == original_digest:
                        target.reject()
                    closed = getattr(target, "_closed", False)
                    if error:
                        QMessageBox.information(
                            self, "Saved setup needs attention",
                            ("The selected setup changed or could not be read. "
                             + ("No new draft was opened. " if closed else "The current request was retained. ")
                             + "Check the saved file, then choose Use Saved Setup again to review the current item."),
                        )
                    else:
                        outcome = "The exact Models location was saved separately. " if location_saved else ""
                        self.statusBar().showMessage(
                            outcome + ("Setup review canceled; no new image draft was kept."
                                       if closed else "Setup review canceled; the current request was retained."),
                            12000,
                        )
                target.load_recipe(selected_path=saved_path, on_unapplied=unapplied)
            open_generation_workspace(
                self, force_new=True,
                on_opened=review_in_new_request,
            )

    def open_generation_setup_source(self):
        """Offer manual authoring and the existing evidence-derived owner route."""
        from .generation_setup_source_dialog import GenerationSetupSourceDialog

        choice = GenerationSetupSourceDialog(
            lambda: CharacterDatabase(USER_DATA_DIR, recover_interrupted=False), self,
        )
        if choice.exec() != QDialog.Accepted:
            return
        if choice.source_kind == "manual":
            self.open_generation_setup_composer()
            return
        owner_kind, owner_id = choice.source_kind, choice.selected_identity
        if not owner_id:
            return
        characters = choice.characters
        try:
            characters.load()
        except (OSError, ValueError):
            QMessageBox.information(
                self, "Character or Outfit unavailable",
                "The Character or Outfit changed. Reopen Create Generation Setup to choose a current identity.",
            )
            return
        self.open_character_outfit_setup(owner_kind, owner_id, characters=characters)

    def open_character_outfit_setup(self, owner_kind, owner_id, *, characters=None):
        """Review selected canonical owner evidence before saving a separate setup."""
        from core.model_resource_registry import ModelResourceRegistry
        from core.model_selection import protected_model_roots
        from core.paths import MODEL_RESOURCES_PATH
        from .generation_character_setup_dialog import CharacterOutfitSetupDialog

        page = getattr(self, "knowledge_page", None)
        characters = characters or (page.characters if page is not None else None)
        records = (characters.characters if owner_kind == "character" else
                   characters.outfits if owner_kind == "outfit" else {}) if characters is not None else {}
        owner = records.get(owner_id)
        if owner is None:
            QMessageBox.information(self, "Character or Outfit changed",
                                    "Reopen the current Character or Outfit before making a setup.")
            return
        dataset = self.state.dataset
        tag_id = characters.entity_tag_id(owner_kind, owner_id)
        tag = self.state.knowledge.tags.get(tag_id)
        names = (tag.display, *tag.aliases) if tag is not None else ()
        dialog = CharacterOutfitSetupDialog(
            self.state.jobs, USER_DATA_DIR / "generation", dataset=dataset,
            owner_kind=owner_kind, owner_id=owner_id, owner_name=owner.name,
            representative_tag_id=tag_id, representative_names=names,
            authority_paths=(characters.characters_path,
                             characters.outfits_path,
                             characters.relationship_path, self.state.knowledge.path),
            alternative_paths=(DEFAULTS_DIR / "relationships.json",
                               RELATIONSHIPS_PATH,
                               DEFAULTS_DIR / "conflicts.json"),
            registry=ModelResourceRegistry(MODEL_RESOURCES_PATH), settings=self.settings,
            protected_model_roots=protected_model_roots(self.settings), parent=self,
        )
        if dialog.exec() == QDialog.Accepted and dialog.saved_path is not None:
            saved_path = dialog.saved_path
            tag_editor = (getattr(page, "_active_tag_editor_dialog", None)
                          if characters is getattr(page, "characters", None) else None)
            choice = QMessageBox(tag_editor or self)
            choice.setWindowTitle("Generation Setup saved")
            choice.setText(f'"{dialog.name.text()}" is saved for reuse.')
            choice.setInformativeText(
                "Use it in a separate new image draft now, or find it later with Use Saved Setup in Generate. "
                "Nothing has been generated."
            )
            use_now = choice.addButton("Use in New Image", QMessageBox.AcceptRole)
            later = choice.addButton("Keep for Later", QMessageBox.RejectRole)
            choice.setDefaultButton(later)
            choice.exec()
            if choice.clickedButton() is not use_now:
                self.statusBar().showMessage(
                    "Generation Setup saved. Find it in Generate > Use Saved Setup.", 12000,
                )
                return

            def review_saved_setup():
                from shiboken6 import isValid
                if not isValid(self):
                    return
                self._review_saved_generation_setup(saved_path)

            if tag_editor is not None and tag_editor.isVisible():
                tag_editor.finished.connect(
                    lambda _result: QTimer.singleShot(0, review_saved_setup)
                )
                tag_editor.request_close()
            else:
                review_saved_setup()

    def release_notes_dialog(self) -> ReleaseNotesDialog:
        entries=load_release_history(Path(__file__).resolve().parents[1],VERSION)
        return ReleaseNotesDialog(entries,self)

    def show_release_notes(self):
        try: dialog=self.release_notes_dialog()
        except (OSError,ValueError) as exc:
            QMessageBox.information(self,"Release notes unavailable",str(exc)); return
        dialog.exec()

    def about_dialog(self) -> QMessageBox:
        box=QMessageBox(QMessageBox.Information,f"About {PRODUCT_NAME}",about_text(),QMessageBox.Ok,self)
        mark=about_mark()
        if not mark.isNull(): box.setIconPixmap(mark)
        return box

    def show_about(self):
        self.about_dialog().exec()

    def _bug_report_context(
        self,
        include_selected_image: bool,
        *,
        modal_workflow: str = "",
        modal_tab: str = "",
    ) -> dict:
        current=self.navigation.currentItem()
        if current is None:
            page="Unknown"
        else:
            read_data=getattr(current,"data",None)
            stored_name=read_data(Qt.UserRole) if callable(read_data) else ""
            page=str(stored_name or current.text())
        if modal_workflow:
            page+=f" > {redact_diagnostic_text(modal_workflow)[:120]}"
            if modal_tab:
                page+=f" > {redact_diagnostic_text(modal_tab)[:120]}"
        context=runtime_context(
            application_version=VERSION,
            knowledge_version=KNOWLEDGE_VERSION,
            data_schema_version=SCHEMA_VERSION,
            page=page,
            recent_events=self.state.events.recent(),
            jobs=self.state.jobs.diagnostic_snapshot(),
        )
        if include_selected_image and self.state.selected_image:
            context["selected_image_identity"]=report_selected_image_identity(
                self.state.selected_image
            )
        return context

    def _bug_report_screenshot(self,target: QWidget|None=None) -> bytes:
        buffer=QBuffer()
        if not buffer.open(QIODevice.WriteOnly):
            return b""
        try:
            source=target if target is not None and target.isVisible() else self
            capture_widgets=(source,*source.findChildren(QWidget))
            for widget in capture_widgets:
                prepare_capture=getattr(
                    widget,"prepare_for_privacy_capture",None
                )
                if callable(prepare_capture):
                    prepare_capture()
            QApplication.processEvents()
            if not source.grab().save(buffer,"PNG"):
                return b""
            return bytes(buffer.data())
        finally:
            buffer.close()

    def open_bug_report(
        self,
        _checked=False,
        *,
        summary: str="",
        current_context: str="",
        feedback_type: str="bug",
        origin: QWidget|None=None,
        modal_workflow: str="",
        modal_tab: str="",
    ):
        report_parent=origin or self
        origin_label=modal_workflow
        if modal_tab:
            origin_label+=f" > {modal_tab}"
        dialog=BugReportDialog(
            app_root=Path(__file__).resolve().parents[1],
            context_provider=lambda include:self._bug_report_context(
                include,
                modal_workflow=modal_workflow,
                modal_tab=modal_tab,
            ),
            settings_provider=lambda:vars(self.settings),
            screenshot_provider=lambda:self._bug_report_screenshot(origin),
            logs_dir=Path(__file__).resolve().parents[1]/"logs",
            parent=report_parent,
            summary=summary,
            current_context=current_context,
            feedback_type=feedback_type,
            origin_context=origin_label,
            screenshot_subject=(
                "Current Dialog" if origin is not None else "Current Window"
            ),
            screenshot_attachment_name=(
                "current_dialog.png"
                if origin is not None else "current_window.png"
            ),
        )
        if dialog.exec()!=QDialog.Accepted or dialog.saved_directory is None:
            return
        box,open_github,copy_path,open_folder=build_feedback_ready_dialog(
            report_parent,dialog
        )
        box.exec()
        if box.clickedButton() is open_github:
            QApplication.clipboard().setText(dialog.report_text)
            opened=QDesktopServices.openUrl(
                feedback_issue_url(dialog.report_id,dialog.report_summary)
            )
            if not opened:
                QMessageBox.warning(
                    self,
                    "Could Not Open GitHub",
                    "The readable report was copied, but the GitHub feedback form "
                    f"could not be opened.\n\n{PUBLIC_REPOSITORY_URL}/issues/new",
                )
        elif copy_path is not None and box.clickedButton() is copy_path:
            QApplication.clipboard().setText(str(dialog.zip_path))
        elif box.clickedButton() is open_folder:
            folder=(
                dialog.zip_path.parent
                if dialog.zip_path is not None
                else dialog.saved_directory.parent
            )
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(folder)))

    def show_reportable_error(self,title: str,message: str,*,severity: str="high"):
        icon=QMessageBox.Warning if severity in {"low","medium"} else QMessageBox.Critical
        box=QMessageBox(icon,title,message,QMessageBox.NoButton,self)
        report=box.addButton("Report a Bug…",QMessageBox.ActionRole)
        close=box.addButton(QMessageBox.Close)
        box.setDefaultButton(close)
        box.exec()
        if box.clickedButton() is report:
            self.open_bug_report(
                summary=title,
                current_context=redact_diagnostic_text(
                    message,app_root=Path(__file__).resolve().parents[1],
                ),
                feedback_type="bug",
            )

    def export_collected_feedback(self,_checked=False):
        app_root=Path(__file__).resolve().parents[1]
        dialog=FeedbackArchiveDialog(app_root/"feedback",self)
        if dialog.exec()!=QDialog.Accepted or dialog.archive_path is None:
            return
        details=(
            "A verified archive of the selected reviewed reports is ready:\n\n"
            f"{dialog.archive_path}\n\n"
            "Original feedback folders were preserved. Send this ZIP through the "
            "support or issue-reporting channel provided to you when ready; nothing "
            "was uploaded automatically."
        )
        if dialog.tracking_warning:
            details+=(
                "\n\nThe archive is valid, but some reports could not record their "
                "export history:\n"+dialog.tracking_warning
            )
        box=QMessageBox(QMessageBox.Information,"Feedback Archive Ready",details,QMessageBox.NoButton,self)
        copy_path=box.addButton("Copy Archive Path",QMessageBox.ActionRole)
        open_folder=box.addButton("Open Containing Folder",QMessageBox.ActionRole)
        close=box.addButton(QMessageBox.Close)
        box.setDefaultButton(close)
        box.exec()
        if box.clickedButton() is copy_path:
            QApplication.clipboard().setText(str(dialog.archive_path))
        elif box.clickedButton() is open_folder:
            QDesktopServices.openUrl(QUrl.fromLocalFile(str(dialog.archive_path.parent)))

    def show_current_help(self):
        page=self.stack.currentWidget()
        header=page.findChild(PageHeader) if page else None
        if header and header.help_text:
            header.show_help(); return
        if page and hasattr(page,"show_help"):
            page.show_help(); return
        QMessageBox.information(self,f"{PRODUCT_NAME} help","Choose a workspace from the left, then press F1 for help with that page.")

    def restore_library(self):
        QApplication.instance().setProperty(
            "collectigenNotificationPhase","remembered_library_restore"
        )
        registry=self.settings.library_registry()
        configured=any(
            not registry.validate(profile,replacing_id=profile.stable_id)
            for profile in registry.profiles
        )
        if not self.settings.restore_last_dataset or not configured:
            self.statusBar().showMessage("Ready")
            self._update_recommended(); self._start_ingest_refresh_once()
            QApplication.instance().setProperty(
                "collectigenNotificationPhase","ordinary"
            )
            return
        self.refresh_library(
            restore_cached=True,validate=self.settings.validate_library_on_startup,
        )

    def _start_ingest_refresh_once(self):
        """Refresh an already-open Ingest page without eagerly constructing it."""
        if not self._startup_ingest_refresh_pending:
            return
        self._startup_ingest_refresh_pending=False
        configured=str(self.settings.ingest_directory or "").strip()
        if not configured:
            return
        path=Path(configured)
        if not path.is_dir():
            return
        page=getattr(self,"ingest_page",None)
        if page is not None:
            page.activate(automatic=True)

    def _handle_ingest_attention(
        self,report: IngestAttentionReport,automatic: bool,
    ) -> None:
        if not isinstance(report,IngestAttentionReport):
            return
        self._ingest_attention_report=report if report.items else None
        previous=str(
            getattr(self.settings,"last_ingest_attention_fingerprint","") or ""
        )
        changed=bool(report.fingerprint and report.fingerprint!=previous)
        if not report.items:
            self.ingest_attention_button.hide()
            if previous:
                self.settings.last_ingest_attention_fingerprint=""
                try:
                    self.settings.save()
                except OSError:
                    pass
            return
        count=report.affected_images or len(report.items)
        label=f"Ingest Attention ({count:,})"
        detail=report.detailed_summary()
        self.ingest_attention_button.setText(label)
        self.ingest_attention_button.setToolTip(detail)
        self.ingest_attention_button.setAccessibleDescription(
            f"{detail} Activate to review these issues in Ingest."
        )
        self.ingest_attention_button.show()
        if changed:
            self.settings.last_ingest_attention_fingerprint=report.fingerprint
            try:
                self.settings.save()
            except OSError:
                self.ingest_attention_button.setToolTip(
                    detail+"\n\nRepeat-notice suppression could not be saved."
                )
        if not automatic or changed:
            self.statusBar().showMessage(
                report.short_summary()+" Review it from the Ingest Attention control.",
                15000,
            )
            if self.isVisible():
                announce_accessibly(
                    self.ingest_attention_button,
                    self.ingest_attention_button.accessibleName(),
                )

    def _open_ingest_attention(self):
        self.navigation.setCurrentRow(1)
        if self.navigation.currentRow()!=1:
            return
        self._ensure_page(1)
        self.ingest_page.review_attention()

    def choose_library(self):
        registry=self.settings.library_registry()
        counts={
            profile.stable_id:len(manager.images)
            for profile,manager in self.state.dataset.profile_managers()
        }
        dialog=LibraryScopeDialog(
            registry,self,
            selected_library_ids=self.settings.library_scope_ids,
            default_library_id=self.settings.active_library_id,
            counts_by_id=counts,
        )
        if dialog.exec()!=QDialog.Accepted:
            return
        try:
            if dialog.choose_folder:
                initial=(
                    self.settings.working_directory or self.settings.dataset_directory
                    or str(Path.cwd())
                )
                selected=QFileDialog.getExistingDirectory(
                    self,"Choose Image Library Folder",initial,
                )
                if not selected:
                    return
                profile=self.settings.select_library_root(selected)
                if self.settings.library_scope_ids:
                    self.settings.library_scope_ids=list(dict.fromkeys(
                        [*self.settings.library_scope_ids,profile.stable_id]
                    ))
            else:
                self.settings.library_scope_ids=list(dialog.scope_library_ids)
            self.settings.save()
        except ValueError as exc:
            QMessageBox.warning(
                self,"Library Folder Overlaps Another Library",
                f"{exc}\n\nChoose the existing named library in Setup, or select a separate folder. No settings or files were changed.",
            )
            return
        if dialog.choose_folder:
            self._pending_vocabulary_items=[]; self.dashboard.set_vocabulary_notice(0)
            self.refresh_library()
            return
        self.state.dataset.configure(
            self.settings.library_registry(),
            preferred_library_id=self.settings.active_library_id,
            scope_library_ids=self.settings.library_scope_ids,
        )
        self._library_scope_changed()

    def _refresh_library_scope_label(self) -> None:
        registry=self.settings.library_registry()
        valid=[
            profile for profile in registry.profiles
            if not registry.validate(profile,replacing_id=profile.stable_id)
        ]
        explicit=tuple(self.settings.library_scope_ids)
        included=[
            profile for profile in valid
            if not explicit or profile.stable_id in explicit
        ]
        if not valid:
            text="Libraries: None"
            detail="No valid named image libraries are configured."
        elif not explicit or len(included)==len(valid):
            text=f"Libraries: All ({len(valid):,})"
            detail=(
                f"All {len(valid):,} named libraries are included throughout the app. "
                "Choose to filter one or several collections."
            )
        elif len(included)==1:
            text=f"Library: {included[0].name}"
            detail=(
                f"Only {included[0].name} is currently included throughout the app. "
                "Choose to include more peer collections."
            )
        else:
            text=f"Libraries: {len(included):,} of {len(valid):,}"
            detail=(
                f"{len(included):,} of {len(valid):,} named libraries are included "
                "throughout the app."
            )
        self.library_scope_button.setText(text)
        self.library_scope_button.setToolTip(detail)
        self.library_scope_button.setAccessibleDescription(detail)

    def _gallery_library_identity(self,path):
        profile=self.settings.library_profile_for_path(path)
        if profile is None:
            return None
        availability=self.state.dataset.library_availability(profile.stable_id)
        status={
            "checking":"Checking files",
            "saved_checking":"Saved view; checking files",
            "validated_checking":"Browse ready; changes paused during startup",
            "validated_cancelled":"Browse ready; library checks paused",
            "saved_unverified":"Saved view; files unavailable",
            "unavailable":"Unavailable",
            "cancelled":"Partially checked",
            "not_loaded":"Not opened in this session",
        }.get(availability,"")
        storage=self.state.dataset.storage_availability(path)
        file_status=(
            "" if str(getattr(storage.state,"value",storage.state))=="available"
            else str(getattr(storage,"label","") or "")
        )
        role=profile.display_role+(
            " • "+" • ".join(value for value in (status,file_status) if value)
            if status or file_status else ""
        )
        return profile.name,role,profile.root,storage

    def _library_scope_changed(self) -> None:
        self._refresh_library_scope_label()
        missing=[
            profile.stable_id
            for profile,manager in self.state.dataset.scoped_profile_managers()
            if manager.root is None
        ]
        if missing and not self._library_scan_active:
            self.refresh_library(
                restore_cached=True,
                validate=self.settings.validate_library_on_startup,
            )
            return
        if hasattr(self,"dataset_page"):
            self.dataset_page.set_dataset(self.state.dataset)
        if hasattr(self,"review_page"):
            self.review_page.queue_page=0
            self.review_page.refresh(reload=False)
        if hasattr(self,"knowledge_page"):
            self.knowledge_page.refresh_visible_gallery_statuses()
        if hasattr(self,"setup_page"):
            self.setup_page._refresh_library_profiles()
        if any(manager.root is not None for manager in self.state.dataset.scoped_managers()):
            self._show_summary(self.state.dataset.summary,checking=False)
        else:
            self.dashboard.clear_overview()
        self.state.events.publish(
            "library_scope_changed",
            library_ids=self.state.dataset.scope_library_ids,
        )
        self._update_recommended()

    def refresh_moved_images(self,paths):
        self.refresh_library(changed_paths=tuple(paths))

    def refresh_library(
        self,*,restore_cached=False,validate=True,
        refresh_external_evidence=False,external_evidence_mode="",
        external_evidence_plan=None,changed_paths=None,
    ):
        # QThread.finished may have fired while its queued owner callback has
        # not yet settled. Keep the current refresh as owner until that callback
        # clears _scan_thread; a stopped thread is not an idle library view.
        if self._scan_thread is not None:
            if changed_paths is not None:
                pending=set(getattr(self,"_pending_move_refresh_paths",()))
                pending.update(Path(value) for value in changed_paths)
                self._pending_move_refresh_paths=pending
            return
        plan=(
            external_evidence_plan
            if isinstance(external_evidence_plan,ExternalEvidenceImportPlan) else None
        )
        if plan is not None and not plan.is_current():
            QMessageBox.information(
                self,"External evidence scope changed",
                "An image, TXT caption, or Image Knowledge file changed after the preview. "
                "Nothing was imported; open Import External Evidence and build the exact preview again.",
            )
            return
        if plan is not None:
            newly_held=[
                path for path in plan.ready_paths
                if not bool(getattr(
                    self.state.dataset.storage_availability(path),
                    "ready_for_write",False,
                ))
            ]
            if newly_held:
                QMessageBox.information(
                    self,"External evidence scope is no longer ready",
                    f"{len(newly_held):,} previewed image(s) became unavailable, "
                    "read-only, or conflicted. Nothing was imported; restore access "
                    "and build the exact preview again.",
                )
                return
        registry=self.settings.library_registry()
        self.state.dataset.configure(
            registry,preferred_library_id=self.settings.active_library_id,
            scope_library_ids=self.settings.library_scope_ids,
        )
        self._refresh_library_scope_label()
        profiles=[
            profile for profile in registry.profiles
            if (
                not registry.validate(profile,replacing_id=profile.stable_id)
                and profile.stable_id in self.state.dataset.scope_library_ids
            )
        ]
        paths_by_id={}
        if changed_paths is not None:
            for value in changed_paths:
                path=Path(value)
                profile=registry.profile_for_path(path)
                if profile is not None:
                    paths_by_id.setdefault(profile.stable_id,[]).append(path)
            profiles=[p for p in profiles if p.stable_id in paths_by_id]
        if plan is not None:
            for path in plan.ready_paths:
                identity=self.state.dataset.identity_for_path(path)
                if (
                    identity is None
                    or identity.library_id not in self.state.dataset.scope_library_ids
                    or Path(path) not in tuple(getattr(self.state.dataset,"images",()) or ())
                ):
                    QMessageBox.information(
                        self,"External evidence scope is unavailable",
                        "One or more previewed images no longer belong to the current included "
                        "libraries. Nothing was imported and the scope was not broadened.",
                    )
                    return
                paths_by_id.setdefault(identity.library_id,[]).append(Path(path))
            profiles=[
                profile for profile in profiles if profile.stable_id in paths_by_id
            ]
        if not profiles:
            if changed_paths is not None:
                return
            if plan is not None:
                QMessageBox.information(
                    self,"No ready external evidence",
                    "The frozen preview contains no currently writable images. Nothing was imported.",
                )
            else:
                self.choose_library()
            return
        from .history_workflow import history_configuration
        from core.history_workflow import configured_capture
        from core.history_configuration import HistoryConfigurationError
        history=history_configuration(self)
        native_external=False
        if plan is not None:
            try:
                # The preview may span a large library. Admit bounded exact
                # groups without reading files, and reject mixed participation
                # before creating any legacy checkpoint.
                participation=set()
                ready=tuple(plan.ready_paths)
                for start in range(0,len(ready),64):
                    admitted=configured_capture(history,ready[start:start+64],operation_kind="external_evidence_import")
                    participation.add(admitted is not None)
                if len(participation)>1:
                    raise HistoryConfigurationError("This selection mixes sources with and without saved history. Narrow the import scope or review History locations in Setup.")
                native_external=True in participation
            except (OSError,ValueError,RuntimeError) as exc:
                QMessageBox.information(self,"Saved history needs attention",str(exc))
                return
        label=(
            f"Importing external evidence for {len(plan.ready_paths):,} image(s)"
            if plan is not None else
            "Opening remembered libraries" if restore_cached else
            "Checking libraries for changes"
        )
        # A retained Dataset root is the existing view authority. Presentation
        # visibility must never decide whether saved/current data is reusable.
        had_ready_view=any(
            manager.root is not None
            for _profile,manager in self.state.dataset.scoped_profile_managers()
        )
        self._library_scan_active=True
        self._scan_cached_ready=had_ready_view; self._last_library_outcome=""
        self._scan_phase_label=label; self._update_recommended()
        started,active=self.state.begin_long_operation(
            "library",label,resource="io",exclusive=not restore_cached,
        )
        if not started:
            self._library_scan_active=False
            self._update_recommended()
            show_operation_in_progress(self,active)
            return
        self._library_refresh_queue=[profile.stable_id for profile in profiles]
        self._library_refresh_current=None; self._library_refresh_failures=[]
        self._library_refresh_cancelled=False
        self._library_refresh_restore_cached=bool(restore_cached)
        self._library_refresh_validate=bool(validate)
        self._library_refresh_external=bool(refresh_external_evidence or plan is not None)
        self._library_refresh_paths_by_id=paths_by_id
        self._library_refresh_kind=(
            "external_evidence" if plan is not None else
            "move" if changed_paths is not None else "library"
        )
        self._library_refresh_evidence_mode=(
            plan.mode if plan is not None else
            str(external_evidence_mode or "") if external_evidence_mode else
            "all" if refresh_external_evidence else ""
        )
        self._library_refresh_evidence_sources=(
            plan.sources if plan is not None else ("txt","metadata")
        )
        self._library_refresh_run_automatic_ai=plan is None and changed_paths is None
        self._library_refresh_scope_label=plan.scope_label if plan is not None else ""
        self._library_refresh_held=plan.held_count if plan is not None else 0
        self._library_refresh_excluded=(
            (Path(self.settings.problems_directory),)
            if self.settings.problems_directory else ()
        )
        self._library_refresh_evidence_checkpoint_service=None
        self._library_refresh_evidence_checkpoint_session=None
        self._library_refresh_history_configuration=history
        if plan is not None and not native_external:
            checkpoint_service=ExternalEvidenceImportCheckpointService(
                OperationJournal(USER_DATA_DIR/"operation_journal.jsonl"),
                rules_fingerprint_provider=lambda:
                    ImageAuthorityInitializationService(
                        knowledge=self.state.knowledge,
                    ).current_rules_fingerprint(),
            )
            try:
                checkpoint_session=checkpoint_service.begin(plan)
            except Exception as exc:
                self._library_scan_active=False
                self._library_refresh_queue=[]
                self.state.finish_long_operation("library")
                self._update_recommended()
                QMessageBox.information(
                    self,"External evidence import could not start",
                    "Nothing was imported. "+str(exc),
                )
                return
            self._library_refresh_evidence_checkpoint_service=checkpoint_service
            self._library_refresh_evidence_checkpoint_session=checkpoint_session
        self._schema_migration_results=[]
        self._authority_initialization_results=[]
        self._automatic_ai_results=[]
        self._start_next_library_refresh()

    def _start_next_library_refresh(self):
        if not self._library_refresh_queue:
            self._finish_federated_refresh(); return
        library_id=self._library_refresh_queue.pop(0)
        profile=self.settings.library_registry().get(library_id)
        manager=self.state.dataset.manager(library_id)
        if profile is None or manager is None:
            self._start_next_library_refresh(); return
        self._library_refresh_current=profile
        self.state.dataset.set_library_availability(library_id,"checking")
        path=Path(profile.root)
        thread=QThread(self); worker=DatasetRefreshWorker(
            manager,path,self.settings.include_subfolders,self._library_refresh_excluded,
            restore_cached=self._library_refresh_restore_cached,
            validate=self._library_refresh_validate,
            migrate_legacy=self._library_refresh_restore_cached,
            knowledge=self.state.knowledge,
            operation_journal=OperationJournal(USER_DATA_DIR/"operation_journal.jsonl"),
            settings=self.settings,ai_service=self.state.ai,
            ai_work_lease=self.state.ai_work_lease,
            refresh_external_evidence=self._library_refresh_external,
            external_evidence_mode=self._library_refresh_evidence_mode,
            external_evidence_sources=self._library_refresh_evidence_sources,
            managed_paths=(
                self._library_refresh_paths_by_id.get(library_id)
                if self._library_refresh_kind=="external_evidence" else None
            ),
            changed_paths=(
                self._library_refresh_paths_by_id.get(library_id)
                if self._library_refresh_kind=="move" else None
            ),
            run_automatic_ai=self._library_refresh_run_automatic_ai,
            external_evidence_checkpoint_service=(
                self._library_refresh_evidence_checkpoint_service
            ),
            external_evidence_checkpoint_session=(
                self._library_refresh_evidence_checkpoint_session
            ),
            history_configuration=getattr(self,"_library_refresh_history_configuration",None),
        ); worker.moveToThread(thread)
        thread.started.connect(worker.run); worker.cached_ready.connect(self._cached_ready,Qt.QueuedConnection)
        worker.inventory_ready.connect(self._inventory_ready,Qt.QueuedConnection)
        worker.phase.connect(self._scan_phase,Qt.QueuedConnection)
        worker.schema_migration_complete.connect(self._schema_migration_complete,Qt.QueuedConnection)
        worker.authority_initialization_complete.connect(self._authority_initialization_complete,Qt.QueuedConnection)
        worker.automatic_ai_complete.connect(self._automatic_ai_complete,Qt.QueuedConnection)
        worker.progress.connect(self._scan_progress,Qt.QueuedConnection)
        worker.complete.connect(self._library_member_complete,Qt.QueuedConnection)
        worker.cancelled.connect(self._library_member_cancelled,Qt.QueuedConnection)
        worker.failed.connect(self._library_member_failed,Qt.QueuedConnection)
        worker.finished.connect(thread.quit)
        # Keep the worker alive through thread shutdown; Qt processes its
        # deferred deletion after QThread.finished even though the worker event
        # loop has stopped. The owner retires the QThread only after wait(0).
        thread.finished.connect(worker.deleteLater)
        thread.finished.connect(self._scan_thread_finished,Qt.QueuedConnection)
        self._scan_thread=thread; self._scan_worker=worker
        self._scan_validating=self._library_refresh_validate
        self._scan_phase_label=(
            "Importing external evidence"
            if self._library_refresh_kind=="external_evidence" else
            "Opening the remembered library"
            if self._library_refresh_restore_cached else
            "Checking the library for changes"
        )
        self._scan_phase_label=f"{self._scan_phase_label}: {profile.name}"
        self.scan_cancel_button.setText("Cancel Safely")
        self.scan_cancel_button.setEnabled(True); self.scan_cancel_button.show()
        self.state.update_long_operation("library",0,0,self._scan_phase_label)
        if self.statusBar().currentMessage() in {
            "Ready","Opening the remembered library in the background…",
            "Importing current TXT and Metadata and applying automatic tag rules…",
        }:
            self.statusBar().clearMessage()
        thread.start()

    @Slot(object)
    def _library_member_complete(self,_summary):
        profile=self._library_refresh_current
        if profile is not None:
            self.state.dataset.set_library_availability(profile.stable_id,"current")
            manager=self.state.dataset.manager(profile.stable_id)
            self.state.events.publish(
                "library_data_changed",
                **({"exact_changes":True} if self._library_refresh_kind=="move" else {}),
                library_id=profile.stable_id,
                images=tuple(
                    str(path)
                    for path in sorted(
                        (
                            self._library_refresh_paths_by_id.get(profile.stable_id,())
                            if self._library_refresh_kind in {"external_evidence","move"} else
                            getattr(manager,"changed_images",())
                        ),key=str,
                    )
                ),
            )

    @Slot(object)
    def _library_member_cancelled(self,_summary):
        profile=self._library_refresh_current
        if (
            profile is not None
            and self.state.dataset.library_availability(profile.stable_id)!="current"
        ):
            previous=self.state.dataset.library_availability(profile.stable_id)
            self.state.dataset.set_library_availability(
                profile.stable_id,
                "validated_cancelled" if previous in {
                    "validated_checking","validated_cancelled",
                } else "cancelled",
            )
        self._library_refresh_cancelled=True
        self._library_refresh_queue=[]

    @Slot(str)
    def _library_member_failed(self,error):
        profile=self._library_refresh_current
        if profile is not None:
            manager=self.state.dataset.manager(profile.stable_id)
            if self.state.dataset.library_availability(profile.stable_id)!="current":
                self.state.dataset.set_library_availability(
                    profile.stable_id,
                    "saved_unverified" if manager is not None and manager.images else "unavailable",
                )
        self._library_refresh_failures.append((
            profile.name if profile is not None else "Unknown library",str(error),
        ))

    def force_refresh_external_metadata(self,plan=None):
        """Start one already reviewed Dataset evidence plan."""
        if not isinstance(plan,ExternalEvidenceImportPlan):
            self.navigation.setCurrentRow(2)
            return TagManagerWindow._queue_dataset_page_action(
                self,
                lambda:self.dataset_page.open_external_evidence_import()
            )
        if self._scan_thread is not None:
            show_operation_in_progress(self,"Library check or evidence import")
            return
        self.refresh_library(external_evidence_plan=plan)

    def apply_source_automation_now(self):
        """Honor newly enabled source automation for the included libraries now."""
        if not self.state.dataset.scope_library_ids:
            message="Automatic evidence intake is enabled and will run when a library is opened"
            self.statusBar().showMessage(message)
            if hasattr(self,"automation_page"):
                self.automation_page.header.set_status(message)
            return
        self.statusBar().showMessage(
            "Importing current TXT and Metadata and applying automatic tag rules…"
        )
        self.refresh_library(
            refresh_external_evidence=True,external_evidence_mode="changed",
        )

    def force_ai_from_anywhere(self,paths,kind: str):
        paths=tuple(Path(value) for value in paths)
        if not paths: return
        def run_batch_ai():
            held=[
                path for path in paths
                if self.state.dataset.manager_for_path(path) is not None
                and not self.state.dataset.storage_availability(
                    path,
                ).ready_for_write
            ]
            if held:
                QMessageBox.information(
                    self,"Complete selection is not ready for AI",
                    f"{len(held):,} of {len(paths):,} selected image(s) are online-only, conflicted, read-only, or unavailable. Nothing was queued.\n\n"
                    "Restore local access or resolve the conflict, refresh the library, then choose the complete group again.",
                )
                return False
            return self.dataset_page.run_batch_ai(
                kind,paths,force_override=True,
            )
        self.navigation.setCurrentRow(2)
        return TagManagerWindow._queue_dataset_page_action(
            self,run_batch_ai,
        )

    @Slot()
    def _scan_thread_finished(self):
        thread=self._scan_thread
        if thread is None:
            return
        # QThread.finished precedes native thread-local cleanup. Do not drop
        # the final worker/thread wrappers or publish the next library result
        # until all thread effects are synchronized. A short queued retry keeps
        # the GUI responsive when cleanup has not settled yet.
        if not thread.wait(0):
            QTimer.singleShot(10,self._scan_thread_finished)
            return
        worker=self._scan_worker
        self._scan_worker=None
        self._scan_thread=None
        thread.deleteLater()
        if self._close_after_scan:
            self._library_refresh_queue=[]
            # The owner may have requested close after QThread.finished was
            # emitted but before this queued slot ran. Resume only after the
            # scan owner and its pending completion callbacks are retired.
            QTimer.singleShot(0,self.close)
            return
        if self._library_refresh_cancelled:
            self._finish_federated_refresh(); return
        self._start_next_library_refresh()

    def _finish_federated_refresh(self):
        summary=self.state.dataset.summary
        self._library_refresh_current=None
        if self._library_refresh_cancelled:
            self._scan_cancelled(summary)
            self._resume_pending_move_refresh()
            return
        failures=list(self._library_refresh_failures)
        if failures and not self.state.dataset.images:
            self._scan_failed("\n".join(
                f"{name}: {error}" for name,error in failures
            ))
            self._resume_pending_move_refresh()
            return
        self._scan_complete(summary)
        self._resume_pending_move_refresh()
        if failures:
            details="\n".join(f"- {name}: {error}" for name,error in failures)
            self.show_reportable_error(
                "Some named libraries could not be checked",
                "Other included libraries remain available. Saved cached views "
                "were retained where possible.\n\n"+details,
                severity="medium",
            )

    def _resume_pending_move_refresh(self):
        pending=getattr(self,"_pending_move_refresh_paths",set())
        if pending and not getattr(self,"_close_after_scan",False):
            self._pending_move_refresh_paths=set()
            QTimer.singleShot(0,lambda paths=tuple(pending):self.refresh_moved_images(paths))

    @Slot(object)
    def _cached_ready(self,summary):
        self._scan_cached_ready=True
        current=self._library_refresh_current
        if current is not None:
            self.state.dataset.set_library_availability(
                current.stable_id,"saved_checking",
            )
        manager=(
            self.state.dataset.manager(current.stable_id)
            if current is not None else None
        )
        index=getattr(manager,"index",None)
        if index is not None:
            self.content_safety.prime_saved_records(index.all_records())
        if hasattr(self,"dataset_page"): self.dataset_page.set_dataset(self.state.dataset)
        combined=self.state.dataset.summary
        self._show_summary(combined,checking=self._scan_validating)

    @Slot(object)
    def _inventory_ready(self,summary):
        # Inventory has now established exact non-hydrating storage state for
        # every current image. Browsing can use that validated view immediately,
        # while writes remain held until startup initialization, analysis, and
        # migration have released the same image/database resources.
        current=self._library_refresh_current
        if current is not None:
            self.state.dataset.set_library_availability(
                current.stable_id,"validated_checking",
            )
        if (getattr(self,"_library_refresh_kind","")!="move"
                and hasattr(self,"dataset_page")):
            self.dataset_page.set_dataset(self.state.dataset)
        if not self._scan_cached_ready:self.dashboard.clear_overview()

    @Slot(str)
    def _scan_phase(self,label):
        current=self._library_refresh_current
        self._scan_phase_label=(
            f"{label}: {current.name}" if current is not None else label
        )
        self._update_recommended()
        self.state.update_long_operation("library",0,0,self._scan_phase_label)

    @Slot(int,int,str)
    def _scan_progress(self,index,total,name):
        self.state.update_long_operation("library",index,total,name)

    @Slot(object)
    def _schema_migration_complete(self,result):
        self._schema_migration_results.append(result)

    @Slot(object)
    def _authority_initialization_complete(self,result):
        self._authority_initialization_results.append(result)
        deferred=tuple(dict.fromkeys(
            Path(value)
            for value in getattr(result,"deferred_rule_refresh_paths",())
        ))
        if deferred:
            # Keep the exact stale scope, but do not immediately compete with
            # the visible library restore and Ingest readiness scan. The
            # preemptible coordinator starts it after those foreground startup
            # stages have had an opportunity to acquire their resource slot.
            pending=getattr(self,"_pending_startup_derived_paths",None)
            if pending is None:
                pending=set(); self._pending_startup_derived_paths=pending
            pending.update(deferred)

    def _schedule_startup_derived_refresh(self):
        if (
            not self._pending_startup_derived_paths
            or self._startup_derived_refresh_scheduled
            or self.state.jobs.shutting_down
        ):
            return
        self._startup_derived_refresh_scheduled=True
        QTimer.singleShot(5000,self._start_startup_derived_refresh)

    def _start_startup_derived_refresh(self):
        self._startup_derived_refresh_scheduled=False
        if getattr(self,"_last_library_outcome","")=="cancelled":
            return  # Keep pending scope for a deliberate library Resume.
        if self.state.jobs.shutting_down or not self._pending_startup_derived_paths:
            return
        if getattr(self,"_vocabulary_task",None) is not None:
            # Do not overlap two rebuildable library-wide startup consumers.
            # Vocabulary discovery finishes first; derived refresh keeps its
            # exact coalesced scope and retries during the next idle window.
            self._startup_derived_refresh_scheduled=True
            QTimer.singleShot(2000,self._start_startup_derived_refresh)
            return
        deferred=tuple(sorted(
            self._pending_startup_derived_paths,key=lambda path:str(path).casefold(),
        ))
        self._pending_startup_derived_paths.clear()
        self.state.derived_refresh.request(
            deferred,
            cause=(
                "System Knowledge or Automation changed; refreshing "
                "rebuildable local results"
            ),
        )

    @Slot(object)
    def _automatic_ai_complete(self,result):
        self._automatic_ai_results.append(result)

    def _cancel_library_scan(self):
        owner=getattr(self,"_footer_cancel_owner","") or getattr(self,"_displayed_long_operation_owner","")
        if not owner:
            owner="content_safety" if self._content_safety_task is not None else "library"
        if owner=="content_safety" and self._content_safety_task is not None:
            self._long_operation_cancel_pending_owner=owner
            self.state.request_long_operation_cancel("content_safety")
            self._content_safety_task.cancel()
            TagManagerWindow._render_long_operation_cancel(self,owner)
            self.statusBar().showMessage("Stopping after the current content-rating check...")
            return
        if owner!="library":return
        worker=self._scan_worker
        if worker is None: return
        self._long_operation_cancel_pending_owner=owner
        self.state.request_long_operation_cancel("library")
        worker.cancel()
        TagManagerWindow._publish_library_stop_boundary(self,closing=False)

    def _publish_library_stop_boundary(self,*,closing: bool):
        event=getattr(self,"_long_operation_events",{}).get("library")
        _label,current,total,message=(event or ("",0,0,""))
        context=" ".join((
            str(getattr(self,"_scan_phase_label","") or ""),str(message or ""),
        )).casefold()
        if "metadata batch" in context:
            boundary="the current Metadata batch"
        elif any(value in context for value in (
            "checking the library","opening the remembered library",
            "validating saved image data",
        )):
            boundary="the current library filesystem step"
        else:
            boundary="the current image-record checkpoint"
        action="Closing" if closing else "Stopping"
        detail=(
            f"{action} safely after {boundary}; completed checkpoints will be kept"
        )
        self.state.update_long_operation("library",current,total,detail)
        TagManagerWindow._render_long_operation_cancel(self,"library")
        self.statusBar().showMessage(detail+"…")

    def _scan_complete(self,summary):
        QApplication.instance().setProperty(
            "collectigenNotificationPhase","library_completion"
        )
        self._library_scan_active=False; self._last_library_outcome="ready"; self.state.finish_long_operation("library")
        self._refresh_library_scope_label()
        if hasattr(self,"setup_page"):self.setup_page._refresh_library_profiles()
        changed_images=(
            tuple(
                path for paths in self._library_refresh_paths_by_id.values()
                for path in paths
            )
            if self._library_refresh_kind=="external_evidence" else
            tuple(getattr(self.state.dataset,"changed_images",()))
        )
        self.content_safety.invalidate(changed_images)
        self.content_safety.confirm_validated_records(
            getattr(self.state.dataset,"validated_records",{}).values()
        )
        self._refresh_protected_previews(
            changed_images,preserve_temporary_reveal=False
        )
        if hasattr(self,"dataset_page"): self.dataset_page.set_dataset(self.state.dataset)
        self.refresh_review_count()
        self._show_summary(summary,checking=False)
        migrations=list(self._schema_migration_results)
        authorities=list(self._authority_initialization_results)
        automatic_results=list(self._automatic_ai_results)
        checkpoint_session=getattr(
            self,"_library_refresh_evidence_checkpoint_session",None,
        )
        evidence_counts=(
            getattr(
                checkpoint_session,"completed_counts",
                ExternalEvidenceImportCounts(),
            )
            if self._library_refresh_kind=="external_evidence" else
            ExternalEvidenceImportCounts()
        )
        for authority in authorities:
            evidence_counts+=getattr(
                authority,"evidence_counts",ExternalEvidenceImportCounts(),
            )
        recovered_count=len(
            getattr(checkpoint_session,"completed_paths",())
        )
        upgraded=sum(int(getattr(result,"upgraded",0)) for result in migrations)
        migration_issues=sum(len(getattr(result,"issues",())) for result in migrations)
        migration_note=""
        if upgraded:
            migration_note=f" • safely upgraded {upgraded:,} legacy records"
        if migration_issues:
            migration_note+=f" • {migration_issues:,} record issue(s) left unchanged"
        held=sum(int(getattr(summary,field,0) or 0) for field in (
            "online_only","conflicts","read_only","unavailable",
        ))
        if held:
            migration_note+=f" • {held:,} storage-held image(s) left unopened"
        self.statusBar().showMessage(
            f"Libraries ready • {summary.images:,} images • reused {summary.reused_from_cache:,} unchanged records{migration_note}",
            15000,
        )

        if authorities:
            created=sum(int(getattr(result,"created",0)) for result in authorities)
            refreshed=sum(int(getattr(result,"refreshed",0)) for result in authorities)
            reused=sum(int(getattr(result,"reused",0)) for result in authorities)
            failed=sum(int(getattr(result,"failed",0)) for result in authorities)
            ai_processed=sum(int(getattr(result,"processed",0)) for result in automatic_results)
            ai_skipped=sum(int(getattr(result,"skipped",0)) for result in automatic_results)
            ai_note=(
                f" | AI analyzed {ai_processed:,}, skipped {ai_skipped:,} current"
                if automatic_results else ""
            )
            storage_note=(
                f" | {held:,} storage-held image(s) left unopened" if held else ""
            )
            if self._library_refresh_kind=="external_evidence":
                processed=sum(int(getattr(result,"processed",0)) for result in authorities)
                evidence_outcome=(
                    "finished with problems" if failed else "complete"
                )
                current_reused=max(
                    0,reused-recovered_count-evidence_counts.unchanged_images,
                )
                result_detail="\n".join(evidence_counts.summary_lines(
                    current_reused=current_reused,recovered=recovered_count,
                    held=self._library_refresh_held,failed=failed,
                ))
                self.statusBar().showMessage(
                    f"External evidence import {evidence_outcome} | {self._library_refresh_scope_label}"
                    f" | checked {processed:,} image(s) | updated {created+refreshed:,}"
                    f" | reread {evidence_counts.reread_images:,} | reused {current_reused:,} current"
                    f" | recovered {recovered_count:,} | held {self._library_refresh_held:,}"
                    f" | {failed:,} problem(s)",
                    20000,
                )
                if hasattr(self,"dataset_page"):
                    self.dataset_page.header.set_status(
                        f"Evidence import {evidence_outcome} • updated {created+refreshed:,} • "
                        f"reviewable findings {evidence_counts.reviewable_findings:,}"
                    )
                    self.dataset_page.header.status.setToolTip(result_detail)
                    self.dataset_page.header.status.setAccessibleDescription(
                        result_detail
                    )
                QTimer.singleShot(
                    0,lambda outcome=(
                        "failed" if failed else "complete"
                    ),scope=self._library_refresh_scope_label,
                    updated=created+refreshed,counts=evidence_counts,
                    current=current_reused,recovered=recovered_count,
                    held_count=self._library_refresh_held,failed_count=failed:
                    external_evidence_result_dialog(
                        self,outcome=outcome,scope_label=scope,
                        updated=updated,counts=counts,
                        current_reused=current,recovered=recovered,
                        held=held_count,failed=failed_count,
                    ).exec()
                )
            else:
                self.statusBar().showMessage(
                    f"Libraries ready | {summary.images:,} images | created {created:,} records"
                    f" | refreshed {refreshed:,} | reused {reused:,} current image records"
                    f"{ai_note}{storage_note} | {failed:,} initialization problems",
                    15000,
                )
        if self._library_refresh_kind=="external_evidence":
            authority_failures=sum(
                int(getattr(value,"failed",0)) for value in authorities
            )
            evidence_failed=bool(
                self._library_refresh_failures or authority_failures
            )
            self._finish_external_evidence_checkpoint(
                "failed" if evidence_failed else "complete",
                result={
                    "scope_label":self._library_refresh_scope_label,
                    "processed":sum(
                        int(getattr(value,"processed",0)) for value in authorities
                    ),
                    "updated":sum(
                        int(getattr(value,"created",0))
                        +int(getattr(value,"refreshed",0))
                        for value in authorities
                    ),
                    "reused":sum(
                        int(getattr(value,"reused",0)) for value in authorities
                    ),
                    "failed":authority_failures,
                    "recovered":recovered_count,
                    **evidence_counts.to_dict(),
                },
                message=(
                    "One or more images or named libraries could not complete the exact evidence import."
                    if evidence_failed else
                    "The exact reviewed external-evidence scope completed."
                ),
            )
        blocking_issues=[
            issue for authority in authorities
            for issue in getattr(authority,"issues",()) if issue.blocking
        ]
        metadata_issue_count=sum(
            issue.code=="metadata_unavailable"
            for authority in authorities
            for issue in getattr(authority,"issues",())
        )
        if blocking_issues:
            combined=type("CombinedAuthorityIssues",(),{"issues":blocking_issues})()
            QTimer.singleShot(
                0,lambda result=combined:self._show_authority_issues(result)
            )
        ai_errors=[
            str(error) for result in automatic_results
            for error in getattr(result,"errors",())
        ]
        routine_notes=[]
        if metadata_issue_count:
            routine_notes.append(
                f"{metadata_issue_count:,} optional Metadata read problem(s)"
            )
        if ai_errors:
            routine_notes.append(
                f"{len(ai_errors):,} automatic AI problem(s) will retry later"
            )
        if routine_notes:
            current=self.statusBar().currentMessage()
            self.statusBar().showMessage(
                current+" • "+" • ".join(routine_notes),20000,
            )
        if self._library_refresh_kind!="external_evidence":
            self._start_ingest_refresh_once()
        self._schedule_startup_derived_refresh()
        if self._library_refresh_kind!="move":
            QTimer.singleShot(0,self._offer_new_vocabulary)
        if self._pending_completed_dataset_image is not None:
            QTimer.singleShot(0,self._finish_pending_completed_dataset_handoff)

    def _show_authority_issues(self,result):
        if not any(item.blocking for item in result.issues):
            return
        title,message=authority_issue_dialog(result)
        QMessageBox.warning(self,title,message)

    def _scan_cancelled(self,summary):
        QApplication.instance().setProperty(
            "collectigenNotificationPhase","library_completion"
        )
        self._library_scan_active=False; self._last_library_outcome="cancelled"; self.state.finish_long_operation("library")
        self._pending_completed_dataset_image=None
        self._refresh_library_scope_label()
        if hasattr(self,"setup_page"):self.setup_page._refresh_library_profiles()
        if hasattr(self,"dataset_page"): self.dataset_page.set_dataset(self.state.dataset)
        self.refresh_review_count()
        self._show_summary(summary,checking=False)
        self.statusBar().showMessage(
            (
                "External evidence import stopped safely; completed image checkpoints were kept"
                if self._library_refresh_kind=="external_evidence" else
                "Library initialization paused safely; refresh to resume unfinished images"
            ),15000,
        )
        if self._library_refresh_kind=="external_evidence":
            self._finish_external_evidence_checkpoint(
                "cancelled",
                message=(
                    "Stopped safely. Build the same exact evidence preview to "
                    "resume verified completed images."
                ),
            )
        # Cancellation is a pause boundary. Do not launch classification or
        # derived followups until the user resumes the library check.
        QApplication.instance().setProperty(
            "collectigenNotificationPhase","ordinary"
        )

    def _offer_new_vocabulary(self):
        app=QApplication.instance()
        app.setProperty(
            "collectigenNotificationPhase","startup_vocabulary_check"
        )
        try:
            if self.ui_test or not self.state.dataset.tag_counts:
                self._pending_vocabulary_items=[]
                self.dashboard.set_vocabulary_notice(0)
                return
            if getattr(self,"_vocabulary_task",None) is not None:
                return

            def work(job):
                if not job.checkpoint():
                    raise InterruptedError("Vocabulary discovery cancelled safely")
                intake=self._vocabulary_intake()
                repair_error=""
                try:
                    intake.repair_acknowledged(self.state.dataset.tag_counts)
                except Exception as exc:
                    repair_error=str(exc)
                if not job.checkpoint():
                    raise InterruptedError("Vocabulary discovery cancelled safely")
                items=self._find_new_vocabulary(
                    intake,should_continue=job.checkpoint,
                    progress=lambda current,total,label:job.update(
                        current,total,label,
                    ),
                )
                return {"items":items,"repair_error":repair_error}

            task=CoordinatedCallTask(
                self.state.jobs,"Check for new SDXL Tag names",work,
                resource="cpu",key="startup-vocabulary-discovery",
            )
            self._vocabulary_task=task
            task.signals.complete.connect(
                lambda result,owner=task:self._vocabulary_discovery_complete(
                    owner,result,
                )
            )
            task.signals.failed.connect(
                lambda message,owner=task:self._vocabulary_discovery_failed(
                    owner,message,
                )
            )
            task.signals.cancelled.connect(
                lambda owner=task:self._vocabulary_discovery_cancelled(owner)
            )
            try:
                task.start()
            except RuntimeError as exc:
                self._vocabulary_discovery_failed(task,str(exc))
        finally:
            app.setProperty("collectigenNotificationPhase","ordinary")

    def _vocabulary_discovery_complete(self,task,result):
        if task is not self._vocabulary_task:
            return
        self._vocabulary_task=None
        payload=result if isinstance(result,dict) else {}
        repair_error=str(payload.get("repair_error") or "")
        if repair_error:
            QMessageBox.warning(
                self,"A saved vocabulary decision needs attention",
                "CollectiGen could not restore a missing Review Center question from an earlier interrupted save. "
                f"No Image Knowledge was changed.\n\n{repair_error}",
            )
        items=list(payload.get("items") or ())
        self._pending_vocabulary_items=items
        self.dashboard.set_vocabulary_notice(len(items))
        self.refresh_review_count()
        if items:
            self.statusBar().showMessage(
                f"Library ready • {len(items):,} new SDXL Tag name{'s are' if len(items) != 1 else ' is'} ready on the Dashboard",
                15000,
            )

    def _vocabulary_discovery_failed(self,task,message):
        if task is not self._vocabulary_task:
            return
        self._vocabulary_task=None
        self.statusBar().showMessage(
            "The background vocabulary check could not finish; use Find New Knowledge to retry. "
            +str(message),15000,
        )

    def _vocabulary_discovery_cancelled(self,task):
        if task is self._vocabulary_task:
            self._vocabulary_task=None

    def _vocabulary_intake(self):
        discovery=DiscoveryRegistry(DISCOVERED_TAGS_PATH)
        patterns=PatternRegistry(DEFAULTS_DIR/"patterns.json",PATTERNS_PATH)
        suggestions=SuggestionEngine(
            FoundationKnowledge(DEFAULTS_DIR/"modifiers.json",DEFAULTS_DIR/"concepts.json"),
            patterns,self.state.knowledge,
        )
        automation=AutomationRuleRepository(
            DEFAULTS_DIR/"automation_rules.json",USER_DATA_DIR/"automation_rules.json",
            rule_pair_admission=RulePairAdmission(USER_DATA_DIR),
        )
        character_resolver=CharacterVocabularyResolver(
            self.state.knowledge,CharacterDatabase(USER_DATA_DIR),
            getattr(self.state.knowledge,"_categories",None),
        )
        return VocabularyIntakeService(
            self.state.knowledge,self.state.reviews,discovery,suggestions,automation,
            character_resolver=character_resolver,
            default_relationships_path=DEFAULTS_DIR/"relationships.json",
            user_relationships_path=RELATIONSHIPS_PATH,
        )

    def _find_new_vocabulary(
        self,intake,*,should_continue=None,progress=None,
    ):
        records=self.state.dataset.index.all_records() if self.state.dataset.index else ()
        return intake.find_new(
            self.state.dataset.tag_counts,records,
            tag_sources=self.state.dataset.tag_sources,
            tag_examples=self.state.dataset.tag_examples,
            should_continue=should_continue,progress=progress,
        )

    def find_new_knowledge(self):
        """Build one non-mutating local report and open its chosen owner."""
        try:
            intake=self._vocabulary_intake()
            items=self._find_new_vocabulary(intake)
            meaning_evidence=TagMeaningEvidenceStore(TAG_MEANING_EVIDENCE_PATH)
            meaning_discovery=TagMeaningDiscoveryService(
                self.state.knowledge,meaning_evidence,
            )
            meaning_report=meaning_discovery.discover()
            unrecorded_meanings=meaning_report.unrecorded(self.state.reviews)
            meaning_issue_counts={}
            for issue in meaning_report.issues:
                reason=str(issue.reason or "other")
                meaning_issue_counts[reason]=meaning_issue_counts.get(reason,0)+1
            broader_discovery=TagBroaderDiscoveryService(
                self.state.knowledge,getattr(
                    self.knowledge_page,"categories",
                    getattr(self.state.knowledge,"_categories",None),
                ),
                intake.suggestions,
                default_relationship_path=DEFAULTS_DIR/"relationships.json",
                user_relationship_path=RELATIONSHIPS_PATH,
            )
            broader_report=broader_discovery.discover()
            unrecorded_broader=broader_report.unrecorded(self.state.reviews)
            unrecorded_held=getattr(broader_report,"unrecorded_held",None)
            held_broader=(
                tuple(unrecorded_held(self.state.reviews))
                if callable(unrecorded_held)
                else tuple(getattr(broader_report,"held_candidates",()))
            )
            broader_issue_counts={}
            for issue in broader_report.issues:
                reason=str(issue.reason or "other")
                broader_issue_counts[reason]=broader_issue_counts.get(reason,0)+1
            modifier_roles=ModifierRoleDecisionService(
                self.state.knowledge,getattr(
                    self.knowledge_page,"categories",
                    getattr(self.state.knowledge,"_categories",None),
                ),
                intake.suggestions.foundation,
                default_path=DEFAULTS_DIR/"relationships.json",
                user_path=RELATIONSHIPS_PATH,
            )
            modifier_inventory=modifier_roles.inventory()
            modifier_review_count=modifier_inventory.review_required_count
            category_base_report=CategoryBaseDiscoveryService(
                self.state.knowledge,getattr(
                    self.knowledge_page,"categories",
                    getattr(self.state.knowledge,"_categories",None),
                ),
                default_relationship_path=DEFAULTS_DIR/"relationships.json",
                user_relationship_path=RELATIONSHIPS_PATH,
            ).discover()
            safe_category_bases=category_base_report.independently_safe
            held_category_bases=category_base_report.broader_scope_required
        except Exception as exc:
            QMessageBox.warning(
                self,"Local Knowledge check could not finish",
                "CollectiGen could not compare the current indexed vocabulary "
                "with saved System Knowledge. No source file or saved decision "
                f"was changed.\n\n{exc}",
            )
            return False
        self._pending_vocabulary_items=list(items)
        self.dashboard.set_vocabulary_notice(len(items))
        pending_questions=sum(
            1 for task in self.state.reviews.tasks.values()
            if task.status=="pending"
            and task.task_type in {
                "new_tag","pattern_group",TAG_MEANING_TASK_TYPE,
                TAG_BROADER_PROPOSAL_TASK_TYPE,
            }
            and self.state.reviews.queue_health(task)[0]=="meaningful"
        )
        collection_questions=sum(
            1 for task in self.state.reviews.tasks.values()
            if task.status=="pending"
            and task.task_type=="collection_member"
            and review_task_in_library_scope(
                task,getattr(self.state,"dataset",None),
            )
        )
        source_questions=sum(
            1 for task in self.state.reviews.tasks.values()
            if task.status=="pending"
            and task.task_type in SOURCE_TASK_TYPES
            and review_task_in_library_scope(
                task,getattr(self.state,"dataset",None),
            )
        )
        collection_index=getattr(
            getattr(getattr(self.state,"dataset",None),"index",None),
            "all_records",None,
        )
        collection_check_available=callable(collection_index)
        review_counts=self.knowledge_page.local_discovery_review_counts()
        report=QMessageBox(self)
        report.setWindowTitle("Find New Knowledge")
        report.setIcon(QMessageBox.Information)
        needs_organization=int(review_counts.get("needs_organization") or 0)
        missing_relationships=int(review_counts.get("missing_relationships") or 0)
        active_areas=sum(bool(count) for count in (
            len(items),pending_questions,collection_questions,source_questions,
            len(unrecorded_meanings),len(unrecorded_broader)+len(held_broader),
            modifier_review_count,
            len(category_base_report.candidates),
            needs_organization,missing_relationships,
        ))
        report.setText(
            f"{active_areas} local Knowledge review area"
            f"{'s' if active_areas!=1 else ''} have saved findings"
            if active_areas else "No saved findings are waiting in these review areas"
        )
        report.setInformativeText(
            "New Tag names awaiting a durable first decision: "
            f"{len(items):,}\n"
            "Tag meaning, related-family, or Broader Tag questions: "
            f"{pending_questions:,}\n"
            "New attributed Meaning & Usage evidence groups: "
            f"{len(unrecorded_meanings):,}\n"
            "Attributed observations not queued: "
            f"{len(meaning_report.issues):,}\n"
            "New exact Broader Tag proposals: "
            f"{len(unrecorded_broader):,}\n"
            "Broader Tag findings requiring broader Category review: "
            f"{len(held_broader):,}\n"
            "Broader Tag blockers or bounds reported (including held): "
            f"{len(broader_report.issues):,}\n"
            "Legacy Modifier Role candidates needing review: "
            f"{modifier_review_count:,}\n"
            "Behavior-preserving Category Base candidates: "
            f"{len(safe_category_bases):,}\n"
            "Category Base candidates requiring broader explicit review: "
            f"{len(held_category_bases):,}\n"
            "Character/outfit collection questions: "
            f"{collection_questions:,}\n"
            "Character Source/Other Name questions: "
            f"{source_questions:,}\n"
            "Tags needing organization: "
            f"{needs_organization:,}\n"
            "Tags missing reusable relationship context: "
            f"{missing_relationships:,}\n\n"
            "Counts may overlap; each action opens the existing canonical owner."
        )
        report.setDetailedText(
            "Evidence scope: current indexed local vocabulary, saved System "
            "Knowledge, the saved Review queue, and the bounded local attributed "
            "Meaning & Usage evidence inbox.\n"
            f"Evidence inbox processed this pass: {meaning_report.processed_observations:,}"
            +(f"; left for a later bounded pass: {meaning_report.bounded_observations:,}" if meaning_report.bounded_observations else "")
            +".\n"
            +(
                "Evidence not queued by reason: "+", ".join(
                    f"{key.replace('_',' ')} = {value:,}"
                    for key,value in sorted(meaning_issue_counts.items())
                )+".\n"
                if meaning_issue_counts else ""
            )
            +f"Broader Tag scan processed this pass: {broader_report.processed_tags:,}"
            +(f"; not scanned by this bounded report: {broader_report.bounded_tags:,}" if broader_report.bounded_tags else "")
            +".\n"
            +(
                "Broader Tag findings not queued by reason: "+", ".join(
                    f"{key.replace('_',' ')} = {value:,}"
                    for key,value in sorted(broader_issue_counts.items())
                )+".\n"
                if broader_issue_counts else ""
            )
            +f"Modifier Role inventory: {len(modifier_inventory.candidates):,} exact stable-Tag matches; "
            +f"{modifier_inventory.category_backed_count:,} category-backed without an explicit decision; "
            +f"{modifier_review_count:,} require one reviewed retain, change-family, or ordinary decision.\n"
            +f"Category Base scan processed this pass: {category_base_report.processed_categories:,}"
            +(f"; not scanned by this bounded report: {category_base_report.bounded_categories:,}" if category_base_report.bounded_categories else "")
            +(f"; exact candidates not retained: {category_base_report.bounded_candidates:,}" if category_base_report.bounded_candidates else "")
            +f". {len(safe_category_bases):,} leaf candidates have an unchanged effective target; "
            +f"{len(held_category_bases):,} new-target or descendant-scope candidates require an explicit scope and impact confirmation in Category Details.\n"
            +"Character collection and Character Source questions are limited to "
            "the current included named-library scope. Use their Check actions "
            "for a fresh current-index scan.\n"
            "Unchanged: this report did not reread TXT, Metadata, or image "
            "bytes, contact an online provider, start a job, or change saved data."
        )
        choices=[]
        if items:
            choices.append((
                report.addButton("Review New Tag Names…",QMessageBox.ActionRole),"names",
            ))
        if pending_questions:
            choices.append((
                report.addButton("Review Meanings…",QMessageBox.ActionRole),"meanings",
            ))
        if unrecorded_meanings:
            choices.append((
                report.addButton(
                    "Queue Attributed Meanings…",QMessageBox.ActionRole,
                ),"attributed_meanings",
            ))
        if unrecorded_broader or held_broader:
            choices.append((
                report.addButton(
                    "Review Broader Tags…",QMessageBox.ActionRole,
                ),"broader_tags",
            ))
        if modifier_review_count:
            choices.append((
                report.addButton(
                    "Review Modifier Roles…",QMessageBox.ActionRole,
                ),"modifier_roles",
            ))
        if category_base_report.candidates:
            choices.append((
                report.addButton(
                    "Review Category Bases…",QMessageBox.ActionRole,
                ),"category_bases",
            ))
        if collection_check_available:
            choices.append((
                report.addButton(
                    "Check Character Collections…",QMessageBox.ActionRole,
                ),"collections",
            ))
            choices.append((
                report.addButton(
                    "Check Character Sources…",QMessageBox.ActionRole,
                ),"sources",
            ))
        if needs_organization:
            choices.append((
                report.addButton("Organize Tags…",QMessageBox.ActionRole),"organization",
            ))
        if missing_relationships:
            choices.append((
                report.addButton("Review Relationships…",QMessageBox.ActionRole),
                "relationships",
            ))
        report.addButton("Close",QMessageBox.RejectRole)
        report.exec()
        clicked=report.clickedButton()
        choice=next((key for button,key in choices if button is clicked),"")
        if choice=="names":
            self._open_new_vocabulary()
        elif choice=="meanings":
            self.open_review_function(
                "review.knowledge.discovery",{"origin":"tag_encyclopedia"},
            )
        elif choice=="attributed_meanings":
            try:
                result=meaning_discovery.queue(
                    meaning_report,self.state.reviews,
                    selected_source_keys=(item.source_key for item in unrecorded_meanings),
                )
            except (OSError,RuntimeError,ValueError) as error:
                QMessageBox.critical(
                    self,"Attributed meanings were not queued",str(error),
                )
                return False
            self.open_review_function(
                "review.knowledge.discovery",{
                    "origin":"attributed_meaning_evidence",
                    "select_id":result.task_ids[0] if result.task_ids else "",
                },
            )
        elif choice=="broader_tags":
            dialog=TagBroaderDiscoveryDialog(
                broader_report,unrecorded_broader,held_broader,self,
            )
            if dialog.exec()!=QDialog.Accepted:return True
            candidate=dialog.selected_candidate()
            if candidate is None:return True
            try:
                result=broader_discovery.queue(
                    broader_report,self.state.reviews,
                    selected_source_keys=(candidate.source_key,),
                    allow_held=not candidate.independently_safe,
                )
            except (OSError,RuntimeError,ValueError) as error:
                QMessageBox.critical(
                    self,"Broader Tag proposals were not queued",str(error),
                )
                return False
            self.open_review_function(
                "review.knowledge.discovery",{
                    "origin":"broader_tag_discovery",
                    "select_id":result.task_ids[0] if result.task_ids else "",
                },
            )
        elif choice=="modifier_roles":
            self.knowledge_page.show_relationship_workspace(
                initial_kind="modifier_role",
            )
        elif choice=="category_bases":
            dialog=CategoryBaseDiscoveryDialog(category_base_report,self)
            if dialog.exec()!=QDialog.Accepted:return True
            candidate=dialog.selected_candidate()
            if candidate is not None:
                self.open_tag_encyclopedia_review(
                    "category_base_candidate",candidate.handoff_scope(),
                )
        elif choice=="collections":
            self.open_review_function(
                "review.character_collections.discover",
                {"origin":"knowledge_discovery"},
            )
        elif choice=="sources":
            self.open_review_function(
                "review.character_sources.discover",
                {"origin":"knowledge_discovery"},
            )
        elif choice=="organization":
            self.open_tag_encyclopedia_review(
                "needs_organization",{"origin":"knowledge_discovery"},
            )
        elif choice=="relationships":
            self.open_tag_encyclopedia_review(
                "missing_relationships",{"origin":"knowledge_discovery"},
            )
        return True

    def _open_new_vocabulary(self):
        items=list(self._pending_vocabulary_items)
        if not items:
            self.dashboard.set_vocabulary_notice(0)
            self.statusBar().showMessage("No new SDXL Tag names are waiting for review",8000)
            return
        intake=self._vocabulary_intake()
        categories=getattr(self.state.knowledge,"_categories",None)
        knowledge_choices=[
            (
                record.id,record.display,
                self.state.knowledge.effective_aliases(record),
            )
            for record in sorted(self.state.knowledge.tags.values(),key=lambda value:value.display.casefold())
        ]
        dialog=NewVocabularyDialog(
            items,categories.choices() if categories is not None else (),self,
            knowledge_choices=knowledge_choices,
            category_creator=self._create_vocabulary_category,
        )
        if not _dialog_accepted(dialog):
            return
        decisions=dialog.decisions(); additions=decisions[dialog.ADD]; aliases=decisions[dialog.ALIAS]; ignores=decisions[dialog.IGNORE]
        total=len(additions)+len(aliases)+len(ignores)
        if not total:
            QMessageBox.warning(
                self,"No vocabulary decisions were saved",
                "The dialog closed without a Remember Tag, Use Existing Tag, or Always Ignore Tag outcome. Reopen it and choose at least one durable decision.",
            )
            return
        if aliases:
            try:
                index=getattr(getattr(self.state,"dataset",None),"index",None)
                records=index.all_records() if index is not None else None
                alias_plan=intake.preflight_aliases(
                    aliases,indexed_records=records,
                )
            except Exception as exc:
                QMessageBox.warning(
                    self,"Other Names need review",
                    "CollectiGen could not prepare the shared Other Name review. "
                    f"No vocabulary decision was saved.\n\n{exc}",
                )
                return
            alias_dialog=TagAliasPreflightDialog(
                alias_plan,self.state.knowledge,parent=self,
            )
            if not _dialog_accepted(alias_dialog):
                if alias_dialog.choice=="merge":
                    self.review_duplicate_tags(alias_dialog.merge_tag_ids())
                elif alias_dialog.choice=="restore":
                    self.review_removed_tags(alias_dialog.removed_tag_ids())
                return
        progress=QProgressDialog("Saving vocabulary and automation decisions…","",0,total,self)
        progress.setWindowTitle("Updating Knowledge"); progress.setCancelButton(None); progress.setWindowModality(Qt.WindowModal); progress.setMinimumDuration(0)
        progress_bar=progress.findChild(QProgressBar)
        if progress_bar is not None: set_progress_bar(progress_bar,0,total)
        def update_progress(index,addition_total):
            current=len(ignores)+len(aliases)+index
            if index==addition_total or index%50==0:
                if progress_bar is not None: set_progress_bar(progress_bar,current,total)
                else: progress.setValue(current)
                QApplication.processEvents()
        try:
            ignored=intake.ignore(ignores)
            if progress_bar is not None: set_progress_bar(progress_bar,len(ignored),total)
            aliased=intake.alias(aliases)
            if progress_bar is not None: set_progress_bar(progress_bar,len(ignored)+len(aliased),total)
            accepted=intake.acknowledge(additions,progress=update_progress)
        except Exception as exc:
            progress.close(); self.state.refresh_persistent_data()
            QMessageBox.critical(
                self,"Vocabulary decisions were not fully saved",
                "CollectiGen could not verify every durable decision. The saved files were reloaded; "
                f"unfinished names will remain reviewable.\n\n{exc}",
            )
            return
        progress.setValue(total)
        self.state.refresh_persistent_data(
            vocabulary_names=() if ignored else (*accepted,*aliased),
        )
        reloaded_automation=AutomationRuleRepository(
            DEFAULTS_DIR/"automation_rules.json",USER_DATA_DIR/"automation_rules.json",
            rule_pair_admission=RulePairAdmission(USER_DATA_DIR),
        )
        saved_ignores={normalize_tag(str(item.get("tag") or "")) for item in reloaded_automation.combined().get("ignore_rules",[])}
        missing_additions=[item.tag for item in additions if self.state.knowledge.find(item.tag) is None]
        missing_aliases=[
            item.tag for item in aliases
            if self.state.knowledge.find(item.tag) is None
            or self.state.knowledge.find(item.tag).id!=item.alias_target_id
        ]
        missing_ignores=[item.tag for item in ignores if normalize_tag(item.tag) not in saved_ignores]
        if missing_additions or missing_aliases or missing_ignores:
            missing=missing_additions+missing_aliases+missing_ignores
            QMessageBox.critical(
                self,"Vocabulary decisions were not retained",
                "CollectiGen reloaded the saved files and could not find these decisions:\n\n"
                +"\n".join(missing[:20])+"\n\nNo success was reported; reopen the dialog to retry.",
            )
            return
        if not accepted and not aliased and not ignored:
            QMessageBox.warning(
                self,"No new vocabulary decisions were needed",
                "The selected names were already present when the save completed. CollectiGen reloaded the current saved state.",
            )
            return
        alias_classification=None
        if aliased:
            alias_classification=TagManagerWindow._record_vocabulary_alias_downstream(
                self,aliases,aliased,
            )
        self._offer_new_vocabulary()
        if hasattr(self,"review_page"): self.review_page.refresh()
        if hasattr(self,"automation_page"): self.automation_page.refresh()
        self.refresh_review_count(); self._show_summary(self.state.dataset.summary)
        prompt=QMessageBox(self)
        prompt.setWindowTitle("SDXL Tag decisions saved")
        prompt.setText(
            f"Added {len(accepted):,} name(s), recorded {len(aliased):,} alias(es), and created {len(ignored):,} global ignore rule(s)."
        )
        if accepted:
            prompt.setInformativeText(
                "Added names have one durable meaning question in Review Center. Aliases now use the canonical tag for recognition, rules, search, and ordering. Global ignores are reversible in "
                "Rules & Automation and suppress matching incoming evidence during automatic refresh and future managed intake; confirmed Image Knowledge remains unchanged."
            )
            review_now=prompt.addButton("Review Added Names Now",QMessageBox.AcceptRole)
            prompt.addButton("Review Later",QMessageBox.RejectRole)
        else:
            prompt.setInformativeText(
                "Aliases now use the canonical tag for recognition, rules, search, and ordering. Global ignores are reversible in Rules & Automation and suppress matching incoming evidence during automatic refresh and future managed intake; confirmed Image Knowledge remains unchanged."
            )
            review_now=None; prompt.addButton("Done",QMessageBox.AcceptRole)
        if alias_classification is not None:
            prompt.setInformativeText(
                prompt.informativeText()+"\n\n"
                +alias_classification.completion_summary()
            )
        prompt.exec()
        if review_now is not None and prompt.clickedButton() is review_now:
            self.open_review_function(
                "review.knowledge.discovery",{"origin":"new_vocabulary"},
            )

    def _record_vocabulary_alias_downstream(self,items,saved_names):
        saved_aliases={normalize_tag(value) for value in saved_names}
        saved_items=tuple(
            item for item in items
            if normalize_tag(item.tag) in saved_aliases
        )
        if not saved_items:return None
        classification=classify_system_knowledge_change(
            "tag_aliases",label="Saved startup Use Existing Tag aliases",
            affected_tag_ids=tuple(
                item.alias_target_id for item in saved_items
                if str(item.alias_target_id or "")
            ),
            source_names=tuple(item.tag for item in saved_items),
            matching_images=True,
        )
        try:self.downstream_updates.record_classification(classification)
        except OSError:pass
        else:self._refresh_downstream_update_notice()
        return classification

    def _create_vocabulary_category(self,initial_name,parent):
        """Reuse Category Workspace without closing or saving the intake review."""
        self._ensure_page(4)
        return self.knowledge_page.category_workspace.create_category_dialog(
            parent=parent,initial_name=initial_name,
        )

    def _scan_failed(self,error):
        QApplication.instance().setProperty(
            "collectigenNotificationPhase","library_completion"
        )
        self._library_scan_active=False
        self._pending_completed_dataset_image=None
        has_view=bool(self.state.dataset.roots and self.state.dataset.images)
        self._last_library_outcome="cached_failed" if has_view else "failed"
        self.state.finish_long_operation("library"); self.statusBar().showMessage("Library refresh failed")
        if self._library_refresh_kind=="external_evidence":
            self._finish_external_evidence_checkpoint(
                "failed",message=str(error),
            )
        if hasattr(self,"setup_page"):self.setup_page._refresh_library_profiles()
        if has_view:
            self.statusBar().showMessage("Cached library opened; CollectiGen could not check it for changes",15000)
            self.show_reportable_error(
                "Library opened from saved information",
                "The remembered library is available from CollectiGen's local cache, but its files could not be checked for changes.\n\n"+error,
                severity="medium",
            )
        else:
            self.show_reportable_error("Library refresh failed",error)
        self._update_recommended()
        self._start_ingest_refresh_once()
        QApplication.instance().setProperty(
            "collectigenNotificationPhase","ordinary"
        )

    def _finish_external_evidence_checkpoint(
        self,status: str,*,result=None,message: str="",
    ) -> None:
        service=getattr(
            self,"_library_refresh_evidence_checkpoint_service",None,
        )
        session=getattr(
            self,"_library_refresh_evidence_checkpoint_session",None,
        )
        self._library_refresh_evidence_checkpoint_service=None
        self._library_refresh_evidence_checkpoint_session=None
        if service is None or session is None:
            return
        service.finish(
            session,status,result=dict(result or {}),message=message,
        )

    def _show_summary(self,summary,*,checking=False):
        images=max(summary.images,1); learned=len(self.state.knowledge.tags)
        understood=sum(1 for tag in self.state.dataset.tag_counts if self.state.knowledge.find(tag))
        library_count=(
            len(tuple(self.state.dataset.scoped_profile_managers()))
            if hasattr(self.state.dataset,"scoped_profile_managers") else
            int(bool(getattr(self.state.dataset,"root",None)))
        )
        limited=sum(int(getattr(summary,field,0) or 0) for field in (
            "online_only","conflicts","read_only","unavailable",
        ))
        self.dashboard.set_overview(
            images=(
                f"{summary.images:,} across {library_count:,} "
                f"librar{'y' if library_count==1 else 'ies'}"
                +("\nChecking for changes" if checking else "")
                +(f"\n{limited:,} need local access or conflict review" if limited else "")
            ),
            captions=f"{summary.captions:,} images\n{summary.captions/images*100:.0f}% have caption files",
            records=f"{summary.sidecars:,} images\n{summary.sidecars/images*100:.0f}% have organized data",
            tags=f"{understood:,} dataset names understood\n{learned:,} learned definitions",
        )
        self._update_recommended()

    def refresh_review_count(self):
        self.pending=self.state.reviews.counts().get("pending",0)

    def review_data_changed(self):
        self.refresh_review_count(); self._update_recommended()

    def knowledge_data_changed(
        self,*,invalidate_ratings: bool=True,reload_knowledge: bool=True,
        preserve_workflow: bool=False,
    ):
        invalidate=getattr(self,"_invalidate_dataset_page_preparation",None)
        if callable(invalidate):
            invalidate()
        if reload_knowledge and not preserve_workflow:
            self.state.knowledge.load()
        self.content_safety.categories.load()
        if invalidate_ratings:
            self.content_safety.clear_cache()
            self._refresh_protected_previews(
                preserve_temporary_reveal=False
            )
        for page_name in ("dataset_page","ingest_page"):
            page=getattr(self,page_name,None)
            if page is not None and hasattr(page,"categories"):
                page.categories.load()
            if page is not None and hasattr(page,"refresh_system_knowledge"):
                if preserve_workflow:page.refresh_system_knowledge(preserve_workflow=True)
                else:page.refresh_system_knowledge()
        if invalidate_ratings:
            for card in self.findChildren(ImageCard): self.content_safety(card.path,card)
        if hasattr(self,"review_page"): self.review_page.refresh()
        if hasattr(self,"automation_page"): self.automation_page.refresh()
        self._update_recommended()

    def _derived_refresh_changed(self,payload) -> None:
        """Publish AUT-04 cache refreshes on the Qt thread."""
        details=dict(payload or {})
        state=str(details.get("state") or "")
        if state in {"complete","cancelled","failed"}:
            if hasattr(self,"review_page"):
                self.review_page.derived_review_changed(details)
            if state=="complete" and hasattr(self,"refresh_review_count"):
                self.refresh_review_count()
        if state=="complete":
            paths=tuple(details.get("images") or ())
            scope=str(details.get("scope") or "images")
            cause=str(details.get("cause") or "Image Knowledge changed")
            failed=int(details.get("failed") or 0)
            if hasattr(self,"ingest_page"):
                self.ingest_page.derived_results_changed(paths,scope=scope,cause=cause)
            if hasattr(self,"dataset_page"):
                self.dataset_page.derived_results_changed(
                    paths,scope=scope,cause=cause,
                    **({"workflow_proofs":details["workflow_proofs"]}
                       if "workflow_proofs" in details else {}),
                )
                if failed:
                    self.dataset_page.header.set_status(
                        f"Current image results refreshed with {failed:,} "
                        f"problem{'s' if failed!=1 else ''}. Use Recheck after "
                        f"restoring access; this refresh followed "
                        f"{cause.rstrip('.').lower()}."
                    )
            summary=getattr(self.state.dataset,"summary",None)
            if summary is not None and getattr(self.state.dataset,"roots",()):
                self._show_summary(summary,checking=False)
            changed=int(details.get("refreshed") or 0)
            reused=int(details.get("reused") or 0)
            held=int(details.get("skipped") or 0)
            message=(
                f"Current image results refreshed • {changed:,} updated"
                f" • {reused:,} already current"
                +(f" • {held:,} unavailable or held" if held else "")
                +(f" • {failed:,} need Recheck" if failed else "")
                +(
                    f" • {int(details.get('review_created') or 0):,} new "
                    "Review question(s)"
                    if int(details.get("review_created") or 0) else ""
                )
                +f" • because {cause.rstrip('.').lower()}"
            )
            self.statusBar().showMessage(message,15000)
        elif state=="failed":
            message=str(details.get("message") or "Background refresh failed")
            if hasattr(self,"dataset_page"):
                self.dataset_page.header.set_status(
                    "Current image results could not refresh automatically. "
                    "Use Recheck after restoring access."
                )
            self.statusBar().showMessage(
                f"Current image results need a recheck • {message}",15000,
            )
        elif state=="deferred":
            message=str(
                details.get("message")
                or "Automatic refresh yielded to visible work and will resume."
            )
            if hasattr(self,"dataset_page"):
                self.dataset_page.header.set_status(message)
            self.statusBar().showMessage(message,10000)

    def open_dataset_tag(self,tag: str):
        self.navigation.setCurrentRow(2)
        return TagManagerWindow._queue_dataset_page_action(
            self,
            lambda:self.dataset_page.show_tag(tag)
        )

    def open_automation_function(self,function_id: str,scope=None):
        if str(function_id)=="review.conflicts.guided_checks":
            self.open_review_function(function_id,scope)
            return
        if str(function_id)=="dataset.source_correction.preview":
            self.open_dataset_function(function_id,scope)
            return
        if str(function_id) in {
            "image_facts.update_matching",
            "dataset.image_facts.update_matching",
        }:
            self.open_dataset_function(function_id,scope)
            return
        if str(function_id) in {
            "review_changes","training_captions.preview",
            "dataset.training_captions.preview",
        }:
            self.open_dataset_function(
                "dataset.training_captions.preview",scope,
            )
            return
        if str(function_id) in {
            "system_knowledge.maintenance",
            "system_knowledge.safe_fixes",
        }:
            function_id="knowledge.system_knowledge.safe_fixes"
        if str(function_id)=="knowledge.system_knowledge.safe_fixes":
            self.open_knowledge_function(function_id,scope)
            return
        self.navigation.setCurrentRow(5); self._ensure_page(5)
        self.automation_page.open_function(function_id,scope)

    def open_review_function(self,function_id: str,scope=None):
        self.navigation.setCurrentRow(3)
        self._ensure_page(3)
        if hasattr(self,"review_page"):
            return self.review_page.open_function(function_id,scope)
        self._pending_review_page_actions.append(
            lambda:self.review_page.open_function(function_id,scope)
        )
        return True

    def open_dataset_function(self,function_id: str,scope=None):
        scope_snapshot=deepcopy(scope)
        if str(function_id) not in {
            "dataset.image_facts.update_matching","image_facts.update_matching",
        }:
            self.navigation.setCurrentRow(2)
        return TagManagerWindow._queue_dataset_page_action(
            self,
            lambda:self.dataset_page.open_function(
                function_id,scope_snapshot,
            )
        )

    def open_recycle_records(self,record_ids):
        exact=tuple(record_ids)
        self.navigation.setCurrentRow(2)
        return TagManagerWindow._queue_dataset_page_action(self,
            lambda:self.dataset_page.open_recycle_bin(record_ids=exact))

    def open_ingest_source_correction(self,scope):
        """Bridge one live Ingest editor scope to the Dataset-owned modal."""
        ingest=getattr(self,"ingest_page",None)
        if ingest is None:
            return False
        exact_scope=deepcopy(scope)
        self.navigation.setCurrentRow(2)
        def open_correction():
            image=ingest.consume_source_correction_handoff(exact_scope)
            if image is None:
                QMessageBox.information(
                    ingest,"Ingest source correction scope expired",
                    "This one-shot image-editor handoff is no longer current. "
                    "Nothing was selected or changed; choose the correction again "
                    "from the current Ingest image.",
                )
                return False
            exact_scope["image"]=str(image)
            opened=self.dataset_page.open_ingest_source_correction(
                exact_scope,ingest.source_correction_image_is_current,
            )
            contextual_projection=getattr(
                self.dataset_page,"has_contextual_projection_handoff",lambda:False,
            )()
            # Choosing Configure Protection deliberately navigates to Automation.
            # A projection handoff remains in Dataset until its explicit Back action.
            # Other outcomes restore the exact Ingest editor with fresh evidence.
            if self.navigation.currentRow()!=5 and not contextual_projection:
                TagManagerWindow._return_ingest_source_correction(
                    self,exact_scope,
                )
            return opened
        return TagManagerWindow._queue_dataset_page_action(
            self,open_correction,
        )

    def _return_ingest_source_correction(self,scope):
        ingest=getattr(self,"ingest_page",None)
        if ingest is None:
            return False
        self.navigation.setCurrentRow(1)
        return ingest.resume_source_correction_return(dict(scope))

    def open_knowledge_function(self,function_id: str,scope=None):
        self.navigation.setCurrentRow(4)
        self._ensure_page(4)
        self.knowledge_page.open_function(function_id,scope)

    def open_automation_ai_setup(self,subsection: str):
        """Open the missing analysis-model prerequisite, retaining Rules' draft."""
        self.navigation.setCurrentRow(6)
        if self.stack.currentIndex()!=6 or not hasattr(self,"setup_page"):
            return
        self.setup_page.show_section(
            "ai","descriptions" if subsection=="descriptions" else "sdxl",
        )
        self.setup_page.set_automation_return_available(True)
        self.setup_page.automation_return_panel.action_button.setFocus(
            Qt.OtherFocusReason
        )

    def offer_preparation_setup_return(self,page):
        if page not in (getattr(self,"ingest_page",None),getattr(self,"dataset_page",None)):
            return
        self._preparation_return_page=page
        self.setup_page.set_preparation_return_available(True)
        self.setup_page.preparation_return_panel.action_button.setFocus(Qt.OtherFocusReason)

    def open_preparation_ai_tag_runtime_setup(self,page):
        """Open the focused native runtime check and retain an explicit return."""
        if page not in (getattr(self,"ingest_page",None),getattr(self,"dataset_page",None)):
            return
        self.navigation.setCurrentRow(6)
        if self.stack.currentIndex()!=6 or not hasattr(self,"setup_page"):
            return
        self.setup_page.open_dependencies("wd")
        self.setup_page.dependency_panel.note_wd_runtime_failure()
        self.offer_preparation_setup_return(page)

    def _return_preparation_setup(self):
        page=getattr(self,"_preparation_return_page",None)
        target=(1 if page is getattr(self,"ingest_page",None) else
                2 if page is getattr(self,"dataset_page",None) else None)
        if target is None:
            return

        def return_to_review():
            self.navigation.setCurrentRow(target)
            if self.stack.currentIndex()==target:
                page.preparation_controls.reopen_setup_return()

        page.preparation_controls.check_setup_return_sources(
            return_to_review,self.setup_page.set_preparation_return_state,
        )

    def open_knowledge_pack_import(self):
        """Open the existing reviewed pack import from a recovery handoff."""
        self.navigation.setCurrentRow(5); self._ensure_page(5)
        self.automation_page.open_function("knowledge_packs")
        QTimer.singleShot(0,self.automation_page.import_knowledge_pack)

    def _refresh_downstream_update_notice(self):
        self.dashboard.set_downstream_notice(
            self.downstream_updates.load(),
            self.downstream_updates.load_projection(),
        )
        dialog=getattr(self,"_downstream_review_dialog",None)
        if dialog is not None:
            dialog.refresh(
                self.downstream_updates.load(),
                self.downstream_updates.load_projection(),
            )

    def _prepare_tag_recovery_notice(self):
        """Recover availability from older journals off-thread, without image reads."""
        from core import ImageIdentityCorrectionService
        snapshot=self.downstream_updates.load()
        journal=OperationJournal(USER_DATA_DIR/"operation_journal.jsonl")
        checkpoint=USER_DATA_DIR/"image_identity_correction_checkpoint.json"
        task=CoordinatedCallTask(
            self.state.jobs,"Checking saved Tag update recovery",
            lambda _job:bool(
                checkpoint.exists()
                or ImageIdentityCorrectionService.latest_undoable_in(journal) is not None
            ),resource="io",key="dashboard:tag-update-recovery",
        )
        self._tag_recovery_notice_task=task
        def complete(available):
            self._tag_recovery_notice_task=None
            # A correction completed while history was read: retain its newer state.
            if self.downstream_updates.load()!=snapshot:return
            try:self.downstream_updates.record_identity_recovery(available)
            except OSError:return
            self._refresh_downstream_update_notice()
        def failed(_message=""):
            self._tag_recovery_notice_task=None
        task.signals.complete.connect(complete)
        task.signals.failed.connect(failed)
        task.signals.cancelled.connect(failed)
        task.start()

    def review_tag_update_recovery(self):
        identity=self.downstream_updates.load()
        if not identity.recovery_available:
            self._refresh_downstream_update_notice()
            return
        scope=identity.handoff_scope()
        scope["start_audit"]=False
        self.open_dataset_function("dataset.image_facts.update_matching",scope)

    def review_downstream_updates(self):
        identity=self.downstream_updates.load()
        projection=self.downstream_updates.load_projection()
        if not (identity.active or projection.pending_targets):
            TagManagerWindow.review_tag_update_recovery(self)
            return
        if identity.active or projection.pending_targets:
            from .downstream_review_dialog import DownstreamReviewDialog
            dialog=getattr(self,"_downstream_review_dialog",None)
            if dialog is None:
                dialog=DownstreamReviewDialog(identity,projection,self)
                self._downstream_review_dialog=dialog
                dialog.destroyed.connect(
                    lambda:setattr(self,"_downstream_review_dialog",None)
                )
                def open_step(target,scope):
                    dialog.hide()
                    self.open_dataset_function(
                        "dataset.image_facts.update_matching"
                        if target in {"identity","identity_recovery"} else
                        "dataset.image_data.sync_order_projections",scope,
                    )
                    # The owning job may continue asynchronously. Keep the
                    # checklist available without concealing its preview.
                    dialog.refresh(self.downstream_updates.load(),self.downstream_updates.load_projection())
                    dialog.show()
                dialog.requested.connect(open_step)
                def dismiss_step(target,revision):
                    # Retire only the exact optional reminder the user saw.
                    # The shared cache transition never writes image data or recovery.
                    current=self.downstream_updates.load_projection()
                    if not revision or current.updated_at!=revision:
                        dialog.refresh(self.downstream_updates.load(),current)
                        dialog.summary.setText("Saved updates changed. Review the refreshed reminders before dismissing.")
                        return
                    try:
                        self.downstream_updates.complete_projection_target(
                            target,expected_updated_at=revision,
                        )
                    except OSError:
                        dialog.summary.setText("Could not save dismissal. The reminder is still pending; try again.")
                        return
                    self._refresh_downstream_update_notice()
                dialog.dismissal_requested.connect(dismiss_step)
            else:
                dialog.refresh(identity,projection)
            dialog.show(); dialog.raise_(); dialog.activateWindow()
            return

    def open_tag_encyclopedia_review(self,review_view_id: str,scope=None):
        self.navigation.setCurrentRow(4); self._ensure_page(4)
        self.knowledge_page.open_review_view(review_view_id,scope)

    def open_dataset_image(self,path: str):
        self.navigation.setCurrentRow(2)
        return TagManagerWindow._queue_dataset_page_action(
            self,
            lambda:self.dataset_page.inspect_image(path)
        )

    def open_completed_dataset_image(self,path: str):
        """Open only a completed image in the current included Dataset view."""
        image=Path(path)
        if getattr(self,"_library_scan_active",False):
            self._pending_completed_dataset_image=image
            self.statusBar().showMessage(
                "Opening the moved image in Dataset when the library check finishes…"
            )
            return True
        dataset=self.state.dataset
        identity=dataset.identity_for_path(image)
        if (not image.is_file() or image not in dataset.images or identity is None
            or identity.library_id not in dataset.scope_library_ids):
            QMessageBox.information(self,"Moved image not yet in Dataset",
                "This exact moved image is not in the current included Dataset view. Check the library scope or refresh Dataset, then choose Open in Dataset again. The move has not been repeated.")
            return False
        return self.open_dataset_image(str(image))

    def _finish_pending_completed_dataset_handoff(self):
        image=self._pending_completed_dataset_image
        if image is None or self._library_scan_active:
            return
        self._pending_completed_dataset_image=None
        self.open_completed_dataset_image(str(image))

    def edit_image_data(self,path: str):
        # Contextual galleries edit in place. Dataset owns the shared editor
        # coordinator, but loading that coordinator is not a navigation action.
        return TagManagerWindow._queue_dataset_page_action(
            self,
            lambda:self.dataset_page.edit_gallery_image(path)
        )

    def edit_system_tag(self,stable_id: str,source_editor=None):
        """Open shared Tag Details while preserving the current image editor."""
        self._ready_knowledge_editor_action=None
        self._ensure_page(4)
        if not hasattr(self,"knowledge_page"):
            self._wait_for_knowledge_editor("edit",stable_id,source_editor)
            return
        if not self.knowledge_page.allow_external_knowledge_action(
            "opening Tag Details"
        ):
            return
        self.knowledge_page._open_tag_details(stable_id,source_editor)

    def create_system_tag(self,name: str,source_editor=None):
        """Create shared System Knowledge while preserving the image edit boundary."""
        self._ready_knowledge_editor_action=None
        self._ensure_page(4)
        if not hasattr(self,"knowledge_page"):
            self._wait_for_knowledge_editor("create",name,source_editor)
            return
        if not self.knowledge_page.allow_external_knowledge_action(
            "creating an SDXL Tag"
        ):
            return
        self.knowledge_page.create_tag_from_image(name,source_editor)

    def create_rule_tag(self,name: str,rule_editor):
        """Open canonical Tag creation without closing or saving the Rule draft."""
        if rule_editor is None or not isValid(rule_editor):
            return False
        self._ready_knowledge_editor_action=None
        self._ensure_page(4)
        if not hasattr(self,"knowledge_page"):
            self._wait_for_knowledge_editor("rule-create",name,rule_editor)
            return True
        if not self.knowledge_page.allow_external_knowledge_action(
            "creating the Tag required by this rule"
        ):
            rule_editor.rule_tag_creation_cancelled(
                "System Knowledge is busy. The rule draft is unchanged; try again when it is ready."
            )
            return False
        return bool(self.knowledge_page.create_tag_for_rule(name,rule_editor))

    def _wait_for_knowledge_editor(self,action,value,source_editor):
        self._finish_knowledge_editor_wait()
        source=weakref.ref(source_editor) if source_editor is not None else None
        self._pending_knowledge_editor_action=(action,value,source)
        progress=QProgressDialog("Preparing the Tag editor…","Cancel",0,0,source_editor or self)
        progress.setWindowTitle("Opening Tag editor")
        progress.setWindowModality(Qt.NonModal)
        progress.setMinimumDuration(0)
        self._pending_knowledge_editor_progress=progress
        progress.canceled.connect(lambda:self._finish_knowledge_editor_wait(cancelled=True))
        progress.show()

    def _finish_knowledge_editor_wait(self,*,cancelled=False):
        pending=getattr(self,"_pending_knowledge_editor_action",None)
        self._pending_knowledge_editor_action=None
        self._ready_knowledge_editor_action=None
        progress=getattr(self,"_pending_knowledge_editor_progress",None)
        self._pending_knowledge_editor_progress=None
        if progress is not None:
            progress.blockSignals(True); progress.close(); progress.deleteLater()
        if cancelled and pending is not None and pending[0]=="rule-create":
            source=pending[2]
            editor=source() if source is not None else None
            if editor is not None and isValid(editor):
                editor.rule_tag_creation_cancelled(
                    "The Tag editor did not open. The rule draft is unchanged; try again."
                )

    def _resume_knowledge_editor_action(self,pending):
        if pending is not getattr(self,"_ready_knowledge_editor_action",None):return
        self._ready_knowledge_editor_action=None
        action,value,source=pending
        editor=source() if source is not None else None
        if source is not None:
            try:
                if editor is None or not editor.isVisible():return
            except RuntimeError:
                return
        if action=="create":self.create_system_tag(value,editor)
        elif action=="rule-create":self.create_rule_tag(value,editor)
        else:self.edit_system_tag(value,editor)

    def review_duplicate_tags(self,tag_ids):
        """Open the existing recoverable duplicate-Tag workflow on request."""
        values=tuple(dict.fromkeys(str(value) for value in tag_ids if str(value)))
        if len(values)<2:return False
        self.navigation.setCurrentRow(4); self._ensure_page(4)
        if not self.knowledge_page.allow_external_knowledge_action(
            "merging duplicate SDXL Tags"
        ):
            return False
        QTimer.singleShot(
            0,lambda:self.knowledge_page.category_workspace.merge_tag_ids(values),
        )
        return True

    def review_removed_tags(self,tag_ids):
        """Open removed-vocabulary recovery for the exact blocked identities."""
        values=tuple(dict.fromkeys(str(value) for value in tag_ids if str(value)))
        if not values:return False
        self.navigation.setCurrentRow(4); self._ensure_page(4)
        if not self.knowledge_page.allow_external_knowledge_action(
            "restoring removed vocabulary"
        ):
            return False
        QTimer.singleShot(
            0,lambda:self.knowledge_page.show_removed_vocabulary(initial_ids=values),
        )
        return True

    def _update_recommended(self):
        missing_locations=[]
        for key,label,consequence in (
            (
                "ingest_directory","Incoming Images",
                "Ingest cannot load incoming images.",
            ),
            (
                "problems_directory","Problems",
                "problem-routing actions are unavailable.",
            ),
            (
                "recycle_directory","Recycle Bin",
                "recycle actions are unavailable.",
            ),
        ):
            if not str(getattr(self.settings,key,"") or "").strip():
                missing_locations.append(f"{label}: {consequence}")
        set_setup_attention=getattr(self.dashboard,"set_setup_attention",None)
        if callable(set_setup_attention):
            set_setup_attention(missing_locations)
        if hasattr(self.settings,"library_registry"):
            registry=self.settings.library_registry()
            configured=any(
                not registry.validate(profile,replacing_id=profile.stable_id)
                for profile in registry.profiles
            )
        else:
            configured=bool(
                getattr(self.settings,"working_directory","")
                or getattr(self.settings,"dataset_directory","")
            )
        roots=getattr(self.state.dataset,"roots",None)
        has_view=bool(
            roots if roots is not None else getattr(self.state.dataset,"root",None)
        )
        has_images=bool(self.state.dataset.images)
        def present(state_key,state_text,recommendation,button,enabled,action,description="",*,announce=True):
            self.dashboard.set_library_status(state_key,state_text)
            self.dashboard.set_recommendation(
                recommendation,button,enabled,description,announce=announce,
            )
            self._recommended_action=action

        if self._library_scan_active:
            if has_view and self._scan_cached_ready:
                present(
                    "saved_checking","Saved library views ready • checking files for changes",
                    "The saved included-library view is ready to browse while CollectiGen checks each collection for changes.",
                    "Browse Saved Dataset",True,"dataset",
                    "Browse saved library information while included collections are checked. Recent external changes may not appear until validation finishes.",
                    announce=False,
                )
            else:
                phase=getattr(self,"_scan_phase_label","") or "Opening the libraries"
                self.dashboard.clear_overview()
                present(
                    "checking","Checking libraries • no current or saved view is ready yet",
                    f"{phase}. Safe browsing will be available as soon as the library inventory is ready.",
                    "Opening Libraries…",False,"none",
                    "Wait for a current or saved included-library view before browsing.",announce=False,
                )
            return
        if self._last_library_outcome=="cancelled":
            present(
                "partial","Partially ready • library checks paused safely",
                "Library initialization paused safely. Completed image records were kept; resume to finish the remaining included libraries.",
                "Resume Library Checks",True,"refresh",
                "Resume the interrupted library checks; completed current records will be reused.",
            )
            return
        if self._last_library_outcome=="cached_failed" and has_view:
            present(
                "saved_unverified","Saved views available • some current files could not be verified",
                "Saved library information is available, but CollectiGen could not check every included collection for changes. Source badges identify unavailable saved views.",
                "Browse Saved Dataset",True,"dataset",
                "Browse the last saved included-library view. Unavailable collections may not include recent external changes.",
            )
            return
        if self._last_library_outcome=="failed":
            self.dashboard.clear_overview()
            present(
                "error","Unavailable • no current or saved included-library view could be opened",
                "The included libraries could not be opened. Restore access to their folders, then retry the checks.",
                "Retry Library Checks",True,"refresh",
                "Retry the included-library checks after restoring folder access.",
            )
            return
        if not has_view:
            self.dashboard.clear_overview()
            if configured:
                present(
                    "configured","Named libraries configured • not opened in this session",
                    "Open the included image libraries before browsing or reviewing their images.",
                    "Open Included Libraries",True,"refresh",
                    "Open the included named libraries and check them for changes.",
                )
            else:
                present(
                    "none","No current or saved image-library view",
                    "Add and include one or more image libraries before browsing or reviewing images.",
                    "Choose Libraries…",True,"choose_library",
                    "Choose the peer image libraries to consider throughout the app.",
                )
            return
        authority_failed=sum(
            int(getattr(result,"failed",0))
            for result in (
                getattr(self,"_authority_initialization_results",None)
                or tuple(filter(None,(
                    getattr(self,"_authority_initialization_result",None),
                )))
            )
        )
        if authority_failed:
            present(
                "partial_error",f"Current view ready with problems • {authority_failed:,} image record(s) left unchanged",
                f"The included libraries are ready, but {authority_failed:,} image(s) could not create or update Image Knowledge and were left unchanged.",
                "Review Dataset",True,"dataset",
                "Open Dataset to inspect the included libraries and their unchanged problem records.",
            )
            return
        if not has_images:
            present(
                "current_empty","Current included-library view • no images found",
                "The included libraries are current but contain no images. Bring in images to begin.",
                "Open Ingest",True,"ingest",
                "Open Ingest to review incoming images before moving them into a named library.",
            )
            return
        dataset_summary=getattr(self.state.dataset,"summary",None)
        limited=sum(
            int(getattr(dataset_summary,field,0) or 0)
            for field in ("online_only","conflicts","read_only","unavailable")
        )
        if limited:
            present(
                "current_limited",
                f"Current included-library view • {limited:,} image(s) need local access, permission, or sync-conflict review",
                "Available images remain usable. Held images stay visible from current or saved catalog information, but CollectiGen will not download, edit, analyze, or move them until their storage issue is resolved and the library is refreshed.",
                "Browse Dataset",True,"dataset",
                "Open Dataset to use available images and see the exact recovery guidance on held images.",
            )
            return
        if self.pending:
            pending_items=(
                f"{self.pending:,} unreviewed "
                +("item" if self.pending==1 else "items")
            )
            present(
                "current","Current included-library view",
                f"{pending_items} from the current libraries and saved knowledge. "
                "These can include unconfirmed Tags, Descriptions, character assignments, "
                "ambiguous Metadata, and other suggestions; several items may belong to one image.",
                "Review Suggestions",True,"review",
                f"Open Review Center with {pending_items}. "
                "The total counts suggestions and evidence, not images or errors.",
            )
        else:
            present(
                "current","Current included-library view",
                "The included libraries are ready. Find incomplete or conflicting image information next.",
                "Improve Incomplete Images",True,"dataset",
                "Open Dataset focused review for incomplete or conflicting image information.",
            )

    def run_recommended(self):
        action=self._recommended_action
        if action=="refresh": self.refresh_library()
        elif action=="choose_library": self.choose_library()
        elif action=="ingest": self.navigation.setCurrentRow(1)
        elif action=="review": self.navigation.setCurrentRow(3)
        elif action=="dataset": self.navigation.setCurrentRow(2)

    def change_theme(self,theme: str):
        # Setup publishes display changes only after its verified save succeeds.
        self.settings.ui_theme=apply_theme(QApplication.instance(),theme,self.settings.color_blind_mode)
        refresh_all_tag_appearance_chips()

    def change_color_blind_mode(self,enabled: bool):
        self.settings.color_blind_mode=bool(enabled); apply_theme(QApplication.instance(),self.settings.ui_theme,self.settings.color_blind_mode)
        refresh_all_tag_appearance_chips()
        if hasattr(self,"knowledge_page"): self.knowledge_page.refresh_tags()
        if hasattr(self,"review_page"): self.review_page.refresh()

    def change_tooltips(self,enabled: bool):
        from .tooltips import apply_tooltip_policy
        self.settings.show_tooltips=apply_tooltip_policy(QApplication.instance(),enabled)

    def change_content_level(self,rating: str):
        # Display preferences do not change classification inputs. Repaint from
        # the retained cache instead of rereading every image after each toggle.
        self.settings.gallery_content_level=rating
        for card in self.findChildren(ImageCard): self.content_safety(card.path,card)
        for preview in self.findChildren(ProtectedImageView):
            preview.refresh_protection()
        if not self.settings.remember_content_reveals: self.content_safety.clear_session_reveals()
        if hasattr(self,"ingest_page"): self.ingest_page.render_page()
        if hasattr(self,"dataset_page"): self.dataset_page.render_page()
        if hasattr(self,"review_page"): self.review_page.refresh()
        if hasattr(self,"knowledge_page"): self.knowledge_page.refresh_tags()
        if hasattr(self,"automation_page"): self.automation_page.refresh()
        blur_protection=(
            rating!="explicit" or bool(self.settings.gallery_hidden_ratings)
            or bool(getattr(self.settings,"gallery_blur_unknown",True))
        )
        if blur_protection:
            paths=list(self.state.dataset.images)
            if hasattr(self,"ingest_page"): paths.extend(self.ingest_page.images)
            problems=getattr(getattr(self,"dataset_page",None),"_problems_dialog",None)
            if problems is not None: paths.extend(problems.images)
            self.request_content_safety(paths)

    def queue_content_sensitivity_refresh(self,scope) -> None:
        """Coalesce System Knowledge rating dependencies before querying images."""
        scope=scope if isinstance(scope,dict) else {}
        self._pending_content_sensitivity_tag_ids.update(
            str(value) for value in scope.get("tag_ids",()) if str(value)
        )
        self._pending_content_sensitivity_category_ids.update(
            str(value) for value in scope.get("category_ids",()) if str(value)
        )
        for values in scope.get("identity_groups",()):
            group=tuple(
                str(value) for value in
                ((values,) if isinstance(values,str) else values)
                if str(value).strip()
            )
            if group:self._pending_content_sensitivity_identity_groups.add(group)
        if (
            (
                self._pending_content_sensitivity_tag_ids
                or self._pending_content_sensitivity_category_ids
                or self._pending_content_sensitivity_identity_groups
            )
            and not self.state.jobs.shutting_down
            and self._content_sensitivity_scope_task is None
            and not self._content_sensitivity_refresh_scheduled
        ):
            self._content_sensitivity_refresh_scheduled=True
            QTimer.singleShot(0,self._resume_content_sensitivity_refresh)

    def _resume_content_sensitivity_refresh(self) -> None:
        self._content_sensitivity_refresh_scheduled=False
        if self._content_sensitivity_scope_task is not None:return
        if not (
            self._pending_content_sensitivity_tag_ids
            or self._pending_content_sensitivity_category_ids
            or self._pending_content_sensitivity_identity_groups
        ):return
        tag_ids=tuple(self._pending_content_sensitivity_tag_ids)
        category_ids=tuple(self._pending_content_sensitivity_category_ids)
        identity_groups=tuple(self._pending_content_sensitivity_identity_groups)
        self._pending_content_sensitivity_tag_ids.clear()
        self._pending_content_sensitivity_category_ids.clear()
        self._pending_content_sensitivity_identity_groups.clear()
        self.state.knowledge.load(); self.content_safety.categories.load()
        workflow_targets={}
        for page_name in ("dataset_page","ingest_page"):
            page=getattr(self,page_name,None)
            managers=getattr(page,"_workflow_index_managers",None)
            if callable(managers):
                for manager in managers():
                    workflow_targets.setdefault(manager.index,set()).update(getattr(manager,"images",()))
            workflow_index=getattr(page,"_workflow_index",None)
            if workflow_index is not None:
                workflow_targets.setdefault(workflow_index,set()).update(getattr(page,"images",()))
        groups=sensitivity_dependency_groups(
            self.state.knowledge,self.content_safety.categories,
            tag_ids=tag_ids,category_ids=category_ids,
            identity_groups=identity_groups,
        )
        index=getattr(getattr(self.state,"dataset",None),"index",None)
        if not groups or (index is None and not workflow_targets):
            self._schedule_pending_content_sensitivity_refresh()
            return
        evidence_queries=self.state.evidence_queries
        def work(job):
            job.update(0,2,"Searching indexed Image Knowledge")
            candidates=(evidence_queries.query(
                index,groups,False,should_continue=job.checkpoint,
            ) if index is not None else ())
            job.update(1,2,"Checking confirmed Image Knowledge")
            filter_confirmed=getattr(index,"confirmed_evidence_paths",None)
            result=(
                filter_confirmed(
                    candidates,groups,should_continue=job.checkpoint,
                )
                if callable(filter_confirmed) else candidates
            )
            affected=set(map(Path,result))
            for target,images in workflow_targets.items():
                paths=target.sensitivity_workflow_images(groups,images=images,should_continue=job.checkpoint)
                paths.update(affected.intersection(images))
                target.invalidate_workflow_images(paths)
                affected.update(paths)
            job.update(2,2,"Affected content ratings and readiness identified")
            return tuple(sorted(affected,key=str))
        task=CoordinatedCallTask(
            self.state.jobs,"Find images affected by content-rating changes",work,
            resource="cpu",
            key=f"content-sensitivity:{evidence_queries.request_key(index,groups,False)}",
        )
        self._content_sensitivity_scope_task=task
        task.sensitivity_groups=groups
        task.signals.complete.connect(
            lambda paths,owner=task:self._content_sensitivity_scope_complete(owner,paths)
        )
        task.signals.failed.connect(
            lambda message,owner=task:self._content_sensitivity_scope_failed(owner,message)
        )
        task.signals.cancelled.connect(
            lambda owner=task:self._content_sensitivity_scope_cancelled(owner)
        )
        try:task.start()
        except RuntimeError as error:self._content_sensitivity_scope_failed(task,str(error))

    def _schedule_pending_content_sensitivity_refresh(self) -> None:
        if (
            not self.state.jobs.shutting_down
            and self._content_sensitivity_scope_task is None
            and not self._content_sensitivity_refresh_scheduled
            and (
                self._pending_content_sensitivity_tag_ids
                or self._pending_content_sensitivity_category_ids
                or self._pending_content_sensitivity_identity_groups
            )
        ):
            self._content_sensitivity_refresh_scheduled=True
            QTimer.singleShot(0,self._resume_content_sensitivity_refresh)

    def _content_sensitivity_scope_complete(self,task,paths) -> None:
        if task is not self._content_sensitivity_scope_task:return
        self._content_sensitivity_scope_task=None
        affected=tuple(dict.fromkeys(Path(value) for value in paths))
        if affected:
            self.content_safety.invalidate(affected)
            self.request_content_safety(affected,force=True)
            self._refresh_content_safety_views(affected)
            for page_name in ("dataset_page","ingest_page"):
                page=getattr(self,page_name,None)
                prepare=getattr(page,"begin_sensitivity_refresh",None)
                if callable(prepare):prepare(affected)
                refresh=getattr(page,"sensitivity_results_changed",None)
                if callable(refresh):refresh(affected)
        self._schedule_pending_content_sensitivity_refresh()

    def _content_sensitivity_scope_failed(self,task,message) -> None:
        if task is not self._content_sensitivity_scope_task:return
        self._content_sensitivity_scope_task=None
        if not self.state.jobs.shutting_down:
            TagManagerWindow._hold_incomplete_sensitivity_statuses(self,task)
            # A failed dependency lookup must not leave a potentially stale
            # rating visible. Protect current views and rebuild lazily instead
            # of starting an automatic full-library scan.
            self.content_safety.clear_cache()
            cards=self.findChildren(ImageCard)
            paths=tuple(dict.fromkeys(
                Path(card.path) for card in cards if card.path is not None
            ))
            for card in cards:self.content_safety(card.path,card)
            self._refresh_protected_previews(
                preserve_temporary_reveal=False
            )
            if paths:self.request_content_safety(paths,force=True)
            self.statusBar().showMessage(
                "Content-rating dependencies could not be narrowed; visible images remain protected and will refresh lazily.",10000,
            )
        self._schedule_pending_content_sensitivity_refresh()

    def _hold_incomplete_sensitivity_statuses(self,task):
        groups=getattr(task,"sensitivity_groups",())
        if not groups:return
        for page_name in ("dataset_page","ingest_page"):
            page=getattr(self,page_name,None)
            hold=getattr(page,"sensitivity_refresh_incomplete",None)
            if callable(hold):hold(groups)

    def _content_sensitivity_scope_cancelled(self,task) -> None:
        # Cancellation cannot leave a previously cached low rating visible.
        # Share the bounded protection/readiness hold used for lookup failure.
        TagManagerWindow._content_sensitivity_scope_failed(self,task,"cancelled")

    def request_content_safety(self,paths,*,force: bool=False) -> None:
        normalized={Path(value) for value in paths}
        held={
            path for path in normalized
            if self.state.dataset.manager_for_path(path) is not None
            and not self.state.dataset.storage_availability(path).readable
        }
        if held:
            self.content_safety.mark_unavailable(
                held,"The image is not currently available locally; no remote download was requested.",
            )
            normalized.difference_update(held)
        if force:
            self._forced_content_safety_paths.update(normalized)
            pending=normalized
        else:pending=self.content_safety.unclassified(normalized)
        if not pending: return
        self._pending_content_safety_paths.update(pending)
        self._resume_content_safety()

    def _resume_content_safety(self) -> None:
        if self._content_safety_task is not None or not self._pending_content_safety_paths: return
        forced=self._forced_content_safety_paths.intersection(self._pending_content_safety_paths)
        ordinary=self._pending_content_safety_paths.difference(forced)
        pending=set(self.content_safety.unclassified(ordinary)); pending.update(forced)
        self._pending_content_safety_paths=pending
        if not pending: return
        started,_active=self.state.begin_long_operation(
            "content_safety",f"Checking content ratings for {len(pending):,} image(s)",
            resource="cpu",exclusive=False,
        )
        if not started: return
        paths=list(self._pending_content_safety_paths); self._pending_content_safety_paths.clear()
        forced_paths=forced.intersection(paths)
        self._forced_content_safety_paths.difference_update(forced_paths)
        if forced_paths:self.content_safety.invalidate(forced_paths)
        def work(progress,should_cancel):
            return self.content_safety.classify_many(paths,progress,should_cancel)
        task=CancelableProgressCallTask(work); self._content_safety_task=task
        task.signals.progress.connect(self._content_safety_progress)
        task.signals.complete.connect(self._content_safety_complete)
        task.signals.failed.connect(lambda message,items=paths:self._content_safety_failed(message,items))
        self.scan_cancel_button.setText("Cancel Safely"); self.scan_cancel_button.setEnabled(True); self.scan_cancel_button.show()
        if self.statusBar().currentMessage()=="Ready":
            self.statusBar().clearMessage()
        self._content_safety_pool.start(task,-1)

    def _content_safety_progress(self,index,total,name):
        self.state.update_long_operation("content_safety",index,total,name)

    def _content_safety_complete(self,result):
        self._content_safety_task=None
        paths=tuple(result.get("paths",()))
        self.state.finish_long_operation("content_safety")
        if result.get("cancelled"):
            checked=max(0,int(result.get("checked",0)))
            self.content_safety.mark_unavailable(paths[checked:],"Content-rating check was paused before this image was reached")
            self.statusBar().showMessage(f"Content-rating check paused safely after {int(result.get('checked',0)):,} image(s)",10000)
        else:
            reused=int(result.get("reused",0))
            self.statusBar().showMessage(
                f"Content ratings ready for {int(result.get('checked',0)):,} image(s)"
                +(f" • reused {reused:,} saved result(s)" if reused else ""),5000,
            )
        self._refresh_content_safety_views(paths)

    def _content_safety_failed(self,message,paths):
        self._content_safety_task=None
        self.content_safety.mark_unavailable(paths,"Content-rating evidence could not be checked")
        self.state.finish_long_operation("content_safety")
        self.statusBar().showMessage("Some content ratings could not be checked; protected unknown images remain blurred",10000)
        self._refresh_content_safety_views(paths)

    def _refresh_content_safety_views(self,paths=None):
        path_key=getattr(
            self.content_safety,"_path_key",lambda value:str(Path(value))
        )
        affected=(
            None if paths is None else {path_key(value) for value in paths}
        )
        find_children=getattr(self,"findChildren",None)
        if callable(find_children):
            cards=find_children(ImageCard)
        else:
            # Keep the publication helper independently testable while the real
            # main window takes the single, faster whole-tree path above.
            cards=[]
            for name in ("ingest_page","dataset_page","review_page","knowledge_page"):
                page=getattr(self,name,None)
                page_find=getattr(page,"findChildren",None)
                if callable(page_find):
                    cards.extend(page_find(ImageCard))
        for card in cards:
            if (
                affected is None
                or path_key(card.path) in affected
            ):
                self.content_safety(card.path,card)
        TagManagerWindow._refresh_protected_previews(
            self,
            paths,preserve_temporary_reveal=True
        )
        dataset=getattr(self,"dataset_page",None)
        if dataset is not None:
            # A visible-page readiness check may have deferred behind another
            # exclusive operation. Revisit its retained paths without rebuilding
            # the gallery or requeuing thumbnails.
            dataset.resume_visible_background_work()

    def _refresh_protected_previews(
        self,paths=None,*,preserve_temporary_reveal: bool
    ):
        path_key=getattr(
            self.content_safety,"_path_key",lambda value:str(Path(value))
        )
        affected=(
            None if paths is None else
            {path_key(value) for value in paths}
        )
        find_children=getattr(self,"findChildren",None)
        if callable(find_children):
            for preview in find_children(ProtectedImageView):
                if (
                    preview.policy is self.content_safety
                    and preview.path is not None
                    and (
                        affected is None
                        or path_key(preview.path) in affected
                    )
                ):
                    preview.refresh_protection(
                        preserve_temporary_reveal=(
                            preserve_temporary_reveal
                        )
                    )

    def change_organization(self,profile: dict):
        self.settings.organization_profile=dict(profile)
        self.statusBar().showMessage("Folder organization profile saved",5000)

    def _restore_window(self):
        geometry=self.qt_settings.value("main/geometry")
        state=self.qt_settings.value("main/state")
        if geometry: self.restoreGeometry(geometry)
        if state: self.restoreState(state)

    def _ensure_window_on_screen(self):
        """Clamp stale saved geometry to the monitor that can display it."""
        app=self.windowHandle().screen() if self.windowHandle() else None
        if app is None:
            from PySide6.QtGui import QGuiApplication
            app=QGuiApplication.primaryScreen()
        if app is None: return
        target=self._visible_client_geometry(app.availableGeometry(),self.geometry(),self.frameGeometry())
        self.setGeometry(target)

    @staticmethod
    def _visible_client_geometry(available: QRect,client: QRect,frame: QRect) -> QRect:
        """Return client geometry whose complete decorated frame is on-screen."""
        left=max(0,client.left()-frame.left()); top=max(0,client.top()-frame.top())
        right=max(0,frame.right()-client.right()); bottom=max(0,frame.bottom()-client.bottom())
        maximum_width=max(900,min(1600,int(available.width()*.90)))
        maximum_height=max(600,min(900,int(available.height()*.86)))
        was_oversized=client.width()>maximum_width or client.height()>maximum_height
        width=min(max(client.width(),900),maximum_width)
        height=min(max(client.height(),600),maximum_height)
        frame_width=width+left+right; frame_height=height+top+bottom
        if was_oversized:
            frame_x=available.left()+(available.width()-frame_width)//2
            frame_y=available.top()+(available.height()-frame_height)//2
        else:
            frame_x=min(max(frame.x(),available.left()),available.right()-frame_width+1)
            frame_y=min(max(frame.y(),available.top()),available.bottom()-frame_height+1)
        return QRect(frame_x+left,frame_y+top,width,height)

    def closeEvent(self,event: QCloseEvent):
        training = getattr(getattr(self, 'state', None), '_training_workflow_controller', None)
        confirm_training = getattr(training, 'confirm_editor_close', None)
        if callable(confirm_training):
            if not getattr(self, '_training_editor_close_connected', False) and hasattr(training, 'editorCloseReady'):
                training.editorCloseReady.connect(lambda: QTimer.singleShot(0, self.close))
                self._training_editor_close_connected = True
            if not confirm_training():
                # Idle Training editors also own unsaved choices. Resolve those
                # before shutting down unrelated work or deleting child dialogs.
                event.ignore(); return
        generation_page = getattr(self, "generation_page", None)
        confirm_drafts = getattr(generation_page, "confirm_draft_loss", None)
        if callable(confirm_drafts):
            if not getattr(self, "_generation_draft_close_connected", False):
                generation_page.draft_save_finished.connect(lambda: QTimer.singleShot(0, self.close))
                self._generation_draft_close_connected = True
            if not confirm_drafts():
                event.ignore(); return
        if hasattr(self,"knowledge_page") and not self.knowledge_page.confirm_discard_order_changes():
            event.ignore(); return
        if hasattr(self,"setup_page") and self.setup_page.has_unsaved_changes() and not self._close_with_unsaved_confirmed:
            if not self.setup_page.confirm_leave(for_close=True):
                event.ignore(); return
            self._close_with_unsaved_confirmed=True
        if hasattr(self,"knowledge_page") and self.knowledge_page.has_active_background_work():
            self.knowledge_page.request_background_shutdown()
            if not self._close_after_knowledge:
                self._close_after_knowledge=True
                self.knowledge_page.background_work_finished.connect(self.close)
            self.statusBar().showMessage(
                "Finishing the verified System Knowledge operation before closing…"
            )
            event.ignore()
            return
        if hasattr(self,"review_page") and self.review_page.has_active_background_work():
            self.review_page.request_background_shutdown()
            if not self._close_after_review:
                self._close_after_review=True
                self.review_page.background_work_finished.connect(self.close)
            self.statusBar().showMessage(
                "Finishing the current Review Center operation before closing…"
            )
            event.ignore()
            return
        if hasattr(self,"ingest_page") and self.ingest_page.has_active_background_work():
            self.ingest_page.request_background_shutdown()
            if not self._close_after_ingest:
                self._close_after_ingest=True
                self.ingest_page.background_work_finished.connect(self.close)
            self.statusBar().showMessage("Stopping Ingest work and releasing AI resources before closing…")
            event.ignore()
            return
        generation_page=getattr(self,"generation_page",None)
        if generation_page is not None and generation_page.has_active_background_work():
            if not getattr(self,"_close_after_generation",False):
                self._close_after_generation=True
                generation_page.background_work_finished.connect(
                    lambda:QTimer.singleShot(0,self.close),
                )
            generation_page.request_background_shutdown()
            self.statusBar().showMessage("Stopping owned video resources and retaining generation recovery evidence before closing…")
            event.ignore()
            return
        training_page=getattr(self,"training_page",None)
        if training_page is not None:
            # Draft-save workers belong to the embedded review rather than a
            # Dataset task. Keep the page alive until those writes finish.
            if training_page.request_background_shutdown():
                if not getattr(self,"_close_after_training_page",False):
                    self._close_after_training_page=True
                    training_page.background_work_finished.connect(self.close)
                event.ignore()
                return
            if training_page._review is not None:
                # A user declined the existing unfinished-review close guard.
                training_page.resume_after_cancelled_shutdown()
                if generation_page is not None:
                    generation_page.resume_after_cancelled_shutdown()
                event.ignore()
                return
        training=getattr(getattr(self,"state",None),"_training_workflow_controller",None)
        if training is not None and training.has_active_background_work():
            training.request_background_shutdown()
            if not getattr(self,"_close_after_training",False):
                self._close_after_training=True
                training.backgroundWorkFinished.connect(self.close)
            self.statusBar().showMessage("Stopping local training and retaining its recovery evidence before closing…")
            event.ignore()
            return
        if hasattr(self,"dataset_page") and self.dataset_page.has_active_background_work():
            self.dataset_page.request_background_shutdown()
            if not self._close_after_dataset:
                self._close_after_dataset=True
                self.dataset_page.background_work_finished.connect(self.close)
            self.statusBar().showMessage("Stopping Dataset AI work and releasing AI resources before closing...")
            event.ignore()
            return
        if hasattr(self,"automation_page") and self.automation_page.has_active_background_work():
            self.automation_page.request_background_shutdown()
            if not self._close_after_automation:
                self._close_after_automation=True
                self.automation_page.background_work_finished.connect(self.close)
            self.statusBar().showMessage(
                "Stopping Rules & Automation work safely before closing…"
            )
            event.ignore()
            return
        if self._scan_thread is not None:
            # A stopped QThread can still own a queued _scan_thread_finished
            # callback. Do not tear down Dataset or state until that slot runs.
            try: running=self._scan_thread.isRunning()
            except RuntimeError: running=False
            if not self._close_after_scan:
                self._close_after_scan=True
                if running and self._scan_worker is not None:
                    self.state.request_long_operation_cancel("library")
                    try: self._scan_worker.cancel()
                    except RuntimeError: pass
                    self._long_operation_cancel_pending_owner="library"
                    TagManagerWindow._publish_library_stop_boundary(self,closing=True)
                else:
                    self.statusBar().showMessage("Finishing the library refresh before closing…")
            event.ignore()
            return
        if not self.ui_test:
            self.qt_settings.setValue("main/geometry",self.saveGeometry())
            self.qt_settings.setValue("main/state",self.saveState())
            self.qt_settings.setValue(
                "main/navigation_width",getattr(
                    self,"_expanded_navigation_width",self.navigation.width(),
                ),
            )
        callback=getattr(self,"_derived_refresh_event_callback",None)
        if callback is not None:
            self.state.events.unsubscribe("derived_refresh_changed",callback)
        callback=getattr(self,"_long_operation_event_callback",None)
        if callback is not None:
            self.state.events.unsubscribe("long_operation_changed",callback)
        review_task=getattr(self,"_review_page_task",None)
        if review_task is not None:
            review_task.cancel()
            self._review_page_task=None
            self._review_page_preparation=None
        self._dataset_preparation_shutdown=True
        dataset_task=getattr(self,"_dataset_page_task",None)
        if dataset_task is not None:
            dataset_task.cancel()
        self._dataset_page_task=None
        self._dataset_page_preparation=None
        self._pending_dataset_page_actions.clear()
        if hasattr(self,"knowledge_page"):
            self.knowledge_page.request_background_shutdown()
        self._finish_knowledge_editor_wait()
        if not self.state.shutdown_background_jobs(wait=True,timeout=2.0):
            event.ignore()
            QMessageBox.warning(self,"Close needs attention",
                "Background work or Metadata readers have not finished closing. "
                "The window remains open; try Close again after they have drained.")
            return
        super().closeEvent(event)
