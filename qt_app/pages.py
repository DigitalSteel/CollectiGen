from __future__ import annotations

from .help_content import workspace_help

import subprocess
from pathlib import Path

from PySide6.QtCore import QSignalBlocker,QThreadPool,QTimer,Signal, Qt,QUrl
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QAbstractItemView,QCheckBox,QComboBox,QDoubleSpinBox,QFileDialog,QFrame,
    QDialog,QGridLayout,QGroupBox,QHBoxLayout,QHeaderView,QLabel,QLineEdit,QListWidget,QMessageBox,
    QProgressBar,QProgressDialog,QPushButton,QScrollArea,QSpinBox,QStackedWidget,QTabWidget,
    QTableWidget,QTableWidgetItem,QTextEdit,QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from .accessibility import announce_accessibly
from .components import FooterProgressDialog as QProgressDialog

from core import (
    AUTOMATION_BEHAVIOR_SETTING_KEYS,
    AI_BASELINE_EVALUATION_SAMPLE_LIMIT,AIBaselineEvaluator,AIEvaluationAdapter,
    CONFIDENCE_PRESETS,FLORENCE2_MODEL,
    DEFAULT_JOYCAPTION_PROMPT,JOYCAPTION_BF16_RECOMMENDED_VRAM_MIB,
    JOYCAPTION_IDENTITY_GUIDANCE,DependencyService,
    JOYCAPTION_MODEL,PLAIN_LANGUAGE_MODELS,Florence2Backend,JoyCaptionBackend,
    LibraryProfile,LibraryRole,PLAIN_LANGUAGE_MAX_NEW_TOKENS_FLOOR,
    WD_TAGGER_DESCRIPTIONS,WD_TAGGER_MODEL_CARDS,
    WD_EVA02_MODEL,WD_TAGGER_MODELS,WDEva02Backend,configured_plain_language_backend,
    joy_benchmark_is_current,
    plain_language_benchmark_is_current,
    nvidia_gpu_profile,recommended_joycaption_quantization,
    recommend_wd_pipeline_settings,wd_benchmark_is_current,
)


DESCRIPTION_PRESETS={
    "concise":{
        "label":"Concise (1–3 sentences)",
        "max_new_tokens":PLAIN_LANGUAGE_MAX_NEW_TOKENS_FLOOR,
        "instructions":(
            "Write one concise visual description as a single paragraph of 1 to 3 "
            "complete sentences. Describe only the most important visible facts: "
            "medium and style; main subjects and notable appearance; pose or action; "
            "setting; and composition. Use clear natural language suitable for image "
            f"training data. {JOYCAPTION_IDENTITY_GUIDANCE} "
            "Do not discuss missing or unknown artist names, signatures, "
            "watermarks, prompts, sources, or provenance."
        ),
    },
    "balanced":{
        "label":"Balanced (3–6 sentences, recommended)",
        "max_new_tokens":PLAIN_LANGUAGE_MAX_NEW_TOKENS_FLOOR,
        "instructions":DEFAULT_JOYCAPTION_PROMPT,
    },
    "detailed":{
        "label":"Detailed (4–7 sentences)",
        "max_new_tokens":384,
        "instructions":(
            "Write one detailed visual description as a single paragraph of 4 to 7 "
            "complete sentences. Describe only what is visibly present: medium and "
            "style; each important subject and notable appearance; clothing and "
            "accessories; pose and action; spatial relationships; composition and "
            "camera viewpoint; setting; lighting; color; and expression. Prioritize "
            "useful visible facts and omit inapplicable categories rather than padding "
            "or speculating. Use clear natural language suitable for image training "
            f"data. {JOYCAPTION_IDENTITY_GUIDANCE} "
            "Do not discuss missing or unknown artist names, signatures, watermarks, "
            "prompts, sources, or provenance."
        ),
    },
}
from core.onboarding import validate_locations
from .components import (
    ContextHelpButton,DisclosureButton,PageHeader,StatusActionPanel,compact_button,guard_scrollable_value_controls,
    set_progress_bar,show_operation_in_progress,
)
from .dependency_dialog import DependencyDialog
from .gallery import GALLERY_DENSITIES
from .ai_evaluation_dialog import AIBaselineEvaluationDialog
from .joy_quality_dialog import JoyCaptionQualityDialog
from .library_profile_dialog import LibraryProfileDialog
from .recycle_bin_dialog import RecycleBinDialog
from .settings_navigation import SettingsNavigator
from .update_dialog import confirm_update_candidate
from .workers import (
    AsyncCallTask,CancelableProgressCallTask,CoordinatedCallTask,
    ProgressCallTask,
)


class MetricCard(QFrame):
    def __init__(self,title: str,parent=None):
        super().__init__(parent); self.setObjectName("metricCard"); self.title=title
        layout=QVBoxLayout(self); layout.setContentsMargins(16,12,16,12)
        heading=QLabel(title); heading.setObjectName("mutedLabel"); self.heading=heading
        self.value=QLabel("Not scanned"); self.value.setObjectName("metricValue")
        self.value.setWordWrap(True)
        layout.addWidget(heading); layout.addWidget(self.value)
        self.setAccessibleName(title); self.set_value("Not scanned")

    def set_value(self,text: str):
        self.value.setText(text)
        spoken="; ".join(part.strip() for part in text.splitlines() if part.strip())
        self.setAccessibleDescription(f"{self.title}: {spoken}")
        self.value.setAccessibleName(f"{self.title}: {spoken}")


class DashboardPage(QWidget):
    recommended=Signal(); vocabulary_requested=Signal()
    downstream_updates_requested=Signal()
    tag_recovery_requested=Signal()
    guided_review_requested=Signal()
    setup_attention_requested=Signal()
    swarm_setup_requested=Signal(str)
    generation_requested=Signal()

    def __init__(self,parent=None):
        super().__init__(parent)
        root=QVBoxLayout(self); root.setContentsMargins(0,0,0,0)
        scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
        root.addWidget(scroll)
        content=QWidget(); self.content=content; scroll.setWidget(content)
        layout=QVBoxLayout(content); layout.setContentsMargins(24,20,24,20); layout.setSpacing(16)
        layout.addWidget(PageHeader(
            "Dashboard","Library health, pending reviews, and local runtime status",
            workspace_help("dashboard"),
        ))

        recommended=QGroupBox("Library status"); self.recommended_group=recommended
        recommended.setAccessibleName("Included library status and next step")
        recommended_layout=QVBoxLayout(recommended)
        recommendation=StatusActionPanel(
            "Included libraries","No current or saved library view",
            "Choose one or more image libraries to begin.","Choose Libraries…",
            self.recommended,primary=True,fill_action=True,
        )
        self.recommendation_panel=recommendation
        self.library_state=recommendation.status_label
        self.library_state.setAccessibleName("Library status")
        self.recommended_text=recommendation.detail_label
        self.recommended_text.setAccessibleName("Library status detail")
        self.recommended_button=recommendation.action_button
        self.recommended_button.setAccessibleDescription("Choose the peer image libraries to consider throughout the app.")
        recommended_layout.addWidget(recommendation)
        layout.addWidget(recommended)
        self._library_state_key="none"
        self._recommendation_signature=(self.recommended_text.text(),self.recommended_button.text(),True)

        guided=QGroupBox("Guided workflows")
        guided.setAccessibleName("Global guided workflows")
        guided_layout=QVBoxLayout(guided)
        guided_panel=StatusActionPanel(
            "Guided Tag Review","Library-wide",
            "Choose a topic, then review matching images from your included libraries.",
            "Open Guided Review…",self.guided_review_requested.emit,
            fill_action=True,
        )
        guided_panel.setAccessibleDescription(
            "Library-wide Guided Tag Review. Opening the wizard does not change image data or start building a queue."
        )
        guided_panel.action_button.setAccessibleDescription(
            "Open Guided Tag Review without starting a queue or changing image data."
        )
        self.guided_workflows=guided
        self.guided_review_panel=guided_panel
        self.guided_review_button=guided_panel.action_button
        guided_layout.addWidget(guided_panel)
        layout.addWidget(guided)

        local_generation=QGroupBox("SwarmUI connection")
        local_generation.setAccessibleName("SwarmUI image-generation connection status")
        local_generation_layout=QVBoxLayout(local_generation)
        self.swarm_scope=QLabel(
            "For SwarmUI image generation. Training and video have separate setup and readiness checks."
        )
        self.swarm_scope.setWordWrap(True)
        self.swarm_scope.setObjectName("mutedLabel")
        self.swarm_state=QLabel("Not configured")
        self.swarm_state.setObjectName("sectionTitle")
        self.swarm_state.setTextFormat(Qt.PlainText)
        self.swarm_summary=QLabel()
        self.swarm_summary.setWordWrap(True)
        self.swarm_summary.setTextFormat(Qt.PlainText)
        self.swarm_freshness=QLabel()
        self.swarm_freshness.setWordWrap(True)
        self.swarm_freshness.setTextFormat(Qt.PlainText)
        self.swarm_freshness.setObjectName("mutedLabel")
        self.swarm_detail=QLabel()
        self.swarm_detail.setWordWrap(True)
        self.swarm_detail.setTextFormat(Qt.PlainText)
        self.swarm_detail.setObjectName("mutedLabel")
        swarm_actions=QHBoxLayout()
        self.swarm_primary_button=compact_button(QPushButton("Set Up Local Generation…"))
        self.swarm_update_button=compact_button(QPushButton("Review Update…"))
        self.swarm_check_button=compact_button(QPushButton("Check Connection"))
        self.generation_work_button=compact_button(QPushButton("Generation Work…"))
        self.generation_work_button.setAccessibleDescription(
            "Resume your generation plan or review retained jobs and verified results. Opening does not submit or recover a job."
        )
        self.swarm_primary_button.clicked.connect(
            self._open_local_generation_action
        )
        self.generation_work_button.clicked.connect(self.generation_requested.emit)
        self.generation_work_button.hide()  # Generate navigation owns this route.
        self.swarm_update_button.clicked.connect(
            lambda:self.swarm_setup_requested.emit("update")
        )
        self.swarm_check_button.clicked.connect(
            lambda:self.swarm_setup_requested.emit("reconnect")
        )
        swarm_actions.addWidget(self.swarm_primary_button)
        swarm_actions.addWidget(self.generation_work_button)
        swarm_actions.addWidget(self.swarm_check_button)
        swarm_actions.addWidget(self.swarm_update_button)
        swarm_actions.addStretch(1)
        local_generation_layout.addWidget(self.swarm_scope)
        local_generation_layout.addWidget(self.swarm_state)
        local_generation_layout.addWidget(self.swarm_summary)
        local_generation_layout.addWidget(self.swarm_freshness)
        local_generation_layout.addLayout(swarm_actions)
        local_generation_layout.addWidget(self.swarm_detail)
        self.local_generation=local_generation
        layout.addWidget(local_generation)
        self.set_swarm_status(None)

        self.setup_notice=QGroupBox("Finish Setup")
        self.setup_notice.setAccessibleName("Finish Setup")
        setup_notice_layout=QHBoxLayout(self.setup_notice)
        self.setup_notice_text=QLabel(); self.setup_notice_text.setWordWrap(True)
        self.setup_notice_text.setAccessibleName("Missing setup locations")
        self.setup_notice_button=QPushButton("Review Setup…")
        self.setup_notice_button.setToolTip(
            "Open Guided Setup at Libraries and folders without scanning or changing files"
        )
        self.setup_notice_button.setAccessibleDescription(
            "Open the Guided Setup locations step to review missing storage locations. Opening it does not scan, move, or change files."
        )
        self.setup_notice_button.clicked.connect(self.setup_attention_requested)
        setup_notice_layout.addWidget(self.setup_notice_text,1)
        setup_notice_layout.addWidget(self.setup_notice_button)
        self.setup_notice.hide(); layout.addWidget(self.setup_notice)

        self.downstream_notice=QGroupBox("Related Updates")
        self.downstream_notice.setAccessibleName("Related updates available")
        downstream_layout=QHBoxLayout(self.downstream_notice)
        downstream_copy=QWidget(); downstream_copy_layout=QVBoxLayout(downstream_copy)
        downstream_copy_layout.setContentsMargins(0,0,0,0)
        downstream_copy_layout.setSpacing(3)
        downstream_title=QLabel("Related updates available")
        downstream_title.setObjectName("sectionTitle")
        self.downstream_notice_title=downstream_title
        self.downstream_notice_text=QLabel(); self.downstream_notice_text.setWordWrap(True)
        self.downstream_notice_text.setAccessibleName("Related update summary")
        downstream_copy_layout.addWidget(downstream_title)
        downstream_copy_layout.addWidget(self.downstream_notice_text)
        self.downstream_notice_button=QPushButton("Review Related Updates…")
        self.downstream_notice_button.setToolTip(
            "Open the exact saved follow-up work without changing files"
        )
        self.downstream_notice_button.setAccessibleDescription(
            "Open the saved Tag updates or recovery. Each image or projection change still requires its own preview and confirmation."
        )
        self.downstream_notice_button.clicked.connect(
            self.downstream_updates_requested
        )
        downstream_layout.addWidget(downstream_copy,1)
        downstream_layout.addWidget(self.downstream_notice_button)
        self.downstream_notice.hide(); layout.addWidget(self.downstream_notice)
        self.tag_tools=QGroupBox("Optional Tag review and recovery")
        tag_tools_layout=QVBoxLayout(self.tag_tools)
        self.tag_recovery_notice=StatusActionPanel(
            "Previous Tag update","Saved recovery available",
            "A previous image Tag update has saved recovery information. "
            "Open it to inspect the changes and available recovery options. "
            "This record does not mean a new update is waiting to be applied.",
            "View Tag Recovery…",self.tag_recovery_requested.emit,fill_action=True,
        )
        self.tag_recovery_text=self.tag_recovery_notice.detail_label
        self.tag_recovery_button=self.tag_recovery_notice.action_button
        self.tag_recovery_button.setToolTip(
            "Optional recovery history. No update is pending from this record; newer edits are protected."
        )
        self.tag_recovery_button.setAccessibleDescription(self.tag_recovery_button.toolTip())
        self.tag_recovery_button.hide()
        self.tag_recovery_notice.hide()

        self.vocabulary_notice=StatusActionPanel(
            "New SDXL Tag names","Optional vocabulary review","",
            "Review Tag Names…",self.vocabulary_requested.emit,fill_action=True,
        )
        self.vocabulary_notice.setAccessibleName("Optional new Tag-name review")
        self.vocabulary_notice_text=self.vocabulary_notice.detail_label
        self.vocabulary_notice_button=self.vocabulary_notice.action_button
        self.vocabulary_notice_button.setToolTip("Open the saved new-name decisions when you are ready")
        self.vocabulary_notice_button.setAccessibleDescription("Open the saved optional SDXL Tag name decisions.")
        self.vocabulary_notice.hide()
        tag_tools_layout.addWidget(self.vocabulary_notice)
        tag_tools_layout.addWidget(self.tag_recovery_notice)
        self.tag_tools.hide(); layout.addWidget(self.tag_tools)

        overview=QGroupBox("Included library overview"); self.overview=overview
        overview.setAccessibleName("Current or saved included-library overview")
        grid=QGridLayout(overview)
        self.images=MetricCard("Images"); self.captions=MetricCard("Caption coverage")
        self.records=MetricCard("Saved image data"); self.tags=MetricCard("Known SDXL Tags")
        self.metrics=[self.images,self.captions,self.records,self.tags]
        for index,card in enumerate(self.metrics): grid.addWidget(card,0,index)
        overview.hide(); layout.addWidget(overview)

        # Actionable notices precede optional guidance and runtime diagnostics.
        for index,widget in enumerate((
            self.setup_notice,self.downstream_notice,self.tag_tools,
        ),start=2):
            layout.removeWidget(widget)
            layout.insertWidget(index,widget)

        layout.addStretch(1)
        QWidget.setTabOrder(self.recommended_button,self.setup_notice_button)
        QWidget.setTabOrder(self.setup_notice_button,self.downstream_notice_button)
        QWidget.setTabOrder(self.downstream_notice_button,self.vocabulary_notice_button)
        QWidget.setTabOrder(self.vocabulary_notice_button,self.tag_recovery_button)
        QWidget.setTabOrder(self.tag_recovery_button,self.guided_review_button)
        QWidget.setTabOrder(self.guided_review_button,self.swarm_primary_button)
        QWidget.setTabOrder(self.swarm_primary_button,self.generation_work_button)
        QWidget.setTabOrder(self.generation_work_button,self.swarm_check_button)
        QWidget.setTabOrder(self.swarm_check_button,self.swarm_update_button)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        width=self.width()
        metric_columns=2 if width<1050 else 4
        grid=self.overview.layout()
        for index,card in enumerate(self.metrics): grid.addWidget(card,index//metric_columns,index%metric_columns)

    def set_library_status(self,key: str,text: str):
        key=str(key or "none"); changed=key!=self._library_state_key
        self._library_state_key=key; self.library_state.setText(text)
        self.library_state.setAccessibleDescription(text)
        self.recommended_group.setAccessibleDescription(
            f"{text}. {self.recommended_text.text()} Action: {self.recommended_button.text()}."
        )
        if changed and self.isVisible():
            announce_accessibly(self.library_state,text)

    def set_recommendation(self,text: str,button_text: str,enabled: bool,description: str="",*,announce=True):
        signature=(text,button_text,bool(enabled)); changed=signature!=self._recommendation_signature
        self._recommendation_signature=signature
        self.recommended_text.setText(text); self.recommended_text.setAccessibleDescription(text)
        self.recommended_button.setText(button_text); self.recommended_button.setEnabled(bool(enabled))
        self.recommended_button.setAccessibleDescription(description or text)
        self.recommended_group.setAccessibleDescription(
            f"{self.library_state.text()}. {text} Action: {button_text}."
        )
        if announce and changed and self.isVisible():
            announce_accessibly(self.recommended_group,text)

    def set_overview(self,*,images: str,captions: str,records: str,tags: str):
        for card,value in zip(self.metrics,(images,captions,records,tags)):
            card.set_value(value)
        self.overview.show()
        self.overview.setAccessibleDescription("; ".join(card.accessibleDescription() for card in self.metrics))

    def clear_overview(self):
        self.overview.hide()
        self.overview.setAccessibleDescription("No current or saved library overview is available.")

    def set_setup_attention(self,issues) -> None:
        values=tuple(str(value).strip() for value in issues if str(value).strip())
        old_text=self.setup_notice_text.text()
        if not values:
            self.setup_notice_text.clear()
            self.setup_notice.setAccessibleDescription(
                "No additional storage location needs attention."
            )
            self.setup_notice.hide()
            return
        count=len(values)
        text=(
            f"{count:,} storage location{'s are' if count!=1 else ' is'} not configured:\n"
            +"\n".join(f"• {value}" for value in values)
        )
        self.setup_notice_text.setText(text)
        self.setup_notice_text.setAccessibleDescription(text)
        self.setup_notice.setAccessibleDescription(
            f"{text} Action: Review Setup."
        )
        self.setup_notice.show()
        if text!=old_text and self.isVisible():
            announce_accessibly(self.setup_notice,text)

    def _open_local_generation_action(self) -> None:
        action=self.swarm_primary_button.property("swarmAction") or "setup"
        if action == "generation":
            self.generation_requested.emit()
        else:
            self.swarm_setup_requested.emit(action)

    def set_swarm_status(self,snapshot) -> None:
        if snapshot is None:
            from core.swarm_runtime import SwarmRuntimeManager
            snapshot=SwarmRuntimeManager().snapshot()
        self.swarm_state.setText(snapshot.headline)
        self.swarm_summary.setText(snapshot.detail)
        if snapshot.configured:
            checked=("Last connection check: "+snapshot.last_health_check
                     if snapshot.last_health_check else "Connection check time unavailable")
            freshness=checked+". Saved observation; use Check Connection to refresh."
            if snapshot.state in {"ready","update_available","busy"}:
                freshness+=" Review the exact model and request in Generate before submitting."
        else:
            freshness="Connect only if you want to generate images through SwarmUI."
        self.swarm_freshness.setText(freshness)
        facts=[]
        if snapshot.runtime_version:
            facts.append(f"SwarmUI version: {snapshot.runtime_version}")
        if snapshot.active_jobs:
            facts.append(f"{snapshot.active_jobs} active/waiting operation{'s' if snapshot.active_jobs != 1 else ''}")
        if snapshot.update_summary:
            facts.append(snapshot.update_summary)
        if snapshot.configured:
            resources=getattr(snapshot,"resource_summary","")
            observed=getattr(snapshot,"resource_checked_at","")
            if resources:
                memory="Memory observation: " + resources + " This does not estimate request capacity."
                if observed and observed[:16] != (snapshot.last_health_check or "")[:16]:
                    memory += " Observed " + observed + "."
                facts.append(memory)
            if snapshot.control_plane_online and not snapshot.work_status_known:
                facts.append("Server-wide work is unverified. Check Connection before runtime changes.")
        detail="\n".join(facts)
        self.swarm_detail.setText(detail)
        self.swarm_detail.setVisible(bool(detail))
        self.local_generation.setAccessibleDescription(
            f"SwarmUI image generation. {snapshot.headline}. {freshness} {detail}"
        )
        if not snapshot.configured:
            action="setup"; label="Set Up Local Generation…"
        elif snapshot.state == "offline":
            action="setup"; label="Start or Repair…"
        elif snapshot.state == "incompatible":
            action="compatibility"; label="Review Compatibility…"
        elif snapshot.state == "needs_attention" and getattr(snapshot,"compatibility_state","") == "unavailable":
            action="compatibility"; label="Review Connection Check…"
        elif snapshot.state == "needs_attention":
            action="setup"; label="Finish SwarmUI Setup…"
        elif snapshot.state == "starting":
            action="setup"; label="View Startup…"
        elif snapshot.state == "busy":
            action="generation"; label="View Generation Work…"
        else:
            action="generation"; label="Plan or Resume Generation…"
        self.swarm_primary_button.setProperty("swarmAction",action)
        self.swarm_primary_button.setText(label)
        self.swarm_primary_button.setAccessibleDescription(
            self.generation_work_button.accessibleDescription() if action == "generation"
            else "Open the local generation connection and setup controls."
        )
        self.swarm_primary_button.setVisible(action != "generation")
        self.generation_work_button.hide()
        self.swarm_update_button.setVisible(snapshot.update_available)
        self.swarm_check_button.setVisible(snapshot.configured)

    def set_vocabulary_notice(self,count: int):
        old_name=self.vocabulary_notice_text.accessibleName(); count=max(0,int(count))
        text=(
            f"The included-library check found {count:,} new SDXL Tag name{'s' if count != 1 else ''} "
            "without saved vocabulary decisions. Choose whether to remember each name, "
            "use an existing Tag, ignore it, or decide later. Undecided names may appear again."
        )
        self.vocabulary_notice_text.setText(text)
        self.vocabulary_notice.setAccessibleDescription(
            f"{count:,} new SDXL Tag name{'s are' if count != 1 else ' is'} ready for optional review."
        )
        self.vocabulary_notice.setVisible(count>0)
        self._update_tag_tools_visibility()
        if count>0:
            self.vocabulary_notice_text.setAccessibleName(text)
            if text!=old_name and self.isVisible():
                announce_accessibly(self.vocabulary_notice,self.vocabulary_notice_text.text())
        else:
            self.vocabulary_notice_text.setAccessibleName("")

    def _update_tag_tools_visibility(self) -> None:
        self.tag_tools.setVisible(
            not self.vocabulary_notice.isHidden() or not self.tag_recovery_notice.isHidden()
        )

    def set_downstream_notice(self,notice,projection=None) -> None:
        old_text=self.downstream_notice_text.text()
        identity_active=bool(getattr(notice,"active",False))
        projection_active=bool(getattr(projection,"active",False))
        recovery=bool(getattr(notice,"recovery_available",False))
        self.tag_recovery_button.setVisible(recovery)
        self.tag_recovery_notice.setVisible(recovery)
        self._update_tag_tools_visibility()
        if not (identity_active or projection_active):
            self.downstream_notice_text.clear()
            self.downstream_notice.setAccessibleDescription(
                "No System Knowledge downstream update is currently pending."
            )
            self.downstream_notice.hide()
            return
        parts=[]
        if identity_active:
            state=str(getattr(notice,"audit_state","needs_audit"))
            possible=getattr(notice,"cached_possible_count",None)
            if state=="audited":
                checked=max(0,int(getattr(notice,"library_image_count",0)))
                candidates=max(0,int(getattr(notice,"candidate_count",0)))
                remaining=max(0,int(getattr(notice,"remaining_count",0)))
                safe=max(0,int(getattr(notice,"safe_count",0)))
                held=max(0,int(getattr(notice,"held_count",0)))
                overlap=max(0,int(
                    getattr(notice,"overlapping_warning_count",0)
                ))
                identity_text=(
                    f"The last reviewed alias audit checked {candidates:,} matching "
                    f"candidate{'s' if candidates!=1 else ''} from {checked:,} "
                    f"indexed image{'s' if checked!=1 else ''}. {remaining:,} matching image"
                    f"{'s' if remaining!=1 else ''} still need attention: "
                    f"{safe:,} have exact corrections and {held:,} are held for "
                    "review."
                )
                if overlap:
                    identity_text+=(
                        f" {overlap:,} of the exact-correction image"
                        f"{'s' if overlap!=1 else ''} also contain unresolved work."
                    )
            elif state=="verification_needed":
                if possible is not None and int(possible)>0:
                    identity_text=(
                        f"A completed alias correction reduced the cached work to "
                        f"{int(possible):,} image{'s' if int(possible)!=1 else ''}. "
                        "Run the saved review again to verify what remains."
                    )
                else:
                    identity_text=(
                        "Applied or restored alias cleanup is ready for a confirming "
                        "audit. This notice stays until that review verifies nothing remains."
                    )
            elif possible is not None:
                identity_text=(
                    f"The saved library index identifies {int(possible):,} possible "
                    f"matching image{'s' if int(possible)!=1 else ''} after a "
                    "System Knowledge name change. Review the saved scope to verify "
                    "the exact work."
                )
            else:
                identity_text=(
                    "A System Knowledge name change still needs a reviewed "
                    "matching-image audit. The exact count is not known yet."
                )
            parts.append(identity_text)
        if projection_active:
            count=getattr(projection,"cached_affected_image_count",None)
            count_text=(
                "No matching images were found in the saved index. Check the current libraries to finish any steps that need no changes."
                if count==0 else
                f"The current saved index identifies {int(count):,} matching image{'s' if int(count)!=1 else ''}."
                if count is not None else
                "Open included libraries to calculate the current matching-image count."
            )
            labels={
                "captions":"Training TXT captions",
                "metadata":"managed Metadata backups",
                "organization":"folder organization",
            }
            targets=tuple(getattr(projection,"pending_targets",()) or ())
            pending=", ".join(labels.get(value,value) for value in targets)
            parts.append(
                "Saved Tag changes may affect related files. "
                f"{count_text} Review {pending}, or dismiss reminders for file updates you do not want."
            )
        text=" ".join(parts)
        self.downstream_notice_text.setText(text)
        self.downstream_notice_text.setAccessibleDescription(text)
        self.downstream_notice.setAccessibleDescription(
            f"{self.downstream_notice_title.text()}. {text} Action: "
            +"Review Related Updates."
        )
        self.downstream_notice.show()
        if text!=old_text and self.isVisible():
            announce_accessibly(self.downstream_notice,text)


class SetupPage(QWidget):
    theme_changed=Signal(str); color_blind_changed=Signal(bool); tooltips_changed=Signal(bool); content_level_changed=Signal(str)
    preparation_defaults_changed=Signal()
    guided_setup_requested=Signal()
    bug_report_requested=Signal()
    feedback_archive_requested=Signal()
    library_profile_changed=Signal(str)
    open_automation=Signal(str,object)
    swarm_status_changed=Signal(object)
    image_setup_return_requested=Signal()
    automation_return_requested=Signal()
    preparation_return_requested=Signal()

    def __init__(
        self,settings,parent=None,*,state=None,
        defer_expensive_initialization: bool=False,
        module_provider=None,
        dependency_service_factory=None,
    ):
        super().__init__(parent); self.settings=settings; self.state=state; self.pool=QThreadPool.globalInstance(); self._benchmark_task=None; self._joy_benchmark_task=None; self._joy_quality_task=None; self._joy_quality_samples=(); self._ai_evaluation_task=None; self._ai_evaluation_samples=(); self._update_task=None; self._update_progress=None
        if state is not None and dependency_service_factory is not None and getattr(state,"dependency_service_factory",None) is not dependency_service_factory:
            raise ValueError("Setup must retain the state's admitted dependency service factory.")
        self.dependency_service_factory=dependency_service_factory if dependency_service_factory is not None else getattr(state,"dependency_service_factory",None)
        if self.dependency_service_factory is not None and not callable(self.dependency_service_factory):
            raise ValueError("The dependency service factory must be callable.")
        self._defer_expensive_initialization=bool(defer_expensive_initialization)
        self._component_inventory_task=None; self._component_inventory_ready=False
        self._dirty_sections=set(); self._suppress_dirty=False
        self._committed_section_values={}
        self._joy_quantization_user_selected=False
        from .theme import THEMES
        outer=QVBoxLayout(self); outer.setContentsMargins(22,18,22,18); outer.setSpacing(12)
        header=PageHeader(
            "Setup","",
            workspace_help("setup"),
        )
        self.header=header; outer.addWidget(header)
        self.image_setup_return_panel=StatusActionPanel(
            "Generation Setup is paused", "Return available",
            "Your image-based setup choices are retained. Save or discard any pending Setup changes, "
            "then return and choose Suggest again when ready.",
            "Return to Generation Setup",self.image_setup_return_requested.emit,
        )
        self.image_setup_return_panel.hide()
        outer.addWidget(self.image_setup_return_panel)
        self.automation_return_panel=StatusActionPanel(
            "AI defaults are paused", "Return available",
            "Your Rules & Automation draft is retained. Save or discard any pending Setup "
            "changes, then return to review the policy. Choosing a model does not save "
            "or run the policy.",
            "Return to AI Defaults",self.automation_return_requested.emit,
        )
        self.automation_return_panel.hide()
        outer.addWidget(self.automation_return_panel)
        self._preparation_return_detail=(
            "The selected images and preparation choices are retained for a fresh review. "
            "Finish or discard pending Setup changes, then return; no image work starts automatically."
        )
        self.preparation_return_panel=StatusActionPanel(
            "Image preparation is paused", "Return available",
            self._preparation_return_detail,
            "Return to Preparation",self.preparation_return_requested.emit,
        )
        self.preparation_return_panel.hide()
        outer.addWidget(self.preparation_return_panel)
        self.models_generation_return_panel=StatusActionPanel(
            "Reviewed model setup is paused", "Return available",
            "Your image-generation recommendation was not applied. Finish pending Setup changes, "
            "then return to the same Models review. Continue will recheck models and guidance.",
            "Return to Reviewed Models…", self.open_models_workspace,
            action_width=290,
        )
        self.models_generation_return_panel.hide()
        outer.addWidget(self.models_generation_return_panel)
        guided=QGroupBox("Guided setup"); guided_layout=QVBoxLayout(guided)
        guided_panel=StatusActionPanel(
            "Guided setup checklist","Available",
            "Choose your image folders, review app preferences, and set up optional tools. You can return to this checklist at any time.",
            "Open Guided Setup…",self.guided_setup_requested.emit,primary=True,
        )
        self.guided_setup_panel=guided_panel; self.guided_setup_button=guided_panel.action_button
        guided_layout.addWidget(guided_panel)
        defaults=QGroupBox("Workflow defaults"); defaults_layout=QGridLayout(defaults)
        defaults_note=QLabel(
            "Everyday preferences save immediately. AI configuration and reusable "
            "policies have their own Save action."
        )
        defaults_note.setWordWrap(True); defaults_layout.addWidget(defaults_note,0,0,1,2)
        self.workflow_default_buttons={}
        for row,(key,label,description,callback) in enumerate((
            ("prepare","Preparation Defaults…","Evidence intake, AI result reuse, fingerprints and review choices for Ingest and Dataset.",
             lambda:self.open_automation.emit("automation.prepare",None)),
            ("automation","AI Behavior Defaults…","Suggestions and automatic behavior after library checks.",
             lambda:self.open_automation.emit("automation.behavior.ai",None)),
            ("analysis","Analysis Models and Descriptions…","Tag models, description style and local analysis configuration. Review changes, then Save AI Configuration.",
             lambda:self.show_section("ai","descriptions")),
            ("generation","Generation Setup…","Image and video connections. Reusable image setups and output presets stay with your Generate draft.",
             lambda:self.show_section("ai","generation")),
            ("training","Training Setup…","Trainer software and folders. Choose goal, model, captions and presets in Training; collections stay in Dataset.",
             lambda:self.show_section("components_downloads","training")),
        ),1):
            note=QLabel(description); note.setWordWrap(True)
            button=compact_button(QPushButton(label)); button.clicked.connect(callback)
            button.setAccessibleDescription(description)
            defaults_layout.addWidget(note,row,0); defaults_layout.addWidget(button,row,1,Qt.AlignRight)
            self.workflow_default_buttons[key]=button
        defaults_layout.setColumnStretch(0,1)
        self.show_tooltips=QCheckBox("Show contextual help tooltips")
        self.show_tooltips.setChecked(settings.show_tooltips)
        appearance=QGroupBox("Appearance and accessibility"); form=QGridLayout(appearance)
        form.addWidget(QLabel("Color theme"),0,0)
        self.theme=QComboBox()
        for key,label in THEMES.items(): self.theme.addItem(label,key)
        index=self.theme.findData(settings.ui_theme); self.theme.setCurrentIndex(max(0,index))
        self.color_blind=QCheckBox("Use color-blind-friendly status colors and border patterns"); self.color_blind.setChecked(settings.color_blind_mode)
        hint=QLabel("Changes apply immediately and are remembered for the next launch."); hint.setWordWrap(True); hint.setObjectName("mutedLabel")
        form.addWidget(self.theme,0,1); form.addWidget(self.color_blind,1,0,1,2)
        form.addWidget(self.show_tooltips,2,0,1,2)
        form.addWidget(hint,3,0,1,2)
        self.theme.currentIndexChanged.connect(self._save_theme_preference)
        self.color_blind.toggled.connect(self._save_color_blind_preference)

        libraries=QGroupBox("Named image libraries"); libraries_layout=QVBoxLayout(libraries)
        libraries_intro=QLabel(
            "Each named library is a separate peer collection with the same Dataset tools. "
            "All valid libraries are included throughout the app by default; the shared "
            "Libraries filter can show one or several without mixing or moving files. The "
            "default library supplies initial focus and older single-folder actions only."
        )
        libraries_intro.setWordWrap(True); libraries_intro.setObjectName("mutedLabel")
        libraries_layout.addWidget(libraries_intro)
        self.library_table=QTableWidget(0,4)
        self.library_table.setHorizontalHeaderLabels(("Name","Type","Folder","Status"))
        self.library_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.library_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.library_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.library_table.setAccessibleName("Named image libraries")
        self.library_table.verticalHeader().hide()
        self.library_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeToContents)
        self.library_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeToContents)
        self.library_table.horizontalHeader().setSectionResizeMode(2,QHeaderView.Stretch)
        self.library_table.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeToContents)
        self.library_table.setMinimumHeight(150)
        libraries_layout.addWidget(self.library_table)
        library_actions=QHBoxLayout()
        self.add_library_button=compact_button(QPushButton("Add Library…"),primary=True)
        self.edit_library_button=compact_button(QPushButton("Edit…"))
        self.activate_library_button=compact_button(QPushButton("Make Default"))
        self.remove_library_button=compact_button(QPushButton("Remove…"))
        self.add_library_button.clicked.connect(self.add_library)
        self.edit_library_button.clicked.connect(self.edit_library)
        self.activate_library_button.clicked.connect(self.activate_library)
        self.remove_library_button.clicked.connect(self.remove_library)
        library_actions.addWidget(self.add_library_button)
        library_actions.addWidget(self.edit_library_button)
        library_actions.addWidget(self.activate_library_button)
        library_actions.addWidget(self.remove_library_button)
        library_actions.addStretch(1)
        libraries_layout.addLayout(library_actions)
        self.library_guidance=QLabel()
        self.library_guidance.setWordWrap(True); self.library_guidance.setObjectName("mutedLabel")
        self.library_guidance.setAccessibleName("Selected library consequences")
        libraries_layout.addWidget(self.library_guidance)
        self.library_registry=self.settings.library_registry()
        self.library_table.itemSelectionChanged.connect(self._library_selection_changed)
        self.library_table.itemDoubleClicked.connect(lambda _item:self.edit_library())
        self._refresh_library_profiles()

        from .folder_locations import FolderLocationsWidget
        self.folder_locations=FolderLocationsWidget(settings,self)
        # Setup places this editor's groups into task panes below. Keep the
        # otherwise unused container (and its introductory/status labels)
        # from painting behind Setup's navigation.
        self.folder_locations.hide()
        from core.folder_locations import APP_FOLDERS
        self.storage_fields={key:self.folder_locations.fields[key] for key,_ in APP_FOLDERS}
        self.storage_status=self.folder_locations.status
        self._folder_ai_root=settings.ai_models_directory
        self.folder_locations.saved.connect(self._folder_locations_saved)
        self.open_recycle_button=compact_button(QPushButton("Open Recycle Bin and Orphan Recovery…"))
        self.open_recycle_button.setEnabled(self.state is not None)
        self.open_recycle_button.clicked.connect(self.open_recycle_bin)
        self.models_workspace_button=compact_button(QPushButton("Models…"))
        self.models_workspace_button.setEnabled(self.state is not None)
        self.models_workspace_button.clicked.connect(self.open_models_workspace)
        self.generation_workspace_button=compact_button(QPushButton("Generation Connections and Plans…"))
        self.generation_workspace_button.setEnabled(self.state is not None)
        self.generation_workspace_button.clicked.connect(self.open_generation_workspace)
        self.resize_readiness=QLabel("Use Simple Resize or AI Upscale from Ingest or Dataset. AI Upscale needs a compatible model and runtime; the resize window helps you set them up. Set preferred dimensions in Rules & Automation.")
        self.resize_readiness.setWordWrap(True)

        startup=QGroupBox("Startup behavior"); startup_grid=QGridLayout(startup)
        self.restore_last=QCheckBox("Reopen included image libraries at startup"); self.restore_last.setChecked(settings.restore_last_dataset)
        self.check_dependencies=QCheckBox("Offer to repair missing core components at startup"); self.check_dependencies.setChecked(settings.check_dependencies_on_startup)
        startup_grid.addWidget(self.restore_last,0,0)
        startup_grid.addWidget(self.check_dependencies,0,1)
        startup_policy_note=QLabel(
            "Startup library checks are configured in Rules & Automation."
        )
        startup_policy_note.setWordWrap(True)
        startup_policy_note.setObjectName("mutedLabel")
        self.open_startup_library_check_policy_button=compact_button(
            QPushButton("Open Library Check Policy…")
        )
        self.open_startup_library_check_policy_button.clicked.connect(
            lambda:self.open_automation.emit("automation.library_checks",None)
        )
        startup_policy_note.setParent(self); startup_policy_note.hide()
        self.open_startup_library_check_policy_button.setParent(self)
        self.open_startup_library_check_policy_button.hide()

        scanning=QGroupBox("Scanning defaults"); scanning_grid=QGridLayout(scanning)
        self.auto_import_source=QCheckBox("Automatically import TXT and Metadata for review")
        self.auto_import_source.setChecked(settings.auto_import_source_evidence)
        self.auto_import_source.setToolTip(
            "Metadata includes adjacent TXT and embedded image fields. When off, scans still create minimal authoritative JSON records without importing either source."
        )
        self.auto_import_source.setParent(self); self.auto_import_source.hide()
        scan_note=QLabel(
            "Rules & Automation owns the default subfolder scope for future "
            "contextual folder scans."
        )
        scan_note.setWordWrap(True); scan_note.setObjectName("mutedLabel")
        self.open_scan_scope_policy_button=compact_button(
            QPushButton("Open Scan Scope Policy…")
        )
        self.open_scan_scope_policy_button.clicked.connect(
            lambda:self.open_automation.emit("automation.library_checks.scope",None)
        )
        evidence_owner_note=QLabel(
            "Changed TXT and Metadata imports are configured in Rules & Automation."
        )
        evidence_owner_note.setWordWrap(True)
        evidence_owner_note.setObjectName("mutedLabel")
        self.open_evidence_intake_button=compact_button(
            QPushButton("Open Evidence Intake…")
        )
        self.open_evidence_intake_button.clicked.connect(
            lambda:self.open_automation.emit("automation.evidence.intake",None)
        )
        scanning_grid.addWidget(scan_note,0,0)
        scanning_grid.addWidget(
            self.open_scan_scope_policy_button,0,1,Qt.AlignRight,
        )
        scanning_grid.addWidget(evidence_owner_note,1,0)
        scanning_grid.addWidget(
            self.open_evidence_intake_button,1,1,Qt.AlignRight,
        )
        scanning_grid.setColumnStretch(0,1)

        display=QGroupBox("Gallery display and content protection"); display_grid=QGridLayout(display)
        self.gallery_size=QComboBox()
        for value in (12,24,36,48,72,96): self.gallery_size.addItem(f"{value} images per gallery page",value)
        self.gallery_size.setCurrentIndex(max(0,self.gallery_size.findData(settings.gallery_page_size)))
        self.content_level=QComboBox()
        for rating,label in (
            ("general","General only by default"),
            ("sensitive","General and Sensitive by default"),
            ("questionable","Up to Questionable by default"),
            ("explicit","All rated images by default"),
        ): self.content_level.addItem(label,rating)
        self.content_level.setCurrentIndex(max(0,self.content_level.findData(settings.gallery_content_level)))
        self.remember_reveals=QCheckBox("Remember intentional reveals until CollectiGen closes"); self.remember_reveals.setChecked(settings.remember_content_reveals)
        self.content_safety_note=QLabel(
            "Calculating the effective preview protection…"
        )
        content_safety_note=self.content_safety_note
        content_safety_note.setWordWrap(True); content_safety_note.setObjectName("mutedLabel")
        self.hidden_ratings={}
        hidden_row=QHBoxLayout()
        for rating in ("general","sensitive","questionable","explicit"):
            control=QCheckBox(rating.title()); control.setChecked(rating in settings.gallery_hidden_ratings)
            self.hidden_ratings[rating]=control; hidden_row.addWidget(control)
        hidden_row.addStretch(1)
        self.blur_unknown=QCheckBox("No dependable rating yet")
        self.blur_unknown.setChecked(getattr(settings,"gallery_blur_unknown",True))
        self.blur_unknown.setToolTip(
            "Applies while a rating is checking or remains unavailable, stale, unreadable, or unresolved. Unknown is not a fifth content rating."
        )
        export_policy_note=QLabel(
            "Rules & Automation owns the maximum content rating included by "
            "Dataset projections and exports."
        )
        export_policy_note.setWordWrap(True)
        export_policy_note.setObjectName("mutedLabel")
        self.open_export_content_policy_button=compact_button(
            QPushButton("Open Export Content Policy…")
        )
        self.open_export_content_policy_button.clicked.connect(
            lambda:self.open_automation.emit("automation.export_content",None)
        )
        general_status=QLabel("Changes save automatically."); general_status.setObjectName("mutedLabel")
        display_grid.addWidget(QLabel("Gallery page size"),0,0); display_grid.addWidget(self.gallery_size,0,1)
        display_grid.addWidget(QLabel("Default unblurred range"),1,0); display_grid.addWidget(self.content_level,1,1)
        display_grid.addWidget(self.remember_reveals,2,0,1,2)
        self.apply_blur_label=QLabel("Always blur (overrides the range)")
        display_grid.addWidget(self.apply_blur_label,3,0); display_grid.addLayout(hidden_row,3,1)
        display_grid.addWidget(self.blur_unknown,4,1)
        display_grid.addWidget(content_safety_note,5,0,1,2)
        display_grid.addWidget(export_policy_note,6,0)
        display_grid.addWidget(
            self.open_export_content_policy_button,6,1,Qt.AlignRight,
        )
        display_grid.addWidget(general_status,7,0,1,2)
        self._update_content_safety_note()

        dependency_service=self.dependency_service()
        self._ai_dependency_service=dependency_service
        # Package imports, executable probes, and GPU discovery can take several
        # seconds on a cold portable runtime. Build the Qt controls immediately;
        # a background inventory replaces these provisional values after paint.
        self._joy_gpu_profile=None
        self._joy_nf4_ready=False
        self._joy_recommended_quantization=str(
            getattr(settings,"joycaption_quantization","bf16")
        )
        self.use_wd=QCheckBox("Generate AI Tags"); self.use_wd.setChecked(settings.suggest_wd_for_missing_tags)
        self.use_joy=QCheckBox("Generate AI Descriptions"); self.use_joy.setChecked(settings.suggest_joycaption_for_missing_descriptions)
        self.auto_run_wd=QCheckBox("Generate After Library Checks")
        self.auto_run_wd.setChecked(settings.auto_run_wd_on_managed_images)
        self.auto_run_joy=QCheckBox("Generate After Library Checks")
        self.auto_run_joy.setChecked(settings.auto_run_joy_on_managed_images)
        self.plain_language_model=QComboBox()
        for model_id,(label,_directory,summary) in PLAIN_LANGUAGE_MODELS.items():
            self.plain_language_model.addItem(label,model_id)
            self.plain_language_model.setItemData(
                self.plain_language_model.count()-1,summary,Qt.ToolTipRole,
            )
        self.plain_language_model.setCurrentIndex(max(
            0,self.plain_language_model.findData(settings.plain_language_model),
        ))
        self.use_wd.setToolTip(
            "Use the enabled local AI Tag models when Prepare Images or Recheck "
            "Data includes AI generation. Results remain attributed evidence."
        )
        self.use_joy.setToolTip(
            "Use the selected local caption model when Prepare Images or Recheck "
            "Data includes AI generation. Results remain attributed evidence."
        )
        self.auto_run_wd.setToolTip(
            "Generate AI Tags automatically for missing or changed images after "
            "a library check. This does not by itself confirm those Tags."
        )
        self.auto_run_joy.setToolTip(
            "Generate AI Descriptions automatically for missing or changed images "
            "after a library check. They remain review findings."
        )
        for control in (
            self.use_wd,self.use_joy,self.auto_run_wd,self.auto_run_joy,
        ):
            control.setAccessibleDescription(control.toolTip())

        self.prepare_confirm_tags=QCheckBox("Confirm Safe Tags During Prepare")
        self.prepare_confirm_tags.setChecked(bool(
            getattr(settings,"prepare_confirm_safe_tags",True)
        ))
        self.prepare_confirm_tags.setToolTip(
            "Use safe automatic Tag confirmation by default during Prepare Images. "
            "Only known, conflict-free Tags meeting the preparation vote policy "
            "can be confirmed."
        )
        self.prepare_confirm_tags.setAccessibleDescription(
            self.prepare_confirm_tags.toolTip()
        )
        self.prepare_descriptions=QCheckBox(
            "Fill / Upgrade Descriptions During Prepare"
        )
        self.prepare_descriptions.setChecked(bool(
            getattr(settings,"prepare_preferred_descriptions",True)
        ))
        self.prepare_descriptions.setToolTip(
            "During Prepare Images, fill an empty Description or upgrade prior "
            "caption-model output only when it is exactly attributable and unchanged."
        )
        self.prepare_descriptions.setAccessibleDescription(
            self.prepare_descriptions.toolTip()
        )
        self.prepare_confidence=QComboBox()
        for key in ("balanced","conservative","permissive"):
            self.prepare_confidence.addItem(CONFIDENCE_PRESETS[key]["label"],key)
        self.prepare_confidence.addItem("Custom","custom")
        self.prepare_confidence.setAccessibleName(
            "Preparation automation confidence"
        )
        self.prepare_votes=QSpinBox(); self.prepare_votes.setRange(1,99)
        self.prepare_votes.setValue(max(1,min(99,int(getattr(
            settings,"prepare_tag_vote_minimum",2,
        )))))
        self.prepare_votes.setAccessibleName("Minimum independent Tag votes")
        self.prepare_external_vote=QCheckBox("TXT / Metadata vote")
        self.prepare_external_vote.setChecked(bool(getattr(
            settings,"prepare_external_evidence_vote",True,
        )))
        configured_prepare_preset=self._matching_preparation_preset() or "custom"
        self.prepare_confidence.setCurrentIndex(max(
            0,self.prepare_confidence.findData(configured_prepare_preset),
        ))
        self.prepare_confidence_warning=QLabel()
        self.prepare_confidence_warning.setObjectName("warningLabel")
        self.prepare_confidence_warning.setWordWrap(True)
        self.wd_general=QDoubleSpinBox(); self.wd_general.setRange(.05,.95); self.wd_general.setSingleStep(.05); self.wd_general.setValue(settings.wd_general_threshold)
        self.wd_character=QDoubleSpinBox(); self.wd_character.setRange(.05,.99); self.wd_character.setSingleStep(.05); self.wd_character.setValue(settings.wd_character_threshold)
        self.wd_rating=QDoubleSpinBox(); self.wd_rating.setRange(.05,.99); self.wd_rating.setSingleStep(.05); self.wd_rating.setValue(settings.wd_rating_threshold)
        self.joy_tokens=QSpinBox(); self.joy_tokens.setRange(PLAIN_LANGUAGE_MAX_NEW_TOKENS_FLOOR,1000); self.joy_tokens.setValue(settings.joycaption_max_new_tokens)
        self.joy_prompt=QTextEdit(settings.joycaption_prompt); self.joy_prompt.setMaximumHeight(95)
        tag_defaults=QGroupBox("Minimum Confidence to Suggest"); self.tag_defaults=tag_defaults; tag_defaults_grid=QGridLayout(tag_defaults)
        threshold_help=(
            "This is the AI model's minimum detection confidence for creating a "
            "reviewable Tag suggestion. Higher values produce fewer suggestions; "
            "this is separate from preparation automation confidence, evidence "
            "confidence, Review Center decisions, and content protection."
        )
        threshold_specs=(
            ("General Tags",self.wd_general,0,0),
            ("Character Tags",self.wd_character,0,2),
            ("Rating Tags",self.wd_rating,1,0),
        )
        for label_text,control,row,column in threshold_specs:
            label=QLabel(label_text); label.setBuddy(control)
            label.setToolTip(threshold_help); control.setToolTip(threshold_help)
            control.setAccessibleDescription(threshold_help)
            tag_defaults_grid.addWidget(label,row,column)
            tag_defaults_grid.addWidget(control,row,column+1)
        tag_defaults.setToolTip(threshold_help)
        description_defaults=QGroupBox("AI Description Style and Generation"); self.description_defaults=description_defaults; description_defaults_grid=QGridLayout(description_defaults)
        self.description_help_button=ContextHelpButton(
            "AI Descriptions",
            """## Choose a style

Presets update the visible instructions and maximum length only after you choose **Apply Preset**. You can review or edit both values before saving.

## Length

The maximum is a generation limit, not an exact word count. A higher value gives the model more room to finish, but any fixed limit can still cut off the final sentence.

## Advanced controls

Advanced settings tune how the model produces text. Leave them at their defaults unless you are comparing results or troubleshooting a specific problem.""",
            description_defaults,
        )
        description_defaults_grid.addWidget(
            self.description_help_button,0,3,Qt.AlignRight
        )
        self.description_preset=QComboBox()
        self.description_preset.addItem("Choose a description preset…","")
        for preset_id,preset in DESCRIPTION_PRESETS.items():
            self.description_preset.addItem(preset["label"],preset_id)
        self.description_preset.setAccessibleName("AI Description preset")
        self.description_preset.setAccessibleDescription(
            "Preview a complete replacement for the visible Instructions and "
            "Maximum Generated Tokens controls. Selecting a preset changes nothing."
        )
        self.apply_description_preset_button=compact_button(
            QPushButton("Apply Preset")
        )
        self.apply_description_preset_button.setEnabled(False)
        self.apply_description_preset_button.setAccessibleDescription(
            "Stage the exact previewed Instructions and Maximum Generated Tokens "
            "without changing Advanced generation controls or saving the configuration."
        )
        preset_label=QLabel("Description Preset")
        preset_label.setBuddy(self.description_preset)
        preset_help=(
            "A preset replaces only the visible Instructions and Maximum Generated "
            "Tokens after you choose Apply Preset. Advanced generation controls do "
            "not change, and Save AI Configuration remains a separate step."
        )
        preset_label.setToolTip(preset_help)
        self.description_preset.setToolTip(preset_help)
        self.description_preset_preview=QLabel(
            "Choose a preset to preview its instructions and length. "
            "Nothing changes until you apply it."
        )
        self.description_preset_preview.setWordWrap(True)
        self.description_preset_preview.setObjectName("mutedLabel")
        self.description_preset_preview.setAccessibleName(
            "AI Description preset preview"
        )
        self.description_preset_preview.setAccessibleDescription(
            self.description_preset_preview.text()
        )
        description_defaults_grid.addWidget(preset_label,1,0)
        description_defaults_grid.addWidget(self.description_preset,1,1,1,2)
        description_defaults_grid.addWidget(
            self.apply_description_preset_button,1,3
        )
        description_defaults_grid.addWidget(
            self.description_preset_preview,2,0,1,4
        )
        instructions_label=QLabel("Instructions")
        instructions_label.setBuddy(self.joy_prompt)
        instructions_help=(
            "Directs JoyCaption what visible details to describe and how to write "
            "them. Presets replace this complete text only after Apply Preset; you "
            "can edit the staged instructions before saving."
        )
        instructions_label.setToolTip(instructions_help)
        self.joy_prompt.setToolTip(instructions_help)
        self.joy_prompt.setAccessibleDescription(instructions_help)
        description_defaults_grid.addWidget(instructions_label,3,0)
        description_defaults_grid.addWidget(self.joy_prompt,3,1,1,3)
        max_length_label=QLabel("Maximum Generated Tokens")
        max_length_help=(
            "Hard generation ceiling for one AI Description. Controlled comparisons "
            "found that a 100-token ceiling stopped descriptions mid-sentence, so "
            f"{PLAIN_LANGUAGE_MAX_NEW_TOKENS_FLOOR} is the minimum allowed for "
            "ordinary generation. Higher limits allow more room, but no token cap "
            "guarantees a complete final sentence. Tokens are model units rather "
            "than an exact word or character count."
        )
        max_length_label.setBuddy(self.joy_tokens)
        max_length_label.setToolTip(max_length_help)
        self.joy_tokens.setToolTip(max_length_help)
        self.joy_tokens.setAccessibleDescription(max_length_help)
        description_defaults_grid.addWidget(max_length_label,4,0); description_defaults_grid.addWidget(self.joy_tokens,4,1)
        self.joy_token_guidance=QLabel(
            f"{PLAIN_LANGUAGE_MAX_NEW_TOKENS_FLOOR} tokens gives ordinary descriptions "
            "room to finish. Any fixed limit can still cut off the last sentence."
        )
        self.joy_token_guidance.setWordWrap(True)
        self.joy_token_guidance.setObjectName("mutedLabel")
        self.joy_token_guidance.setAccessibleName("AI Description length guidance")
        self.joy_token_guidance.setAccessibleDescription(max_length_help)
        description_defaults_grid.addWidget(self.joy_token_guidance,5,0,1,4)
        description_defaults_grid.setColumnStretch(1,1)
        tagger_panel=QGroupBox("Model Choices and Runtime"); self.tagger_panel=tagger_panel; tagger_grid=QGridLayout(tagger_panel)
        self.tag_model_help_button=ContextHelpButton(
            "AI Tag Models",
            """## Model choices

Enable one or more downloaded models. Each model contributes its own Tag suggestions.

## Consensus

Consensus is the number of enabled models that must agree before CollectiGen treats a suggestion as shared. One model cannot provide independent multi-model agreement.

## Runtime

Use **Automatic** unless you need to choose the CPU or an NVIDIA GPU explicitly. Batch size can improve speed but may use more memory.""",
            tagger_panel,
        )
        tagger_grid.addWidget(self.tag_model_help_button,0,1,Qt.AlignRight)
        self.wd_taggers={}; self.wd_tagger_status={}
        for index,(model_id,(label,_directory)) in enumerate(WD_TAGGER_MODELS.items()):
            benchmark=settings.ai_model_benchmarks.get(model_id,{})
            current=wd_benchmark_is_current(settings,model_id,benchmark) if benchmark else False
            seconds=float(benchmark["seconds_per_image"]) if current and benchmark.get("seconds_per_image") else None
            model_dir=dependency_service.wd_model_dir_for(model_id)
            ready=(model_dir/"model.onnx").is_file() and (model_dir/"selected_tags.csv").is_file()
            control=QCheckBox(label); control.setProperty("modelLabel",label)
            control.setChecked(ready and model_id in settings.enabled_wd_taggers); control.setEnabled(ready)
            availability="Downloaded and ready." if ready else "Available to download from Components."
            info=WD_TAGGER_MODEL_CARDS[model_id]
            control.setToolTip(f"Best fit: {info['best_for']}.\n\n{WD_TAGGER_DESCRIPTIONS[model_id]}\n\n{availability}")
            control.setAccessibleDescription(availability)
            card=QFrame(); card.setObjectName("modelCard"); card_layout=QVBoxLayout(card); card_layout.setContentsMargins(10,8,10,8); card_layout.setSpacing(3)
            card_layout.addWidget(control)
            best=QLabel(f"Best for: {info['best_for']}."); best.setWordWrap(True); card_layout.addWidget(best)
            summary=QLabel(info["summary"]); summary.setWordWrap(True); summary.setObjectName("mutedLabel"); card_layout.addWidget(summary)
            model_status=QLabel(); model_status.setOpenExternalLinks(False); model_status.setTextFormat(Qt.RichText)
            model_status.setWordWrap(True)
            model_status.setText(self._model_status_html(
                model_id,ready=ready,seconds=seconds,stale=bool(benchmark) and not current,
            )); card_layout.addWidget(model_status)
            model_status.linkActivated.connect(lambda href,mid=model_id:self._model_status_link(mid,href))
            self.wd_taggers[model_id]=control; self.wd_tagger_status[model_id]=model_status
            tagger_grid.addWidget(card,1+index//2,index%2)
        tagger_grid.setColumnStretch(0,1); tagger_grid.setColumnStretch(1,1)
        self.wd_consensus=QSpinBox(); self.wd_consensus.setRange(1,len(WD_TAGGER_MODELS)+1); self.wd_consensus.setValue(settings.wd_consensus_minimum)
        self.auto_merge_ai=QCheckBox("Confirm Safe Tag Consensus Automatically"); self.auto_merge_ai.setChecked(settings.auto_merge_ai_tags)
        self.wd_provider=QComboBox(); self.wd_provider.addItem("Automatic","auto"); self.wd_provider.addItem("NVIDIA GPU (CUDA)","cuda"); self.wd_provider.addItem("CPU","cpu")
        self.wd_provider.setCurrentIndex(max(0,self.wd_provider.findData(settings.wd_execution_provider)))
        self.wd_batch_size=QSpinBox(); self.wd_batch_size.setRange(1,16); self.wd_batch_size.setValue(settings.wd_batch_size)
        tagger_grid.addWidget(QLabel("Execution device"),4,0); tagger_grid.addWidget(self.wd_provider,4,1)
        tagger_grid.addWidget(QLabel("Images per model call"),5,0); tagger_grid.addWidget(self.wd_batch_size,5,1)
        self.hardware_status_label=QLabel("Hardware: checking in the background…")
        device=self.hardware_status_label; device.setObjectName("mutedLabel")
        tagger_grid.addWidget(device,6,0,1,2)
        self.choose_registered_tagger=compact_button(QPushButton("Choose Verified Tag Model…"))
        self.choose_registered_tagger.clicked.connect(lambda:self.choose_preparation_resource("tagger"))
        tagger_grid.addWidget(self.choose_registered_tagger,7,0,1,2)
        tagger_toggle=DisclosureButton(
            "Choose Tag Models and Runtime…",
            "Hide Tag Models and Runtime",
            tagger_panel,
        ); self.tagger_toggle=tagger_toggle

        tag_model_panel=QGroupBox("Tag Analysis Models")
        self.tag_model_panel=tag_model_panel
        tag_model_layout=QGridLayout(tag_model_panel)
        self.tag_model_status=QLabel()
        self.tag_model_status.setWordWrap(True)
        self.tag_model_status.setObjectName("mutedLabel")
        self.tag_model_status.setAccessibleName("AI Tag model selection status")
        downloads=compact_button(QPushButton("Manage AI Tag Components…"))
        self.manage_tag_components_button=downloads
        downloads.clicked.connect(lambda:self.manage_ai_components("wd"))
        self.wd_benchmark_button=compact_button(
            QPushButton("Benchmark AI Tag Models…")
        )
        self.wd_benchmark_button.clicked.connect(self.benchmark_ai)
        tag_model_actions=QHBoxLayout()
        tag_model_actions.setContentsMargins(0,0,0,0)
        tag_model_actions.addWidget(tagger_toggle)
        tag_model_actions.addStretch(1)
        tag_model_layout.addWidget(self.tag_model_status,0,0)
        tag_model_layout.addWidget(downloads,0,1,Qt.AlignRight)
        tag_model_layout.addLayout(tag_model_actions,1,0,1,2)
        tag_model_layout.addWidget(tagger_panel,2,0,1,2)
        tag_model_layout.setColumnStretch(0,1)
        ready_taggers=sum(control.isEnabled() for control in self.wd_taggers.values())
        selected_taggers=sum(
            control.isEnabled() and control.isChecked()
            for control in self.wd_taggers.values()
        )
        tagger_toggle.setChecked(ready_taggers>0 and selected_taggers==0)

        saved_behavior=QGroupBox("Saved AI Behavior")
        saved_behavior_layout=QVBoxLayout(saved_behavior)
        self.saved_automation_status=QLabel()
        self.saved_automation_status.setWordWrap(True)
        self.saved_automation_status.setObjectName("sectionTitle")
        self.automation_example=QLabel()
        self.automation_example.setWordWrap(True)
        self.automation_example.setObjectName("mutedLabel")
        saved_behavior_layout.addWidget(self.saved_automation_status)
        saved_behavior_layout.addWidget(self.automation_example)

        workflow=QGroupBox("Optional Configuration Preset"); workflow_grid=QGridLayout(workflow)
        workflow_intro=QLabel(
            "Presets are optional shortcuts for changing several settings at "
            "once. Choosing one only previews its exact changes. Stage Preset "
            "Changes updates the controls; Save AI Configuration is the only write."
        )
        workflow_intro.setWordWrap(True)
        workflow_intro.setObjectName("mutedLabel")
        self.automation_mode=QComboBox()
        self.automation_mode.addItem("Choose an optional preset…","")
        self.automation_mode.addItem("Review first","review")
        self.automation_mode.addItem("Analyze automatically","analyze")
        self.automation_mode.addItem("Maximum safe automation","maximum")
        self.automation_mode.setAccessibleName("Optional AI configuration preset")
        self.automation_mode.setAccessibleDescription(
            "Selecting a preset previews its exact settings without "
            "changing or saving the current AI configuration."
        )
        self.apply_automation_preset_button=compact_button(
            QPushButton("Stage Preset Changes"),primary=True,
        )
        self.apply_automation_preset_button.setEnabled(False)
        self.apply_automation_preset_button.setAccessibleName(
            "Stage Preset Changes"
        )
        self.apply_automation_preset_button.setAccessibleDescription(
            "Stage the complete previewed preset without saving it."
        )
        self.automation_preset_preview=QLabel(
            "No preset selected. Your saved or pending settings are unchanged."
        )
        self.automation_preset_preview.setWordWrap(True)
        self.automation_preset_preview.setObjectName("mutedLabel")
        self.automation_preset_preview.setAccessibleName(
            "Optional preset change preview"
        )
        self._automation_preview_plan=None
        self._applied_automation_preset=""
        workflow_grid.addWidget(workflow_intro,0,0,1,3)
        workflow_grid.addWidget(QLabel("Preset"),1,0)
        workflow_grid.addWidget(self.automation_mode,1,1)
        workflow_grid.addWidget(self.apply_automation_preset_button,1,2)
        workflow_grid.addWidget(self.automation_preset_preview,2,0,1,3)
        workflow_grid.setColumnStretch(1,1)

        capability_overview=QGroupBox("Image Analysis Status")
        capability_grid=QGridLayout(capability_overview)
        self.overview_capability_note=QLabel(
            "Installed means the model files are available on this computer. "
            "Enabled or selected means the model will be used by the settings "
            "on the Analyze Tags or Describe Images tab. Image generation and "
            "upscaling have separate model and runtime requirements."
        )
        self.overview_capability_note.setWordWrap(True)
        self.overview_capability_note.setObjectName("mutedLabel")
        self.overview_tag_status=QLabel(); self.overview_tag_status.setWordWrap(True)
        self.overview_description_status=QLabel(); self.overview_description_status.setWordWrap(True)
        self.overview_tag_status.setAccessibleName("AI Tag capability status")
        self.overview_description_status.setAccessibleName(
            "AI Description capability status"
        )
        capability_grid.addWidget(self.overview_capability_note,0,0)
        capability_grid.addWidget(self.overview_tag_status,1,0)
        capability_grid.addWidget(self.overview_description_status,2,0)
        capability_grid.setColumnStretch(0,1)

        automation_owner=QGroupBox("Automatic behavior")
        automation_owner_layout=QGridLayout(automation_owner)
        automation_owner_note=QLabel(
            "Rules & Automation owns reusable AI defaults, automatic generation, "
            "safe-confirmation votes, preparation behavior, and suggestion confidence. "
            "Setup only manages the models, runtime, hardware, and generation parameters they require."
        )
        automation_owner_note.setWordWrap(True)
        automation_owner_note.setObjectName("mutedLabel")
        self.open_automation_behavior_button=compact_button(
            QPushButton("Open Automatic Behavior…"),primary=True,
        )
        self.open_automation_behavior_button.clicked.connect(
            lambda:self.open_automation.emit("automation.behavior.ai",None)
        )
        automation_owner_layout.addWidget(automation_owner_note,0,0)
        automation_owner_layout.addWidget(
            self.open_automation_behavior_button,0,1,Qt.AlignRight,
        )
        automation_owner_layout.setColumnStretch(0,1)
        self.ai_evaluation_button=compact_button(
            QPushButton("Compare Tag and Description Models…")
        )
        self.ai_evaluation_button.setToolTip(
            "Run the supported local AI models currently Ready on this computer "
            "on the same small sample, then review quality, unsupported claims, "
            "content-safety handling, speed, and peak resource use without "
            "installing missing models or saving paths or raw outputs."
        )
        self.ai_evaluation_button.setAccessibleDescription(
            self.ai_evaluation_button.toolTip()
        )
        self.ai_evaluation_button.clicked.connect(self.evaluate_ai_baseline)
        installed_ai_review=QGroupBox("Review Installed AI")
        self.installed_ai_review=installed_ai_review
        installed_ai_review_layout=QGridLayout(installed_ai_review)
        installed_ai_review_note=QLabel(
            "Compare the supported local models currently Ready on this computer "
            "using one small shared sample. Missing models are skipped and nothing "
            "is installed or saved."
        )
        installed_ai_review_note.setWordWrap(True)
        installed_ai_review_note.setObjectName("mutedLabel")
        installed_ai_review_layout.addWidget(installed_ai_review_note,0,0)
        installed_ai_review_layout.addWidget(
            self.ai_evaluation_button,0,1,Qt.AlignRight,
        )
        installed_ai_review_layout.setColumnStretch(0,1)

        tag_generation=QGroupBox("AI Generation"); self.tag_generation=tag_generation
        tag_generation_layout=QVBoxLayout(tag_generation)
        tag_generation_layout.setContentsMargins(8,5,8,6)
        tag_generation_layout.setSpacing(3)
        tag_generation_layout.addWidget(self.use_wd)
        tag_generation_layout.addWidget(self.auto_run_wd)
        tag_generation_layout.addStretch(1)

        tag_changes=QGroupBox("Automatic Changes"); self.tag_changes=tag_changes
        tag_changes_grid=QGridLayout(tag_changes)
        tag_changes_grid.setContentsMargins(8,5,8,6)
        tag_changes_grid.setHorizontalSpacing(7)
        tag_changes_grid.setVerticalSpacing(3)
        self.auto_merge_ai.setText("Confirm Safe Tags After Library Checks")
        self.wd_consensus_label=QLabel("Minimum Tag votes")
        self.wd_consensus_label.setToolTip(
            "For automatic changes after library checks, each enabled AI Tag "
            "model counts once. A matching pending TXT or Metadata finding can "
            "add one corroborating vote in total."
        )
        self.wd_consensus_label.setBuddy(self.wd_consensus)
        self.wd_consensus.setAccessibleName(
            "Minimum Tag votes after library checks"
        )
        tag_changes_grid.addWidget(self.auto_merge_ai,0,0,1,2)
        tag_changes_grid.addWidget(self.wd_consensus_label,1,0)
        tag_changes_grid.addWidget(self.wd_consensus,1,1)
        tag_changes_grid.addWidget(self.prepare_confirm_tags,2,0,1,2)
        prepare_confidence_label=QLabel("Preparation Confidence")
        prepare_confidence_label.setBuddy(self.prepare_confidence)
        tag_changes_grid.addWidget(prepare_confidence_label,3,0)
        tag_changes_grid.addWidget(self.prepare_confidence,3,1)
        prepare_votes=QHBoxLayout()
        prepare_votes.setContentsMargins(0,0,0,0)
        prepare_votes.setSpacing(5)
        prepare_votes_label=QLabel("Minimum Tag votes")
        prepare_votes_label.setBuddy(self.prepare_votes)
        prepare_votes.addWidget(prepare_votes_label)
        prepare_votes.addWidget(self.prepare_votes)
        prepare_votes.addWidget(self.prepare_external_vote)
        prepare_votes.addStretch(1)
        tag_changes_grid.addLayout(prepare_votes,4,0,1,2)
        tag_changes_grid.addWidget(self.prepare_confidence_warning,5,0,1,2)
        tag_changes_grid.setColumnStretch(1,1)
        tag_workflow=QWidget(); self.tag_workflow=tag_workflow
        self.tag_workflow_layout=QGridLayout(tag_workflow)
        self.tag_workflow_layout.setContentsMargins(0,0,0,0)
        self.tag_workflow_layout.setHorizontalSpacing(7)
        self.tag_workflow_layout.setVerticalSpacing(5)
        self.tag_workflow_layout.addWidget(tag_generation,0,0)
        self.tag_workflow_layout.addWidget(tag_changes,0,1)
        self.tag_workflow_layout.setColumnStretch(0,1)
        self.tag_workflow_layout.setColumnStretch(1,2)

        description_generation=QGroupBox("AI Generation")
        self.description_generation=description_generation
        description_generation_layout=QVBoxLayout(description_generation)
        description_generation_layout.setContentsMargins(8,5,8,6)
        description_generation_layout.setSpacing(3)
        description_generation_layout.addWidget(self.use_joy)
        description_generation_layout.addWidget(self.auto_run_joy)
        description_generation_layout.addStretch(1)
        description_changes=QGroupBox("Automatic Changes")
        self.description_changes=description_changes
        description_changes_layout=QVBoxLayout(description_changes)
        description_changes_layout.setContentsMargins(8,5,8,6)
        description_changes_layout.setSpacing(3)
        description_changes_layout.addWidget(self.prepare_descriptions)
        description_changes_layout.addStretch(1)
        description_changes.setToolTip(self.prepare_descriptions.toolTip())
        tag_generation.setAccessibleName("AI Tag generation settings")
        tag_changes.setAccessibleName("Automatic Tag change settings")
        description_generation.setAccessibleName(
            "AI Description generation settings"
        )
        description_changes.setAccessibleName(
            "Automatic Description change settings"
        )
        description_workflow=QWidget(); self.description_workflow=description_workflow
        self.description_workflow_layout=QGridLayout(description_workflow)
        self.description_workflow_layout.setContentsMargins(0,0,0,0)
        self.description_workflow_layout.setHorizontalSpacing(7)
        self.description_workflow_layout.setVerticalSpacing(5)
        self.description_workflow_layout.addWidget(description_generation,0,0)
        self.description_workflow_layout.addWidget(description_changes,0,1)
        self.description_workflow_layout.setColumnStretch(0,1)
        self.description_workflow_layout.setColumnStretch(1,2)
        # Keep the retired widgets alive for staged legacy Setup state and old
        # leave/discard snapshots, but never place them in the visible Setup UI.
        # Automation is the only user-facing owner of these controls now.
        self._retired_ai_behavior_widgets=(
            saved_behavior,workflow,tag_workflow,tag_defaults,
            description_workflow,
        )
        for retired in self._retired_ai_behavior_widgets:
            retired.setParent(self); retired.hide()
        joy_benchmark_panel=QGroupBox("AI Description Model and Runtime")
        self.joy_benchmark_panel=joy_benchmark_panel
        joy_benchmark_layout=QGridLayout(joy_benchmark_panel)
        self.plain_language_model_note=QLabel()
        self.plain_language_model_note.setWordWrap(True)
        self.plain_language_model_note.setObjectName("mutedLabel")
        manage_caption_models=compact_button(
            QPushButton("Manage AI Description Components…")
        )
        self.manage_description_components_button=manage_caption_models
        manage_caption_models.clicked.connect(
            self.manage_plain_language_downloads
        )
        self.joy_benchmark_status=QLabel(); self.joy_benchmark_status.setWordWrap(True)
        self.joy_benchmark_status.setObjectName("mutedLabel")
        self.joy_benchmark_button=compact_button(QPushButton("Benchmark AI Description Model…"))
        self.joy_benchmark_button.clicked.connect(self.benchmark_joy)
        self.joy_quality_button=compact_button(QPushButton("Compare Caption Quality…"))
        self.joy_quality_button.setToolTip(
            "Generate a temporary controlled BF16/NF4 comparison with up to "
            "four deliberately selected local images."
        )
        self.joy_quality_button.clicked.connect(self.compare_joy_quality)
        self.joy_length_quality_button=compact_button(
            QPushButton("Compare Length Limits…")
        )
        self.joy_length_quality_button.setToolTip(
            "Generate a temporary controlled 4-bit NF4 comparison between "
            "the saved maximum description length and the 100-token TagGUI "
            "reference with up to four deliberately selected local images."
        )
        self.joy_length_quality_button.clicked.connect(
            self.compare_joy_length_quality
        )
        joy_benchmark_layout.addWidget(QLabel("Caption model"),0,0)
        joy_benchmark_layout.addWidget(self.plain_language_model,0,1)
        joy_benchmark_layout.addWidget(manage_caption_models,0,2)
        joy_benchmark_layout.addWidget(self.plain_language_model_note,1,0,1,3)
        self.choose_registered_captioner=compact_button(QPushButton("Choose Verified Caption Model…"))
        self.choose_registered_captioner.clicked.connect(lambda:self.choose_preparation_resource("captioner"))
        joy_benchmark_layout.addWidget(self.choose_registered_captioner,5,0,1,3)
        joy_benchmark_layout.setColumnStretch(1,1)
        # Component readiness is deliberately provisional until the background
        # inventory finishes.  Calling ``capability_ready`` here would perform
        # the same executable/package probes that this page is designed to keep
        # off the Qt event loop.
        self._refresh_joy_benchmark_status(dependency_service,ready=False)
        self.advanced_ai=QGroupBox("Advanced Generation Controls"); advanced_grid=QGridLayout(self.advanced_ai)
        self.joy_min_tokens=QSpinBox(); self.joy_min_tokens.setRange(0,1000); self.joy_min_tokens.setValue(settings.joycaption_min_new_tokens)
        self.joy_beams=QSpinBox(); self.joy_beams.setRange(1,12); self.joy_beams.setValue(settings.joycaption_num_beams)
        self.joy_length_penalty=QDoubleSpinBox(); self.joy_length_penalty.setRange(.1,3); self.joy_length_penalty.setSingleStep(.1); self.joy_length_penalty.setValue(settings.joycaption_length_penalty)
        self.joy_sample=QCheckBox("Use varied/random wording (sampling)"); self.joy_sample.setChecked(settings.joycaption_do_sample)
        self.joy_temperature=QDoubleSpinBox(); self.joy_temperature.setRange(.1,2); self.joy_temperature.setSingleStep(.1); self.joy_temperature.setValue(settings.joycaption_temperature)
        self.joy_top_k=QSpinBox(); self.joy_top_k.setRange(0,200); self.joy_top_k.setValue(settings.joycaption_top_k)
        self.joy_top_p=QDoubleSpinBox(); self.joy_top_p.setRange(.1,1); self.joy_top_p.setSingleStep(.05); self.joy_top_p.setValue(settings.joycaption_top_p)
        self.joy_repetition=QDoubleSpinBox(); self.joy_repetition.setRange(.5,2); self.joy_repetition.setSingleStep(.05); self.joy_repetition.setValue(settings.joycaption_repetition_penalty)
        self.joy_ngram=QSpinBox(); self.joy_ngram.setRange(0,10); self.joy_ngram.setValue(settings.joycaption_no_repeat_ngram_size)
        self.joy_gpu=QSpinBox(); self.joy_gpu.setRange(0,16); self.joy_gpu.setValue(settings.joycaption_gpu_index)
        self.joy_quantization=QComboBox()
        self.joy_quantization.addItem("Full model (BF16, highest fidelity)","bf16")
        self.joy_quantization.addItem("4-bit model (NF4, lower memory; may be faster)","nf4")
        self.joy_quantization.setCurrentIndex(max(
            0,self.joy_quantization.findData(
                self._joy_recommended_quantization
            ),
        ))
        self._joy_previous_quantization=str(self.joy_quantization.currentData())
        self.joy_quantization.setToolTip(
            "Full BF16 preserves the model's native weights but uses substantially "
            "more GPU memory. 4-bit NF4 reduces model memory and may run faster on "
            "memory-limited GPUs, with a possible caption-quality difference. "
            "New profiles use a hardware-aware recommendation; every saved choice "
            "is preserved until you change it."
        )
        advanced_controls=(
            ("Minimum length",self.joy_min_tokens,
             "Minimum tokens requested before the model may stop. Raising this can force unnecessary text; keep it well below the maximum."),
            ("Number of beams",self.joy_beams,
             "Candidate sequences explored and ranked for one Description. More beams can improve consistency but take longer; 1 uses one draft."),
            ("Length preference",self.joy_length_penalty,
             "Beam-search preference for longer values above 1 or shorter values below 1. A value of 1 is neutral."),
            ("Temperature",self.joy_temperature,
             "Randomness used only when varied/random wording is enabled. Higher values are more varied and less predictable."),
            ("Top-K",self.joy_top_k,
             "When sampling, restrict each next token to the K most likely choices. Set 0 to disable this restriction."),
            ("Top-P",self.joy_top_p,
             "When sampling, keep the smallest likely-token set whose combined probability reaches this value. Lower values are more conservative."),
            ("Repetition penalty",self.joy_repetition,
             "Values above 1 discourage repeated wording; values that are too high can distort the Description. A value of 1 is neutral."),
            ("No-repeat phrase size",self.joy_ngram,
             "Prevent repeating token sequences of this length. Set 0 to disable the restriction."),
        )
        for index,(label,control,help_text) in enumerate(advanced_controls):
            label_widget=QLabel(label)
            label_widget.setBuddy(control)
            label_widget.setToolTip(help_text)
            label_widget.setAccessibleDescription(help_text)
            control.setAccessibleName(label)
            control.setToolTip(help_text)
            control.setAccessibleDescription(help_text)
            advanced_grid.addWidget(label_widget,index//3,(index%3)*2)
            advanced_grid.addWidget(control,index//3,(index%3)*2+1)
        sampling_help=(
            "Generate a sampled, non-deterministic wording choice instead of "
            "deterministic beam search. Temperature, Top-K, and Top-P apply only "
            "while this is enabled; leave it off for controlled comparisons."
        )
        self.joy_sample.setToolTip(sampling_help)
        self.joy_sample.setAccessibleDescription(sampling_help)
        advanced_grid.addWidget(self.joy_sample,3,0,1,6)
        self.joy_memory_guidance=QLabel()
        self.joy_memory_guidance.setWordWrap(True)
        self.joy_memory_guidance.setObjectName("mutedLabel")
        self.joy_memory_guidance.setTextFormat(Qt.RichText)
        self.joy_memory_guidance.setOpenExternalLinks(False)
        self.joy_memory_guidance.linkActivated.connect(
            lambda _href:self.open_dependencies("joy")
        )
        runtime_memory_label=QLabel("Model memory mode")
        runtime_memory_label.setBuddy(self.joy_quantization)
        runtime_memory_label.setToolTip(self.joy_quantization.toolTip())
        self.joy_quantization.setAccessibleName("Model memory mode")
        self.joy_quantization.setAccessibleDescription(
            self.joy_quantization.toolTip()
        )
        runtime_gpu_help=(
            "Zero-based NVIDIA GPU index used for local AI Descriptions. GPU 0 "
            "is the first detected NVIDIA GPU."
        )
        runtime_gpu_label=QLabel("GPU number")
        runtime_gpu_label.setBuddy(self.joy_gpu)
        runtime_gpu_label.setToolTip(runtime_gpu_help)
        self.joy_gpu.setAccessibleName("GPU number")
        self.joy_gpu.setToolTip(runtime_gpu_help)
        self.joy_gpu.setAccessibleDescription(runtime_gpu_help)
        joy_benchmark_layout.addWidget(runtime_memory_label,2,0)
        joy_benchmark_layout.addWidget(self.joy_quantization,2,1,1,2)
        joy_benchmark_layout.addWidget(runtime_gpu_label,3,0)
        joy_benchmark_layout.addWidget(self.joy_gpu,3,1)
        joy_benchmark_layout.addWidget(self.joy_memory_guidance,4,0,1,3)
        self._update_joy_memory_guidance()
        advanced_toggle=DisclosureButton("Advanced Generation…","Hide Advanced Generation",self.advanced_ai); self.advanced_toggle=advanced_toggle
        description_defaults_grid.addWidget(advanced_toggle,6,0,1,4,Qt.AlignLeft)
        description_defaults_grid.addWidget(self.advanced_ai,7,0,1,4)
        self.ai_dirty=QLabel(); self.ai_dirty.setObjectName("warningLabel"); self.ai_dirty.hide()
        self.save_ai_button=compact_button(QPushButton("Save AI Configuration"),primary=True); self.save_ai_button.clicked.connect(self.save_ai); self.save_ai_button.setEnabled(False); self.save_ai_button.hide()
        ai_actions=QHBoxLayout(); ai_actions.addWidget(self.ai_dirty); ai_actions.addStretch(1); ai_actions.addWidget(self.save_ai_button)

        organization=QGroupBox("Folder organization policy")
        organization_grid=QGridLayout(organization)
        organization_note=QLabel(
            "Folder routes and the reusable filename default are configured in "
            "Rules & Automation. Setup retains named-library locations only; "
            "opening the policy does not scan or move images."
        )
        organization_note.setWordWrap(True)
        organization_note.setObjectName("mutedLabel")
        self.open_organization_policy_button=compact_button(
            QPushButton("Open Organization Policy…"),
        )
        self.open_organization_policy_button.clicked.connect(
            lambda:self.open_automation.emit("automation.organization",None)
        )
        organization_grid.addWidget(organization_note,0,0)
        organization_grid.addWidget(
            self.open_organization_policy_button,0,1,Qt.AlignRight,
        )
        organization_grid.setColumnStretch(0,1)
        scanning.setParent(self); scanning.hide()
        organization.setParent(self); organization.hide()
        self.dependency_panel=DependencyDialog(settings,self,state=self.state,embedded=True,
            dependency_service_factory=self.dependency_service_factory)
        self.dependency_panel.capabilities_changed.connect(
            lambda:self._refresh_ai_availability(self.dependency_panel.service)
        )
        self.dependency_panel.settings_changed.connect(self._sync_ai_controls)
        components=self.dependency_panel
        from .swarm_runtime_widget import SwarmRuntimeWidget
        self.swarm_runtime=SwarmRuntimeWidget(settings,self)
        self.swarm_runtime.status_changed.connect(self.swarm_status_changed)
        training=QGroupBox("Trainer Software"); training_layout=QVBoxLayout(training)
        training_note=QLabel(
            "Install or repair the supported local trainer here. Training owns "
            "plan presets, packages, run review, saved history, and recovery. "
            "Dataset owns collection membership and caption rules. Setup changes "
            "do not rewrite existing training plans or frozen packages."
        )
        training_note.setWordWrap(True); training_layout.addWidget(training_note)
        self.training_history_button=compact_button(QPushButton("Training History…"))
        self.training_history_button.clicked.connect(self.open_training_history)
        self.training_history_button.setParent(self); self.training_history_button.hide()
        self.training_setup_button=compact_button(QPushButton("Trainer Setup…"))
        self.training_setup_button.clicked.connect(self.open_training_setup)
        training_actions=QHBoxLayout()
        training_actions.addWidget(self.training_setup_button)
        training_actions.addStretch(1)
        training_layout.addLayout(training_actions)

        performance=QGroupBox("Performance and quality checks")
        performance_layout=QGridLayout(performance)
        performance_note=QLabel(
            "Measure the saved local configuration or compare temporary outputs. "
            "These checks do not install models or change Image Knowledge."
        )
        performance_note.setWordWrap(True)
        performance_note.setObjectName("mutedLabel")
        performance_layout.addWidget(performance_note,0,0,1,3)
        performance_layout.addWidget(self.wd_benchmark_button,1,0)
        performance_layout.addWidget(self.joy_benchmark_button,1,1)
        performance_layout.addWidget(self.joy_benchmark_status,2,0,1,3)
        performance_layout.addWidget(self.joy_quality_button,3,0)
        performance_layout.addWidget(self.joy_length_quality_button,3,1)
        performance_layout.addWidget(installed_ai_review,4,0,1,3)
        performance_layout.setColumnStretch(2,1)

        updates=QGroupBox("Application Updates"); update_grid=QGridLayout(updates)
        self.update_channel=QComboBox(); self.update_channel.addItem("Stable releases","stable"); self.update_channel.addItem("Prerelease previews (currently alpha)","beta")
        self.update_channel.setCurrentIndex(max(0,self.update_channel.findData(settings.update_channel)))
        self.check_updates_on_startup=QCheckBox("Check for updates on startup")
        self.check_updates_on_startup.setChecked(settings.check_updates_on_startup)
        self.check_updates_on_startup.setToolTip(
            "When enabled, startup checks GitHub release metadata for the selected channel. No update is downloaded without asking."
        )
        self.update_status=QLabel(); self.update_status.setWordWrap(True); self.update_status.setObjectName("mutedLabel")
        from .components import bind_status_footer
        bind_status_footer(updates,self.update_status,title="Application updates",follow_visibility=True)
        check_update=compact_button(QPushButton("Check for Updates"),primary=True); self.check_update_button=check_update; check_update.clicked.connect(self.check_for_update)
        offline_update=compact_button(QPushButton("Use Offline Update…")); self.offline_update_button=offline_update; offline_update.clicked.connect(self.prepare_offline_update)
        rollback=compact_button(QPushButton("Restore Previous Version…")); self.rollback_update_button=rollback; rollback.clicked.connect(self.request_update_rollback)
        installed=self._application_update_service().installed_package
        check_update.setEnabled(installed); offline_update.setEnabled(installed)
        self.check_updates_on_startup.setEnabled(installed)
        rollback.setEnabled(self._rollback_available())
        update_note=QLabel(
            "Updates are verified before they are prepared and are applied after restart. Interrupted downloads resume safely."
            if installed else
            "This is a source checkout. Application replacement is enabled only in a verified portable installation."
        )
        update_note.setWordWrap(True); update_note.setObjectName("mutedLabel")
        update_grid.addWidget(QLabel("Channel"),0,0); update_grid.addWidget(self.update_channel,0,1)
        update_grid.addWidget(check_update,0,2,Qt.AlignLeft); update_grid.addWidget(offline_update,0,3,Qt.AlignLeft)
        update_grid.addWidget(self.check_updates_on_startup,1,0,1,2)
        update_grid.addWidget(update_note,2,0,1,3); update_grid.addWidget(rollback,2,3,Qt.AlignLeft)

        maintenance=QGroupBox("Support and diagnostics"); tools=QGridLayout(maintenance)
        logs=compact_button(QPushButton("Open Logs")); self.open_logs_button=logs; logs.clicked.connect(self.open_logs)
        report_bug=compact_button(QPushButton("Feedback…")); report_bug.clicked.connect(self.bug_report_requested.emit)
        export_feedback=compact_button(QPushButton("Export Collected Feedback…")); export_feedback.clicked.connect(self.feedback_archive_requested.emit)
        tools.addWidget(logs,0,0,Qt.AlignLeft)
        tools.addWidget(report_bug,0,1,Qt.AlignLeft)
        tools.addWidget(export_feedback,0,2,Qt.AlignLeft)
        # Help in the shared ribbon owns support commands. Keep their existing
        # sources alive without a Setup section containing only launch buttons.
        maintenance.setParent(self);maintenance.hide()

        def section_page(*widgets):
            page=QWidget(); page_layout=QVBoxLayout(page); page_layout.setContentsMargins(0,0,0,0)
            scroll=QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QFrame.NoFrame)
            content=QWidget(); content_layout=QVBoxLayout(content); content_layout.setContentsMargins(8,12,8,12); content_layout.setSpacing(14)
            for widget in widgets: content_layout.addWidget(widget)
            content_layout.addStretch(1); scroll.setWidget(content); guard_scrollable_value_controls(content); page_layout.addWidget(scroll)
            return page

        def subsection_tabs(accessible_name,*specs):
            tabs=QTabWidget(); tabs.setDocumentMode(True); tabs.setAccessibleName(accessible_name)
            indexes={}
            for key,label,widgets in specs:
                page=widgets if isinstance(widgets,QWidget) else section_page(*widgets)
                indexes[key]=tabs.addTab(page,label)
            return tabs,indexes

        self.libraries_storage_panel=QWidget()
        library_storage_root=QVBoxLayout(self.libraries_storage_panel)
        library_storage_root.setContentsMargins(0,0,0,0)
        library_storage_note=QLabel(
            "Choose a task. Folder preferences save independently and do not move existing files."
        )
        library_storage_note.setWordWrap(True)
        library_storage_root.addWidget(library_storage_note)
        library_storage_layout=QHBoxLayout()
        library_storage_root.addLayout(library_storage_layout,1)
        self.libraries_storage_nav=QListWidget()
        self.libraries_storage_nav.setAccessibleName("Libraries and storage tasks")
        self.libraries_storage_nav.setFixedWidth(190)
        self.libraries_storage_nav.setWordWrap(True)
        self.libraries_storage_nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.libraries_storage_nav.setSpacing(3)
        for label in ("Image libraries", "Image folders & backups", "Image History & Versions",
                      "Global Data History", "Resize version folders"):
            self.libraries_storage_nav.addItem(label)
        library_storage_layout.addWidget(self.libraries_storage_nav)
        self.libraries_storage_content=QStackedWidget()
        library_storage_layout.addWidget(self.libraries_storage_content,1)

        def storage_task_page(primary,*related,expand=True):
            page=QWidget()
            body=QVBoxLayout(page)
            body.setContentsMargins(8,12,8,12)
            body.setSpacing(10)
            body.addWidget(primary,1 if expand else 0)
            if related:
                actions=QHBoxLayout()
                actions.addStretch(1)
                for control in related:
                    actions.addWidget(control)
                body.addLayout(actions)
            if not expand:
                body.addStretch(1)
            return page

        self.libraries_storage_content.addWidget(storage_task_page(
            libraries,self.open_scan_scope_policy_button,self.open_evidence_intake_button,
            expand=False))
        self.libraries_storage_content.addWidget(storage_task_page(
            self.folder_locations.groups["app"],self.open_organization_policy_button,
            self.open_recycle_button))
        self.libraries_storage_content.addWidget(storage_task_page(
            self.folder_locations.groups["history"]))
        self.libraries_storage_content.addWidget(storage_task_page(
            self.folder_locations.groups["structured_history"]))
        resize_page=storage_task_page(self.folder_locations.groups["resize"])
        self.libraries_storage_content.addWidget(resize_page)
        self.libraries_storage_nav.currentRowChanged.connect(
            self.libraries_storage_content.setCurrentIndex)
        self.libraries_storage_nav.setCurrentRow(0)

        from .ai_runtime_controls import AIRuntimeMemoryPanel
        self.ai_memory=AIRuntimeMemoryPanel(settings,self.state,self)
        self.ai_memory.changed.connect(lambda:self._mark_dirty("ai"))
        analysis_memory_note=QLabel(
            "These memory preferences apply to local image analysis models. "
            "Generation and training use their own runtime and per-plan resource checks. "
            "Save AI Configuration saves analysis models, description settings and this memory policy."
        )
        analysis_memory_note.setWordWrap(True)
        self.model_locations_panel=QWidget()
        model_locations_layout=QVBoxLayout(self.model_locations_panel)
        model_locations_layout.setContentsMargins(0,0,0,0)
        locations_header=QHBoxLayout()
        locations_note=QLabel("Choose where new models go, prepare optional Fast copies, or set the separate analysis-model folder. Existing models can be indexed in place.")
        locations_note.setWordWrap(True)
        locations_header.addWidget(locations_note,1)
        locations_header.addWidget(self.models_workspace_button,alignment=Qt.AlignTop)
        model_locations_layout.addLayout(locations_header)
        self.models_workspace_button.setText("Browse or Index Models…")
        locations_work=QHBoxLayout()
        self.model_locations_nav=QListWidget()
        self.model_locations_nav.setAccessibleName("Model locations tasks")
        self.model_locations_nav.setFixedWidth(190)
        for label in ("New model folders","Fast storage","Analysis model folder"):
            self.model_locations_nav.addItem(label)
        locations_work.addWidget(self.model_locations_nav)
        self.model_locations_content=QStackedWidget()
        self.model_locations_content.addWidget(self.folder_locations.groups["models"])
        self.model_locations_content.addWidget(self.folder_locations.groups["analysis"])
        locations_work.addWidget(self.model_locations_content,1)
        model_locations_layout.addLayout(locations_work,1)
        self.folder_locations.model_section_picker.hide()
        def activate_model_location(row):
            if row < 0:
                return
            self.model_locations_content.setCurrentIndex(1 if row==2 else 0)
            if row < 2:
                self.folder_locations.select_model_section("folders" if row==0 else "fast")
        self.model_locations_nav.currentRowChanged.connect(activate_model_location)
        self.folder_locations.model_section_requested.connect(
            lambda section:self.model_locations_nav.setCurrentRow(0 if section=="folders" else 1)
        )
        self.model_locations_nav.setCurrentRow(0)
        self.description_tasks_panel=QWidget()
        description_tasks_layout=QVBoxLayout(self.description_tasks_panel)
        description_tasks_layout.setContentsMargins(0,0,0,0)
        description_tasks_note=QLabel(
            "Choose what to configure. Description style and model/device choices "
            "share Save AI Configuration; switching tasks does not save or discard edits."
        )
        description_tasks_note.setWordWrap(True)
        description_tasks_layout.addWidget(description_tasks_note)
        description_tasks_row=QHBoxLayout()
        description_tasks_layout.addLayout(description_tasks_row,1)
        self.description_tasks_nav=QListWidget()
        self.description_tasks_nav.setAccessibleName("Describe Images tasks")
        self.description_tasks_nav.setFixedWidth(190)
        for label in ("Description Style","Model & Device"):
            self.description_tasks_nav.addItem(label)
        description_tasks_row.addWidget(self.description_tasks_nav)
        self.description_tasks_content=QStackedWidget()
        self.description_tasks_content.addWidget(section_page(description_defaults))
        self.description_tasks_content.addWidget(section_page(joy_benchmark_panel))
        description_tasks_row.addWidget(self.description_tasks_content,1)
        self.description_tasks_nav.currentRowChanged.connect(
            self.description_tasks_content.setCurrentIndex
        )
        self.description_tasks_nav.setCurrentRow(0)
        generation_setup=QWidget(); generation_setup_layout=QVBoxLayout(generation_setup)
        generation_setup_layout.setContentsMargins(0,0,0,0)
        generation_note=QLabel(
            "Image generation connects to SwarmUI here. Video generation has separate "
            "setup in Generate. Setup changes do not submit a job."
        )
        generation_note.setWordWrap(True); generation_setup_layout.addWidget(generation_note)
        self.video_setup_button=compact_button(QPushButton("Open Video Setup…"))
        self.video_setup_button.setEnabled(self.state is not None)
        self.video_setup_button.setToolTip("Open a video draft at Local Setup; review and save its reusable runtime settings there.")
        self.video_setup_button.clicked.connect(self.open_video_setup)
        generation_setup_layout.addWidget(self.video_setup_button,alignment=Qt.AlignRight)
        generation_setup_layout.addWidget(self.swarm_runtime)
        generation_setup_layout.addWidget(self.generation_workspace_button,alignment=Qt.AlignRight)
        self.generation_workspace_button.setText("Open Generate")
        upscale_setup=QWidget(); upscale_setup_layout=QVBoxLayout(upscale_setup)
        upscale_setup_layout.setContentsMargins(0,0,0,0)
        self.resize_readiness.setText("Resize changes image dimensions without an AI model. AI upscale estimates detail and may alter it. Generative upscale can reinterpret content and requires an admitted generation method. Choose Resize or AI Upscale on an image to review its exact model, method and readiness.")
        upscale_setup_layout.addWidget(self.resize_readiness)
        self.upscale_models_button=compact_button(QPushButton("Manage Upscaler Models…"))
        self.upscale_models_button.setEnabled(self.state is not None)
        self.upscale_models_button.clicked.connect(self.open_upscaler_models)
        upscale_setup_layout.addWidget(self.upscale_models_button,alignment=Qt.AlignRight)
        self.ai_sections,ai_indexes=subsection_tabs(
            "AI tools and model setup",
            ("sdxl","Analyze Tags",(
                tag_model_panel,
            )),
            ("descriptions","Describe Images",self.description_tasks_panel),
            ("memory","Model Memory",(analysis_memory_note,self.ai_memory,)),
            ("generation","Generate && Remix",(generation_setup,)),
            ("upscale","Upscale && Restore",(upscale_setup,)),
            ("locations","Model Locations",self.model_locations_panel),
        )
        self.component_sections,component_indexes=subsection_tabs(
            "Components and downloads details",
            ("components","App && AI Components",(components,)),
            ("training","Trainer Software",(training,)),
        )
        automation_owner.setParent(self); automation_owner.hide()
        ai_root=QWidget(); ai_root_layout=QVBoxLayout(ai_root)
        ai_root_layout.setContentsMargins(0,0,0,0)
        ai_root_layout.addWidget(capability_overview)
        self.analysis_status_panel=capability_overview
        self.ai_sections.currentChanged.connect(lambda index:capability_overview.setVisible(
            index in {ai_indexes["sdxl"],ai_indexes["descriptions"],ai_indexes["memory"]}
        ))
        ai_root_layout.addWidget(self.ai_sections,1)
        ai_root_layout.addLayout(ai_actions)

        sections=QTabWidget(); self.sections=sections
        sections.setDocumentMode(True)
        sections.tabBar().hide()
        from .modules_page import ModulesPage
        self.modules_page=ModulesPage(provider=module_provider)
        section_specs=(
            ("getting_started","Getting Started",section_page(guided,defaults),
             ("guided checklist","first run","readiness","workflow defaults","preferences","presets")),
            ("libraries_storage","Libraries & Storage",
             self.libraries_storage_panel,
             ("folders","locations","scan scope","evidence intake","organization")),
            ("appearance_accessibility","Appearance & Accessibility",
             section_page(appearance,display),
             ("theme","tooltips","gallery","content protection","blur")),
            ("ai_models_runtime","AI Tools & Models",ai_root,
             ("analysis","tags","descriptions","generation","upscale","model locations","caption","gpu","memory","device")),
            ("components_downloads","Components & Downloads",
             self.component_sections,
             ("swarmui","local generation","application","metadata","models","trainer","offline transfer")),
            ("performance_quality","Performance & Quality",
             section_page(performance),
             ("benchmark","speed","compare","evaluation","bf16","nf4")),
            ("updates_startup","Updates & Startup",section_page(startup,updates),
             ("reopen libraries","component checks","release channel")),
            ("modules","Modules",self.modules_page,
             ("installed inventory","available releases","remote","dependencies","versions")),
        )
        self._section_indexes={}
        navigator=SettingsNavigator(accessible_name="Setup sections")
        self.navigator=navigator
        self.settings_navigation=navigator
        navigator.set_content(sections)
        for key,label,page,keywords in section_specs:
            index=sections.addTab(page,label)
            self._section_indexes[key]=index
            navigator.add_section(
                key,label,lambda value=index:sections.setCurrentIndex(value),
                keywords=keywords,
            )
        self._subsection_indexes={"ai":ai_indexes,"components":component_indexes}
        self._setup_section_keys={
            index:key for key,index in self._section_indexes.items()
        }
        sections.currentChanged.connect(
            lambda index:navigator.sync_section(
                self._setup_section_keys.get(index,"")
            )
        )
        sections.currentChanged.connect(self._stage_joy_recommendation_on_ai_entry)

        for key,label,control,keywords,activate in (
            ("getting_started","Guided setup checklist",self.guided_setup_button,
             ("first run","readiness","resume"),None),
            ("libraries_storage","Named image libraries",self.library_table,
             ("default library","collection","folder"),lambda:self.libraries_storage_nav.setCurrentRow(0)),
            ("libraries_storage","Incoming images folder",self.storage_fields["ingest_directory"],
             ("storage","path"),lambda:self.libraries_storage_nav.setCurrentRow(1)),
            ("libraries_storage","History & Versions",self.folder_locations.groups["history"],
             ("saved data","private history","history folder","versions"),lambda:self.libraries_storage_nav.setCurrentRow(2)),
            ("libraries_storage","Global Data History",self.folder_locations.groups["structured_history"],
             ("collections","Allowed Details","global history","private storage"),lambda:self.libraries_storage_nav.setCurrentRow(3)),
            ("libraries_storage","Problems folder",self.storage_fields["problems_directory"],
             ("storage","path"),lambda:self.libraries_storage_nav.setCurrentRow(1)),
            ("libraries_storage","Recycle Bin folder",self.storage_fields["recycle_directory"],
             ("storage","recovery","path"),lambda:self.libraries_storage_nav.setCurrentRow(1)),
            ("ai_models_runtime","Analysis model files folder",self.storage_fields["ai_models_directory"],
             ("storage","models","path"),lambda:(self.ai_sections.setCurrentIndex(ai_indexes["locations"]),self.model_locations_nav.setCurrentRow(2))),
            ("libraries_storage","Library scan scope policy",self.open_scan_scope_policy_button,
             ("subfolders","automation"),lambda:self.libraries_storage_nav.setCurrentRow(0)),
            ("libraries_storage","Changed TXT and Metadata intake",self.open_evidence_intake_button,
             ("external evidence","automation"),lambda:self.libraries_storage_nav.setCurrentRow(0)),
            ("libraries_storage","Folder organization policy",self.open_organization_policy_button,
             ("routes","filenames","automation"),lambda:self.libraries_storage_nav.setCurrentRow(1)),
            ("appearance_accessibility","Color theme",self.theme,("appearance",),None),
            ("appearance_accessibility","Color-blind-friendly status colors",self.color_blind,
             ("accessibility","patterns"),None),
            ("appearance_accessibility","Contextual help tooltips",self.show_tooltips,
             ("accessibility","help"),None),
            ("appearance_accessibility","Gallery page size",self.gallery_size,
             ("images","paging"),None),
            ("appearance_accessibility","Default unblurred range",self.content_level,
             ("content protection","rating"),None),
            ("appearance_accessibility","Remember intentional image reveals",self.remember_reveals,
             ("content protection","preview"),None),
            ("appearance_accessibility","Blur images without a dependable rating",self.blur_unknown,
             ("content protection","unknown","preview"),None),
            ("appearance_accessibility","Always blur General images",self.hidden_ratings["general"],
             ("content protection","rating"),None),
            ("appearance_accessibility","Always blur Sensitive images",self.hidden_ratings["sensitive"],
             ("content protection","rating"),None),
            ("appearance_accessibility","Always blur Questionable images",self.hidden_ratings["questionable"],
             ("content protection","rating"),None),
            ("appearance_accessibility","Always blur Explicit images",self.hidden_ratings["explicit"],
             ("content protection","rating"),None),
            ("ai_models_runtime","AI Tag models",self.tag_model_panel,
             ("wd","models","runtime"),lambda:self.ai_sections.setCurrentIndex(ai_indexes["sdxl"])),
            ("ai_models_runtime","AI memory policy",self.ai_memory.mode,
             ("keep warm","preload","on demand","model loading","memory"),lambda:self.ai_sections.setCurrentIndex(ai_indexes["memory"])),
            ("ai_models_runtime","Free AI Memory",self.ai_memory.free,
             ("release","vram","loaded models"),lambda:self.ai_sections.setCurrentIndex(ai_indexes["memory"])),
            ("ai_models_runtime","AI Tag execution device",self.wd_provider,
             ("wd","runtime","cpu","gpu"),lambda:self.ai_sections.setCurrentIndex(ai_indexes["sdxl"])),
            ("ai_models_runtime","AI Tag images per model call",self.wd_batch_size,
             ("wd","runtime","batch size"),lambda:self.ai_sections.setCurrentIndex(ai_indexes["sdxl"])),
            ("ai_models_runtime","AI Description caption model",self.plain_language_model,
             ("joycaption","florence","model"),lambda:self._select_description_task("model")),
            ("ai_models_runtime","AI Description model memory mode",self.joy_quantization,
             ("bf16","nf4","vram","runtime"),lambda:self._select_description_task("model")),
            ("ai_models_runtime","AI Description GPU number",self.joy_gpu,
             ("device","hardware","runtime"),lambda:self._select_description_task("model")),
            ("ai_models_runtime","AI Description instructions",self.joy_prompt,
             ("prompt","generation"),lambda:self._select_description_task("style")),
            ("ai_models_runtime","Maximum generated tokens",self.joy_tokens,
             ("description","length","generation"),lambda:self._select_description_task("style")),
            ("ai_models_runtime","Minimum generated tokens",self.joy_min_tokens,
             ("description","advanced","length"),lambda:self._select_description_task("style")),
            ("ai_models_runtime","Description beam count",self.joy_beams,
             ("advanced","generation"),lambda:self._select_description_task("style")),
            ("ai_models_runtime","Description length preference",self.joy_length_penalty,
             ("advanced","generation","beam search"),lambda:self._select_description_task("style")),
            ("ai_models_runtime","Description sampling",self.joy_sample,
             ("advanced","random wording"),lambda:self._select_description_task("style")),
            ("ai_models_runtime","Description temperature",self.joy_temperature,
             ("advanced","sampling"),lambda:self._select_description_task("style")),
            ("ai_models_runtime","Description Top-K",self.joy_top_k,
             ("advanced","sampling"),lambda:self._select_description_task("style")),
            ("ai_models_runtime","Description Top-P",self.joy_top_p,
             ("advanced","sampling"),lambda:self._select_description_task("style")),
            ("ai_models_runtime","Description repetition penalty",self.joy_repetition,
             ("advanced","generation"),lambda:self._select_description_task("style")),
            ("ai_models_runtime","Description no-repeat phrase size",self.joy_ngram,
             ("advanced","generation"),lambda:self._select_description_task("style")),
            ("components_downloads","Application and Metadata components",self.dependency_panel.capabilities_panel,
             ("repair","exiftool"),lambda:(self.component_sections.setCurrentIndex(component_indexes["components"]),self.dependency_panel.select_task("install"))),
            ("ai_models_runtime","Local Generation with SwarmUI",self.swarm_runtime,
             ("generation","connect","existing install","install","endpoint","update","repair"),lambda:(self.ai_sections.setCurrentIndex(ai_indexes["generation"]),self.swarm_runtime.select_default_task())),
            ("ai_models_runtime","Use Existing SwarmUI",self.swarm_runtime.use_existing_button,
             ("adopt","connect","installation"),lambda:(self.ai_sections.setCurrentIndex(ai_indexes["generation"]),self.swarm_runtime.select_task("connect"))),
            ("ai_models_runtime","Install SwarmUI for Me",self.swarm_runtime.install_button,
             ("deploy","fresh","managed","download"),lambda:(self.ai_sections.setCurrentIndex(ai_indexes["generation"]),self.swarm_runtime.select_task("connect"))),
            ("ai_models_runtime","Check SwarmUI connection",self.swarm_runtime.health_button,
             ("health","reconnect","offline"),lambda:(self.ai_sections.setCurrentIndex(ai_indexes["generation"]),self.swarm_runtime.select_task("run"))),
            ("ai_models_runtime","Review SwarmUI Update",self.swarm_runtime.update_button,
             ("maintenance","version"),lambda:(self.ai_sections.setCurrentIndex(ai_indexes["generation"]),self.swarm_runtime.select_task("maintenance"))),
            ("components_downloads","Repair application components",self.dependency_panel.app_button,
             ("core services","libraries","metadata"),lambda:(self.component_sections.setCurrentIndex(component_indexes["components"]),self.dependency_panel.select_task("install"))),
            ("components_downloads","Set up Metadata support",self.dependency_panel.exif_button,
             ("exiftool","embedded metadata","repair"),lambda:self.dependency_panel.select_task("install")),
            ("components_downloads","Manage AI Tag models",self.dependency_panel.wd_manage_button,
             ("wd","install","remove"),lambda:self.dependency_panel.select_task("install")),
            ("components_downloads","Manage AI Description models",self.dependency_panel.joy_manage_button,
             ("caption","joycaption","florence","install","remove"),lambda:self.dependency_panel.select_task("install")),
            ("components_downloads","Trainer software",self.training_setup_button,
             ("local training","install","repair"),lambda:self.component_sections.setCurrentIndex(component_indexes["training"])),
            ("components_downloads","Import offline model pack",self.dependency_panel.import_pack_button,
             ("transfer","models"),lambda:self.dependency_panel.select_task("offline")),
            ("components_downloads","Export installed model",self.dependency_panel.export_pack_button,
             ("offline transfer","model pack"),lambda:self.dependency_panel.select_task("offline")),
            ("components_downloads","Choose local ExifTool",self.dependency_panel.local_exiftool_button,
             ("offline transfer","metadata","executable"),lambda:self.dependency_panel.select_task("offline")),
            ("components_downloads","Import offline AI components",self.dependency_panel.import_components_button,
             ("transfer","python environment"),lambda:self.dependency_panel.select_task("offline")),
            ("components_downloads","Export installed AI components",self.dependency_panel.export_components_button,
             ("offline transfer","python environment"),lambda:self.dependency_panel.select_task("offline")),
            ("components_downloads","Technical component inventory",self.dependency_panel.table,
             ("versions","installed paths","component status"),lambda:self.dependency_panel.select_task("inventory")),
            ("performance_quality","AI Tag model benchmark",self.wd_benchmark_button,
             ("speed","hardware"),None),
            ("performance_quality","AI Description speed benchmark",self.joy_benchmark_button,
             ("caption","hardware"),None),
            ("performance_quality","Caption quality comparison",self.joy_quality_button,
             ("bf16","nf4"),None),
            ("performance_quality","Description length comparison",self.joy_length_quality_button,
             ("tokens","quality"),None),
            ("performance_quality","Installed local AI evaluation",self.ai_evaluation_button,
             ("quality","models"),None),
            ("updates_startup","Reopen libraries at startup",self.restore_last,
             ("launch","default"),None),
            ("updates_startup","Repair missing core components at startup",self.check_dependencies,
             ("launch","application"),None),
            ("updates_startup","Application update channel",self.update_channel,
             ("stable","prerelease"),None),
            ("updates_startup","Check for updates at startup",self.check_updates_on_startup,
             ("launch","release"),None),
        ):
            navigator.add_target(
                key,label,control,keywords=keywords,activate=activate,
            )
        navigator.select_section("getting_started")
        for key,button in self.workflow_default_buttons.items():
            navigator.add_target("getting_started",button.text().removesuffix("…"),button,
                keywords=("workflow",key,"defaults","preferences","presets"))
        navigator.add_target("ai_models_runtime","Local video setup",self.video_setup_button,
            keywords=("video","comfyui","runtime","connection","defaults"),
            activate=lambda:self.ai_sections.setCurrentIndex(ai_indexes["generation"]))
        outer.addWidget(navigator,1)

        for control in (
            self.restore_last,self.check_dependencies,self.show_tooltips,
            self.remember_reveals,self.blur_unknown,*self.hidden_ratings.values(),
        ):
            control.toggled.connect(lambda _checked:self.save_general())
        for control in (self.gallery_size,self.content_level):
            control.currentIndexChanged.connect(lambda _index:self.save_general())
        self.update_channel.currentIndexChanged.connect(self._save_update_channel)
        self.check_updates_on_startup.toggled.connect(self._save_update_startup_preference)
        from core.folder_locations import APP_FOLDERS, RESIZE_FOLDERS
        for key,label in (*APP_FOLDERS[4:], *RESIZE_FOLDERS):
            navigator.add_target("libraries_storage", label, self.folder_locations.fields[key],
                                 keywords=("folder", "location", "storage", "backup", "resize"),
                                 activate=lambda row=4 if key in dict(RESIZE_FOLDERS) else 1:
                                     self.libraries_storage_nav.setCurrentRow(row))
        for label,control,row in (("Acquired model folder",self.folder_locations.fields["default_destination"],0),
                                  ("Model role and family folder overrides",self.folder_locations.overrides,0),
                                  ("Fast storage folder and capacity",self.folder_locations.storage_policy.fast_root,1),
                                  ("Fast storage advanced policy",self.folder_locations.storage_policy.advanced,1),
                                  ("Browse or index existing models",self.models_workspace_button,0)):
            navigator.add_target("ai_models_runtime",label,control,
                keywords=("models","lora","vae","location","storage"),
                activate=lambda selected=row:(self.ai_sections.setCurrentIndex(ai_indexes["locations"]),self.model_locations_nav.setCurrentRow(selected)))
        navigator.add_target("ai_models_runtime","Upscaler models",self.upscale_models_button,
            keywords=("resize","restore","upscale"),
            activate=lambda:self.ai_sections.setCurrentIndex(ai_indexes["upscale"]))
        for control in (
            self.auto_merge_ai,self.prepare_confirm_tags,
            self.prepare_descriptions,self.joy_sample,*self.wd_taggers.values(),
        ):
            control.toggled.connect(lambda _checked:self._mark_dirty("ai"))
        for control in self.wd_taggers.values():
            control.toggled.connect(lambda _checked:self._refresh_auto_apply_availability())
        for control in (
            self.wd_general,self.wd_character,self.wd_rating,self.joy_tokens,self.wd_consensus,self.wd_provider,
            self.wd_batch_size,self.joy_min_tokens,self.joy_beams,self.joy_length_penalty,
            self.joy_temperature,self.joy_top_k,self.joy_top_p,self.joy_repetition,
            self.joy_ngram,self.joy_gpu,
        ):
            signal=control.currentIndexChanged if isinstance(control,QComboBox) else control.valueChanged
            signal.connect(lambda _value:self._mark_dirty("ai"))
        self.joy_gpu.valueChanged.connect(
            lambda _value:self._refresh_joy_memory_recommendation()
        )
        self.joy_quantization.currentIndexChanged.connect(
            self._joy_quantization_changed
        )
        self.plain_language_model.currentIndexChanged.connect(
            self._plain_language_model_changed
        )
        self.prepare_confidence.currentIndexChanged.connect(
            self._prepare_confidence_preset_changed
        )
        self.prepare_votes.valueChanged.connect(
            self._prepare_confidence_values_changed
        )
        self.prepare_external_vote.toggled.connect(
            self._prepare_confidence_values_changed
        )
        self.prepare_confirm_tags.toggled.connect(
            self._update_prepare_confidence
        )
        self.use_wd.toggled.connect(self._update_prepare_confidence)
        self.joy_prompt.textChanged.connect(lambda:self._mark_dirty("ai"))
        self.description_preset.currentIndexChanged.connect(
            self._preview_description_preset
        )
        self.apply_description_preset_button.clicked.connect(
            self._apply_description_preset
        )
        self.joy_prompt.textChanged.connect(self._preview_description_preset)
        self.joy_tokens.valueChanged.connect(self._preview_description_preset)
        self.automation_mode.currentIndexChanged.connect(
            self._preview_automation_preset
        )
        self.apply_automation_preset_button.clicked.connect(
            self._apply_automation_preset
        )
        for control in (
            self.use_wd,self.use_joy,self.auto_run_wd,self.auto_run_joy,
            self.auto_merge_ai,self.prepare_confirm_tags,
            self.prepare_descriptions,*self.wd_taggers.values(),
        ):
            control.toggled.connect(lambda _checked:self._automation_controls_changed())
        self.wd_consensus.valueChanged.connect(lambda _value:self._automation_controls_changed())
        self._refresh_auto_apply_availability(); self._update_prepare_confidence()
        self._update_automation_example(); self._arrange_ai_workflows(self.width())
        self._plain_language_model_changed(
            self.plain_language_model.currentIndex()
        )
        committed=self._ai_control_values()
        committed["joycaption_quantization"]=str(
            getattr(settings,"joycaption_quantization","bf16")
        )
        self._committed_section_values["ai"]=committed
        self._automation_behavior_values={
            key:getattr(settings,key) for key in AUTOMATION_BEHAVIOR_SETTING_KEYS
        }
        if committed!=self._ai_control_values():
            self._mark_dirty("ai")
        self._update_automation_example()
        self.overview_tag_status.setText("AI Tags: checking installed models in the background…")
        self.overview_description_status.setText(
            "AI Descriptions: checking the selected model and installed runtime in the background…"
        )
        # The real application owns a shared job coordinator which also drains
        # this work during shutdown.  Lightweight/standalone SetupPage users
        # (including deterministic UI tests) retain the provisional display and
        # may request an inventory explicitly instead of leaking a pool task.
        if self.state is not None and getattr(self.state,"jobs",None) is not None:
            QTimer.singleShot(0,self._start_component_inventory)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        self._arrange_ai_workflows(event.size().width())

    def _start_component_inventory(self):
        if self._component_inventory_task is not None:
            return
        self.header.set_status("Checking local components in the background…")

        def inspect(_job=None):
            service=self.dependency_service()
            service.components(include_sizes=False)
            pack_available=service.component_pack_available()
            profile=nvidia_gpu_profile(self.settings.joycaption_gpu_index)
            return service,profile,pack_available

        if self.state is not None and getattr(self.state,"jobs",None) is not None:
            task=CoordinatedCallTask(
                self.state.jobs,"Checking local components",inspect,
                resource="io",key="setup-component-inventory",
            )
        else:
            task=AsyncCallTask(inspect)
        self._component_inventory_task=task
        task.signals.complete.connect(self._component_inventory_complete)
        task.signals.failed.connect(self._component_inventory_failed)
        if isinstance(task,CoordinatedCallTask):
            task.start()
        else:
            self.pool.start(task)

    def _component_inventory_complete(self,result):
        self._component_inventory_task=None
        service,profile,pack_available=result
        ai_was_dirty="ai" in self._dirty_sections
        self._component_inventory_ready=True
        self._joy_gpu_profile=profile
        self.hardware_status_label.setText(
            f"Hardware: {profile.name} • {profile.total_memory_gib:.1f} GiB VRAM"
            if profile is not None else "Hardware: CPU available; no supported NVIDIA GPU detected"
        )
        previous_suppression=self._suppress_dirty; self._suppress_dirty=True
        try:
            self.dependency_panel.service=service
            self.dependency_panel.refresh(component_pack_available=pack_available)
        finally:
            self._suppress_dirty=previous_suppression
        if not ai_was_dirty:
            # Capability discovery may disable an unavailable model control, but
            # that is a runtime presentation update rather than a user edit.
            self._committed_section_values["ai"]=self._ai_control_values()
            self._clear_dirty("ai")
        if self.sections.currentIndex()==self._section_indexes["ai_models_runtime"]:
            self._refresh_joy_memory_recommendation(service)
        self._update_automation_example()
        self.header.set_status("Local component check complete")

    def _component_inventory_failed(self,message):
        self._component_inventory_task=None
        self.hardware_status_label.setText("Hardware: component check needs attention")
        self.dependency_panel.status.setText(
            f"Component inventory could not be completed: {message}"
        )
        self.header.set_status("Local component check needs attention")

    def _arrange_ai_workflows(self,width: int) -> None:
        mode="stacked" if width<950 else "wide"
        if getattr(self,"_ai_workflow_layout_mode","")==mode:
            return
        self._ai_workflow_layout_mode=mode
        specifications=(
            (self.tag_workflow_layout,self.tag_generation,self.tag_changes),
            (
                self.description_workflow_layout,
                self.description_generation,self.description_changes,
            ),
        )
        for grid,first,second in specifications:
            grid.removeWidget(first); grid.removeWidget(second)
            grid.setColumnStretch(0,0); grid.setColumnStretch(1,0)
            if mode=="stacked":
                grid.addWidget(first,0,0); grid.addWidget(second,1,0)
                grid.setColumnStretch(0,1)
            else:
                grid.addWidget(first,0,0); grid.addWidget(second,0,1)
                grid.setColumnStretch(0,1); grid.setColumnStretch(1,2)

    def set_image_setup_return_available(self,available: bool) -> None:
        self.image_setup_return_panel.setVisible(bool(available))

    def set_automation_return_available(self,available: bool) -> None:
        self.automation_return_panel.setVisible(bool(available))

    def set_preparation_return_available(self,available: bool) -> None:
        self.preparation_return_panel.setVisible(bool(available))
        if available:
            self.set_preparation_return_state()
        if not available:
            self.dependency_panel.clear_embedded_focus()

    def set_preparation_return_state(self,status: str="",detail: str="") -> None:
        panel=self.preparation_return_panel
        panel.status_label.setText(status or "Return available")
        panel.detail_label.setText(detail or self._preparation_return_detail)

    def _select_description_task(self,task: str):
        self.ai_sections.setCurrentIndex(self._subsection_indexes["ai"]["descriptions"])
        self.description_tasks_nav.setCurrentRow(1 if task=="model" else 0)

    def show_section(self,section: str,subsection: str=""):
        """Navigate by stable Setup section names instead of fragile tab indexes."""
        if (section in {"components","components_downloads","ai","ai_models_runtime"}
                and subsection in {"local_generation","swarmui","generation","locations","upscale","memory"}):
            detail="generation" if subsection in {"local_generation","swarmui"} else subsection
            self.navigator.select_section("ai_models_runtime")
            self.ai_sections.setCurrentIndex(self._subsection_indexes["ai"][detail])
            return
        if section=="support_diagnostics" or (section,subsection)==("maintenance","metadata"):
            controller=getattr(self.window(),"command_controller",None)
            if controller is not None:controller.reveal_group(6,"Help")
            return
        if section=="maintenance" and subsection=="knowledge":
            self.open_automation.emit("knowledge_packs",None)
            return
        legacy_routes={
            ("general",""):"getting_started",
            ("general","startup"):"updates_startup",
            ("general","appearance"):"appearance_accessibility",
            ("general","scanning"):"libraries_storage",
            ("library",""):"libraries_storage",
            ("library","locations"):"libraries_storage",
            ("library","organization"):"libraries_storage",
            ("ai",""):"ai_models_runtime",
            ("ai","overview"):"ai_models_runtime",
            ("ai","sdxl"):"ai_models_runtime",
            ("ai","descriptions"):"ai_models_runtime",
            ("ai","description_model"):"ai_models_runtime",
            ("ai","components"):"components_downloads",
            ("maintenance",""):"updates_startup",
            ("maintenance","updates"):"updates_startup",
        }
        destination=(
            section if section in self._section_indexes
            else legacy_routes.get((section,subsection))
        )
        if not destination or not self.navigator.select_section(destination):
            return
        if section=="ai" and subsection in {"overview","sdxl","descriptions","description_model"}:
            detail="sdxl" if subsection=="overview" else subsection
            if detail in {"descriptions","description_model"}:
                self._select_description_task("model" if detail=="description_model" else "style")
            else:
                self.ai_sections.setCurrentIndex(self._subsection_indexes["ai"][detail])
        if destination=="components_downloads" and subsection in {
            "components","local_generation","training","swarmui",
        }:
            detail="local_generation" if subsection=="swarmui" else subsection
            self.component_sections.setCurrentIndex(
                self._subsection_indexes["components"][detail]
            )

    def _stage_joy_recommendation_on_ai_entry(self,index: int) -> None:
        if (
            index==self._section_indexes["ai_models_runtime"]
            and self._component_inventory_ready
        ):
            self._refresh_joy_memory_recommendation(self.dependency_panel.service)

    def refresh_from_settings(self):
        """Reflect guided-setup writes without creating a second settings model."""
        controls=(
            (self.restore_last,self.settings.restore_last_dataset),
            (self.check_dependencies,self.settings.check_dependencies_on_startup),
            (self.check_updates_on_startup,self.settings.check_updates_on_startup),
            (self.show_tooltips,self.settings.show_tooltips),
            (self.auto_import_source,self.settings.auto_import_source_evidence),
            (self.remember_reveals,self.settings.remember_content_reveals),
            (self.blur_unknown,getattr(self.settings,"gallery_blur_unknown",True)),
            (self.use_wd,self.settings.suggest_wd_for_missing_tags),
            (self.use_joy,self.settings.suggest_joycaption_for_missing_descriptions),
            (self.auto_run_wd,self.settings.auto_run_wd_on_managed_images),
            (self.auto_run_joy,self.settings.auto_run_joy_on_managed_images),
        )
        for control,checked in controls:
            blocker=QSignalBlocker(control); control.setChecked(bool(checked)); del blocker
        self.library_registry=self.settings.library_registry()
        self._refresh_library_profiles(self.settings.active_library_id)
        self.folder_locations.reload()
        for rating,control in self.hidden_ratings.items():
            blocker=QSignalBlocker(control); control.setChecked(rating in self.settings.gallery_hidden_ratings); del blocker
        for combo,value in (
            (self.gallery_size,self.settings.gallery_page_size),
            (self.content_level,self.settings.gallery_content_level),
            (self.update_channel,self.settings.update_channel),
        ):
            blocker=QSignalBlocker(combo); combo.setCurrentIndex(max(0,combo.findData(value))); del blocker
        self._update_content_safety_note()
        self._sync_ai_controls(); self.header.set_status("Guided setup choices loaded")

    def _mark_dirty(self,section: str):
        if self._suppress_dirty:
            return
        if (
            section=="ai"
            and self._committed_section_values.get("ai")==self._ai_control_values()
            and not self._joy_quantization_user_selected
        ):
            self._clear_dirty(section)
            return
        self._dirty_sections.add(section)
        label=getattr(self,f"{section}_dirty",None)
        button=getattr(self,f"save_{section}_button",None)
        if label is not None:
            label.setText({
                "storage":"Unsaved storage changes",
                "ai":"Unsaved AI configuration changes",
                "organization":"Unsaved folder routing changes",
            }.get(section,"Unsaved changes")); label.show()
        if button is not None:
            button.setEnabled(True); button.show()

    @staticmethod
    def _nf4_component_ready(service: DependencyService) -> bool:
        component=service.component("BitsAndBytes")
        return bool(component and component.installed)

    def _refresh_joy_memory_recommendation(
        self,service: DependencyService | None = None,*,stage: bool = True,
    ):
        if not hasattr(self,"joy_quantization"):
            return
        service=service or self.dependency_service()
        self._joy_gpu_profile=nvidia_gpu_profile(self.joy_gpu.value())
        self._joy_nf4_ready=self._nf4_component_ready(service)
        recommendation=recommended_joycaption_quantization(
            self.settings,
            nf4_ready=self._joy_nf4_ready,
            gpu_profile=self._joy_gpu_profile,
        )
        self._joy_recommended_quantization=recommendation
        if (
            stage
            and not self.settings.joycaption_quantization_explicit
            and not self._joy_quantization_user_selected
        ):
            blocker=QSignalBlocker(self.joy_quantization)
            self.joy_quantization.setCurrentIndex(max(
                0,self.joy_quantization.findData(recommendation),
            ))
            del blocker
            self._joy_previous_quantization=str(
                self.joy_quantization.currentData()
            )
            if "ai" in self._committed_section_values:
                self._mark_dirty("ai")
        self._update_joy_memory_guidance()

    def _update_joy_memory_guidance(self):
        if not hasattr(self,"joy_memory_guidance"):
            return
        selected=str(self.plain_language_model.currentData())
        if selected==FLORENCE2_MODEL:
            self.joy_memory_guidance.setText(
                "Florence-2 uses its fixed detailed-caption task and Full BF16. "
                "Custom JoyCaption instructions and memory-mode comparisons do "
                "not apply; generated descriptions remain review findings."
            )
            self.joy_memory_guidance.setAccessibleDescription(
                self.joy_memory_guidance.text()
            )
            return
        profile=self._joy_gpu_profile
        mode=str(self.joy_quantization.currentData())
        if profile is None:
            if self.settings.joycaption_quantization_explicit and mode=="nf4":
                text=(
                    "No dependable NVIDIA total-memory reading is available. "
                    "Your explicit 4-bit NF4 choice is preserved, but NF4 still "
                    "requires its reviewed component and supported NVIDIA "
                    "hardware. Full BF16 remains the recoverable CPU path."
                )
            else:
                text=(
                    "No dependable NVIDIA total-memory reading is available. "
                    "Full BF16 remains the safe compatibility default, including "
                    "the recoverable CPU path."
                )
        elif profile.total_memory_mib>=JOYCAPTION_BF16_RECOMMENDED_VRAM_MIB:
            text=(
                f"{profile.name} reports {profile.total_memory_gib:.1f} GiB of "
                "total dedicated memory. Full BF16 is the recommended starting "
                "mode for this 24-GB-class-or-larger GPU."
            )
            if self.settings.joycaption_quantization_explicit and mode=="nf4":
                text+=" Your explicit 4-bit NF4 choice is preserved."
        elif self._joy_nf4_ready:
            if self.settings.joycaption_quantization_explicit:
                saved=" Your explicit saved choice is preserved."
            elif mode!=self.settings.joycaption_quantization:
                saved=" This recommendation is pending; Save AI Configuration to use it."
            elif mode=="bf16":
                saved=" Full BF16 remains saved until you review and save a choice."
            else:
                saved=""
            text=(
                f"{profile.name} reports {profile.total_memory_gib:.1f} GiB of "
                "total dedicated memory. 4-bit NF4 is recommended below 24 GiB "
                "to reduce model memory; caption quality may differ."
                f"{saved}"
            )
        else:
            if self.settings.joycaption_quantization_explicit and mode=="nf4":
                text=(
                    f"{profile.name} reports {profile.total_memory_gib:.1f} GiB "
                    "of total dedicated memory. Your explicit 4-bit NF4 choice "
                    "is preserved, but its reviewed component is not ready. "
                    f"<a href=\"components\">Repair 4-bit mode in "
                    "Components…</a>"
                )
            else:
                text=(
                    f"{profile.name} reports {profile.total_memory_gib:.1f} GiB "
                    "of total dedicated memory, so 4-bit NF4 is recommended. Its "
                    "reviewed component is not ready; CollectiGen kept Full BF16 "
                    f"and installed nothing. <a href=\"components\">Set up 4-bit "
                    "mode in Components…</a>"
                )
        if mode=="bf16" and profile is not None and (
            profile.total_memory_mib<JOYCAPTION_BF16_RECOMMENDED_VRAM_MIB
        ):
            text+=" Full BF16 may be substantially slower or may not fit."
        self.joy_memory_guidance.setText(text)
        self.joy_memory_guidance.setAccessibleDescription(
            self.joy_memory_guidance.text().replace(
                '<a href="components">',""
            ).replace("</a>","")
        )

    def _preview_description_preset(self,_value=None):
        preset_id=str(self.description_preset.currentData() or "")
        preset=DESCRIPTION_PRESETS.get(preset_id)
        if not preset:
            text=(
                "Choose a preset to preview its instructions and length. "
                "Nothing changes until you apply it."
            )
            self.description_preset_preview.setText(text)
            self.description_preset_preview.setAccessibleDescription(text)
            self.apply_description_preset_button.setEnabled(False)
            return
        target_prompt=str(preset["instructions"])
        target_tokens=int(preset["max_new_tokens"])
        matches=(
            self.joy_prompt.toPlainText().strip()==target_prompt
            and self.joy_tokens.value()==target_tokens
        )
        if str(self.plain_language_model.currentData())!=JOYCAPTION_MODEL:
            text=(
                "Description presets apply to JoyCaption custom instructions. "
                "Florence-2 uses its fixed caption task, so nothing will change."
            )
        elif matches:
            text=(
                "This preset already matches the pending Instructions and Maximum "
                "Generated Tokens. Advanced generation controls remain unchanged."
            )
        else:
            text=(
                f"Instructions will be replaced with: {target_prompt}\n"
                f"Maximum Generated Tokens will be set to {target_tokens}. "
                "Advanced generation controls will not change. Nothing is saved "
                "until Save AI Configuration."
            )
        self.description_preset_preview.setText(text)
        self.description_preset_preview.setAccessibleDescription(text)
        self.apply_description_preset_button.setEnabled(
            str(self.plain_language_model.currentData())==JOYCAPTION_MODEL
            and not matches
        )

    def _apply_description_preset(self):
        preset_id=str(self.description_preset.currentData() or "")
        preset=DESCRIPTION_PRESETS.get(preset_id)
        if (
            not preset
            or str(self.plain_language_model.currentData())!=JOYCAPTION_MODEL
        ):
            return
        self.joy_prompt.setPlainText(str(preset["instructions"]))
        self.joy_tokens.setValue(int(preset["max_new_tokens"]))
        self._mark_dirty("ai")
        self._preview_description_preset()
        self.header.set_status(
            f"{preset['label']} staged; review and Save AI Configuration"
        )

    def _plain_language_model_changed(self,_index):
        selected=str(self.plain_language_model.currentData())
        is_joy=selected==JOYCAPTION_MODEL
        summary=PLAIN_LANGUAGE_MODELS.get(selected,(selected,"",""))[2]
        detail=(
            summary+(
                " Custom instructions and BF16/NF4 controls below apply."
                if is_joy else
                " It uses the fixed More Detailed Caption task and does not "
                "execute model-supplied Python code."
            )
        )
        self.plain_language_model_note.setText(
            "Detailed, instruction-guided AI Descriptions"
            if is_joy else "Fast, single-pass AI Descriptions"
        )
        self.plain_language_model_note.setToolTip(detail)
        self.plain_language_model_note.setAccessibleDescription(detail)
        self.plain_language_model.setToolTip(detail)
        self.plain_language_model.setAccessibleDescription(detail)
        self.joy_prompt.setEnabled(is_joy)
        self.description_preset.setEnabled(is_joy)
        self.joy_quantization.setEnabled(is_joy)
        self.joy_quality_button.setVisible(is_joy)
        self.joy_length_quality_button.setVisible(is_joy)
        self._preview_description_preset()
        self._update_joy_memory_guidance()
        if (
            not self._suppress_dirty
            and "ai" in self._committed_section_values
        ):
            self._mark_dirty("ai")
        if (
            hasattr(self,"overview_description_status")
            and self._component_inventory_ready
        ):
            self._update_ai_overview_readiness()

    @staticmethod
    def _bf16_warning_text(profile) -> str:
        return (
            f"{profile.name} reports {profile.total_memory_gib:.1f} GiB of total "
            "dedicated GPU memory.\n\nJoyCaption's full model alone needs roughly "
            "17 GB of GPU memory, and upstream recommends a 24-GB-class GPU. "
            "On this hardware, Full BF16 may be substantially slower or may not "
            "fit. 4-bit NF4 is the recommended memory mode below 24 GiB, though "
            "caption quality may differ.\n\nUse Full BF16 anyway?"
        )

    def _joy_quantization_changed(self,_index):
        mode=str(self.joy_quantization.currentData())
        if self._suppress_dirty:
            self._joy_previous_quantization=mode
            self._update_joy_memory_guidance()
            return
        profile=nvidia_gpu_profile(self.joy_gpu.value())
        self._joy_gpu_profile=profile
        if (
            mode=="bf16"
            and profile is not None
            and profile.total_memory_mib<JOYCAPTION_BF16_RECOMMENDED_VRAM_MIB
            and QMessageBox.question(
                self,
                "Full BF16 may not fit this GPU",
                self._bf16_warning_text(profile),
                QMessageBox.Yes|QMessageBox.No,
                QMessageBox.No,
            )!=QMessageBox.Yes
        ):
            blocker=QSignalBlocker(self.joy_quantization)
            self.joy_quantization.setCurrentIndex(max(
                0,self.joy_quantization.findData(
                    self._joy_previous_quantization
                ),
            ))
            del blocker
            self._update_joy_memory_guidance()
            return
        self._joy_previous_quantization=mode
        self._joy_quantization_user_selected=True
        self._update_joy_memory_guidance()
        self._mark_dirty("ai")

    def _clear_dirty(self,section: str):
        self._dirty_sections.discard(section)
        label=getattr(self,f"{section}_dirty",None)
        button=getattr(self,f"save_{section}_button",None)
        if label is not None:
            label.hide()
        if button is not None:
            button.setEnabled(False); button.hide()

    def has_unsaved_changes(self) -> bool:
        return bool(self._dirty_sections) or bool(self.folder_locations.dirty_groups())

    def _ai_control_values(self) -> dict:
        return {
            **self.ai_memory.values(),
            "suggest_wd_for_missing_tags":self.use_wd.isChecked(),
            "suggest_joycaption_for_missing_descriptions":self.use_joy.isChecked(),
            "auto_run_wd_on_managed_images":self.auto_run_wd.isChecked(),
            "auto_run_joy_on_managed_images":self.auto_run_joy.isChecked(),
            "plain_language_model":str(self.plain_language_model.currentData()),
            "wd_general_threshold":self.wd_general.value(),
            "wd_character_threshold":self.wd_character.value(),
            "wd_rating_threshold":self.wd_rating.value(),
            "enabled_wd_taggers":tuple(
                model_id for model_id,control in self.wd_taggers.items()
                if control.isChecked()
            ),
            "wd_consensus_minimum":self.wd_consensus.value(),
            "auto_merge_ai_tags":self.auto_merge_ai.isChecked(),
            "prepare_confidence_preset":str(
                self.prepare_confidence.currentData() or "custom"
            ),
            "prepare_tag_vote_minimum":self.prepare_votes.value(),
            "prepare_external_evidence_vote":(
                self.prepare_external_vote.isChecked()
            ),
            "prepare_confirm_safe_tags":self.prepare_confirm_tags.isChecked(),
            "prepare_preferred_descriptions":(
                self.prepare_descriptions.isChecked()
            ),
            "wd_execution_provider":str(self.wd_provider.currentData()),
            "wd_batch_size":self.wd_batch_size.value(),
            "joycaption_max_new_tokens":self.joy_tokens.value(),
            "joycaption_min_new_tokens":self.joy_min_tokens.value(),
            "joycaption_num_beams":self.joy_beams.value(),
            "joycaption_length_penalty":self.joy_length_penalty.value(),
            "joycaption_do_sample":self.joy_sample.isChecked(),
            "joycaption_temperature":self.joy_temperature.value(),
            "joycaption_top_k":self.joy_top_k.value(),
            "joycaption_top_p":self.joy_top_p.value(),
            "joycaption_repetition_penalty":self.joy_repetition.value(),
            "joycaption_no_repeat_ngram_size":self.joy_ngram.value(),
            "joycaption_gpu_index":self.joy_gpu.value(),
            "joycaption_quantization":str(self.joy_quantization.currentData()),
            "joycaption_prompt":self.joy_prompt.toPlainText(),
        }

    def _set_ai_control_values(self,values: dict):
        self._suppress_dirty=True
        try:
            self.ai_memory.set_values(values)
            self.use_wd.setChecked(values["suggest_wd_for_missing_tags"])
            self.use_joy.setChecked(
                values["suggest_joycaption_for_missing_descriptions"]
            )
            self.auto_run_wd.setChecked(
                values["auto_run_wd_on_managed_images"]
            )
            self.auto_run_joy.setChecked(
                values["auto_run_joy_on_managed_images"]
            )
            controls={
                "wd_general_threshold":self.wd_general,
                "wd_character_threshold":self.wd_character,
                "wd_rating_threshold":self.wd_rating,
                "wd_consensus_minimum":self.wd_consensus,
                "prepare_tag_vote_minimum":self.prepare_votes,
                "wd_batch_size":self.wd_batch_size,
                "joycaption_max_new_tokens":self.joy_tokens,
                "joycaption_min_new_tokens":self.joy_min_tokens,
                "joycaption_num_beams":self.joy_beams,
                "joycaption_length_penalty":self.joy_length_penalty,
                "joycaption_temperature":self.joy_temperature,
                "joycaption_top_k":self.joy_top_k,
                "joycaption_top_p":self.joy_top_p,
                "joycaption_repetition_penalty":self.joy_repetition,
                "joycaption_no_repeat_ngram_size":self.joy_ngram,
                "joycaption_gpu_index":self.joy_gpu,
            }
            for key,control in controls.items():
                control.setValue(values[key])
            enabled=set(values["enabled_wd_taggers"])
            for model_id,control in self.wd_taggers.items():
                control.setChecked(model_id in enabled)
            self.auto_merge_ai.setChecked(values["auto_merge_ai_tags"])
            self.prepare_confirm_tags.setChecked(
                values["prepare_confirm_safe_tags"]
            )
            self.prepare_descriptions.setChecked(
                values["prepare_preferred_descriptions"]
            )
            self.prepare_external_vote.setChecked(
                values["prepare_external_evidence_vote"]
            )
            self.prepare_confidence.setCurrentIndex(max(
                0,self.prepare_confidence.findData(
                    values["prepare_confidence_preset"]
                ),
            ))
            self.wd_provider.setCurrentIndex(
                max(0,self.wd_provider.findData(values["wd_execution_provider"]))
            )
            self.joy_quantization.setCurrentIndex(max(
                0,self.joy_quantization.findData(values["joycaption_quantization"]),
            ))
            self.plain_language_model.setCurrentIndex(max(
                0,self.plain_language_model.findData(
                    values["plain_language_model"]
                ),
            ))
            self.joy_sample.setChecked(values["joycaption_do_sample"])
            self.joy_prompt.setPlainText(values["joycaption_prompt"])
        finally:
            self._suppress_dirty=False
        self._refresh_auto_apply_availability()
        self._update_prepare_confidence()
        self._update_automation_example()
        self._plain_language_model_changed(
            self.plain_language_model.currentIndex()
        )

    @staticmethod
    def _bounded_change_value(value: str,limit: int=72) -> str:
        value=" ".join(str(value).split())
        if len(value)<=limit:
            return value
        return value[:limit-1].rstrip()+"…"

    def _ai_change_value(self,key: str,value,*,exact=False) -> str:
        if key=="ai_runtime_mode":
            text={"on_demand":"On Demand","keep_warm":"Keep Warm","preload":"Preload After Startup"}.get(value,str(value))
        elif key=="ai_runtime_memory_budget_mb":
            text=f"{value:,} MiB" if value else "Automatic for this machine"
        elif key=="ai_runtime_idle_minutes":
            text=f"{value} minutes"
        elif key=="enabled_wd_taggers":
            labels=[
                WD_TAGGER_MODELS.get(model_id,(model_id,""))[0]
                for model_id in value
            ]
            text=", ".join(labels) if labels else "None"
        elif key in {
            "suggest_wd_for_missing_tags",
            "suggest_joycaption_for_missing_descriptions",
            "auto_run_wd_on_managed_images",
            "auto_run_joy_on_managed_images",
            "auto_merge_ai_tags","prepare_external_evidence_vote",
            "prepare_confirm_safe_tags","prepare_preferred_descriptions",
            "joycaption_do_sample",
        }:
            text="On" if value else "Off"
        elif key in {
            "wd_execution_provider","joycaption_quantization",
            "plain_language_model","prepare_confidence_preset",
        }:
            control=(
                self.wd_provider if key=="wd_execution_provider" else
                self.plain_language_model if key=="plain_language_model" else
                self.prepare_confidence
                if key=="prepare_confidence_preset" else
                self.joy_quantization
            )
            index=control.findData(value)
            text=control.itemText(index) if index>=0 else str(value)
        elif key=="joycaption_prompt":
            text=str(value) if exact else (self._bounded_change_value(value) or "Empty")
        elif isinstance(value,float):
            text=f"{value:g}"
        else:
            text=str(value)
        return text if exact else self._bounded_change_value(text)

    def _pending_section_changes(self,section: str) -> list[tuple[str,str,str,str,str]]:
        if section!="ai":
            return []
        labels={
            "ai_runtime_mode":"AI memory policy",
            "ai_runtime_idle_minutes":"Release AI after idle time",
            "ai_runtime_memory_budget_mb":"AI memory budget",
            "suggest_wd_for_missing_tags":"Generate AI Tags on demand",
            "suggest_joycaption_for_missing_descriptions":"Generate AI Descriptions on demand",
            "auto_run_wd_on_managed_images":"Generate AI Tags after library checks",
            "auto_run_joy_on_managed_images":"Generate AI Descriptions after library checks",
            "plain_language_model":"AI Description model",
            "wd_general_threshold":"General Tags minimum confidence",
            "wd_character_threshold":"Character Tags minimum confidence",
            "wd_rating_threshold":"Rating Tags minimum confidence",
            "enabled_wd_taggers":"Enabled AI Tag models",
            "wd_consensus_minimum":"Library-check minimum Tag votes",
            "auto_merge_ai_tags":"Confirm Safe Tags after library checks",
            "prepare_confidence_preset":"Preparation confidence",
            "prepare_tag_vote_minimum":"Preparation minimum Tag votes",
            "prepare_external_evidence_vote":"Preparation TXT / Metadata vote",
            "prepare_confirm_safe_tags":"Confirm Safe Tags during Prepare",
            "prepare_preferred_descriptions":"Fill / Upgrade Descriptions during Prepare",
            "wd_execution_provider":"Execution device",
            "wd_batch_size":"Images per model call",
            "joycaption_max_new_tokens":"Maximum generated tokens",
            "joycaption_min_new_tokens":"Minimum description length",
            "joycaption_num_beams":"Number of beams",
            "joycaption_length_penalty":"Length preference",
            "joycaption_do_sample":"Varied/random wording",
            "joycaption_temperature":"Temperature",
            "joycaption_top_k":"Top-K",
            "joycaption_top_p":"Top-P",
            "joycaption_repetition_penalty":"Repetition penalty",
            "joycaption_no_repeat_ngram_size":"No-repeat phrase size",
            "joycaption_gpu_index":"GPU number",
            "joycaption_quantization":"Model memory mode",
            "joycaption_prompt":"Description instructions",
        }
        committed=self._committed_section_values.get("ai",{})
        current=self._ai_control_values()
        changes=[]
        for key,label in labels.items():
            if committed.get(key)==current.get(key):
                continue
            changes.append((
                label,
                self._ai_change_value(key,committed.get(key)),
                self._ai_change_value(key,current.get(key)),
                self._ai_change_value(key,committed.get(key),exact=True),
                self._ai_change_value(key,current.get(key),exact=True),
            ))
        if (
            self._joy_quantization_user_selected
            and not self.settings.joycaption_quantization_explicit
            and committed.get("joycaption_quantization")
            ==current.get("joycaption_quantization")
        ):
            value=current.get("joycaption_quantization","bf16")
            changes.append((
                "Model memory mode",
                "Hardware recommendation",
                self._ai_change_value("joycaption_quantization",value),
                "Hardware recommendation",
                self._ai_change_value(
                    "joycaption_quantization",value,exact=True
                ),
            ))
        return changes

    def _pending_change_text(self) -> tuple[str,str,int]:
        groups=[]; details=[]; total=0
        for section in sorted(self._dirty_sections):
            changes=self._pending_section_changes(section)
            if not changes:
                continue
            heading={"ai":"AI Configuration"}.get(section,section.replace("_"," ").title())
            total+=len(changes)
            groups.append(f"{heading} ({len(changes)} change{'s' if len(changes)!=1 else ''})")
            groups.extend(f"• {label}: {old} → {new}" for label,old,new,_,_ in changes)
            details.append(heading)
            details.extend(
                f"{label}\nBefore: {old_exact}\nAfter: {new_exact}"
                for label,_,_,old_exact,new_exact in changes
            )
        return "\n".join(groups),"\n\n".join(details),total

    def _focus_ai_setting(self,control):
        if control is self.save_ai_button:
            self.show_section("ai","overview")
        elif control in {
            self.wd_general,self.wd_character,self.wd_rating,
            *self.wd_taggers.values(),self.wd_provider,self.wd_batch_size,
        }:
            self.show_section("ai","sdxl")
        elif control in {self.plain_language_model,self.joy_quantization,self.joy_gpu}:
            self.show_section("ai","description_model")
        else:
            self.show_section("ai","descriptions")
        if control in {
            self.joy_min_tokens,self.joy_beams,self.joy_length_penalty,
            self.joy_sample,self.joy_temperature,self.joy_top_k,self.joy_top_p,
            self.joy_repetition,self.joy_ngram,
        }:
            self.advanced_toggle.setChecked(True)
        if control in set(self.wd_taggers.values())|{self.wd_provider,self.wd_batch_size}:
            self.tagger_toggle.setChecked(True)
        control.setFocus(Qt.OtherFocusReason)

    def _discard_section(self,section: str):
        if section=="ai":
            self._set_ai_control_values(self._committed_section_values["ai"])
            self._joy_previous_quantization=str(
                self.joy_quantization.currentData()
            )
            self._joy_quantization_user_selected=False
            self._update_joy_memory_guidance()
            blocker=QSignalBlocker(self.automation_mode)
            self.automation_mode.setCurrentIndex(0)
            del blocker
            self._automation_preview_plan=None
            self._applied_automation_preset=""
            self.apply_automation_preset_button.setEnabled(False)
            self.automation_preset_preview.setText(
                "No preset selected. Your saved or pending settings are unchanged."
            )
            self._update_automation_example()
        self._clear_dirty(section)
        self.header.set_status("Pending AI configuration changes discarded")

    def _save_pending_sections(self) -> bool:
        for section in list(sorted(self._dirty_sections)):
            if section=="ai" and not self.save_ai():
                return False
        return not self._dirty_sections

    def _pending_changes_dialog(self,*,for_close: bool,summary: str,details: str,total: int):
        dialog=QMessageBox(
            QMessageBox.Warning,
            "Close with unsaved changes" if for_close else "Leave Setup with unsaved changes",
            "Review the pending Setup changes before continuing.",
            parent=self,
        )
        dialog.setInformativeText(summary)
        if details:
            dialog.setDetailedText(details)
        save=dialog.addButton(
            "Save and Close" if for_close else "Save and Leave",
            QMessageBox.AcceptRole,
        )
        discard=dialog.addButton("Discard Changes",QMessageBox.DestructiveRole)
        cancel=dialog.addButton(
            "Cancel" if for_close else "Return to Setup",
            QMessageBox.RejectRole,
        )
        save.setAccessibleName(save.text())
        save.setAccessibleDescription(
            f"Save all {total} pending Setup changes, then "
            f"{'close CollectiGen' if for_close else 'open the requested workspace'}."
        )
        discard.setAccessibleName("Discard Changes")
        discard.setAccessibleDescription(
            f"Restore the last saved values for all {total} pending Setup changes, then "
            f"{'close CollectiGen' if for_close else 'open the requested workspace'}."
        )
        cancel.setAccessibleName(cancel.text())
        cancel.setAccessibleDescription(
            "Keep every pending value and return to Setup without saving or discarding."
        )
        save.setObjectName("savePendingSetupChanges")
        discard.setObjectName("discardPendingSetupChanges")
        cancel.setObjectName("returnToSetup")
        dialog.setDefaultButton(cancel); dialog.setEscapeButton(cancel)
        dialog.setAccessibleName(dialog.windowTitle())
        dialog.setAccessibleDescription(
            f"{total} pending Setup change{'s' if total!=1 else ''}. {summary}"
        )
        return dialog,{"save":save,"discard":discard,"cancel":cancel}

    def _pending_change_action(self,*,for_close: bool,summary: str,details: str,total: int) -> str:
        dialog,buttons=self._pending_changes_dialog(
            for_close=for_close,summary=summary,details=details,total=total,
        )
        dialog.exec()
        clicked=dialog.clickedButton()
        return next(
            (action for action,button in buttons.items() if clicked is button),
            "cancel",
        )

    def confirm_leave(self,*,for_close=False) -> bool:
        if for_close:
            swarm = getattr(self, "swarm_runtime", None)
            if swarm is not None and not swarm.request_onboarding_close(self.window().close):
                return False
        if not self.folder_locations.confirm_leave():
            return False
        if not self._dirty_sections:
            return True
        summary,details,total=self._pending_change_text()
        action=self._pending_change_action(
            for_close=for_close,summary=summary,details=details,total=total,
        )
        if action=="save":
            return self._save_pending_sections()
        if action=="discard":
            for section in list(sorted(self._dirty_sections)):
                self._discard_section(section)
            return True
        return False

    def _save_update_channel(self,_index=0):
        if self._save_preferences({"update_channel":str(self.update_channel.currentData())}):
            self.header.set_status("Update channel saved")

    def _save_theme_preference(self,_index=0):
        theme=str(self.theme.currentData())
        if self._save_preferences({"ui_theme":theme}):
            self.theme_changed.emit(theme)
            self.header.set_status("Appearance saved automatically")

    def _sync_imported_appearance(self, theme):
        # Import already saved only this field. Do not publish other drafts.
        with QSignalBlocker(self.theme):
            self.theme.setCurrentIndex(max(0, self.theme.findData(theme)))
        self.header.set_status("Imported appearance saved")

    def _save_color_blind_preference(self,checked):
        if self._save_preferences({"color_blind_mode":bool(checked)}):
            self.color_blind_changed.emit(bool(checked))
            self.header.set_status("Appearance saved automatically")

    def _save_update_startup_preference(self,checked: bool):
        if self._save_preferences({"check_updates_on_startup":bool(checked)}):
            self.header.set_status(
                "Startup update checks enabled" if checked
                else "Startup update checks disabled"
            )

    def _begin_operation(self,owner: str,label: str,*,resource: str="io") -> bool:
        if self.state is None:
            return True
        if resource=="io":
            started,active=self.state.begin_long_operation(owner,label)
        else:
            started,active=self.state.begin_long_operation(owner,label,resource=resource)
        if not started:
            show_operation_in_progress(self,active)
        return started

    def _finish_operation(self,owner: str):
        if self.state is not None:
            self.state.finish_long_operation(owner)

    def _application_update_service(self):
        from core.application_update import ApplicationUpdateService
        from version import VERSION
        return ApplicationUpdateService(Path(__file__).resolve().parents[1],VERSION)

    def _rollback_available(self) -> bool:
        from version import VERSION
        path=Path(__file__).resolve().parents[1]/"data"/"update-state"/"active-update.json"
        try:
            import json
            payload=json.loads(path.read_text(encoding="utf-8-sig"))
        except (OSError,ValueError,TypeError): return False
        return isinstance(payload,dict) and payload.get("status")=="healthy" and payload.get("target_version")==VERSION

    def _set_update_controls(self,enabled: bool):
        installed=self._application_update_service().installed_package
        self.check_update_button.setEnabled(enabled and installed)
        self.offline_update_button.setEnabled(enabled and installed)
        self.update_channel.setEnabled(enabled)
        self.check_updates_on_startup.setEnabled(enabled and installed)

    def check_for_update(self):
        if self._update_task is not None: return
        if not self._begin_operation("setup_update","Checking for application updates"): return
        channel=str(self.update_channel.currentData()); self.settings.update_channel=channel; self.settings.save()
        service=self._application_update_service(); self._set_update_controls(False); self.update_status.setText("Checking the signed release manifest…")
        task=AsyncCallTask(lambda:service.check(channel)); self._update_task=task
        task.signals.complete.connect(self._update_check_complete); task.signals.failed.connect(self._update_failed); self.pool.start(task)

    def _update_check_complete(self,candidate):
        self._update_task=None; self._set_update_controls(True); self._finish_operation("setup_update")
        if candidate is None:
            self.update_status.setText("This installation is current for the selected channel."); return
        release=candidate.release
        if not confirm_update_candidate(candidate,self):
            self.update_status.setText(f"CollectiGen v{release.version} is available; download was not started."); return
        self.prepare_update(candidate)

    def prepare_update(self,candidate):
        if not self._begin_operation("setup_update",f"Preparing CollectiGen v{candidate.release.version}"): return
        service=self._application_update_service(); artifact=candidate.release.artifact("portable-windows-x64")
        dialog=QProgressDialog("Downloading the verified application package…","",0,artifact.size_bytes,self)
        dialog.setWindowTitle("Prepare application update")
        dialog.setCancelButtonText("Cancel Safely")
        dialog.setMinimumDuration(0); dialog.show(); self._update_progress=dialog
        def work(progress,should_cancel):
            return service.download_and_prepare(candidate,progress,should_cancel)
        task=CancelableProgressCallTask(work); self._update_task=task; self._set_update_controls(False)
        dialog.canceled.connect(task.cancel)
        task.signals.progress.connect(self._update_download_progress)
        task.signals.complete.connect(self._update_prepared)
        task.signals.cancelled.connect(self._update_cancelled)
        task.signals.failed.connect(self._update_failed); self.pool.start(task)

    def _update_download_progress(self,current,total,label):
        if self._update_progress is None: return
        if self.state is not None: self.state.update_long_operation("setup_update",current,total,label)
        self._update_progress.setMaximum(max(1,total)); self._update_progress.setValue(current)
        self._update_progress.setLabelText(f"Downloading {label}\n{current:,} of {total:,} bytes verified after completion")

    def prepare_offline_update(self):
        if self._update_task is not None: return
        source=QFileDialog.getOpenFileName(self,"Choose offline CollectiGen update","","CollectiGen update (*.zip)")[0]
        if not source: return
        if not self._begin_operation("setup_update","Verifying an offline application update"): return
        service=self._application_update_service(); self._set_update_controls(False); self.update_status.setText("Verifying the offline update and its complete package inventory…")
        task=AsyncCallTask(lambda:service.prepare_offline_bundle(Path(source))); self._update_task=task
        task.signals.complete.connect(self._update_prepared); task.signals.failed.connect(self._update_failed); self.pool.start(task)

    def _update_prepared(self,prepared):
        self._update_task=None; self._set_update_controls(True); self._finish_operation("setup_update")
        if self._update_progress is not None: self._update_progress.close(); self._update_progress=None
        self.update_status.setText(f"Verified CollectiGen v{prepared.version} is ready and will be applied after restart.")
        QMessageBox.information(
            self,"Update ready after restart",
            f"CollectiGen v{prepared.version} passed signature, SHA-256, and package-inventory verification.\n\nClose CollectiGen normally, then start it again to apply the update. If startup or migration fails, the next start restores the previous application and its matching data snapshot.",
        )

    def _update_cancelled(self):
        self._update_task=None; self._set_update_controls(True); self._finish_operation("setup_update")
        if self._update_progress is not None: self._update_progress.close(); self._update_progress=None
        self.update_status.setText(
            "Update preparation was cancelled safely. A verified download can resume from the retained partial file."
        )

    def _update_failed(self,message):
        self._update_task=None; self._set_update_controls(True); self._finish_operation("setup_update")
        if self._update_progress is not None: self._update_progress.close(); self._update_progress=None
        self.update_status.setText("The update was not prepared; the installed application was not changed.")
        QMessageBox.warning(self,"Update could not be prepared",message)

    def request_update_rollback(self):
        if QMessageBox.warning(
            self,"Restore previous application version",
            "Restore the previous verified application and its exact pre-update data snapshot after restart?\n\nChanges made since the update will not be kept.",
            QMessageBox.Yes|QMessageBox.Cancel,QMessageBox.Cancel,
        )!=QMessageBox.Yes: return
        try:
            from core.application_update import request_update_rollback
            from version import VERSION
            previous=request_update_rollback(Path(__file__).resolve().parents[1],VERSION)
        except Exception as exc:
            QMessageBox.warning(self,"Previous version is unavailable",str(exc)); return
        self.rollback_update_button.setEnabled(False)
        self.update_status.setText(f"CollectiGen v{previous} and its matching data snapshot will be restored after restart.")

    def choose_storage(self,key: str):
        entry=self.storage_fields[key]
        selected=QFileDialog.getExistingDirectory(self,"Choose folder",entry.text().strip() or str(Path.cwd()))
        if selected: entry.setText(selected); self.save_storage()

    def open_recycle_bin(self):
        if self.state is None:return
        dialog=RecycleBinDialog(self.state,self)
        dialog.changed.connect(lambda:self.header.set_status("Recycle and recovery locations changed"))
        dialog.exec()

    def _selected_library_profile(self) -> LibraryProfile | None:
        row=self.library_table.currentRow()
        if row<0:
            return None
        item=self.library_table.item(row,0)
        stable_id=str(item.data(Qt.UserRole) or "") if item is not None else ""
        return self.library_registry.get(stable_id)

    def _refresh_library_profiles(self,select_id: str="") -> None:
        selected=select_id or (
            self._selected_library_profile().stable_id
            if hasattr(self,"library_table") and self._selected_library_profile() is not None
            else ""
        )
        profiles=self.library_registry.profiles
        self.library_table.setRowCount(len(profiles))
        selected_row=-1
        for row,profile in enumerate(profiles):
            active=profile.stable_id==self.settings.active_library_id
            included=(
                not self.settings.library_scope_ids
                or profile.stable_id in self.settings.library_scope_ids
            )
            issues=self.library_registry.validate(profile,replacing_id=profile.stable_id)
            status=("Default • " if active else "")+(
                "Included" if included else "Filtered out"
            )
            runtime="not_loaded"
            manager=None
            dataset=getattr(self.state,"dataset",None) if self.state is not None else None
            if dataset is not None and hasattr(dataset,"library_availability"):
                runtime=dataset.library_availability(profile.stable_id)
                manager=dataset.manager(profile.stable_id)
            runtime_label={
                "not_loaded":"Not opened",
                "checking":"Checking files",
                "saved_checking":"Saved view; checking files",
                "validated_checking":"Browse ready; changes paused during startup",
                "validated_cancelled":"Browse ready; library checks paused",
                "current":"Current",
                "saved_unverified":"Saved view; files unavailable",
                "unavailable":"Unavailable",
                "cancelled":"Partially checked",
            }.get(runtime,str(runtime).replace("_"," ").title())
            if manager is not None and runtime=="current":
                limitations=[]
                for field,label in (
                    ("online_only","online-only"),("conflicts","sync conflict"),
                    ("read_only","read-only"),("unavailable","unavailable"),
                ):
                    count=int(getattr(manager.summary,field,0) or 0)
                    if count:limitations.append(f"{count:,} {label}")
                if profile.recognized_role is LibraryRole.CLOUD_SYNCED:
                    limited=sum(
                        int(getattr(manager.summary,field,0) or 0)
                        for field in ("online_only","conflicts","read_only","unavailable")
                    )
                    ready=max(0,int(getattr(manager.summary,"images",0) or 0)-limited)
                    runtime_label+=f"; {ready:,} ready locally"
                if limitations:
                    runtime_label+="; "+", ".join(limitations)
            if runtime!="not_loaded":
                status+=" • "+runtime_label
            if issues:
                status=("Default • " if active else "")+"Needs attention"
            values=(profile.name or "Unnamed",profile.display_role,profile.root or "No folder",status)
            for column,value in enumerate(values):
                item=QTableWidgetItem(value)
                item.setData(Qt.UserRole,profile.stable_id)
                item.setToolTip(
                    "\n".join(issue.message for issue in issues)
                    if issues else (
                        value if column!=3 else
                        status+f"\nFile status: {runtime_label}\nChanging the app-wide library filter never moves or mixes files. Refresh rechecks current local availability."
                    )
                )
                self.library_table.setItem(row,column,item)
            if profile.stable_id==selected:
                selected_row=row
        if selected_row<0 and profiles:
            selected_row=next(
                (row for row,profile in enumerate(profiles) if profile.stable_id==self.settings.active_library_id),
                0,
            )
        if selected_row>=0:
            self.library_table.selectRow(selected_row)
        else:
            self.library_table.clearSelection()
        self._library_selection_changed()

    def _library_selection_changed(self) -> None:
        profile=self._selected_library_profile()
        if profile is None:
            self.library_guidance.setText(
                "Add a library to give each collection a clear privacy and availability role. Existing legacy working folders are treated as Local Only when first saved."
            )
            self.edit_library_button.setEnabled(False)
            self.activate_library_button.setEnabled(False)
            self.remove_library_button.setEnabled(False)
            return
        details=profile.role_details
        if details is None:
            explanation="This saved type is not supported by this CollectiGen version. Edit it before making the library the default."
        else:
            explanation=f"{details.label}. {details.summary} {details.consequences}"
        active=profile.stable_id==self.settings.active_library_id
        included=(
            not self.settings.library_scope_ids
            or profile.stable_id in self.settings.library_scope_ids
        )
        self.library_guidance.setText(
            ("Default library for initial focus and older single-folder actions. " if active else "")
            +("Included in the shared app filter. " if included else "Currently filtered out throughout the app. ")
            +explanation
        )
        issues=self.library_registry.validate(profile,replacing_id=profile.stable_id)
        self.edit_library_button.setEnabled(True)
        self.activate_library_button.setEnabled(not active and not issues)
        self.remove_library_button.setEnabled(not active)
        self.activate_library_button.setToolTip(
            "Already the default library." if active else (
                "Correct this library's details before making it the default."
                if issues else "Use this collection for initial focus and older single-folder actions. No files move and other included libraries remain visible."
            )
        )
        self.remove_library_button.setToolTip(
            "Choose another default library before removing this definition."
            if active else "Remove only this saved definition. Files are not deleted or moved."
        )

    def _persist_library_registry(
        self,*,active_library_id: str | None=None,select_id: str="",
    ) -> bool:
        from copy import deepcopy
        from core.folder_locations import APP_FOLDERS, validate_model_folder_separation
        previous_id=self.settings.active_library_id
        previous_root=self.settings.working_directory
        previous_profiles=list(self.settings.library_profiles)
        previous={key:deepcopy(getattr(self.settings,key)) for key in (
            "active_library_id","working_directory","library_profiles",
            "library_scope_ids","recent_directories",
        )}
        try:
            snapshot,_=self.folder_locations.registry.snapshot()
            validate_model_folder_separation(
                {key:getattr(self.settings,key,"") for key,_ in APP_FOLDERS},
                self.library_registry.profiles,snapshot.get("workspace",{}),snapshot=snapshot,
            )
            self.settings.set_library_registry(
                self.library_registry,active_library_id=active_library_id,
            )
            self.settings.save()
        except (OSError,ValueError,RuntimeError) as exc:
            for key,value in previous.items():
                setattr(self.settings,key,value)
            self.library_registry=self.settings.library_registry()
            self._refresh_library_profiles(previous_id)
            self.header.set_status("Library changes were not saved")
            QMessageBox.warning(self,"Library Folders Need Attention",str(exc))
            return False
        self._refresh_library_profiles(select_id or self.settings.active_library_id)
        active_changed=(
            previous_id!=self.settings.active_library_id
            or previous_root!=self.settings.working_directory
        )
        self.header.set_status("Named libraries saved automatically")
        if active_changed or previous_profiles!=self.settings.library_profiles:
            self.library_profile_changed.emit(
                select_id or self.settings.active_library_id
            )
        return True

    def add_library(self) -> None:
        dialog=LibraryProfileDialog(self.library_registry,self)
        if not dialog.exec() or dialog.profile is None:
            return
        self.library_registry.add(dialog.profile)
        active_id=(
            dialog.profile.stable_id
            if not self.settings.active_library_id else self.settings.active_library_id
        )
        self._persist_library_registry(
            active_library_id=active_id,select_id=dialog.profile.stable_id,
        )

    def edit_library(self) -> None:
        current=self._selected_library_profile()
        if current is None:
            return
        dialog=LibraryProfileDialog(self.library_registry,self,profile=current)
        if not dialog.exec() or dialog.profile is None:
            return
        self.library_registry.replace(dialog.profile)
        self._persist_library_registry(select_id=dialog.profile.stable_id)

    def activate_library(self) -> None:
        profile=self._selected_library_profile()
        if profile is None or profile.stable_id==self.settings.active_library_id:
            return
        issues=self.library_registry.validate(profile,replacing_id=profile.stable_id)
        if issues:
            QMessageBox.warning(
                self,"Library Details Need Attention",
                "\n".join(f"• {issue.message}" for issue in issues),
            )
            return
        self._persist_library_registry(
            active_library_id=profile.stable_id,select_id=profile.stable_id,
        )

    def remove_library(self) -> None:
        profile=self._selected_library_profile()
        if profile is None:
            return
        if profile.stable_id==self.settings.active_library_id:
            QMessageBox.information(
                self,"Choose Another Default Library",
                "Make another library the default before removing this definition. No files were changed.",
            )
            return
        if QMessageBox.question(
            self,"Remove Library Definition",
            f'Remove the saved definition for "{profile.name}"?\n\nNo image, Image Knowledge, TXT, Metadata, cache, or folder will be deleted or moved.',
            QMessageBox.Yes|QMessageBox.Cancel,QMessageBox.Cancel,
        )!=QMessageBox.Yes:
            return
        self.library_registry.remove(profile.stable_id)
        self._persist_library_registry()

    def dependency_service(self):
        factory=self.dependency_service_factory if self.dependency_service_factory is not None else DependencyService
        return factory(self.settings)

    def open_dependencies(self,capability: str="",models=None):
        if capability=="wd":
            models=(list(models) if models is not None else [
                model_id for model_id,control in self.wd_taggers.items()
                if control.isChecked()
            ])
            if not models:
                models=[
                    model_id for model_id in self.settings.enabled_wd_taggers
                    if model_id in WD_TAGGER_MODELS
                ] or [WD_EVA02_MODEL]
        else:
            models=None
        self.show_section("ai","components")
        self.dependency_panel.focus_capability(capability,models)
        self.dependency_panel.refresh(); self.dependency_panel.focus_capability(capability)

    def open_models_workspace(self):
        suspended = getattr(self, "_suspended_model_workspace", None)
        if suspended is not None:
            from shiboken6 import isValid
            if isValid(suspended) and getattr(suspended, "_setup_suspended", False):
                self.show_section("components_downloads", "local_generation")
                self.swarm_runtime.return_to_request_button.setFocus(Qt.OtherFocusReason)
                return
            self._suspended_model_workspace = None
        retained = getattr(self, "_retained_model_generation_dialog", None)
        if retained is not None:
            from shiboken6 import isValid
            if not isValid(retained) or retained.generation_handoff is None:
                retained = None
            self._retained_model_generation_dialog = None
            self.models_generation_return_panel.hide()
            self.models_workspace_button.show()
        from core.paths import MODEL_RESOURCES_PATH
        from core.model_resource_registry import ModelResourceRegistry
        from .model_workspace_dialog import ModelWorkspaceDialog
        if retained is not None:
            dialog = retained
            dialog.dialog_commands.select_group("Planning")
        else:
            roots=[p.root for p in self.settings.library_registry().profiles]
            roots.extend(str(getattr(self.settings,key,"")) for key in (
                "ingest_directory","problems_directory","recycle_directory","external_write_backup_directory",
            ) if getattr(self.settings,key,""))
            dialog=ModelWorkspaceDialog(ModelResourceRegistry(MODEL_RESOURCES_PATH),self.state.jobs,self,
                protected_roots=roots,settings=self.settings)
        result = dialog.exec()
        if getattr(dialog, "_setup_suspended", False):
            self._suspended_model_workspace = dialog
            dialog.finished.connect(lambda _result: setattr(self, "_suspended_model_workspace", None))
        elif result == QDialog.Accepted and getattr(dialog, "_generation_handoff_ready", False):
            QTimer.singleShot(0, lambda: self._continue_models_generation_handoff(dialog))
        elif retained is not None:
            dialog.generation_handoff = None

    def _retain_models_generation_handoff(self, dialog, message):
        """Return to editable Setup without losing an unapplied Models review."""
        dialog._generation_handoff_ready = False
        dialog.dialog_commands.select_group("Planning")
        dialog.status.setText(message)
        self._retained_model_generation_dialog = dialog
        self.models_generation_return_panel.detail_label.setText(
            message + " Return to the same Models review when ready; no draft was changed."
        )
        self.models_workspace_button.hide()
        self.models_generation_return_panel.show()
        self.models_generation_return_panel.action_button.setFocus(Qt.OtherFocusReason)

    def _continue_models_generation_handoff(self, dialog):
        """Open one new Generate draft only after Models actually closed."""
        from core.paths import MODEL_RESOURCES_PATH
        from core.model_resource_registry import ModelResourceRegistry
        from .generation_workflow import open_generation_workspace

        if not getattr(dialog, "_generation_handoff_ready", False) or dialog.generation_handoff is None:
            return
        guidance, configuration, registry_revision, service, catalog_token = dialog.generation_handoff

        def retry(message):
            self._retain_models_generation_handoff(dialog, message)

        try:
            if (ModelResourceRegistry(MODEL_RESOURCES_PATH).snapshot()[1] != registry_revision or
                    service.catalog_write_token() != catalog_token):
                retry("Models or guidance changed before Generate opened. Review a fresh recommendation; no draft was changed.")
                return
        except (OSError, RuntimeError, ValueError) as exc:
            retry(f"Could not recheck Models before Generate: {exc}. Review the recommendation again.")
            return

        if not self.confirm_leave():
            retry("Finish the pending Setup changes before opening Generate. The reviewed Models recommendation remains available.")
            return

        opened = False

        def apply_to_new_draft(target):
            nonlocal opened
            opened = True
            try:
                if service.catalog_write_token() != catalog_token:
                    target.status.setText("Model guidance changed before application. Open Recommended Setup to review a fresh recommendation; this new draft was not changed.")
                    return
                applied = target.apply_model_guidance(guidance, configuration,
                    expected_registry_revision=registry_revision)
            except (OSError, RuntimeError, ValueError) as exc:
                target.status.setText(f"Could not apply the reviewed setup: {exc}. Open Recommended Setup to review again; this new draft was not changed.")
                return
            if applied:
                dialog.generation_handoff = None
                dialog._generation_handoff_ready = False
            else:
                target.status.setText(target.status.text() + " Open Recommended Setup to review again; the new draft was not changed.")

        open_generation_workspace(self, force_new=True, on_opened=apply_to_new_draft)
        if not opened:
            retry("Generate could not open a new draft. The reviewed setup is still here; finish the other open workflow, then Continue in Generate again.")

    def open_upscaler_models(self):
        if self.state is None:
            return
        from core.paths import MODEL_RESOURCES_PATH
        from core.model_resource_registry import ModelResourceRegistry
        from core.model_selection import protected_model_roots
        from .resolution_models_dialog import UpscalerModelsDialog
        dialog=UpscalerModelsDialog(ModelResourceRegistry(MODEL_RESOURCES_PATH),
            self.state.jobs,self,protected_roots=protected_model_roots(self.settings),allow_select=False)
        dialog.exec()

    def choose_preparation_resource(self, role):
        from PySide6.QtWidgets import QDialog
        from core.paths import MODEL_RESOURCES_PATH
        from core.model_resource_registry import ModelResourceRegistry
        from core.model_selection import protected_model_roots
        from .model_selector_dialog import ModelSelectorDialog
        dialog = ModelSelectorDialog(ModelResourceRegistry(MODEL_RESOURCES_PATH), self.state.jobs,
            parent=self, role=role, protected_roots=protected_model_roots(self.settings), settings=self.settings)
        if dialog.exec() != QDialog.Accepted:
            return
        model_id = dialog.selected_component_model_id
        if role == "captioner":
            index = self.plain_language_model.findData(model_id)
            if index >= 0:
                self.plain_language_model.setCurrentIndex(index)
        elif role == "tagger" and model_id in self.wd_taggers:
            # Add one deliberately chosen tagger without discarding the other
            # votes or changing the consensus policy. Setup Save remains owner.
            control = self.wd_taggers[model_id]
            control.setChecked(True)

    def open_generation_workspace(self):
        from .generation_workflow import open_generation_workspace
        open_generation_workspace(self)

    def open_video_setup(self):
        from .video_generation_workflow import open_video_generation_workspace
        open_video_generation_workspace(self,on_opened=lambda dialog:dialog.show_local_setup())

    def open_training_setup(self):
        from .training_setup_queue_controller import open_trainer_setup
        open_trainer_setup(self.state,self)

    def open_training_history(self):
        from .training_workflow_controller import shared_training_workflow
        shared_training_workflow(self.state,self.window()).history()

    def manage_ai_components(self,capability: str,model_id: str=""):
        self.show_section("ai","components")
        self.dependency_panel.manage_capability(capability,model_id)

    def manage_plain_language_downloads(self):
        selected=str(self.plain_language_model.currentData())
        if selected!=str(self.settings.plain_language_model):
            QMessageBox.information(
                self,"Save AI Configuration first",
                "Save the selected AI Description model with Save AI "
                "Configuration before managing its "
                "download. Components will then show the matching setup path.",
            )
            self._focus_ai_setting(self.save_ai_button)
            return
        self.manage_ai_components("joy",selected)

    def _refresh_ai_availability(
        self,service: DependencyService | None=None,*,
        stage_recommendation: bool | None=None,
    ):
        if stage_recommendation is None:
            sections=getattr(self,"sections",None)
            stage_recommendation=(
                self._component_inventory_ready and not self._suppress_dirty
                and sections is not None
                and sections.currentIndex()==self._section_indexes["ai_models_runtime"]
            )
        service=service or self.dependency_service()
        self._ai_dependency_service=service
        wd_ready=service.capability_ready("wd"); joy_ready=service.capability_ready("joy")
        self.auto_run_wd.setEnabled(wd_ready or self.auto_run_wd.isChecked())
        self.auto_run_joy.setEnabled(joy_ready or self.auto_run_joy.isChecked())
        self.auto_run_wd.setToolTip(
            "Generate AI Tags automatically for missing or changed images after "
            "a library check. Results remain reviewable unless Confirm Safe Tags "
            "After Library Checks is enabled."
            if wd_ready else
            "Set up the enabled AI Tag models before turning this on. A previously enabled option can still be turned off."
        )
        self.auto_run_joy.setToolTip(
            "Generate AI Descriptions automatically for missing or changed images "
            "after a library check. They remain review findings."
            if joy_ready else
            "Set up AI Descriptions before turning this on. A previously enabled option can still be turned off."
        )
        self.auto_run_wd.setAccessibleDescription(self.auto_run_wd.toolTip())
        self.auto_run_joy.setAccessibleDescription(self.auto_run_joy.toolTip())
        if hasattr(self,"wd_taggers"):
            self._refresh_model_choices(service)
        if hasattr(self,"joy_benchmark_status"):
            self._refresh_joy_benchmark_status(service)
        if hasattr(self,"joy_quantization"):
            self._refresh_joy_memory_recommendation(
                service,stage=stage_recommendation,
            )
        if hasattr(self,"auto_merge_ai"):
            self._refresh_auto_apply_availability()
        if hasattr(self,"overview_tag_status"):
            self._update_ai_overview_readiness(service)

    def _refresh_model_choices(self,service: DependencyService | None = None):
        """Keep model selection aligned with model files that are actually ready."""
        service=service or self.dependency_service()
        previous_suppression=self._suppress_dirty; self._suppress_dirty=True
        try:
            for model_id,control in self.wd_taggers.items():
                component=service.component(service.wd_component_name(model_id))
                ready=bool(component and component.installed)
                if not ready:
                    control.setChecked(False)
                control.setEnabled(ready)
                availability="Downloaded and ready." if ready else "Download this model in Components before enabling it."
                info=WD_TAGGER_MODEL_CARDS[model_id]
                control.setToolTip(f"Best fit: {info['best_for']}.\n\n{WD_TAGGER_DESCRIPTIONS[model_id]}\n\n{availability}")
                control.setAccessibleDescription(availability)
                benchmark=self.settings.ai_model_benchmarks.get(model_id,{})
                current=wd_benchmark_is_current(self.settings,model_id,benchmark) if benchmark else False
                seconds=float(benchmark["seconds_per_image"]) if current and benchmark.get("seconds_per_image") else None
                self.wd_tagger_status[model_id].setText(self._model_status_html(
                    model_id,ready=ready,seconds=seconds,
                    stale=bool(benchmark) and not current,
                ))
        finally:
            self._suppress_dirty=previous_suppression

    def _refresh_auto_apply_availability(self):
        selected_ids=tuple(
            model_id for model_id,control in self.wd_taggers.items()
            if control.isChecked() and control.isEnabled()
        )
        selected=len(selected_ids)
        ready=sum(control.isEnabled() for control in self.wd_taggers.values())
        if hasattr(self,"tag_model_status"):
            selected_names=[
                str(self.wd_taggers[model_id].property("modelLabel"))
                for model_id in selected_ids
            ]
            committed=self._committed_section_values.get("ai",{})
            committed_ready_ids=tuple(
                model_id for model_id in committed.get("enabled_wd_taggers",())
                if model_id in self.wd_taggers
                and self.wd_taggers[model_id].isEnabled()
            )
            pending=(
                bool(committed)
                and selected_ids!=committed_ready_ids
                and "ai" in self._dirty_sections
            )
            if selected_names:
                action="Pending selection" if pending else "Using"
                choice=f"{action} {', '.join(selected_names)} for AI Tag generation."
                if pending:
                    choice+=" Save AI Configuration to use this change."
            elif ready:
                choice=(
                    "None selected (pending). Choose Tag Models and Runtime or Save AI Configuration to disable AI Tags."
                    if pending else
                    "None selected. Choose Tag Models and Runtime to enable AI Tags."
                )
            else:
                choice="No models ready. Manage AI Tag Components to add one."
            self.tag_model_status.setText(
                f"{ready:,} of {len(self.wd_taggers):,} model files installed; runtime checked before use. "
                f"{choice}"
            )
            self.tag_model_status.setAccessibleDescription(
                self.tag_model_status.text()
            )
        available=selected>=1
        automatic=self.auto_merge_ai.isChecked() and available
        previous=self.wd_consensus.blockSignals(True)
        try:
            self.wd_consensus.setMinimum(2 if automatic else 1)
            self.wd_consensus.setMaximum(max(2 if automatic else 1,selected+1))
            if self.wd_consensus.value()>selected+1:
                self.wd_consensus.setValue(max(1,selected+1))
        finally:
            self.wd_consensus.blockSignals(previous)
        self.wd_consensus.setToolTip(
            "Each enabled AI Tag model contributes one vote. A matching pending "
            "TXT or Metadata finding contributes at most one additional vote, "
            "even when both sources contain it."
        )
        self.wd_consensus.setAccessibleDescription(self.wd_consensus.toolTip())
        self.auto_merge_ai.setEnabled(available)
        self.auto_merge_ai.setToolTip(
            (
                "Only known Tags meeting the vote threshold and every conflict "
                "check are confirmed automatically. With one model, an exact "
                "matching pending TXT or Metadata finding supplies the required "
                "second vote."
                if selected==1 else
                "Only known Tags meeting the vote threshold and every conflict "
                "check are confirmed automatically."
            )
            if available else
            "Enable at least one downloaded AI Tag model to use automatic consensus application."
        )
        self.auto_merge_ai.setAccessibleDescription(self.auto_merge_ai.toolTip())
        if not available and self.auto_merge_ai.isChecked():
            self.auto_merge_ai.setChecked(False)

    def _matching_preparation_preset(self) -> str:
        configured=(
            self.prepare_votes.value(),
            self.prepare_external_vote.isChecked(),
        )
        return next((
            key for key,policy in CONFIDENCE_PRESETS.items()
            if configured==(
                int(policy["minimum_tag_votes"]),
                bool(policy["external_evidence_vote"]),
            )
        ),"")

    def _prepare_confidence_preset_changed(self,*_args) -> None:
        preset=str(self.prepare_confidence.currentData() or "custom")
        if preset in CONFIDENCE_PRESETS:
            policy=CONFIDENCE_PRESETS[preset]
            votes_blocker=QSignalBlocker(self.prepare_votes)
            external_blocker=QSignalBlocker(self.prepare_external_vote)
            self.prepare_votes.setValue(int(policy["minimum_tag_votes"]))
            self.prepare_external_vote.setChecked(bool(
                policy["external_evidence_vote"]
            ))
            del votes_blocker,external_blocker
        self._update_prepare_confidence()
        if not self._suppress_dirty:
            self._mark_dirty("ai"); self._automation_controls_changed()

    def _prepare_confidence_values_changed(self,*_args) -> None:
        preset=self._matching_preparation_preset() or "custom"
        blocker=QSignalBlocker(self.prepare_confidence)
        self.prepare_confidence.setCurrentIndex(
            self.prepare_confidence.findData(preset)
        )
        del blocker
        self._update_prepare_confidence()
        if not self._suppress_dirty:
            self._mark_dirty("ai"); self._automation_controls_changed()

    def _update_prepare_confidence(self,*_args) -> None:
        active=(
            self.use_wd.isChecked()
            and self.prepare_confirm_tags.isChecked()
        )
        for control in (
            self.prepare_confidence,self.prepare_votes,
            self.prepare_external_vote,
        ):
            control.setEnabled(active)
        required=self.prepare_votes.value()
        external=(
            " Matching typed TXT or Metadata adds one vote only when a current "
            "AI Tag model also found that Tag."
            if self.prepare_external_vote.isChecked()
            else " TXT and Metadata do not add a vote."
        )
        detail=(
            (
                f"At least {required} independent Tag vote"
                f"{'s are' if required!=1 else ' is'} required during Prepare "
                f"Images. Each current AI Tag model counts once.{external} "
                "Only known, conflict-free Tags can be confirmed; ambiguous, "
                "ignored, rejected, stale, unavailable, and failed results remain "
                "unchanged or reviewable."
            )
            if active else
            "Turn on Generate AI Tags and Confirm Safe Tags During Prepare to "
            "use these preparation confidence controls."
        )
        warning=active and required==1
        self.prepare_confidence_warning.setText(
            "⚠ One AI model may confirm Tags" if warning else ""
        )
        self.prepare_confidence_warning.setVisible(warning)
        self.prepare_confidence_warning.setToolTip(detail)
        self.prepare_confidence_warning.setAccessibleDescription(detail)
        self.tag_changes.setToolTip(detail)
        for control in (
            self.prepare_confidence,self.prepare_votes,
            self.prepare_external_vote,
        ):
            control.setToolTip(detail)
            control.setAccessibleDescription(detail)
        confirmation_detail=(
            "Use safe automatic Tag confirmation by default during Prepare "
            "Images. "+detail
        )
        self.prepare_confirm_tags.setToolTip(confirmation_detail)
        self.prepare_confirm_tags.setAccessibleDescription(confirmation_detail)

    def _automation_controls_changed(self):
        if self._suppress_dirty:
            return
        self._refresh_auto_apply_availability()
        self._applied_automation_preset=""
        blocker=QSignalBlocker(self.automation_mode)
        self.automation_mode.setCurrentIndex(0)
        del blocker
        self._automation_preview_plan=None
        self.apply_automation_preset_button.setEnabled(False)
        self.automation_preset_preview.setText(
            "No preset selected. Your saved or pending settings are unchanged."
        )
        self._update_automation_example()

    @staticmethod
    def _automation_preset_label(mode: str) -> str:
        return {
            "review":"Review first",
            "analyze":"Analyze automatically",
            "maximum":"Maximum safe automation",
        }.get(mode,"Custom settings")

    @staticmethod
    def _components_are_ready(service: DependencyService,names) -> bool:
        return all(
            (component:=service.component(name)) is not None
            and component.installed
            for name in names
        )

    def _automation_preset_plan(
        self,mode: str,*,service: DependencyService | None=None,
        current: dict | None=None,
    ) -> dict:
        service=service or self.dependency_service()
        current=dict(current or self._ai_control_values())
        proposed=dict(current)
        notes=[]
        wd_runtime_ready=self._components_are_ready(
            service,DependencyService.WD_PACKAGES,
        )
        ready_wd=tuple(
            model_id for model_id in WD_TAGGER_MODELS
            if (
                (component:=service.component(
                    service.wd_component_name(model_id)
                )) is not None
                and component.installed
            )
        )
        available_wd=ready_wd if wd_runtime_ready else ()
        unavailable_wd=tuple(
            model_id for model_id in WD_TAGGER_MODELS
            if model_id not in available_wd
        )
        selected=tuple(
            model_id for model_id in current["enabled_wd_taggers"]
            if model_id in available_wd
        )
        if mode=="maximum":
            selected=available_wd
        elif not selected and available_wd:
            selected=available_wd[:1]
        wd_ready=bool(selected)
        if not wd_runtime_ready:
            notes.append(
                "AI Tags cannot be enabled until their reviewed runtime is ready."
            )
        elif not ready_wd:
            notes.append(
                "AI Tags cannot be enabled until at least one Tag model is downloaded: "
                +", ".join(
                    WD_TAGGER_MODELS[model_id][0]
                    for model_id in unavailable_wd
                )+"."
            )
        elif unavailable_wd:
            notes.append(
                "Unavailable AI Tag models are not included: "
                +", ".join(
                    WD_TAGGER_MODELS[model_id][0]
                    for model_id in unavailable_wd
                )+"."
            )

        description_model=str(current["plain_language_model"])
        description_names={
            *DependencyService.JOY_BASE_PACKAGES,
            DependencyService.JOY_HARDWARE_COMPONENT,
            service.plain_language_component_name(description_model),
        }
        if (
            description_model==JOYCAPTION_MODEL
            and current["joycaption_quantization"]=="nf4"
        ):
            description_names.add("BitsAndBytes")
        description_ready=self._components_are_ready(
            service,description_names,
        )
        if not description_ready:
            model_label=PLAIN_LANGUAGE_MODELS.get(
                description_model,(description_model,"","")
            )[0]
            notes.append(
                f"AI Descriptions cannot be enabled until {model_label} and "
                "its reviewed runtime are ready."
            )

        proposed.update({
            "suggest_wd_for_missing_tags":wd_ready,
            "suggest_joycaption_for_missing_descriptions":description_ready,
            "enabled_wd_taggers":selected,
            "auto_run_wd_on_managed_images":False,
            "auto_run_joy_on_managed_images":False,
            "auto_merge_ai_tags":False,
            "prepare_confirm_safe_tags":False,
            "prepare_preferred_descriptions":False,
        })
        proposed["wd_consensus_minimum"]=min(
            int(current["wd_consensus_minimum"]),
            max(1,len(selected)+1),
        )
        if mode in {"analyze","maximum"}:
            proposed["auto_run_wd_on_managed_images"]=wd_ready
            proposed["auto_run_joy_on_managed_images"]=description_ready
        if mode=="maximum":
            proposed["auto_merge_ai_tags"]=wd_ready
            proposed["prepare_confirm_safe_tags"]=wd_ready
            proposed["prepare_preferred_descriptions"]=description_ready
            if wd_ready:
                proposed["wd_consensus_minimum"]=max(
                    2,len(selected)//2+1,
                )
                proposed["prepare_confidence_preset"]="balanced"
                proposed["prepare_tag_vote_minimum"]=int(
                    CONFIDENCE_PRESETS["balanced"]["minimum_tag_votes"]
                )
                proposed["prepare_external_evidence_vote"]=bool(
                    CONFIDENCE_PRESETS["balanced"]["external_evidence_vote"]
                )
        keys=(
            "suggest_wd_for_missing_tags",
            "suggest_joycaption_for_missing_descriptions",
            "enabled_wd_taggers",
            "auto_run_wd_on_managed_images",
            "auto_run_joy_on_managed_images",
            "wd_consensus_minimum","auto_merge_ai_tags",
            "prepare_confidence_preset","prepare_tag_vote_minimum",
            "prepare_external_evidence_vote","prepare_confirm_safe_tags",
            "prepare_preferred_descriptions",
        )
        changes=tuple(
            key for key in keys if current.get(key)!=proposed.get(key)
        )
        return {
            "mode":mode,"values":proposed,"notes":tuple(notes),
            "changes":changes,
        }

    @staticmethod
    def _automation_preview_groups() -> tuple[tuple[str,tuple[str,...]],...]:
        return (
            ("Manual / On-Demand Analysis",(
                "suggest_wd_for_missing_tags",
                "suggest_joycaption_for_missing_descriptions",
                "enabled_wd_taggers",
            )),
            ("After Library Checks",(
                "auto_run_wd_on_managed_images",
                "auto_run_joy_on_managed_images",
                "auto_merge_ai_tags","wd_consensus_minimum",
            )),
            ("During Prepare",(
                "prepare_confirm_safe_tags","prepare_preferred_descriptions",
                "prepare_confidence_preset","prepare_tag_vote_minimum",
                "prepare_external_evidence_vote",
            )),
        )

    @staticmethod
    def _automation_setting_label(key: str) -> str:
        return {
            "suggest_wd_for_missing_tags":"Generate AI Tags",
            "suggest_joycaption_for_missing_descriptions":"Generate AI Descriptions",
            "enabled_wd_taggers":"Enabled AI Tag models",
            "auto_run_wd_on_managed_images":"Generate AI Tags",
            "auto_run_joy_on_managed_images":"Generate AI Descriptions",
            "auto_merge_ai_tags":"Confirm Safe Tags",
            "wd_consensus_minimum":"Minimum Tag votes",
            "prepare_confirm_safe_tags":"Confirm Safe Tags",
            "prepare_preferred_descriptions":"Fill / Upgrade Descriptions",
            "prepare_confidence_preset":"Preparation Confidence",
            "prepare_tag_vote_minimum":"Minimum Tag votes",
            "prepare_external_evidence_vote":"TXT / Metadata vote",
        }.get(key,key.replace("_"," ").title())

    def _automation_preset_preview_text(self,plan: dict) -> str:
        current=self._ai_control_values()
        lines=[]
        for heading,keys in self._automation_preview_groups():
            changed=[key for key in keys if key in plan["changes"]]
            if not changed:
                continue
            lines.append(heading)
            lines.extend(
                "• "+self._automation_setting_label(key)+": "
                +self._ai_change_value(key,current.get(key),exact=True)
                +" → "+self._ai_change_value(
                    key,plan["values"].get(key),exact=True
                )
                for key in changed
            )
        if not lines:
            lines.append(
                "This preset already matches the current controls."
            )
        if plan["notes"]:
            lines.append("Unavailable in this proposal")
            lines.extend("• "+note for note in plan["notes"])
        lines.append(
            "Nothing changes until Stage Preset Changes; nothing is saved until "
            "Save AI Configuration."
        )
        return "\n".join(lines)

    def _preview_automation_preset(self,_index=0):
        mode=str(self.automation_mode.currentData() or "")
        if not mode:
            self._automation_preview_plan=None
            self.apply_automation_preset_button.setEnabled(False)
            self.automation_preset_preview.setText(
                "No preset selected. Your saved or pending settings are unchanged."
            )
            return
        service=self.dependency_service()
        plan=self._automation_preset_plan(mode,service=service)
        self._automation_preview_plan=plan
        preview=self._automation_preset_preview_text(plan)
        self.automation_preset_preview.setText(preview)
        self.automation_preset_preview.setAccessibleDescription(preview)
        self.apply_automation_preset_button.setEnabled(bool(plan["changes"]))
        self._update_ai_overview_readiness(service)

    def _matching_automation_preset(self,values: dict) -> str:
        if (
            getattr(self,"_defer_expensive_initialization",False)
            and not self._component_inventory_ready
        ):
            return ""
        service=getattr(self,"_ai_dependency_service",None)
        for mode in ("review","analyze","maximum"):
            plan=self._automation_preset_plan(
                mode,current=values,service=service,
            )
            if not plan["changes"]:
                return mode
        return ""

    @staticmethod
    def _automation_summary(values: dict) -> str:
        manual=[]
        if values.get("suggest_wd_for_missing_tags"):
            manual.append("AI Tags")
        if values.get("suggest_joycaption_for_missing_descriptions"):
            manual.append("AI Descriptions")
        automatic=[]
        if values.get("auto_run_wd_on_managed_images"):
            automatic.append("AI Tags")
        if values.get("auto_run_joy_on_managed_images"):
            automatic.append("AI Descriptions")
        prepare=[]
        if values.get("prepare_confirm_safe_tags"):
            prepare.append("confirm safe Tags")
        if values.get("prepare_preferred_descriptions"):
            prepare.append("fill or upgrade Descriptions")
        result=(
            "Manual runs: "
            +("generate "+" and ".join(manual) if manual else "use explicit model actions only")
            +".\nAfter library checks: "
            +("generate "+" and ".join(automatic) if automatic else "do not run AI automatically")
            +".\nDuring Prepare: "
            +(" and ".join(prepare) if prepare else "keep AI results for review")+"."
        )
        if values.get("auto_merge_ai_tags"):
            result+=(
                f"\nAutomatic Tag confirmation: require at least "
                f"{values.get('wd_consensus_minimum',1)} independent votes."
            )
        else:
            result+="\nAutomatic Tag confirmation: off."
        return result

    def _update_automation_example(self):
        current=self._ai_control_values()
        committed=self._committed_section_values.get("ai",current)
        saved_mode=self._matching_automation_preset(committed)
        saved_label=(
            "Saved AI configuration matches preset: "
            +self._automation_preset_label(saved_mode)
            if saved_mode else
            "Saved AI configuration: Custom settings"
        )
        self.saved_automation_status.setText(saved_label)
        text=self._automation_summary(committed)
        if current!=committed:
            changes=self._pending_section_changes("ai")
            labels=[label for label,_old,_new,_old_exact,_new_exact in changes]
            if labels:
                shown=", ".join(labels[:4])
                if len(labels)>4:
                    shown+=f", and {len(labels)-4:,} more"
                text+=f"\n\nPending changes (not saved): {shown}."
            else:
                text+="\n\nPending AI configuration changes are not saved."
            pending_summary=self._automation_summary(current)
            saved_summary=self._automation_summary(committed)
            if pending_summary!=saved_summary:
                text+="\n\nBehavior if saved:\n"+pending_summary
        self.automation_example.setText(text)
        self.automation_example.setToolTip(
            text+" Matching completed work is skipped unless you force a re-run. "
            "Generated Descriptions and unsafe, unknown, or conflicting Tags "
            "remain review findings."
        )
        self.automation_example.setAccessibleDescription(
            self.automation_example.toolTip()
        )

    def _update_ai_overview_readiness(
        self,service: DependencyService | None=None,
    ):
        if not hasattr(self,"overview_tag_status"):
            return
        service=service or self.dependency_service()
        ready_models=[
            model_id for model_id in WD_TAGGER_MODELS
            if (
                (component:=service.component(
                    service.wd_component_name(model_id)
                )) is not None
                and component.installed
            )
        ]
        wd_runtime=self._components_are_ready(
            service,DependencyService.WD_PACKAGES,
        )
        selected=sum(
            control.isChecked() for control in self.wd_taggers.values()
        )
        tag_text=(
            f"AI Tags: {len(ready_models):,} of {len(WD_TAGGER_MODELS):,} "
            f"models installed; {selected:,} enabled in the current settings."
            if wd_runtime else
            "AI Tags: the shared runtime needs setup or repair. Installed model "
            "files are kept."
        )
        description_model=str(self.plain_language_model.currentData())
        description_names={
            *DependencyService.JOY_BASE_PACKAGES,
            DependencyService.JOY_HARDWARE_COMPONENT,
            service.plain_language_component_name(description_model),
        }
        if (
            description_model==JOYCAPTION_MODEL
            and str(self.joy_quantization.currentData())=="nf4"
        ):
            description_names.add("BitsAndBytes")
        description_ready=self._components_are_ready(
            service,description_names,
        )
        description_label=PLAIN_LANGUAGE_MODELS.get(
            description_model,(description_model,"","")
        )[0]
        description_text=(
            f"AI Descriptions: {description_label} is installed and selected."
            if description_ready else
            f"AI Descriptions: the selected model, {description_label}, needs "
            "setup or repair."
        )
        self.overview_tag_status.setText(tag_text)
        self.overview_tag_status.setAccessibleDescription(tag_text)
        self.overview_description_status.setText(description_text)
        self.overview_description_status.setAccessibleDescription(
            description_text
        )

    def _apply_automation_preset(self):
        plan=self._automation_preview_plan
        if not plan or not plan["changes"]:
            return
        refreshed=self._automation_preset_plan(
            plan["mode"],service=self.dependency_service(),
        )
        if (
            refreshed["values"]!=plan["values"]
            or refreshed["notes"]!=plan["notes"]
        ):
            self._automation_preview_plan=refreshed
            preview=self._automation_preset_preview_text(refreshed)
            self.automation_preset_preview.setText(preview)
            self.automation_preset_preview.setAccessibleDescription(preview)
            self.apply_automation_preset_button.setEnabled(bool(
                refreshed["changes"]
            ))
            self.header.set_status(
                "AI capability readiness changed; review the refreshed preset preview"
            )
            return
        self._set_ai_control_values(plan["values"])
        self._applied_automation_preset=plan["mode"]
        self._mark_dirty("ai")
        self._update_automation_example()
        self._preview_automation_preset()
        if "ai" in self._dirty_sections:
            self.header.set_status(
                f"{self._automation_preset_label(plan['mode'])} staged; "
                "review and Save AI Configuration"
            )
        else:
            self.header.set_status(
                "Preset already matches the saved AI configuration"
            )

    def _sync_ai_controls(self):
        """Reflect dependency removal immediately instead of retaining stale checked controls."""
        self._suppress_dirty=True
        try:
            self.use_wd.setChecked(self.settings.suggest_wd_for_missing_tags)
            self.use_joy.setChecked(self.settings.suggest_joycaption_for_missing_descriptions)
            self.auto_run_wd.setChecked(self.settings.auto_run_wd_on_managed_images)
            self.auto_run_joy.setChecked(self.settings.auto_run_joy_on_managed_images)
            self.prepare_confirm_tags.setChecked(
                self.settings.prepare_confirm_safe_tags
            )
            self.prepare_descriptions.setChecked(
                self.settings.prepare_preferred_descriptions
            )
            self.prepare_votes.setValue(
                self.settings.prepare_tag_vote_minimum
            )
            self.prepare_external_vote.setChecked(
                self.settings.prepare_external_evidence_vote
            )
            preset_blocker=QSignalBlocker(self.prepare_confidence)
            self.prepare_confidence.setCurrentIndex(max(
                0,self.prepare_confidence.findData(
                    self.settings.prepare_confidence_preset
                ),
            ))
            del preset_blocker
            self.plain_language_model.setCurrentIndex(max(
                0,self.plain_language_model.findData(
                    self.settings.plain_language_model
                ),
            ))
            enabled=set(self.settings.enabled_wd_taggers)
            for model_id,control in self.wd_taggers.items(): control.setChecked(model_id in enabled)
        finally:
            self._suppress_dirty=False
        self._refresh_ai_availability(stage_recommendation=False)
        self._update_prepare_confidence()
        self._plain_language_model_changed(
            self.plain_language_model.currentIndex()
        )
        if "ai" in self._committed_section_values:
            committed=dict(self._committed_section_values["ai"])
            committed.update({
                "suggest_wd_for_missing_tags":self.use_wd.isChecked(),
                "suggest_joycaption_for_missing_descriptions":self.use_joy.isChecked(),
                "auto_run_wd_on_managed_images":self.auto_run_wd.isChecked(),
                "auto_run_joy_on_managed_images":self.auto_run_joy.isChecked(),
                "enabled_wd_taggers":tuple(
                    model_id for model_id,control in self.wd_taggers.items()
                    if control.isChecked()
                ),
            })
            self._committed_section_values["ai"]=committed
            self._mark_dirty("ai")

    def sync_automation_behavior(self):
        """Refresh Automation-owned hidden compatibility controls safely."""
        self._suppress_dirty=True
        try:
            self.use_wd.setChecked(self.settings.suggest_wd_for_missing_tags)
            self.use_joy.setChecked(
                self.settings.suggest_joycaption_for_missing_descriptions
            )
            self.auto_run_wd.setChecked(
                self.settings.auto_run_wd_on_managed_images
            )
            self.auto_run_joy.setChecked(
                self.settings.auto_run_joy_on_managed_images
            )
            self.wd_general.setValue(self.settings.wd_general_threshold)
            self.wd_character.setValue(self.settings.wd_character_threshold)
            self.wd_rating.setValue(self.settings.wd_rating_threshold)
            self.wd_consensus.setValue(self.settings.wd_consensus_minimum)
            self.auto_merge_ai.setChecked(self.settings.auto_merge_ai_tags)
            self.prepare_confirm_tags.setChecked(
                self.settings.prepare_confirm_safe_tags
            )
            self.prepare_descriptions.setChecked(
                self.settings.prepare_preferred_descriptions
            )
            self.prepare_votes.setValue(self.settings.prepare_tag_vote_minimum)
            self.prepare_external_vote.setChecked(
                self.settings.prepare_external_evidence_vote
            )
            preset_blocker=QSignalBlocker(self.prepare_confidence)
            self.prepare_confidence.setCurrentIndex(max(
                0,self.prepare_confidence.findData(
                    self.settings.prepare_confidence_preset
                ),
            ))
            del preset_blocker
        finally:
            self._suppress_dirty=False
        policy_values={
            key:getattr(self.settings,key)
            for key in AUTOMATION_BEHAVIOR_SETTING_KEYS
        }
        self._automation_behavior_values=policy_values
        if "ai" in self._committed_section_values:
            current=self._ai_control_values()
            committed=dict(self._committed_section_values["ai"])
            for key in AUTOMATION_BEHAVIOR_SETTING_KEYS:
                if key in current:
                    committed[key]=current[key]
            self._committed_section_values["ai"]=committed
        self._refresh_auto_apply_availability()
        self._update_prepare_confidence()
        self._update_automation_example()
        self._mark_dirty("ai")

    @staticmethod
    def _model_status_html(
        model_id: str, *, ready: bool, seconds: float | None, stale: bool=False,
    ) -> str:
        info=WD_TAGGER_MODEL_CARDS[model_id]
        availability="Ready" if ready else '<a href="action:download">Download Needed</a>'
        if seconds is not None:
            speed=f"Local speed: ~{seconds:.2f}s/image"
        elif stale:
            speed='Local speed: <a href="action:benchmark">Benchmark Outdated</a>'
        else:
            speed='Local speed: <a href="action:benchmark">Run Benchmark</a>'
        return f"{availability} · {speed} · <a href=\"{info['source_url']}\">{info['source_label']}</a>"

    def _model_status_link(self,model_id: str,href: str):
        if href=="action:download": self.manage_ai_components("wd",model_id)
        elif href=="action:benchmark": self.benchmark_ai([model_id])
        else: QDesktopServices.openUrl(QUrl(href))

    @staticmethod
    def _joy_benchmark_status_text(settings,ready: bool) -> tuple[str,str]:
        model_id=str(
            getattr(settings,"plain_language_model",JOYCAPTION_MODEL)
            or JOYCAPTION_MODEL
        )
        model_label=PLAIN_LANGUAGE_MODELS.get(model_id,(model_id,"",""))[0]
        benchmark=(getattr(settings,"ai_model_benchmarks",{}) or {}).get(model_id,{})
        if not ready:
            return (
                "Setup needed before AI Description speed can be measured.",
                f"Set up {model_label} in Components, then run this benchmark.",
            )
        benchmark_current=(
            joy_benchmark_is_current(settings,benchmark)
            if model_id==JOYCAPTION_MODEL else
            plain_language_benchmark_is_current(settings,model_id,benchmark)
        )
        if benchmark and not benchmark_current:
            return (
                "Benchmark outdated — the model, generation settings, prompt, or runtime changed.",
                "Run the benchmark again to update AI Description time estimates.",
            )
        if not benchmark:
            return (
                "Not benchmarked — AI Description processing time cannot be estimated yet.",
                f"Measures the current saved {model_label} configuration on up to four local images.",
            )
        seconds=float(benchmark.get("seconds_per_image",0) or 0)
        tokens=float(benchmark.get("tokens_per_second",0) or 0)
        device=benchmark.get("device",{}) if isinstance(benchmark.get("device",{}),dict) else {}
        device_label=(
            str(device.get("name") or "NVIDIA GPU")
            if device.get("kind")=="cuda" else
            str(device.get("kind") or "unknown").upper()
        )
        generation=(
            benchmark.get("configuration",{}).get("generation",{})
            if isinstance(benchmark.get("configuration",{}),dict) else {}
        )
        beams=max(1,int(generation.get("num_beams",1) or 1))
        max_tokens=max(1,int(generation.get("max_new_tokens",1) or 1))
        precision=str(device.get("precision") or "").upper()
        quantization=str(
            device.get("quantization")
            or generation.get("quantization")
            or "bf16"
        )
        memory_mode=(
            "4-bit NF4" if quantization=="nf4"
            else "Full BF16"
        )
        text=f"Measured locally: ~{seconds:.1f}s/image"
        if tokens>0:
            text+=f" • ~{tokens:.1f} generated tokens/second"
        text+=(
            f" on {device_label}"
            f" • {memory_mode}"
            f" • {beams:,} beam{'s' if beams != 1 else ''}"
            f" • up to {max_tokens:,} tokens."
        )
        tooltip=(
            f"Cold first image: ~{float(benchmark.get('cold_seconds',0) or 0):.1f}s\n"
            f"Warm range: ~{float(benchmark.get('warm_seconds_min',seconds) or seconds):.1f}"
            f"–{float(benchmark.get('warm_seconds_max',seconds) or seconds):.1f}s/image\n"
            f"Warm measurements: {int(benchmark.get('warm_measurement_count',0) or 0):,}\n"
            f"Actual execution: {device_label}"
            f"{f' ({precision})' if precision else ''}\n"
            f"Model memory mode: {memory_mode}\n"
            f"Generation: {beams:,} beam{'s' if beams != 1 else ''}, "
            f"up to {max_tokens:,} tokens\n"
            +(
                "Multiple beams can be substantially slower and use more GPU memory.\n"
                if beams>1 else ""
            )+
            "Speed only; caption quality is not scored."
        )
        return text,tooltip

    def _refresh_joy_benchmark_status(
        self,service: DependencyService | None=None,*,ready: bool | None=None,
    ):
        if ready is None:
            if not self._component_inventory_ready and service is None:
                ready=False
            else:
                service=service or self.dependency_service()
                ready=service.capability_ready("joy")
        text,tooltip=self._joy_benchmark_status_text(self.settings,ready)
        self.joy_benchmark_status.setText(text)
        self.joy_benchmark_status.setToolTip(tooltip)
        self.joy_benchmark_status.setAccessibleName(f"AI Description model speed: {text}")
        self.joy_benchmark_button.setText(
            "Run AI Description Benchmark Again…" if ready and
            bool((self.settings.ai_model_benchmarks or {}).get(
                self.settings.plain_language_model
            ))
            else "Benchmark AI Description Model…"
        )

    @staticmethod
    def _benchmark_samples(selected,limit: int=8) -> list[Path]:
        unique=list(dict.fromkeys(str(value) for value in selected if str(value)))
        limit=max(1,int(limit))
        if len(unique)>limit:
            indexes=[round(index*(len(unique)-1)/(limit-1)) for index in range(limit)] if limit>1 else [0]
            unique=[unique[index] for index in indexes]
        return [Path(value) for value in unique]

    @staticmethod
    def _benchmark_confirmation_text(model_count: int) -> str:
        model_label="model" if model_count==1 else "models"
        return (
            f"This speed benchmark will test {model_count:,} enabled AI Tag "
            f"{model_label} on both NVIDIA CUDA (when available) and CPU. It "
            "measures batches of 1, 2, 4, 8, and 16 images twice.\n\n"
            "It may take several minutes or longer, especially with multiple "
            "models or during the CPU measurements. You can cancel safely after "
            "the current batch finishes; existing saved benchmark results will "
            "be kept.\n\n"
            "The images you choose remain on this computer. Saved benchmark "
            "records contain timing and configuration details plus whether CPU "
            "and GPU outputs agreed—not image names, paths, or generated tags.\n\n"
            "Continue to image selection?"
        )

    def _prepare_cold_ai_work(self,should_cancel):
        if should_cancel():
            raise InterruptedError("AI comparison cancelled safely")
        release=getattr(self.state,"prepare_ai_exclusive_work",None)
        if callable(release):
            release()

    def benchmark_ai(self,model_ids=None):
        if (
            self._benchmark_task is not None
            or getattr(self,"_joy_benchmark_task",None) is not None
            or getattr(self,"_joy_quality_task",None) is not None
            or getattr(self,"_ai_evaluation_task",None) is not None
        ): return
        requested=[] if isinstance(model_ids,bool) or model_ids is None else list(model_ids)
        enabled=requested or [model_id for model_id,control in self.wd_taggers.items() if control.isChecked()]
        if not enabled: QMessageBox.information(self,"Choose an AI Tag Model","Enable at least one AI Tag model first."); return
        service=self.dependency_service()
        missing=[model_id for model_id in enabled
                 if not service.component(service.wd_component_name(model_id)).installed]
        if missing:
            self.dependency_panel.focus_capability("wd",missing)
            if self.dependency_panel.setup_wd():
                self.show_section("ai","components")
            return
        if QMessageBox.question(
            self,"Run hardware speed benchmark?",
            self._benchmark_confirmation_text(len(enabled)),
        )!=QMessageBox.Yes:
            return
        selected=QFileDialog.getOpenFileNames(
            self,"Choose up to 8 representative images","",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp)",
        )[0]
        samples=self._benchmark_samples(selected)
        if not samples: return
        if not self._begin_operation(
            "setup_benchmark",f"Benchmarking {len(enabled):,} AI Tag model(s)",
            resource="gpu",
        ): return
        self._benchmark_progress=QProgressDialog(
            f"Preparing a local benchmark with {len(samples):,} sample images…",
            "Cancel Safely",0,len(enabled),self,
        )
        self._benchmark_progress.setWindowTitle("Hardware benchmark")
        self._benchmark_progress.setMinimumDuration(0)
        self._benchmark_progress.canceled.connect(self._cancel_benchmark)
        self._benchmark_bar=self._benchmark_progress.findChild(QProgressBar)
        if self._benchmark_bar is not None: set_progress_bar(self._benchmark_bar,0,len(enabled),detail="Preparing benchmark")
        self._benchmark_progress.show()
        thresholds=(self.settings.wd_general_threshold,self.settings.wd_character_threshold,self.settings.wd_rating_threshold)
        def work(progress,should_cancel):
            self._prepare_cold_ai_work(should_cancel)
            results={}
            for index,model_id in enumerate(enabled,1):
                if should_cancel(): raise InterruptedError("Benchmark cancelled safely")
                model_name=WD_TAGGER_MODELS.get(model_id,(model_id,""))[0]
                progress(index-1,len(enabled),f"Benchmarking {model_name}")
                model_dir=service.wd_model_dir_for(model_id)
                results[model_id]=WDEva02Backend(
                    model_dir,*thresholds,model_id=model_id,
                ).benchmark(samples,should_cancel=should_cancel)
                progress(index,len(enabled),f"Finished {model_name}")
            return results
        task=CancelableProgressCallTask(work); self._benchmark_task=task
        task.signals.progress.connect(self._benchmark_step)
        task.signals.complete.connect(self._benchmark_complete)
        task.signals.cancelled.connect(self._benchmark_cancelled)
        task.signals.failed.connect(self._benchmark_failed); self.pool.start(task)

    def _cancel_benchmark(self):
        if self._benchmark_task is None: return
        self._benchmark_task.cancel()
        if self.state is not None:
            self.state.request_long_operation_cancel("setup_benchmark")
        self.header.set_status("Cancelling hardware benchmark safely after the current model call…")

    def _benchmark_step(self,index,total,label):
        if self.state is not None: self.state.update_long_operation("setup_benchmark",index,total,label)
        self._benchmark_progress.setLabelText(label)
        if self._benchmark_bar is not None: set_progress_bar(self._benchmark_bar,index,total,detail=label)
        else: self._benchmark_progress.setValue(index)

    def _benchmark_complete(self,results):
        self._benchmark_task=None; self._benchmark_progress.close(); self._finish_operation("setup_benchmark")
        self.settings.ai_model_benchmarks.update(results); self.settings.save()
        recommendation=recommend_wd_pipeline_settings(results)
        provider=str(recommendation["provider"]); batch_size=int(recommendation["batch_size"])
        self.wd_provider.setCurrentIndex(max(0,self.wd_provider.findData(provider))); self.wd_batch_size.setValue(batch_size)
        lines=[]
        for model_id,result in results.items():
            batch=result.get("measurements",{}).get(provider,{}).get("batches",{}).get(str(batch_size),{})
            seconds=float(batch.get("seconds_per_image") or result["seconds_per_image"])
            control=self.wd_taggers[model_id]
            self.wd_tagger_status[model_id].setText(self._model_status_html(model_id,ready=True,seconds=seconds))
            lines.append(f"{control.property('modelLabel')}: ~{seconds:.2f}s/image on {provider.upper()}")
        sample_count=max((int(value.get("sample_count",1)) for value in results.values()),default=1)
        repeats=max((int(value.get("repetitions",1)) for value in results.values()),default=1)
        lines.append(
            f"Recommended for the complete enabled pipeline: {provider.upper()}, "
            f"Images per model call: {batch_size}"
        )
        lines.append(f"Measured locally with {sample_count} sample images and {repeats} timing repetitions.")
        lines.append("This measures speed only; it does not change models, tag thresholds, or Image Knowledge.")
        if not bool(recommendation.get("output_agreement",True)):
            lines.append("CPU and GPU crossed at least one configured tag threshold differently; review the benchmark detail before choosing a device.")
        self.header.set_status("Hardware benchmark complete • recommendation selected but not yet saved")
        self.header.status.setToolTip("\n".join(lines))

    def _benchmark_cancelled(self):
        self._benchmark_task=None; self._benchmark_progress.close(); self._finish_operation("setup_benchmark")
        self.header.set_status("Hardware benchmark cancelled safely • existing results were kept")

    def _benchmark_failed(self,message):
        self._benchmark_task=None; self._benchmark_progress.close(); self._finish_operation("setup_benchmark"); QMessageBox.warning(self,"Hardware benchmark could not finish",message)

    @staticmethod
    def _joy_benchmark_confirmation_text(settings) -> str:
        beams=max(1,int(getattr(settings,"joycaption_num_beams",4)))
        tokens=max(1,int(getattr(settings,"joycaption_max_new_tokens",256)))
        model_id=str(
            getattr(settings,"plain_language_model",JOYCAPTION_MODEL)
            or JOYCAPTION_MODEL
        )
        model_label=PLAIN_LANGUAGE_MODELS.get(model_id,(model_id,"",""))[0]
        quantization=(
            str(getattr(settings,"joycaption_quantization","bf16"))
            if model_id==JOYCAPTION_MODEL else "bf16"
        )
        memory_mode="4-bit NF4" if quantization=="nf4" else "full BF16"
        return (
            f"This speed benchmark will run {model_label} with your "
            f"current saved settings ({memory_mode}, {beams:,} beam"
            f"{'s' if beams != 1 else ''}, up to {tokens:,} generated tokens). "
            "It measures one cold first image, then two warm passes over up to "
            "four representative images.\n\n"
            "AI Description generation is much heavier than AI Tag generation. "
            "This may take many minutes or longer, especially on slower GPUs or "
            "with longer descriptions, full BF16 weights, or multiple beams. "
            "You can cancel safely after the current image finishes.\n\n"
            "The images you choose remain on this computer. Saved benchmark "
            "records contain only timing, token-count, device, model, and "
            "configuration details—not image names, paths, captions, or pixels.\n\n"
            "This measures speed only. It does not score caption accuracy or "
            "change your generation settings.\n\n"
            "Continue to image selection?"
        )

    def benchmark_joy(self):
        if (
            getattr(self,"_benchmark_task",None) is not None
            or self._joy_benchmark_task is not None
            or getattr(self,"_joy_quality_task",None) is not None
            or getattr(self,"_ai_evaluation_task",None) is not None
        ):
            return
        if "ai" in self._dirty_sections:
            QMessageBox.information(
                self,"Save AI Configuration first",
                "The benchmark measures the saved AI Description configuration "
                "used by image analysis. Save AI Configuration, then run the "
                "benchmark again.",
            )
            self._focus_ai_setting(self.save_ai_button)
            return
        service=self.dependency_service()
        if not service.capability_ready("joy"):
            self.show_section("ai","components")
            self.dependency_panel.focus_capability("joy")
            self.dependency_panel.setup_joy()
            return
        if QMessageBox.question(
            self,"Run AI Description Speed Benchmark?",
            self._joy_benchmark_confirmation_text(self.settings),
        )!=QMessageBox.Yes:
            return
        selected=QFileDialog.getOpenFileNames(
            self,"Choose up to 4 representative images","",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp)",
        )[0]
        samples=self._benchmark_samples(selected,4)
        if not samples:
            return
        if not self._begin_operation(
            "setup_benchmark","Benchmarking the AI Description model",resource="gpu",
        ):
            return
        total=1+2*len(samples)
        self._joy_benchmark_progress=QProgressDialog(
            f"Preparing the AI Description benchmark with {len(samples):,} sample images…",
            "Cancel Safely",0,total,self,
        )
        self._joy_benchmark_progress.setWindowTitle("AI Description Speed Benchmark")
        self._joy_benchmark_progress.setMinimumDuration(0)
        self._joy_benchmark_progress.canceled.connect(self._cancel_joy_benchmark)
        self._joy_benchmark_bar=self._joy_benchmark_progress.findChild(QProgressBar)
        if self._joy_benchmark_bar is not None:
            set_progress_bar(
                self._joy_benchmark_bar,0,total,
                detail="Preparing AI Description benchmark",unit="benchmark steps",
            )
        self._joy_benchmark_progress.show()
        settings=self.settings
        backend=configured_plain_language_backend(settings)
        def work(progress,should_cancel):
            self._prepare_cold_ai_work(should_cancel)
            return backend.benchmark(
                samples,repeats=2,should_cancel=should_cancel,progress=progress,
            )
        task=CancelableProgressCallTask(work); self._joy_benchmark_task=task
        task.signals.progress.connect(self._joy_benchmark_step)
        task.signals.complete.connect(self._joy_benchmark_complete)
        task.signals.cancelled.connect(self._joy_benchmark_cancelled)
        task.signals.failed.connect(self._joy_benchmark_failed)
        self.pool.start(task)

    def _cancel_joy_benchmark(self):
        if self._joy_benchmark_task is None:
            return
        self._joy_benchmark_task.cancel()
        if self.state is not None:
            self.state.request_long_operation_cancel("setup_benchmark")
        self.header.set_status(
            "Cancelling AI Description benchmark safely after the current image…"
        )

    def _joy_benchmark_step(self,index,total,label):
        if self.state is not None:
            self.state.update_long_operation("setup_benchmark",index,total,label)
        self._joy_benchmark_progress.setLabelText(label)
        if self._joy_benchmark_bar is not None:
            set_progress_bar(
                self._joy_benchmark_bar,index,total,detail=label,unit="benchmark steps",
            )
        else:
            self._joy_benchmark_progress.setValue(index)

    def _joy_benchmark_complete(self,result):
        self._joy_benchmark_task=None
        self._joy_benchmark_progress.close()
        self._finish_operation("setup_benchmark")
        self.settings.ai_model_benchmarks[
            self.settings.plain_language_model
        ]=result
        self.settings.save()
        self._refresh_joy_benchmark_status()
        seconds=float(result.get("seconds_per_image",0) or 0)
        tokens=float(result.get("tokens_per_second",0) or 0)
        device=result.get("device",{}) if isinstance(result.get("device",{}),dict) else {}
        generation=result.get("configuration",{}).get("generation",{})
        beams=max(1,int(generation.get("num_beams",1) or 1))
        quantization=str(device.get("quantization") or generation.get("quantization") or "bf16")
        memory_mode="4-bit NF4" if quantization=="nf4" else "Full BF16"
        self.header.set_status("AI Description speed benchmark complete")
        self.header.status.setToolTip(
            f"Warm speed: ~{seconds:.1f}s/image\n"
            f"Generated-token speed: ~{tokens:.1f} tokens/second\n"
            f"Cold first image: ~{float(result.get('cold_seconds',0) or 0):.1f}s\n"
            f"Actual execution: {device.get('name') or str(device.get('kind') or 'unknown').upper()}\n"
            f"Model memory mode: {memory_mode}\n"
            f"Generation: {beams:,} beam{'s' if beams != 1 else ''}\n"
            "Speed only; model settings and Image Knowledge were not changed."
        )

    def _joy_benchmark_cancelled(self):
        self._joy_benchmark_task=None
        self._joy_benchmark_progress.close()
        self._finish_operation("setup_benchmark")
        self.header.set_status(
            "AI Description benchmark cancelled safely • existing result was kept"
        )

    def _joy_benchmark_failed(self,message):
        self._joy_benchmark_task=None
        self._joy_benchmark_progress.close()
        self._finish_operation("setup_benchmark")
        QMessageBox.warning(
            self,"AI Description Benchmark Could Not Finish",message,
        )

    @staticmethod
    def _joy_quality_confirmation_text(settings) -> str:
        beams=max(1,int(getattr(settings,"joycaption_num_beams",4)))
        tokens=max(1,int(getattr(settings,"joycaption_max_new_tokens",256)))
        return (
            "This controlled comparison will generate two captions for each "
            "selected image: one with Full BF16 and one with 4-bit NF4. Both "
            f"modes use the same saved instructions, {beams:,} beam"
            f"{'s' if beams != 1 else ''}, and up to {tokens:,} generated "
            "tokens. Varied/random wording must remain off so a sampling "
            "difference is not mistaken for a memory-mode difference.\n\n"
            "Choose up to four representative images. Full BF16 and NF4 load "
            "separately, so the comparison may take many minutes or longer; "
            "Full BF16 may be substantially slower or may not fit on a "
            "lower-memory GPU. You can cancel safely after the current caption "
            "finishes.\n\n"
            "The images and generated captions remain on this computer and "
            "exist only for the review window. Nothing is saved to benchmark "
            "history, model settings, Image Knowledge, TXT, or Metadata.\n\n"
            "Continue to image selection?"
        )

    @staticmethod
    def _joy_length_quality_confirmation_text(settings) -> str:
        beams=max(1,int(getattr(settings,"joycaption_num_beams",4)))
        tokens=max(1,int(getattr(settings,"joycaption_max_new_tokens",256)))
        return (
            "This controlled comparison will generate two 4-bit NF4 captions "
            "for each selected image: one with the saved maximum of "
            f"{tokens:,} generated tokens and one with the 100-token limit "
            "normally used by TagGUI. Both use the same saved model, "
            f"instructions, {beams:,} beam"
            f"{'s' if beams != 1 else ''}, penalties, and GPU. "
            "Varied/random wording must remain off so sampling is not mistaken "
            "for a length-limit difference.\n\n"
            "Choose up to four representative images. The NF4 model loads once "
            "and generates both controlled captions, so the comparison may "
            "take several minutes or longer. You can cancel safely after the "
            "current caption finishes.\n\n"
            "The images and generated captions remain on this computer and "
            "exist only for the review window. Nothing is saved to benchmark "
            "history, model settings, Image Knowledge, TXT, or Metadata.\n\n"
            "Continue to image selection?"
        )

    def _joy_quality_backend(
        self,service: DependencyService,*,quantization: str,
    ) -> JoyCaptionBackend:
        settings=self.settings
        return JoyCaptionBackend(
            service.joy_model_dir,prompt=settings.joycaption_prompt,
            max_new_tokens=settings.joycaption_max_new_tokens,
            min_new_tokens=settings.joycaption_min_new_tokens,
            num_beams=settings.joycaption_num_beams,
            length_penalty=settings.joycaption_length_penalty,
            do_sample=settings.joycaption_do_sample,
            temperature=settings.joycaption_temperature,
            top_k=settings.joycaption_top_k,top_p=settings.joycaption_top_p,
            repetition_penalty=settings.joycaption_repetition_penalty,
            no_repeat_ngram_size=settings.joycaption_no_repeat_ngram_size,
            gpu_index=settings.joycaption_gpu_index,
            quantization=quantization,
        )

    def _start_joy_quality_comparison(
        self,samples,backend: JoyCaptionBackend,*,
        comparison_method: str,
        operation_status: str,
        progress_title: str,
        progress_detail: str,
    ) -> None:
        if not self._begin_operation(
            "setup_quality_comparison",operation_status,resource="gpu",
        ):
            return
        self._joy_quality_samples=tuple(samples)
        total=2*len(samples)
        self._joy_quality_progress=QProgressDialog(
            f"{progress_detail} with "
            f"{len(samples):,} sample images…",
            "Cancel Safely",0,total,self,
        )
        self._joy_quality_progress.setWindowTitle(progress_title)
        self._joy_quality_progress.setMinimumDuration(0)
        self._joy_quality_progress.canceled.connect(
            self._cancel_joy_quality
        )
        self._joy_quality_bar=self._joy_quality_progress.findChild(
            QProgressBar
        )
        if self._joy_quality_bar is not None:
            set_progress_bar(
                self._joy_quality_bar,0,total,
                detail=progress_detail,unit="captions",
            )
        self._joy_quality_progress.show()

        def work(progress,should_cancel):
            self._prepare_cold_ai_work(should_cancel)
            compare=getattr(backend,comparison_method)
            return compare(
                samples,should_cancel=should_cancel,progress=progress,
            )

        task=CancelableProgressCallTask(work)
        self._joy_quality_task=task
        task.signals.progress.connect(self._joy_quality_step)
        task.signals.complete.connect(self._joy_quality_complete)
        task.signals.cancelled.connect(self._joy_quality_cancelled)
        task.signals.failed.connect(self._joy_quality_failed)
        self.pool.start(task)

    def compare_joy_quality(self):
        if (
            getattr(self,"_benchmark_task",None) is not None
            or getattr(self,"_joy_benchmark_task",None) is not None
            or self._joy_quality_task is not None
            or getattr(self,"_ai_evaluation_task",None) is not None
        ):
            return
        service=self.dependency_service()
        if (
            not service.capability_ready("joy")
            or not self._nf4_component_ready(service)
        ):
            QMessageBox.information(
                self,
                "Set up both memory modes",
                "Caption quality comparison requires the ready AI Description "
                "model, supported NVIDIA hardware, and the reviewed 4-bit NF4 "
                "component. Open Components to repair or finish that setup.",
            )
            self.open_dependencies("joy")
            return
        if "ai" in self._dirty_sections:
            QMessageBox.information(
                self,"Save AI Configuration first",
                "The comparison holds every saved AI Description setting fixed "
                "except model memory mode. Save AI Configuration, then compare "
                "caption quality again.",
            )
            self._focus_ai_setting(self.save_ai_button)
            return
        if bool(getattr(self.settings,"joycaption_do_sample",False)):
            QMessageBox.information(
                self,"Use deterministic wording first",
                "Turn off Use varied/random wording in Advanced Generation and "
                "choose Save AI Configuration before comparing BF16 with NF4. "
                "Otherwise random sampling would confound the comparison.",
            )
            self._focus_ai_setting(self.joy_sample)
            return
        if QMessageBox.question(
            self,"Compare BF16 and NF4 caption quality?",
            self._joy_quality_confirmation_text(self.settings),
        )!=QMessageBox.Yes:
            return
        selected=QFileDialog.getOpenFileNames(
            self,"Choose up to 4 representative images","",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp)",
        )[0]
        samples=self._benchmark_samples(selected,4)
        if not samples:
            return
        backend=self._joy_quality_backend(
            service,
            quantization=str(
                getattr(self.settings,"joycaption_quantization","bf16")
            ),
        )
        self._start_joy_quality_comparison(
            samples,backend,
            comparison_method="compare_memory_modes",
            operation_status="Comparing BF16 and NF4 caption quality",
            progress_title="BF16 and NF4 caption quality",
            progress_detail="Preparing caption comparison",
        )

    def compare_joy_length_quality(self):
        if (
            getattr(self,"_benchmark_task",None) is not None
            or getattr(self,"_joy_benchmark_task",None) is not None
            or self._joy_quality_task is not None
            or getattr(self,"_ai_evaluation_task",None) is not None
        ):
            return
        service=self.dependency_service()
        if (
            not service.capability_ready("joy")
            or not self._nf4_component_ready(service)
        ):
            QMessageBox.information(
                self,
                "Set up 4-bit NF4 first",
                "Length-limit comparison requires the ready AI Description "
                "model, supported NVIDIA hardware, and the reviewed 4-bit NF4 "
                "component. Open Components to repair or finish that setup.",
            )
            self.open_dependencies("joy")
            return
        if "ai" in self._dirty_sections:
            QMessageBox.information(
                self,"Save AI Configuration first",
                "The comparison holds every saved AI Description setting fixed "
                "except the maximum output length. Save AI Configuration, then "
                "compare length limits again.",
            )
            self._focus_ai_setting(self.save_ai_button)
            return
        if bool(getattr(self.settings,"joycaption_do_sample",False)):
            QMessageBox.information(
                self,"Use deterministic wording first",
                "Turn off Use varied/random wording in Advanced Generation and "
                "choose Save AI Configuration before comparing output lengths. "
                "Otherwise random sampling would confound the comparison.",
            )
            self._focus_ai_setting(self.joy_sample)
            return
        tokens=max(
            1,int(getattr(self.settings,"joycaption_max_new_tokens",256))
        )
        if tokens<=100:
            QMessageBox.information(
                self,"Save a longer comparison limit first",
                "Set Maximum AI Description length above 100 and "
                "choose Save AI Configuration before comparing it with the 100-token "
                "TagGUI reference.",
            )
            self._focus_ai_setting(self.joy_tokens)
            return
        minimum=max(
            0,int(getattr(self.settings,"joycaption_min_new_tokens",0))
        )
        if minimum>100:
            QMessageBox.information(
                self,"Lower the minimum length first",
                "Set Minimum length to 100 or fewer generated tokens and save "
                "Model Settings before running the controlled comparison.",
            )
            self._focus_ai_setting(self.joy_min_tokens)
            return
        if QMessageBox.question(
            self,"Compare saved and TagGUI length limits?",
            self._joy_length_quality_confirmation_text(self.settings),
        )!=QMessageBox.Yes:
            return
        selected=QFileDialog.getOpenFileNames(
            self,"Choose up to 4 representative images","",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp)",
        )[0]
        samples=self._benchmark_samples(selected,4)
        if not samples:
            return
        backend=self._joy_quality_backend(
            service,quantization="nf4",
        )
        self._start_joy_quality_comparison(
            samples,backend,
            comparison_method="compare_token_limits",
            operation_status=(
                f"Comparing {tokens:,}- and 100-token caption quality"
            ),
            progress_title="NF4 caption length limits",
            progress_detail=(
                f"Preparing {tokens:,}- versus 100-token comparison"
            ),
        )

    def _cancel_joy_quality(self):
        if self._joy_quality_task is None:
            return
        self._joy_quality_task.cancel()
        if self.state is not None:
            self.state.request_long_operation_cancel(
                "setup_quality_comparison"
            )
        self.header.set_status(
            "Cancelling caption comparison safely after the current caption…"
        )

    def _joy_quality_step(self,index,total,label):
        if self.state is not None:
            self.state.update_long_operation(
                "setup_quality_comparison",index,total,label,
            )
        self._joy_quality_progress.setLabelText(label)
        if self._joy_quality_bar is not None:
            set_progress_bar(
                self._joy_quality_bar,index,total,
                detail=label,unit="captions",
            )
        else:
            self._joy_quality_progress.setValue(index)

    def _joy_quality_complete(self,result):
        self._joy_quality_task=None
        self._joy_quality_progress.close()
        self._finish_operation("setup_quality_comparison")
        samples=self._joy_quality_samples
        self._joy_quality_samples=()
        self.header.set_status(
            "Caption comparison complete • temporary results ready for review"
        )
        dialog=JoyCaptionQualityDialog(
            samples,result,self.settings,self,
        )
        dialog.exec()
        self.header.set_status(
            "Caption comparison closed • temporary results discarded"
        )

    def _joy_quality_cancelled(self):
        self._joy_quality_task=None
        self._joy_quality_samples=()
        self._joy_quality_progress.close()
        self._finish_operation("setup_quality_comparison")
        self.header.set_status(
            "Caption comparison cancelled safely • no results were kept"
        )

    def _joy_quality_failed(self,message):
        self._joy_quality_task=None
        self._joy_quality_samples=()
        self._joy_quality_progress.close()
        self._finish_operation("setup_quality_comparison")
        QMessageBox.warning(
            self,
            "Caption quality comparison could not finish",
            f"{message}\n\nNo comparison, settings, or image data were saved.",
        )

    @staticmethod
    def _ai_evaluation_model_label(model_id: str) -> str:
        if model_id in WD_TAGGER_MODELS:
            return WD_TAGGER_MODELS[model_id][0]
        if model_id in PLAIN_LANGUAGE_MODELS:
            return PLAIN_LANGUAGE_MODELS[model_id][0]
        return model_id

    @staticmethod
    def _ai_evaluation_confirmation_text(
        model_ids,blocked=(),
    ) -> str:
        labels=[SetupPage._ai_evaluation_model_label(value) for value in model_ids]
        noun="model" if len(labels)==1 else "models"
        text=(
            f"This review runs the {len(labels):,} supported local AI {noun} "
            "currently Ready on this computer on the same sample of up to four "
            "images. Models that are not Ready are skipped; nothing is downloaded, "
            "installed, or repaired by this evaluation.\n\nModels in this run:\n• "
            +"\n• ".join(labels)
        )
        if blocked:
            text+=(
                "\n\nInstalled models skipped with the current saved settings:\n• "
                +"\n• ".join(
                    f"{SetupPage._ai_evaluation_model_label(model_id)} — {reason}"
                    for model_id,reason in blocked
                )
            )
        return text+(
            "\n\n"
            "It may take many minutes or longer because each model loads and "
            "runs locally; cancellation finishes after the current model call.\n\n"
            "Choose a small varied sample that you can judge confidently, "
            "including different subjects, settings, and content ratings when "
            "appropriate. The review asks about useful visible accuracy, material "
            "unsupported claims, and content-safety handling.\n\n"
            "Selected paths, pixels, generated tags, and generated descriptions "
            "exist only for the evaluation window. CollectiGen changes no model "
            "settings, Image Knowledge, TXT, Metadata, or benchmark history. You "
            "can copy only an anonymized aggregate summary after every result is "
            "reviewed.\n\nContinue to image selection?"
        )

    @staticmethod
    def _ai_evaluation_required_components(
        service: DependencyService,settings,model_id: str,
    ) -> set[str]:
        if model_id in WD_TAGGER_MODELS:
            return {
                *DependencyService.WD_PACKAGES,
                service.wd_component_name(model_id),
            }
        if model_id not in PLAIN_LANGUAGE_MODELS:
            raise KeyError(model_id)
        names={
            *DependencyService.JOY_BASE_PACKAGES,
            DependencyService.JOY_HARDWARE_COMPONENT,
            service.plain_language_component_name(model_id),
        }
        if (
            model_id==JOYCAPTION_MODEL
            and str(getattr(settings,"joycaption_quantization","bf16"))=="nf4"
        ):
            names.add("BitsAndBytes")
        return names

    @staticmethod
    def _ai_evaluation_model_availability(
        service: DependencyService,settings,
    ) -> tuple[tuple[str,...],tuple[tuple[str,str],...]]:
        ready=[]; blocked=[]
        for model_id in (*WD_TAGGER_MODELS,*PLAIN_LANGUAGE_MODELS):
            component_name=(
                service.wd_component_name(model_id)
                if model_id in WD_TAGGER_MODELS
                else service.plain_language_component_name(model_id)
            )
            component=service.component(component_name)
            if component is None or not component.installed:
                continue
            reasons=[]
            if (
                model_id in WD_TAGGER_MODELS
                and str(getattr(settings,"wd_execution_provider","auto"))=="cpu"
            ):
                reasons.append("AI Tag processing is set to CPU")
            if (
                model_id in PLAIN_LANGUAGE_MODELS
                and bool(getattr(settings,"joycaption_do_sample",False))
            ):
                reasons.append("varied/random wording is enabled")
            missing=sorted(
                name for name in SetupPage._ai_evaluation_required_components(
                    service,settings,model_id,
                )
                if name!=component_name
                and (
                    (required:=service.component(name)) is None
                    or not required.installed
                )
            )
            if missing:
                reasons.append("runtime needs repair: "+", ".join(missing))
            if reasons:
                blocked.append((model_id,"; ".join(reasons)))
            else:
                ready.append(model_id)
        return tuple(ready),tuple(blocked)

    def _ai_evaluation_adapters(
        self,service: DependencyService,model_ids=None,
    ) -> tuple[AIEvaluationAdapter,...]:
        settings=self.settings
        selected=set(model_ids or (*WD_TAGGER_MODELS,*PLAIN_LANGUAGE_MODELS))
        adapters=[]
        for model_id,(label,_directory) in WD_TAGGER_MODELS.items():
            if model_id not in selected:
                continue
            adapters.append(AIEvaluationAdapter(
                model_id,label,"sdxl_tags",
                WDEva02Backend(
                    service.wd_model_dir_for(model_id),
                    settings.wd_general_threshold,
                    settings.wd_character_threshold,
                    settings.wd_rating_threshold,
                    model_id=model_id,
                    execution_provider=settings.wd_execution_provider,
                    batch_size=settings.wd_batch_size,
                ),
                "cuda",
            ))
        common={
            "max_new_tokens":settings.joycaption_max_new_tokens,
            "min_new_tokens":settings.joycaption_min_new_tokens,
            "num_beams":settings.joycaption_num_beams,
            "length_penalty":settings.joycaption_length_penalty,
            "do_sample":settings.joycaption_do_sample,
            "temperature":settings.joycaption_temperature,
            "top_k":settings.joycaption_top_k,
            "top_p":settings.joycaption_top_p,
            "repetition_penalty":settings.joycaption_repetition_penalty,
            "no_repeat_ngram_size":settings.joycaption_no_repeat_ngram_size,
            "gpu_index":settings.joycaption_gpu_index,
        }
        if JOYCAPTION_MODEL in selected:
            adapters.append(AIEvaluationAdapter(
                JOYCAPTION_MODEL,PLAIN_LANGUAGE_MODELS[JOYCAPTION_MODEL][0],
                "plain_language",
                JoyCaptionBackend(
                    service.plain_language_model_dir_for(JOYCAPTION_MODEL),
                    prompt=settings.joycaption_prompt,
                    quantization=settings.joycaption_quantization,
                    **common,
                ),
                "cuda",
            ))
        if FLORENCE2_MODEL in selected:
            adapters.append(AIEvaluationAdapter(
                FLORENCE2_MODEL,PLAIN_LANGUAGE_MODELS[FLORENCE2_MODEL][0],
                "plain_language",
                Florence2Backend(
                    service.plain_language_model_dir_for(FLORENCE2_MODEL),
                    **common,
                ),
                "cuda",
            ))
        return tuple(adapters)

    def evaluate_ai_baseline(self):
        if (
            getattr(self,"_benchmark_task",None) is not None
            or getattr(self,"_joy_benchmark_task",None) is not None
            or getattr(self,"_joy_quality_task",None) is not None
            or getattr(self,"_ai_evaluation_task",None) is not None
        ):
            return
        if "ai" in self._dirty_sections:
            QMessageBox.information(
                self,"Save AI Configuration first",
                "The installed AI evaluation records one exact saved configuration "
                "for every included model. Save AI Configuration, then start the evaluation again.",
            )
            self._focus_ai_setting(self.save_ai_button)
            return
        service=self.dependency_service()
        model_ids,blocked=SetupPage._ai_evaluation_model_availability(
            service,self.settings,
        )
        if not model_ids:
            detail=(
                "\n\nInstalled models that cannot run with the current saved "
                "settings:\n• "+"\n• ".join(
                    f"{SetupPage._ai_evaluation_model_label(model_id)} — {reason}"
                    for model_id,reason in blocked
                )
                if blocked else ""
            )
            QMessageBox.information(
                self,"No installed AI model is ready",
                "This evaluation uses only supported models currently Ready on "
                "this computer and never installs missing models. At least one "
                "model with its usable runtime and saved deterministic CUDA "
                "settings is needed."+detail,
            )
            return
        if QMessageBox.question(
            self,"Evaluate installed local AI?",
            SetupPage._ai_evaluation_confirmation_text(model_ids,blocked),
        )!=QMessageBox.Yes:
            return
        selected=QFileDialog.getOpenFileNames(
            self,"Choose up to 4 representative images","",
            "Images (*.png *.jpg *.jpeg *.webp *.bmp)",
        )[0]
        samples=self._benchmark_samples(
            selected,AI_BASELINE_EVALUATION_SAMPLE_LIMIT,
        )
        if not samples:
            return
        if not self._begin_operation(
            "setup_baseline_evaluation",
            "Evaluating the supported local AI baseline",resource="gpu",
        ):
            return
        adapters=self._ai_evaluation_adapters(service,model_ids)
        evaluator=AIBaselineEvaluator(adapters)
        total=len(adapters)*len(samples)
        self._ai_evaluation_samples=tuple(samples)
        self._ai_evaluation_progress=QProgressDialog(
            f"Preparing {len(adapters):,} local models across "
            f"{len(samples):,} sample images…",
            "Cancel Safely",0,total,self,
        )
        self._ai_evaluation_progress.setWindowTitle("Installed Local AI Evaluation")
        self._ai_evaluation_progress.setMinimumDuration(0)
        self._ai_evaluation_progress.canceled.connect(
            self._cancel_ai_evaluation
        )
        self._ai_evaluation_bar=self._ai_evaluation_progress.findChild(
            QProgressBar
        )
        if self._ai_evaluation_bar is not None:
            set_progress_bar(
                self._ai_evaluation_bar,0,total,
                detail="Preparing installed local AI evaluation",unit="model results",
            )
        self._ai_evaluation_progress.show()

        def work(progress,should_cancel):
            self._prepare_cold_ai_work(should_cancel)
            return evaluator.run(
                samples,should_cancel=should_cancel,progress=progress,
            )

        task=CancelableProgressCallTask(work)
        self._ai_evaluation_task=task
        task.signals.progress.connect(self._ai_evaluation_step)
        task.signals.complete.connect(self._ai_evaluation_complete)
        task.signals.cancelled.connect(self._ai_evaluation_cancelled)
        task.signals.failed.connect(self._ai_evaluation_failed)
        self.pool.start(task)

    def _cancel_ai_evaluation(self):
        if self._ai_evaluation_task is None:
            return
        self._ai_evaluation_task.cancel()
        if self.state is not None:
            self.state.request_long_operation_cancel(
                "setup_baseline_evaluation"
            )
        self.header.set_status(
            "Cancelling installed AI evaluation safely after the current model call…"
        )

    def _ai_evaluation_step(self,index,total,label):
        if self.state is not None:
            self.state.update_long_operation(
                "setup_baseline_evaluation",index,total,label,
            )
        self._ai_evaluation_progress.setLabelText(label)
        if self._ai_evaluation_bar is not None:
            set_progress_bar(
                self._ai_evaluation_bar,index,total,
                detail=label,unit="model results",
            )
        else:
            self._ai_evaluation_progress.setValue(index)

    def _ai_evaluation_complete(self,session):
        self._ai_evaluation_task=None
        self._ai_evaluation_progress.close()
        self._finish_operation("setup_baseline_evaluation")
        samples=self._ai_evaluation_samples
        self._ai_evaluation_samples=()
        self.header.set_status(
            "Installed local AI run complete • aggregate review ready"
        )
        dialog=AIBaselineEvaluationDialog(
            samples,session,self.settings,self,
        )
        try:
            dialog.exec()
        finally:
            dialog.discard_sensitive_state()
            dialog.deleteLater()
        self.header.set_status(
            "Installed AI review closed • temporary paths and raw outputs discarded"
        )

    def _ai_evaluation_cancelled(self):
        self._ai_evaluation_task=None
        self._ai_evaluation_samples=()
        self._ai_evaluation_progress.close()
        self._finish_operation("setup_baseline_evaluation")
        self.header.set_status(
            "Installed AI evaluation cancelled safely • no results were kept"
        )

    def _ai_evaluation_failed(self,message):
        self._ai_evaluation_task=None
        self._ai_evaluation_samples=()
        self._ai_evaluation_progress.close()
        self._finish_operation("setup_baseline_evaluation")
        QMessageBox.warning(
            self,"Installed Local AI Evaluation Could Not Finish",
            f"{message}\n\nNo evaluation, settings, or image data were saved.",
        )

    def save_storage(self):
        if all(str(getattr(self.settings,key,""))==entry.text().strip()
               for key,entry in self.storage_fields.items()):
            return
        if not self.folder_locations.save_group("app"):
            QMessageBox.warning(self,"Storage locations need attention",self.storage_status.text())

    def _folder_locations_saved(self):
        model_path_changed=self._folder_ai_root!=self.settings.ai_models_directory
        self._folder_ai_root=self.settings.ai_models_directory
        if model_path_changed and hasattr(self,"dependency_panel"):
            self.dependency_panel.refresh()
        self.header.set_status("Folder locations saved")

    def _update_content_safety_note(self):
        level=str(self.content_level.currentData() or "sensitive")
        labels={
            "general":"General",
            "sensitive":"General and Sensitive",
            "questionable":"General, Sensitive, and Questionable",
            "explicit":"all rated images",
        }
        always=[
            rating.title() for rating,control in self.hidden_ratings.items()
            if control.isChecked()
        ]
        override=(
            " Always-blur overrides: "+", ".join(always)+"."
            if always else " No rating-specific always-blur override is selected."
        )
        unknown=(
            " Images without a dependable rating are also blurred."
            if self.blur_unknown.isChecked() else
            " Images without a dependable rating may appear unblurred."
        )
        self.content_safety_note.setText(
            f"Effective preview protection: {labels[level]} may appear unblurred by default; higher ratings are blurred."
            +override+unknown+
            " Blurred images stay in the gallery and can be intentionally revealed."
        )

    def _restore_preference_controls(self):
        """Restore only immediate preferences, leaving staged AI edits untouched."""
        for control,value in (
            (self.restore_last,self.settings.restore_last_dataset),
            (self.check_dependencies,self.settings.check_dependencies_on_startup),
            (self.show_tooltips,self.settings.show_tooltips),
            (self.color_blind,self.settings.color_blind_mode),
            (self.blur_unknown,self.settings.gallery_blur_unknown),
            (self.remember_reveals,self.settings.remember_content_reveals),
            (self.check_updates_on_startup,self.settings.check_updates_on_startup),
            *((control,rating in self.settings.gallery_hidden_ratings)
              for rating,control in self.hidden_ratings.items()),
        ):
            blocker=QSignalBlocker(control); control.setChecked(value); del blocker
        for control,value in (
            (self.gallery_size,self.settings.gallery_page_size),
            (self.content_level,self.settings.gallery_content_level),
            (self.update_channel,self.settings.update_channel),
            (self.theme,self.settings.ui_theme),
        ):
            blocker=QSignalBlocker(control); control.setCurrentIndex(max(0,control.findData(value))); del blocker
        self._update_content_safety_note()

    def _save_preferences(self,values) -> bool:
        previous={key:getattr(self.settings,key) for key in values}
        try:
            for key,value in values.items(): setattr(self.settings,key,value)
            self.settings.save()
        except Exception as exc:
            for key,value in previous.items(): setattr(self.settings,key,value)
            self._restore_preference_controls()
            self.header.set_status("Preference not saved; previous choices restored")
            QMessageBox.critical(self,"Could Not Save Preference",
                "The preference could not be saved. Previous choices are restored in Setup; "
                "your staged AI configuration is unchanged. Resolve the storage problem and try again.\n\n"+str(exc))
            return False
        return True

    def save_general(self):
        previous_unknown=bool(getattr(self.settings,"gallery_blur_unknown",True))
        if previous_unknown and not self.blur_unknown.isChecked():
            if QMessageBox.question(
                self,"Show images without a dependable rating?",
                "Images whose rating is still checking, unavailable, stale, unreadable, or unresolved may be shown without blur. This changes presentation only and does not confirm or edit Image Knowledge.\n\nTurn off Unknown blur protection?",
            )!=QMessageBox.Yes:
                blocker=QSignalBlocker(self.blur_unknown); self.blur_unknown.setChecked(True); del blocker
                return
        previous_content=(
            self.settings.gallery_content_level,tuple(self.settings.gallery_hidden_ratings),
            self.settings.remember_content_reveals,getattr(self.settings,"gallery_blur_unknown",True),
        )
        page_size=int(self.gallery_size.currentData())
        if not self._save_preferences({
            "restore_last_dataset":self.restore_last.isChecked(),
            "check_dependencies_on_startup":self.check_dependencies.isChecked(),
            "show_tooltips":self.show_tooltips.isChecked(),
            "gallery_page_size":page_size,
            "gallery_card_width":min(GALLERY_DENSITIES,key=lambda value:abs(value[0]-page_size))[1],
            "gallery_content_level":str(self.content_level.currentData()),
            "gallery_hidden_ratings":[rating for rating,control in self.hidden_ratings.items() if control.isChecked()],
            "gallery_blur_unknown":self.blur_unknown.isChecked(),
            "remember_content_reveals":self.remember_reveals.isChecked(),
        }):
            return False
        self._update_content_safety_note()
        current_content=(
            self.settings.gallery_content_level,tuple(self.settings.gallery_hidden_ratings),
            self.settings.remember_content_reveals,self.settings.gallery_blur_unknown,
        )
        if current_content!=previous_content: self.content_level_changed.emit(self.settings.gallery_content_level)
        self.tooltips_changed.emit(self.settings.show_tooltips)
        self.header.set_status("Preference saved automatically")
        return True

    def sync_evidence_intake(self):
        """Reflect the Automation-owned intake default without writing it."""
        blocker=QSignalBlocker(self.auto_import_source)
        self.auto_import_source.setChecked(
            bool(self.settings.auto_import_source_evidence)
        )
        del blocker

    def save_ai(self) -> bool:
        prompt=self.joy_prompt.toPlainText().strip()
        if (
            self.plain_language_model.currentData()==JOYCAPTION_MODEL
            and not prompt
        ):
            QMessageBox.warning(
                self,"Instructions required",
                "Enter instructions for AI Description generation.",
            )
            self._focus_ai_setting(self.joy_prompt)
            return False
        enabled=[model_id for model_id,control in self.wd_taggers.items() if control.isChecked()]
        if self.use_wd.isChecked() and not enabled:
            QMessageBox.warning(
                self,"Choose an AI Tag Model",
                "Enable at least one local AI Tag model.",
            )
            self.show_section("ai","sdxl"); self.tagger_toggle.setChecked(True)
            next(iter(self.wd_taggers.values())).setFocus(Qt.OtherFocusReason)
            return False
        service=self.dependency_service()
        missing=[model_id for model_id in enabled
                 if not service.component(service.wd_component_name(model_id)).installed]
        if missing:
            QMessageBox.warning(
                self,"Download models before enabling them",
                "These AI Tag models are not downloaded and cannot be enabled:\n\n"+
                "\n".join(WD_TAGGER_MODELS[model_id][0] for model_id in missing),
            )
            self._refresh_model_choices(service)
            self._focus_ai_setting(self.wd_taggers[missing[0]])
            return False
        pending_keys=tuple(
            key for key in self._ai_control_values()
            if key not in AUTOMATION_BEHAVIOR_SETTING_KEYS
        )
        previous={
            key:getattr(self.settings,key)
            for key in pending_keys
        }
        previous["joycaption_quantization_explicit"]=bool(
            self.settings.joycaption_quantization_explicit
        )
        saved_min_tokens=min(self.joy_tokens.value(),self.joy_min_tokens.value())
        try:
            self.settings.enabled_wd_taggers=enabled
            for key,value in self.ai_memory.values().items():
                setattr(self.settings,key,value)
            self.settings.wd_execution_provider=str(self.wd_provider.currentData())
            self.settings.wd_batch_size=self.wd_batch_size.value()
            self.settings.plain_language_model=str(
                self.plain_language_model.currentData()
            )
            self.settings.joycaption_max_new_tokens=self.joy_tokens.value()
            self.settings.joycaption_min_new_tokens=saved_min_tokens
            self.settings.joycaption_num_beams=self.joy_beams.value()
            self.settings.joycaption_length_penalty=self.joy_length_penalty.value()
            self.settings.joycaption_do_sample=self.joy_sample.isChecked()
            self.settings.joycaption_temperature=self.joy_temperature.value()
            self.settings.joycaption_top_k=self.joy_top_k.value()
            self.settings.joycaption_top_p=self.joy_top_p.value()
            self.settings.joycaption_repetition_penalty=self.joy_repetition.value()
            self.settings.joycaption_no_repeat_ngram_size=self.joy_ngram.value()
            self.settings.joycaption_gpu_index=self.joy_gpu.value()
            self.settings.joycaption_quantization=str(
                self.joy_quantization.currentData()
            )
            self.settings.joycaption_quantization_explicit=True
            if prompt:
                self.settings.joycaption_prompt=prompt
            self.settings.save()
        except Exception as exc:
            for key,value in previous.items():
                setattr(self.settings,key,value)
            QMessageBox.critical(
                self,"AI configuration was not saved",
                "CollectiGen kept the pending values in Setup because the settings "
                f"write could not be verified.\n\n{exc}",
            )
            self._focus_ai_setting(self.save_ai_button)
            return False
        if self.joy_min_tokens.value()!=saved_min_tokens:
            blocker=QSignalBlocker(self.joy_min_tokens)
            self.joy_min_tokens.setValue(saved_min_tokens)
            del blocker
        configure=getattr(self.state,"configure_ai_runtimes",None)
        if callable(configure):
            configure()
        committed=self._ai_control_values()
        self._committed_section_values["ai"]=committed
        self.sync_automation_behavior()
        self._joy_quantization_user_selected=False
        self._joy_previous_quantization=str(self.joy_quantization.currentData())
        self._clear_dirty("ai")
        self._update_joy_memory_guidance()
        self._refresh_ai_availability(service)
        blocker=QSignalBlocker(self.automation_mode)
        self.automation_mode.setCurrentIndex(0)
        del blocker
        self._automation_preview_plan=None
        self._applied_automation_preset=""
        self.apply_automation_preset_button.setEnabled(False)
        self.automation_preset_preview.setText(
            "No preset selected. Your saved or pending settings are unchanged."
        )
        self._update_automation_example()
        self._update_ai_overview_readiness(service)
        self.preparation_defaults_changed.emit()
        saved_mode=self._matching_automation_preset(
            self._committed_section_values["ai"]
        )
        self.header.set_status(
            f"AI configuration saved — {self._automation_preset_label(saved_mode)}"
        )
        return True

    def export_pack(self):
        from .knowledge_pack_actions import export_pack
        return export_pack(self)

    def import_pack(self):
        from .knowledge_pack_actions import import_pack
        return import_pack(self)

    def open_logs(self):
        path=Path(__file__).resolve().parents[1]/"logs"; path.mkdir(exist_ok=True)
        try: subprocess.Popen(["explorer",str(path)])
        except OSError as exc: QMessageBox.critical(self,"Could not open logs",str(exc))
