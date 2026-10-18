from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThreadPool, QTimer, Qt,Signal
from PySide6.QtWidgets import (
    QCheckBox,QComboBox,QDialog,QDialogButtonBox,QFileDialog,QFrame,QGridLayout,
    QGroupBox,QHBoxLayout,QLabel,QInputDialog,QLayout,QMessageBox,QProgressBar,
    QPushButton,QScrollArea,QSpinBox,QTabWidget,QTreeWidget,QTreeWidgetItem,
    QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core import (
    DependencyService,JOYCAPTION_MODEL,MODEL_ACQUISITIONS,
    PLAIN_LANGUAGE_MODELS,WD_EVA02_MODEL,WD_TAGGER_DESCRIPTIONS,WD_TAGGER_MODELS,
)
from core.dependencies import (
    OptionalRuntimeImportCheck,WD_SETUP_PRESETS,matching_wd_setup_preset,
)
from .components import compact_button,set_progress_bar
from .capability_manager_dialog import CapabilityManagerDialog
from .modal_feedback import FeedbackEnabledDialog
from .workers import AsyncCallTask,ProgressCallTask,CoordinatedCallTask
from shiboken6 import isValid


CAPABILITY_COMPONENTS={
    "application": {"Python 3.10+","PySide6","Pillow","ExifTool"},
    "metadata": {"ExifTool"},
    "video": {"PyAV"},
}


def capability_components(
    capability: str,service: DependencyService,model_id: str="",
) -> set[str]:
    if capability=="joy":
        model_id=str(model_id or getattr(
            service.settings,"plain_language_model",JOYCAPTION_MODEL,
        ) or JOYCAPTION_MODEL)
        if model_id not in PLAIN_LANGUAGE_MODELS:
            model_id=JOYCAPTION_MODEL
        names={
            *DependencyService.JOY_BASE_PACKAGES,
            DependencyService.JOY_HARDWARE_COMPONENT,
            service.plain_language_component_name(model_id),
        }
        if (
            model_id==JOYCAPTION_MODEL
            and str(getattr(
                service.settings,"joycaption_quantization","bf16"
            ))=="nf4"
        ):
            names.add("BitsAndBytes")
        return names
    if capability!="wd":
        return set(CAPABILITY_COMPONENTS.get(capability,set()))
    enabled=[value for value in service.settings.enabled_wd_taggers if value in WD_TAGGER_MODELS] or [WD_EVA02_MODEL]
    return {*DependencyService.WD_PACKAGES,*(service.wd_component_name(value) for value in enabled)}


class WdSetupSelectionDialog(FeedbackEnabledDialog):
    """Choose an understandable AI Tag model plan before acquisition review."""

    feedback_workflow = "AI Tag setup selection"

    def __init__(
        self,service: DependencyService,model_ids,consensus_minimum: int,
        parent=None,
    ):
        super().__init__(parent)
        self.service=service; self._syncing=False
        selected=tuple(dict.fromkeys(
            value for value in model_ids if value in WD_TAGGER_MODELS
        )) or (WD_EVA02_MODEL,)
        minimum=max(1,min(int(consensus_minimum),len(selected)))
        self.setWindowTitle("Choose AI Tag Setup")
        self.resize(820,620); self.setMinimumSize(700,520)
        layout=QVBoxLayout(self)
        title=QLabel("Choose AI Tag Setup")
        title.setObjectName("pageTitle"); layout.addWidget(title)
        intro=QLabel(
            "Choose which local models CollectiGen should install and use. "
            "More models can provide independent agreement, but require more "
            "download, storage, and processing time. Nothing is downloaded on "
            "this screen."
        )
        intro.setWordWrap(True); layout.addWidget(intro)

        preset_row=QHBoxLayout(); preset_row.addWidget(QLabel("Starting point"))
        self.preset=QComboBox()
        for key,value in WD_SETUP_PRESETS.items():
            self.preset.addItem(value.label,key)
        self.preset.addItem("Custom","custom")
        self.preset.setAccessibleName("AI Tag setup starting point")
        self.preset.setAccessibleDescription(
            "Choose a speed and ensemble shortcut, or Custom to select exact models."
        )
        preset_row.addWidget(self.preset,1); layout.addLayout(preset_row)
        self.summary=QLabel(); self.summary.setWordWrap(True)
        self.summary.setObjectName("mutedLabel"); layout.addWidget(self.summary)

        models=QGroupBox("Models to install and enable")
        model_grid=QGridLayout(models)
        self.model_checks={}
        for row,(model_id,(display,_directory)) in enumerate(WD_TAGGER_MODELS.items()):
            control=QCheckBox(display)
            control.setChecked(model_id in selected)
            control.setToolTip(WD_TAGGER_DESCRIPTIONS[model_id])
            control.setAccessibleDescription(WD_TAGGER_DESCRIPTIONS[model_id])
            status=QLabel(
                "Installed" if (
                    component:=service.component(service.wd_component_name(model_id))
                ) is not None and component.installed else "Download required"
            )
            status.setObjectName("mutedLabel")
            status.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
            model_grid.addWidget(control,row,0)
            model_grid.addWidget(status,row,1)
            self.model_checks[model_id]=control
        model_grid.setColumnStretch(0,1)
        layout.addWidget(models)

        vote_row=QHBoxLayout()
        vote_label=QLabel("Minimum matching model votes")
        self.consensus=QSpinBox(); self.consensus.setRange(1,len(selected))
        self.consensus.setValue(minimum)
        self.consensus.setAccessibleDescription(
            "How many selected models must independently suggest the same Tag "
            "for multi-model consensus. Findings remain reviewable evidence."
        )
        vote_row.addWidget(vote_label); vote_row.addWidget(self.consensus)
        vote_row.addStretch(1); layout.addLayout(vote_row)
        note=QLabel(
            "This threshold controls agreement between the selected models. A "
            "single-model setup cannot provide independent multi-model consensus, "
            "and model output does not become confirmed Image Knowledge without "
            "the existing review or enabled safe-merge policy."
        )
        note.setWordWrap(True); note.setObjectName("mutedLabel")
        layout.addWidget(note)

        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.continue_button=buttons.addButton(
            "Continue to Download Review…",QDialogButtonBox.AcceptRole,
        )
        self.continue_button.setProperty("primary",True)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        current=matching_wd_setup_preset(selected,minimum)
        self.preset.setCurrentIndex(max(0,self.preset.findData(current)))
        self.preset.currentIndexChanged.connect(self._preset_changed)
        for control in self.model_checks.values():
            control.toggled.connect(self._custom_selection_changed)
        self.consensus.valueChanged.connect(self._custom_consensus_changed)
        self._refresh_summary()

    def _selected_models(self) -> tuple[str,...]:
        return tuple(
            model_id for model_id,control in self.model_checks.items()
            if control.isChecked()
        )

    def _preset_changed(self,_index):
        if self._syncing:return
        preset=WD_SETUP_PRESETS.get(str(self.preset.currentData() or "custom"))
        if preset is None:
            self._refresh_summary(); return
        self._syncing=True
        try:
            for model_id,control in self.model_checks.items():
                control.setChecked(model_id in preset.model_ids)
            self.consensus.setMaximum(len(preset.model_ids))
            self.consensus.setValue(preset.consensus_minimum)
        finally:
            self._syncing=False
        self._refresh_summary()

    def _custom_selection_changed(self,_checked):
        if self._syncing:return
        self._syncing=True
        try:
            self.preset.setCurrentIndex(self.preset.findData("custom"))
            count=len(self._selected_models())
            self.consensus.setMaximum(max(1,count))
            self.consensus.setValue(min(self.consensus.value(),max(1,count)))
        finally:
            self._syncing=False
        self._refresh_summary()

    def _custom_consensus_changed(self,_value):
        if self._syncing:return
        selected=self._selected_models()
        if matching_wd_setup_preset(selected,self.consensus.value())=="custom":
            self._syncing=True
            try:self.preset.setCurrentIndex(self.preset.findData("custom"))
            finally:self._syncing=False
        self._refresh_summary()

    def _refresh_summary(self):
        selected=self._selected_models(); count=len(selected)
        self.consensus.setMaximum(max(1,count))
        preset=WD_SETUP_PRESETS.get(str(self.preset.currentData() or "custom"))
        tradeoff=(preset.summary if preset is not None else
            "Manual selection: install only the checked models and use the vote threshold below.")
        votes=min(self.consensus.value(),max(1,count))
        self.summary.setText(
            f"{tradeoff}\n\nSelected: {count} model{'s' if count!=1 else ''}; "
            f"at least {votes} matching vote{'s' if votes!=1 else ''}."
        )
        self.continue_button.setEnabled(bool(selected))

    def selected_configuration(self) -> tuple[tuple[str,...],int,str]:
        selected=self._selected_models()
        minimum=max(1,min(self.consensus.value(),max(1,len(selected))))
        label=self.preset.currentText() or "Custom"
        return selected,minimum,label


class DependencyDialog(FeedbackEnabledDialog):
    """Native setup workflow for application, AI-model, and metadata components."""
    feedback_workflow = "Components and model setup"

    capabilities_changed=Signal()
    settings_changed=Signal()
    setup_queue_changed=Signal()

    def __init__(self,settings,parent=None,*,focus_capability: str="",state=None,embedded: bool=False,dependency_service_factory=None):
        super().__init__(parent); self.settings=settings; self.state=state or getattr(parent,"state",None)
        if self.state is not None and dependency_service_factory is not None and getattr(self.state,"dependency_service_factory",None) is not dependency_service_factory:
            raise ValueError("Components must retain the state's admitted dependency service factory.")
        self.dependency_service_factory=dependency_service_factory if dependency_service_factory is not None else getattr(self.state,"dependency_service_factory",None)
        if self.dependency_service_factory is not None and not callable(self.dependency_service_factory):
            raise ValueError("The dependency service factory must be callable.")
        self.service=self.dependency_service()
        self.pool=QThreadPool.globalInstance(); self._task=None; self._controls=[]; self.embedded=embedded; self.requested_wd_models=[]
        self._wd_runtime_import_check=None
        self._focused_capability=focus_capability if not embedded and (focus_capability in {"application","metadata","wd","joy"} or (focus_capability=="video" and getattr(self.service,"capability_profile","")=="organizer-image")) else ""
        self._queue_states={}; self._queue_event_callback=None
        self._video_review_task=None;self._video_review_closing=False
        if embedded:
            self.setWindowFlags(Qt.Widget); self.setMinimumSize(0,0)
        else:
            self.setWindowTitle("Setup and dependencies")
            if self._focused_capability:
                self.resize(980,520); self.setMinimumSize(800,420)
            else:
                self.resize(1180,760); self.setMinimumSize(900,600)
        root_layout=QVBoxLayout(self); self.content_scroll=None; self.content_body=None
        if embedded:
            layout=root_layout
        else:
            self.content_scroll=QScrollArea()
            self.content_scroll.setWidgetResizable(True)
            self.content_scroll.setFrameShape(QFrame.NoFrame)
            self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            self.content_body=QWidget()
            layout=QVBoxLayout(self.content_body)
            layout.setContentsMargins(0,0,0,0)
            layout.setSizeConstraint(QLayout.SetMinimumSize)
            self.content_scroll.setWidget(self.content_body)
            root_layout.addWidget(self.content_scroll,1)
        focus_labels={"application":"Core application","metadata":"Metadata","wd":"AI Tags","joy":"AI Descriptions"}
        focus_labels["video"]="Optional Video"
        self._focused_label=focus_labels.get(self._focused_capability,"")
        title=QLabel(
            "Components and downloads" if embedded else
            f"Set up {self._focused_label}" if self._focused_capability else
            "Setup and dependencies"
        )
        self.title_label=title
        title.setObjectName("sectionTitle" if embedded else "pageTitle"); layout.addWidget(title)
        intro=QLabel(
            f"Check {self._focused_label} below. "
            "Closing Setup does not restart your task; review its choices and start it again. "
            "Show other Setup options if you need another component or offline transfer."
            if self._focused_capability else
            "Set up the local capability you need. CollectiGen includes its reviewed shared runtime automatically; "
            "model downloads begin only after confirmation. Use Manage to install or remove individual optional AI models."
        )
        self.intro_label=intro
        intro.setWordWrap(True); intro.setObjectName("mutedLabel"); layout.addWidget(intro)
        self.task_tabs=QTabWidget()
        self.task_tabs.setDocumentMode(True)
        self.task_tabs.setAccessibleName("Component and download tasks")
        self.install_page=QWidget(); install_layout=QVBoxLayout(self.install_page)
        install_layout.setContentsMargins(0,8,0,0)
        self.offline_page=QWidget(); offline_task_layout=QVBoxLayout(self.offline_page)
        offline_task_layout.setContentsMargins(0,8,0,0)
        self.inventory_page=QWidget(); inventory_layout=QVBoxLayout(self.inventory_page)
        inventory_layout.setContentsMargins(0,8,0,0)
        self.task_indexes={
            "install":self.task_tabs.addTab(self.install_page,"Install or Repair"),
            "offline":self.task_tabs.addTab(self.offline_page,"Offline Transfer"),
            "inventory":self.task_tabs.addTab(self.inventory_page,"Technical Inventory"),
        }
        self.task_tabs.currentChanged.connect(self._update_task_intro)
        self.show_all_setup_button=None
        if self._focused_capability or embedded:
            self.show_all_setup_button=compact_button(QPushButton("Show Other Setup Options…"))
            self.show_all_setup_button.setCheckable(True)
            self.show_all_setup_button.setAccessibleDescription(
                "Show other capabilities, offline transfer, and technical component details without leaving this setup review."
            )
            self.show_all_setup_button.setToolTip(self.show_all_setup_button.accessibleDescription())
            self.show_all_setup_button.toggled.connect(self._show_all_setup_options)
            self._controls.append(self.show_all_setup_button)
            layout.addWidget(self.show_all_setup_button,0,Qt.AlignLeft)
            if embedded:
                self.show_all_setup_button.hide()

        capabilities=QWidget(); self.capabilities_panel=capabilities
        capability_layout=QVBoxLayout(capabilities)
        capability_layout.setContentsMargins(0,0,0,0); capability_layout.setSpacing(8)
        capability_title=QLabel("Capability status")
        capability_title.setObjectName("sectionTitle")
        capability_layout.addWidget(capability_title)
        self.capability_layout=capability_layout; self.capability_status={}
        self.capability_rows={}; self.capability_actions={}; self.capability_groups={}
        capability_specs={
            "application":("Core application","Keeps the interface, image reading, and required local utilities available.","Repair",self.setup_application),
            "metadata":("Metadata","Reads and safely updates embedded generation and image Metadata with ExifTool.","Set Up",self.setup_exiftool),
            "wd":("AI Tags","Runs the enabled local WD models and stores their AI Tags for review.","Set Up",self.setup_wd),
            "joy":("AI Descriptions","Runs the selected local caption model to generate AI Descriptions.","Set Up",self.setup_joy),
        }
        group_specs=(
            ("application","Application && Metadata",
             "Required for ordinary library and Metadata work.",
             ("application","metadata")),
            ("optional","AI Models",
             "Local models can be installed or removed independently.",
             ("wd","joy")),
        )
        if getattr(self.service,"capability_profile","")=="organizer-image":
            capability_specs["video"]=("Video","Optional local video verification and playback; image work stays available.","Set Up Video",self.setup_video)
            group_specs+=(
                ("video","Optional Video","Reviewed durable components; no automatic download.",("video",)),
            )
        for group_key,group_title,group_help,keys in group_specs:
            group=QGroupBox(group_title); group_layout=QVBoxLayout(group)
            group_layout.setSpacing(7)
            group_note=QLabel(group_help); group_note.setWordWrap(True)
            group_note.setObjectName("mutedLabel"); group_layout.addWidget(group_note)
            self.capability_groups[group_key]=group
            for key in keys:
                title_text,help_text,action_text,callback=capability_specs[key]
                row_frame=QFrame(); row_frame.setObjectName("capabilityRow")
                row_frame.setProperty("capabilityKind",group_key)
                row_frame.setAccessibleName(f"{title_text} capability")
                row_frame.setAccessibleDescription(help_text)
                row_layout=QHBoxLayout(row_frame)
                row_layout.setContentsMargins(11,7,11,7); row_layout.setSpacing(10)
                title_label=QLabel(title_text); title_label.setToolTip(help_text)
                status_label=QLabel("Checking…"); status_label.setObjectName("capabilityStatus"); status_label.setToolTip(help_text)
                status_label.setAlignment(Qt.AlignRight|Qt.AlignVCenter)
                status_label.setMinimumWidth(125)
                action_panel=QWidget(); action_panel.setObjectName("capabilityActions")
                action_layout=QHBoxLayout(action_panel)
                action_layout.setContentsMargins(0,0,0,0); action_layout.setSpacing(6)
                button=compact_button(QPushButton(action_text),primary=True); button.clicked.connect(callback); button.setToolTip(help_text)
                self.capability_status[key]=status_label; self._controls.append(button)
                self.capability_rows[key]=row_frame; self.capability_actions[key]=action_panel
                row_layout.addWidget(title_label); row_layout.addStretch(1)
                row_layout.addWidget(status_label,0,Qt.AlignRight|Qt.AlignVCenter)
                action_layout.addWidget(button)
                if key=="video":
                    self.cancel_video_review_button=compact_button(QPushButton("Cancel Video Review"))
                    self.cancel_video_review_button.clicked.connect(self.cancel_video_review)
                    self.cancel_video_review_button.hide()
                    action_layout.addWidget(self.cancel_video_review_button)
                if key in {"wd","joy"}:
                    manage_text=(
                        "Manage AI Tags…" if key=="wd" else
                        "Manage AI Descriptions…"
                    )
                    manage=compact_button(QPushButton(manage_text))
                    manage.setAccessibleName(
                        "Manage AI Tag components" if key=="wd" else
                        "Manage AI Description components"
                    )
                    manage.setAccessibleDescription(
                        f"View, install, or uninstall individual {title_text} models and the shared runtime."
                    )
                    manage.setToolTip(manage.accessibleDescription())
                    manage.clicked.connect(lambda _checked=False,value=key:self.manage_capability(value))
                    self._controls.append(manage); action_layout.addWidget(manage)
                    if key=="wd": self.wd_manage_button=manage
                    else: self.joy_manage_button=manage
                row_layout.addWidget(action_panel,0,Qt.AlignRight|Qt.AlignVCenter)
                row_minimum=max(40,row_frame.sizeHint().height())
                row_frame.setMinimumHeight(row_minimum)
                group_layout.addWidget(row_frame)
                if key=="application": self.app_button=button
                elif key=="wd": self.wd_button=button
                elif key=="joy": self.joy_button=button
                elif key=="metadata": self.exif_button=button
                elif key=="video": self.video_button=button
            if group_key=="optional":
                self.upscaler_models_button=compact_button(QPushButton("Get Upscaler Models…"))
                self.upscaler_models_button.setAccessibleName("Get local upscaler models")
                self.upscaler_models_button.setAccessibleDescription(
                    "Review and acquire optional local ESRGAN upscaler models. "
                    "Opening the catalog does not download a model."
                )
                self.upscaler_models_button.setToolTip(self.upscaler_models_button.accessibleDescription())
                self.upscaler_models_button.clicked.connect(self.open_upscaler_models)
                self._upscaler_catalog_available=self.state is not None and hasattr(self.state,"jobs")
                if not self._upscaler_catalog_available:
                    self.upscaler_models_button.setEnabled(False)
                    self.upscaler_models_button.setToolTip(
                        "Upscaler models are available from Setup inside the application workspace."
                    )
                self._controls.append(self.upscaler_models_button)
                group_layout.addWidget(self.upscaler_models_button, 0, Qt.AlignLeft)
            capability_layout.addWidget(group)
        install_layout.addWidget(capabilities)

        self.queue_group=QGroupBox("Confirmed setup queue")
        queue_layout=QVBoxLayout(self.queue_group)
        self.queue_help=QLabel(
            "Confirmed component setup runs in order. Waiting work can be "
            "cancelled independently; failed work can be retried after its "
            "prerequisites are corrected."
        )
        self.queue_help.setWordWrap(True)
        self.queue_help.setObjectName("mutedLabel")
        self.queue_table=QTreeWidget()
        self.queue_table.setHeaderLabels((
            "State","Setup item","Result or current detail",
        ))
        self.queue_table.setRootIsDecorated(False)
        self.queue_table.setAlternatingRowColors(True)
        self.queue_table.setUniformRowHeights(True)
        self.queue_table.setMinimumHeight(62)
        self.queue_table.setMaximumHeight(170)
        self.queue_table.setAccessibleName("Confirmed Setup operation queue")
        self.queue_table.itemSelectionChanged.connect(self._update_queue_actions)
        queue_actions=QHBoxLayout()
        self.cancel_waiting_button=compact_button(QPushButton("Cancel Queued Setup"))
        self.cancel_waiting_button.setAccessibleDescription(
            "Cancel only the selected waiting Setup operation without affecting running work."
        )
        self.cancel_waiting_button.clicked.connect(self.cancel_waiting_setup)
        self.retry_setup_button=compact_button(QPushButton("Retry Setup"),primary=True)
        self.retry_setup_button.setAccessibleDescription(
            "Queue the selected failed or cancelled Setup operation again after rechecking prerequisites."
        )
        self.retry_setup_button.clicked.connect(self.retry_selected_setup)
        queue_actions.addStretch(1); queue_actions.addWidget(self.cancel_waiting_button)
        queue_actions.addWidget(self.retry_setup_button)
        queue_layout.addWidget(self.queue_help)
        queue_layout.addWidget(self.queue_table)
        queue_layout.addLayout(queue_actions); layout.addWidget(self.queue_group)

        offline=QWidget(); offline_layout=QVBoxLayout(offline)
        offline_layout.setContentsMargins(0,0,0,0)
        offline_help=QLabel(
            "Move verified models or optional AI components between compatible computers "
            "without a network request. Review the exact pack before import; existing files "
            "are preserved if verification fails."
        )
        offline_help.setWordWrap(True)
        self.import_pack_button=compact_button(QPushButton("Import Model Pack…"),primary=True)
        self.import_pack_button.setAccessibleName("Import offline model pack")
        self.import_pack_button.setAccessibleDescription(
            "Choose and verify a local CollectiGen model pack without accessing the network."
        )
        self.import_pack_button.clicked.connect(self.import_model_pack)
        self.export_pack_button=compact_button(QPushButton("Export Installed Model…"))
        self.export_pack_button.setAccessibleName("Export installed model as offline pack")
        self.export_pack_button.setAccessibleDescription(
            "Choose a verified installed model and create an offline pack without accessing the network."
        )
        self.export_pack_button.clicked.connect(self.export_model_pack)
        self.export_pack_button.setEnabled(False)
        self.local_exiftool_button=compact_button(QPushButton("Choose Local ExifTool…"))
        self.local_exiftool_button.setAccessibleName("Choose local ExifTool for offline Metadata support")
        self.local_exiftool_button.setAccessibleDescription(
            "Choose and verify an existing exiftool executable without downloading or copying it."
        )
        self.local_exiftool_button.clicked.connect(self.locate_exiftool)
        self.import_components_button=compact_button(QPushButton("Import AI Components…"),primary=True)
        self.import_components_button.setAccessibleName("Import offline AI component pack")
        self.import_components_button.setAccessibleDescription(
            "Choose and verify a local CollectiGen optional AI component pack without accessing the network."
        )
        self.import_components_button.clicked.connect(self.import_component_pack)
        self.export_components_button=compact_button(QPushButton("Export AI Components…"))
        self.export_components_button.setAccessibleName("Export installed AI components as offline pack")
        self.export_components_button.setAccessibleDescription(
            "Create a verified offline pack from the installed optional AI Python environment without accessing the network."
        )
        self.export_components_button.clicked.connect(self.export_component_pack)
        self.export_components_button.setEnabled(False)
        self._controls.extend((
            self.import_pack_button,self.export_pack_button,self.local_exiftool_button,
            self.import_components_button,self.export_components_button,
        ))
        offline_layout.addWidget(offline_help)
        transfer_groups=QHBoxLayout()
        model_group=QGroupBox("Model packs"); model_actions=QVBoxLayout(model_group)
        model_actions.addWidget(self.import_pack_button)
        model_actions.addWidget(self.export_pack_button)
        model_actions.addStretch(1)
        component_group=QGroupBox("AI component packs"); component_actions=QVBoxLayout(component_group)
        component_actions.addWidget(self.import_components_button)
        component_actions.addWidget(self.export_components_button)
        component_actions.addStretch(1)
        transfer_groups.addWidget(model_group,1)
        transfer_groups.addWidget(component_group,1)
        offline_layout.addLayout(transfer_groups)
        tool_group=QGroupBox("Local metadata tool"); tool_layout=QHBoxLayout(tool_group)
        tool_layout.addWidget(self.local_exiftool_button,0,Qt.AlignLeft)
        tool_layout.addStretch(1)
        offline_layout.addWidget(tool_group)
        upscaler_note=QLabel(
            "Upscaler file? In Install or Repair, choose Get Upscaler Models, then Use Existing File."
        )
        upscaler_note.setWordWrap(True); upscaler_note.setObjectName("mutedLabel")
        offline_layout.addWidget(upscaler_note)
        self.offline_group=offline; offline_task_layout.addWidget(offline)
        offline_task_layout.addStretch(1)

        details_panel=QWidget(); self.details_panel=details_panel; details_layout=QVBoxLayout(details_panel); details_layout.setContentsMargins(0,0,0,0)
        self.table=QTreeWidget(); self.table.setHeaderLabels(("Status","Component","Version or model","Capability","What it enables","Location"))
        self.table.setRootIsDecorated(False); self.table.setAlternatingRowColors(True); self.table.setUniformRowHeights(True)
        self.table.header().setMinimumSectionSize(120)
        details_layout.addWidget(self.table,1)
        self.table.setMinimumHeight(260)
        inventory_layout.addWidget(details_panel,1)
        layout.addWidget(self.task_tabs,1)

        footer=QHBoxLayout(); self.status=QLabel("Checking components…"); self.status.setObjectName("mutedLabel")
        self.progress=QProgressBar(); set_progress_bar(self.progress); self.progress.hide(); self.progress.setMaximumWidth(260)
        refresh=compact_button(QPushButton("Check Again")); self.refresh_button=refresh
        refresh.clicked.connect(self.refresh_inventory); self._controls.append(refresh)
        self.close_button=None
        footer.addWidget(self.status,1); footer.addWidget(self.progress); footer.addWidget(refresh)
        if not embedded:
            buttons=QDialogButtonBox(QDialogButtonBox.Close); buttons.rejected.connect(self.reject); self.close_button=buttons.button(QDialogButtonBox.Close)
            footer.addWidget(buttons)
        if embedded: layout.addLayout(footer)
        else: root_layout.addLayout(footer)
        from .components import bind_status_footer
        bind_status_footer(self,self.status,self.progress,refresh,title="Components")
        bind_status_footer(self,self.queue_group)
        self.setup_queue_changed.connect(self.refresh_setup_queue)
        if self._setup_queue() is not None:
            self._queue_event_callback=lambda **_payload:self.setup_queue_changed.emit()
            self.state.events.subscribe(
                "setup_queue_changed",self._queue_event_callback,
            )
        if embedded:
            self.status.setText("Component inventory will load in the background.")
            self.refresh_setup_queue()
        else:
            self.refresh()
            self.refresh_setup_queue()
        if self._focused_capability:
            self._show_all_setup_options(False)
        self.focus_capability(focus_capability)

    def _show_all_setup_options(self,show_all: bool):
        if not self._focused_capability:
            return
        focused=self._focused_capability
        for group_key,group in self.capability_groups.items():
            group.setVisible(show_all or (
                group_key=="optional" and focused in {"wd","joy"}
            ) or (group_key=="application" and focused in {"application","metadata"}) or (group_key=="video" and focused=="video"))
        for key,row in self.capability_rows.items():
            row.setVisible(show_all or key==focused)
        self.upscaler_models_button.setVisible(show_all)
        if not show_all:
            self.task_tabs.setCurrentIndex(self.task_indexes["install"])
        self.task_tabs.tabBar().setVisible(show_all)
        if self.show_all_setup_button is not None:
            self.show_all_setup_button.setText(
                f"Show Only {self._focused_label}" if show_all else
                "Show Other Setup Options…"
            )

    def select_task(self, task: str):
        """Reveal the requested named task before Setup search focuses its action."""
        if task not in self.task_indexes:
            raise ValueError(f"Unknown component task: {task}")
        if self._focused_capability and task != "install":
            self.show_all_setup_button.setChecked(True)
        self.task_tabs.setCurrentIndex(self.task_indexes[task])

    def _update_task_intro(self, index: int):
        if index == self.task_indexes["offline"]:
            self.intro_label.setText(
                "Choose a local pack to import or export. Every import has a separate exact review."
            )
        elif index == self.task_indexes["inventory"]:
            self.intro_label.setText(
                "Inspect installed component versions and locations. Check Again refreshes this view."
            )
        elif self._focused_capability:
            self.intro_label.setText(
                f"Review {self._focused_label} below. Check installed components or choose "
                "setup or repair; nothing changes until you confirm. Show other options "
                "for another component or offline transfer."
            )
        else:
            self.intro_label.setText(
                "Set up or repair the local capability you need. Optional AI model downloads "
                "begin only after confirmation."
            )

    def _setup_queue(self):
        return getattr(self.state,"setup_queue",None) if self.state is not None else None

    def _selected_queue_operation_id(self) -> str:
        item=self.queue_table.currentItem()
        return str(item.data(0,Qt.UserRole) or "") if item is not None else ""

    def refresh_setup_queue(self):
        queue=self._setup_queue(); selected=self._selected_queue_operation_id()
        operations=queue.snapshot() if queue is not None else ()
        previous=dict(self._queue_states)
        self.queue_table.clear(); self._queue_states={}
        labels={
            "waiting":"Waiting","running":"Running","complete":"Complete",
            "failed":"Needs attention","cancelled":"Cancelled",
        }
        for operation in operations:
            detail=operation.detail or operation.error or operation.result
            item=QTreeWidgetItem((
                labels.get(operation.state,operation.state.title()),
                self._setup_operation_display_label(operation),detail,
            ))
            item.setData(0,Qt.UserRole,operation.id)
            item.setToolTip(2,operation.error or operation.result or detail)
            self.queue_table.addTopLevelItem(item)
            self._queue_states[operation.id]=operation.state
            if operation.id==selected:
                self.queue_table.setCurrentItem(item)
        for column in range(3):
            self.queue_table.resizeColumnToContents(column)
        self.queue_table.setColumnWidth(
            2,max(260,min(700,self.queue_table.columnWidth(2))),
        )
        row_height=max(
            24,
            self.queue_table.sizeHintForRow(0) if operations else 0,
            self.queue_table.fontMetrics().height()+8,
        )
        table_height=(
            self.queue_table.header().height()
            + max(1,min(5,len(operations)))*row_height
            + self.queue_table.frameWidth()*2+4
        )
        self.queue_table.setFixedHeight(max(62,min(170,table_height)))
        error=getattr(queue,"load_error","") if queue is not None else ""
        self.queue_group.setVisible(bool(operations or error))
        active=sum(
            operation.state in {"waiting","running"}
            for operation in operations
        )
        attention=sum(
            operation.state in {"failed","cancelled"}
            for operation in operations
        )
        if active:
            self.queue_group.setTitle("Confirmed setup queue")
            self.queue_help.setText(
                f"{active:,} setup operation{'s are' if active!=1 else ' is'} "
                "waiting or running. Waiting work can be cancelled independently; "
                "failed work can be retried after its prerequisites are corrected."
            )
        elif attention or error:
            self.queue_group.setTitle("Setup needs attention")
            self.queue_help.setText(
                "No setup work is currently running. Select a failed or cancelled "
                "item to retry it after correcting its prerequisites."
            )
        else:
            self.queue_group.setTitle("Recent setup results")
            self.queue_help.setText(
                "All listed setup work is complete. Nothing is waiting or running."
            )
        if error:
            self.status.setText(error); self.status.setToolTip(error)
        transitioned=any(
            previous.get(operation.id) in {"waiting","running"}
            and operation.state in {"complete","failed","cancelled"}
            for operation in operations
        )
        if transitioned:
            self.service=self.dependency_service()
            self._wd_runtime_import_check=None
            self.refresh()
            latest=next((
                value for value in reversed(operations)
                if previous.get(value.id) in {"waiting","running"}
                and value.state in {"complete","failed","cancelled"}
            ),None)
            if latest is not None:
                self.status.setText(latest.detail)
                self.status.setToolTip(latest.error or latest.result or latest.detail)
        self._update_queue_actions()

    @staticmethod
    def _setup_operation_display_label(operation) -> str:
        label=str(getattr(operation,"label","") or "Setup")
        if getattr(operation,"state","")!="complete":
            return label
        for active,complete in (
            ("Setting up ","Set up "),
            ("Downloading and verifying ","Downloaded and verified "),
            ("Installing ","Installed "),
            ("Repairing ","Repaired "),
            ("Importing and verifying ","Imported and verified "),
            ("Exporting ","Exported "),
        ):
            if label.casefold().startswith(active.casefold()):
                return complete+label[len(active):]
        return label

    def _update_queue_actions(self):
        operation_id=self._selected_queue_operation_id(); queue=self._setup_queue()
        operations=tuple(queue.snapshot()) if queue is not None else ()
        operation=next((
            value for value in operations
            if value.id==operation_id
        ),None)
        self.cancel_waiting_button.setVisible(any(
            value.state=="waiting" for value in operations
        ))
        self.retry_setup_button.setVisible(any(
            value.state in {"failed","cancelled"} for value in operations
        ))
        self.cancel_waiting_button.setEnabled(
            operation is not None and operation.state=="waiting",
        )
        self.retry_setup_button.setEnabled(
            operation is not None and operation.state in {"failed","cancelled"},
        )

    def cancel_waiting_setup(self):
        queue=self._setup_queue(); operation_id=self._selected_queue_operation_id()
        if queue is None or not operation_id:
            return
        _accepted,message=queue.cancel_waiting(operation_id)
        self.status.setText(message); self.refresh_setup_queue()

    def retry_selected_setup(self):
        queue=self._setup_queue(); operation_id=self._selected_queue_operation_id()
        if queue is None or not operation_id:
            return
        _accepted,message=queue.retry(operation_id)
        self.status.setText(message); self.refresh_setup_queue()

    def _queue_confirmed_setup(
        self,kind: str,label: str,params: dict|None=None,
    ) -> bool:
        queue=self._setup_queue()
        if queue is None:
            return False
        try:
            outcome=queue.enqueue_confirmed(kind,label,params)
        except (OSError,RuntimeError,ValueError) as exc:
            message=str(exc)
            self.status.setText(f"Setup was not queued: {message}")
            QMessageBox.warning(self,"Setup was not queued",message)
            return True
        self.status.setText(outcome.message)
        self.refresh_setup_queue()
        if not outcome.accepted:
            QMessageBox.information(
                self,"Setup did not enter the queue",outcome.message,
            )
        return True

    def closeEvent(self,event):
        if self._video_review_task is not None:
            self._video_review_closing=True;self.cancel_video_review();event.ignore();return
        if self._queue_event_callback is not None and self.state is not None:
            self.state.events.unsubscribe(
                "setup_queue_changed",self._queue_event_callback,
            )
            self._queue_event_callback=None
        super().closeEvent(event)

    def reject(self):
        if self._video_review_task is not None:
            self._video_review_closing=True;self.cancel_video_review();return
        super().reject()

    def cancel_video_review(self):
        task=self._video_review_task
        if task is not None and task.job_id:
            task.manager.cancel(task.job_id)
            self.status.setText("Stopping Video review; waiting for the current read to return.")

    def _run_video_review_worker(self,label,operation,completed,*,cancellable=True):
        if self._video_review_task is not None:return
        state=self.state;factory=self.dependency_service_factory
        if state is None or not hasattr(state,"jobs"):
            self.status.setText("Video review needs the application-owned worker queue.");return
        def work(job):
            def check():
                if not job.checkpoint():raise InterruptedError("Video review stopped")
            check()
            if self.state is not state or self.dependency_service_factory is not factory:
                raise RuntimeError("Video review context changed. Review again.")
            return operation(check)
        task=CoordinatedCallTask(state.jobs,label,work,resource="io")
        self._video_review_task=task;self.set_busy(True,label)
        if hasattr(self,"cancel_video_review_button"):
            self.cancel_video_review_button.setVisible(cancellable)
            self.cancel_video_review_button.setEnabled(True)
        def finish(result=None,error=None,cancelled=False):
            if not isValid(self) or self._video_review_task is not task:return
            self._video_review_task=None;self.set_busy(False)
            if hasattr(self,"cancel_video_review_button"):self.cancel_video_review_button.hide()
            if self._video_review_closing:
                self.close();return
            if cancelled:
                self.status.setText("Video review stopped. Any confirmed queued work remains visible below.")
                self.refresh_setup_queue();return
            if error is not None:
                self.status.setText(f"Video setup held: {error}")
                QMessageBox.warning(self,"Video setup held",str(error));return
            if self.state is not state or self.dependency_service_factory is not factory:
                self.status.setText("Video review context changed. Review again.");return
            completed(result)
        task.signals.complete.connect(lambda result:finish(result))
        task.signals.failed.connect(lambda error:finish(error=error))
        task.signals.cancelled.connect(lambda:finish(cancelled=True))
        self.destroyed.connect(lambda *_:task.cancel())
        try:task.start()
        except Exception as exc:finish(error=str(exc))

    def focus_capability(self,capability: str,model_ids: list[str] | None=None):
        self.select_task("install")
        if self.embedded and capability in self.capability_rows:
            self._focused_capability=capability
            self._focused_label={
                "application":"Core application", "metadata":"Metadata",
                "wd":"AI Tags", "joy":"AI Descriptions", "video":"Optional Video",
            }[capability]
            self.title_label.setText(f"Set up {self._focused_label}")
            self._update_task_intro(self.task_indexes["install"])
            self.show_all_setup_button.setChecked(False)
            self.show_all_setup_button.show()
            self._show_all_setup_options(False)
        if capability=="wd" and model_ids is not None:
            self.requested_wd_models=[value for value in model_ids if value in WD_TAGGER_MODELS]
        button={"application":self.app_button,"wd":self.wd_button,"joy":self.joy_button,"metadata":self.exif_button,"video":getattr(self,"video_button",None)}.get(capability)
        if button is not None:
            if button.isHidden() and capability in {"wd","joy"}:
                button=self.wd_manage_button if capability=="wd" else self.joy_manage_button
            button.setFocus(Qt.OtherFocusReason)

    def note_wd_runtime_failure(self) -> None:
        """Prefer a just-observed preparation failure over an older load check."""
        self._wd_runtime_import_check=OptionalRuntimeImportCheck(
            False,
            "AI Tag preparation could not load ONNX Runtime in this session. "
            "Open Manage AI Tags to Check Runtime or review Repair.",
        )
        self.refresh()

    def clear_embedded_focus(self):
        """Restore ordinary Components when a contextual Setup return ends."""
        if not self.embedded or not self._focused_capability:
            return
        self._focused_capability=""
        self._focused_label=""
        self.title_label.setText("Components and downloads")
        self._update_task_intro(self.task_indexes["install"])
        self.show_all_setup_button.setChecked(False)
        self.show_all_setup_button.hide()
        for group in self.capability_groups.values():
            group.show()
        for row in self.capability_rows.values():
            row.show()
        self.upscaler_models_button.show()
        self.task_tabs.setCurrentIndex(self.task_indexes["install"])
        self.task_tabs.tabBar().show()

    def refresh_inventory(self):
        """Discard cached readiness and recheck the current local installation."""
        self.service=self.dependency_service()
        self._wd_runtime_import_check=None
        self.refresh()

    def refresh(self,*,component_pack_available: bool | None=None):
        selected_name=self.selected_name(); self.table.clear()
        components=self.service.components(include_sizes=False)
        for component in components:
            item=QTreeWidgetItem((
                "Ready" if component.installed else "Missing",component.name,component.version or "—",
                component.group,component.purpose,
                component.location or (component.detail if component.installed else "Not installed"),
            ))
            item.setData(0,Qt.UserRole,component.name); self.table.addTopLevelItem(item)
            if component.name==selected_name: self.table.setCurrentItem(item)
        for column in range(5): self.table.resizeColumnToContents(column)
        self.table.setColumnWidth(5,max(160,self.table.columnWidth(5)))
        header=self.table.header()
        for column in range(self.table.columnCount()):
            label=self.table.headerItem().text(column)
            header_width=header.fontMetrics().horizontalAdvance(label)+12
            self.table.setColumnWidth(
                column,max(header_width,self.table.columnWidth(column)),
            )
        names={item.name:item for item in components}
        app_ready=not any(not item.installed for item in components if item.required)
        wd_names=capability_components("wd",self.service)
        if self.requested_wd_models:
            wd_names={*DependencyService.WD_PACKAGES,*(self.service.wd_component_name(value) for value in self.requested_wd_models)}
        wd_ready=all(names[name].installed for name in wd_names)
        if self._wd_runtime_import_check is not None and not self._wd_runtime_import_check.loaded:
            wd_ready=False
        joy_names=capability_components("joy",self.service)
        joy_ready=all(names[name].installed for name in joy_names)
        capability_states=(
            ("application",self.app_button,app_ready,"Repair…","Repair…",capability_components("application",self.service)),
            (
                "wd",self.wd_button,wd_ready,
                "Choose AI Tag Setup…",
                "Review AI Tag Setup…",wd_names,
            ),
            ("joy",self.joy_button,joy_ready,"Choose AI Description Setup…","Review AI Description Setup…",joy_names),
            ("metadata",self.exif_button,names["ExifTool"].installed,"Set Up…","Complete Setup…",capability_components("metadata",self.service)),
        )
        if hasattr(self,"video_button"):
            component=names.get("PyAV")
            capability_states+=(("video",self.video_button,bool(component and component.installed and not getattr(self.state,"video_component_hold","")),"Set Up Video","Review Video Setup",{"PyAV"}),)
        missing_capabilities=sum(not ready for _key,_button,ready,_action,_partial,_names in capability_states)
        recovered=len(self.service.recovered_removals)+len(getattr(self.service,"recovered_installs",()))
        if recovered:
            self.status.setText(f"Recovered {recovered:,} model folder(s) after an interrupted component change.")
        else:
            self.status.setText("Components are installed; AI Tag native load has not been checked." if not missing_capabilities and self._wd_runtime_import_check is None and wd_ready else
                                "Everything is available." if not missing_capabilities else
                                ("1 capability needs setup." if missing_capabilities==1 else f"{missing_capabilities:,} capabilities need setup."))
        for key,button,ready,action,partial_action,component_names in capability_states:
            size=sum(names[name].size_bytes for name in component_names if name in names)
            installed_count=sum(bool(names[name].installed) for name in component_names if name in names)
            status="Ready" if ready else ("Partially ready" if installed_count else "Not set up")
            if key=="video" and getattr(self.state,"video_component_hold",""):
                status="Recovery held"
                button.setToolTip(self.state.video_component_hold)
            if key=="wd" and installed_count==len(component_names):
                status=(
                    "Runtime loads" if self._wd_runtime_import_check is not None
                    and self._wd_runtime_import_check.loaded else
                    "Needs repair" if self._wd_runtime_import_check is not None else
                    "Installed; native load not checked"
                )
            if size: status+=f" · {self.service.format_size(size)} installed"
            self.capability_status[key].setText(status)
            was_focused=button.hasFocus()
            button.setText(partial_action if not ready and installed_count else action)
            button.setVisible(not ready and not (
                key=="wd" and self._wd_runtime_import_check is not None
                and not self._wd_runtime_import_check.loaded
            ))
            if ready and was_focused and key==self._focused_capability and key in {"wd","joy"}:
                (self.wd_manage_button if key=="wd" else self.joy_manage_button).setFocus(Qt.OtherFocusReason)
        if self._focused_capability and not recovered:
            focused_ready=next(ready for key,_button,ready,*_rest in capability_states
                               if key==self._focused_capability)
            self.status.setText(
                "AI Tag files are installed. Choose Manage AI Tags to check the native runtime before use."
                if self._focused_capability=="wd" and focused_ready
                and self._wd_runtime_import_check is None else
                self._wd_runtime_import_check.summary
                if self._focused_capability=="wd" and self._wd_runtime_import_check is not None else
                f"{self._focused_label} is ready. Close Setup and start your task again."
                if focused_ready else f"{self._focused_label} needs setup."
            )
        self.wd_manage_button.setVisible(True)
        self.joy_manage_button.setVisible(True)
        if self.table.topLevelItemCount() and not self.table.currentItem(): self.table.setCurrentItem(self.table.topLevelItem(0))
        self.export_pack_button.setEnabled(bool(self._exportable_models()))
        # Setup's inventory worker already inspected package metadata. Consume
        # that result during delivery; explicit later refreshes inspect anew.
        self.export_components_button.setEnabled(
            self.service.component_pack_available()
            if component_pack_available is None else component_pack_available
        )
        self.capabilities_changed.emit()

    def selected_name(self):
        item=self.table.currentItem()
        return str(item.data(0,Qt.UserRole)) if item else ""

    def _exportable_models(self):
        available=[]
        for model_id in (*WD_TAGGER_MODELS,*PLAIN_LANGUAGE_MODELS):
            name=(
                self.service.wd_component_name(model_id)
                if model_id in WD_TAGGER_MODELS else
                self.service.plain_language_component_name(model_id)
            )
            component=self.service.component(name)
            if component and component.installed and component.version==MODEL_ACQUISITIONS[model_id].revision:
                available.append((model_id,self.service.model_display_name(model_id)))
        return available

    def import_model_pack(self):
        selected=QFileDialog.getOpenFileName(
            self,"Choose Offline Model Pack","","CollectiGen model pack (*.zip)",
        )[0]
        if not selected: return
        try: info=self.service.inspect_model_pack(selected)
        except Exception as exc:
            QMessageBox.critical(self,"Offline Model Pack Was Not Accepted",str(exc)); return
        target=self.service.model_target(info.model_id)
        existing="\n\nThe currently installed model will be preserved unless the complete verified replacement succeeds." if target.exists() else ""
        details=(
            f"Model: {info.display_name}\nRevision: {info.revision}\n"
            f"Unpacked size: {self.service.format_size(info.size_bytes)} across {info.file_count:,} files\n"
            f"License: {info.license_name}\nTerms: {info.license_url}\nSource: {info.source_url}\n"
            f"Install to: {target}\n\nThis import reads only the selected local file and makes no network request."
            f"{existing}\n\nImport and verify this model?"
        )
        if QMessageBox.question(self,"Import Offline Model Pack",details)!=QMessageBox.Yes: return
        self.run_task(
            f"Importing and verifying {info.display_name}…",
            lambda:self.service.import_model_pack(selected),
            f"{info.display_name} imported",
            failure_title="Offline Model Pack Import Failed",
        )

    def import_component_pack(self):
        selected=QFileDialog.getOpenFileName(
            self,"Choose Offline AI Component Pack","","CollectiGen AI component pack (*.zip)",
        )[0]
        if not selected: return
        try: info=self.service.inspect_component_pack(selected)
        except Exception as exc:
            QMessageBox.critical(self,"Offline AI Component Pack Was Not Accepted",str(exc)); return
        profiles="\n".join(f"  • {value}" for value in info.display_names)
        existing=(
            "\n\nThis replaces the current optional AI Python environment only after the complete "
            "pack passes verification. Existing models, settings, and the previous environment "
            "are preserved if import fails."
            if self.service.component_pack_available() else ""
        )
        details=(
            f"Profiles:\n{profiles}\n\nPython: {info.python_version}\n"
            f"Runtime platform: {info.runtime_platform}\n"
            f"Unpacked size: {self.service.format_size(info.size_bytes)} across {info.file_count:,} files\n\n"
            "This import reads only the selected local file and makes no network request. "
            "Model weights are separate and can be moved with an Offline Model Pack."
            f"{existing}\n\nImport and verify these optional AI components?"
        )
        if QMessageBox.question(self,"Import Offline AI Components",details)!=QMessageBox.Yes: return
        self.run_task(
            "Importing and verifying optional AI components…",
            lambda:self.service.import_component_pack(selected),
            "Offline AI components imported",
            failure_title="Offline AI Component Import Failed",
        )

    def export_component_pack(self):
        if not self.service.component_pack_available():
            self.status.setText("No verified optional AI component environment is available to export."); return
        suggested="CollectiGen-optional-AI-components.zip"
        selected=QFileDialog.getSaveFileName(
            self,"Export Offline AI Component Pack",suggested,"CollectiGen AI component pack (*.zip)",
        )[0]
        if not selected: return
        if not selected.casefold().endswith(".zip"): selected+=".zip"
        if QMessageBox.question(
            self,"Export Offline AI Components",
            "Create a verified offline pack containing the complete installed optional AI Python environment?\n\n"
            "The pack may be several gigabytes and can contain third-party packages under separate license terms. "
            "Model weights are not included. Export makes no network request and changes no installed component.",
        )!=QMessageBox.Yes: return
        self.run_task(
            "Exporting and verifying optional AI components…",
            lambda:self.service.export_component_pack(selected),
            "Offline AI component pack exported",
        )

    def export_model_pack(self):
        available=self._exportable_models()
        if not available:
            self.status.setText("No exact-revision installed model is available to export."); return
        selected_component=self.service.component(self.selected_name())
        selected_id=(
            self.service.model_id_for_component(selected_component.name)
            if selected_component and selected_component.action=="wd_model" else
            self.service.model_id_for_component(selected_component.name)
            if selected_component and selected_component.action=="joy_model"
            else ""
        )
        model_id=selected_id if any(value==selected_id for value,_name in available) else ""
        if not model_id and len(available)==1: model_id=available[0][0]
        if not model_id:
            labels=[name for _value,name in available]
            chosen,accepted=QInputDialog.getItem(
                self,"Export Installed Model","Choose the verified installed model to export:",labels,0,False,
            )
            if not accepted: return
            model_id=next(value for value,name in available if name==chosen)
        terms=MODEL_ACQUISITIONS[model_id]
        display_name=self.service.model_display_name(model_id)
        suggested="CollectiGen-"+display_name.replace(" ","-")+"-model-pack.zip"
        selected=QFileDialog.getSaveFileName(
            self,"Export Offline Model Pack",suggested,"CollectiGen model pack (*.zip)",
        )[0]
        if not selected: return
        if not selected.casefold().endswith(".zip"): selected+=".zip"
        if QMessageBox.question(
            self,"Export Offline Model Pack",
            f"Create a verified offline pack for {display_name}?\n\n"
            f"License: {terms.license_name}\nTerms: {terms.license_url}\n\n"
            "The pack may be large. Keep or share it only as permitted by the model license. "
            "Export does not access the network and does not remove the installed model.",
        )!=QMessageBox.Yes: return
        self.run_task(
            f"Exporting {display_name}…",
            lambda:self.service.export_model_pack(model_id,selected),
            "Offline model pack exported",
        )

    def dependency_service(self):
        factory=self.dependency_service_factory if self.dependency_service_factory is not None else DependencyService
        return factory(self.settings)

    def setup_video(self):
        """Review the exact optional plan, then use only the original guarded queue."""
        if self._video_review_task is not None:return
        self._video_review_closing=False
        from core.setup_operation_queue import SetupOperationQueue
        queue=self._setup_queue()
        if queue is None:
            self.status.setText("Video setup needs the application-owned queue and decoder recovery guards.")
            return
        service=self.service
        def prepare(check):
            check()
            plan=service.preview_video_install(check_interrupt=check)
            component=service.component("PyAV");check()
            if component is None:raise RuntimeError("The reviewed Video component is unavailable.")
            return plan,component
        def reviewed(result):
            plan,component=result
            params=SetupOperationQueue.video_plan_params(plan)
            if QMessageBox.question(self,"Set Up Optional Video",
            "Set up local video verification and playback? Image work remains available.\n\n"
            f"Component: PyAV 18.1.0\nSource: official PyPI hash-locked wheel\n"
            f"Destination: {plan.destination_root}\nProfile: {plan.profile_id}\n\n"
            f"License: {component.license_name}\nTerms: {component.license_url}\nSource: {component.source_url}\n\n"
            "Installation needs a qualified profile and waits for video decoders to stop. "
            "Local AI components pause while their shared software environment is updated; existing profiles are retained. "
                "No model or media file is changed. Continue?")!=QMessageBox.Yes:
                self.status.setText("Video setup declined; image work remains available.");return
            def enqueued(outcome):
                self.status.setText(outcome.message);self.refresh_setup_queue()
                if not outcome.accepted:QMessageBox.information(self,"Setup did not enter the queue",outcome.message)
            self._run_video_review_worker("Checking confirmed Video setup",
                lambda check:queue.enqueue_confirmed("setup_video","Setting Up Optional Video",params,check_interrupt=check),
                enqueued,cancellable=False)
        self._run_video_review_worker("Reviewing optional Video install plan",prepare,reviewed)

    def setup_application(self):
        missing=[
            item for item in self.service.components(include_sizes=False)
            if item.required and not item.installed
        ]
        if not missing: self.status.setText("Application components are ready."); return
        manual=[item.name for item in missing if not item.package and item.action!="exiftool"]
        if manual:
            QMessageBox.warning(self,"Manual update required","These components cannot be installed inside the running app:\n\n"+"\n".join(manual))
        packages=[item.package for item in missing if item.package]
        install_exif=any(item.action=="exiftool" for item in missing)
        if not packages and not install_exif: return
        details=[]
        if packages: details.append("Install:\n"+"\n".join(packages))
        if install_exif: details.append("Download and verify the bundled ExifTool Metadata utility.")
        if QMessageBox.question(
            self,"Repair application components","\n\n".join(details)+"\n\nContinue?",
        )!=QMessageBox.Yes: return
        if self._queue_confirmed_setup(
            "setup_application","Repairing application components",
            {"packages":packages,"install_exiftool":install_exif},
        ):
            return
        def repair():
            results=[]
            if packages: results.append(self.service.install_packages(packages))
            if install_exif: results.append(self.service.download_exiftool())
            return "\n".join(results)
        self.run_task("Repairing application components…",repair,"Application components ready")

    def setup_wd(self):
        initial_models=(
            self.requested_wd_models
            or [
                value for value in self.settings.enabled_wd_taggers
                if value in WD_TAGGER_MODELS
            ]
            or [WD_EVA02_MODEL]
        )
        selection=self._choose_wd_setup_configuration(initial_models)
        if selection is None:return False
        model_ids,consensus_minimum,preset_label=selection
        packages=self.service.missing_packages(DependencyService.WD_PACKAGES)
        missing_models=[value for value in model_ids if not self.service.component(self.service.wd_component_name(value)).installed]
        details=[
            f"Selected setup: {preset_label}",
            f"Use {len(model_ids)} model{'s' if len(model_ids)!=1 else ''}; "
            f"require at least {consensus_minimum} matching "
            f"vote{'s' if consensus_minimum!=1 else ''} for model consensus.",
        ]
        if packages:
            profile=self.service.optional_profile_id("wd")
            details.append(f"Install the exact reviewed {profile} runtime profile with verified wheel hashes.")
        if missing_models: details.append("Download requested models:\n"+"\n".join(self.service.wd_component_name(value) for value in missing_models))
        if missing_models:
            details.append("Model source: Hugging Face · License: Apache-2.0 · Exact reviewed revisions are recorded after download.")
        if not packages and not missing_models:
            details.append(
                "All selected models and the shared runtime are already installed. "
                "Continuing only saves and enables this selection."
            )
        if any("[cuda,cudnn]" in value for value in packages):
            details.append("NVIDIA CUDA/cuDNN packages use NVIDIA proprietary software terms. Review: https://docs.nvidia.com/cuda/eula/index.html")
        if QMessageBox.question(self,"Set Up AI Tags",self._wd_setup_confirmation(details))!=QMessageBox.Yes: return False
        previous_models=list(self.settings.enabled_wd_taggers)
        previous_consensus=int(self.settings.wd_consensus_minimum)
        if (
            previous_models!=list(model_ids)
            or previous_consensus!=consensus_minimum
        ):
            try:
                self.settings.enabled_wd_taggers=list(model_ids)
                self.settings.wd_consensus_minimum=consensus_minimum
                self.settings.save()
            except Exception as exc:
                self.settings.enabled_wd_taggers=previous_models
                self.settings.wd_consensus_minimum=previous_consensus
                QMessageBox.critical(
                    self,"AI Tag selection was not saved",
                    "Nothing was queued or downloaded because the selected "
                    f"models could not be saved.\n\n{exc}",
                )
                return False
            self.requested_wd_models=list(model_ids)
            self.settings_changed.emit()
        if not packages and not missing_models:
            self.status.setText("Selected AI Tag setup is ready.")
            self.refresh()
            return True
        if self._queue_confirmed_setup(
            "setup_wd","Setting Up AI Tags",{"model_ids":list(model_ids)},
        ):
            return True
        self.run_task(
            "Setting Up AI Tags…",
            lambda progress:self.service.setup_wd(
                None,download_model=bool(missing_models),model_ids=model_ids,progress=progress,
            ),
            "AI Tags Ready",with_progress=True,
        )
        return True

    def _choose_wd_setup_configuration(self,model_ids):
        dialog=WdSetupSelectionDialog(
            self.service,model_ids,
            int(getattr(self.settings,"wd_consensus_minimum",1)),self,
        )
        if dialog.exec()!=QDialog.Accepted:
            return None
        return dialog.selected_configuration()

    @staticmethod
    def _wd_setup_confirmation(details) -> str:
        privacy_note="After setup, AI Tag generation runs locally on this computer. Images are not uploaded for analysis."
        acquisition_note="Setup may connect to the internet to download the missing runtime components listed below."
        return privacy_note+"\n\n"+acquisition_note+"\n\n"+"\n\n".join(details)+"\n\nContinue with setup?"

    def download_wd_model(self,model_id: str=""):
        if not model_id:
            model_id=self.service.model_id_for_component(self.selected_name())
        if model_id not in WD_TAGGER_MODELS:
            return
        component=self.service.component(self.service.wd_component_name(model_id))
        if not model_id or component is None: return
        packages=self.service.missing_packages(DependencyService.WD_PACKAGES)
        model_dir=self.service.wd_model_dir_for(model_id)
        profile=self.service.optional_profile_id("wd")
        package_note=f"\n\nCollectiGen will also install the exact reviewed {profile} runtime profile with verified wheel hashes." if packages else ""
        terms=MODEL_ACQUISITIONS[model_id]
        gpu_note="\n\nNVIDIA CUDA/cuDNN packages use NVIDIA proprietary software terms." if any("[cuda,cudnn]" in value for value in packages) else ""
        if QMessageBox.question(self,f"Set up {component.name}",f"Install everything required and download the model to:\n\n{model_dir}{package_note}\n\nSource: {terms.source_url}\nLicense: {terms.license_name}\nRevision: {terms.revision}{gpu_note}\n\nContinue?")!=QMessageBox.Yes: return
        if self._queue_confirmed_setup(
            "setup_wd",f"Setting up {component.name}",
            {"model_ids":[model_id]},
        ):
            return
        self.run_task(
            f"Setting up {component.name}…",
            lambda progress:self.service.setup_wd(
                None,download_model=True,model_ids=[model_id],progress=progress,
            ),
            f"{component.name} ready",with_progress=True,
        )

    def setup_joy(self):
        model_id=str(
            getattr(self.settings,"plain_language_model",JOYCAPTION_MODEL)
            or JOYCAPTION_MODEL
        )
        if model_id not in PLAIN_LANGUAGE_MODELS:
            model_id=JOYCAPTION_MODEL
        display=PLAIN_LANGUAGE_MODELS[model_id][0]
        component_name=self.service.plain_language_component_name(model_id)
        hardware=self.service.component(DependencyService.JOY_HARDWARE_COMPONENT)
        if hardware is None or not hardware.installed:
            self.status.setText("Setup did not start. The reviewed caption-model profile requires an NVIDIA GPU.")
            return False
        packages=self.service.missing_packages(
            capability_components("joy",self.service,model_id)
        )
        model_ready=bool(self.service.component(component_name).installed)
        if not packages and model_ready: self.status.setText("AI Descriptions are ready."); return
        details=[]
        if packages:
            details.append("Install the exact reviewed caption-model NVIDIA CUDA runtime profile with verified wheel hashes.")
            details.append("The official PyTorch CUDA wheel conveys NVIDIA runtime components under separate terms.")
        if not model_ready:
            size_note=(
                "multi-gigabyte" if model_id==JOYCAPTION_MODEL
                else "compact"
            )
            details.append(f"Download the {size_note} {display} model.")
        if not model_ready:
            terms=MODEL_ACQUISITIONS[model_id]
            details.append(f"Source: {terms.source_url}\nLicense: {terms.license_name}\n{terms.terms_notice}\nTerms: {terms.license_url}")
        if QMessageBox.question(self,"Set Up AI Descriptions",f"{display} runs locally and only produces reviewable AI Descriptions.\n\n"+"\n\n".join(details)+"\n\nContinue?")!=QMessageBox.Yes: return
        if self._queue_confirmed_setup(
            "setup_joy",f"Setting up {display}",{"model_id":model_id},
        ):
            return True
        self.run_task(
            f"Setting up {display}…",
            lambda progress:self.service.setup_joycaption(
                None,download_model=not model_ready,
                model_id=model_id,progress=progress,
            ),
            f"{display} ready",with_progress=True,
        )

    def download_joy_model(self,model_id: str=""):
        component=(
            self.service.component(self.service.plain_language_component_name(model_id))
            if model_id in PLAIN_LANGUAGE_MODELS else
            self.service.component(self.selected_name())
        )
        if not model_id:
            model_id=(
                self.service.model_id_for_component(component.name)
                if component is not None else ""
            )
        if model_id not in PLAIN_LANGUAGE_MODELS:
            return
        display=PLAIN_LANGUAGE_MODELS[model_id][0]
        hardware=self.service.component(DependencyService.JOY_HARDWARE_COMPONENT)
        if hardware is None or not hardware.installed:
            self.status.setText("Setup did not start. The reviewed caption-model profile requires an NVIDIA GPU.")
            return
        packages=self.service.missing_packages(
            capability_components("joy",self.service,model_id)
        )
        package_note="\n\nCollectiGen will also install the exact reviewed caption-model NVIDIA CUDA runtime profile with verified wheel hashes and separate NVIDIA runtime terms." if packages else ""
        terms=MODEL_ACQUISITIONS[model_id]
        model_dir=self.service.plain_language_model_dir_for(model_id)
        if QMessageBox.question(self,f"Set up {display}",f"Install everything required and download the model to:\n\n{model_dir}{package_note}\n\nSource: {terms.source_url}\nLicense: {terms.license_name}\n{terms.terms_notice}\nTerms: {terms.license_url}\nRevision: {terms.revision}\n\nContinue?")!=QMessageBox.Yes: return
        if self._queue_confirmed_setup(
            "setup_joy",f"Setting up {display}",{
                "model_id":model_id,"require_selected_model":False,
            },
        ):
            return
        self.run_task(
            f"Setting up {display}…",
            lambda progress:self.service.setup_joycaption(
                None,download_model=True,model_id=model_id,progress=progress,
            ),
            f"{display} ready",with_progress=True,
        )

    def _removal_preview(self,plan) -> str:
        folders="\n".join(f"  • {path}" for path in plan.model_paths) or "  • No model folders are currently present."
        packages="\n".join(f"  • {name}" for name in plan.distributions) or "  • No capability-exclusive Python packages are currently installed."
        retained="\n".join(f"  • {name}" for name in plan.retained) or "  • All application and shared components remain installed."
        return (
            f"Model folders:\n{folders}\n\nCapability-exclusive packages:\n{packages}\n\n"
            f"Kept installed:\n{retained}\n\nEstimated space: {self.service.format_size(plan.size_bytes)}\n{plan.effect}"
        )

    def manage_capability(self,capability: str,model_id: str=""):
        dialog=CapabilityManagerDialog(
            self.service,capability,self,focus_model_id=model_id,
            runtime_observation=(self._wd_runtime_import_check if capability=="wd" else None),
        )
        result=dialog.exec()
        if capability=="wd" and dialog.runtime_check is not None:
            self._wd_runtime_import_check=dialog.runtime_check
            self.refresh()
        if result!=QDialog.Accepted:
            return
        action,model_id=dialog.requested_action
        if action=="install_model":
            if capability=="wd": self.download_wd_model(model_id)
            else: self.download_joy_model(model_id)
        elif action=="uninstall_model":
            self._start_removal_inventory(capability,model_ids=[model_id])
        elif action=="install_runtime":
            self.setup_capability_runtime(capability)
        elif action=="uninstall_runtime":
            self._start_removal_inventory(capability,runtime_only=True)

    def open_upscaler_models(self):
        """Open the shared optional-upscaler catalog without changing components."""
        if self._task is not None:
            self.status.setText("Wait for the current Setup operation to finish before opening upscaler models.")
            return
        if self.state is None or not hasattr(self.state,"jobs"):
            self.status.setText("Upscaler models are available from Setup inside the application workspace.")
            return
        from core.model_resource_registry import ModelResourceRegistry
        from core.paths import MODEL_RESOURCES_PATH
        from .resolution_models_dialog import UpscalerModelsDialog
        roots=[profile.root for profile in self.settings.library_registry().profiles]
        roots.extend(str(getattr(self.settings,key,"")) for key in (
            "ingest_directory","problems_directory","recycle_directory",
            "external_write_backup_directory",
        ) if getattr(self.settings,key,""))
        dialog=UpscalerModelsDialog(
            ModelResourceRegistry(MODEL_RESOURCES_PATH),self.state.jobs,self,
            protected_roots=roots,allow_select=False,
        )
        dialog.exec()

    def setup_capability_runtime(self,capability: str):
        label="AI Tag" if capability=="wd" else "AI Description"
        if capability=="joy":
            hardware=self.service.component(DependencyService.JOY_HARDWARE_COMPONENT)
            if hardware is None or not hardware.installed:
                QMessageBox.information(
                    self,"AI Description Runtime Is Unavailable",
                    "The reviewed AI Description runtime requires a supported NVIDIA GPU. Nothing was installed.",
                )
                return
        profile=self.service.optional_profile_id(capability)
        terms=(
            "The GPU profile includes NVIDIA CUDA/cuDNN packages under NVIDIA's separate software terms."
            if capability=="wd" else
            "The profile includes the official PyTorch CUDA build and NVIDIA runtime components under separate terms."
        )
        if QMessageBox.question(
            self,f"Install Shared {label} Runtime",
            f"Install or repair the exact reviewed {profile} runtime?\n\n"
            "This runtime is shared by every installed model in this capability. "
            "Model files are managed separately.\n\n"
            f"{terms}\n\nContinue?",
        )!=QMessageBox.Yes:
            return
        model_id=str(
            getattr(self.settings,"plain_language_model",JOYCAPTION_MODEL)
            or JOYCAPTION_MODEL
        )
        params={"runtime_only":True}
        if capability=="joy": params["model_id"]=model_id
        kind="setup_wd" if capability=="wd" else "setup_joy"
        if self._queue_confirmed_setup(
            kind,f"Installing shared {label} runtime",params,
        ):
            return
        self.run_task(
            f"Installing shared {label} runtime…",
            lambda progress:self.service.install_profile(capability,progress=progress),
            f"Shared {label} runtime ready",with_progress=True,
        )

    def _start_removal_inventory(
        self,capability: str,*,model_ids: list[str] | None=None,
        runtime_only: bool=False,
    ):
        subject=(
            "shared AI Tag runtime" if capability=="wd" else
            "shared AI Description runtime"
        ) if runtime_only else (
            "installed AI Tag model" if capability=="wd" else
            "installed AI Description model"
        )
        status=f"Reviewing {subject}…"
        if self._task is not None and not self._task.is_finished():
            self.status.setText("Installed-component review is already running.")
            return
        if self.state is not None:
            started,active=self.state.begin_long_operation(
                "dependencies",status.rstrip("…"),resource="io",
            )
            if not started:
                self.status.setText(f"Management did not open — already running: {active}")
                QMessageBox.information(
                    self,"Uninstall Review Did Not Open",
                    f"CollectiGen is currently busy with:\n\n{active}\n\n"
                    "Uninstall inventory did not start. Wait for that task to "
                    "finish, or cancel it safely, then choose Uninstall again.",
                )
                return
        selected_ids=list(model_ids) if model_ids is not None else None
        service=self.service
        self.set_busy(True,status)
        task=AsyncCallTask(
            lambda:service.runtime_removal_plan(capability)
            if runtime_only else
            service.removal_plan(capability,model_ids=selected_ids)
        )
        self._task=task
        task.signals.complete.connect(self._management_inventory_complete)
        task.signals.failed.connect(self._management_inventory_failed)
        self.pool.start(task,-1)

    def _management_inventory_complete(self,plan):
        if self.state is not None:
            self.state.finish_long_operation("dependencies")
        self._task=None; self.set_busy(False)
        self.status.setText("Installed AI components reviewed.")
        self._show_removal_confirmation(plan)

    def _management_inventory_failed(self,message: str):
        if self.state is not None:
            self.state.finish_long_operation("dependencies")
        self._task=None; self.set_busy(False)
        self.status.setText("Installed-component review failed.")
        QMessageBox.critical(
            self,"Installed Component Review Failed",str(message),
        )

    def _show_removal_confirmation(self,plan):
        box=QMessageBox(self); box.setWindowTitle(f"Uninstall {plan.label}")
        box.setIcon(QMessageBox.Warning); box.setTextFormat(Qt.PlainText)
        box.setText(f"Uninstall {plan.label}?")
        box.setInformativeText(
            f"Space to reclaim: {self.service.format_size(plan.size_bytes)}\n\n"
            f"{plan.effect}\n\nExisting Image Knowledge and review evidence are preserved."
        )
        box.setDetailedText(self._removal_preview(plan))
        uninstall=box.addButton("Uninstall",QMessageBox.DestructiveRole)
        cancel=box.addButton(QMessageBox.Cancel); box.setDefaultButton(cancel); box.setEscapeButton(cancel)
        uninstall.setEnabled(bool(plan.model_paths or plan.distributions))
        box.exec()
        if box.clickedButton() is not uninstall: return
        def remove():
            result=self.service.remove(plan)
            parts=[f"Estimated space reclaimed: {self.service.format_size(result.reclaimed_bytes)}"]
            if result.removed_distributions: parts.append("Removed packages: "+", ".join(result.removed_distributions))
            if result.warnings: parts.extend(result.warnings)
            return "\n".join(parts)
        self.run_task(
            f"Removing {plan.label}…",remove,f"Removed {plan.label}",after=self.settings_changed.emit,
        )

    def setup_exiftool(self):
        component=self.service.component("ExifTool")
        if component and component.installed: self.status.setText("Metadata support is ready."); self.status.setToolTip(component.detail); return
        if QMessageBox.question(self,"Download ExifTool","Download the official Windows ExifTool package into CollectiGen's private runtime folder?\n\nNo Windows system folders will be changed.")!=QMessageBox.Yes: return
        if self._queue_confirmed_setup(
            "setup_exiftool","Downloading and verifying ExifTool",
        ):
            return
        self.run_task("Downloading and verifying ExifTool…",self.service.download_exiftool,"Metadata ready")

    def locate_exiftool(self):
        selected=QFileDialog.getOpenFileName(self,"Select exiftool.exe","","ExifTool (exiftool*.exe);;Applications (*.exe)")[0]
        if not selected: return
        self.run_task("Verifying ExifTool…",lambda:self.service.choose_exiftool(selected),"Metadata ready")

    def run_task(self,status,callable_,title,*,after=None,with_progress=False,
                 failure_title=None):
        if self.state is not None:
            started,active=self.state.begin_long_operation("dependencies",status.rstrip("…"))
            if not started:
                self.status.setText(f"Setup did not start — already running: {active}")
                QMessageBox.information(
                    self,"Setup did not start",
                    f"CollectiGen is currently busy with:\n\n{active}\n\n"
                    "Component setup did not start. Wait for that task to finish, or cancel it safely, then choose Set up again.",
                )
                return
        self.set_busy(True,status)
        task=ProgressCallTask(callable_) if with_progress else AsyncCallTask(callable_)
        self._task=task
        if with_progress:
            task.signals.progress.connect(self.task_progress)
        def complete(message):
            self.task_complete(title,str(message))
            if after is not None: after()
        task.signals.complete.connect(complete)
        failure_title=(failure_title or
                       title.removesuffix(" ready").removesuffix(" complete")+" failed")
        task.signals.failed.connect(lambda message:self.task_failed(failure_title,message)); self.pool.start(task)

    def task_progress(self,current,total,message):
        message=str(message or "Working on local AI setup…")
        if self.state is not None:
            self.state.update_long_operation("dependencies",current,total,message)
        self.status.setText(message)
        set_progress_bar(
            self.progress,current,total,detail=message,
            unknown_text=message,
        )

    def set_busy(self,busy,status=""):
        for control in self._controls: control.setEnabled(not busy)
        if hasattr(self,"upscaler_models_button"):
            self.upscaler_models_button.setEnabled(
                not busy and self._upscaler_catalog_available
            )
        self.table.setEnabled(not busy)
        if self.close_button is not None: self.close_button.setEnabled(not busy)
        self.progress.setVisible(busy)
        if busy:
            set_progress_bar(
                self.progress,detail=status,
                unknown_text=str(status or "Working…"),
            )
        if status: self.status.setText(status)

    def task_complete(self,title,message):
        if self.state is not None: self.state.finish_long_operation("dependencies")
        self.service=self.dependency_service()
        self._wd_runtime_import_check=None
        self.set_busy(False); self.refresh()
        status=title
        if self.service.optional_cleanup_pending:
            status+=" · Restart to finish old-file cleanup."
            message+=(
                "\n\nThe requested component change is complete. Restart "
                "CollectiGen to release and remove the previous local-AI files."
            )
        self.status.setText(status); self.status.setToolTip(message)

    def task_failed(self,title,message):
        if self.state is not None: self.state.finish_long_operation("dependencies")
        self.service=self.dependency_service()
        self._wd_runtime_import_check=None
        self.set_busy(False); self.refresh(); QMessageBox.critical(self,title,message)


def _verify_wd_runtime(parent,service) -> bool:
    """Contain native imports in the existing child probe while Qt remains responsive."""
    dialog=QDialog(parent)
    dialog.setWindowTitle("Check AI Tag Runtime")
    layout=QVBoxLayout(dialog)
    layout.addWidget(QLabel("Checking Pillow, NumPy and ONNX Runtime. No images or models are loaded."))
    cancel=QPushButton("Cancel")
    layout.addWidget(cancel)
    cancel.clicked.connect(dialog.reject)
    observation={}
    task=AsyncCallTask(service.check_wd_runtime_import)
    def complete(value):
        observation["result"]=value
        dialog.accept()
    def failed(_message):
        observation["result"]=OptionalRuntimeImportCheck(False,"The AI Tag runtime check failed. Review AI Tag Components and check again.")
        dialog.accept()
    task.signals.complete.connect(complete)
    task.signals.failed.connect(failed)
    dialog.rejected.connect(task.cancel)
    QTimer.singleShot(0,lambda:QThreadPool.globalInstance().start(task))
    accepted=dialog.exec()==QDialog.Accepted
    if not accepted:
        return False
    result=observation.get("result")
    if result is None or not result.loaded:
        QMessageBox.warning(parent,"AI Tag Runtime Needs Attention",getattr(result,"summary","The AI Tag runtime could not be verified. Review AI Tag Components."))
        return False
    return True


def ensure_capability(parent,settings,capability: str,task_name: str,*,allow_without: bool=False,on_setup_opened=None,dependency_service_factory=None) -> bool:
    """Offer native setup when a requested task is missing local components."""
    state=getattr(parent,"state",None)
    if state is not None and dependency_service_factory is not None and getattr(state,"dependency_service_factory",None) is not dependency_service_factory:
        raise ValueError("Capability review must retain the state's admitted dependency service factory.")
    factory=dependency_service_factory if dependency_service_factory is not None else getattr(state,"dependency_service_factory",None)
    context_factory=factory
    if factory is None:factory=DependencyService
    service=factory(settings); wanted=capability_components(capability,service)
    if capability in {"wd","joy"}:
        issue=service.optional_profile_runtime_issue()
        if issue:
            QMessageBox.warning(parent,"AI Runtime Needs Attention",issue)
            return False
    missing=[
        item for item in service.components(include_sizes=False)
        if item.name in wanted and not item.installed
    ]
    if not missing:
        return _verify_wd_runtime(parent,service) if capability=="wd" else True
    details="\n".join(f"• {item.name}" for item in missing)
    answer=QMessageBox.question(
        parent,"Setup required",
        f"{task_name} needs these local components:\n\n{details}\n\nOpen Setup and install them now?",
    )
    if answer==QMessageBox.Yes:
        if on_setup_opened is not None:
            on_setup_opened()
        window=parent.window() if hasattr(parent,"window") else None
        if window is not None and hasattr(window,"navigation"):
            window.navigation.setCurrentRow(6)
            setup=getattr(window,"setup_page",None)
            if setup is not None: setup.open_dependencies(capability)
            return allow_without
        DependencyDialog(settings,parent,focus_capability=capability,dependency_service_factory=context_factory).exec()
        refreshed=factory(settings); wanted=capability_components(capability,refreshed)
        missing=[
            item for item in refreshed.components(include_sizes=False)
            if item.name in wanted and not item.installed
        ]
        return (_verify_wd_runtime(parent,refreshed) if capability=="wd" and not missing else not missing or allow_without)
    return allow_without
