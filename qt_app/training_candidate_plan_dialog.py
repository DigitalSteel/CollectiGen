from __future__ import annotations

import json
import re
from dataclasses import dataclass,replace
from enum import Enum
from types import SimpleNamespace

from PySide6.QtCore import QEvent,QSignalBlocker,Qt,QTimer,Signal
from PySide6.QtWidgets import (
    QCheckBox,QComboBox,QDialog,QDialogButtonBox,QDoubleSpinBox,QFormLayout,
    QGroupBox,QHeaderView,QHBoxLayout,QInputDialog,QLabel,QLineEdit,QMessageBox,QPushButton,
    QScrollArea,QSpinBox,QSplitter,QTabWidget,QTextEdit,QTreeWidget,QTreeWidgetItem,QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core import (
    CandidateOverride,PERMISSION_FIELD_LABELS,TrainingCandidatePlan,
    TrainingCandidateSelectionService,TrainingConfiguration,TrainingGoal,
    TrainingPermissionDisposition,TrainingPlanDraftError,
    estimate_training_resources,validate_training_configuration,
)
from core.training_configuration import recommended_training_configuration
from core.training_presets import preview_training_preset

from .modal_feedback import FeedbackEnabledDialog
from .gallery import EmbeddedGallery
from .workflow_context import WorkflowContextSummary
from .workers import ProgressCallTask,start_qt_delivery_task


class CandidateFixAction(Enum):
    KNOWLEDGE = "knowledge"
    DESCRIPTIONS = "descriptions"
    CHARACTERS = "characters"
    METADATA = "metadata"
    PERMISSION = "permission"
    SETUP = "setup"
    PROFILE = "profile"
    REFRESH = "refresh"


class TrainingScopeKind(Enum):
    SELECTED = "selected"
    MATCHING = "matching"


@dataclass(frozen=True)
class TrainingScopeChoice:
    kind: TrainingScopeKind
    count: int
    description: str = ""


@dataclass(frozen=True)
class CandidateFixRequest:
    stable_id: str
    action: CandidateFixAction


def candidate_fix_action(code: str) -> CandidateFixAction:
    """Route a named evidence type; explanatory prose never selects an action."""
    if code.startswith("permission_"):
        return CandidateFixAction.PERMISSION
    if code=="profile_rating_excluded":
        return CandidateFixAction.PROFILE
    if code in {"conversion_review","low_resolution","extreme_aspect_ratio"}:
        return CandidateFixAction.SETUP
    if code in {"source_unavailable","portable_identity_unavailable"}:
        return CandidateFixAction.REFRESH
    if code in {"profile_missing_description","profile_thin_description"}:
        return CandidateFixAction.DESCRIPTIONS
    if code in {"profile_unknown_character","profile_unknown_outfit"}:
        return CandidateFixAction.CHARACTERS
    if code=="possible_filename_leakage":
        return CandidateFixAction.METADATA
    return CandidateFixAction.KNOWLEDGE


class TrainingCandidateGoalDialog(FeedbackEnabledDialog):
    """Capture the goal and exact projection profile before background work."""

    feedback_workflow = "Training dataset planning"

    def __init__(
        self,
        candidate_count: int,
        profile_store,
        maximum_content_rating: str,
        parent=None,
        *,
        current_profile_id: str = "",
        scope_choices: tuple[TrainingScopeChoice, ...] = (),
        collection_name: str = "",
        settings=None,
        jobs=None,
    ) -> None:
        super().__init__(parent)
        self.profile_store = profile_store
        self.maximum_content_rating = maximum_content_rating
        self._guidance_settings = settings
        self._guidance_jobs = jobs
        self._guided_profile = None
        self._guided_configuration = None
        self._guided_input_signature = None
        self._guided_target_id = ""
        self._guidance_output_issue = ""
        self.collection_name = str(collection_name or "").strip()
        collection_origin = bool(self.collection_name)
        self.setWindowTitle(
            "Build Training Data" if collection_origin else "Plan Training Dataset"
        )
        self.resize(720, 600)
        outer = QVBoxLayout(self)
        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setFrameShape(QScrollArea.NoFrame)
        content = QWidget()
        layout = QVBoxLayout(content)
        self.content_scroll.setWidget(content)
        outer.addWidget(self.content_scroll, 1)
        intro = QLabel(
            f"Choose what the full Collection “{self.collection_name}” should teach."
            if collection_origin
            else "Choose the images and what this dataset should teach."
        )
        intro.setWordWrap(True)
        intro.setObjectName("sectionTitle")
        layout.addWidget(intro)

        self.workflow_context=WorkflowContextSummary(self)
        layout.addWidget(self.workflow_context)

        self.scope_choices=scope_choices or (
            TrainingScopeChoice(TrainingScopeKind.SELECTED,candidate_count),
        )
        layout.addWidget(
            QLabel("Full Collection membership" if collection_origin else "Images to include in this plan")
        )
        self.scope_choice=QComboBox()
        self.scope_choice.setAccessibleName(
            "Full Collection membership" if collection_origin else "Training image scope"
        )
        labels={TrainingScopeKind.SELECTED:"Selected Images",TrainingScopeKind.MATCHING:"Matching Results"}
        for choice in self.scope_choices:
            label=(
                "Full Collection Membership"
                if collection_origin else labels[choice.kind]
            )
            self.scope_choice.addItem(f"{label} ({choice.count:,})",choice.kind)
            self.scope_choice.model().item(self.scope_choice.count()-1).setEnabled(choice.count>0)
        self.scope_choice.setCurrentIndex(next(
            (index for index,choice in enumerate(self.scope_choices) if choice.count>0),0,
        ))
        layout.addWidget(self.scope_choice)
        self.scope_detail=QLabel()
        self.scope_detail.setTextFormat(Qt.PlainText)
        self.scope_detail.setWordWrap(True)
        self.scope_detail.setObjectName("mutedLabel")
        layout.addWidget(self.scope_detail)

        layout.addWidget(QLabel("What should this dataset teach?"))
        self.goal_text = QLineEdit()
        self.goal_text.setPlaceholderText(
            "For example: this character's identity across varied outfits and settings"
        )
        self.goal_text.setAccessibleName("Plain-language training goal")
        self.goal_text.setAccessibleDescription(
            "Required description of the character, style, product, or concept the selected images should teach."
        )
        layout.addWidget(self.goal_text)

        layout.addWidget(QLabel("Goal type"))
        self.goal_kind = QComboBox()
        for label, value in (
            ("Character", "character"),
            ("Style", "style"),
            ("Product", "product"),
            ("Broader concept", "concept"),
        ):
            self.goal_kind.addItem(label, value)
        self.goal_kind.setAccessibleName("Training goal type")
        layout.addWidget(self.goal_kind)

        self.customize_button = QPushButton("Customize Model, Captions and Output…")
        self.customize_button.setCheckable(True)
        self.customize_button.setAccessibleDescription(
            "Show optional exact target, base-model, caption profile and output choices."
        )
        layout.addWidget(self.customize_button)
        self.customize_panel = QWidget()
        customize_layout = QVBoxLayout(self.customize_panel)
        customize_layout.setContentsMargins(0, 0, 0, 0)
        customize_layout.addWidget(QLabel("Caption and target profile"))
        self.profile_choice = QComboBox()
        self.profile_choice.setAccessibleName("Training projection profile")
        for profile in self.profile_store.all_profiles():
            self.profile_choice.addItem(
                f"{profile.name} — v{profile.version}", profile.profile_id
            )
        current = self.profile_choice.findData(current_profile_id)
        self.profile_choice.setCurrentIndex(current if current >= 0 else 0)
        customize_layout.addWidget(self.profile_choice)

        target_form=QFormLayout()
        self.target_version=QLineEdit()
        self.target_version.setText(self.selected_profile().target_model_version)
        self.target_version.setPlaceholderText("Optional exact family/version label")
        self.target_version.setAccessibleName("Target model version")
        self.base_model_id=QLineEdit()
        self.base_model_id.setPlaceholderText(
            "Portable registry ID or exact model hash; not a local path"
        )
        self.base_model_id.setAccessibleName("Base model stable identifier")
        target_form.addRow("Target version",self.target_version)
        target_form.addRow("Base model ID",self.base_model_id)
        self.output_kind=QComboBox()
        for label,value in (
            ("LoRA", "lora"),
            ("Full fine-tune", "full_fine_tune"),
            ("Embedding", "embedding"),
        ):
            self.output_kind.addItem(label,value)
        self.output_kind.setAccessibleName("Intended training output")
        self.output_kind.setAccessibleDescription(
            "Records the intended artifact kind for this plan. It does not verify "
            "trainer, base-model, or native fine-tune compatibility."
        )
        target_form.addRow("Intended output",self.output_kind)
        customize_layout.addLayout(target_form)
        layout.addWidget(self.customize_panel)
        self.customize_panel.hide()
        self.customize_button.toggled.connect(self._toggle_customization)

        self.model_guidance_button = QPushButton("Models && Guidance…")
        self.model_guidance_button.setEnabled(jobs is not None)
        self.model_guidance_button.clicked.connect(self._model_guidance)
        layout.addWidget(self.model_guidance_button)
        self.clear_guidance_button = QPushButton("Use Manual Profile")
        self.clear_guidance_button.clicked.connect(self._clear_guidance)
        self.clear_guidance_button.hide()
        layout.addWidget(self.clear_guidance_button)

        self.profile_detail = QLabel()
        self.profile_detail.setWordWrap(True)
        self.profile_detail.setObjectName("mutedLabel")
        layout.addWidget(self.profile_detail)
        consequence = QLabel(
            "This creates an in-memory planning preview only. It does not write captions "
            "or a package, change Image Knowledge, download a model, install a trainer, "
            "upload data, or start training."
        )
        consequence.setWordWrap(True)
        consequence.setObjectName("mutedLabel")
        layout.addWidget(consequence)
        layout.addStretch(1)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.continue_button = QPushButton(
            "Review Training Data" if collection_origin else "Build Read-Only Plan"
        )
        self.continue_button.setProperty("primary", True)
        buttons.addButton(self.continue_button, QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.goal_text.textChanged.connect(self._update_state)
        self.goal_kind.currentIndexChanged.connect(self._update_state)
        self.profile_choice.currentIndexChanged.connect(self._update_state)
        self.target_version.textChanged.connect(self._update_state)
        self.base_model_id.textChanged.connect(self._update_state)
        self.output_kind.currentIndexChanged.connect(self._update_state)
        self.scope_choice.currentIndexChanged.connect(self._update_state)
        self._update_state()

    def _toggle_customization(self, shown: bool) -> None:
        self.customize_panel.setMinimumHeight(
            self.customize_panel.sizeHint().height() if shown else 0
        )
        self.customize_panel.setVisible(shown)
        self.customize_button.setText(
            "Hide Model and Output Customization" if shown
            else "Customize Model, Captions and Output…"
        )

    def selected_scope_kind(self) -> TrainingScopeKind:
        return self.scope_choice.currentData()

    def selected_profile(self):
        if self._guided_profile is not None and self.profile_choice.currentData() == self._guided_profile.profile_id:
            return self._guided_profile.with_maximum_content_rating(self.maximum_content_rating)
        return self.profile_store.resolve(
            self.profile_choice.currentData()
        ).with_maximum_content_rating(self.maximum_content_rating)

    def selected_configuration(self):
        configuration=(
            self._guided_configuration
            if self._guided_configuration is not None
            and self._guidance_signature() == self._guided_input_signature
            else self._recommended_configuration()
        )
        return self._with_current_goal_configuration(configuration)

    def _recommended_configuration(self):
        profile=self.selected_profile()
        choice=self.scope_choices[self.scope_choice.currentIndex()]
        return recommended_training_configuration(
            target_model_family=profile.target_model_family,
            target_model_version=self.target_version.text().strip(),
            base_model_id=self.base_model_id.text().strip(),
            goal_kind=str(self.goal_kind.currentData() or "concept"),
            included_count=choice.count,
            profile_token_budget=profile.token_budget,
        )

    def _with_current_goal_configuration(self,configuration):
        profile=self.selected_profile()
        output_kind=str(self.output_kind.currentData() or configuration.output_kind)
        baseline=next(
            (item.value for item in configuration.recommendations
             if item.field=="output_kind"),
            configuration.output_kind,
        )
        override_reason=configuration.override_reason
        if output_kind!=baseline and not override_reason:
            override_reason=(
                f"User selected {self.output_kind.currentText()} as the intended "
                "training output before candidate review."
            )
        return replace(
            configuration,
            target_model_family=profile.target_model_family,
            target_model_version=self.target_version.text().strip(),
            base_model_id=self.base_model_id.text().strip(),
            output_kind=output_kind,
            override_reason=override_reason,
        )

    def _guidance_signature(self):
        profile = self.selected_profile()
        return (self.goal_text.text().strip(), self.goal_kind.currentData(), profile.profile_id, profile.version,
                self.target_version.text().strip(), self.base_model_id.text().strip(),
                self.output_kind.currentData())

    def _clear_guidance(self):
        previous = self._guided_profile
        self._guided_configuration = None
        self._guided_input_signature = None
        if previous is not None:
            index = self.profile_choice.findData(previous.profile_id)
            if index >= 0: self.profile_choice.removeItem(index)
        self._guided_profile = None
        self._guidance_output_issue = ""
        self.clear_guidance_button.hide()
        self._update_state()

    def _model_guidance(self):
        from core.model_resource_registry import ModelResourceRegistry
        from core.paths import MODEL_RESOURCES_PATH
        from .model_workspace_dialog import ModelWorkspaceDialog
        settings = self._guidance_settings
        roots = [p.root for p in settings.library_registry().profiles] if settings is not None else []
        roots.extend(str(getattr(settings, key, "")) for key in ("ingest_directory", "problems_directory", "recycle_directory", "external_write_backup_directory") if getattr(settings, key, ""))
        dialog = ModelWorkspaceDialog(ModelResourceRegistry(MODEL_RESOURCES_PATH), self._guidance_jobs, self,
            protected_roots=roots, settings=settings, select_plan=True, goal=self.goal_text.text(),
            goal_kind=self.goal_kind.currentData(), target_id=self._guided_target_id,
            current_configuration=self._with_current_goal_configuration(
                self._guided_configuration or self._recommended_configuration()
            ),
            image_count=self.scope_choices[self.scope_choice.currentIndex()].count)
        if dialog.exec() != QDialog.Accepted or dialog.selected_guidance is None: return
        plan = dialog.selected_guidance
        configuration=dialog.selected_guidance_configuration
        expected_output=str(self.output_kind.currentData() or "")
        if (configuration is None or not expected_output
                or configuration.output_kind!=expected_output):
            self._guidance_output_issue=(
                "Models & Guidance did not confirm the selected intended output. "
                "Choose a supported output, reopen guidance, or use a manual profile; "
                "the output intent was not rewritten."
            )
            self.clear_guidance_button.show()
            self._update_state()
            return
        previous_profile = self._guided_profile
        if previous_profile is not None and previous_profile.profile_id != plan.caption_profile.profile_id:
            previous_index = self.profile_choice.findData(previous_profile.profile_id)
            if previous_index >= 0: self.profile_choice.removeItem(previous_index)
        self._guided_profile = plan.caption_profile
        self._guided_configuration = configuration
        if self._guided_profile is None: return
        index = self.profile_choice.findData(self._guided_profile.profile_id)
        if index < 0:
            self.profile_choice.addItem(self._guided_profile.name, self._guided_profile.profile_id)
            index = self.profile_choice.count() - 1
        else: self.profile_choice.setItemText(index, self._guided_profile.name)
        self.profile_choice.setCurrentIndex(index)
        self.goal_text.setText(plan.request.goal)
        index = self.goal_kind.findData(plan.request.goal_kind)
        if index >= 0: self.goal_kind.setCurrentIndex(index)
        self.target_version.setText(plan.version)
        self.base_model_id.setText(self._guided_configuration.base_model_id)
        self._guided_input_signature = self._guidance_signature()
        self._guided_target_id = plan.request.target_id
        self._guidance_output_issue = ""
        self.clear_guidance_button.show()
        self._update_state()

    def training_goal(self) -> TrainingGoal:
        profile = self.selected_profile()
        return TrainingGoal(
            self.goal_text.text(),
            kind=str(self.goal_kind.currentData() or "concept"),
            target_model_family=profile.target_model_family,
            target_model_version=self.target_version.text().strip(),
            base_model_id=self.base_model_id.text().strip(),
        )

    def _update_state(self) -> None:
        choice=self.scope_choices[self.scope_choice.currentIndex()]
        self.continue_button.setEnabled(bool(self.goal_text.text().strip()) and choice.count>0)
        detail=(choice.description+"\n" if choice.description else "")
        scope=("full Collection membership" if self.collection_name
               else "selected images" if choice.kind==TrainingScopeKind.SELECTED
               else "matching images across all pages")
        detail+=f"Freeze exactly {choice.count:,} {scope}. Later search changes will not expand this draft."
        self.scope_detail.setText(detail)
        profile = self.selected_profile()
        target = profile.target_model_family or "Unspecified target family"
        if profile.target_model_version:
            target += f" {profile.target_model_version}"
        self.profile_detail.setText(
            f"Target: {target} • Caption strategy: {profile.caption_strategy} • "
            f"Trigger policy: {profile.trigger_policy}. Intended output: "
            f"{self.output_kind.currentText()}. This is a declared outcome; a "
            f"compatible trainer must validate it later. {profile.description}"
        )
        if self._guided_configuration is not None and self._guidance_signature() != self._guided_input_signature:
            self.continue_button.setEnabled(False)
            self.profile_detail.setText(self.profile_detail.text() + " Guidance inputs changed. Reopen Models & Guidance to review the updated plan, or choose Use Manual Profile. Your previous expert settings remain retained.")
        if self._guidance_output_issue:
            self.continue_button.setEnabled(False)
            self.profile_detail.setText(
                self.profile_detail.text()+" "+self._guidance_output_issue
            )
        configuration=self.selected_configuration()
        model_id=configuration.base_model_id
        selected_target=profile.target_model_family or "Unspecified target family"
        selected_version=self.target_version.text().strip()
        if selected_version:
            selected_target+=f" {selected_version}"
        model=(
            f"{profile.name} v{profile.version} for {selected_target}"
            + (f" • Base model: {model_id}" if model_id else " • Base model not chosen")
            + f" • Intended output: {self.output_kind.currentText()}"
        )
        if not self.goal_text.text().strip():
            readiness="Needs a plain-language training goal."
            next_step="Describe what the images should teach."
        elif choice.count<=0:
            readiness="No images are available in this scope."
            next_step="Choose a scope that contains images."
        elif self._guidance_output_issue:
            readiness="Guidance did not confirm the selected intended output."
            next_step="Choose a supported output, reopen Models & Guidance, or use a manual profile."
        elif self._guided_configuration is not None and self._guidance_signature() != self._guided_input_signature:
            readiness="Guidance inputs changed; the prior recommendation needs review."
            next_step="Reopen Models & Guidance, or choose Use Manual Profile."
        else:
            readiness="Ready to build a read-only candidate plan."
            next_step=(
                "Review the Collection's candidate data and blockers."
                if self.collection_name
                else "Build the plan, then review its candidates and blockers."
            )
        using=(
            f"Collection “{self.collection_name}”: {choice.count:,} full member(s)"
            if self.collection_name
            else f"Selected images: {choice.count:,}"
            if choice.kind==TrainingScopeKind.SELECTED
            else f"Matching results: {choice.count:,} across all result pages"
        )
        self.workflow_context.update_context(
            using=using,
            model=model,
            result=(
                "A read-only candidate plan. Saving later puts it in Training Drafts; "
                "package preparation and training remain explicit later steps."
            ),
            readiness=readiness,
            next_step=next_step,
        )


class TrainingRecommendationRestoreDialog(FeedbackEnabledDialog):
    """Review a saved snapshot diff before changing the local settings draft."""

    feedback_workflow="Restore training recommendation"

    def __init__(self,current: TrainingConfiguration,restored: TrainingConfiguration,parent=None):
        super().__init__(parent)
        self.setWindowTitle("Restore Saved Recommendation")
        self.resize(900,600)
        layout=QVBoxLayout(self)
        intro=QLabel(
            "Review the changes to this settings draft. These are the recommendation "
            "values saved with the plan, not fresh advice for your current machine or model. "
            "Use These Settings in the previous window to apply them to the plan."
        )
        intro.setWordWrap(True); layout.addWidget(intro)
        self.provenance=QTextEdit()
        self.provenance.setReadOnly(True); self.provenance.setMaximumHeight(100)
        self.provenance.setAccessibleName("Saved recommendation version and scope")
        self.provenance.setPlainText(
            f"Profile: {current.recommendation_profile_id}\nScope: {current.recommendation_scope}"
        )
        layout.addWidget(self.provenance)
        self.changes=QTreeWidget()
        self.changes.setHeaderLabels(["Setting","Current","Saved Recommendation","Why"])
        self.changes.setAccessibleName("Settings changed by restoring the saved recommendation")
        self.changes.setRootIsDecorated(False)
        self.changes.setWordWrap(True)
        for item in current.recommendations:
            before=current.value_for(item.field)
            after=restored.value_for(item.field)
            if before!=after:
                row=QTreeWidgetItem([
                    item.field.replace("_"," ").title(),
                    json.dumps(before,ensure_ascii=False),json.dumps(after,ensure_ascii=False),
                    item.reason,
                ])
                for column in range(4): row.setToolTip(column,row.text(column))
                self.changes.addTopLevelItem(row)
        if current.override_reason:
            row=QTreeWidgetItem(["Override Reason",current.override_reason,"Cleared",
                "All saved recommended values will be restored."])
            for column in range(4): row.setToolTip(column,row.text(column))
            self.changes.addTopLevelItem(row)
        for column,width in enumerate((170,130,190)):
            self.changes.setColumnWidth(column,width)
        self.changes.header().setStretchLastSection(True)
        layout.addWidget(self.changes,1)
        self.change_detail=QTextEdit()
        self.change_detail.setReadOnly(True); self.change_detail.setMaximumHeight(110)
        self.change_detail.setAccessibleName("Selected setting exact values and reason")
        def show_change(item,_previous=None):
            self.change_detail.setPlainText("\n".join(
                f"{label}: {item.text(column)}"
                for column,label in enumerate(("Setting","Current","Saved","Why"))
            ) if item is not None else "No settings need restoration.")
        self.changes.currentItemChanged.connect(show_change)
        self.changes.setCurrentItem(self.changes.topLevelItem(0))
        layout.addWidget(self.change_detail)
        detail=QLabel(
            "Model identity, output name, trigger/class terms, seed, image membership "
            "and saved evidence are kept. For new advice, return to the training goal's "
            "Models & Guidance. Restoring does not check hardware, prepare a package or start training."
        )
        detail.setWordWrap(True); layout.addWidget(detail)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.restore_button=QPushButton("Restore Saved Values")
        self.restore_button.setProperty("primary",True)
        buttons.addButton(self.restore_button,QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        buttons.button(QDialogButtonBox.Cancel).setDefault(True)


class TrainingPresetPreviewDialog(FeedbackEnabledDialog):
    """Review the complete preset change before replacing the settings draft."""

    feedback_workflow="Training preset preview"

    def __init__(self,preview,parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review Training Preset")
        self.resize(860,620)
        layout=QVBoxLayout(self)
        intro=QLabel("Review every change below. Apply updates this settings draft; package preparation and Start remain separate.")
        intro.setWordWrap(True); layout.addWidget(intro)
        self.changes=QTreeWidget()
        self.changes.setHeaderLabels(["Setting","Current","Proposed","Why"])
        self.changes.setRootIsDecorated(False)
        self.changes.setAccessibleName("Training preset changes")
        for change in preview.changes:
            self.changes.addTopLevelItem(QTreeWidgetItem([
                change.field.replace('_',' ').title(),str(change.before),str(change.after),change.reason]))
        if not preview.changes:
            self.changes.addTopLevelItem(QTreeWidgetItem(["No setting changes","","","Current values are retained."]))
        self.changes.header().setSectionResizeMode(0,QHeaderView.ResizeToContents)
        self.changes.header().setSectionResizeMode(1,QHeaderView.ResizeToContents)
        self.changes.header().setSectionResizeMode(2,QHeaderView.ResizeToContents)
        self.changes.header().setSectionResizeMode(3,QHeaderView.Stretch)
        self.changes.setWordWrap(True); layout.addWidget(self.changes,1)
        self.details=QTextEdit(); self.details.setReadOnly(True)
        self.details.setAccessibleName("Preset workload and limitations")
        self.details.setPlainText(_training_preset_text(preview))
        layout.addWidget(self.details,1)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.apply_button=QPushButton("Apply to Settings Draft" if preview.changes else "Keep Current Settings")
        self.apply_button.setProperty("primary",True)
        self.apply_button.setEnabled(preview.native_status!="native_incompatible")
        buttons.addButton(self.apply_button,QDialogButtonBox.AcceptRole)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        buttons.button(QDialogButtonBox.Cancel).setDefault(True)


def _training_preset_text(preview):
    status={"native_candidate":"Settings match a supported local adapter profile. Exact resources and launch still need checking.",
            "native_incompatible":"These settings do not match the current local adapter. Preview a local starting point or keep them for external training.",
            "external_manual_only":"External training plan. This does not establish that CollectiGen can run these settings."}[preview.native_status]
    unchanged=("Already at this preset's supported lower-memory settings; no settings would change."
               if preview.preset_id=="lower_memory" and not preview.changes
               and preview.native_status=="native_candidate" else "")
    return "\n\n".join(part for part in (status,unchanged,"\n".join(preview.native_reasons),
                           preview.workload.summary,"\n".join(preview.cautions)) if part)


class TrainingConfigurationDialog(FeedbackEnabledDialog):
    """Edit portable handoff choices without claiming a trainer translation."""

    feedback_workflow="Training package settings"

    def __init__(self,plan: TrainingCandidatePlan,parent=None,*,resource_registry=None,jobs=None) -> None:
        super().__init__(parent)
        self.plan=plan
        self.resource_registry=resource_registry
        self.jobs=jobs
        self.original=plan.configuration or TrainingConfiguration()
        self.setWindowTitle("Configure Training Handoff")
        self.resize(800,700)
        layout=QVBoxLayout(self)
        intro=QLabel(
            "Choose a local adapter starting point or keep an external training plan. "
            "Preview the changes and workload before applying. Exact model, runtime and "
            "memory readiness are checked separately before training."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel")
        layout.addWidget(intro)
        presets=QHBoxLayout()
        self.preset_picker=QComboBox()
        self.preset_picker.setAccessibleName("Training preset")
        for label,key in (("Local adapter starting point","native_default"),
                          ("Lower memory","lower_memory"),
                          ("External training · keep current settings","external_manual")):
            self.preset_picker.addItem(label,key)
        self.preview_preset_button=QPushButton("Preview Preset…")
        self.preview_preset_button.clicked.connect(self._preview_preset)
        presets.addWidget(self.preset_picker,1); presets.addWidget(self.preview_preset_button)
        layout.addLayout(presets)
        self.restore_recommendation_button=QPushButton("Restore Saved Recommendation…")
        self.restore_recommendation_button.setAutoDefault(False)
        self.restore_recommendation_button.clicked.connect(self._restore_recommendation)
        layout.addWidget(self.restore_recommendation_button)
        self.recommendation_status=QLabel()
        self.recommendation_status.setWordWrap(True)
        self.recommendation_status.setTextFormat(Qt.PlainText)
        self.recommendation_status.setObjectName("mutedLabel")
        layout.addWidget(self.recommendation_status)
        self.tabs=QTabWidget(); layout.addWidget(self.tabs,1)

        basics=QWidget(); basics_form=QFormLayout(basics)
        self.target_family=QLineEdit(self.original.target_model_family)
        self.target_version=QLineEdit(self.original.target_model_version)
        self.base_model_id=QLineEdit(self.original.base_model_id)
        self.base_model_id.setCursorPosition(0)
        self.base_model_id.setPlaceholderText("Registry ID or exact model hash")
        self.output_name=QLineEdit(self.original.output_name)
        self.output_kind=QComboBox()
        for label,value in (("LoRA","lora"),("Full fine-tune","full_fine_tune"),("Embedding","embedding")):
            self.output_kind.addItem(label,value)
        self.output_kind.setCurrentIndex(max(0,self.output_kind.findData(self.original.output_kind)))
        self.target_resolution=QSpinBox(); self.target_resolution.setRange(256,2048)
        self.target_resolution.setSingleStep(64); self.target_resolution.setValue(self.original.target_resolution)
        self.bucket_policy=QComboBox(); self.bucket_policy.addItem("Aspect-ratio buckets","aspect_ratio"); self.bucket_policy.addItem("Square buckets","square")
        self.bucket_policy.setCurrentIndex(max(0,self.bucket_policy.findData(self.original.bucket_policy)))
        self.crop_policy=QComboBox(); self.crop_policy.addItem("No package crop","none"); self.crop_policy.addItem("Centered crop request","center")
        self.crop_policy.setCurrentIndex(max(0,self.crop_policy.findData(self.original.crop_policy)))
        self.allow_upscale=QCheckBox("Allow a future compatible adapter to upscale")
        self.allow_upscale.setChecked(self.original.allow_upscale)
        basics_form.addRow("Target family",self.target_family)
        basics_form.addRow("Target version",self.target_version)
        base_model_row=QHBoxLayout()
        base_model_row.addWidget(self.base_model_id,1)
        self.choose_model_button=QPushButton("Choose Local Model…")
        self.choose_model_button.setAutoDefault(False)
        self.choose_model_button.setEnabled(resource_registry is not None and jobs is not None)
        self.choose_model_button.clicked.connect(self._choose_local_model)
        base_model_row.addWidget(self.choose_model_button)
        basics_form.addRow("Base model ID",base_model_row)
        basics_form.addRow("Expected output name",self.output_name)
        basics_form.addRow("Expected output kind",self.output_kind)
        basics_form.addRow("Planning resolution",self.target_resolution)
        basics_form.addRow("Bucket policy",self.bucket_policy)
        basics_form.addRow("Crop policy",self.crop_policy)
        basics_form.addRow("Upscaling",self.allow_upscale)
        self.tabs.addTab(basics,"Goal && Images")

        advanced=QWidget(); advanced_form=QFormLayout(advanced)
        self.caption_policy=QComboBox()
        self.caption_policy.addItem("Use exact profile caption","profile_only")
        self.caption_policy.addItem("Prepend trigger","trigger")
        self.caption_policy.addItem("Prepend trigger and class","trigger_and_class")
        self.caption_policy.setCurrentIndex(max(0,self.caption_policy.findData(self.original.caption_prefix_policy)))
        self.trigger_term=QLineEdit(self.original.trigger_term)
        self.class_term=QLineEdit(self.original.class_term)
        self.max_tokens=QSpinBox(); self.max_tokens.setRange(1,1_000_000); self.max_tokens.setValue(self.original.max_caption_tokens)
        self.repeats=QSpinBox(); self.repeats.setRange(1,100); self.repeats.setValue(self.original.repeats)
        self.epochs=QSpinBox(); self.epochs.setRange(1,10_000); self.epochs.setValue(self.original.epochs)
        self.batch_size=QSpinBox(); self.batch_size.setRange(1,64); self.batch_size.setValue(self.original.batch_size)
        self.accumulation=QSpinBox(); self.accumulation.setRange(1,128); self.accumulation.setValue(self.original.gradient_accumulation)
        self.learning_rate=QDoubleSpinBox(); self.learning_rate.setDecimals(8); self.learning_rate.setRange(0.00000001,0.01); self.learning_rate.setSingleStep(0.00001); self.learning_rate.setValue(self.original.learning_rate)
        self.precision=QComboBox()
        for label,value in (("BF16 request","bf16"),("FP16 request","fp16"),("Float32 request","float32")):
            self.precision.addItem(label,value)
        self.precision.setCurrentIndex(max(0,self.precision.findData(self.original.precision)))
        self.seed=QDoubleSpinBox(); self.seed.setDecimals(0)
        self.seed.setRange(0,4_294_967_295); self.seed.setValue(self.original.seed)
        advanced_form.addRow("Caption prefix",self.caption_policy)
        advanced_form.addRow("Trigger term",self.trigger_term)
        advanced_form.addRow("Class term",self.class_term)
        advanced_form.addRow("Requested token limit",self.max_tokens)
        advanced_form.addRow("Image repeats",self.repeats)
        advanced_form.addRow("Epochs",self.epochs)
        advanced_form.addRow("Batch size",self.batch_size)
        advanced_form.addRow("Gradient accumulation",self.accumulation)
        advanced_form.addRow("Learning rate",self.learning_rate)
        advanced_form.addRow("Precision",self.precision)
        advanced_form.addRow("Seed",self.seed)
        self.tabs.addTab(advanced,"Advanced")

        self.workload_text=QTextEdit(); self.workload_text.setReadOnly(True)
        self.workload_text.setAccessibleName("Training workload and adapter readiness")
        self.tabs.addTab(self.workload_text,"Workload && Readiness")

        self.override_reason=QTextEdit(); self.override_reason.setFixedHeight(70)
        self.override_reason.setPlainText(self.original.override_reason)
        self.override_reason.setPlaceholderText(
            "Required only when changing a scoped recommended default"
        )
        self.override_reason.setAccessibleName("Training setting override reason")
        layout.addWidget(QLabel("Reason for changed recommended defaults"))
        layout.addWidget(self.override_reason)
        self.issue=QLabel(); self.issue.setWordWrap(True); self.issue.setObjectName("mutedLabel")
        layout.addWidget(self.issue)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        self.save_button=buttons.button(QDialogButtonBox.Save)
        self.save_button.setText("Use These Settings"); self.save_button.setProperty("primary",True)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._setting_controls={
            "target_model_family":self.target_family,"target_model_version":self.target_version,
            "base_model_id":self.base_model_id,"output_name":self.output_name,
            "output_kind":self.output_kind,"target_resolution":self.target_resolution,
            "bucket_policy":self.bucket_policy,"crop_policy":self.crop_policy,
            "allow_upscale":self.allow_upscale,"caption_prefix_policy":self.caption_policy,
            "trigger_term":self.trigger_term,"class_term":self.class_term,
            "max_caption_tokens":self.max_tokens,"repeats":self.repeats,"epochs":self.epochs,
            "batch_size":self.batch_size,"gradient_accumulation":self.accumulation,
            "learning_rate":self.learning_rate,"precision":self.precision,"seed":self.seed,
            "override_reason":self.override_reason,
        }
        self._rendered_settings=self._control_values()
        for control in self._setting_controls.values():
            signal=getattr(control,"textChanged",None) or getattr(control,"valueChanged",None) or getattr(control,"currentIndexChanged",None) or getattr(control,"toggled",None)
            if signal is not None:
                signal.connect(self._update_state)
        self._update_state()

    def _choose_local_model(self) -> None:
        from .model_selector_dialog import ModelSelectorDialog
        from core.model_selection import protected_model_roots
        host = self.parent()
        while host is not None and getattr(host, "state", None) is None:
            host = host.parent()
        settings = getattr(getattr(host, "state", None), "settings", None)
        dialog=ModelSelectorDialog(self.resource_registry,self.jobs,self.target_family.text(),self,
            role="checkpoint", protected_roots=protected_model_roots(settings))
        if dialog.exec()==QDialog.Accepted:
            self.base_model_id.setText(dialog.selected_resource_id)
            self.base_model_id.setCursorPosition(0)
            self.target_family.setText(dialog.selected_family)

    def configuration(self) -> TrainingConfiguration:
        changes={field:value for field,value in self._control_values().items()
                 if value!=self._rendered_settings[field]}
        return replace(self.original,**changes)

    def _control_values(self) -> dict[str,object]:
        values={}
        for field,control in self._setting_controls.items():
            if isinstance(control,QLineEdit): value=control.text().strip()
            elif isinstance(control,QTextEdit): value=control.toPlainText().strip()
            elif isinstance(control,QComboBox): value=control.currentData()
            elif isinstance(control,QCheckBox): value=control.isChecked()
            else: value=control.value()
            values[field]=int(value) if field=="seed" else value
        return values

    def _restore_recommendation(self) -> None:
        current=self.configuration()
        try:
            restored=current.restore_saved_recommendation()
        except ValueError as exc:
            self.recommendation_status.setText(str(exc))
            return
        if restored==current: return
        dialog=TrainingRecommendationRestoreDialog(current,restored,self)
        result=dialog.exec()
        dialog.deleteLater()
        self.activateWindow()
        if result!=QDialog.Accepted:
            self.restore_recommendation_button.setFocus(Qt.OtherFocusReason)
            return
        if self.configuration()!=current:
            self.recommendation_status.setText("Settings changed during review. Preview the restore again.")
            return
        blockers=[QSignalBlocker(control) for control in self._setting_controls.values()]
        self.original=restored
        for field,control in self._setting_controls.items():
            value=restored.value_for(field)
            if isinstance(control,QLineEdit): control.setText(value)
            elif isinstance(control,QTextEdit): control.setPlainText(value)
            elif isinstance(control,QComboBox): control.setCurrentIndex(control.findData(value))
            elif isinstance(control,QCheckBox): control.setChecked(value)
            else: control.setValue(value)
        self._rendered_settings=self._control_values()
        del blockers
        self._update_state()
        self.save_button.setFocus(Qt.OtherFocusReason)

    def _preset_preview(self,preset_id):
        candidates=getattr(self.plan,"candidates",None)
        count=(None if candidates is None else sum(
            item.decision=="include" and item.role=="training" for item in candidates))
        return preview_training_preset(self.configuration(),preset_id=preset_id,
            training_image_count=count,goal_kind=getattr(getattr(self.plan,"goal",None),"kind","concept"))

    def _preview_preset(self):
        current=self.configuration()
        try:
            preview=self._preset_preview(self.preset_picker.currentData())
        except ValueError as exc:
            self.issue.setText(str(exc)); return
        dialog=TrainingPresetPreviewDialog(preview,self)
        accepted=dialog.exec()==QDialog.Accepted
        dialog.deleteLater()
        if not accepted: return
        if self.configuration()!=current:
            self.issue.setText("Settings changed during review. Preview the preset again."); return
        if preview.native_status=="native_incompatible": return
        blockers=[QSignalBlocker(control) for control in self._setting_controls.values()]
        self.original=preview.configuration
        for field,control in self._setting_controls.items():
            value=self.original.value_for(field)
            if isinstance(control,QLineEdit): control.setText(value)
            elif isinstance(control,QTextEdit): control.setPlainText(value)
            elif isinstance(control,QComboBox): control.setCurrentIndex(control.findData(value))
            elif isinstance(control,QCheckBox): control.setChecked(value)
            else: control.setValue(value)
        self._rendered_settings=self._control_values()
        del blockers
        self._update_state()
        self.tabs.setCurrentWidget(self.workload_text)
        self.save_button.setFocus(Qt.OtherFocusReason)

    def _update_state(self,*_args) -> None:
        current=self.configuration()
        try:
            preview=self._preset_preview("current")
            self.workload_text.setPlainText(_training_preset_text(preview))
            self.save_button.setText("Use These Settings" if preview.native_status=="native_candidate"
                                     else "Use for External Training")
        except ValueError as exc:
            self.workload_text.setPlainText(str(exc))
        try:
            restored=current.restore_saved_recommendation()
        except ValueError as exc:
            self.restore_recommendation_button.setEnabled(False)
            self.recommendation_status.setText(f"Saved recommendation unavailable: {exc}")
        else:
            changed=restored!=current
            self.restore_recommendation_button.setEnabled(changed)
            self.recommendation_status.setText(
                "Preview changes back to the saved recommendation. Fresh advice is separate."
                if changed else "Settings match the saved recommendation. Fresh advice is separate."
            )
        rendered=self._control_values()
        exact=[f"{field.replace('_',' ')} = {current.value_for(field)!r}" for field,value in rendered.items()
               if value!=current.value_for(field)]
        if exact:
            self.recommendation_status.setText(self.recommendation_status.text()+
                " Exact saved values are retained beyond these controls' display precision: "+", ".join(exact)+".")
        issues=validate_training_configuration(
            current,profile_family=self.plan.profile.target_model_family,
            profile_version=self.plan.profile.target_model_version,
        )
        blocking=[item for item in issues if item.level=="blocking"]
        self.save_button.setEnabled(not blocking)
        if blocking:
            self.issue.setText(
                f"Needs attention: {blocking[0].summary} {blocking[0].direct_fix}"
            )
            return
        remaining=[]
        if not self.configuration().base_model_id.strip():
            remaining.append("a portable base model ID")
        if not self.plan.permission_ready:
            remaining.append("a source/permission disposition")
        remaining.append("the final exact-file preflight")
        self.issue.setText(
            "Settings are structurally valid. Package readiness still requires "
            +", ".join(remaining[:-1])
            +(f", and {remaining[-1]}." if len(remaining)>1 else f"{remaining[-1]}.")
        )


class TrainingCandidateOverrideDialog(FeedbackEnabledDialog):
    """Record one explainable candidate decision without hiding its recommendation."""

    feedback_workflow="Training dataset candidate override"

    def __init__(self,candidate,parent=None) -> None:
        super().__init__(parent)
        self.candidate=candidate
        self.setWindowTitle("Edit Candidate Override")
        self.resize(650,420)
        layout=QVBoxLayout(self)
        title=QLabel(candidate.image.name); title.setObjectName("sectionTitle")
        title.setWordWrap(True); layout.addWidget(title)
        recommendation=TrainingCandidateSelectionService._recommended_candidate(
            candidate
        )
        detail=QLabel(
            f"Current recommendation: {recommendation.decision.title()} • "
            f"{recommendation.role.title()}\n{recommendation.decision_reason}"
        )
        detail.setWordWrap(True); detail.setObjectName("mutedLabel")
        layout.addWidget(detail)
        form=QFormLayout()
        self.decision=QComboBox()
        self.decision.setAccessibleName("Candidate override decision")
        self.decision.setAccessibleDescription(
            "Explicitly include or exclude this image from the saved plan."
        )
        self.decision.addItem("Include","include")
        self.decision.addItem("Exclude","exclude")
        self.decision.setCurrentIndex(
            max(0,self.decision.findData(candidate.decision))
        )
        self.role=QComboBox()
        self.role.setAccessibleName("Candidate override split role")
        self.role.setAccessibleDescription(
            "Assign this image to training, validation, or holdout."
        )
        self.role.addItem("Training","training")
        self.role.addItem("Validation","validation")
        self.role.addItem("Holdout","holdout")
        self.role.setCurrentIndex(max(0,self.role.findData(candidate.role)))
        self.reason=QTextEdit()
        self.reason.setPlainText(
            candidate.decision_reason if candidate.user_override else ""
        )
        self.reason.setFixedHeight(90)
        self.reason.setPlaceholderText(
            "Why should this differ from the current recommendation?"
        )
        self.reason.setAccessibleName("Candidate override reason")
        form.addRow("Decision",self.decision)
        form.addRow("Role",self.role)
        form.addRow("Required reason",self.reason)
        layout.addLayout(form)
        note=QLabel(
            "This changes only the draft recommendation and role. It does not alter "
            "the image, Image Knowledge, captions, Metadata, or training files."
        )
        note.setWordWrap(True); note.setObjectName("mutedLabel")
        layout.addWidget(note); layout.addStretch(1)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        self.save_button=buttons.button(QDialogButtonBox.Save)
        self.save_button.setText("Record Override")
        self.save_button.setProperty("primary",True)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.reason.textChanged.connect(self._update_state)
        self._update_state()

    def _update_state(self) -> None:
        self.save_button.setEnabled(bool(self.reason.toPlainText().strip()))

    def override(self) -> CandidateOverride:
        return CandidateOverride(
            str(self.decision.currentData()),self.reason.toPlainText().strip(),
            str(self.role.currentData()),
        )


class TrainingPermissionDispositionDialog(FeedbackEnabledDialog):
    """Record a user decision without presenting it as legal clearance."""

    feedback_workflow="Training source and permission review"

    def __init__(self,plan: TrainingCandidatePlan,parent=None) -> None:
        super().__init__(parent)
        self.plan=plan
        self.setWindowTitle("Record Source & Permission Disposition")
        self.resize(720,500)
        layout=QVBoxLayout(self)
        intro=QLabel(
            "CollectiGen carries the reported source evidence and your decision into "
            "this exact plan. It does not determine ownership, consent, privacy rights, "
            "license compatibility, or legal permission."
        )
        intro.setWordWrap(True); intro.setObjectName("sectionTitle")
        layout.addWidget(intro)
        included=plan.count("include")
        self.evidence_summary=QLabel(
            f"Exact included scope: {included:,} image"
            f"{'s' if included!=1 else ''} • "
            f"{plan.permission_unknown_count:,} unknown fields across creator, source, "
            "license, consent/use restrictions, and real-person/privacy evidence."
        )
        self.evidence_summary.setWordWrap(True)
        self.evidence_summary.setObjectName("mutedLabel")
        self.evidence_summary.setTextInteractionFlags(
            Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard
        )
        layout.addWidget(self.evidence_summary)
        form=QFormLayout()
        self.status=QComboBox()
        self.status.setAccessibleName("Source and permission disposition")
        self.status.setAccessibleDescription(
            "Keep packaging on hold or proceed for this exact planned use."
        )
        self.status.addItem("Needs review — keep package on hold","needs_review")
        self.status.addItem("Proceed for this exact planned use","proceed")
        current=plan.permission_disposition.status
        self.status.setCurrentIndex(max(0,self.status.findData(current)))
        self.reason=QTextEdit(); self.reason.setFixedHeight(100)
        self.reason.setAccessibleName("Source and permission disposition reason")
        self.reason.setPlaceholderText(
            "Record the evidence you reviewed and why this exact scope should proceed or remain on hold."
        )
        self.reason.setPlainText(plan.permission_disposition.reason)
        form.addRow("Disposition",self.status)
        form.addRow("Required reason",self.reason)
        layout.addLayout(form)
        self.unknown_acknowledgement=QCheckBox(
            "I understand that unknown evidence remains unknown and that this decision "
            "does not mean CollectiGen verified legal permission."
        )
        self.unknown_acknowledgement.setAccessibleName(
            "Acknowledge unknown source and permission evidence"
        )
        self.unknown_acknowledgement.setChecked(
            plan.permission_disposition.unknown_evidence_acknowledged
        )
        self.unknown_acknowledgement.setVisible(bool(plan.permission_unknown_count))
        layout.addWidget(self.unknown_acknowledgement)
        note=QLabel(
            "To remove one image from the package decision, close this dialog and use "
            "Edit Override on that candidate. No image, Image Knowledge, Metadata, "
            "content rating, caption, or training file is changed here."
        )
        note.setWordWrap(True); note.setObjectName("mutedLabel")
        layout.addWidget(note); layout.addStretch(1)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        self.save_button=buttons.button(QDialogButtonBox.Save)
        self.save_button.setText("Record Disposition")
        self.save_button.setProperty("primary",True)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.status.currentIndexChanged.connect(self._update_state)
        self.reason.textChanged.connect(self._update_state)
        self.unknown_acknowledgement.toggled.connect(self._update_state)
        self._update_state()

    def _update_state(self) -> None:
        can_proceed=(
            self.status.currentData()!="proceed"
            or not self.plan.permission_unknown_count
            or self.unknown_acknowledgement.isChecked()
        )
        self.save_button.setEnabled(
            bool(self.reason.toPlainText().strip()) and can_proceed
        )

    def disposition(self) -> TrainingPermissionDisposition:
        return TrainingPermissionDisposition(
            str(self.status.currentData()),self.reason.toPlainText().strip(),
            bool(
                self.plan.permission_unknown_count
                and self.unknown_acknowledgement.isChecked()
            ),
        )


class TrainingPlanDraftPickerDialog(FeedbackEnabledDialog):
    """Choose one exact persisted plan without broadening its saved scope."""

    feedback_workflow="Training dataset draft resume"

    def __init__(self,drafts,parent=None) -> None:
        super().__init__(parent)
        self.drafts=tuple(drafts)
        self.setWindowTitle("Resume Training Dataset Draft")
        self.resize(680,330)
        layout=QVBoxLayout(self)
        intro=QLabel(
            "Choose a saved source-neutral draft. CollectiGen will resolve only its "
            "recorded library identities and re-inspect the current source bytes."
        )
        intro.setWordWrap(True); intro.setObjectName("sectionTitle")
        layout.addWidget(intro)
        self.choice=QComboBox(); self.choice.setAccessibleName("Saved training draft")
        for draft in self.drafts:
            self.choice.addItem(
                f"{draft.name} — {draft.candidate_count:,} image(s)",draft.draft_id,
            )
        layout.addWidget(self.choice)
        self.detail=QLabel(); self.detail.setWordWrap(True)
        self.detail.setObjectName("mutedLabel"); layout.addWidget(self.detail)
        layout.addStretch(1)
        buttons=QDialogButtonBox(QDialogButtonBox.Open|QDialogButtonBox.Cancel)
        self.open_button=buttons.button(QDialogButtonBox.Open)
        self.open_button.setText("Rebuild Draft from Current Sources")
        self.open_button.setProperty("primary",True)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.choice.currentIndexChanged.connect(self._update_detail)
        self._update_detail()

    def selected_draft(self):
        draft_id=str(self.choice.currentData() or "")
        return next(
            (draft for draft in self.drafts if draft.draft_id==draft_id),None
        )

    def _update_detail(self) -> None:
        draft=self.selected_draft()
        self.open_button.setEnabled(draft is not None)
        if draft is None:
            self.detail.setText("No saved draft is available.")
            return
        profile=draft.profile
        self.detail.setText(
            f"Goal: {draft.goal.text}\nProfile: {profile.name} v{profile.version} • "
            f"Updated {draft.updated_at}. Any changed source or knowledge evidence "
            "will be visible in the rebuilt preview before the draft is saved again."
        )


class TrainingCandidatePlanDialog(FeedbackEnabledDialog):
    """Present layered candidate evidence and the exact neutral preview."""

    feedback_workflow = "Training dataset planning"
    CANDIDATE_PAGE_SIZE = 100
    MANIFEST_CANDIDATE_LIMIT = 500
    fix_requested = Signal(object)
    duplicate_review_requested = Signal()
    cancel_review_requested = Signal()
    package_requested = Signal()

    def __init__(
        self,plan: TrainingCandidatePlan,parent=None,*,draft_store=None,draft=None,
        task_pool=None,can_fix: bool=False,resource_registry=None,jobs=None,
    ) -> None:
        super().__init__(parent)
        self.plan = plan
        self.action=""
        self.draft_store=draft_store
        self.draft=draft
        self.task_pool=task_pool
        self.resource_registry=resource_registry
        self.jobs=jobs
        self._draft_save_task=None
        self._review_busy=False
        self._can_fix=bool(can_fix)
        self._refresh_required=False
        self._discard_refresh_confirmed=False
        self._candidate_by_id={}
        self._filtered_candidates=()
        self._filtered_positions={}
        self._candidate_page=0
        self._preview_path=None
        self._overrides={
            candidate.stable_id:CandidateOverride(
                candidate.decision,candidate.decision_reason,candidate.role,
            )
            for candidate in plan.candidates if candidate.user_override
        }
        self.setWindowTitle("Review Training Data")
        self.resize(1080, 740)
        layout = QVBoxLayout(self)
        self.workflow_context=WorkflowContextSummary(self)
        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setObjectName("sectionTitle")
        layout.addWidget(self.summary)
        self.scope_summary=QLabel()
        self.scope_summary.setWordWrap(True)
        layout.addWidget(self.scope_summary)
        next_row=QHBoxLayout()
        self.next_step=QLabel()
        self.next_step.setWordWrap(True)
        self.next_step.setAccessibleName("Next training plan action")
        self.next_action=QPushButton()
        self.next_action.clicked.connect(self._activate_next_action)
        self.save_draft_button=QPushButton("Save Draft…")
        self.save_draft_button.setVisible(draft_store is not None)
        self.save_draft_button.clicked.connect(self._save_draft)
        next_row.addWidget(self.next_step,1); next_row.addWidget(self.next_action)
        layout.addLayout(next_row)
        self.draft_status=QLabel()
        self.draft_status.setWordWrap(True); self.draft_status.setObjectName("mutedLabel")
        layout.addWidget(self.draft_status)
        layers = QLabel(
            "Review each evidence layer independently. "
            "No combined quality or confidence score is used."
        )
        layers.setWordWrap(True)
        layers.setObjectName("mutedLabel")
        self.details_button=QPushButton("More Evidence and Exact Details…")
        self.details_button.setCheckable(True)
        self.details_button.setAccessibleDescription(
            "Show Dataset Evidence, Similar Images and the exact Manifest Preview."
        )
        details_row=QHBoxLayout()
        details_row.addWidget(self.details_button)
        details_row.addStretch(1)
        layout.addLayout(details_row)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._candidate_tab(), "Candidates")
        self.tabs.addTab(self._dataset_evidence_tab(),"Dataset Evidence")
        self.tabs.addTab(self._permission_tab(),"Source && Permission")
        self.tabs.addTab(self._training_setup_tab(),"Training Setup")
        self.tabs.addTab(self._manifest_tab(),"Manifest Preview")
        # Full provenance remains in the existing exact-details task rather
        # than repeating the default header's scope, readiness and next step.
        exact_layout=self.tabs.widget(4).layout()
        exact_layout.insertWidget(0,self.workflow_context)
        exact_layout.insertWidget(1,layers)
        self.draft_detail=QLabel()
        self.draft_detail.setWordWrap(True)
        self.draft_detail.setTextInteractionFlags(Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard)
        exact_layout.insertWidget(2,self.draft_detail)
        from .training_duplicate_evidence_view import TrainingDuplicateEvidenceView
        self.similarity_view=TrainingDuplicateEvidenceView(self)
        self.similarity_view.inspect_requested.connect(self._focus_candidate)
        self.similarity_view.review_requested.connect(self.duplicate_review_requested)
        self.tabs.addTab(self.similarity_view,"Similar Images")
        from .training_caption_review_view import TrainingCaptionReviewView
        self.caption_review=TrainingCaptionReviewView(self)
        self.caption_review.inspect_requested.connect(self._focus_candidate)
        self.caption_review.edit_requested.connect(self._edit_caption_candidate)
        self.tabs.addTab(self.caption_review,"Caption Review")
        self.tabs.currentChanged.connect(self._refresh_caption_review)
        self.details_button.toggled.connect(self._toggle_exact_details)
        self._toggle_exact_details(False)
        layout.addWidget(self.tabs, 1)
        self.limitations = QLabel()
        self.limitations.setWordWrap(True)
        self.limitations.setObjectName("mutedLabel")
        self.limitations.setTextInteractionFlags(
            Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard
        )
        self.tabs.widget(4).layout().addWidget(self.limitations)
        refresh_row=QHBoxLayout()
        self.refresh_status=QLabel()
        self.refresh_status.setWordWrap(True)
        self.refresh_status.setAccessibleName("Training evidence refresh status")
        self.cancel_refresh_button=QPushButton("Cancel Safely")
        self.cancel_refresh_button.clicked.connect(self.cancel_review_requested)
        self.cancel_refresh_button.hide()
        refresh_row.addWidget(self.refresh_status,1)
        refresh_row.addWidget(self.cancel_refresh_button)
        layout.addLayout(refresh_row)
        from .components import bind_status_footer
        bind_status_footer(self,self.refresh_status,self.cancel_refresh_button,title="Training evidence")
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.addButton(self.save_draft_button,QDialogButtonBox.ActionRole)
        self.save_draft_button.setVisible(draft_store is not None)
        self.permission_disposition_button=QPushButton("Record Permission…")
        self.configure_button=QPushButton("Configure Handoff…")
        self.package_button=QPushButton("Continue to Package…")
        self.tabs.widget(2).layout().addWidget(self.permission_disposition_button)
        self.tabs.widget(3).layout().addWidget(self.configure_button)
        buttons.addButton(self.package_button,QDialogButtonBox.AcceptRole)
        self.package_button.setProperty("primary",True)
        self.close_button=buttons.button(QDialogButtonBox.Close)
        self.edit_override_button.clicked.connect(self._edit_override)
        self.use_recommendation_button.clicked.connect(self._use_recommendation)
        self.permission_disposition_button.clicked.connect(
            self._record_permission_disposition
        )
        self.configure_button.clicked.connect(self._configure_handoff)
        self.package_button.clicked.connect(self._continue_to_package)
        buttons.rejected.connect(self._close_requested)
        consequence=QLabel("Preview first; building the package and starting training require separate confirmation.")
        consequence.setWordWrap(True)
        consequence.setObjectName("mutedLabel")
        layout.addWidget(consequence)
        layout.addWidget(buttons)
        self._refresh_plan_views()

    def _toggle_exact_details(self, shown: bool) -> None:
        if not shown and self.tabs.currentIndex() in (1, 4, 5):
            self.tabs.setCurrentIndex(0)
        for index in (1, 4, 5):
            self.tabs.setTabVisible(index, shown)
        self.details_button.setText(
            "Hide Extra Evidence" if shown else "More Evidence and Exact Details…"
        )

    def _show_evidence_tab(self, index: int) -> None:
        if not self.tabs.isTabVisible(index):
            self.details_button.setChecked(True)
        self.tabs.setCurrentIndex(index)

    @staticmethod
    def _bounded_manifest_summary(plan: TrainingCandidatePlan) -> dict[str, object]:
        return {
            "schema_version":plan.schema_version,
            "kind":"collectigen_training_candidate_plan_summary",
            "preview_truncated":True,
            "goal":plan.goal.to_dict(),
            "selection_scope":{
                "source_label":plan.source_label,
                "scope_fingerprint":plan.scope_fingerprint,
                "candidate_count":len(plan.frozen_scope),
                "processed_count":len(plan.candidates),
            },
            "profile_provenance":plan.profile.provenance(),
            "counts":{
                "included":plan.count("include"),
                "excluded":plan.count("exclude"),
                "blocking":sum(bool(candidate.blocking) for candidate in plan.candidates),
                "training":sum(
                    candidate.decision=="include" and candidate.role=="training"
                    for candidate in plan.candidates
                ),
                "validation":sum(
                    candidate.decision=="include" and candidate.role=="validation"
                    for candidate in plan.candidates
                ),
                "holdout":sum(
                    candidate.decision=="include" and candidate.role=="holdout"
                    for candidate in plan.candidates
                ),
                "not_processed":len(plan.not_processed),
            },
            "issues":[issue.to_dict() for issue in plan.issues],
            "permission_review":plan.permission_disposition.to_dict(),
            "permission_summary":{
                "included_images":plan.count("include"),
                "unknown_fields":plan.permission_unknown_count,
                "ready_for_future_package_gate":plan.permission_ready,
            },
            "training_configuration":(
                plan.configuration.to_dict() if plan.configuration is not None else None
            ),
            "training_configuration_issues":[
                issue.to_dict() for issue in plan.configuration_issues
            ],
            "package_ready":plan.package_ready,
            "limitations":list(plan.limitations),
        }

    def _candidate_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        filters=QHBoxLayout()
        self.candidate_filter=QComboBox()
        for label,key in (("All candidates","all"),("Blocking issues","blocking"),
                          ("Included","include"),("Excluded","exclude"),
                          ("User overrides","overrides")):
            self.candidate_filter.addItem(label,key)
        self.candidate_filter.setAccessibleName("Filter training candidates")
        self.candidate_search=QLineEdit()
        self.candidate_search.setPlaceholderText("Find an image or caption in this frozen selection")
        self.candidate_search.setAccessibleName("Search training candidates")
        self.candidate_search.installEventFilter(self)
        self._candidate_search_timer=QTimer(self)
        self._candidate_search_timer.setSingleShot(True)
        self._candidate_search_timer.setInterval(180)
        self._candidate_search_timer.timeout.connect(self._filter_candidates)
        self.candidate_search.textChanged.connect(lambda *_:self._candidate_search_timer.start())
        self.candidate_search.returnPressed.connect(self._filter_candidates)
        self.candidate_filter.currentIndexChanged.connect(self._filter_candidates)
        filters.addWidget(self.candidate_filter); filters.addWidget(self.candidate_search,1)
        layout.addLayout(filters)
        splitter=QSplitter(Qt.Horizontal)
        self.tree = QTreeWidget()
        self.tree.setAccessibleName("Training dataset candidate evidence")
        self.tree.setHeaderLabels(
            ("Image / Layer", "State", "Role", "Why / Evidence", "Exact caption / Fix")
        )
        self.tree.setAlternatingRowColors(True)
        self.tree.setRootIsDecorated(True)
        header=self.tree.header()
        header.setSectionResizeMode(0,QHeaderView.Interactive)
        self.tree.setColumnWidth(0,200)
        header.setSectionResizeMode(1,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3,QHeaderView.Stretch)
        header.setSectionResizeMode(4,QHeaderView.Interactive)
        self.tree.setColumnWidth(4,240)
        self.tree.setUniformRowHeights(True)
        self.tree.currentItemChanged.connect(self._candidate_selection_changed)
        self.tree.itemActivated.connect(lambda *_:self._activate_candidate_fix())
        splitter.addWidget(self.tree)
        self.candidate_preview=EmbeddedGallery(page_size=1,card_width=180,show_open_dataset=False)
        self.candidate_preview.setAccessibleName("Selected training candidate protected preview")
        self.candidate_preview.setMinimumWidth(225)
        self.candidate_preview.setMaximumWidth(260)
        self.candidate_preview.set_unavailable("Choose a candidate to see its image.","Selected image")
        splitter.addWidget(self.candidate_preview)
        splitter.setStretchFactor(0,1)
        layout.addWidget(splitter,1)
        pager=QHBoxLayout()
        self.candidate_page_status=QLabel()
        self.previous_candidates=QPushButton("Previous")
        self.previous_candidates.clicked.connect(lambda:self._change_candidate_page(-1))
        self.candidate_page_choice=QSpinBox()
        self.candidate_page_choice.setPrefix("Page ")
        self.candidate_page_choice.setAccessibleName("Training candidate page")
        self.candidate_page_choice.lineEdit().installEventFilter(self)
        self.candidate_page_choice.valueChanged.connect(lambda value:self._show_candidate_page(value-1))
        self.show_more_candidates=QPushButton("Next")
        self.show_more_candidates.clicked.connect(lambda:self._change_candidate_page(1))
        pager.addWidget(self.candidate_page_status,1)
        pager.addWidget(self.previous_candidates); pager.addWidget(self.candidate_page_choice)
        pager.addWidget(self.show_more_candidates)
        layout.addLayout(pager)
        self.selected_finding_summary=QTextEdit()
        self.selected_finding_summary.setAccessibleName("Selected training finding")
        self.selected_finding_summary.setReadOnly(True)
        self.selected_finding_summary.setFixedHeight(32)
        self.selected_finding_summary.document().documentLayout().documentSizeChanged.connect(
            lambda size:self.selected_finding_summary.setFixedHeight(min(88,max(32,int(size.height())+12)))
        )
        self.selected_finding_summary.hide()
        layout.addWidget(self.selected_finding_summary)
        actions=QHBoxLayout()
        self.edit_override_button=QPushButton("Edit Override…")
        self.use_recommendation_button=QPushButton("Use Recommendation")
        self.fix_candidate_button=QPushButton("Edit Image Knowledge…")
        self.fix_candidate_button.clicked.connect(self._activate_candidate_fix)
        self.refresh_candidate_button=QPushButton("Refresh Image")
        self.refresh_candidate_button.clicked.connect(self._request_candidate_refresh)
        actions.addWidget(self.fix_candidate_button)
        actions.addWidget(self.refresh_candidate_button)
        actions.addWidget(self.edit_override_button); actions.addWidget(self.use_recommendation_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        return page

    def _filter_candidates(self,*_args,selected_id: str="") -> None:
        self._candidate_search_timer.stop()
        query=self.candidate_search.text().strip().casefold()
        state=self.candidate_filter.currentData()
        self._filtered_candidates=tuple(candidate for candidate in self.plan.candidates if (
            (state=="all" or (state=="blocking" and candidate.blocking)
             or (state=="overrides" and candidate.user_override)
             or candidate.decision==state)
            and (not query or query in candidate.image.name.casefold()
                 or query in candidate.caption.casefold())
        ))
        self._filtered_positions={candidate.stable_id:index for index,candidate in enumerate(self._filtered_candidates)}
        index=self._filtered_positions.get(selected_id,0)
        self._show_candidate_page(index//self.CANDIDATE_PAGE_SIZE,selected_id=selected_id)

    def eventFilter(self,watched,event) -> bool:
        if event.type()==QEvent.KeyPress and event.key() in {Qt.Key_Return,Qt.Key_Enter}:
            if watched is self.candidate_search:
                self._filter_candidates()
                event.accept()
                return True
            if watched is self.candidate_page_choice.lineEdit():
                self.candidate_page_choice.interpretText()
                event.accept()
                return True
        return super().eventFilter(watched,event)

    def _change_candidate_page(self,direction: int) -> None:
        self._show_candidate_page(self._candidate_page+direction)

    def _show_candidate_page(self,page: int,*,selected_id: str="") -> None:
        total=len(self._filtered_candidates)
        pages=max(1,(total+self.CANDIDATE_PAGE_SIZE-1)//self.CANDIDATE_PAGE_SIZE)
        self._candidate_page=max(0,min(page,pages-1))
        start=self._candidate_page*self.CANDIDATE_PAGE_SIZE
        end=min(total,start+self.CANDIDATE_PAGE_SIZE)
        self.tree.blockSignals(True); self.tree.setUpdatesEnabled(False)
        self.tree.clear()
        selected=None
        for candidate in self._filtered_candidates[start:end]:
            item=self._candidate_item(candidate)
            item.setData(0,Qt.UserRole,candidate.stable_id)
            self.tree.addTopLevelItem(item)
            if candidate.stable_id==selected_id:selected=item
        if total:self.tree.setCurrentItem(selected or self.tree.topLevelItem(0))
        self.tree.setUpdatesEnabled(True); self.tree.blockSignals(False)
        self.candidate_page_choice.blockSignals(True)
        self.candidate_page_choice.setRange(1,pages)
        self.candidate_page_choice.setValue(self._candidate_page+1)
        self.candidate_page_choice.blockSignals(False)
        self.previous_candidates.setEnabled(self._candidate_page>0)
        self.show_more_candidates.setEnabled(end<total)
        self.candidate_page_status.setText(
            f"{start+1:,}–{end:,} of {total:,} matching • {len(self.plan.candidates):,} in frozen selection"
            if total else f"No matches • {len(self.plan.candidates):,} in frozen selection"
        )
        self._candidate_selection_changed()

    def _selected_candidate(self):
        item=self.tree.currentItem()
        while item is not None and item.parent() is not None:
            item=item.parent()
        stable_id=str(item.data(0,Qt.UserRole) or "") if item is not None else ""
        return self._candidate_by_id.get(stable_id)

    def _candidate_selection_changed(self,*_args) -> None:
        candidate=self._selected_candidate()
        item=self.tree.currentItem()
        summary=item.data(0,Qt.UserRole+2) if item is not None else None
        self.selected_finding_summary.setPlainText(str(summary or ""))
        self.selected_finding_summary.setVisible(bool(candidate and summary))
        self._refresh_caption_review()
        self.edit_override_button.setEnabled(candidate is not None)
        self.use_recommendation_button.setEnabled(
            bool(candidate and candidate.user_override)
        )
        action=self._selected_fix_action()
        labels={
            CandidateFixAction.KNOWLEDGE:"Edit Image Knowledge…",
            CandidateFixAction.DESCRIPTIONS:"Edit Descriptions…",
            CandidateFixAction.CHARACTERS:"Edit Characters…",
            CandidateFixAction.METADATA:"Inspect Metadata…",
            CandidateFixAction.PERMISSION:"Review Permission",
            CandidateFixAction.SETUP:"Review Training Setup",
            CandidateFixAction.PROFILE:"Review Profile Limit",
            CandidateFixAction.REFRESH:"Refresh Image",
        }
        self.fix_candidate_button.setText(labels.get(action,"Choose a Finding"))
        navigation=action in {CandidateFixAction.PERMISSION,CandidateFixAction.SETUP,CandidateFixAction.PROFILE}
        self.fix_candidate_button.setEnabled(bool(
            candidate and action is not None and (self._can_fix or navigation) and not self._review_busy
        ))
        self.fix_candidate_button.setToolTip(
            "Open the matching review for this image, then return to the same training plan. "
            "Image Knowledge saves keep source pixels, embedded Metadata and Training TXT unchanged."
        )
        self.refresh_candidate_button.setEnabled(bool(
            candidate and self._can_fix and not self._review_busy
        ))
        path=candidate.image if candidate is not None else None
        if path!=self._preview_path:
            self._preview_path=path
            if path is None:
                self.candidate_preview.set_unavailable("No candidate matches this filter.","Selected image")
            else:
                def configure(_path,card):
                    card.set_compact_mode(True)
                    card.allow_data_edit=False
                    card.set_note("Read-only plan preview")
                self.candidate_preview.set_paths((path,),context="Selected image",configure=configure)

    def _refresh_caption_review(self,*_args) -> None:
        view=getattr(self,"caption_review",None)
        if view is None:
            return
        if self._refresh_required or self._review_busy or self._draft_save_task is not None:
            view.clear("Caption review is on hold while the plan's evidence is refreshed or saved.")
        elif self.tabs.currentWidget() is view:
            view.set_candidate(self.plan,self._selected_candidate(),can_edit=self._can_fix)
        else:
            # Discard old image text immediately; defer new formatting until opened.
            view.clear("Choose Caption Review to inspect the selected image.")

    def _edit_caption_candidate(self,stable_id: str) -> None:
        candidate=self._selected_candidate()
        if (candidate is None or candidate.stable_id!=stable_id or not self._can_fix
                or self._review_busy or self._refresh_required or self._draft_save_task is not None):
            return
        self.fix_requested.emit(CandidateFixRequest(stable_id,CandidateFixAction.KNOWLEDGE))

    def _selected_fix_action(self) -> CandidateFixAction | None:
        item=self.tree.currentItem()
        value=item.data(0,Qt.UserRole+1) if item is not None else None
        if value is None and item is not None and item.parent() is not None and item.childCount():
            return None
        return value if isinstance(value,CandidateFixAction) else CandidateFixAction.KNOWLEDGE

    def _activate_candidate_fix(self) -> None:
        candidate=self._selected_candidate()
        if candidate is None or self._review_busy or self._draft_save_task is not None:
            return
        action=self._selected_fix_action()
        if action is None:
            item=self.tree.currentItem()
            item.setExpanded(True)
            self.tree.setCurrentItem(item.child(0))
            return
        if action in {CandidateFixAction.PERMISSION,CandidateFixAction.SETUP,CandidateFixAction.PROFILE}:
            self._show_evidence_tab({CandidateFixAction.PERMISSION:2,CandidateFixAction.SETUP:3,CandidateFixAction.PROFILE:4}[action])
        elif self._can_fix:
            self.fix_requested.emit(CandidateFixRequest(candidate.stable_id,action))

    def _request_candidate_refresh(self) -> None:
        candidate=self._selected_candidate()
        if (candidate is not None or self._refresh_required) and self._can_fix and not self._review_busy and self._draft_save_task is None:
            self.fix_requested.emit(CandidateFixRequest(candidate.stable_id if candidate else "",CandidateFixAction.REFRESH))

    def set_review_busy(self,busy: bool,message: str="") -> None:
        self._review_busy=busy
        self._set_draft_save_busy(busy)
        self.cancel_refresh_button.setVisible(busy)
        self.cancel_refresh_button.setEnabled(busy)
        if message:self.refresh_status.setText(message)

    def require_evidence_refresh(self,message: str) -> None:
        self._refresh_required=True
        self._refresh_caption_review()
        self._discard_refresh_confirmed=False
        self.refresh_status.setText(message)
        self.package_button.setEnabled(False)
        self.save_draft_button.setEnabled(False)
        self.similarity_view.review.setEnabled(False)
        self._refresh_next_action()

    def adopt_refreshed_plan(self,plan: TrainingCandidatePlan,stable_id: str) -> None:
        self.plan=plan
        self._refresh_required=False
        self._preview_path=None
        self._refresh_plan_views(stable_id)
        # A corrected caption may no longer match the previous issue/search filter.
        # Keep the return destination exact and explain any visible filter reset.
        if stable_id not in self._filtered_positions:
            self._focus_candidate(stable_id)
            self.refresh_status.setText(
                "Image evidence refreshed. The view filter was cleared to keep the corrected image visible."
            )
        else:
            self.refresh_status.setText("Image evidence refreshed. Review the updated findings before continuing.")

    def _edit_override(self) -> None:
        candidate=self._selected_candidate()
        if candidate is None:
            return
        dialog=TrainingCandidateOverrideDialog(candidate,self)
        if dialog.exec()!=QDialog.Accepted:
            return
        self._overrides[candidate.stable_id]=dialog.override()
        self._apply_overrides(candidate.stable_id)

    def _use_recommendation(self) -> None:
        candidate=self._selected_candidate()
        if candidate is None or not candidate.user_override:
            return
        self._overrides.pop(candidate.stable_id,None)
        self._apply_overrides(candidate.stable_id)

    def _apply_overrides(self,selected_id: str) -> None:
        self.plan=TrainingCandidateSelectionService.apply_overrides(
            self.plan,self._overrides,
        )
        self._refresh_plan_views(selected_id)

    def _record_permission_disposition(self) -> None:
        dialog=TrainingPermissionDispositionDialog(self.plan,self)
        if dialog.exec()!=QDialog.Accepted:
            return
        self.plan=TrainingCandidateSelectionService.apply_permission_disposition(
            self.plan,dialog.disposition(),
        )
        self._refresh_plan_views()

    def _configure_handoff(self) -> None:
        dialog=TrainingConfigurationDialog(self.plan,self,resource_registry=self.resource_registry,jobs=self.jobs)
        if dialog.exec()!=QDialog.Accepted:
            return
        self.plan=TrainingCandidateSelectionService.apply_configuration(
            self.plan,dialog.configuration(),
        )
        self._refresh_plan_views()

    def _refresh_plan_views(self,selected_id: str="") -> None:
        if not selected_id:
            selected=self._selected_candidate()
            selected_id=selected.stable_id if selected is not None else ""
        self._candidate_by_id={candidate.stable_id:candidate for candidate in self.plan.candidates}
        full_manifest=len(self.plan.candidates)<=self.MANIFEST_CANDIDATE_LIMIT
        manifest=(
            self.plan.to_manifest_dict()
            if full_manifest else self._bounded_manifest_summary(self.plan)
        )
        counts=manifest["counts"]
        self.summary.setText(
            "Ready to preview package" if self.plan.package_ready else "Review your training dataset"
        )
        self.scope_summary.setText(
            f"{len(self.plan.candidates):,} inspected • {counts['included']:,} included • "
            f"{counts['excluded']:,} excluded • {counts['blocking']:,} blocked"
        )
        self.scope_summary.setToolTip(
            f"{counts['training']:,} training • {counts['validation']:,} validation • "
            f"{counts['holdout']:,} holdout. Scope: {self.plan.source_label}"
        )
        self._refresh_next_action()
        self.limitations.setText("Current limits: "+" ".join(self.plan.limitations))
        self.manifest_note.setText(
            "Exact source-neutral preview. This JSON is saved only when you choose Save Draft."
            if full_manifest else
            "This selection is too large for a responsive full JSON view. The summary "
            "retains the frozen selection and profile fingerprints plus exact counts; "
            "the complete source-neutral plan is still saved in a draft."
        )
        self.manifest_preview.setPlainText(
            json.dumps(manifest,indent=2,ensure_ascii=False)
        )
        self._refresh_dataset_evidence(counts)
        self.similarity_view.set_plan(self.plan)
        self.similarity_view.review.setEnabled(self._can_fix and len(self.plan.candidates)>1 and not self._refresh_required)
        self._refresh_permission_evidence()
        self._refresh_training_setup()
        self._filter_candidates(selected_id=selected_id)
        self.save_draft_button.setEnabled(
            self.draft_store is not None and not self.plan.cancelled and not self._refresh_required
        )
        self.package_button.setEnabled(self.plan.package_ready and not self._refresh_required)
        if self.draft is not None:
            self.draft_detail.clear()
            if not full_manifest:
                self.draft_status.setText(
                    f"Saved draft “{self.draft.name}” remains available. Review the current "
                    "candidate evidence before saving an updated snapshot to this same draft."
                )
                return
            previous=self.draft.plan_manifest.get("plan_fingerprint")
            current=self.plan.to_manifest_dict().get("plan_fingerprint")
            if previous!=current:
                changes=self.draft.comparison_changes(self.plan)
                detail=" ".join(changes[:3])
                if len(changes)>3:
                    detail+=f" {len(changes)-3:,} more difference(s) are in the preview."
                self.draft_detail.setText(
                    "Rebuilt from a saved draft. Current source or Image Knowledge "
                    "evidence differs from its saved snapshot; review the exact changes "
                    f"before saving an updated draft. {detail}"
                )
                self.draft_status.setText("Current evidence differs from the saved draft. Review Manifest Preview before saving an updated snapshot.")
            elif not self.draft_status.text():
                self.draft_status.setText(
                    f"Rebuilt from saved draft “{self.draft.name}”; current evidence "
                    "matches its saved plan fingerprint."
                )

    def _refresh_next_action(self) -> None:
        blocker=next((candidate for candidate in self.plan.candidates if candidate.blocking),None)
        if self._refresh_required:
            text="Image evidence changed. Refresh it before continuing to a package."
            label="Refresh Evidence"; action=("refresh",0)
        elif self.plan.cancelled:
            text="Inspection was cancelled. Rebuild the frozen plan to inspect the remaining images."
            label="Review Inspected Images"; action=("tab",0)
        elif blocker is not None:
            text=f"Next: review {blocker.image.name}. {blocker.blocking[0].summary}"
            label="Review Blocker"; action=("candidate",blocker.stable_id)
        elif not any(candidate.decision=="include" and candidate.role=="training" for candidate in self.plan.candidates):
            text="Next: choose at least one included training image. Review the recommended roles and your overrides."
            label="Review Candidates"; action=("tab",0)
        elif not self.plan.permission_ready:
            text="Next: review source and permission evidence for this included selection and record your decision."
            label="Review Permission"; action=("tab",2)
        elif not self.plan.package_ready:
            text="Next: review the training goal, model, settings and any remaining blocking evidence."
            label="Review Setup"; action=("tab",3)
        else:
            text="Next: preview the exact package files, destination and recovery plan before creating them."
            label="Review Package…"; action=("package",None)
        self.next_step.setText(text); self.next_action.setText(label)
        self.next_action.setVisible(action[0]!="package")
        self._next_action=action
        self._refresh_workflow_context()

    def _refresh_workflow_context(self) -> None:
        profile=self.plan.profile
        configuration=self.plan.configuration
        target=(
            configuration.target_model_family if configuration is not None
            else self.plan.goal.target_model_family or profile.target_model_family
        ) or "Unspecified target"
        version=(
            configuration.target_model_version if configuration is not None
            else self.plan.goal.target_model_version or profile.target_model_version
        )
        if version:
            target=f"{target} {version}"
        base_model=(
            configuration.base_model_id if configuration is not None
            else self.plan.goal.base_model_id
        )
        model=f"{profile.name} v{profile.version} for {target}"
        model+=f" • Base model: {base_model}" if base_model else " • Base model not chosen"
        source=self.plan.source_label
        collection_source=re.fullmatch(
            r"Collection (.+) \[(concept\.[0-9a-f]{32})\] snapshot ([0-9a-f]{64})",
            source,
        )
        if collection_source is not None:
            source=f"Collection {collection_source.group(1)} (saved exact snapshot)"
        using=(
            f"Source: {source} • {len(self.plan.frozen_scope):,} frozen • "
            f"{len(self.plan.candidates):,} inspected • {self.plan.count('include'):,} included"
        )
        self.workflow_context.labels["using"].setToolTip(
            f"Exact saved source: {self.plan.source_label}"
        )
        blocking_candidates=sum(bool(candidate.blocking) for candidate in self.plan.candidates)
        blocking_plan_issues=sum(issue.level=="blocking" for issue in self.plan.issues)
        if self._refresh_required:
            readiness="Blocked until changed image evidence is refreshed."
        elif self.plan.cancelled:
            readiness=(
                f"Inspection stopped after {len(self.plan.candidates):,} of "
                f"{len(self.plan.frozen_scope):,} images."
            )
        elif blocking_candidates or blocking_plan_issues:
            total=blocking_candidates+blocking_plan_issues
            readiness=f"Needs review: {total:,} blocking finding(s)."
        elif not self.plan.permission_ready:
            readiness="Candidate scope is ready; source and permission review remains."
        elif not self.plan.package_ready:
            readiness="Candidate scope and permission are ready; training setup remains."
        else:
            readiness="Ready to preview the exact training package."
        self.workflow_context.update_context(
            using=using,
            model=model,
            result=(
                "A reviewed plan that can be saved to Training Drafts or prepared as a "
                "training package; creating the package and starting training remain explicit."
            ),
            readiness=readiness,
            next_step=self.next_step.text(),
        )
        self.summary.setText(f"Review Training Data — {target}")
        self.scope_summary.setText(
            f"Goal: {self.plan.goal.text}\n"
            f"{source} • {len(self.plan.frozen_scope):,} frozen • "
            f"{len(self.plan.candidates):,} inspected • {self.plan.count('include'):,} included • "
            f"{self.plan.count('exclude'):,} excluded"
        )

    def _activate_next_action(self) -> None:
        if self._refresh_required:
            self._request_candidate_refresh()
            return
        if self._draft_save_task is not None:return
        kind,value=self._next_action
        if kind=="candidate":self._focus_candidate(value)
        elif kind=="package":self._continue_to_package()
        else:
            self._show_evidence_tab(value)
            if value==2:self.permission_disposition_button.setFocus()
            elif value==3:self.configure_button.setFocus()
            else:self.tree.setFocus()

    def _save_draft(self) -> None:
        if self.draft_store is None or self._review_busy or self._refresh_required or self._draft_save_task is not None:
            return
        suggested=(
            self.draft.name if self.draft is not None
            else self.plan.goal.text[:80].strip() or "Training dataset draft"
        )
        name,accepted=QInputDialog.getText(
            self,"Save Training Dataset Draft","Draft name",text=suggested,
        )
        if not accepted:
            return
        draft_id=self.draft.draft_id if self.draft is not None else ""
        expected=(self.draft.fingerprint if self.draft is not None else None)
        if self.task_pool is not None:
            self._set_draft_save_busy(True)

            def save(_progress):
                return self.draft_store.save_plan(
                    self.plan,name,draft_id=draft_id,
                    expected_fingerprint=expected,
                )

            task=ProgressCallTask(save); self._draft_save_task=task
            task.signals.complete.connect(self._draft_saved)
            task.signals.failed.connect(self._draft_save_failed)
            start_qt_delivery_task(self.task_pool,task)
            return
        try:
            result=self.draft_store.save_plan(
                self.plan,name,
                draft_id=draft_id,expected_fingerprint=expected,
            )
        except Exception as exc:
            self.draft_status.setText(
                f"Draft not saved: {exc}. Your plan is still here; choose Save Draft to retry."
            )
            QMessageBox.warning(self,"Training draft not saved",str(exc))
            return
        self._draft_saved(result)

    def _set_draft_save_busy(self,busy: bool) -> None:
        busy=busy or self._review_busy
        if busy:
            self.caption_review.clear("Caption review is on hold while the plan is saved or refreshed.")
        else:
            self._refresh_caption_review()
        self.tabs.setEnabled(not busy)
        self.next_action.setEnabled(not busy)
        self.tree.setEnabled(not busy)
        self.edit_override_button.setEnabled(
            not busy and self._selected_candidate() is not None
        )
        self.use_recommendation_button.setEnabled(
            not busy and bool(
                self._selected_candidate() and self._selected_candidate().user_override
            )
        )
        self.fix_candidate_button.setEnabled(not busy and self._can_fix and self._selected_candidate() is not None and self._selected_fix_action() is not None)
        self.refresh_candidate_button.setEnabled(not busy and self._can_fix and self._selected_candidate() is not None)
        self.save_draft_button.setEnabled(not busy and self.draft_store is not None and not self._refresh_required)
        self.permission_disposition_button.setEnabled(not busy)
        self.configure_button.setEnabled(not busy)
        self.package_button.setEnabled(not busy and self.plan.package_ready and not self._refresh_required)
        self.close_button.setEnabled(not busy)
        if busy and not self._review_busy:
            self.draft_status.setText(
                "Saving the complete source-neutral draft with verified read-back…"
            )

    def _draft_saved(self,result) -> None:
        self._draft_save_task=None
        self._set_draft_save_busy(False)
        self.draft=result.draft
        self.draft_status.setText(
            f"Saved source-neutral draft “{result.draft.name}”. Source images, Image "
            "Knowledge, captions, Metadata, and training files were not changed."
        )

    def _draft_save_failed(self,message: str) -> None:
        self._draft_save_task=None
        self._set_draft_save_busy(False)
        self.draft_status.setText(
            f"Draft not saved: {message}. Your plan is still here; choose Save Draft to retry."
        )
        QMessageBox.warning(self,"Training draft not saved",message)

    def _close_requested(self) -> None:
        if self._draft_save_task is None and not self._review_busy and self._confirm_pending_close():
            self.accept()

    def _confirm_pending_close(self) -> bool:
        if not self._refresh_required or self._discard_refresh_confirmed:
            return True
        if QMessageBox.question(
            self,"Close unfinished training review?",
            "Saved image corrections will remain saved. Unsaved training-plan decisions "
            "and this refresh retry will be discarded. Reopen your saved draft or build "
            "the exact selection again before preparing a package.\n\nClose this review?",
            QMessageBox.Yes|QMessageBox.No,QMessageBox.No,
        )!=QMessageBox.Yes:
            return False
        self._discard_refresh_confirmed=True
        return True

    def show_package_notice(self,message) -> None:
        """Keep handoff feedback outside the retained review's scroller."""
        self.refresh_status.setText(message)
        owner=getattr(self,"_training_workspace_owner",None)
        if owner is not None:
            owner.status.setText(message)
            owner._sync_stage_status()
        from .components import refresh_status_footer
        refresh_status_footer(self)

    def _continue_to_package(self) -> None:
        if self._draft_save_task is None and not self._review_busy and not self._refresh_required and self.plan.package_ready:
            self.package_requested.emit()

    def closeEvent(self,event) -> None:
        if self._draft_save_task is not None or self._review_busy:
            event.ignore()
            return
        if not self._confirm_pending_close():
            event.ignore()
            return
        super().closeEvent(event)

    def reject(self) -> None:
        if self._draft_save_task is None and not self._review_busy and self._confirm_pending_close():
            super().reject()

    def _dataset_evidence_tab(self) -> QWidget:
        page=QWidget(); layout=QVBoxLayout(page)
        self.dataset_evidence_summary=QLabel()
        self.dataset_evidence_summary.setWordWrap(True)
        self.dataset_evidence_summary.setObjectName("sectionTitle")
        layout.addWidget(self.dataset_evidence_summary)
        note=QLabel(
            "These are named split, caption, resolution, aspect, and leakage "
            "observations—not a universal dataset-quality or confidence score. "
            "Activate or double-click a finding to focus one affected candidate."
        )
        note.setWordWrap(True); note.setObjectName("mutedLabel")
        layout.addWidget(note)
        self.dataset_evidence=QTreeWidget()
        self.dataset_evidence.setAccessibleName(
            "Training role recommendations and dataset evidence"
        )
        self.dataset_evidence.setAccessibleDescription(
            "Named split integrity, balance, diversity, correlation, leakage, and limitation findings."
        )
        self.dataset_evidence.setHeaderLabels((
            "Category / Finding","State","Affected","Why / Measured Evidence","Direct Review",
        ))
        self.dataset_evidence.setAlternatingRowColors(True)
        header=self.dataset_evidence.header()
        header.setSectionResizeMode(0,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3,QHeaderView.Stretch)
        header.setSectionResizeMode(4,QHeaderView.Interactive)
        self.dataset_evidence.setColumnWidth(4,280)
        self.dataset_evidence.itemDoubleClicked.connect(
            self._focus_evidence_candidate
        )
        self.dataset_evidence.itemActivated.connect(
            self._focus_evidence_candidate
        )
        layout.addWidget(self.dataset_evidence,1)
        self.split_rule_detail=QLabel()
        self.split_rule_detail.setWordWrap(True)
        self.split_rule_detail.setObjectName("mutedLabel")
        self.split_rule_detail.setTextInteractionFlags(
            Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard
        )
        self.split_rule_detail.setAccessibleName("Deterministic split rule evidence")
        layout.addWidget(self.split_rule_detail)
        return page

    def _permission_tab(self) -> QWidget:
        page=QWidget(); layout=QVBoxLayout(page)
        self.permission_summary=QLabel()
        self.permission_summary.setWordWrap(True)
        self.permission_summary.setObjectName("sectionTitle")
        layout.addWidget(self.permission_summary)
        note=QLabel(
            "Reported evidence is carried exactly from the named structured contract. "
            "Unknown means not recorded—not permitted or prohibited. Raw Metadata, "
            "generator records, Image Knowledge completeness, and content ratings are "
            "not interpreted as permission. CollectiGen does not make a legal judgment."
        )
        note.setWordWrap(True); note.setObjectName("mutedLabel")
        layout.addWidget(note)
        self.permission_evidence=QTreeWidget()
        self.permission_evidence.setAccessibleName(
            "Training source and permission evidence"
        )
        self.permission_evidence.setAccessibleDescription(
            "Reported and unknown creator, source, license, consent or use restriction, and real-person or privacy fields."
        )
        self.permission_evidence.setHeaderLabels((
            "Evidence field","State","Affected","Reported evidence","Direct review",
        ))
        self.permission_evidence.setAlternatingRowColors(True)
        header=self.permission_evidence.header()
        header.setSectionResizeMode(0,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3,QHeaderView.Stretch)
        header.setSectionResizeMode(4,QHeaderView.Interactive)
        self.permission_evidence.setColumnWidth(4,260)
        self.permission_evidence.itemActivated.connect(
            self._focus_evidence_candidate
        )
        self.permission_evidence.itemDoubleClicked.connect(
            self._focus_evidence_candidate
        )
        layout.addWidget(self.permission_evidence,1)
        self.permission_disposition_detail=QLabel()
        self.permission_disposition_detail.setWordWrap(True)
        self.permission_disposition_detail.setObjectName("mutedLabel")
        self.permission_disposition_detail.setTextInteractionFlags(
            Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard
        )
        self.permission_disposition_detail.setAccessibleName(
            "Recorded source and permission disposition"
        )
        layout.addWidget(self.permission_disposition_detail)
        return page

    def _training_setup_tab(self) -> QWidget:
        page=QWidget(); layout=QVBoxLayout(page)
        self.training_setup_summary=QLabel()
        self.training_setup_summary.setWordWrap(True)
        self.training_setup_summary.setObjectName("sectionTitle")
        layout.addWidget(self.training_setup_summary)
        self.training_setup_counts=QLabel()
        self.training_setup_counts.setWordWrap(True)
        layout.addWidget(self.training_setup_counts)
        self.training_setup_scroll=QScrollArea(page)
        self.training_setup_scroll.setWidgetResizable(True)
        self.training_setup_scroll.setFrameShape(QScrollArea.NoFrame)
        self.training_setup_scroll.setAccessibleName("Training setup details")
        content=QWidget()
        details_layout=QVBoxLayout(content)
        details_layout.setContentsMargins(0,0,0,0)
        self.training_setup_scroll.setWidget(content)
        layout.addWidget(self.training_setup_scroll,1)
        self.training_setup_context=QLabel()
        self.training_setup_context.setWordWrap(True)
        self.training_setup_context.setObjectName("mutedLabel")
        details_layout.addWidget(self.training_setup_context)
        note=QLabel(
            "These are editable planning choices, not a training run or a universal recipe. "
            "A named trainer must still verify model loading and tokenization; this handoff "
            "does not download, install, upload or execute anything."
        )
        note.setWordWrap(True); note.setObjectName("mutedLabel")
        details_layout.addWidget(note)
        self.training_setup=QTreeWidget()
        self.training_setup.setAccessibleName("Training handoff settings and evidence")
        self.training_setup.setHeaderLabels((
            "Area / Setting","Current request","State",
        ))
        self.training_setup.setAlternatingRowColors(True)
        self.training_setup.setMinimumHeight(220)
        header=self.training_setup.header()
        header.setSectionResizeMode(0,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1,QHeaderView.Stretch)
        header.setSectionResizeMode(2,QHeaderView.ResizeToContents)
        self.training_setup.currentItemChanged.connect(self._show_training_setup_detail)
        details_layout.addWidget(self.training_setup,1)
        self.training_setup_detail=QGroupBox("Selected setting")
        detail_form=QFormLayout(self.training_setup_detail)
        self.training_setup_why=QLabel()
        self.training_setup_fix=QLabel()
        for value in (self.training_setup_why,self.training_setup_fix):
            value.setWordWrap(True)
            value.setTextInteractionFlags(
                Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard
            )
        detail_form.addRow("Why it matters",self.training_setup_why)
        detail_form.addRow("What to check",self.training_setup_fix)
        details_layout.addWidget(self.training_setup_detail)
        estimate_group=QGroupBox("Planning estimate")
        estimate_group.setAccessibleName("Training planning estimate and limits")
        estimate_form=QFormLayout(estimate_group)
        self.resource_workload=QLabel()
        self.resource_storage=QLabel()
        self.resource_limits=QLabel()
        self.resource_estimate=QLabel()
        for value in (self.resource_workload,self.resource_storage,
                      self.resource_limits,self.resource_estimate):
            value.setWordWrap(True)
            value.setObjectName("mutedLabel")
            value.setTextInteractionFlags(
                Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard
            )
        estimate_form.addRow("Workload",self.resource_workload)
        estimate_form.addRow("Storage",self.resource_storage)
        estimate_form.addRow("Time, GPU, workspace",self.resource_limits)
        estimate_form.addRow("Estimate limits",self.resource_estimate)
        details_layout.addWidget(estimate_group)
        return page

    def _show_training_setup_detail(self,item,previous=None) -> None:
        detail=item.data(0,Qt.UserRole) if item is not None else None
        self.training_setup_detail.setVisible(bool(detail))
        self.training_setup_why.setText(detail[0] if detail else "")
        self.training_setup_fix.setText(detail[1] if detail else "")

    def _refresh_training_setup(self) -> None:
        selected=self.training_setup.currentItem()
        selected_name=selected.text(0) if selected is not None else None
        self.training_setup.clear()
        configuration=self.plan.configuration
        if configuration is None:
            self.training_setup_summary.setText(
                "No portable training configuration is available."
            )
            self.training_setup_counts.clear()
            self.training_setup_context.clear()
            for value in (self.resource_workload,self.resource_storage,self.resource_limits):
                value.clear()
            self.resource_estimate.setText(
                "Rebuild this legacy plan to calculate scoped recommendations."
            )
            return
        family=configuration.target_model_family or "Unspecified"
        version=(f" {configuration.target_model_version}" if configuration.target_model_version else "")
        base=configuration.base_model_id or "Not recorded"
        rows=(
            ("Target compatibility",f"{family}{version} • base {base}","Declared, not verified","The caption profile and settings must name the same family/version. The manual adapter never loads the base model.","Enter a portable base-model ID; the eventual trainer must verify it."),
            ("Resolution / buckets / crop",f"{configuration.target_resolution}px • {configuration.bucket_policy} • crop {configuration.crop_policy}","Requested","Package files preserve their source framing unless explicit technical normalization is required. Bucketing/crop remains a downstream request.","Review every per-image dimension and future crop before execution."),
            ("Captions / trigger / class",f"{configuration.caption_prefix_policy} • limit {configuration.max_caption_tokens}","Exact text, tokenizer unavailable","Canonical captions are preserved; stable trigger/class terms are prepended only when deliberately selected. Variants are unsupported by this adapter.","Review exact package captions and tokenizer effects in the eventual trainer."),
            ("Repeats / weighting",f"{configuration.repeats} repeat(s) • {configuration.epochs} epoch(s)","Requested","Every training image receives the same baseline repeats; effective exposures are shown below.","Use per-image inclusion decisions rather than hidden weights."),
            ("Regularization / class images","None","Not included","The package does not invent class images or decide that this goal needs DreamBooth-style regularization.","A future named trainer adapter must identify and preview any applicable need."),
            ("Validation / holdout","Reviewed assigned roles","Ready when roles exist","The deterministic split roles are preserved exactly and never substituted.","Review the Dataset Evidence tab before packaging."),
            ("Expected output / lineage",f"{configuration.output_kind} • {configuration.output_name}","Expected, not created","A future output should link to the base model, plan, package, settings, and source-manifest fingerprints.","No model is created or registered by TRN-01."),
        )
        for row in rows:
            item=QTreeWidgetItem(row[:3])
            item.setData(0,Qt.UserRole,row[3:])
            self.training_setup.addTopLevelItem(item)
        for issue in self.plan.configuration_issues:
            item=QTreeWidgetItem((
                f"Check — {issue.summary}","—",issue.level.title(),
            ))
            item.setData(0,Qt.UserRole,(
                issue.reason,issue.direct_fix or "Review this setting.",
            ))
            self.training_setup.addTopLevelItem(item)
        restored=next((self.training_setup.topLevelItem(index)
                       for index in range(self.training_setup.topLevelItemCount())
                       if self.training_setup.topLevelItem(index).text(0)==selected_name),None)
        self.training_setup.setCurrentItem(restored or self.training_setup.topLevelItem(0))
        estimate=estimate_training_resources(configuration,self.plan.candidates)
        self.resource_workload.setText(
            f"{estimate.effective_training_exposures:,} effective image exposures; "
            +(f"{estimate.optimizer_steps:,} optimizer steps" if estimate.optimizer_steps is not None
              else "optimizer steps pending exact trainer batching")
        )
        self.resource_storage.setText(
            f"Package lower bound {estimate.package_bytes_lower_bound:,} bytes; "
            f"safe staging reserve {estimate.package_staging_bytes:,} bytes."
        )
        self.resource_limits.setText(
            f"Time: {estimate.time_range}\n"
            f"GPU memory: {estimate.vram_range}\n"
            f"Training workspace: {estimate.training_workspace_range}"
        )
        self.resource_estimate.setText(
            f"Method: {estimate.heuristic_id}\n"
            +"\n".join(f"• {assumption}" for assumption in estimate.assumptions)
        )
        blocking=sum(item.level=="blocking" for item in self.plan.configuration_issues)
        cautions=sum(item.level=="caution" for item in self.plan.configuration_issues)
        self.training_setup_summary.setText(
            "Package gate satisfied" if self.plan.package_ready
            else "Package gate remains on hold"
        )
        self.training_setup_counts.setText(
            f"Settings only: {blocking:,} blocking issue(s) • {cautions:,} caution(s). "
            "Other package requirements are reviewed in the plan's other tabs."
        )
        self.training_setup_context.setText(
            f"Manual package adapter v{configuration.adapter_version} • "
            f"{len(configuration.overridden_fields):,} changed recommended default(s)"
        )

    def _refresh_permission_evidence(self) -> None:
        self.permission_evidence.clear()
        included=tuple(
            candidate for candidate in self.plan.candidates
            if candidate.decision=="include"
        )
        for name,label in PERMISSION_FIELD_LABELS:
            reported=[]; unknown=[]
            for candidate in included:
                field=getattr(candidate.permission_evidence,name)
                if field.status=="reported":
                    reported.append((candidate,field))
                else:
                    unknown.append(candidate)
            examples=[]
            for _candidate,field in reported:
                rendered=f"{field.value} ({field.evidence_source})"
                if rendered not in examples:
                    examples.append(rendered)
                if len(examples)>=3:
                    break
            state=(
                "Reported" if reported and not unknown else
                "Mixed" if reported else "Unknown"
            )
            detail=" • ".join(examples) if examples else "No structured evidence recorded"
            if len(reported)>len(examples):
                more=len(reported)-len(examples)
                detail+=f" • {more:,} more reported value{'s' if more!=1 else ''}"
            item=QTreeWidgetItem((
                label,state,
                f"{len(reported):,} reported / {len(unknown):,} unknown",
                detail,
                "Review the source evidence and record a disposition for this exact included scope.",
            ))
            target=(unknown[0] if unknown else reported[0][0] if reported else None)
            if target is not None:
                item.setData(0,Qt.UserRole,target.stable_id)
            self.permission_evidence.addTopLevelItem(item)
        disposition=self.plan.permission_disposition
        state={
            "pending":"Pending","needs_review":"Needs review","proceed":"Proceed",
        }.get(disposition.status,"Invalid")
        image_count=len(included)
        unknown_count=self.plan.permission_unknown_count
        self.permission_summary.setText(
            f"{state} • {image_count:,} included image"
            f"{'s' if image_count!=1 else ''} • "
            f"{unknown_count:,} unknown evidence field"
            f"{'s' if unknown_count!=1 else ''} • "
            +(
                "future package permission gate satisfied for this exact scope"
                if self.plan.permission_ready else
                "future package permission gate remains on hold"
            )
        )
        reason=disposition.reason or "No user disposition has been recorded."
        self.permission_disposition_detail.setText(
            f"Disposition: {state}. {reason} Evidence scope: "
            f"{disposition.evidence_scope_fingerprint}. This record is a user decision, not legal clearance."
        )

    def _refresh_dataset_evidence(self,counts=None) -> None:
        self.dataset_evidence.clear()
        analysis=self.plan.analysis
        if analysis is None:
            self.dataset_evidence_summary.setText(
                "Dataset evidence is unavailable for this legacy plan."
            )
            self.split_rule_detail.setText(
                "Rebuild the plan from current sources to calculate named evidence."
            )
            return
        counts=counts or {
            "training":sum(
                candidate.decision=="include" and candidate.role=="training"
                for candidate in self.plan.candidates
            ),
            "validation":sum(
                candidate.decision=="include" and candidate.role=="validation"
                for candidate in self.plan.candidates
            ),
            "holdout":sum(
                candidate.decision=="include" and candidate.role=="holdout"
                for candidate in self.plan.candidates
            ),
        }
        override_count=sum(candidate.user_override for candidate in self.plan.candidates)
        included={
            candidate.stable_id for candidate in self.plan.candidates
            if candidate.decision=="include"
        }
        active_groups=[]
        for group in analysis.groups:
            active=sum(stable_id in included for stable_id in group.member_stable_ids)
            if active:
                active_groups.append((group,active))
        grouped_ids={
            stable_id for group,_active in active_groups
            for stable_id in group.member_stable_ids if stable_id in included
        }
        ungrouped=len(included-grouped_ids)
        exact=sum(
            group.evidence_kind=="byte_identical" and active>1
            for group,active in active_groups
        )
        group_count=len(active_groups)+ungrouped
        video=sum(group.evidence_kind=="source_video" for group,_active in active_groups)
        standalone=group_count-exact-video
        self.dataset_evidence_summary.setText(
            f"Current plan roles • {counts['training']:,} training • "
            f"{counts['validation']:,} validation • {counts['holdout']:,} holdout • "
            f"{group_count:,} active split group(s) "
            f"({standalone:,} standalone, {exact:,} byte-identical, {video:,} source-video)"
            +(f" • {override_count:,} user override(s)" if override_count else "")
        )
        for finding in analysis.findings:
            affected=(
                f"{finding.affected_count:,}/{finding.denominator:,}"
                if finding.denominator else str(finding.affected_count)
            )
            item=QTreeWidgetItem((
                f"{finding.category} — {finding.summary}",finding.level.title(),
                affected,finding.reason,finding.direct_fix or "Review the evidence.",
            ))
            if finding.stable_ids:
                item.setData(0,Qt.UserRole,finding.stable_ids[0])
            item.setToolTip(3," • ".join(finding.evidence))
            self.dataset_evidence.addTopLevelItem(item)
        for limitation in analysis.limitations:
            self.dataset_evidence.addTopLevelItem(QTreeWidgetItem((
                "Limitation","Limitation","—",limitation,
                "Do not interpret unavailable evidence as a completed check.",
            )))
        targets=", ".join(
            f"{role} {percent}%" for role,percent in analysis.role_targets
        )
        self.split_rule_detail.setText(
            f"Split rule: {analysis.split_rule_version} • seed "
            f"{analysis.split_seed} • target proportions {targets}. "
            "The rule assigns whole exact-evidence groups by stable hash; small "
            "datasets stay training-only until enough distinct split groups exist."
        )

    def _focus_evidence_candidate(self,item,*_args) -> None:
        stable_id=str(item.data(0,Qt.UserRole) or "")
        self._focus_candidate(stable_id)

    def _focus_candidate(self,stable_id: str) -> None:
        if stable_id not in self._candidate_by_id:return
        if stable_id not in self._filtered_positions:
            self.candidate_filter.blockSignals(True)
            self.candidate_search.blockSignals(True)
            self.candidate_filter.setCurrentIndex(0); self.candidate_search.clear()
            self.candidate_filter.blockSignals(False)
            self.candidate_search.blockSignals(False)
            self._filter_candidates(selected_id=stable_id)
        else:
            self._show_candidate_page(
                self._filtered_positions[stable_id]//self.CANDIDATE_PAGE_SIZE,
                selected_id=stable_id,
            )
        self.tabs.setCurrentIndex(0)
        self.tree.scrollToItem(self.tree.currentItem())
        self.tree.setFocus()

    @staticmethod
    def _candidate_item(candidate) -> QTreeWidgetItem:
        evidence = candidate.evidence
        technical = (
            f"{evidence.width}×{evidence.height} {evidence.format}/{evidence.mode}"
            if evidence is not None else "Source unavailable"
        )
        levels = {
            "blocking": sum(
                issue.level == "blocking" for issue in candidate.issues
            ),
            "caution": sum(
                issue.level == "caution" for issue in candidate.issues
            ),
            "recommendation": sum(
                issue.level == "recommendation" for issue in candidate.issues
            ),
        }
        evidence_summary = (
            f"{technical} • {levels['blocking']} blocking • "
            f"{levels['caution']} cautions • "
            f"{levels['recommendation']} recommendations"
        )
        recommendation=(
            candidate.decision.title()
            +(" (User Override)" if candidate.user_override else "")
        )
        role=(
            candidate.role.title()
            +(" (User Override)" if candidate.user_override else " (Recommended)")
        )
        item = QTreeWidgetItem((
            candidate.image.name,
            recommendation,
            role,
            f"{candidate.decision_reason} {evidence_summary}",
            candidate.caption or "(No eligible caption)",
        ))
        item.setToolTip(0, str(candidate.image))
        item.setToolTip(4, candidate.caption)
        grouped={
            "Image Knowledge completeness":[],
            "Selected-profile readiness":[],
            "Source-file preflight":[],
            "Source & permission evidence":[],
            "Training goal/dataset observations":[],
        }
        for issue in candidate.issues:
            grouped[TrainingCandidatePlanDialog._issue_layer(issue)].append(issue)
        for _name,label,field in candidate.permission_evidence.field_items():
            grouped["Source & permission evidence"].append(SimpleNamespace(
                level=field.status,
                summary=label,
                reason=(
                    f"{field.value} Evidence source: {field.evidence_source}."
                    if field.status=="reported" else
                    "No structured evidence is recorded for this field; no permission conclusion is inferred."
                ),
                direct_fix="Review this field in Source & Permission before packaging.",
                evidence=(),
            ))
        if evidence is not None:
            conversions=(
                ", ".join(evidence.conversion_requirements)
                if evidence.conversion_requirements else "No conversion flagged"
            )
            grouped["Source-file preflight"].insert(0,SimpleNamespace(
                level="observed",summary="Observed source",
                reason=(
                    f"{technical}; {evidence.frame_count} frame(s); "
                    f"orientation {evidence.exif_orientation}; "
                    f"alpha {'yes' if evidence.has_alpha else 'no'}; "
                    f"color profile {'yes' if evidence.has_color_profile else 'no'}; "
                    f"{conversions}."
                ),
                direct_fix="",evidence=(f"sha256={evidence.sha256}",),
            ))
        selected_profile_issues=grouped["Selected-profile readiness"]
        if not selected_profile_issues:
            grouped["Selected-profile readiness"].append(SimpleNamespace(
                level="ready",summary="Eligible",
                reason=(
                    "The selected profile produced the exact non-empty caption "
                    "shown above."
                ),
                direct_fix="",evidence=(),
            ))
        if not grouped["Image Knowledge completeness"]:
            grouped["Image Knowledge completeness"].append(SimpleNamespace(
                level="observed",summary="No blocking completeness issue",
                reason=(
                    "The current profile assessment reported no Image Knowledge "
                    "completeness blocker."
                ),
                direct_fix="",evidence=(),
            ))
        if not grouped["Training goal/dataset observations"]:
            grouped["Training goal/dataset observations"].append(SimpleNamespace(
                level="observed",summary="See Dataset Evidence",
                reason=(
                    "The Dataset Evidence tab reports deterministic split, caption, "
                    "resolution, aspect, balance, correlation, and leakage observations."
                ),
                direct_fix="",evidence=(),
            ))
        for layer,issues in grouped.items():
            layer_item=QTreeWidgetItem((layer,"","","",""))
            if layer=="Source & permission evidence":
                layer_item.setData(0,Qt.UserRole+1,CandidateFixAction.PERMISSION)
            item.addChild(layer_item)
            for issue in issues:
                detail = QTreeWidgetItem((
                    issue.summary,
                    issue.level.title(),
                    "",
                    issue.reason,
                    issue.direct_fix or "—",
                ))
                detail.setToolTip(3, " • ".join(issue.evidence))
                detail.setToolTip(0,issue.summary)
                detail.setData(0,Qt.UserRole+2,f"{issue.level.title()}: {issue.summary}")
                detail.setData(0,Qt.UserRole+1,
                    CandidateFixAction.PERMISSION if layer=="Source & permission evidence"
                    else candidate_fix_action(str(getattr(issue,"code", ""))))
                layer_item.addChild(detail)
        return item

    @staticmethod
    def _issue_layer(issue) -> str:
        if issue.code.startswith("profile_"):
            if issue.code in {"profile_rating_excluded","profile_empty_export"}:
                return "Selected-profile readiness"
            return "Image Knowledge completeness"
        if issue.code.startswith("permission_"):
            return "Source & permission evidence"
        if issue.code in {
            "source_unavailable","conversion_review","low_resolution",
            "extreme_aspect_ratio","portable_identity_unavailable",
        }:
            return "Source-file preflight"
        return "Training goal/dataset observations"

    def _manifest_tab(self) -> QWidget:
        page = QWidget()
        layout = QVBoxLayout(page)
        self.manifest_note=QLabel(); self.manifest_note.setWordWrap(True)
        layout.addWidget(self.manifest_note)
        self.manifest_preview=QTextEdit()
        self.manifest_preview.setReadOnly(True)
        self.manifest_preview.setAccessibleName(
            "Source-neutral training plan manifest preview"
        )
        layout.addWidget(self.manifest_preview)
        return page
