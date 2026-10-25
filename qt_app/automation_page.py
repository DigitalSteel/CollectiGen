from __future__ import annotations

from .help_content import workspace_help

import re
import time
from copy import deepcopy
from dataclasses import dataclass,replace
from datetime import datetime,timezone
from pathlib import Path

from PySide6.QtCore import QSignalBlocker,QThreadPool,Signal,Qt,QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,QCheckBox,QComboBox,QDialog,QDialogButtonBox,QDoubleSpinBox,
    QFileDialog,QFormLayout,QFrame,QGridLayout,QGroupBox,QHBoxLayout,QHeaderView,
    QLabel,QLineEdit,QMessageBox,QPushButton,QScrollArea,QSizePolicy,QSpinBox,
    QTabWidget,QTextEdit,QTreeWidget,QTreeWidgetItem,QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core import (
    AutomationCheckpointStore,
    AutomationBehaviorPolicy,
    AutomationEngine,
    AutomationRuleAuditService,
    AutomationRuleRepository,
    AllowedDimensionPolicyService,
    CategoryRegistry,
    ConflictRuleEngine,
    CONFIDENCE_PRESETS,
    DependencyService,
    DeterministicFactExpansionPolicy,
    ExternalEvidenceIntakePolicy,
    ExportContentPolicy,
    EXTERNAL_WRITE_BACKUP_ALWAYS,
    EXTERNAL_WRITE_BACKUP_ASK,
    EXTERNAL_WRITE_BACKUP_OFF,
    ExternalWritePolicy,
    FACT_EXPANSION_MAX_DEPTH,
    FACT_EXPANSION_MIN_DEPTH,
    FACT_EXPANSION_RELATIONSHIP_GROUPS,
    LEVEL_LABELS,
    LIBRARY_NAMING_IMAGE_HASH,
    LIBRARY_NAMING_KEEP,
    LibraryCheckPolicy,
    OrganizationProfile,
    ProjectionRuleRepairService,
    RULE_INTENT_LABELS,
    RuleIntentMigrationService,
    ReusableValueBindingMigrationService,
    FoundationKnowledge,
    ModifierCompositionService,
    TagDatabase,
    TagTemplateDiscoveryService,
    TagTemplateExpansionService,
    TagTemplateOrganizationApplyService,
    TagTemplateOrganizationPlanningService,
    TagTemplateStoreService,
    DEFAULT_TAG_TEMPLATE_MAX_PER_RUN,
    active_automation_engine,
    normalize_library_naming_policy,
    safe_folder_name,
    tag_template_id,
    tag_template_selection_fingerprint,
)
from core.organization import DEFAULT_LEVEL_CONTAINERS
from core.rule_intents import explicit_rule_intent
from core.paths import CATEGORIES_PATH,DEFAULTS_DIR,RELATIONSHIPS_PATH,USER_DATA_DIR
from core.automation_rule_conversion import RulePairAdmission
from core.tag_template_builder import TagTemplateBuilderService
from core.tag_template_policy import (
    TagTemplateGenerationService,TagTemplatePolicyService,
)
from core.tag_template_projection import TagTemplateProjectionService
from .components import (
    BoundedSearchableComboBox,ContextHelpButton,PageHeader,bind_status_footer,guard_scrollable_value_controls,
    show_operation_in_progress,
)
from .settings_navigation import SettingsNavigator
from .conflict_rule_workspace import ConflictRuleWorkspaceDialog, conflict_workspace_entries
from .modal_feedback import FeedbackEnabledDialog
from .reusable_value_migration_dialog import (
    ReusableValueAuthorityDecisionDialog,ReusableValueMigrationPreviewDialog,
    ReusableValueSemanticReviewDialog,
)
from .tag_template_discovery_dialog import TagTemplateDiscoveryDialog
from .tag_template_builder_dialog import TagTemplateBuilderDialog
from .tag_template_expansion_dialog import TagTemplateExpansionDialog
from .tag_template_organization_dialog import TagTemplateOrganizationDialog
from .tag_template_policy_dialog import TagTemplatePolicyDialog
from .workers import CancelableProgressCallTask,CoordinatedCallTask,ProgressCallTask


def _rules_admission_for_knowledge(path):
    admission = RulePairAdmission(USER_DATA_DIR)
    return admission if admission.owns(path, "knowledge") else None


ORIGIN_LABELS={"Default":"Built in","User":"My rules"}


def _rule_destination_is_repository(rule_type, sources, result):
    """Use one draft-to-store decision for Save and its early explanation."""
    return rule_type == "Always ignore SDXL Tag" or any(
        "{" in value for value in [*sources, result]
    )

BEHAVIOR_SETTING_LABELS={
    "suggest_wd_for_missing_tags":"Offer AI Tags",
    "suggest_joycaption_for_missing_descriptions":"Offer AI Descriptions",
    "auto_run_wd_on_managed_images":"Analyze Tags after library checks",
    "auto_run_joy_on_managed_images":"Analyze Descriptions after library checks",
    "wd_general_threshold":"General Tag confidence",
    "wd_character_threshold":"Character Tag confidence",
    "wd_rating_threshold":"Rating Tag confidence",
    "wd_consensus_minimum":"Minimum votes after library checks",
    "auto_merge_ai_tags":"Confirm safe Tags after library checks",
    "prepare_confidence_preset":"Preparation confidence",
    "prepare_tag_vote_minimum":"Minimum votes during Prepare",
    "prepare_external_evidence_vote":"Count matching TXT / Metadata as one vote",
    "prepare_confirm_safe_tags":"Confirm safe Tags during Prepare",
    "prepare_preferred_descriptions":"Fill or upgrade Descriptions during Prepare",
    "prepare_read_external_evidence":"Read TXT and Metadata during Prepare",
    "ingest_create_duplicate_fingerprints":"Create duplicate fingerprints",
    "prepare_ai_skip_value":"Reuse matching AI results within",
    "prepare_ai_skip_unit":"AI result reuse unit",
}


def _behavior_display_value(key,value):
    if isinstance(value,bool):return "On" if value else "Off"
    if key.endswith("_threshold"):return f"{value:.0%}"
    if key=="prepare_confidence_preset":
        return CONFIDENCE_PRESETS.get(value,{}).get("label","Custom")
    if key=="prepare_ai_skip_unit":
        return {"hours":"Hours","days":"Days","months":"Months (30 days)"}.get(value,str(value))
    return str(value)


def _automation_conflict_token(state):
    knowledge=state.knowledge
    paths=(DEFAULTS_DIR/"relationships.json",DEFAULTS_DIR/"conflicts.json",
           RELATIONSHIPS_PATH,getattr(knowledge,"path",None))
    stamps=[]
    for path in paths:
        if path is None:
            continue
        try:
            stat=Path(path).stat()
            stamps.append((str(path),stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns))
        except FileNotFoundError:
            stamps.append((str(path),None))
    return (id(knowledge),getattr(knowledge,"_loaded_bytes",None),
            getattr(knowledge,"_relationship_signature_revision",None),tuple(stamps))


@dataclass(frozen=True)
class AutomationConflictRow:
    relationship_id: str
    tag_ids: tuple[str,...]
    disabled: bool
    columns: tuple[str,str,str,str]
    tooltip: str


@dataclass(frozen=True)
class AutomationConflictProjection:
    rows: tuple[AutomationConflictRow,...]
    source_token: tuple

    def is_current(self,state):
        return self.source_token==_automation_conflict_token(state)


def prepare_automation_conflict_projection(state,job=None):
    """Render immutable browse rows; the canonical editor retains write authority."""
    from core.conflict_policy import ConflictAllowPolicy

    def checkpoint():
        if job is not None and not job.checkpoint():
            raise InterruptedError("Conflict list loading cancelled")
        if job is not None:
            # Allow a Qt timer turn between the two CPU-heavy browse phases.
            time.sleep(.01)

    checkpoint()
    token=_automation_conflict_token(state)
    knowledge=state.knowledge
    _service,entries=conflict_workspace_entries(
        knowledge,DEFAULTS_DIR,RELATIONSHIPS_PATH,checkpoint=checkpoint,
    )
    checkpoint()
    rows=[]
    for entry in sorted(entries,key=lambda item:(item.relationship.label.casefold(),item.relationship.id)):
        relationship=entry.relationship
        tag_ids=tuple(endpoint.id for endpoint in relationship.endpoints if endpoint.kind=="tag")
        names=[knowledge.tags[tag_id].display if tag_id in knowledge.tags
               else next((e.label for e in relationship.endpoints if e.id==tag_id),tag_id)
               for tag_id in tag_ids]
        scope_label={
            "image":"Image-wide","same_character":"Same subject / character",
            "same_object":"Same owned object / assignment","same_context":"Same context",
        }.get(relationship.scope,relationship.scope.replace("_"," ").title())
        policy=ConflictAllowPolicy.from_payload(relationship.extras.get("allow_when"))
        rows.append(AutomationConflictRow(
            relationship.id,tag_ids,relationship.disabled,
            (("Disabled · " if relationship.disabled else "")+(relationship.label or relationship.id),
             ", ".join(names),scope_label,
             policy.summary(lambda tag_id:knowledge.tags[tag_id].display if tag_id in knowledge.tags else tag_id)),
            f"{relationship.id} · {entry.authority} · {relationship.source}\n"
            "Double-click to edit this rule. Image facts stay unchanged.",
        ))
    checkpoint()
    return AutomationConflictProjection(tuple(rows),token)


class ReusableValueEditorDialog(FeedbackEnabledDialog):
    """Keep one legacy wildcard-list draft open through validation and save."""

    feedback_workflow="Reusable values editor"

    def __init__(self,parent,*,name,values,consumers,save_callback):
        super().__init__(parent)
        self.setWindowTitle("Edit legacy value list" if name else "Add legacy value list")
        self.resize(540,300)
        self.save_callback=save_callback
        self.save_failed=False
        layout=QVBoxLayout(self)
        intro=QLabel(
            "Edit text values for a legacy rule wildcard. This does not create or edit "
            "a reusable Tag; renaming does not update rules that use the old name."
        )
        intro.setWordWrap(True); layout.addWidget(intro)
        form=QFormLayout()
        self.name_input=QLineEdit(name)
        self.name_input.setAccessibleName("Legacy list name")
        self.values_input=QLineEdit(", ".join(values))
        self.values_input.setAccessibleName("Comma-separated recognized values")
        form.addRow("List name",self.name_input)
        form.addRow("Recognized values",self.values_input)
        layout.addLayout(form)
        self.scope_toggle=QPushButton("Show Current Rule Scope")
        self.scope_toggle.setCheckable(True)
        self.scope_toggle.setProperty("compact",True)
        self.scope_details=QTextEdit()
        self.scope_details.setPlainText(
            ("Current consumers: "+consumers if consumers else "No current rule consumers recorded.")+
            "\nUse Review Compatibility for exact authority and consumer decisions. "
            "Saving this list changes rule matching, not confirmed image facts."
        )
        self.scope_details.setReadOnly(True)
        self.scope_details.setAccessibleName("Current rule scope and limits")
        self.scope_details.setMaximumHeight(104)
        self.scope_details.hide()
        self.scope_toggle.toggled.connect(self.scope_details.setVisible)
        self.scope_toggle.toggled.connect(
            lambda opened:self.scope_toggle.setText(
                "Hide Current Rule Scope" if opened else "Show Current Rule Scope"
            )
        )
        layout.addWidget(self.scope_toggle,0,Qt.AlignLeft)
        layout.addWidget(self.scope_details)
        self.error=QLabel()
        self.error.setObjectName("errorLabel")
        self.error.setAccessibleName("Reusable value save status")
        self.error.setFocusPolicy(Qt.StrongFocus)
        self.error.setWordWrap(True)
        self.error.hide();layout.addWidget(self.error)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        self.save_button=buttons.button(QDialogButtonBox.Save)
        self.save_button.setText("Save List")
        self.save_button.setProperty("primary",True)
        self.save_button.clicked.connect(self._save)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _save(self):
        try:
            values=[value.strip() for value in self.values_input.text().split(",") if value.strip()]
            self.save_callback(self.name_input.text(),values)
        except Exception as exc:
            self.save_failed=True
            self.error.setText(str(exc));self.error.show()
            self.error.setFocus()
            return
        self.accept()


class RuleEditorDialog(FeedbackEnabledDialog):
    feedback_workflow = "Automation rule editor"

    _placeholder=re.compile(r"\{([a-zA-Z][a-zA-Z0-9_]*)\}")

    def __init__(self,parent=None,*,rule_type="Merge SDXL Tags",sources=(),target="",known_tags=(),templates=(),template_variables=(),knowledge=None,categories=None,save_callback=None):
        super().__init__(parent); self.setWindowTitle("Automatic Rule"); self.resize(700,430)
        self._save_callback=save_callback
        self._saving=False
        self._saved_once=False
        variable_items=template_variables.items() if hasattr(template_variables,"items") else ((value,()) for value in template_variables)
        self.variable_values={str(name).strip().casefold():[str(value).strip() for value in values if str(value).strip()] for name,values in variable_items if str(name).strip()}
        self.template_variables=set(self.variable_values)
        variable_tokens=[f"{{{value}}}" for value in sorted(self.template_variables)]
        choices=[]; choice_search_terms={}; choice_ids={}; seen=set()
        for item in known_tags:
            if isinstance(item,(tuple,list)):
                value=item[0] if item else ""
                search_terms=item[1] if len(item)>1 else ()
                stable_id=item[2] if len(item)>2 else value
            else:
                value=item; search_terms=(); stable_id=value
            value=str(value).strip()
            if value and value.casefold() not in seen:
                seen.add(value.casefold()); choices.append(value)
                if isinstance(search_terms,str): search_terms=(search_terms,)
                choice_search_terms[value.casefold()]=tuple(search_terms)
                choice_ids[value.casefold()]=stable_id
        for value in [*templates,*variable_tokens,*sources,target]:
            value=str(value).strip()
            if value and value.casefold() not in seen: seen.add(value.casefold()); choices.append(value)
        self.known_choices=choices
        layout=QVBoxLayout(self); form=QFormLayout()
        self.rule_type=QComboBox(); self.rule_type.addItems(("Merge SDXL Tags","Always ignore SDXL Tag")); self.rule_type.setCurrentText(rule_type)
        self.source_panel=QWidget(); source_layout=QVBoxLayout(self.source_panel); source_layout.setContentsMargins(0,0,0,0)
        source_add=QHBoxLayout(); self.source_choice=BoundedSearchableComboBox(
            placeholder="Search known SDXL Tags, aliases, or templates…",
            max_visible=12,allow_custom=True,
        )
        self.source_choice.setAccessibleName("Matching SDXL Tag or template")
        for value in choices:
            self.source_choice.addItem(
                value,choice_ids.get(value.casefold(),value),
                search_terms=choice_search_terms.get(value.casefold(),()),
            )
        add_source=QPushButton("Add"); add_source.setProperty("compact",True); add_source.clicked.connect(self._add_source)
        source_add.addWidget(self.source_choice,1); source_add.addWidget(add_source); source_layout.addLayout(source_add)
        self.sources=QTreeWidget(); self.sources.setHeaderLabels(("Matching SDXL Tags",)); self.sources.setRootIsDecorated(False); self.sources.setMaximumHeight(105)
        source_layout.addWidget(self.sources)
        remove_source=QPushButton("Remove"); remove_source.setProperty("compact",True); remove_source.clicked.connect(self._remove_sources)
        source_layout.addWidget(remove_source,0,Qt.AlignLeft)
        for value in sources: self.sources.addTopLevelItem(QTreeWidgetItem((str(value),)))
        self.target=BoundedSearchableComboBox(
            placeholder="Search known SDXL Tags, aliases, or templates…",
            max_visible=12,allow_custom=True,
        )
        self.target.setAccessibleName("Result SDXL Tag or template")
        for value in choices:
            self.target.addItem(
                value,choice_ids.get(value.casefold(),value),
                search_terms=choice_search_terms.get(value.casefold(),()),
            )
        self.target.setCurrentText(target)
        self.wildcard_panel=QWidget(); wildcard_layout=QHBoxLayout(self.wildcard_panel); wildcard_layout.setContentsMargins(0,0,0,0)
        self.wildcard_choice=QComboBox(); self.wildcard_choice.addItems(variable_tokens)
        source_wildcard=QPushButton("Add to Match"); source_wildcard.setProperty("compact",True); source_wildcard.clicked.connect(self._wildcard_source)
        target_wildcard=QPushButton("Add to Result"); target_wildcard.setProperty("compact",True); target_wildcard.clicked.connect(self._wildcard_target)
        wildcard_layout.addWidget(self.wildcard_choice); wildcard_layout.addWidget(source_wildcard); wildcard_layout.addWidget(target_wildcard); wildcard_layout.addStretch(1)
        self.sources_label=QLabel("SDXL Tags that appear together"); self.sources_label.setMinimumWidth(self.sources_label.sizeHint().width()); self.target_label=QLabel("Result Tag")
        form.addRow("Rule type",self.rule_type); form.addRow(self.sources_label,self.source_panel); form.addRow(self.target_label,self.target); form.addRow("Reusable wildcard",self.wildcard_panel); layout.addLayout(form)
        self.wildcard_label = form.labelForField(self.wildcard_panel)
        self.behavior_text=QLabel(); self.behavior_text.setWordWrap(True); self.behavior_text.setObjectName("mutedLabel")
        layout.addWidget(self.behavior_text)
        self.help_text=QLabel("Choose a known SDXL Tag or enter a template such as {color} shirt. Manage reusable values in the Reusable Values & Tags tab after closing this editor.")
        self.help_text.setWordWrap(True); self.help_text.setObjectName("mutedLabel"); layout.addWidget(self.help_text)
        self.type_change_notice=QLabel()
        self.type_change_notice.setWordWrap(True)
        self.type_change_notice.setObjectName("mutedLabel")
        self.type_change_notice.setAccessibleName("Rule storage change availability")
        self.type_change_notice.hide()
        layout.addWidget(self.type_change_notice)
        self._missing_tag_names = None
        self._create_missing_tag = None
        self._missing_tag_saved = None
        self._missing_tag_pending = ""
        self._missing_tag_request_values = None
        self.missing_tag_panel = QWidget()
        missing_layout = QVBoxLayout(self.missing_tag_panel)
        missing_layout.setContentsMargins(0, 0, 0, 0)
        self.missing_tag_status = QLabel()
        self.missing_tag_status.setWordWrap(True)
        missing_layout.addWidget(self.missing_tag_status)
        missing_actions = QHBoxLayout()
        self.missing_tag_choice = QComboBox()
        self.missing_tag_choice.setAccessibleName("Missing Tag required by this rule")
        self.create_missing_tag_button = QPushButton("Create Selected Tag…")
        self.create_missing_tag_button.clicked.connect(self._request_missing_tag)
        missing_actions.addWidget(self.missing_tag_choice, 1)
        missing_actions.addWidget(self.create_missing_tag_button)
        missing_layout.addLayout(missing_actions)
        self.missing_tag_panel.hide()
        layout.addWidget(self.missing_tag_panel)
        layout.addStretch(1)
        buttons=QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        self.save_button=buttons.button(QDialogButtonBox.Save)
        self.save_button.setText("Save Automatic Rule")
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.rule_type.currentTextChanged.connect(self._type_changed); self._type_changed(self.rule_type.currentText())
        if knowledge is not None and categories is not None:
            from .rule_tag_editor import RuleTagEditor
            self.resize(820, 620)
            self._syncing_rule_tags = False
            self.source_tags_editor = RuleTagEditor(knowledge, categories, relation_label="Matching", names=True)
            self.source_tags_editor.literal_validator = self._valid_choice
            source_layout.insertWidget(0, self.source_tags_editor)
            self.template_edit_toggle = QPushButton("Edit Templates…")
            self.template_edit_toggle.setCheckable(True)
            source_layout.insertWidget(1, self.template_edit_toggle, 0, Qt.AlignLeft)
            def toggle_templates(visible):
                self.source_tags_editor.setVisible(not visible)
                wildcard_visible = visible and bool(self.template_variables) and self.rule_type.currentText() != "Always ignore SDXL Tag"
                self.wildcard_panel.setVisible(wildcard_visible)
                self.wildcard_label.setVisible(wildcard_visible)
                for control in (self.source_choice, add_source, self.sources, remove_source):
                    control.setVisible(visible)
            self.template_edit_toggle.toggled.connect(toggle_templates)
            toggle_templates(False)
            self.source_tags_editor.changed.connect(self._set_rule_source_tags)
            for signal in (self.sources.model().rowsInserted, self.sources.model().rowsRemoved, self.sources.model().dataChanged):
                signal.connect(self._sync_rule_source_tags)
            self._sync_rule_source_tags()
            self.template_edit_toggle.setChecked(any("{" in value for value in sources))

    def _set_rule_source_tags(self, values):
        if self._syncing_rule_tags:
            return
        self._syncing_rule_tags = True
        try:
            self.sources.clear()
            for value in values:
                self.sources.addTopLevelItem(QTreeWidgetItem((value,)))
        finally:
            self._syncing_rule_tags = False

    def _sync_rule_source_tags(self, *_args):
        if self._syncing_rule_tags:
            return
        self.source_tags_editor.set_values(
            self.sources.topLevelItem(index).text(0) for index in range(self.sources.topLevelItemCount())
        )

    def _choice(self,combo):
        return self._valid_choice(combo.currentText().strip())

    def _valid_choice(self,value):
        match=next((item for item in self.known_choices if item.casefold()==value.casefold()),"")
        if match: return match
        matches=self._placeholder.findall(value); placeholders={name.casefold() for name in matches}
        braces_are_valid=value.count("{")==value.count("}")==len(matches)
        if value and placeholders and braces_are_valid and placeholders<=self.template_variables: return value
        return ""

    def _add_source(self):
        value=self._choice(self.source_choice)
        if not value:
            QMessageBox.information(self,"Choose a known SDXL Tag or template","Select a known System Knowledge SDXL Tag, or use a defined reusable value in braces, such as {color} shirt."); return
        if value.casefold() not in {self.sources.topLevelItem(i).text(0).casefold() for i in range(self.sources.topLevelItemCount())}:
            self.sources.addTopLevelItem(QTreeWidgetItem((value,)))
        self.source_choice.setCurrentText("")

    def _remove_sources(self):
        for item in self.sources.selectedItems(): self.sources.takeTopLevelItem(self.sources.indexOfTopLevelItem(item))

    def _generalize(self,value):
        token=self.wildcard_choice.currentText().strip()
        if not token: return value
        name=token[1:-1].casefold()
        for recognized in sorted(self.variable_values.get(name,()),key=len,reverse=True):
            updated,count=re.subn(rf"(?<!\w){re.escape(recognized)}(?!\w)",token,value,count=1,flags=re.IGNORECASE)
            if count: return updated
        return f"{token} {value}".strip()

    def _wildcard_source(self):
        selected=self.sources.selectedItems()
        if selected: selected[0].setText(0,self._generalize(selected[0].text(0)))
        else: self.source_choice.setCurrentText(self._generalize(self.source_choice.currentText().strip()))

    def _wildcard_target(self):
        self.target.setCurrentText(self._generalize(self.target.currentText().strip()))

    def _type_changed(self,value):
        ignore=value=="Always ignore SDXL Tag"
        self.target.setVisible(not ignore); self.target_label.setVisible(not ignore)
        template_mode = not hasattr(self, "template_edit_toggle") or self.template_edit_toggle.isChecked()
        wildcard_visible = not ignore and bool(self.template_variables) and template_mode
        self.wildcard_panel.setVisible(wildcard_visible)
        self.wildcard_label.setVisible(wildcard_visible)
        self.sources_label.setText("Incoming SDXL Tag to reject" if ignore else "SDXL Tags that appear together")
        self.behavior_text.setText(
            "Incoming-evidence rejection: this exact SDXL Tag is suppressed when TXT, Metadata, or AI offers it again. Existing confirmed Image Knowledge is never removed."
            if ignore else
            "Training projection: when all matching SDXL Tags are present, exported training data may use the concise result. Complete Image Knowledge and incoming findings keep every valid fact."
        )

    def values(self):
        sources=[self.sources.topLevelItem(i).text(0) for i in range(self.sources.topLevelItemCount())]
        return self.rule_type.currentText(),sources,"" if self.rule_type.currentText()=="Always ignore SDXL Tag" else self._choice(self.target)

    def configure_missing_tags(self, names, create_tag, tag_saved):
        self._missing_tag_names = names
        self._create_missing_tag = create_tag
        self._missing_tag_saved = tag_saved
        self.rule_type.currentTextChanged.connect(self.refresh_missing_tags)
        self.target.currentTextChanged.connect(self.refresh_missing_tags)
        for signal in (self.sources.model().rowsInserted,
                       self.sources.model().rowsRemoved,
                       self.sources.model().dataChanged):
            signal.connect(self.refresh_missing_tags)
        self.refresh_missing_tags()

    def refresh_missing_tags(self, *_args):
        if self._missing_tag_names is None:
            return
        names = tuple(self._missing_tag_names())
        selected = self.missing_tag_choice.currentText()
        self.missing_tag_choice.clear()
        self.missing_tag_choice.addItems(names)
        if selected in names:
            self.missing_tag_choice.setCurrentText(selected)
        self.missing_tag_panel.setVisible(bool(names) or bool(self._missing_tag_pending))
        self.create_missing_tag_button.setEnabled(bool(names) and not self._missing_tag_pending)
        if not self._missing_tag_pending:
            self.missing_tag_status.setText(
                "Create each missing Tag separately. A Tag saves in System Knowledge; "
                "this rule remains a draft until Save Automatic Rule."
                if names else ""
            )

    def _request_missing_tag(self):
        if self._missing_tag_pending or self._missing_tag_names is None:
            return
        name = self.missing_tag_choice.currentText().strip()
        if name not in self._missing_tag_names():
            self.refresh_missing_tags()
            return
        self._missing_tag_pending = name
        selected_type, sources, result = self.values()
        self._missing_tag_request_values = (selected_type, tuple(sources), result)
        self.create_missing_tag_button.setEnabled(False)
        self.missing_tag_status.setText(
            f"Opening Tag Details for {name}. The rule draft stays here and is not saved."
        )
        if not self._create_missing_tag(name, self):
            self.rule_tag_creation_cancelled("The Tag editor could not open. Try again.")

    def rule_tag_created(self, record):
        if not self._missing_tag_pending:
            return
        requested = self._missing_tag_pending
        self._missing_tag_pending = ""
        requested_values = self._missing_tag_request_values
        self._missing_tag_request_values = None
        try:
            self._missing_tag_saved(record)
        except (OSError,RuntimeError,ValueError) as error:
            self.missing_tag_status.setText(
                f"Tag saved, but the Rule editor could not refresh System Knowledge: {error}. "
                "Keep this draft open and retry after restoring Tag access."
            )
            self.create_missing_tag_button.setEnabled(True)
            return
        self.refresh_missing_tags()
        remaining = tuple(self._missing_tag_names())
        selected_type, sources, result = self.values()
        current_values = (selected_type, tuple(sources), result)
        if requested in remaining:
            message = (
                f"Tag saved independently. {requested} still needs an exact matching Tag; "
                "create it or adjust this draft before saving the rule."
            )
        elif requested_values != current_values:
            message = (
                "Tag saved independently. The Rule draft changed while Tag Details was open; "
                "review it before Save Automatic Rule."
            )
        else:
            message = (
                "Tag saved independently. Review the current Rule draft, then choose "
                "Save Automatic Rule."
            )
        self.missing_tag_status.setText(message)

    def rule_tag_creation_cancelled(self, message="Tag creation cancelled. The rule draft is unchanged."):
        if not self._missing_tag_pending:
            return
        self._missing_tag_pending = ""
        self._missing_tag_request_values = None
        self.refresh_missing_tags()
        self.missing_tag_status.setText(message)

    def accept(self):
        if self._saving or self._saved_once:return
        rule_type,sources,target=self.values()
        minimum=1 if rule_type=="Always ignore SDXL Tag" else 2
        if len(sources)<minimum or (rule_type!="Always ignore SDXL Tag" and not target):
            QMessageBox.warning(self,"Incomplete rule",f"Select at least {minimum} matching SDXL Tag{'s' if minimum>1 else ''}"+(" and a result SDXL Tag." if rule_type!="Always ignore SDXL Tag" else ".")); return
        if rule_type=="Always ignore SDXL Tag" and len(sources)!=1:
            QMessageBox.warning(self,"Choose one SDXL Tag","An always-ignore rule must contain exactly one SDXL Tag."); return
        if self._save_callback is not None:
            self._saving=True
            self.save_button.setEnabled(False)
            try:
                if not self._save_callback(rule_type,sources,target):return
                self._saved_once=True
            finally:
                self._saving=False
                self.save_button.setEnabled(True)
        super().accept()


class RuleIntentMigrationDialog(FeedbackEnabledDialog):
    feedback_workflow = "Automation rule migration"

    """Review legacy rule meanings without reading or changing image records."""

    def __init__(self,service,candidates,parent=None):
        super().__init__(parent); self.service=service; self.candidates=tuple(candidates); self._combos={}
        self.setWindowTitle("Review Legacy Rule Intent"); self.resize(940,660)
        layout=QVBoxLayout(self)
        intro=QLabel(
            "Older rules did not record whether they were meant to reject noisy evidence, "
            "request an image correction, or shape an exported training caption. Review the "
            "meaning below before migrating. Confirmed Image Knowledge is never changed."
        )
        intro.setWordWrap(True); layout.addWidget(intro)
        self.rules=QTreeWidget(); self.rules.setHeaderLabels(("Use","Stored rule","Meaning after migration","Storage"))
        self.rules.setRootIsDecorated(False); self.rules.setAlternatingRowColors(False)
        self.rules.header().setSectionResizeMode(0,QHeaderView.ResizeToContents)
        self.rules.header().setSectionResizeMode(1,QHeaderView.Stretch)
        self.rules.header().setSectionResizeMode(2,QHeaderView.ResizeToContents)
        self.rules.header().setSectionResizeMode(3,QHeaderView.ResizeToContents)
        for candidate in self.candidates:
            item=QTreeWidgetItem(("",candidate.rule_summary,"",candidate.storage_label))
            item.setFlags(item.flags()|Qt.ItemIsUserCheckable); item.setCheckState(0,Qt.Checked)
            item.setData(0,Qt.UserRole,candidate.id); self.rules.addTopLevelItem(item)
            combo=QComboBox()
            for intent in candidate.allowed_intents: combo.addItem(RULE_INTENT_LABELS[intent],intent)
            combo.setCurrentIndex(combo.findData(candidate.suggested_intent))
            combo.currentIndexChanged.connect(lambda _index,rule_id=candidate.id:self._intent_changed(rule_id))
            self.rules.setItemWidget(item,2,combo); self._combos[candidate.id]=combo
        self.rules.currentItemChanged.connect(self._selected_changed); layout.addWidget(self.rules,1)
        heading=QLabel("Before and after example"); heading.setObjectName("sectionTitle"); layout.addWidget(heading)
        hint=QLabel("This example uses the SDXL Tags stored in the rule. It does not open or modify an image.")
        hint.setObjectName("mutedLabel"); hint.setWordWrap(True); layout.addWidget(hint)
        self.preview=QTextEdit(); self.preview.setReadOnly(True); self.preview.setMinimumHeight(190); layout.addWidget(self.preview)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.apply_button=buttons.addButton("Migrate Checked Rules",QDialogButtonBox.AcceptRole); self.apply_button.setProperty("primary",True)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        if self.rules.topLevelItemCount(): self.rules.setCurrentItem(self.rules.topLevelItem(0))

    @staticmethod
    def _values(values):
        return ", ".join(values) if values else "None"

    def _candidate(self,rule_id):
        return next((candidate for candidate in self.candidates if candidate.id==rule_id),None)

    def _intent_changed(self,rule_id):
        item=self.rules.currentItem()
        if item is not None and item.data(0,Qt.UserRole)==rule_id:self._show_preview(item)

    def _selected_changed(self,item,*_):
        if item is not None:self._show_preview(item)

    def _show_preview(self,item):
        rule_id=item.data(0,Qt.UserRole); candidate=self._candidate(rule_id)
        if candidate is None:return
        intent=self._combos[rule_id].currentData(); preview=self.service.preview(candidate,intent)
        self.preview.setPlainText(
            f"Confirmed Image Knowledge\n"
            f"Before: {self._values(preview.image_knowledge_before)}\n"
            f"After:  {self._values(preview.image_knowledge_after)}\n\n"
            f"Training projection\n"
            f"Before: {self._values(preview.projection_before)}\n"
            f"After:  {self._values(preview.projection_after)}\n\n"
            f"Incoming evidence\n"
            f"Before: {self._values(preview.incoming_evidence_before)}\n"
            f"After:  {self._values(preview.incoming_evidence_after)}\n\n"
            f"{preview.explanation}"
        )

    def selections(self):
        result={}
        for index in range(self.rules.topLevelItemCount()):
            item=self.rules.topLevelItem(index)
            if item.checkState(0)==Qt.Checked:
                rule_id=item.data(0,Qt.UserRole); result[rule_id]=self._combos[rule_id].currentData()
        return result

    def accept(self):
        if not self.selections():
            QMessageBox.information(self,"Choose rules to migrate","Check at least one legacy rule, or choose Cancel."); return
        super().accept()


class ProjectionRuleRepairDialog(FeedbackEnabledDialog):
    feedback_workflow = "Projection rule repair"

    """Preview exact prerequisite repairs without opening image records."""

    def __init__(self,candidates,parent=None):
        super().__init__(parent); self.candidates=tuple(candidates)
        self.setWindowTitle("Review Related Projection Rules"); self.resize(980,650)
        layout=QVBoxLayout(self)
        intro=QLabel(
            "Some training-projection rules repeat a prerequisite that an earlier "
            "rule already reduces. The checked repairs remove only prerequisites "
            "proven redundant by an exact active rule. Confirmed Image Knowledge "
            "and image-facing files are never changed."
        )
        intro.setWordWrap(True); layout.addWidget(intro)
        self.rules=QTreeWidget()
        self.rules.setHeaderLabels(("Use","Stored rule","After repair","Storage"))
        self.rules.setRootIsDecorated(False); self.rules.setAlternatingRowColors(False)
        self.rules.header().setSectionResizeMode(0,QHeaderView.ResizeToContents)
        self.rules.header().setSectionResizeMode(1,QHeaderView.Stretch)
        self.rules.header().setSectionResizeMode(2,QHeaderView.Stretch)
        self.rules.header().setSectionResizeMode(3,QHeaderView.ResizeToContents)
        for candidate in self.candidates:
            item=QTreeWidgetItem((
                "",candidate.before_summary,candidate.after_summary,
                candidate.storage_label,
            ))
            item.setFlags(item.flags()|Qt.ItemIsUserCheckable)
            item.setCheckState(0,Qt.Checked)
            item.setData(0,Qt.UserRole,candidate.id)
            self.rules.addTopLevelItem(item)
        self.rules.currentItemChanged.connect(self._selected_changed)
        layout.addWidget(self.rules,1)
        heading=QLabel("Why this is safe"); heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        self.detail=QTextEdit(); self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(150); layout.addWidget(self.detail)
        recovery=QLabel(
            "Applying creates verified backups and an exact restore point. The "
            "repair updates rule definitions only."
        )
        recovery.setWordWrap(True); recovery.setObjectName("mutedLabel")
        layout.addWidget(recovery)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        apply_button=buttons.addButton(
            "Repair Checked Rules",QDialogButtonBox.AcceptRole
        )
        apply_button.setProperty("primary",True)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        if self.rules.topLevelItemCount():
            self.rules.setCurrentItem(self.rules.topLevelItem(0))

    def _candidate(self,candidate_id):
        return next(
            (
                candidate for candidate in self.candidates
                if candidate.id==candidate_id
            ),
            None,
        )

    def _selected_changed(self,item,*_):
        candidate=(
            self._candidate(item.data(0,Qt.UserRole))
            if item is not None else None
        )
        self.detail.setPlainText(
            (
                f"Before\n{candidate.before_summary}\n\n"
                f"After\n{candidate.after_summary}\n\n"
                f"{candidate.explanation}"
            )
            if candidate is not None else ""
        )

    def selected_ids(self):
        return tuple(
            self.rules.topLevelItem(index).data(0,Qt.UserRole)
            for index in range(self.rules.topLevelItemCount())
            if self.rules.topLevelItem(index).checkState(0)==Qt.Checked
        )

    def accept(self):
        if not self.selected_ids():
            QMessageBox.information(
                self,"Choose rules to repair",
                "Check at least one related rule, or choose Cancel.",
            )
            return
        super().accept()


class AutomationPage(QWidget):
    refresh_requested=Signal(); edit_image_data=Signal(str); knowledge_changed=Signal()
    background_work_finished=Signal(); behavior_changed=Signal()
    evidence_policy_changed=Signal(bool)
    organization_policy_changed=Signal(dict)
    ai_setup_requested=Signal(str)

    def __init__(
        self,state,parent=None,*,defer_expensive_initialization: bool=False,
        create_rule_tag=None,
    ):
        super().__init__(parent); self.state=state; self.pool=QThreadPool.globalInstance(); self._task=None
        self._create_rule_tag = create_rule_tag
        self._defer_expensive_initialization=bool(defer_expensive_initialization)
        self._capability_task=None; self._behavior_capability_result=None
        self._conflict_projection_task=None; self._conflict_projection=None
        self._conflict_projection_generation=0; self._conflict_projection_retries=0
        self._conflict_initial_paint_done=False
        self._pending_conflict_editor_open=False
        self._conflict_focus_tag_id=""; self._conflict_focus_relationship_id=""
        self.repository=AutomationRuleRepository(
            DEFAULTS_DIR/"automation_rules.json", USER_DATA_DIR/"automation_rules.json",
            rule_pair_admission=RulePairAdmission(USER_DATA_DIR),
        )
        self.intent_migration=RuleIntentMigrationService(self.repository,self.state.knowledge)
        self._intent_migration_recovery_notice=""
        self._intent_migration_recovery_error=""
        try:
            recovered=self.intent_migration.recover_interrupted()
            if recovered:
                self._intent_migration_recovery_notice=(
                    "Recovered an interrupted legacy-rule migration before opening Automation. "
                    "The exact pre-migration rules were restored; Image Knowledge was unchanged."
                )
        except (OSError,RuntimeError,ValueError) as exc:
            self._intent_migration_recovery_error=str(exc)
        self.value_binding_categories=CategoryRegistry(
            DEFAULTS_DIR/"categories.json",CATEGORIES_PATH,
        )
        self.value_binding_migration=ReusableValueBindingMigrationService(
            self.repository,self.state.knowledge,self.value_binding_categories,
            journal_path=USER_DATA_DIR/"operation_journal.jsonl",
        )
        self._value_binding_recovery_notice=""
        self._value_binding_recovery_error=""
        try:
            recovered=self.value_binding_migration.recover_interrupted()
            if recovered:
                self._value_binding_recovery_notice=(
                    "Recovered an interrupted reusable-value authority save. "
                    "The exact pre-decision Automation payload was restored."
                )
        except (OSError,RuntimeError,ValueError) as exc:
            self._value_binding_recovery_error=str(exc)
        self.projection_repairs=ProjectionRuleRepairService(
            self.repository,self.state.knowledge
        )
        self.template_organization_operations=TagTemplateOrganizationApplyService(
            tags_path=self.state.knowledge.path,
            rule_pair_admission=self.state.knowledge.rule_pair_admission,
            category_default_path=DEFAULTS_DIR/"categories.json",
            category_user_path=CATEGORIES_PATH,
            modifier_path=DEFAULTS_DIR/"modifiers.json",
            concept_path=DEFAULTS_DIR/"concepts.json",
            default_relationship_path=DEFAULTS_DIR/"relationships.json",
            user_relationship_path=RELATIONSHIPS_PATH,
            journal_path=USER_DATA_DIR/"operation_journal.jsonl",
        )
        self.template_generation_operations=TagTemplateGenerationService(
            self.template_organization_operations,
            USER_DATA_DIR/"operation_journal.jsonl",
        )
        self._template_recovery_notice=""; self._template_recovery_error=""
        try:
            recovered=self.template_generation_operations.recover_interrupted()
            if recovered:
                self._template_recovery_notice=(
                    "Recovered interrupted reusable Tag operation history before opening Automation. Current System Knowledge was verified."
                )
        except Exception as exc:
            self._template_recovery_error=str(exc)
        self._template_plan_action="organize"
        self.checkpoint=AutomationCheckpointStore(USER_DATA_DIR/"automation_checkpoint.json")
        self._rule_task_context=""
        self._shutdown_requested=False
        self._fact_expansion_syncing=False
        self._external_write_syncing=False
        self._behavior_syncing=False
        self._behavior_saved_policy=None
        self._behavior_preview_policy=None
        self._evidence_intake_saved_policy=None
        self._export_content_saved_policy=None
        self._library_check_saved_policy=None
        self._organization_saved_signature=None
        layout=QVBoxLayout(self); layout.setContentsMargins(28,24,28,24); layout.setSpacing(12)
        self.header=PageHeader(
            "Rules & Automation","",
            workspace_help("automation"),
        ); layout.addWidget(self.header)
        self.resolution_policy_button=QPushButton("Resolution Policy…")
        self.resolution_policy_button.setToolTip("Review preferred pixel dimensions and independent, off-by-default resizing permissions.")
        self.resolution_policy_button.clicked.connect(self._open_resolution_policy)
        layout.addWidget(self.resolution_policy_button)
        self.tabs=QTabWidget()
        self.settings_navigation=SettingsNavigator(accessible_name="Rules and Automation")
        self.settings_navigation.set_content(self.tabs)
        layout.addWidget(self.settings_navigation,1)
        self._build_rules_tab(); self._build_values_tab(); self._build_fact_expansion_tab(); self.refresh(reload=False)
        self._build_settings_navigation()
        self.settings_navigation.select_section("rules")
        if self._defer_expensive_initialization:
            QTimer.singleShot(0,self._start_behavior_capability_check)

    def _open_resolution_policy(self):
        from .resolution_dialog import open_resolution_workspace
        open_resolution_workspace(self)

    def _build_settings_navigation(self):
        navigation=self.settings_navigation
        self.tabs.tabBar().hide()
        self.policy_sections.tabBar().hide()
        self.behavior_detail_tabs.tabBar().hide()
        destinations=(
            ("rules","Rules",None,None,"conflict merge ignore matching"),
            ("reusable","Reusable Tags",None,None,"templates wildcard knowledge packs"),
            ("prepare","Prepare Images","ai",2,"fingerprints reuse months evidence confirmation"),
            ("ai","AI Analysis Defaults","ai",0,"contextual tags descriptions ignored sentences presets training generation"),
            ("after_checks","After Library Checks","ai",1,"automatic generate votes consensus"),
            ("thresholds","Tag Suggestions","ai",3,"confidence rating character threshold"),
            ("libraries","Library Checks & Evidence","libraries",None,"startup subfolders TXT Metadata import"),
            ("organization","Organization","organization",None,"folder filename routes containers"),
            ("related_tags","Related Tags","related_tags",None,"expansion relationships depth"),
            ("exports","Export Content","exports",None,"dataset export content rating"),
            ("protection","File Protection","protection",None,"backup originals external writes destination"),
        )
        def activate(key,policy,detail):
            if key in {"rules","reusable"}:
                self.tabs.setCurrentWidget(self.rules_tab if key=="rules" else self.values_tab)
            else:
                self.tabs.setCurrentWidget(self.fact_expansion_tab)
                self.policy_sections.setCurrentIndex(self.policy_section_indexes[policy])
                if detail is not None:
                    self.behavior_detail_tabs.setCurrentIndex(detail)
        for key,label,policy,detail,keywords in destinations:
            navigation.add_section(key,label,
                lambda k=key,p=policy,d=detail:activate(k,p,d),keywords=(keywords,))
        targets=(
            ("rules","Merge and ignore rules","rule_search"),
            ("rules","Conflict rules","conflict_search"),
            ("reusable","Reviewed Tag templates","template_tree"),
            ("reusable","Legacy wildcard lists","value_tree"),
            ("reusable","Import Knowledge Pack","import_knowledge_pack_button"),
            ("reusable","Export Knowledge Pack","export_knowledge_pack_button"),
            ("ai","Offer AI Tags","behavior_use_wd"),
            ("ai","Offer AI Descriptions","behavior_use_joy"),
            ("ai","Ignored Descriptions everywhere","description_exclusions_button"),
            ("ai","Image analysis preset","behavior_preset"),
            ("prepare","Read external TXT and Metadata during Prepare","behavior_prepare_read_external"),
            ("prepare","Create duplicate fingerprints","behavior_prepare_fingerprints"),
            ("prepare","Reuse matching AI results amount","behavior_prepare_reuse_value"),
            ("prepare","Reuse duration Hours Days Months","behavior_prepare_reuse_unit"),
            ("prepare","Confirm safe Tags during Prepare","behavior_prepare_confirm"),
            ("prepare","Fill or upgrade Descriptions","behavior_prepare_descriptions"),
            ("prepare","Preparation confidence preset","behavior_prepare_preset"),
            ("prepare","Minimum preparation Tag votes","behavior_prepare_votes"),
            ("prepare","TXT Metadata corroborating vote","behavior_prepare_external_vote"),
            ("after_checks","Analyze Tags after library checks","behavior_auto_wd"),
            ("after_checks","Analyze Descriptions after library checks","behavior_auto_joy"),
            ("after_checks","Confirm safe Tags automatically","behavior_auto_confirm"),
            ("after_checks","Minimum consensus votes","behavior_consensus"),
            ("thresholds","General Tag confidence threshold","behavior_general_threshold"),
            ("thresholds","Character Tag confidence threshold","behavior_character_threshold"),
            ("thresholds","Rating Tag confidence threshold","behavior_rating_threshold"),
            ("libraries","Check libraries at startup","library_check_startup"),
            ("libraries","Include subfolders","library_check_subfolders"),
            ("libraries","Import changed TXT and Metadata during library checks","evidence_intake_auto"),
            ("organization","Filename naming policy","organization_naming"),
            ("related_tags","Enable related Tag suggestions","fact_expansion_enabled"),
            ("related_tags","Related Tag expansion depth","fact_expansion_depth"),
            ("exports","Maximum export content rating","export_content_rating"),
            ("protection","Back up originals before external writes","external_write_backup_mode"),
        )
        for key,label,attribute in targets:
            control=getattr(self,attribute,None)
            if control is not None:
                navigation.add_target(key,label,control)
        for index,control in enumerate(self.organization_general_levels,1):
            navigation.add_target("organization",f"Normal image folder route level {index}",control)
        for index,control in enumerate(self.organization_known_levels,1):
            navigation.add_target("organization",f"Known character folder route level {index}",control)
        navigation.add_target("organization","Known character hierarchy",self.organization_known_route)
        navigation.add_target("organization","Use fallback folder",self.organization_fallback_enabled)
        navigation.add_target("organization","Fallback folder name",self.organization_fallback)
        for key,(check,entry) in self.organization_container_controls.items():
            navigation.add_target("organization",f"{LEVEL_LABELS[key]} category container",check)
            navigation.add_target("organization",f"{LEVEL_LABELS[key]} container folder name",entry)
        for control in self.fact_expansion_relationship_checks.values():
            navigation.add_target("related_tags",control.text(),control)
        navigation.add_target("protection","Backup destination folder",self.choose_external_write_backup)
        self.tabs.currentChanged.connect(self._sync_settings_navigation)
        self.policy_sections.currentChanged.connect(self._sync_settings_navigation)
        self.behavior_detail_tabs.currentChanged.connect(self._sync_settings_navigation)

    def _sync_settings_navigation(self,*_args):
        if not hasattr(self,"settings_navigation"):return
        current=self.tabs.currentWidget()
        if current is self.rules_tab:key="rules"
        elif current is self.values_tab:key="reusable"
        else:
            index=self.policy_sections.currentIndex()
            key=next((k for k,v in self.policy_section_indexes.items() if v==index),"ai")
            if key=="ai":
                key=("ai","after_checks","prepare","thresholds")[self.behavior_detail_tabs.currentIndex()]
        self.settings_navigation.sync_section(key)

    def _open_description_exclusions(self):
        from .description_exclusions_dialog import DescriptionExclusionsDialog
        dialog=DescriptionExclusionsDialog(self.state.settings,self)
        dialog.exec()
        self._refresh_description_exclusions_summary()

    def _refresh_description_exclusions_summary(self):
        if not hasattr(self,"description_exclusions_summary"):return
        count=len(getattr(self.state.settings,"ignored_plain_language_sentences",()))
        self.description_exclusions_summary.setText(
            f"{count:,} descriptions ignored everywhere. Restore any you want to review again."
            if count else "No descriptions are ignored everywhere. Image-specific ignores remain in each image editor."
        )

    def import_knowledge_pack(self):
        from .knowledge_pack_actions import import_pack
        import_pack(self)

    def export_knowledge_pack(self):
        from .knowledge_pack_actions import export_pack
        export_pack(self)

    def _build_fact_expansion_tab(self):
        page=QWidget(); self.fact_expansion_tab=page; page_layout=QVBoxLayout(page)
        page_layout.setContentsMargins(0,0,0,0)
        ownership=QLabel(
            "Choose the reusable behavior you want to change. Each section keeps "
            "its own stated save boundary; opening a section never starts work."
        )
        ownership.setWordWrap(True); ownership.setObjectName("mutedLabel")
        page_layout.addWidget(ownership)
        self.policy_sections=QTabWidget()
        self.policy_sections.setAccessibleName("Reusable behavior policy sections")
        page_layout.addWidget(self.policy_sections,1)
        self.policy_section_indexes={}; self.policy_scrolls={}

        def add_section(key,label,builders,*,scrollable=True):
            section=QWidget(); section_layout=QVBoxLayout(section)
            section_layout.setContentsMargins(0,0,0,0)
            if not scrollable:
                for builder in builders:
                    builder(section_layout)
                self.policy_section_indexes[key]=self.policy_sections.addTab(section,label)
                return self.policy_scrolls[key]
            scroll=QScrollArea(); scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            content=QWidget(); content_layout=QVBoxLayout(content)
            content_layout.setContentsMargins(8,12,8,12)
            content_layout.setSpacing(14)
            for builder in builders:
                builder(content_layout)
            content_layout.addStretch(1)
            scroll.setWidget(content); section_layout.addWidget(scroll)
            guard_scrollable_value_controls(content)
            self.policy_scrolls[key]=scroll
            self.policy_section_indexes[key]=self.policy_sections.addTab(
                section,label,
            )
            return scroll

        self.automation_settings_scroll=add_section(
            "ai","AI && Prepare",(self._build_behavior_settings,),scrollable=False,
        )
        add_section(
            "libraries","Libraries && Evidence",
            (self._build_library_check_settings,self._build_evidence_intake_settings),
        )
        add_section("exports","Export Content",(self._build_export_content_settings,))
        add_section("protection","File Protection",(self._build_external_write_settings,))
        add_section(
            "organization","Organization",
            (self._build_organization_settings,),scrollable=False,
        )
        add_section(
            "related_tags","Related Tags",(self._build_fact_expansion_settings,),
        )
        self.tabs.addTab(page,"Behavior && Policies")
        self._refresh_behavior_settings()
        self._refresh_library_check_settings()
        self._refresh_evidence_intake_settings()
        self._refresh_export_content_settings()
        self._refresh_organization_settings()
        self._refresh_fact_expansion_settings()
        self._refresh_external_write_settings()

    def _add_policy_detail(self,tabs,key,label):
        """Scroll only the editor, leaving its policy save row outside."""
        scroll=QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QFrame.NoFrame)
        content=QWidget(); body=QVBoxLayout(content)
        body.setContentsMargins(8,8,8,8); body.setSpacing(12)
        scroll.setWidget(content); tabs.addTab(scroll,label)
        self.policy_scrolls[key]=scroll
        return body

    def _build_fact_expansion_settings(self,layout):
        policy=DeterministicFactExpansionPolicy.from_settings(
            getattr(self.state,"settings",None)
        )
        heading_row=QHBoxLayout(); heading=QLabel("Related Tag Suggestions")
        heading.setObjectName("sectionTitle"); heading_row.addWidget(heading); heading_row.addStretch(1)
        self.fact_expansion_help=ContextHelpButton(
            "Related Tag Suggestions",
            """## What this does

The image editor can suggest Tags connected through relationships you have reviewed. Suggestions are based on saved relationships, not similar wording.

## Before anything changes

You review the complete suggestion first, then choose whether to add it. Saving the editor remains a separate step. TXT, Metadata, captions, and image files are not changed by this setting.""",
        )
        heading_row.addWidget(self.fact_expansion_help); layout.addLayout(heading_row)
        intro=QLabel(
            "Suggest related Tags in the image editor using relationships you have "
            "reviewed. Every suggestion is previewed before it is added."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel"); layout.addWidget(intro)
        identity=QLabel(f"Policy: {policy.display_name}")
        identity.setObjectName("mutedLabel"); layout.addWidget(identity)
        self.fact_expansion_enabled=QCheckBox(
            "Enable Add Safe Related Tags in image editors"
        )
        self.fact_expansion_enabled.setToolTip(
            "Show the exact safe-add preview only when this policy has eligible reviewed relationships."
        )
        self.fact_expansion_enabled.toggled.connect(
            self._save_fact_expansion_settings
        )
        layout.addWidget(self.fact_expansion_enabled)
        relationships=QGroupBox("Reviewed relationship types")
        relationships_layout=QVBoxLayout(relationships)
        relationship_hint=QLabel(
            "Every step in a suggested path must use one of the selected reviewed relationship types."
        )
        relationship_hint.setWordWrap(True)
        relationship_hint.setObjectName("mutedLabel")
        relationships_layout.addWidget(relationship_hint)
        self.fact_expansion_relationship_checks={}
        for label,kinds in FACT_EXPANSION_RELATIONSHIP_GROUPS:
            checkbox=QCheckBox(label)
            checkbox.setProperty("relationshipKinds",tuple(kinds))
            checkbox.toggled.connect(self._save_fact_expansion_settings)
            relationships_layout.addWidget(checkbox)
            self.fact_expansion_relationship_checks[tuple(kinds)]=checkbox
        layout.addWidget(relationships)
        depth_row=QHBoxLayout()
        depth_row.addWidget(QLabel("Relationship levels"))
        self.fact_expansion_depth=QSpinBox()
        self.fact_expansion_depth.setRange(
            FACT_EXPANSION_MIN_DEPTH,FACT_EXPANSION_MAX_DEPTH
        )
        self.fact_expansion_depth.setToolTip(
            "Depth 1 includes direct reviewed relationships only. Higher values allow bounded transitive paths."
        )
        self.fact_expansion_depth.valueChanged.connect(
            self._save_fact_expansion_settings
        )
        depth_row.addWidget(self.fact_expansion_depth)
        depth_row.addStretch(1); layout.addLayout(depth_row)
        self.fact_expansion_status=QLabel()
        self.fact_expansion_status.setWordWrap(True)
        self.fact_expansion_status.setObjectName("mutedLabel")
        layout.addWidget(self.fact_expansion_status)

    def _build_external_write_settings(self,layout):
        protection=QGroupBox("Protect files changed by external writes")
        protection_layout=QVBoxLayout(protection)
        protection_intro=QLabel(
            "Contextual workflows consume this one shared policy before replacing "
            "TXT, Metadata, or another external file. Training TXT is the first "
            "consumer; each creating workflow still previews exact targets and "
            "owns restore."
        )
        protection_intro.setWordWrap(True)
        protection_intro.setObjectName("mutedLabel")
        protection_layout.addWidget(protection_intro)
        self.external_write_save_note=QLabel(
            "Changing the backup choice saves this policy immediately. It does not "
            "rewrite existing files; each later write has its own preview."
        )
        self.external_write_save_note.setWordWrap(True)
        self.external_write_save_note.setObjectName("mutedLabel")
        protection_layout.addWidget(self.external_write_save_note)
        mode_row=QHBoxLayout()
        mode_row.addWidget(QLabel("Back up originals before external writes"))
        self.external_write_backup_mode=QComboBox()
        self.external_write_backup_mode.addItem(
            "Always (recommended)",EXTERNAL_WRITE_BACKUP_ALWAYS,
        )
        self.external_write_backup_mode.addItem(
            "Ask for each operation",EXTERNAL_WRITE_BACKUP_ASK,
        )
        self.external_write_backup_mode.addItem(
            "Off",EXTERNAL_WRITE_BACKUP_OFF,
        )
        self.external_write_backup_mode.currentIndexChanged.connect(
            self._save_external_write_mode
        )
        mode_row.addWidget(self.external_write_backup_mode)
        mode_row.addStretch(1)
        protection_layout.addLayout(mode_row)
        folder_row=QHBoxLayout()
        self.external_write_backup_directory=QLabel()
        self.external_write_backup_directory.setWordWrap(True)
        self.external_write_backup_directory.setObjectName("mutedLabel")
        folder_row.addWidget(self.external_write_backup_directory,1)
        self.choose_external_write_backup=QPushButton("Setup Folders…")
        self.choose_external_write_backup.clicked.connect(
            self._choose_external_write_backup_directory
        )
        folder_row.addWidget(self.choose_external_write_backup)
        self.clear_external_write_backup=QPushButton("Clear")
        self.clear_external_write_backup.clicked.connect(
            self._clear_external_write_backup_directory
        )
        self.clear_external_write_backup.hide()
        protection_layout.addLayout(folder_row)
        self.external_write_backup_status=QLabel()
        self.external_write_backup_status.setWordWrap(True)
        self.external_write_backup_status.setObjectName("mutedLabel")
        protection_layout.addWidget(self.external_write_backup_status)
        layout.addWidget(protection)

    def _build_export_content_settings(self,layout):
        group=QGroupBox("Dataset export content")
        self.export_content_group=group
        group_layout=QVBoxLayout(group)
        note=QLabel(
            "This reusable safety boundary limits the maximum confirmed image "
            "rating included by Dataset projections and exports. Saving it does "
            "not create, rewrite, or remove any export."
        )
        note.setWordWrap(True); note.setObjectName("mutedLabel")
        group_layout.addWidget(note)
        row=QHBoxLayout(); row.addWidget(QLabel("Maximum rating included"))
        self.export_content_rating=QComboBox()
        for rating,label in (
            ("general","General only"),("sensitive","Up to sensitive"),
            ("questionable","Up to questionable"),("explicit","All ratings"),
        ):
            self.export_content_rating.addItem(label,rating)
        row.addWidget(self.export_content_rating,1); group_layout.addLayout(row)
        consequence=QLabel(
            "Dataset still owns the exact selected scope, preview, explicit "
            "confirmation, result, and recovery for every outward write."
        )
        consequence.setWordWrap(True); consequence.setObjectName("mutedLabel")
        group_layout.addWidget(consequence)
        self.export_content_status=QLabel()
        self.export_content_status.setWordWrap(True)
        self.export_content_status.setObjectName("mutedLabel")
        group_layout.addWidget(self.export_content_status)
        actions=QHBoxLayout(); actions.addStretch(1)
        self.save_export_content_policy=QPushButton("Save Export Settings")
        self.save_export_content_policy.setEnabled(False)
        self.save_export_content_policy.clicked.connect(
            self._save_export_content_settings
        )
        actions.addWidget(self.save_export_content_policy)
        group_layout.addLayout(actions); layout.addWidget(group)
        self.export_content_rating.currentIndexChanged.connect(
            self._export_content_controls_changed
        )

    def _export_content_policy_from_controls(self):
        return ExportContentPolicy(
            str(self.export_content_rating.currentData() or "explicit")
        )

    def _refresh_export_content_settings(self):
        settings=getattr(self.state,"settings",None)
        if settings is None:
            self.save_export_content_policy.setEnabled(False)
            self.export_content_status.setText("Application settings are unavailable.")
            return
        policy=ExportContentPolicy.from_settings(settings)
        self._export_content_saved_policy=policy
        blocker=QSignalBlocker(self.export_content_rating)
        self.export_content_rating.setCurrentIndex(max(
            0,self.export_content_rating.findData(policy.maximum_content_rating)
        ))
        del blocker
        self.save_export_content_policy.setEnabled(False)
        self.export_content_status.setText(
            "Saved Dataset export content boundary. No export was created or changed."
        )

    def _export_content_controls_changed(self,*_):
        changed=(
            self._export_content_policy_from_controls()
            !=self._export_content_saved_policy
        )
        if not changed:
            self._clear_export_content_save_failure_notice()
        self.save_export_content_policy.setEnabled(changed)
        self.export_content_status.setText(
            "Pending export content policy change has not been saved or applied."
            if changed else
            "Saved Dataset export content boundary. No export was created or changed."
        )

    def _clear_export_content_save_failure_notice(self):
        notice=getattr(self,"_export_content_save_failure_notice","")
        status_bar=getattr(self.window(),"statusBar",lambda:None)()
        if status_bar is not None and notice and status_bar.currentMessage()==notice:
            status_bar.clearMessage()
        self._export_content_save_failure_notice=""

    def _save_export_content_settings(self):
        settings=getattr(self.state,"settings",None)
        if settings is None:return False
        previous=ExportContentPolicy.from_settings(settings)
        policy=self._export_content_policy_from_controls()
        policy.apply_to(settings)
        try:
            settings.save()
        except Exception as exc:
            previous.apply_to(settings)
            self._export_content_saved_policy=previous
            self.save_export_content_policy.setEnabled(policy!=previous)
            self.export_content_status.setText(
                "Pending change was not saved; the prior verified export content policy remains active."
            )
            title="Export content policy was not saved"
            detail="The prior verified setting remains active.\n\n"+str(exc)
            self._export_content_save_failure_notice=f"{title}\n{detail}"
            QMessageBox.warning(self,title,detail)
            return False
        self._export_content_saved_policy=policy
        self._clear_export_content_save_failure_notice()
        self.save_export_content_policy.setEnabled(False)
        self.export_content_status.setText(
            "Saved. Future Dataset previews use this boundary; no export was changed."
        )
        return True

    @staticmethod
    def _organization_level_boxes(selected):
        boxes=[]
        for position in range(4):
            box=QComboBox(); box.addItem("Not used","")
            box.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            box.setMinimumContentsLength(8)
            for key,label in LEVEL_LABELS.items():
                box.addItem(label,key)
            if position<len(selected):
                box.setCurrentIndex(max(0,box.findData(selected[position])))
            boxes.append(box)
        return boxes

    @staticmethod
    def _selected_organization_levels(boxes):
        return [str(box.currentData()) for box in boxes if box.currentData()]

    def _build_organization_settings(self,layout):
        group=QGroupBox("Folder organization policy")
        self.organization_policy_group=group
        group_layout=QVBoxLayout(group)
        intro=QLabel(
            "This reusable policy supplies the default folder route and filename "
            "behavior for contextual Move to Library and Reorganize previews. "
            "Saving it does not scan or move any image."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel")
        group_layout.addWidget(intro)

        self.organization_detail_tabs=QTabWidget()
        self.organization_detail_tabs.setAccessibleName("Folder organization settings")
        group_layout.addWidget(self.organization_detail_tabs,1)
        route_layout=self._add_policy_detail(
            self.organization_detail_tabs,"organization","Routes && Filenames",
        )
        container_layout=self._add_policy_detail(
            self.organization_detail_tabs,"organization_containers","Category Containers",
        )
        naming_row=QHBoxLayout()
        naming_row.addWidget(QLabel("Default image filename"))
        self.organization_naming=QComboBox()
        self.organization_naming.addItem(
            "Keep current filename",LIBRARY_NAMING_KEEP,
        )
        self.organization_naming.addItem(
            "Rename using image hash",LIBRARY_NAMING_IMAGE_HASH,
        )
        self.organization_naming.setToolTip(
            "Contextual move previews may use a different choice for one exact "
            "operation without changing this reusable default."
        )
        naming_row.addWidget(self.organization_naming,1)
        route_layout.addLayout(naming_row)

        routes=QGroupBox("Default destination route")
        routes_grid=QGridLayout(routes)
        routes_grid.addWidget(QLabel("Normal images"),0,0)
        profile=OrganizationProfile.from_dict(
            getattr(getattr(self.state,"settings",None),"organization_profile",{})
        )
        self.organization_general_levels=self._organization_level_boxes(
            profile.levels
        )
        for index,box in enumerate(self.organization_general_levels):
            routes_grid.addWidget(box,0,index+1)
        self.organization_known_route=QCheckBox(
            "Use a different hierarchy when a known character is assigned"
        )
        self.organization_known_route.setChecked(profile.use_known_character_route)
        routes_grid.addWidget(self.organization_known_route,1,0,1,5)
        routes_grid.addWidget(QLabel("Known characters"),2,0)
        self.organization_known_levels=self._organization_level_boxes(
            profile.known_character_levels
        )
        for index,box in enumerate(self.organization_known_levels):
            routes_grid.addWidget(box,2,index+1)
        self.organization_fallback_enabled=QCheckBox(
            "Use a fallback folder for missing values"
        )
        self.organization_fallback_enabled.setChecked(profile.fallback_enabled)
        self.organization_fallback_enabled.setToolTip(
            "Off by default. When Off, a missing route value is skipped and "
            "sorting continues at the nearest resolved parent folder."
        )
        routes_grid.addWidget(self.organization_fallback_enabled,3,0,1,5)
        routes_grid.addWidget(QLabel("Fallback folder name"),4,0)
        self.organization_fallback=QLineEdit(profile.fallback_folder)
        self.organization_fallback.setEnabled(profile.fallback_enabled)
        routes_grid.addWidget(self.organization_fallback,4,1,1,2)

        self.organization_container_panel=QGroupBox("Category containers")
        container_grid=QGridLayout(self.organization_container_panel)
        container_intro=QLabel(
            "Add a stable folder before high-cardinality values so each route "
            "level is unambiguous."
        )
        container_intro.setWordWrap(True); container_intro.setObjectName("mutedLabel")
        container_grid.addWidget(container_intro,0,0,1,3)
        self.organization_container_controls={}
        for row,(level,default_label) in enumerate(
            DEFAULT_LEVEL_CONTAINERS.items(),1
        ):
            enabled=level in profile.level_containers
            check=QCheckBox(f"Container before {LEVEL_LABELS[level]}")
            check.setChecked(enabled)
            entry=QLineEdit(profile.level_containers.get(level,default_label))
            entry.setEnabled(enabled); check.toggled.connect(entry.setEnabled)
            self.organization_container_controls[level]=(check,entry)
            container_grid.addWidget(check,row,0)
            container_grid.addWidget(QLabel("Folder name"),row,1)
            container_grid.addWidget(entry,row,2)
        container_layout.addWidget(self.organization_container_panel)
        container_layout.addStretch(1)
        route_layout.addWidget(routes)

        self.organization_route_preview=QLabel()
        self.organization_route_preview.setWordWrap(True)
        self.organization_route_preview.setObjectName("mutedLabel")
        route_layout.addWidget(self.organization_route_preview)
        route_layout.addStretch(1)
        consequence=QLabel(
            "The saved policy applies to future previews only. Existing images "
            "are never reorganized automatically."
        )
        consequence.setWordWrap(True); consequence.setObjectName("mutedLabel")
        group_layout.addWidget(consequence)
        self.organization_status=QLabel()
        self.organization_status.setWordWrap(True)
        self.organization_status.setObjectName("mutedLabel")
        group_layout.addWidget(self.organization_status)
        actions=QHBoxLayout(); actions.addStretch(1)
        self.save_organization_policy=QPushButton("Save Organization Settings")
        self.save_organization_policy.setEnabled(False)
        self.save_organization_policy.clicked.connect(
            self._save_organization_settings
        )
        actions.addWidget(self.save_organization_policy)
        group_layout.addLayout(actions)
        layout.addWidget(group)

        for key in ("organization","organization_containers"):
            guard_scrollable_value_controls(self.policy_scrolls[key].widget())
        for control in (
            self.organization_naming,*self.organization_general_levels,
            *self.organization_known_levels,
        ):
            control.currentIndexChanged.connect(
                self._organization_controls_changed
            )
        self.organization_known_route.toggled.connect(
            self._organization_controls_changed
        )
        self.organization_fallback_enabled.toggled.connect(
            self.organization_fallback.setEnabled
        )
        self.organization_fallback_enabled.toggled.connect(
            self._organization_controls_changed
        )
        self.organization_fallback.textChanged.connect(
            self._organization_controls_changed
        )
        for check,entry in self.organization_container_controls.values():
            check.toggled.connect(self._organization_controls_changed)
            entry.textChanged.connect(self._organization_controls_changed)

    def _active_organization_containers(self):
        return {
            level:entry.text().strip()
            for level,(check,entry) in self.organization_container_controls.items()
            if check.isChecked() and entry.text().strip()
        }

    def _organization_policy_from_controls(self):
        profile=OrganizationProfile(
            levels=self._selected_organization_levels(
                self.organization_general_levels
            ),
            use_known_character_route=self.organization_known_route.isChecked(),
            known_character_levels=self._selected_organization_levels(
                self.organization_known_levels
            ),
            fallback_enabled=self.organization_fallback_enabled.isChecked(),
            fallback_folder=self.organization_fallback.text().strip() or "Unsorted",
            level_containers=self._active_organization_containers(),
        )
        naming=normalize_library_naming_policy(
            self.organization_naming.currentData()
        )
        return profile,naming

    @staticmethod
    def _organization_signature(profile,naming):
        return (
            tuple(profile.levels),profile.use_known_character_route,
            tuple(profile.known_character_levels),profile.fallback_enabled,
            profile.fallback_folder,
            tuple(sorted(profile.level_containers.items())),naming,
        )

    def _refresh_organization_settings(self):
        settings=getattr(self.state,"settings",None)
        if settings is None:
            self.save_organization_policy.setEnabled(False)
            self.organization_status.setText("Application settings are unavailable.")
            return
        profile=OrganizationProfile.from_dict(settings.organization_profile)
        naming=normalize_library_naming_policy(settings.library_move_naming_policy)
        controls=(
            self.organization_naming,*self.organization_general_levels,
            self.organization_known_route,*self.organization_known_levels,
            self.organization_fallback_enabled,self.organization_fallback,
            *(control for pair in self.organization_container_controls.values()
              for control in pair),
        )
        blockers=[QSignalBlocker(control) for control in controls]
        try:
            self.organization_naming.setCurrentIndex(max(
                0,self.organization_naming.findData(naming)
            ))
            for boxes,values in (
                (self.organization_general_levels,profile.levels),
                (self.organization_known_levels,profile.known_character_levels),
            ):
                for index,box in enumerate(boxes):
                    value=values[index] if index<len(values) else ""
                    box.setCurrentIndex(max(0,box.findData(value)))
            self.organization_known_route.setChecked(
                profile.use_known_character_route
            )
            self.organization_fallback_enabled.setChecked(
                profile.fallback_enabled
            )
            self.organization_fallback.setText(profile.fallback_folder)
            self.organization_fallback.setEnabled(profile.fallback_enabled)
            for level,(check,entry) in self.organization_container_controls.items():
                enabled=level in profile.level_containers
                check.setChecked(enabled); entry.setEnabled(enabled)
                entry.setText(profile.level_containers.get(
                    level,DEFAULT_LEVEL_CONTAINERS[level]
                ))
        finally:
            del blockers
        self._organization_saved_signature=self._organization_signature(
            profile,naming
        )
        self.save_organization_policy.setEnabled(False)
        self.organization_status.setText(
            "Saved reusable organization policy. No image has been scanned or moved."
        )
        self._update_organization_preview()

    def _update_organization_preview(self):
        containers=self._active_organization_containers()
        def labels(boxes):
            parts=[]
            for value in self._selected_organization_levels(boxes):
                if containers.get(value):
                    parts.append(containers[value])
                parts.append(f"{LEVEL_LABELS[value]} value")
            return parts
        normal=" → ".join(labels(self.organization_general_levels)) or "Library root"
        known=" → ".join(labels(self.organization_known_levels)) or normal
        text=f"Normal route: {normal}"
        if self.organization_known_route.isChecked():
            text+=f"\nKnown-character route: {known}"
        if self.organization_fallback_enabled.isChecked():
            fallback=self.organization_fallback.text().strip() or "Unsorted"
            text+=f"\nMissing values use the optional fallback: {fallback}"
        else:
            text+=(
                "\nMissing values are skipped; sorting continues with the next "
                "configured level or the nearest resolved parent."
            )
        self.organization_route_preview.setText(text)

    def _organization_controls_changed(self,*_):
        self._update_organization_preview()
        profile,naming=self._organization_policy_from_controls()
        changed=(
            self._organization_signature(profile,naming)
            !=self._organization_saved_signature
        )
        self.save_organization_policy.setEnabled(changed)
        self.organization_status.setText(
            "Pending organization policy changes have not been saved or applied."
            if changed else
            "Saved reusable organization policy. No image has been scanned or moved."
        )

    def _save_organization_settings(self):
        settings=getattr(self.state,"settings",None)
        if settings is None:return False
        fallback=self.organization_fallback.text().strip() or "Unsorted"
        if (
            self.organization_fallback_enabled.isChecked()
            and safe_folder_name(fallback,"")!=fallback
        ):
            QMessageBox.warning(
                self,"Check Missing Information Folder",
                "Enter a valid Windows folder name for missing organization values.",
            )
            self.organization_fallback.setFocus(); return False
        for level,(check,entry) in self.organization_container_controls.items():
            if not check.isChecked():continue
            value=entry.text().strip()
            if not value or safe_folder_name(value,"")!=value:
                QMessageBox.warning(
                    self,"Check Category Container",
                    f"Enter a valid Windows folder name for the container before {LEVEL_LABELS[level]}.",
                )
                entry.setFocus(); return False
        profile,naming=self._organization_policy_from_controls()
        previous_profile=dict(settings.organization_profile)
        previous_naming=settings.library_move_naming_policy
        settings.organization_profile=profile.to_dict()
        settings.library_move_naming_policy=naming
        try:
            settings.save()
        except Exception as exc:
            settings.organization_profile=previous_profile
            settings.library_move_naming_policy=previous_naming
            self.organization_status.setText(
                "Pending change was not saved; the prior verified organization policy remains active."
            )
            QMessageBox.warning(
                self,"Organization policy was not saved",
                "The prior verified setting remains active.\n\n"+str(exc),
            )
            return False
        self._organization_saved_signature=self._organization_signature(
            profile,naming
        )
        self.save_organization_policy.setEnabled(False)
        self.organization_status.setText(
            "Saved. Future organization previews use this policy; existing images were not changed."
        )
        self.organization_policy_changed.emit(profile.to_dict())
        return True

    def _build_behavior_settings(self,layout):
        behavior=QGroupBox("Image analysis and preparation defaults"); self.behavior_group=behavior
        behavior_layout=QVBoxLayout(behavior)
        intro=QLabel(
            "Choose how existing images are analyzed in Ingest and Dataset. "
            "These defaults do not start Training or Generate jobs."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel")
        behavior_layout.addWidget(intro)

        preset_row=QHBoxLayout()
        preset_row.addWidget(QLabel("Image analysis preset"))
        self.behavior_preset=QComboBox()
        self.behavior_preset.addItem("Choose an optional preset…","")
        self.behavior_preset.addItem("Review first","review")
        self.behavior_preset.addItem("Analyze automatically","analyze")
        self.behavior_preset.addItem("Analyze and confirm safe results","maximum")
        self.behavior_preset.setAccessibleName("Optional automation behavior preset")
        self.behavior_preset.currentIndexChanged.connect(self._preview_behavior_preset)
        preset_row.addWidget(self.behavior_preset,1)
        self.stage_behavior_preset=QPushButton("Stage Preset Changes")
        self.stage_behavior_preset.setEnabled(False)
        self.stage_behavior_preset.clicked.connect(self._stage_behavior_preset)
        preset_row.addWidget(self.stage_behavior_preset)
        behavior_layout.addLayout(preset_row)
        self.behavior_preset_preview=QLabel(
            "No preset selected. Saved behavior is unchanged."
        )
        self.behavior_preset_preview.setWordWrap(True)
        self.behavior_preset_preview.setObjectName("mutedLabel")
        behavior_layout.addWidget(self.behavior_preset_preview)
        self.choose_ai_models=QPushButton("Choose AI Models…")
        self.choose_ai_models.setToolTip(
            "Open the AI Tag and Description model controls in Setup. "
            "Your pending policy and preset review stay here."
        )
        self.choose_ai_models.clicked.connect(self._open_ai_setup)
        self.choose_ai_models.hide()
        behavior_layout.addWidget(self.choose_ai_models,0,Qt.AlignLeft)
        self.behavior_preset_changes=QTreeWidget()
        self.behavior_preset_changes.setHeaderLabels(("Setting","Current draft","Preset"))
        self.behavior_preset_changes.setRootIsDecorated(False)
        self.behavior_preset_changes.setAccessibleName("Image analysis preset changes")
        self.behavior_preset_changes.setMinimumHeight(110)
        self.behavior_preset_changes.setMaximumHeight(150)
        self.behavior_preset_changes.setColumnWidth(0,440)
        self.behavior_preset_changes.setColumnWidth(1,170)
        self.behavior_preset_changes.hide()
        behavior_layout.addWidget(self.behavior_preset_changes)

        self.behavior_detail_tabs=QTabWidget()
        self.behavior_detail_tabs.setAccessibleName("AI and preparation settings")
        behavior_layout.addWidget(self.behavior_detail_tabs,1)
        defaults_layout=self._add_policy_detail(self.behavior_detail_tabs,"ai","AI Defaults")
        checks_layout=self._add_policy_detail(self.behavior_detail_tabs,"ai_checks","After Library Checks")
        prepare_layout=self._add_policy_detail(self.behavior_detail_tabs,"ai_prepare","Prepare")
        thresholds_layout=self._add_policy_detail(self.behavior_detail_tabs,"ai_thresholds","Tag Thresholds")
        defaults=QGroupBox("Contextual AI defaults")
        defaults_grid=QGridLayout(defaults)
        self.behavior_use_wd=QCheckBox("Offer AI Tags in contextual workflows")
        self.behavior_use_joy=QCheckBox("Offer AI Descriptions in contextual workflows")
        self.behavior_auto_wd=QCheckBox("Analyze Tags after library checks")
        self.behavior_auto_joy=QCheckBox("Analyze Descriptions after library checks")
        defaults_grid.addWidget(self.behavior_use_wd,0,0)
        defaults_grid.addWidget(self.behavior_use_joy,1,0)
        checks_layout.addWidget(self.behavior_auto_wd)
        checks_layout.addWidget(self.behavior_auto_joy)
        defaults_grid.setColumnStretch(0,1)
        defaults_layout.addWidget(defaults)
        ignores=QGroupBox("Ignored Descriptions")
        ignores_layout=QVBoxLayout(ignores)
        self.description_exclusions_summary=QLabel()
        self.description_exclusions_summary.setWordWrap(True)
        ignores_layout.addWidget(self.description_exclusions_summary)
        self.description_exclusions_button=QPushButton("Review Ignored Descriptions…")
        self.description_exclusions_button.clicked.connect(self._open_description_exclusions)
        ignores_layout.addWidget(self.description_exclusions_button,0,Qt.AlignLeft)
        defaults_layout.addWidget(ignores)
        self._refresh_description_exclusions_summary()
        defaults_layout.addStretch(1)

        changes=QGroupBox("Safe changes during Prepare")
        changes_grid=QGridLayout(changes)
        self.behavior_auto_confirm=QCheckBox("Confirm safe Tags after library checks")
        self.behavior_consensus=QSpinBox(); self.behavior_consensus.setRange(1,99)
        self.behavior_prepare_confirm=QCheckBox("Confirm safe Tags during Prepare")
        self.behavior_prepare_descriptions=QCheckBox("Fill or upgrade Descriptions during Prepare")
        self.behavior_prepare_preset=QComboBox()
        for key in ("balanced","conservative","permissive"):
            self.behavior_prepare_preset.addItem(CONFIDENCE_PRESETS[key]["label"],key)
        self.behavior_prepare_preset.addItem("Custom","custom")
        self.behavior_prepare_votes=QSpinBox(); self.behavior_prepare_votes.setRange(1,99)
        self.behavior_prepare_external_vote=QCheckBox("Count matching TXT / Metadata as one vote")
        checks_layout.addWidget(self.behavior_auto_confirm)
        check_votes=QHBoxLayout()
        check_votes.addWidget(QLabel("Minimum votes after library checks"))
        check_votes.addWidget(self.behavior_consensus)
        checks_layout.addLayout(check_votes); checks_layout.addStretch(1)
        changes_grid.addWidget(self.behavior_prepare_confirm,2,0,1,2)
        changes_grid.addWidget(self.behavior_prepare_descriptions,3,0,1,2)
        changes_grid.addWidget(QLabel("Preparation confidence"),4,0)
        changes_grid.addWidget(self.behavior_prepare_preset,4,1)
        changes_grid.addWidget(QLabel("Minimum votes during Prepare"),5,0)
        changes_grid.addWidget(self.behavior_prepare_votes,5,1)
        changes_grid.addWidget(self.behavior_prepare_external_vote,6,0,1,2)
        changes_grid.setColumnStretch(0,1)
        prepare_layout.addWidget(changes)
        self.behavior_prepare_read_external=QCheckBox("Read adjacent TXT and Metadata during Prepare")
        self.behavior_prepare_read_external.setToolTip("Default for Prepare Images only. Library-check evidence intake has its own trigger. Imported sources remain reviewable evidence.")
        prepare_layout.addWidget(self.behavior_prepare_read_external)
        self.behavior_prepare_fingerprints=QCheckBox("Create duplicate fingerprints during preparation")
        self.behavior_prepare_fingerprints.setToolTip(
            "Default for Prepare Images in Ingest and Dataset. Reuse valid fingerprints and refresh changed images. "
            "This does not compare libraries, remove images, or start work when saved."
        )
        prepare_layout.addWidget(self.behavior_prepare_fingerprints)
        reuse_row=QHBoxLayout()
        reuse_row.addWidget(QLabel("Reuse matching AI results within"))
        self.behavior_prepare_reuse_value=QSpinBox()
        self.behavior_prepare_reuse_value.setRange(1,999)
        self.behavior_prepare_reuse_value.setAccessibleName("Default AI result reuse amount")
        self.behavior_prepare_reuse_unit=QComboBox()
        for label,key in (("Hours","hours"),("Days","days"),("Months (30 days)","months")):
            self.behavior_prepare_reuse_unit.addItem(label,key)
        self.behavior_prepare_reuse_unit.setAccessibleName("Default AI result reuse unit")
        reuse_row.addWidget(self.behavior_prepare_reuse_value)
        reuse_row.addWidget(self.behavior_prepare_reuse_unit)
        prepare_layout.addLayout(reuse_row)
        reuse_help=QLabel("Changed images, models, prompts or settings still require fresh AI results. Force All Scans overrides reuse for one run.")
        reuse_help.setWordWrap(True); reuse_help.setObjectName("mutedLabel")
        prepare_layout.addWidget(reuse_help)
        prepare_layout.addStretch(1)

        confidence=QGroupBox("Minimum confidence for reviewable AI Tag suggestions")
        confidence_grid=QGridLayout(confidence)
        self.behavior_general_threshold=QDoubleSpinBox()
        self.behavior_character_threshold=QDoubleSpinBox()
        self.behavior_rating_threshold=QDoubleSpinBox()
        for control,maximum in (
            (self.behavior_general_threshold,.95),
            (self.behavior_character_threshold,.99),
            (self.behavior_rating_threshold,.99),
        ):
            control.setRange(.05,maximum); control.setSingleStep(.05)
        for column,(label,control) in enumerate((
            ("General Tags",self.behavior_general_threshold),
            ("Character Tags",self.behavior_character_threshold),
            ("Rating Tags",self.behavior_rating_threshold),
        )):
            confidence_grid.addWidget(QLabel(label),0,column)
            confidence_grid.addWidget(control,1,column)
            confidence_grid.setColumnStretch(column,1)
        thresholds_layout.addWidget(confidence)
        thresholds_layout.addStretch(1)

        self.behavior_summary=QLabel(); self.behavior_summary.setWordWrap(True)
        self.behavior_summary.setObjectName("mutedLabel")
        self.behavior_status=QLabel(); self.behavior_status.setWordWrap(True)
        self.behavior_status.setObjectName("mutedLabel")
        defaults_layout.insertWidget(1,self.behavior_summary)
        save_scope=QLabel("Save applies staged AI defaults, Tag suggestions, after-check behavior and Prepare defaults together.")
        save_scope.setWordWrap(True); save_scope.setObjectName("mutedLabel")
        behavior_layout.addWidget(save_scope)
        behavior_layout.addWidget(self.behavior_status)
        actions=QHBoxLayout(); actions.addStretch(1)
        self.discard_behavior=QPushButton("Discard Pending Changes")
        self.discard_behavior.setToolTip("Restore saved AI analysis, Tag suggestions, after-check and Prepare defaults. Other policy sections keep their edits.")
        self.discard_behavior.setEnabled(False)
        self.discard_behavior.clicked.connect(self._refresh_behavior_settings)
        actions.addWidget(self.discard_behavior)
        self.save_behavior=QPushButton("Save AI / Prepare Defaults")
        self.save_behavior.setEnabled(False)
        self.save_behavior.clicked.connect(self._save_behavior_settings)
        actions.addWidget(self.save_behavior); behavior_layout.addLayout(actions)
        layout.addWidget(behavior)

        for key in ("ai","ai_checks","ai_prepare","ai_thresholds"):
            guard_scrollable_value_controls(self.policy_scrolls[key].widget())
        for control in (
            self.behavior_prepare_read_external,self.behavior_prepare_fingerprints,
            self.behavior_use_wd,self.behavior_use_joy,
            self.behavior_auto_wd,self.behavior_auto_joy,
            self.behavior_auto_confirm,self.behavior_prepare_confirm,
            self.behavior_prepare_descriptions,self.behavior_prepare_external_vote,
        ):
            control.toggled.connect(self._behavior_controls_changed)
        self.behavior_prepare_reuse_unit.currentIndexChanged.connect(self._behavior_controls_changed)
        for control in (
            self.behavior_prepare_reuse_value,
            self.behavior_consensus,self.behavior_prepare_votes,
            self.behavior_general_threshold,self.behavior_character_threshold,
            self.behavior_rating_threshold,
        ):
            control.valueChanged.connect(self._behavior_controls_changed)
        self.behavior_auto_confirm.toggled.connect(self._behavior_confirmation_changed)
        self.behavior_prepare_preset.currentIndexChanged.connect(
            self._behavior_preparation_preset_changed
        )
        self.behavior_prepare_votes.valueChanged.connect(
            self._behavior_preparation_values_changed
        )
        self.behavior_prepare_external_vote.toggled.connect(
            self._behavior_preparation_values_changed
        )

    def _build_library_check_settings(self,layout):
        group=QGroupBox("Library checks")
        self.library_check_group=group
        group_layout=QVBoxLayout(group)
        note=QLabel(
            "These reusable defaults govern when the existing library-check "
            "workflow runs and the initial scope of contextual folder scans. "
            "Saving them does not open or scan a library."
        )
        note.setWordWrap(True); note.setObjectName("mutedLabel")
        group_layout.addWidget(note)
        self.library_check_startup=QCheckBox(
            "Check included libraries for changes at startup"
        )
        self.library_check_subfolders=QCheckBox(
            "Include subfolders by default in contextual folder scans"
        )
        group_layout.addWidget(self.library_check_startup)
        group_layout.addWidget(self.library_check_subfolders)
        contextual=QLabel(
            "Ingest and Dataset still own the visible scope, progress, "
            "cancellation, and results for each check."
        )
        contextual.setWordWrap(True); contextual.setObjectName("mutedLabel")
        group_layout.addWidget(contextual)
        self.library_check_status=QLabel()
        self.library_check_status.setWordWrap(True)
        self.library_check_status.setObjectName("mutedLabel")
        group_layout.addWidget(self.library_check_status)
        actions=QHBoxLayout(); actions.addStretch(1)
        self.save_library_check_policy=QPushButton("Save Library Check Settings")
        self.save_library_check_policy.setEnabled(False)
        self.save_library_check_policy.clicked.connect(
            self._save_library_check_settings
        )
        actions.addWidget(self.save_library_check_policy)
        group_layout.addLayout(actions)
        layout.addWidget(group)
        self.library_check_startup.toggled.connect(
            self._library_check_controls_changed
        )
        self.library_check_subfolders.toggled.connect(
            self._library_check_controls_changed
        )

    def _library_check_policy_from_controls(self):
        return LibraryCheckPolicy(
            validate_library_on_startup=self.library_check_startup.isChecked(),
            include_subfolders=self.library_check_subfolders.isChecked(),
        )

    def _refresh_library_check_settings(self):
        settings=getattr(self.state,"settings",None)
        if settings is None:
            self.save_library_check_policy.setEnabled(False)
            self.library_check_status.setText("Application settings are unavailable.")
            return
        policy=LibraryCheckPolicy.from_settings(settings)
        self._library_check_saved_policy=policy
        blockers=(
            QSignalBlocker(self.library_check_startup),
            QSignalBlocker(self.library_check_subfolders),
        )
        self.library_check_startup.setChecked(
            policy.validate_library_on_startup
        )
        self.library_check_subfolders.setChecked(policy.include_subfolders)
        del blockers
        self.save_library_check_policy.setEnabled(False)
        self.library_check_status.setText(
            "Saved library-check trigger and default folder scope. No check was run."
        )

    def _library_check_controls_changed(self,*_):
        changed=(
            self._library_check_policy_from_controls()
            !=self._library_check_saved_policy
        )
        self.save_library_check_policy.setEnabled(changed)
        self.library_check_status.setText(
            "Pending library-check policy changes have not been saved or run."
            if changed else
            "Saved library-check trigger and default folder scope. No check was run."
        )

    def _save_library_check_settings(self):
        settings=getattr(self.state,"settings",None)
        if settings is None:return False
        previous=LibraryCheckPolicy.from_settings(settings)
        policy=self._library_check_policy_from_controls()
        policy.apply_to(settings)
        try:
            settings.save()
        except Exception as exc:
            previous.apply_to(settings)
            self._library_check_saved_policy=previous
            self.save_library_check_policy.setEnabled(policy!=previous)
            self.library_check_status.setText(
                "Pending change was not saved; the prior verified library-check policy remains active."
            )
            QMessageBox.warning(
                self,"Library check policy was not saved",
                "The prior verified setting remains active.\n\n"+str(exc),
            )
            return False
        self._library_check_saved_policy=policy
        self.save_library_check_policy.setEnabled(False)
        self.library_check_status.setText(
            "Saved. Future checks use this trigger and default scope; no check was run."
        )
        return True

    def _build_evidence_intake_settings(self,layout):
        group=QGroupBox("External evidence intake")
        self.evidence_intake_group=group
        group_layout=QVBoxLayout(group)
        note=QLabel(
            "This reusable default controls whether ordinary library checks read "
            "new or changed adjacent TXT and embedded Metadata as attributed, "
            "reviewable evidence. It never confirms that evidence by itself."
        )
        note.setWordWrap(True); note.setObjectName("mutedLabel")
        group_layout.addWidget(note)
        self.evidence_intake_auto=QCheckBox(
            "Import changed TXT and Metadata during library checks"
        )
        self.evidence_intake_auto.setToolTip(
            "When off, library checks still create or reuse authoritative Image "
            "Knowledge without reading external TXT or Metadata. Deliberate "
            "Import External Evidence remains in Ingest and Dataset."
        )
        group_layout.addWidget(self.evidence_intake_auto)
        contextual=QLabel(
            "Ingest and Dataset own deliberate Import External Evidence scope, "
            "preview, progress, cancellation, results, and recovery. Saving this "
            "default does not create a second Automation job."
        )
        contextual.setWordWrap(True); contextual.setObjectName("mutedLabel")
        group_layout.addWidget(contextual)
        self.evidence_intake_status=QLabel()
        self.evidence_intake_status.setWordWrap(True)
        self.evidence_intake_status.setObjectName("mutedLabel")
        group_layout.addWidget(self.evidence_intake_status)
        actions=QHBoxLayout(); actions.addStretch(1)
        self.save_evidence_intake=QPushButton("Save Evidence Intake")
        self.save_evidence_intake.setEnabled(False)
        self.save_evidence_intake.clicked.connect(
            self._save_evidence_intake_settings
        )
        actions.addWidget(self.save_evidence_intake)
        group_layout.addLayout(actions)
        layout.addWidget(group)
        self.evidence_intake_auto.toggled.connect(
            self._evidence_intake_controls_changed
        )

    def _refresh_evidence_intake_settings(self):
        settings=getattr(self.state,"settings",None)
        if settings is None:
            self.save_evidence_intake.setEnabled(False)
            self.evidence_intake_status.setText("Application settings are unavailable.")
            return
        policy=ExternalEvidenceIntakePolicy.from_settings(settings)
        self._evidence_intake_saved_policy=policy
        blocker=self.evidence_intake_auto.blockSignals(True)
        self.evidence_intake_auto.setChecked(policy.auto_import_source_evidence)
        self.evidence_intake_auto.blockSignals(blocker)
        self.save_evidence_intake.setEnabled(False)
        self.evidence_intake_status.setText(
            "Saved. Changed TXT and Metadata will be imported during ordinary "
            "library checks."
            if policy.auto_import_source_evidence else
            "Saved. Ordinary library checks will not read external TXT or Metadata."
        )

    def _evidence_intake_controls_changed(self,*_):
        policy=ExternalEvidenceIntakePolicy(
            auto_import_source_evidence=self.evidence_intake_auto.isChecked()
        )
        self.save_evidence_intake.setEnabled(
            policy!=getattr(self,"_evidence_intake_saved_policy",None)
        )
        self.evidence_intake_status.setText(
            "Pending change is staged here and has not been saved or run."
            if self.save_evidence_intake.isEnabled() else
            "Saved evidence-intake behavior is unchanged."
        )

    def _save_evidence_intake_settings(self):
        settings=getattr(self.state,"settings",None)
        if settings is None:return False
        previous=ExternalEvidenceIntakePolicy.from_settings(settings)
        policy=ExternalEvidenceIntakePolicy(
            auto_import_source_evidence=self.evidence_intake_auto.isChecked()
        )
        policy.apply_to(settings)
        try:
            settings.save()
        except Exception as exc:
            previous.apply_to(settings)
            self._evidence_intake_saved_policy=previous
            self.save_evidence_intake.setEnabled(policy!=previous)
            self.evidence_intake_status.setText(
                "Pending change was not saved; the prior verified intake policy remains active."
            )
            QMessageBox.warning(
                self,"Evidence intake was not saved",
                "The prior verified setting remains active.\n\n"+str(exc),
            )
            return False
        self._evidence_intake_saved_policy=policy
        self.save_evidence_intake.setEnabled(False)
        self.evidence_intake_status.setText(
            "Saved. Current included libraries were offered to the existing "
            "refresh workflow; no Automation job was created."
            if policy.auto_import_source_evidence and not previous.auto_import_source_evidence
            else "Saved. The new default applies to future library checks."
        )
        self.evidence_policy_changed.emit(policy.auto_import_source_evidence)
        return True

    def sync_reusable_settings(self):
        """Reflect onboarding writes without discarding staged Automation edits."""
        settings=getattr(self.state,"settings",None)
        if settings is None:return
        if self.save_behavior.isEnabled():
            self._behavior_saved_policy=AutomationBehaviorPolicy.from_settings(
                settings
            )
            staged=self._behavior_policy_from_controls()
            self._set_behavior_pending(staged!=self._behavior_saved_policy)
            self.behavior_status.setText(
                "Guided Setup changed the saved starting configuration; staged "
                "Automation controls were kept for review."
                if self.save_behavior.isEnabled() else
                "Saved behavior now matches the staged controls."
            )
        else:
            self._refresh_behavior_settings()
        if self.save_library_check_policy.isEnabled():
            self._library_check_saved_policy=LibraryCheckPolicy.from_settings(
                settings
            )
            staged=self._library_check_policy_from_controls()
            self.save_library_check_policy.setEnabled(
                staged!=self._library_check_saved_policy
            )
            self.library_check_status.setText(
                "Guided Setup changed the saved starting configuration; the "
                "staged library-check controls were kept for review."
                if self.save_library_check_policy.isEnabled() else
                "Saved library-check policy now matches the staged controls."
            )
        else:
            self._refresh_library_check_settings()
        if self.save_evidence_intake.isEnabled():
            self._evidence_intake_saved_policy=(
                ExternalEvidenceIntakePolicy.from_settings(settings)
            )
            staged=ExternalEvidenceIntakePolicy(
                auto_import_source_evidence=self.evidence_intake_auto.isChecked()
            )
            self.save_evidence_intake.setEnabled(
                staged!=self._evidence_intake_saved_policy
            )
            self.evidence_intake_status.setText(
                "Guided Setup changed the saved starting configuration; the "
                "staged intake control was kept for review."
                if self.save_evidence_intake.isEnabled() else
                "Saved evidence intake now matches the staged control."
            )
        else:
            self._refresh_evidence_intake_settings()
        if self.save_export_content_policy.isEnabled():
            self._export_content_saved_policy=ExportContentPolicy.from_settings(
                settings
            )
            staged=self._export_content_policy_from_controls()
            self.save_export_content_policy.setEnabled(
                staged!=self._export_content_saved_policy
            )
            self.export_content_status.setText(
                "Guided Setup changed the saved starting configuration; the "
                "staged export content control was kept for review."
                if self.save_export_content_policy.isEnabled() else
                "Saved export content policy now matches the staged control."
            )
        else:
            self._refresh_export_content_settings()
        if self.save_organization_policy.isEnabled():
            saved_profile=OrganizationProfile.from_dict(
                settings.organization_profile
            )
            saved_naming=normalize_library_naming_policy(
                settings.library_move_naming_policy
            )
            self._organization_saved_signature=self._organization_signature(
                saved_profile,saved_naming
            )
            staged_profile,staged_naming=self._organization_policy_from_controls()
            changed=(
                self._organization_signature(staged_profile,staged_naming)
                !=self._organization_saved_signature
            )
            self.save_organization_policy.setEnabled(changed)
            self.organization_status.setText(
                "Guided Setup changed the saved starting configuration; the "
                "staged organization controls were kept for review."
                if changed else
                "Saved organization policy now matches the staged controls."
            )
        else:
            self._refresh_organization_settings()

    def _behavior_policy_from_controls(self):
        return AutomationBehaviorPolicy.from_dict({
            "suggest_wd_for_missing_tags":self.behavior_use_wd.isChecked(),
            "suggest_joycaption_for_missing_descriptions":self.behavior_use_joy.isChecked(),
            "auto_run_wd_on_managed_images":self.behavior_auto_wd.isChecked(),
            "auto_run_joy_on_managed_images":self.behavior_auto_joy.isChecked(),
            "wd_general_threshold":self.behavior_general_threshold.value(),
            "wd_character_threshold":self.behavior_character_threshold.value(),
            "wd_rating_threshold":self.behavior_rating_threshold.value(),
            "wd_consensus_minimum":self.behavior_consensus.value(),
            "auto_merge_ai_tags":self.behavior_auto_confirm.isChecked(),
            "prepare_confidence_preset":str(
                self.behavior_prepare_preset.currentData() or "custom"
            ),
            "prepare_tag_vote_minimum":self.behavior_prepare_votes.value(),
            "prepare_external_evidence_vote":self.behavior_prepare_external_vote.isChecked(),
            "prepare_confirm_safe_tags":self.behavior_prepare_confirm.isChecked(),
            "prepare_preferred_descriptions":self.behavior_prepare_descriptions.isChecked(),
            "prepare_read_external_evidence":self.behavior_prepare_read_external.isChecked(),
            "ingest_create_duplicate_fingerprints":self.behavior_prepare_fingerprints.isChecked(),
            "prepare_ai_skip_value":self.behavior_prepare_reuse_value.value(),
            "prepare_ai_skip_unit":self.behavior_prepare_reuse_unit.currentData(),
        })

    def _set_behavior_controls(self,policy):
        self._behavior_syncing=True
        try:
            self.behavior_prepare_read_external.setChecked(policy.prepare_read_external_evidence)
            self.behavior_prepare_fingerprints.setChecked(policy.ingest_create_duplicate_fingerprints)
            self.behavior_prepare_reuse_value.setValue(policy.prepare_ai_skip_value)
            self.behavior_prepare_reuse_unit.setCurrentIndex(self.behavior_prepare_reuse_unit.findData(policy.prepare_ai_skip_unit))
            self.behavior_use_wd.setChecked(policy.suggest_wd_for_missing_tags)
            self.behavior_use_joy.setChecked(policy.suggest_joycaption_for_missing_descriptions)
            self.behavior_auto_wd.setChecked(policy.auto_run_wd_on_managed_images)
            self.behavior_auto_joy.setChecked(policy.auto_run_joy_on_managed_images)
            self.behavior_general_threshold.setValue(policy.wd_general_threshold)
            self.behavior_character_threshold.setValue(policy.wd_character_threshold)
            self.behavior_rating_threshold.setValue(policy.wd_rating_threshold)
            self.behavior_consensus.setValue(policy.wd_consensus_minimum)
            self.behavior_auto_confirm.setChecked(policy.auto_merge_ai_tags)
            self.behavior_prepare_preset.setCurrentIndex(max(
                0,self.behavior_prepare_preset.findData(policy.prepare_confidence_preset)
            ))
            self.behavior_prepare_votes.setValue(policy.prepare_tag_vote_minimum)
            self.behavior_prepare_external_vote.setChecked(
                policy.prepare_external_evidence_vote
            )
            self.behavior_prepare_confirm.setChecked(policy.prepare_confirm_safe_tags)
            self.behavior_prepare_descriptions.setChecked(
                policy.prepare_preferred_descriptions
            )
        finally:
            self._behavior_syncing=False
        self._behavior_confirmation_changed()

    @staticmethod
    def _inspect_behavior_capabilities(settings):
        if settings is None:return False,False,0
        service=DependencyService(settings)
        enabled=tuple(getattr(settings,"enabled_wd_taggers",()) or ())
        ready_tags=tuple(
            model_id for model_id in enabled
            if (
                (component:=service.component(service.wd_component_name(model_id)))
                is not None and component.installed
            )
        )
        selected=str(getattr(settings,"plain_language_model","") or "")
        description_component=(
            service.component(service.plain_language_component_name(selected))
            if selected else None
        )
        return bool(ready_tags),bool(
            description_component and description_component.installed
        ),len(ready_tags)

    def _behavior_capabilities(self):
        return self._inspect_behavior_capabilities(
            getattr(self.state,"settings",None)
        )

    def _start_behavior_capability_check(self):
        if self._capability_task is not None:
            return
        settings=getattr(self.state,"settings",None)
        inspect=self._inspect_behavior_capabilities
        task=CoordinatedCallTask(
            self.state.jobs,"Checking Automation capabilities",
            lambda _job:inspect(settings),
            resource="io",key="automation:component-capabilities",
        )
        self._capability_task=task
        task.signals.complete.connect(
            lambda result,owner=task:self._behavior_capability_complete(
                owner,result,
            )
        )
        task.signals.failed.connect(
            lambda message,owner=task:self._behavior_capability_failed(
                owner,message,
            )
        )
        task.signals.cancelled.connect(
            lambda owner=task:self._behavior_capability_failed(
                owner,"Capability check was cancelled safely.",
            )
        )
        task.start()

    def _behavior_capability_complete(self,task,result):
        if task is not self._capability_task:
            return
        self._capability_task=None
        self._behavior_capability_result=tuple(result)
        self._show_ai_setup_if_needed(*self._behavior_capability_result[:2])
        if self.save_behavior.isEnabled() or self._behavior_preview_policy is not None:
            self.behavior_status.setText(
                "Model readiness checked. Pending controls and preset preview were kept; nothing was saved or run."
            )
        else:
            self._refresh_behavior_settings()
        if self._shutdown_requested:
            self._notify_background_work_finished()

    def _behavior_capability_failed(self,task,message):
        if task is not None and task is not self._capability_task:
            return
        self._capability_task=None
        self._behavior_capability_result=None
        if hasattr(self,"choose_ai_models"):
            self.choose_ai_models.setVisible(True)
        if hasattr(self,"behavior_status"):
            pending=self.save_behavior.isEnabled() or self._behavior_preview_policy is not None
            self.behavior_status.setText(
                ("Pending changes kept. " if pending else "Saved. ")+
                "Optional component readiness could not be checked: "
                +str(message)
            )
        if self._shutdown_requested:
            self._notify_background_work_finished()

    def _set_behavior_pending(self,pending):
        self.save_behavior.setEnabled(pending)
        self.discard_behavior.setEnabled(pending or self._behavior_preview_policy is not None)

    def _show_ai_setup_if_needed(self,tag_ready,description_ready):
        self.choose_ai_models.setVisible(not (tag_ready and description_ready))

    def _open_ai_setup(self):
        tag_ready,description_ready,_count=self._behavior_capabilities()
        self.ai_setup_requested.emit("sdxl" if not tag_ready else "descriptions")

    def refresh_behavior_prerequisites(self):
        """Refresh model readiness after Setup without consuming a policy draft."""
        tag_ready,description_ready,count=self._behavior_capabilities()
        self._behavior_capability_result=(tag_ready,description_ready,count)
        self._show_ai_setup_if_needed(tag_ready,description_ready)
        if self._behavior_preview_policy is not None:
            self._preview_behavior_preset()
        elif self.save_behavior.isEnabled():
            self.behavior_status.setText(
                "Model readiness refreshed. Pending policy changes are still unsaved; "
                "review and save them here when ready."
            )
        else:
            self.behavior_status.setText(
                "Saved. Setup is missing a ready AI model; automation waits without changing your policy."
                if not (tag_ready and description_ready) else
                "Saved. Model prerequisites are ready; no automation was started."
            )

    def _clear_behavior_preset_preview(self,message="No preset selected. Saved behavior is unchanged."):
        self._behavior_preview_policy=None
        self.stage_behavior_preset.setEnabled(False)
        blocker=QSignalBlocker(self.behavior_preset)
        self.behavior_preset.setCurrentIndex(0)
        del blocker
        self.behavior_preset_changes.clear()
        self.behavior_preset_changes.hide()
        self.behavior_preset_preview.setText(message)

    def _refresh_behavior_settings(self):
        if not hasattr(self,"save_behavior"):return
        settings=getattr(self.state,"settings",None)
        if settings is None:
            self._set_behavior_pending(False)
            self.behavior_status.setText("Application settings are unavailable.")
            return
        policy=AutomationBehaviorPolicy.from_settings(settings)
        self._behavior_saved_policy=policy
        self._set_behavior_controls(policy)
        self._clear_behavior_preset_preview()
        self._set_behavior_pending(False)
        if (
            self._defer_expensive_initialization
            and self._behavior_capability_result is None
        ):
            self.behavior_status.setText(
                "Saved. Checking installed model readiness in the background…"
            )
            self.behavior_summary.setText(policy.summary())
            return
        tag_ready,description_ready,_count=(
            self._behavior_capability_result
            if self._behavior_capability_result is not None else
            self._behavior_capabilities()
        )
        self._show_ai_setup_if_needed(tag_ready,description_ready)
        missing=[]
        if not tag_ready:missing.append("AI Tag model")
        if not description_ready:missing.append("AI Description model")
        self.behavior_status.setText(
            "Saved. Setup is missing a ready "+" and ".join(missing)
            +"; those enabled defaults wait without being erased."
            if missing else
            "Saved. Installed model and hardware prerequisites remain in Setup."
        )
        self.behavior_summary.setText(policy.summary())

    def _behavior_controls_changed(self,*_):
        if self._behavior_syncing:return
        policy=self._behavior_policy_from_controls()
        self.behavior_summary.setText(policy.summary())
        self._clear_behavior_preset_preview()
        self._set_behavior_pending(policy!=self._behavior_saved_policy)
        self.behavior_status.setText(
            "Pending changes are staged here and have not been saved or run."
            if self.save_behavior.isEnabled() else
            "Saved. Installed model and hardware prerequisites remain in Setup."
        )

    def _behavior_confirmation_changed(self,*_):
        active=self.behavior_auto_confirm.isChecked()
        self.behavior_consensus.setMinimum(2 if active else 1)
        self.behavior_consensus.setToolTip(
            "Each ready AI Tag model counts once; matching TXT or Metadata may "
            "provide one corroborating vote."
        )

    def _behavior_preparation_preset_changed(self,*_):
        if self._behavior_syncing:return
        preset=str(self.behavior_prepare_preset.currentData() or "custom")
        if preset in CONFIDENCE_PRESETS:
            policy=self._behavior_policy_from_controls().preparation_preset(preset)
            self._behavior_syncing=True
            try:
                self.behavior_prepare_votes.setValue(policy.prepare_tag_vote_minimum)
                self.behavior_prepare_external_vote.setChecked(
                    policy.prepare_external_evidence_vote
                )
            finally:
                self._behavior_syncing=False
        self._behavior_controls_changed()

    def _behavior_preparation_values_changed(self,*_):
        if self._behavior_syncing:return
        match="custom"
        for key,values in CONFIDENCE_PRESETS.items():
            if (
                self.behavior_prepare_votes.value()==int(values["minimum_tag_votes"])
                and self.behavior_prepare_external_vote.isChecked()
                ==bool(values["external_evidence_vote"])
            ):
                match=key; break
        self._behavior_syncing=True
        try:
            self.behavior_prepare_preset.setCurrentIndex(
                max(0,self.behavior_prepare_preset.findData(match))
            )
        finally:
            self._behavior_syncing=False
        self._behavior_controls_changed()

    def _preview_behavior_preset(self,*_):
        mode=str(self.behavior_preset.currentData() or "")
        if not mode:
            self._clear_behavior_preset_preview()
            self._set_behavior_pending(self.save_behavior.isEnabled())
            return
        current=self._behavior_policy_from_controls()
        tag_ready,description_ready,count=self._behavior_capabilities()
        self._show_ai_setup_if_needed(tag_ready,description_ready)
        proposed=current.workflow_preset(
            mode,tag_ready=tag_ready,description_ready=description_ready,
            enabled_tag_models=count,
        )
        changed=[
            key for key,value in proposed.to_dict().items()
            if value!=current.to_dict()[key]
        ]
        self._behavior_preview_policy=proposed
        self.stage_behavior_preset.setEnabled(bool(changed))
        self.discard_behavior.setEnabled(True)
        self.behavior_preset_changes.clear()
        for key in changed:
            self.behavior_preset_changes.addTopLevelItem(QTreeWidgetItem((
                BEHAVIOR_SETTING_LABELS[key],
                _behavior_display_value(key,getattr(current,key)),
                _behavior_display_value(key,getattr(proposed,key)),
            )))
        self.behavior_preset_changes.setVisible(bool(changed))
        notes=[]
        if not tag_ready:notes.append("AI Tag automation remains off until Setup has a ready Tag model.")
        if not description_ready:notes.append("AI Description automation remains off until Setup has a ready Description model.")
        self.behavior_preset_preview.setText(
            (f"Review {len(changed)} proposed changes to the current draft below." if changed else "This preset already matches the staged controls.")
            +("\n"+" ".join(notes) if notes else "")
            +"\nNothing changes until Stage Preset Changes; nothing is saved until Save AI / Prepare Defaults."
        )

    def _stage_behavior_preset(self):
        if self._behavior_preview_policy is None:return
        policy=self._behavior_preview_policy
        self._set_behavior_controls(policy)
        self._clear_behavior_preset_preview("Preset changes are staged. Save to make them active, or discard to restore saved defaults.")
        self._set_behavior_pending(policy!=self._behavior_saved_policy)
        self.behavior_summary.setText(policy.summary())
        self.behavior_status.setText(
            "Preset changes are staged and have not been saved or run."
        )

    def _save_behavior_settings(self):
        settings=getattr(self.state,"settings",None)
        if settings is None:return False
        previous=AutomationBehaviorPolicy.from_settings(settings)
        policy=self._behavior_policy_from_controls()
        policy.apply_to(settings)
        try:
            settings.save()
        except Exception as exc:
            previous.apply_to(settings)
            self._behavior_saved_policy=previous
            self._set_behavior_pending(policy!=previous)
            self.behavior_status.setText(
                "Pending changes were not saved; the prior verified behavior remains active."
            )
            QMessageBox.warning(
                self,"Automatic behavior was not saved",
                "The prior verified settings remain active.\n\n"+str(exc),
            )
            return False
        self._behavior_saved_policy=policy
        self._clear_behavior_preset_preview()
        self._set_behavior_pending(False)
        self.behavior_status.setText(
            "Saved. New automatic work uses this behavior; no job was started."
        )
        self.behavior_summary.setText(policy.summary())
        self.behavior_changed.emit()
        return True

    def _refresh_external_write_settings(self):
        if not hasattr(self,"external_write_backup_mode"):return
        settings=getattr(self.state,"settings",None)
        policy=ExternalWritePolicy.from_settings(settings)
        self._external_write_syncing=True
        try:
            index=self.external_write_backup_mode.findData(policy.backup_mode)
            self.external_write_backup_mode.setCurrentIndex(max(0,index))
        finally:
            self._external_write_syncing=False
        directory=policy.configured_backup_directory
        self.external_write_backup_directory.setText(
            f"Backup folder: {directory}"
            if directory is not None else "Backup folder: Not configured"
        )
        registry=(
            settings.library_registry() if settings is not None else None
        )
        issues=policy.backup_destination_issues(
            registry,enabled=policy.backup_mode!=EXTERNAL_WRITE_BACKUP_OFF,
        )
        available=settings is not None
        self.external_write_backup_mode.setEnabled(available)
        self.choose_external_write_backup.setEnabled(available)
        self.clear_external_write_backup.setEnabled(
            available and directory is not None
        )
        if not available:
            status="Application settings are unavailable in this maintenance context."
        elif policy.backup_mode==EXTERNAL_WRITE_BACKUP_OFF:
            status=(
                "Off. New external writes create no protected recovery; earlier "
                "protected operations remain available."
            )
        elif issues:
            status="Protected external writes are blocked: "+" ".join(issues)
        elif policy.backup_mode==EXTERNAL_WRITE_BACKUP_ASK:
            status=(
                "Ask for each operation. Backup is selected by default; the "
                "exact preview shows whether restore will be available."
            )
        else:
            status=(
                "Always protect replaced external files. Backup creation and "
                "read-back must succeed before the first target changes."
            )
        self.external_write_backup_status.setText(status)

    def _save_external_write_policy(self,mode,directory):
        settings=getattr(self.state,"settings",None)
        if settings is None:return False
        previous=(
            getattr(settings,"external_write_backup_mode","always"),
            getattr(settings,"external_write_backup_directory",""),
        )
        settings.external_write_backup_mode=mode
        settings.external_write_backup_directory=str(directory or "")
        try:
            settings.save()
        except Exception as exc:
            (
                settings.external_write_backup_mode,
                settings.external_write_backup_directory,
            )=previous
            self._refresh_external_write_settings()
            QMessageBox.warning(
                self,"External-write protection was not saved",str(exc),
            )
            return False
        self._refresh_external_write_settings()
        return True

    def _save_external_write_mode(self,*_):
        if self._external_write_syncing:return
        settings=getattr(self.state,"settings",None)
        if settings is None:return
        self._save_external_write_policy(
            self.external_write_backup_mode.currentData(),
            getattr(settings,"external_write_backup_directory",""),
        )

    def _choose_external_write_backup_directory(self):
        from .folder_locations import open_folder_locations
        open_folder_locations(self, focus="external_write_backup_directory")
        self._refresh_external_write_settings()

    def _clear_external_write_backup_directory(self):
        self._choose_external_write_backup_directory()

    def _refresh_fact_expansion_settings(self):
        if not hasattr(self,"fact_expansion_enabled"):return
        settings=getattr(self.state,"settings",None)
        policy=DeterministicFactExpansionPolicy.from_settings(settings)
        self._fact_expansion_syncing=True
        try:
            self.fact_expansion_enabled.setChecked(policy.enabled)
            enabled_kinds=set(policy.relationship_kinds)
            for kinds,checkbox in self.fact_expansion_relationship_checks.items():
                checkbox.setChecked(bool(set(kinds)&enabled_kinds))
            self.fact_expansion_depth.setValue(policy.max_depth)
        finally:
            self._fact_expansion_syncing=False
        available=settings is not None
        for widget in (
            self.fact_expansion_enabled,
            self.fact_expansion_depth,
            *self.fact_expansion_relationship_checks.values(),
        ):
            widget.setEnabled(available)
        if not available:
            self.fact_expansion_status.setText(
                "Application settings are unavailable in this maintenance context."
            )
        elif not policy.enabled:
            self.fact_expansion_status.setText(
                "Off by default. Related tags remain available for ordinary manual review."
            )
        elif not policy.relationship_kinds:
            self.fact_expansion_status.setText(
                "The policy is enabled, but no reviewed relationship types are selected, so no tags qualify."
            )
        else:
            self.fact_expansion_status.setText(
                f"Enabled with {len(policy.relationship_kinds):,} relationship type(s) "
                f"and a maximum depth of {policy.max_depth}. Exact additions are always previewed."
            )

    def _save_fact_expansion_settings(self,*_):
        if self._fact_expansion_syncing:return
        settings=getattr(self.state,"settings",None)
        if settings is None:return
        previous=(
            bool(getattr(settings,"fact_expansion_enabled",False)),
            list(getattr(settings,"fact_expansion_relationships",[])),
            int(getattr(settings,"fact_expansion_max_depth",FACT_EXPANSION_MIN_DEPTH)),
        )
        selected=[]
        for kinds,checkbox in self.fact_expansion_relationship_checks.items():
            if checkbox.isChecked():selected.extend(kinds)
        settings.fact_expansion_enabled=self.fact_expansion_enabled.isChecked()
        settings.fact_expansion_relationships=selected
        settings.fact_expansion_max_depth=self.fact_expansion_depth.value()
        try:
            settings.save()
        except Exception as exc:
            (
                settings.fact_expansion_enabled,
                settings.fact_expansion_relationships,
                settings.fact_expansion_max_depth,
            )=previous
            self._refresh_fact_expansion_settings()
            QMessageBox.warning(
                self,
                "Fact-expansion settings were not saved",
                str(exc),
            )
            return
        self._refresh_fact_expansion_settings()

    def _build_rules_tab(self):
        page=QWidget(); self.rules_tab=page; layout=QVBoxLayout(page)
        distinction=QLabel(
            "Rules build training captions, screen incoming evidence, or flag conflicts "
            "for review. Saved image facts stay unchanged."
        )
        distinction.setWordWrap(True); distinction.setObjectName("mutedLabel"); layout.addWidget(distinction)
        goals=QWidget(); goal_layout=QVBoxLayout(goals); goal_layout.setContentsMargins(0,0,0,0)
        goals.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Maximum)
        goal_heading=QLabel("What would you like to change?"); goal_heading.setObjectName("sectionTitle")
        goal_layout.addWidget(goal_heading)
        goal_choices=QHBoxLayout(); self.rule_goal_buttons={}
        for key,label in (
            ("captions","Build Training Captions"),
            ("screen","Screen Incoming Tags"),
            ("conflicts","Flag Conflicts"),
            ("behavior","Adjust Automatic Behavior"),
            ("all","Browse All Rules…"),
        ):
            button=QPushButton(label); button.setAutoDefault(False)
            button.clicked.connect(lambda _checked=False, selected=key:self._select_rule_goal(selected))
            goal_choices.addWidget(button); self.rule_goal_buttons[key]=button
        goal_layout.addLayout(goal_choices)
        self.rule_goal_explanation=QLabel("Choose a goal to see its rules. Browse all rules for the full inventory and advanced controls.")
        self.rule_goal_explanation.setWordWrap(True); self.rule_goal_explanation.setObjectName("mutedLabel")
        goal_layout.addWidget(self.rule_goal_explanation); layout.addWidget(goals)
        self._rule_goal=None
        self._suggestions_visible=False
        self.intent_migration_panel=QWidget(); migration_layout=QHBoxLayout(self.intent_migration_panel); migration_layout.setContentsMargins(0,0,0,0)
        self.intent_migration_status=QLabel(); self.intent_migration_status.setWordWrap(True); self.intent_migration_status.setObjectName("mutedLabel")
        self.intent_migration_review=QPushButton("Review Legacy Rule Intent…"); self.intent_migration_review.setProperty("primary",True); self.intent_migration_review.clicked.connect(self.review_rule_intent_migration)
        self.intent_migration_undo=QPushButton("Restore Last Intent Migration"); self.intent_migration_undo.clicked.connect(self.undo_rule_intent_migration)
        migration_layout.addWidget(self.intent_migration_status,1); migration_layout.addWidget(self.intent_migration_review); migration_layout.addWidget(self.intent_migration_undo)
        layout.addWidget(self.intent_migration_panel)
        self.projection_repair_panel=QWidget(); repair_layout=QHBoxLayout(self.projection_repair_panel); repair_layout.setContentsMargins(0,0,0,0)
        self.projection_repair_status=QLabel(); self.projection_repair_status.setWordWrap(True); self.projection_repair_status.setObjectName("mutedLabel")
        self.projection_repair_review=QPushButton("Review Related Rule Repairs…"); self.projection_repair_review.setProperty("primary",True); self.projection_repair_review.clicked.connect(self.review_projection_rule_repairs)
        self.projection_repair_undo=QPushButton("Restore Last Rule Repair"); self.projection_repair_undo.clicked.connect(self.undo_projection_rule_repair)
        repair_layout.addWidget(self.projection_repair_status,1); repair_layout.addWidget(self.projection_repair_review); repair_layout.addWidget(self.projection_repair_undo)
        layout.addWidget(self.projection_repair_panel)
        self.legacy_recovery_panel=QWidget(); legacy_layout=QHBoxLayout(self.legacy_recovery_panel); legacy_layout.setContentsMargins(0,0,0,0)
        self.legacy_recovery_status=QLabel(
            "A historical Automation batch remains recoverable. Current projection rules never rewrite Image Knowledge."
        )
        self.legacy_recovery_status.setWordWrap(True); self.legacy_recovery_status.setObjectName("mutedLabel")
        self.legacy_recovery_button=QPushButton("Restore Historical Automation Batch…"); self.legacy_recovery_button.clicked.connect(self.undo)
        legacy_layout.addWidget(self.legacy_recovery_status,1); legacy_layout.addWidget(self.legacy_recovery_button)
        self.legacy_recovery_panel.setVisible(self.checkpoint.exists()); layout.addWidget(self.legacy_recovery_panel)
        self.rule_task_status=QLabel(); self.rule_task_status.setWordWrap(True); self.rule_task_status.setObjectName("mutedLabel")
        self.rule_inventory=QWidget(); rule_layout=QVBoxLayout(self.rule_inventory)
        rule_layout.setContentsMargins(0,0,0,0)
        self.rule_heading=QLabel("Merge and Ignore Rules"); self.rule_heading.setObjectName("sectionTitle"); rule_layout.addWidget(self.rule_heading)
        self.rule_start_actions=QHBoxLayout()
        self.add_rule_button=QPushButton("Add Rule…"); self.add_rule_button.setProperty("primary",True); self.add_rule_button.clicked.connect(self.add_rule)
        self.suggest_button=QPushButton("Suggest Merges"); self.suggest_button.clicked.connect(self.suggest_rules)
        self.rule_start_actions.addWidget(self.add_rule_button); self.rule_start_actions.addWidget(self.suggest_button); self.rule_start_actions.addStretch(1)
        rule_layout.addLayout(self.rule_start_actions)
        self.rule_search=QLineEdit(); self.rule_search.setPlaceholderText("Filter merge and ignore rules…"); self.rule_search.textChanged.connect(self.filter_rules); rule_layout.addWidget(self.rule_search)
        self.rule_scope_status=QLabel(); self.rule_scope_status.setObjectName("mutedLabel")
        self.rule_scope_status.setWordWrap(True); rule_layout.addWidget(self.rule_scope_status)
        self.rule_tree=QTreeWidget(); self.rule_tree.setHeaderLabels(("Rule meaning","When these SDXL Tags appear","Result","Origin")); self.rule_tree.setRootIsDecorated(False); self.rule_tree.setAlternatingRowColors(False)
        self.rule_tree.setSelectionMode(QAbstractItemView.ExtendedSelection); self.rule_tree.setAccessibleName("Merge and ignore rules")
        self.rule_tree.itemSelectionChanged.connect(self._rule_selection_changed)
        self.rule_tree.itemDoubleClicked.connect(lambda *_:self.edit_rule()); rule_layout.addWidget(self.rule_tree,1)
        self.rule_selection_status=QLabel("No rules selected"); self.rule_selection_status.setObjectName("mutedLabel"); self.rule_selection_status.setWordWrap(True); self.rule_selection_status.setAccessibleName("Rule selection")
        rule_layout.addWidget(self.rule_selection_status)
        self.rule_selection_actions=QHBoxLayout()
        self.edit_rule_button=QPushButton("Edit…"); self.edit_rule_button.clicked.connect(self.edit_rule)
        self.delete_rule_button=QPushButton("Delete"); self.delete_rule_button.clicked.connect(self.delete_rule)
        self.rule_selection_actions.addStretch(1); self.rule_selection_actions.addWidget(self.edit_rule_button); self.rule_selection_actions.addWidget(self.delete_rule_button)
        rule_layout.addLayout(self.rule_selection_actions)
        self.rule_cancel_button=QPushButton("Cancel Safely"); self.rule_cancel_button.clicked.connect(self.cancel_rule_task); self.rule_cancel_button.hide()
        bind_status_footer(
            page,self.rule_task_status,self.rule_cancel_button,
            title="Automation",follow_visibility=True,
        )

        self.suggestion_panel=QWidget(); suggestion_layout=QVBoxLayout(self.suggestion_panel); suggestion_layout.setContentsMargins(0,8,0,4)
        suggestion_header=QHBoxLayout(); self.suggestion_summary=QLabel(); self.suggestion_summary.setObjectName("mutedLabel"); suggestion_header.addWidget(self.suggestion_summary); suggestion_header.addStretch(1)
        hide_suggestions=QPushButton("Hide"); hide_suggestions.setProperty("compact",True); hide_suggestions.clicked.connect(self.hide_rule_suggestions); suggestion_header.addWidget(hide_suggestions); suggestion_layout.addLayout(suggestion_header)
        self.suggestion_tree=QTreeWidget(); self.suggestion_tree.setHeaderLabels(("When these SDXL Tags appear","Keep","Images","Examples")); self.suggestion_tree.setRootIsDecorated(False); self.suggestion_tree.itemDoubleClicked.connect(lambda *_:self.use_rule_suggestion())
        self.suggestion_tree.setColumnWidth(0,260); self.suggestion_tree.setColumnWidth(1,180); self.suggestion_tree.setColumnWidth(2,80)
        self.suggestion_tree.header().setStretchLastSection(True); suggestion_layout.addWidget(self.suggestion_tree)
        use_suggestion=QPushButton("Review Suggestion"); use_suggestion.setProperty("primary",True); use_suggestion.setProperty("compact",True); use_suggestion.clicked.connect(self.use_rule_suggestion)
        suggestion_actions=QHBoxLayout(); suggestion_actions.addStretch(1); suggestion_actions.addWidget(use_suggestion); suggestion_layout.addLayout(suggestion_actions)
        self.suggestion_panel.hide(); rule_layout.addWidget(self.suggestion_panel,1)
        layout.addWidget(self.rule_inventory,1)

        self.conflict_inventory=QWidget(); conflict_layout=QVBoxLayout(self.conflict_inventory)
        conflict_layout.setContentsMargins(0,0,0,0)
        conflict_heading=QLabel("Diagnostic conflict rules"); conflict_heading.setObjectName("sectionTitle"); conflict_layout.addWidget(conflict_heading)
        conflict_hint=QLabel(
            "Conflict rules flag possible mismatches for review. They never change "
            "images or captions."
        )
        conflict_hint.setWordWrap(True); conflict_hint.setObjectName("mutedLabel"); conflict_layout.addWidget(conflict_hint)
        conflict_filters = QHBoxLayout()
        self.conflict_search = QLineEdit()
        self.conflict_search.setPlaceholderText("Find conflict rules by name or Tag…")
        self.conflict_search.setClearButtonEnabled(True)
        self.conflict_lifecycle = QComboBox()
        self.conflict_lifecycle.addItem("Active rules", "active")
        self.conflict_lifecycle.addItem("Disabled rules", "disabled")
        self.conflict_lifecycle.addItem("All rules", "all")
        conflict_filters.addWidget(self.conflict_search, 1)
        conflict_filters.addWidget(self.conflict_lifecycle)
        conflict_layout.addLayout(conflict_filters)
        conflict_progress=QHBoxLayout()
        self.conflict_loading_status=QLabel(); self.conflict_loading_status.setWordWrap(True)
        self.conflict_loading_status.setAccessibleName("Conflict rule status")
        self.conflict_retry=QPushButton("Retry Loading Rules")
        self.conflict_retry.clicked.connect(self._reload_conflict_projection)
        self.conflict_retry.hide()
        conflict_progress.addWidget(self.conflict_loading_status,1)
        conflict_progress.addWidget(self.conflict_retry)
        conflict_layout.addLayout(conflict_progress)
        self.conflict_search.textChanged.connect(self._filter_conflict_rules)
        self.conflict_lifecycle.currentIndexChanged.connect(self._filter_conflict_rules)
        self.diagnostic_conflict_tree=QTreeWidget(); self.diagnostic_conflict_tree.setHeaderLabels(("Conflict","SDXL Tags","Compare Tags","Allow when")); self.diagnostic_conflict_tree.setRootIsDecorated(False); self.diagnostic_conflict_tree.setMinimumHeight(220); self.diagnostic_conflict_tree.setMaximumHeight(320); self.diagnostic_conflict_tree.setAccessibleName("Diagnostic conflict rules")
        self.diagnostic_conflict_tree.itemDoubleClicked.connect(lambda *_:self.open_conflict_rule_workspace())
        conflict_layout.addWidget(self.diagnostic_conflict_tree)
        conflict_actions=QHBoxLayout(); self.new_conflict_rule=QPushButton("New Conflict Rule…"); self.new_conflict_rule.setProperty("compact",True); self.new_conflict_rule.clicked.connect(lambda:self.open_conflict_rule_workspace(start_new=True)); self.edit_conflict_rule=QPushButton("Edit Conflict Rule…"); self.edit_conflict_rule.setProperty("primary",True); self.edit_conflict_rule.setProperty("compact",True); self.edit_conflict_rule.clicked.connect(self.open_conflict_rule_workspace); conflict_actions.addWidget(self.new_conflict_rule); conflict_actions.addStretch(1); conflict_actions.addWidget(self.edit_conflict_rule); conflict_layout.addLayout(conflict_actions)
        conflict_layout.addStretch(1)
        self.diagnostic_conflict_tree.itemSelectionChanged.connect(self._conflict_selection_changed)
        self.edit_conflict_rule.setEnabled(False)
        layout.addWidget(self.conflict_inventory,1)
        self.rule_start_spacer=QWidget(); layout.addWidget(self.rule_start_spacer,1)
        self.tabs.addTab(page,"Rules")
        self._select_rule_goal(None)

    def _select_rule_goal(self, goal):
        if goal=="behavior":
            self.settings_navigation.select_section("ai")
            return
        if goal not in (None,"captions","screen","conflicts","all"):
            return
        if goal!=self._rule_goal:
            self.rule_tree.clearSelection()
            self.diagnostic_conflict_tree.clearSelection()
        self._rule_goal=goal
        self.rule_inventory.setVisible(goal in ("captions","screen","all"))
        self.conflict_inventory.setVisible(goal in ("conflicts","all"))
        self.rule_start_spacer.setVisible(goal is None)
        explanations={
            None:"Choose a goal to see its rules. Browse all rules for the full inventory and advanced controls.",
            "captions":"Training-projection rules shape concise exported captions. Confirmed image facts remain unchanged.",
            "screen":"Screen a known incoming Tag from future TXT, Metadata or AI suggestions. Existing confirmed image facts are not deleted.",
            "conflicts":"Diagnostic rules flag possible mismatches for review. They do not change images or captions.",
            "all":"Browse every rule and its exact authority. Select a rule to inspect or edit it.",
        }
        self.rule_goal_explanation.setText(explanations[goal])
        for key,button in self.rule_goal_buttons.items():
            button.setProperty("primary",key==goal)
            button.style().unpolish(button); button.style().polish(button)
        self.rule_heading.setText("Incoming Tag Screening Rules" if goal=="screen" else "Training Caption Rules" if goal=="captions" else "Merge and Ignore Rules")
        self.rule_search.setPlaceholderText("Filter screening rules…" if goal=="screen" else "Filter caption rules…" if goal=="captions" else "Filter merge and ignore rules…")
        self.add_rule_button.setText("Add Screening Rule…" if goal=="screen" else "Add Caption Rule…" if goal=="captions" else "Add Rule…")
        self.suggest_button.setVisible(goal in ("captions","all"))
        self._sync_rule_suggestions_view()
        if self._suggestions_visible:
            self.header.set_status(
                "Merge suggestions are ready" if goal in ("captions","all")
                else "Caption merge suggestions are available; open Build Training Captions to review them"
            )
        self.filter_rules()

    def _sync_rule_suggestions_view(self):
        showing=self._suggestions_visible and self._rule_goal in ("captions","all")
        self.suggestion_panel.setVisible(showing)
        self.rule_tree.setVisible(not showing)
        self.rule_selection_status.setVisible(not showing)
        self.edit_rule_button.setVisible(not showing)
        self.delete_rule_button.setVisible(not showing)

    def _build_values_tab(self):
        page=QWidget(); self.values_tab=page; outer=QVBoxLayout(page)
        self.value_sections=QTabWidget(); self.value_sections.setAccessibleName("Reusable Tag sections")
        outer.addWidget(self.value_sections,1)
        templates=QWidget(); layout=QVBoxLayout(templates)
        self.value_sections.addTab(templates,"Reviewed Templates")
        legacy_group=QGroupBox("Legacy wildcard compatibility")
        legacy_layout=QVBoxLayout(legacy_group)
        intro=QLabel(
            "These exact string lists keep older {name} Automation rules "
            "working. They are not confirmed Tags and are never converted, "
            "renamed, or deleted without a reviewed compatibility migration."
        )
        intro.setWordWrap(True); legacy_layout.addWidget(intro)
        self.value_authority_status=QLabel()
        self.value_authority_status.setObjectName("mutedLabel")
        self.value_authority_status.setWordWrap(True)
        self.value_authority_status.setAccessibleName(
            "Legacy wildcard value authority review"
        )
        legacy_layout.addWidget(self.value_authority_status)
        self.value_binding_history_panel=QWidget()
        history_layout=QHBoxLayout(self.value_binding_history_panel)
        history_layout.setContentsMargins(0,0,0,0)
        self.value_binding_history_status=QLabel()
        self.value_binding_history_status.setWordWrap(True)
        self.value_binding_history_status.setObjectName("mutedLabel")
        self.value_binding_undo=QPushButton("Restore Last Authority Decision")
        self.value_binding_undo.clicked.connect(
            self.undo_variable_binding_migration
        )
        history_layout.addWidget(self.value_binding_history_status,1)
        history_layout.addWidget(self.value_binding_undo)
        legacy_layout.addWidget(self.value_binding_history_panel)
        self.value_tree=QTreeWidget()
        self.value_tree.setHeaderLabels((
            "Legacy value list","Recognized strings","Authority review","Consumers",
        ))
        self.value_tree.setAccessibleName("Legacy wildcard compatibility lists")
        self.value_tree.setRootIsDecorated(False)
        self.value_tree.setAlternatingRowColors(False)
        self.value_tree.itemSelectionChanged.connect(self._variable_selection_changed)
        self.value_tree.itemDoubleClicked.connect(
            lambda *_:self.preview_variable_migration()
        )
        self.value_tree.header().setSectionResizeMode(0,QHeaderView.ResizeToContents)
        self.value_tree.header().setSectionResizeMode(1,QHeaderView.Stretch)
        self.value_tree.header().setSectionResizeMode(2,QHeaderView.ResizeToContents)
        self.value_tree.header().setSectionResizeMode(3,QHeaderView.ResizeToContents)
        legacy_layout.addWidget(self.value_tree,1)
        legacy_actions=QHBoxLayout()
        self.add_value_button=QPushButton("Add Legacy List…")
        self.add_value_button.clicked.connect(self.add_variable)
        self.edit_value_button=QPushButton("Edit…")
        self.edit_value_button.clicked.connect(self.edit_variable)
        self.delete_value_button=QPushButton("Delete")
        self.delete_value_button.clicked.connect(self.delete_variable)
        self.preview_value_migration_button=QPushButton("Review Compatibility…")
        self.preview_value_migration_button.clicked.connect(
            self.preview_variable_migration
        )
        self.preview_value_migration_button.setEnabled(False)
        self.review_value_authority_button=QPushButton("Decide Authority…")
        self.review_value_authority_button.clicked.connect(
            self.review_variable_authority
        )
        self.review_value_authority_button.setEnabled(False)
        legacy_actions.addWidget(self.add_value_button)
        legacy_actions.addWidget(self.edit_value_button)
        legacy_actions.addWidget(self.delete_value_button)
        legacy_actions.addStretch(1)
        legacy_actions.addWidget(self.review_value_authority_button)
        legacy_actions.addWidget(self.preview_value_migration_button)
        legacy_layout.addLayout(legacy_actions)
        self.value_sections.addTab(legacy_group,"Legacy Compatibility")
        discovery=QLabel(
            "Build reviewed Tag patterns for identifying images and preparing "
            "training captions. Approval saves a disabled pattern; it does "
            "not create Tags or tag images."
        )
        template_group=QGroupBox("Reviewed reusable Tag templates")
        template_layout=QVBoxLayout(template_group)
        discovery.setObjectName("mutedLabel"); discovery.setWordWrap(True)
        discovery.setToolTip(
            "Patterns use stable Tag anchors and reviewed typed modifier values. "
            "Discovery compares exact canonical names and declared Other Names. "
            "Approval changes no Image Knowledge."
        )
        discovery.setAccessibleDescription(discovery.toolTip())
        template_layout.addWidget(discovery)
        self.template_tree=QTreeWidget(); self.template_tree.setHeaderLabels(("Template","Stable anchor","Dimension","Selected","Version")); self.template_tree.setRootIsDecorated(False); self.template_tree.setMaximumHeight(180); self.template_tree.setAccessibleName("Reviewed reusable Tag templates")
        self.template_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.template_tree.itemSelectionChanged.connect(self._template_selection_changed)
        self.template_tree.itemDoubleClicked.connect(
            lambda *_:self.review_selected_tag_template()
        )
        template_layout.addWidget(self.template_tree)
        self.edit_template_button=QPushButton("Edit Template…")
        self.edit_template_button.setProperty("compact",True)
        self.edit_template_button.clicked.connect(
            lambda:self.build_reusable_tag_template(
                self._selected_tag_template_record(),
            )
        )
        self.review_template_button=QPushButton("Review Saved Template…")
        self.review_template_button.setProperty("compact",True)
        self.review_template_button.clicked.connect(self.review_selected_tag_template)
        self.preview_template_organization_button=QPushButton(
            "Preview Organization Plan…"
        )
        self.preview_template_organization_button.setProperty("compact",True)
        self.preview_template_organization_button.clicked.connect(
            self.preview_selected_tag_template_organization
        )
        self.create_projection_rule_button=QPushButton(
            "Create and Add Projection Rule…"
        )
        self.create_projection_rule_button.setProperty("compact",True)
        self.create_projection_rule_button.clicked.connect(
            lambda:self.preview_selected_tag_template_organization(
                action="projection",
            )
        )
        self.review_template_policy_button=QPushButton("Review Policies…")
        self.review_template_policy_button.setProperty("compact",True)
        self.review_template_policy_button.clicked.connect(
            lambda:self.preview_selected_tag_template_organization(
                action="policies",
            )
        )
        self.run_template_generation_button=QPushButton("Run Reviewed Generation…")
        self.run_template_generation_button.setProperty("compact",True)
        self.run_template_generation_button.clicked.connect(
            lambda:self.preview_selected_tag_template_organization(
                action="generation",
            )
        )
        self.template_storage_review=QPushButton("Review Template Storage…")
        self.template_storage_review.setProperty("compact",True)
        self.template_storage_review.setAccessibleName(
            "Review held reusable Tag template storage"
        )
        self.template_storage_review.clicked.connect(self._review_template_storage_issue)
        self.template_storage_review.hide()
        template_actions=QHBoxLayout(); template_actions.addWidget(self.edit_template_button)
        template_actions.addWidget(self.review_template_button)
        template_actions.addWidget(self.preview_template_organization_button)
        template_actions.addStretch(1)
        template_actions.addWidget(self.template_storage_review)
        template_layout.addLayout(template_actions)
        automation_actions=QHBoxLayout(); automation_actions.addStretch(1)
        automation_actions.addWidget(self.create_projection_rule_button)
        automation_actions.addWidget(self.review_template_policy_button)
        automation_actions.addWidget(self.run_template_generation_button)
        template_layout.addLayout(automation_actions)
        layout.addWidget(template_group,0,Qt.AlignTop)
        self.template_organization_history_panel=QWidget()
        history_layout=QHBoxLayout(self.template_organization_history_panel)
        history_layout.setContentsMargins(0,0,0,0)
        self.template_organization_history_status=QLabel()
        self.template_organization_history_status.setObjectName("mutedLabel")
        self.template_organization_history_status.setWordWrap(True)
        self.template_organization_undo=QPushButton("Restore Last Template Organization")
        self.template_organization_undo.clicked.connect(
            self.undo_tag_template_organization
        )
        history_layout.addWidget(self.template_organization_history_status,1)
        history_layout.addWidget(self.template_organization_undo)
        layout.addWidget(self.template_organization_history_panel)
        self.template_task_status=QLabel(); self.template_task_status.setObjectName("mutedLabel"); self.template_task_status.setWordWrap(True)
        if self._template_recovery_error:
            self.template_task_status.setText(
                "Reusable Tag recovery needs review: "+self._template_recovery_error
            )
        elif self._template_recovery_notice:
            self.template_task_status.setText(self._template_recovery_notice)
        row=QHBoxLayout()
        self.discover_templates_button=QPushButton("Discover Reusable Tag Families…"); self.discover_templates_button.setProperty("primary",True); self.discover_templates_button.clicked.connect(self.discover_reusable_tag_families)
        self.build_template_button=QPushButton("Build Reusable Tags…")
        self.build_template_button.setProperty("primary",True)
        self.build_template_button.clicked.connect(
            lambda:self.build_reusable_tag_template()
        )
        self.template_cancel_button=QPushButton("Cancel Safely"); self.template_cancel_button.clicked.connect(self.cancel_rule_task); self.template_cancel_button.hide()
        bind_status_footer(
            page,self.template_task_status,self.template_cancel_button,
            title="Automation",follow_visibility=True,
        )
        row.addStretch(1); row.addWidget(self.discover_templates_button); row.addWidget(self.build_template_button); layout.addLayout(row); self.tabs.addTab(page,"Reusable Values && Tags")

        packs=QWidget(); packs_layout=QVBoxLayout(packs)
        pack_note=QLabel("Transfer custom Tags, categories, characters and reusable knowledge. Images and built-in knowledge are not included.")
        pack_note.setWordWrap(True); packs_layout.addWidget(pack_note)
        self.import_knowledge_pack_button=QPushButton("Import Knowledge Pack…")
        self.export_knowledge_pack_button=QPushButton("Export Knowledge Pack…")
        self.import_knowledge_pack_button.clicked.connect(self.import_knowledge_pack)
        self.export_knowledge_pack_button.clicked.connect(self.export_knowledge_pack)
        pack_row=QHBoxLayout(); pack_row.addWidget(self.import_knowledge_pack_button)
        pack_row.addWidget(self.export_knowledge_pack_button); pack_row.addStretch(1)
        packs_layout.addLayout(pack_row); packs_layout.addStretch(1)
        self.value_sections.addTab(packs,"Knowledge Packs")

    def open_function(self,function_id,scope=None):
        """Open one stable Automation destination without starting its work."""
        controller=getattr(self.window(),"command_controller",None)
        if function_id=="knowledge_packs" and controller is not None:
            controller.reveal_group(5,"Knowledge Packs")
            return
        exact={
            "automation.prepare":("prepare","behavior_prepare_read_external"),
            "automation.prepare.fingerprints":("prepare","behavior_prepare_fingerprints"),
            "automation.prepare.reuse":("prepare","behavior_prepare_reuse_value"),
            "automation.after_checks":("after_checks","behavior_auto_wd"),
            "automation.ai_defaults":("ai","behavior_use_wd"),
            "automation.tag_thresholds":("thresholds","behavior_general_threshold"),
            "automation.description_ignores":("ai","description_exclusions_button"),
            "automation.library_checks.scope":("libraries","library_check_subfolders"),
            "automation.organization.containers":("organization",None),
            "knowledge_packs":("reusable","import_knowledge_pack_button"),
        }
        if function_id in exact:
            section,attribute=exact[function_id]
            self.settings_navigation.select_section(section)
            if function_id=="automation.organization.containers":
                self.organization_detail_tabs.setCurrentIndex(1)
            elif attribute:
                SettingsNavigator._reveal(getattr(self,attribute))
            self.header.set_status("Settings are ready to review; no work was started.")
            return True
        function_id=str(function_id or "rules")
        scope=dict(scope or {}) if isinstance(scope,dict) else {}
        destinations={
            "conflicts.guided_checks":getattr(self,"rules_tab",None),
            "rules":getattr(self,"rules_tab",None),
            "reusable_values":getattr(self,"values_tab",None),
            "templates.discover":getattr(self,"values_tab",None),
            "automation.settings":self.fact_expansion_tab,
            "automation.behavior.ai":self.fact_expansion_tab,
            "automation.library_checks":self.fact_expansion_tab,
            "automation.evidence.intake":self.fact_expansion_tab,
            "automation.export_content":self.fact_expansion_tab,
            "automation.organization":self.fact_expansion_tab,
            "automation.fact_expansion":self.fact_expansion_tab,
            "automation.external_writes":self.fact_expansion_tab,
        }
        destination=destinations.get(function_id)
        if destination is None:return False
        if function_id in {"rules","conflicts.guided_checks"}:
            self.settings_navigation.select_section("rules")
        index=self.tabs.indexOf(destination)
        if index>=0:self.tabs.setCurrentIndex(index)
        policy_sections={
            "automation.settings":"ai",
            "automation.behavior.ai":"ai",
            "automation.library_checks":"libraries",
            "automation.evidence.intake":"libraries",
            "automation.export_content":"exports",
            "automation.external_writes":"protection",
            "automation.organization":"organization",
            "automation.fact_expansion":"related_tags",
        }
        if function_id in policy_sections:
            self.policy_sections.setCurrentIndex(
                self.policy_section_indexes[policy_sections[function_id]]
            )
        if function_id=="automation.organization":
            self.organization_detail_tabs.setCurrentIndex(0)
        if function_id!="conflicts.guided_checks":
            self._pending_conflict_editor_open=False
            self.header.set_status("")
        if function_id=="conflicts.guided_checks":
            self._select_rule_goal("conflicts")
            self._pending_conflict_editor_open=bool(scope.get("open_editor"))
            self._conflict_focus_tag_id=str(scope.get("tag_id") or "")
            self._conflict_focus_relationship_id=str(
                scope.get("relationship_id") or ""
            )
            self._focus_conflict_tree()
            self.header.set_status(
                "Review diagnostic conflict rules below. No image, caption, Metadata, or projection was changed."
            )
            self.diagnostic_conflict_tree.setFocus(Qt.OtherFocusReason)
            if scope.get("open_editor"):
                self._pending_conflict_editor_open=True
                if self._conflict_projection is not None:
                    QTimer.singleShot(0,self._open_pending_conflict_editor)
        elif function_id=="rules":
            self._select_rule_goal("all")
        elif function_id=="automation.behavior.ai":
            self.behavior_detail_tabs.setCurrentIndex(0)
            self.header.set_status(
                "Automatic AI behavior is ready to review; no job was started."
            )
            self.behavior_preset.setFocus(Qt.OtherFocusReason)
        elif function_id=="automation.library_checks":
            self.header.set_status(
                "Library-check trigger and scope are ready to review; no check was started."
            )
            self.library_check_startup.setFocus(Qt.OtherFocusReason)
        elif function_id=="automation.evidence.intake":
            self.header.set_status(
                "External evidence intake policy is ready to review; no import was started."
            )
            self.evidence_intake_auto.setFocus(Qt.OtherFocusReason)
        elif function_id=="automation.export_content":
            self.header.set_status(
                "Dataset export content policy is ready to review; no export was started."
            )
            self.export_content_rating.setFocus(Qt.OtherFocusReason)
        elif function_id=="automation.organization":
            self.header.set_status(
                "Organization policy is ready to review; no image was scanned or moved."
            )
            self.organization_naming.setFocus(Qt.OtherFocusReason)
        elif function_id=="automation.fact_expansion":
            self.header.set_status(
                "Related-Tag expansion policy is ready to review; no image was changed."
            )
            self.fact_expansion_enabled.setFocus(Qt.OtherFocusReason)
        elif function_id=="automation.external_writes":
            self.header.set_status(
                "External-write protection is ready to review; no file was changed."
            )
            self.external_write_backup_mode.setFocus(Qt.OtherFocusReason)
        elif function_id=="templates.discover":
            self.header.set_status(
                "Reusable Tag family discovery is ready. No scan starts until you choose Discover Reusable Tag Families."
            )
            self.discover_templates_button.setFocus(Qt.OtherFocusReason)
        return True

    def discover_reusable_tag_families(self):
        """Run one bounded, read-only System Knowledge discovery report."""
        if not self._start_rule_task(
            "Preparing an exact reusable Tag family report…",cancellable=True,
            context="Reusable Tag family discovery",
        ):
            return
        tag_path=self.state.knowledge.path

        def discover(progress,cancelled):
            # Use a worker-owned snapshot so authority publication and indexing
            # never mutate the live UI's TagDatabase from the worker thread.
            tags=TagDatabase(tag_path, rule_pair_admission=_rules_admission_for_knowledge(tag_path))
            categories=CategoryRegistry(
                DEFAULTS_DIR/"categories.json",CATEGORIES_PATH,
            )
            foundation=FoundationKnowledge(
                DEFAULTS_DIR/"modifiers.json",DEFAULTS_DIR/"concepts.json",
            )
            policy=AllowedDimensionPolicyService(
                tags,categories,foundation,
                default_path=DEFAULTS_DIR/"relationships.json",
                user_path=RELATIONSHIPS_PATH,
            )
            compositions=ModifierCompositionService(
                tags,categories,foundation,workspace=policy.workspace,
            )
            return TagTemplateDiscoveryService(
                tags,categories,foundation,
                policy_resolver=policy,composition_resolver=compositions,
            ).discover(progress,cancelled)

        task=CancelableProgressCallTask(discover); self._task=task
        task.signals.progress.connect(self._rule_task_progress)
        task.signals.complete.connect(self._template_discovery_ready)
        task.signals.cancelled.connect(self._rule_task_cancelled)
        task.signals.failed.connect(self._rule_task_failed)
        self.pool.start(task)

    def build_reusable_tag_template(self,record=None):
        """Open the structured chip builder, then preview its complete bounded closure."""
        if record is not None and not record.supported:return
        categories=CategoryRegistry(
            DEFAULTS_DIR/"categories.json",CATEGORIES_PATH,
        )
        try:
            categories.load()
            foundation=FoundationKnowledge(
                DEFAULTS_DIR/"modifiers.json",DEFAULTS_DIR/"concepts.json",
            )
            dialog=TagTemplateBuilderDialog(
                self.state.knowledge,categories,foundation,self,record=record,
            )
        except Exception as exc:
            QMessageBox.warning(
                self,"Reusable Tag builder unavailable",
                "The current stable Tag and modifier authority could not be opened; nothing changed.\n\n"+str(exc),
            ); return
        if dialog.exec()!=QDialog.Accepted:return
        draft=dialog.draft(); self._template_builder_expected_record=record
        if not self._start_rule_task(
            "Previewing the complete reviewed selector subset closure…",
            cancellable=True,context="Structured reusable Tag expansion preview",
        ):
            return
        tag_path=self.state.knowledge.path
        ignore_values=tuple(
            str(item.get("tag") or "")
            for item in self.repository.combined().get("ignore_rules",())
        )

        def preview(progress,cancelled):
            service,_tags,_categories=self._structured_template_service(
                tag_path,ignore_values=ignore_values,
            )
            return service.preview(draft,progress,cancelled)

        task=CancelableProgressCallTask(preview); self._task=task
        task.signals.progress.connect(self._rule_task_progress)
        task.signals.complete.connect(self._template_expansion_ready)
        task.signals.cancelled.connect(self._rule_task_cancelled)
        task.signals.failed.connect(self._rule_task_failed)
        self.pool.start(task)

    def _template_discovery_ready(self,report):
        if getattr(self._task,"is_cancelled",lambda:False)():
            self._rule_task_cancelled(); return
        self._finish_rule_task(
            f"Reusable Tag family report ready: {len(report.candidates):,} candidate(s); no System Knowledge changed."
        )
        if self._shutdown_requested:
            self._notify_background_work_finished(); return
        dialog=TagTemplateDiscoveryDialog(report,self)
        result=dialog.exec()
        candidate=(
            dialog.selected_candidate
            if result==QDialog.Accepted else None
        )
        if candidate is not None and not self._shutdown_requested:
            if self._preview_reusable_tag_family(candidate):return
        self._notify_background_work_finished()

    def _preview_reusable_tag_family(self,candidate):
        if not self._start_rule_task(
            "Revalidating the selected family and expanding reviewed values…",
            cancellable=True,context="Reusable Tag family expansion preview",
        ):
            return False
        tag_path=self.state.knowledge.path
        ignore_values=tuple(
            str(record.get("tag") or "")
            for record in self.repository.combined().get("ignore_rules",())
        )

        def preview(progress,cancelled):
            tags=TagDatabase(tag_path, rule_pair_admission=_rules_admission_for_knowledge(tag_path))
            categories=CategoryRegistry(
                DEFAULTS_DIR/"categories.json",CATEGORIES_PATH,
            )
            foundation=FoundationKnowledge(
                DEFAULTS_DIR/"modifiers.json",DEFAULTS_DIR/"concepts.json",
            )
            policy=AllowedDimensionPolicyService(
                tags,categories,foundation,
                default_path=DEFAULTS_DIR/"relationships.json",
                user_path=RELATIONSHIPS_PATH,
            )
            compositions=ModifierCompositionService(
                tags,categories,foundation,workspace=policy.workspace,
            )
            return TagTemplateExpansionService(
                tags,categories,foundation,policy_resolver=policy,
                composition_resolver=compositions,ignore_values=ignore_values,
            ).preview(candidate,progress,cancelled)

        task=CancelableProgressCallTask(preview); self._task=task
        task.signals.progress.connect(self._rule_task_progress)
        task.signals.complete.connect(self._template_expansion_ready)
        task.signals.cancelled.connect(self._rule_task_cancelled)
        task.signals.failed.connect(self._rule_task_failed)
        self.pool.start(task)
        return True

    def _template_expansion_ready(self,report):
        if getattr(self._task,"is_cancelled",lambda:False)():
            self._rule_task_cancelled(); return
        self._finish_rule_task(
            f"Reusable Tag family expansion ready: {len(report.items):,} result(s); no System Knowledge changed."
        )
        if not self._shutdown_requested:
            max_selected=DEFAULT_TAG_TEMPLATE_MAX_PER_RUN
            initial_selected_ids=()
            try:
                if hasattr(report,"draft"):
                    max_selected=report.draft.max_per_run
                    current_id=str(report.draft.template_id or "")
                else:
                    current_id=tag_template_id(report.candidate.id)
                current=next((
                    record for _origin,record in
                    TagTemplateStoreService(self.repository).records_with_origin()
                    if current_id and record.id==current_id
                ),None)
                if current is not None:
                    if not hasattr(report,"draft"):
                        max_selected=current.max_per_run
                    if current.supported:
                        initial_selected_ids=current.selected_result_ids
            except ValueError:
                pass
            dialog=TagTemplateExpansionDialog(
                report,self,max_selected=max_selected,
                initial_selected_ids=initial_selected_ids,
            )
            result=dialog.exec()
            if result==QDialog.Accepted and dialog.approve_requested:
                approve=(
                    self._approve_structured_tag_template
                    if hasattr(report,"draft") else
                    self._approve_reusable_tag_template
                )
                if approve(
                    report,selected_result_ids=getattr(dialog,"selected_result_ids",()),
                ):return
        self._notify_background_work_finished()

    @staticmethod
    def _structured_template_service(tag_path,*,ignore_values=()):
        tags=TagDatabase(tag_path, rule_pair_admission=_rules_admission_for_knowledge(tag_path))
        categories=CategoryRegistry(
            DEFAULTS_DIR/"categories.json",CATEGORIES_PATH,
        )
        foundation=FoundationKnowledge(
            DEFAULTS_DIR/"modifiers.json",DEFAULTS_DIR/"concepts.json",
        )
        policy=AllowedDimensionPolicyService(
            tags,categories,foundation,
            default_path=DEFAULTS_DIR/"relationships.json",
            user_path=RELATIONSHIPS_PATH,
        )
        compositions=ModifierCompositionService(
            tags,categories,foundation,workspace=policy.workspace,
        )
        return TagTemplateBuilderService(
            tags,categories,foundation,policy_resolver=policy,
            composition_resolver=compositions,ignore_values=ignore_values,
        ),tags,categories

    @staticmethod
    def _revalidated_saved_template_expansion(
        expected_record,tag_path,progress,cancelled,
    ):
        repository=AutomationRuleRepository(
            DEFAULTS_DIR/"automation_rules.json",
            USER_DATA_DIR/"automation_rules.json",
            rule_pair_admission=RulePairAdmission(USER_DATA_DIR),
        )
        current=next((
            record for _origin,record in
            TagTemplateStoreService(repository).records_with_origin()
            if record.id==expected_record.id
        ),None)
        if (
            current is None or not current.supported
            or current.semantic_fingerprint!=expected_record.semantic_fingerprint
        ):
            raise RuntimeError(
                "This saved reusable Tag template changed after selection. Refresh and choose the current record."
            )
        if cancelled():raise InterruptedError()
        if current.token_position=="structured" or current.structured_slots:
            ignore_values=tuple(
                str(item.get("tag") or "")
                for item in repository.combined().get("ignore_rules",())
            )
            service,tags,categories=AutomationPage._structured_template_service(
                tag_path,ignore_values=ignore_values,
            )
            draft=TagTemplateBuilderService.draft_from_record(current)
            expansion=service.preview(draft,progress,cancelled)
            if expansion.approval_fingerprint!=current.expansion_fingerprint:
                selected={item.id:item for item in expansion.items}
                if not all(
                    stable_id in selected and not selected[stable_id].held
                    for stable_id in current.selected_result_ids
                ):
                    raise RuntimeError(
                        "This saved structured template expansion is stale. Review and reapprove its current result selection."
                    )
                current=replace(
                    current,expansion_fingerprint=expansion.approval_fingerprint,
                    selection_fingerprint=tag_template_selection_fingerprint(
                        expansion.approval_fingerprint,current.selected_result_ids,
                    ),
                )
            return current,expansion,tags,categories
        tags=TagDatabase(tag_path, rule_pair_admission=_rules_admission_for_knowledge(tag_path))
        categories=CategoryRegistry(
            DEFAULTS_DIR/"categories.json",CATEGORIES_PATH,
        )
        foundation=FoundationKnowledge(
            DEFAULTS_DIR/"modifiers.json",DEFAULTS_DIR/"concepts.json",
        )
        policy=AllowedDimensionPolicyService(
            tags,categories,foundation,
            default_path=DEFAULTS_DIR/"relationships.json",
            user_path=RELATIONSHIPS_PATH,
        )
        compositions=ModifierCompositionService(
            tags,categories,foundation,workspace=policy.workspace,
        )
        discovery=TagTemplateDiscoveryService(
            tags,categories,foundation,policy_resolver=policy,
            composition_resolver=compositions,
        ).discover(progress,cancelled)
        candidate_id=(
            "tag_template_candidate."+current.id[len("tag_template."):]
        )
        candidate=next((
            item for item in discovery.candidates if item.id==candidate_id
        ),None)
        structurally_current=bool(
            candidate is not None and candidate.ready_for_review
            and candidate.id==candidate_id
            and candidate.template==current.template
            and candidate.anchor_ids==current.anchor_ids
            and candidate.modifier_family_id==current.modifier_family_id
            and candidate.modifier_family==current.modifier_family
            and candidate.placeholder==current.placeholder
            and candidate.token_position==current.token_position
            and candidate.policy_fingerprint==current.policy_fingerprint
            and not candidate.family_hold_reasons
        )
        if not structurally_current:
            raise RuntimeError(
                "This saved reusable Tag family is stale. Run discovery and approve its current authority again."
            )
        ignore_values=tuple(
            str(item.get("tag") or "")
            for item in repository.combined().get("ignore_rules",())
        )
        expansion=TagTemplateExpansionService(
            tags,categories,foundation,policy_resolver=policy,
            composition_resolver=compositions,ignore_values=ignore_values,
        ).preview(candidate,progress,cancelled)
        if expansion.approval_fingerprint!=current.expansion_fingerprint:
            selected={item.id:item for item in expansion.items}
            scope_is_current=bool(
                expansion.included_value_ids==current.included_value_ids
                and expansion.excluded_value_ids==current.excluded_value_ids
                and expansion.invalid_value_ids==current.invalid_value_ids
                and expansion.held_value_ids==current.held_value_ids
                and all(
                    stable_id in selected and not selected[stable_id].held
                    for stable_id in current.selected_result_ids
                )
                and len(current.selected_result_ids)<=current.max_per_run
            )
            if not scope_is_current:
                raise RuntimeError(
                    "This saved reusable Tag expansion is stale. Review and reapprove the current result selection."
                )
            # Materializing or deliberately moving a selected result changes
            # the report inventory but not the reviewed template/value scope.
            # Use current fingerprints for this operation without rewriting or
            # versioning the saved Automation definition.
            current=replace(
                current,candidate_fingerprint=candidate.review_fingerprint,
                expansion_fingerprint=expansion.approval_fingerprint,
                selection_fingerprint=tag_template_selection_fingerprint(
                    expansion.approval_fingerprint,current.selected_result_ids,
                ),
            )
        return current,expansion,tags,categories

    def review_selected_tag_template(self):
        record=self._selected_tag_template_record()
        if record is None or not record.supported:return
        if (
            getattr(record,"token_position","")=="structured"
            or bool(getattr(record,"structured_slots",()))
        ):
            self.build_reusable_tag_template(record); return
        if not self._start_rule_task(
            "Revalidating the saved reusable Tag template and selected results…",
            cancellable=True,context="Reusable Tag family expansion preview",
        ):
            return
        tag_path=self.state.knowledge.path

        def review(progress,cancelled):
            _record,expansion,_tags,_categories=(
                self._revalidated_saved_template_expansion(
                    record,tag_path,progress,cancelled,
                )
            )
            return expansion

        task=CancelableProgressCallTask(review); self._task=task
        task.signals.progress.connect(self._rule_task_progress)
        task.signals.complete.connect(self._template_expansion_ready)
        task.signals.cancelled.connect(self._rule_task_cancelled)
        task.signals.failed.connect(self._rule_task_failed)
        self.pool.start(task)

    def preview_selected_tag_template_organization(self,*,action="organize"):
        record=self._selected_tag_template_record()
        if record is None or not record.supported:return
        if action=="generation" and not record.generation_enabled:
            QMessageBox.information(
                self,"Generation is off",
                "Review this template's policies and enable generation before running it.",
            ); return
        self._template_plan_action=str(action or "organize")
        if not self._start_rule_task(
            "Building a current read-only Category and organization plan…",
            cancellable=True,context="Reusable Tag organization preview",
        ):
            return
        tag_path=self.state.knowledge.path

        def preview(progress,cancelled):
            current,expansion,tags,categories=(
                self._revalidated_saved_template_expansion(
                    record,tag_path,progress,cancelled,
                )
            )
            if cancelled():raise InterruptedError()
            return TagTemplateOrganizationPlanningService(
                tags,categories,
            ).preview(current,expansion,progress,cancelled)

        task=CancelableProgressCallTask(preview); self._task=task
        task.signals.progress.connect(self._rule_task_progress)
        task.signals.complete.connect(self._template_organization_ready)
        task.signals.cancelled.connect(self._rule_task_cancelled)
        task.signals.failed.connect(self._rule_task_failed)
        self.pool.start(task)

    def _template_organization_ready(self,report):
        if getattr(self._task,"is_cancelled",lambda:False)():
            self._rule_task_cancelled(); return
        self._finish_rule_task(
            f"Reusable Tag organization plan ready: {len(report.items):,} saved selected result(s), {report.held_count:,} held; no System Knowledge changed."
        )
        action=getattr(self,"_template_plan_action","organize")
        if not self._shutdown_requested:
            record=self._selected_tag_template_record()
            if record is None or record.id!=report.template_id:
                QMessageBox.warning(
                    self,"Template selection changed",
                    "The saved reusable Tag template selection changed. Refresh and preview its current plan again.",
                )
                self._notify_background_work_finished(); return
            if action=="policies":
                policy_record=record
                if report.expansion_fingerprint!=record.expansion_fingerprint:
                    policy_record=replace(
                        record,expansion_fingerprint=report.expansion_fingerprint,
                        selection_fingerprint=tag_template_selection_fingerprint(
                            report.expansion_fingerprint,record.selected_result_ids,
                        ),
                    )
                dialog=TagTemplatePolicyDialog(policy_record,report,self)
                if dialog.exec()==QDialog.Accepted:
                    if self._save_tag_template_policies(
                        policy_record,dialog.preview,expected_record=record,
                    ):return
                self._notify_background_work_finished(); return
            if action=="generation":
                if report.held_count or not report.destination.ready:
                    QMessageBox.warning(
                        self,"Generation is held",
                        "Resolve the held organization scope before running reviewed generation.",
                    )
                elif QMessageBox.question(
                    self,"Run reviewed reusable Tag generation",
                    f"Run the separately reviewed generation policy for all {len(report.items):,} saved result(s)?\n\n"
                    "The exact Categories, Tags, and canonical relationships are revalidated and written as one recoverable local System Knowledge transaction. Image Knowledge, TXT, Metadata, captions, pixels, file locations, and external systems remain unchanged.",
                )==QMessageBox.Yes:
                    if self._apply_tag_template_organization(
                        record,report,action="generation",
                    ):return
                self._notify_background_work_finished(); return
            dialog=TagTemplateOrganizationDialog(report,self)
            if dialog.exec()==QDialog.Accepted:
                if getattr(dialog,"base_approval_requested",False):
                    if self._approve_template_category_base(record,report):return
                elif getattr(dialog,"create_requested",False):
                    if self._apply_tag_template_organization(
                        record,report,
                        action="projection" if action=="projection" else "organize",
                    ):return
        self._notify_background_work_finished()

    def _apply_tag_template_organization(self,record,expected_plan,*,action="organize"):
        if not self._start_rule_task(
            "Revalidating the confirmed scope before the recoverable System Knowledge transaction…",
            cancellable=True,context="Reusable Tag organization apply",
        ):
            return False
        tag_path=self.state.knowledge.path

        def apply_plan(progress,cancelled):
            current,expansion,tags,categories=(
                self._revalidated_saved_template_expansion(
                    record,tag_path,progress,cancelled,
                )
            )
            if cancelled():raise InterruptedError()
            fresh_plan=TagTemplateOrganizationPlanningService(
                tags,categories,
            ).preview(current,expansion,progress,cancelled)
            if fresh_plan.plan_fingerprint!=expected_plan.plan_fingerprint:
                raise RuntimeError(
                    "The reusable Tag organization plan changed after confirmation. Review the refreshed plan before applying it."
                )
            foundation=FoundationKnowledge(
                DEFAULTS_DIR/"modifiers.json",DEFAULTS_DIR/"concepts.json",
            )
            if action=="generation":
                return self.template_generation_operations.run(
                    current,expansion,fresh_plan,tags=tags,categories=categories,
                    foundation=foundation,progress=progress,cancelled=cancelled,
                )
            result=self.template_organization_operations.apply(
                current,expansion,fresh_plan,tags=tags,categories=categories,
                foundation=foundation,progress=progress,cancelled=cancelled,
            )
            if action!="projection":return result
            repository=AutomationRuleRepository(
                DEFAULTS_DIR/"automation_rules.json",
                USER_DATA_DIR/"automation_rules.json",
                rule_pair_admission=RulePairAdmission(USER_DATA_DIR),
            )
            saved=next((
                item for _origin,item in
                TagTemplateStoreService(repository).records_with_origin()
                if item.id==record.id
            ),None)
            if saved is None:
                raise RuntimeError("The reusable Tag template disappeared before its projection rule could be saved.")
            projection=TagTemplateProjectionService.publish_rule(repository,saved)
            return result,projection

        task=CancelableProgressCallTask(apply_plan); self._task=task
        task.signals.progress.connect(self._rule_task_progress)
        task.signals.complete.connect(self._template_organization_applied)
        task.signals.cancelled.connect(self._rule_task_cancelled)
        task.signals.failed.connect(self._rule_task_failed)
        self.pool.start(task)
        return True

    def _template_organization_applied(self,result):
        projection=None
        if isinstance(result,tuple):result,projection=result
        if result.changed:
            recovery=(
                f" Recovery backup: {result.backup_directory}."
                if result.backup_directory is not None else ""
            )
            message=(
                f"Created {len(result.created_tag_ids):,} Tag(s), moved {len(result.moved_tag_ids):,}, "
                f"verified {len(result.reused_tag_ids):,} in place, and "
                f"{'created' if result.category_created else 'reused'} the derived Category as one recoverable System Knowledge operation. "
                +(
                    " A parameterized projection rule was saved for this exact stable template scope."
                    if projection is not None else
                    " Image Knowledge, files, projection rules, and automatic policies were unchanged."
                )
                +recovery
            )
        else:
            message=(
                "The reviewed reusable Tag organization was already current; no System Knowledge write was needed."
                +(
                    " A parameterized projection rule was saved for this exact stable template scope."
                    if projection is not None else ""
                )
            )
        self._finish_rule_task(message)
        self.state.knowledge.load()
        self.state.events.publish("knowledge_changed")
        self.knowledge_changed.emit()
        self.repository.load(); self.refresh(reload=False)
        self._notify_background_work_finished()

    def _approve_template_category_base(self,record,report):
        destination=report.destination
        roles=record.anchor_roles or tuple(
            "base" if index==0 else "component"
            for index in range(len(record.anchor_ids))
        )
        base_ids=tuple(
            stable_id for stable_id,role in zip(record.anchor_ids,roles)
            if role=="base"
        )
        if (
            len(base_ids)!=1 or not destination.parent_category_id
            or not destination.base_match_basis
        ):
            QMessageBox.warning(
                self,"Category Base approval unavailable",
                "The current plan does not contain one exact stable Base Tag proposal.",
            ); return False
        if not self._start_rule_task(
            "Saving the reviewed exact Category Base proposal on the template…",
            cancellable=False,context="Reusable Tag Category Base approval",
        ):
            return False

        def approve(_progress,_cancelled):
            repository=AutomationRuleRepository(
                DEFAULTS_DIR/"automation_rules.json",
                USER_DATA_DIR/"automation_rules.json",
                rule_pair_admission=RulePairAdmission(USER_DATA_DIR),
            )
            current=next((
                item for _origin,item in
                TagTemplateStoreService(repository).records_with_origin()
                if item.id==record.id
            ),None)
            if current is None or current.semantic_fingerprint!=record.semantic_fingerprint:
                raise RuntimeError(
                    "The reusable Tag template changed after the Category Base proposal opened. Preview its current plan again."
                )
            policy=dict(current.destination_policy)
            policy["approved_category_base"]={
                "category_id":destination.parent_category_id,
                "target_id":base_ids[0],
                "match_basis":destination.base_match_basis,
            }
            proposed=replace(
                current,version=current.version+1,
                reviewed_at=datetime.now(timezone.utc).isoformat(timespec="seconds"),
                destination_policy=policy,generation_enabled=False,
                fact_expansion_enabled=False,policy_review={},
            )
            proposed.validate()
            return TagTemplateStoreService(repository).save_record(
                proposed,expected_current_fingerprint=current.semantic_fingerprint,
            )

        task=CancelableProgressCallTask(approve); self._task=task
        task.signals.complete.connect(self._template_base_approval_ready)
        task.signals.failed.connect(self._rule_task_failed)
        self.pool.start(task); return True

    def _template_base_approval_ready(self,result):
        self._finish_rule_task(
            f"Saved the reviewed exact Category Base proposal on {result.record.template} v{result.record.version}. Reopen the organization plan before any System Knowledge write; template automation policies are off."
        )
        self.repository.load(); self.refresh(reload=False)
        self._notify_background_work_finished()

    def _save_tag_template_policies(self,record,preview,*,expected_record=None):
        expected_record=expected_record or record
        if not self._start_rule_task(
            "Saving the separately reviewed template automation policies…",
            cancellable=False,context="Reusable Tag automation policy approval",
        ):
            return False

        def approve(_progress,_cancelled):
            repository=AutomationRuleRepository(
                DEFAULTS_DIR/"automation_rules.json",
                USER_DATA_DIR/"automation_rules.json",
                rule_pair_admission=RulePairAdmission(USER_DATA_DIR),
            )
            current=next((
                item for _origin,item in
                TagTemplateStoreService(repository).records_with_origin()
                if item.id==expected_record.id
            ),None)
            if (
                current is None
                or current.semantic_fingerprint!=expected_record.semantic_fingerprint
            ):
                raise RuntimeError(
                    "The reusable Tag template changed after policy review opened. Preview its current plan again."
                )
            return TagTemplatePolicyService(repository).approve(
                record,preview,
                expected_current_fingerprint=current.semantic_fingerprint,
            )

        task=CancelableProgressCallTask(approve); self._task=task
        task.signals.complete.connect(self._template_policy_approval_ready)
        task.signals.failed.connect(self._rule_task_failed)
        self.pool.start(task); return True

    def _template_policy_approval_ready(self,result):
        record=result.record
        self._finish_rule_task(
            f"Saved separately reviewed policies for {record.template} v{record.version}: generation {'on' if record.generation_enabled else 'off'}; association suggestions {'on' if record.fact_expansion_enabled else 'off'}. No System Knowledge, Image Knowledge, or files changed."
        )
        self.repository.load(); self.refresh(reload=False)
        self._notify_background_work_finished()

    def _refresh_template_organization_history(self):
        operation=self.template_organization_operations.undoable_operation()
        self.template_organization_history_panel.setVisible(bool(operation))
        self.template_organization_undo.setVisible(bool(operation))
        if operation is None:
            self.template_organization_history_status.clear(); return
        created=len(operation.result.get("created_tag_ids",()))
        moved=len(operation.result.get("moved_tag_ids",()))
        generation=self.template_generation_operations.journal.latest(
            kind="tag_template_generation",statuses=("complete",),
        )
        self._template_history_is_generation=bool(
            generation is not None
            and str(generation.result.get("organization_operation_id") or "")==operation.id
        )
        self.template_organization_undo.setText(
            "Restore Last Reviewed Generation"
            if self._template_history_is_generation else
            "Restore Last Template Organization"
        )
        self.template_organization_history_status.setText(
            f"The last {'reviewed generation' if self._template_history_is_generation else 'reusable Tag organization'} created {created:,} Tag(s) and moved {moved:,}; exact restore remains available until any touched System Knowledge store changes."
        )

    def undo_tag_template_organization(self):
        operation=self.template_organization_operations.undoable_operation()
        if operation is None:return
        if QMessageBox.question(
            self,"Restore reusable Tag organization",
            "Restore the exact Categories, Tags, and canonical relationships from before the last reusable Tag organization?\n\nThis is available only while none of those three stores has changed. Image Knowledge, TXT, Metadata, captions, pixels, file locations, and Automation rules remain unchanged.",
        )!=QMessageBox.Yes:return
        if not self._start_rule_task(
            "Verifying and restoring the last reusable Tag organization…",
            cancellable=False,context="Reusable Tag organization restore",
        ):
            return
        task=CancelableProgressCallTask(
            lambda _progress,_cancelled:
            (
                self.template_generation_operations.undo_last()
                if getattr(self,"_template_history_is_generation",False) else
                self.template_organization_operations.undo_last()
            )
        )
        self._task=task
        task.signals.complete.connect(self._template_organization_undone)
        task.signals.failed.connect(self._rule_task_failed)
        self.pool.start(task)

    def _template_organization_undone(self,_operation_id):
        self._finish_rule_task(
            "Restored the exact pre-template Categories, Tags, and relationships. Image Knowledge and files were unchanged."
        )
        self.state.knowledge.load()
        self.state.events.publish("knowledge_changed")
        self.knowledge_changed.emit()
        self.refresh(reload=False)
        self.header.set_status(
            "Restored the exact pre-template Categories, Tags, and relationships. Image Knowledge and files were unchanged."
        )
        self._notify_background_work_finished()

    def _approve_structured_tag_template(self,report,*,selected_result_ids=()):
        selected_result_ids=tuple(map(str,selected_result_ids))
        expected=getattr(self,"_template_builder_expected_record",None)
        if QMessageBox.question(
            self,"Approve Structured Reusable Tag Template",
            f"Approve {report.draft.display_template} with its complete reviewed selector scope?\n\n"
            f"Potential valid results: {report.potential_count:,}\n"
            f"Ready results selected for later creation planning: {len(selected_result_ids):,}\n"
            f"Per-run maximum: {report.draft.max_per_run:,}\n\n"
            "This saves one disabled, versioned template. It creates no Tags or Categories and leaves generation, projection, association suggestions, Image Knowledge, files, and external systems unchanged.",
        )!=QMessageBox.Yes:
            return False
        if not self._start_rule_task(
            "Revalidating and saving the structured reusable Tag template…",
            cancellable=True,context="Structured reusable Tag template approval",
        ):
            return False
        tag_path=self.state.knowledge.path
        ignore_values=tuple(
            str(item.get("tag") or "")
            for item in self.repository.combined().get("ignore_rules",())
        )

        def approve(progress,cancelled):
            service,_tags,_categories=self._structured_template_service(
                tag_path,ignore_values=ignore_values,
            )
            current_report=service.preview(report.draft,progress,cancelled)
            if current_report.approval_fingerprint!=report.approval_fingerprint:
                raise RuntimeError(
                    "This structured reusable Tag expansion changed after review. Preview it again before approval."
                )
            repository=AutomationRuleRepository(
                DEFAULTS_DIR/"automation_rules.json",
                USER_DATA_DIR/"automation_rules.json",
                rule_pair_admission=RulePairAdmission(USER_DATA_DIR),
            )
            current=None
            if expected is not None:
                current=next((
                    item for _origin,item in
                    TagTemplateStoreService(repository).records_with_origin()
                    if item.id==expected.id
                ),None)
                if current is None or current.semantic_fingerprint!=expected.semantic_fingerprint:
                    raise RuntimeError(
                        "The saved reusable Tag template changed after the builder opened. Reopen the current version before saving."
                    )
            return service.approve(
                repository,current_report,selected_result_ids=selected_result_ids,
                current=current,
            )

        task=CancelableProgressCallTask(approve); self._task=task
        task.signals.progress.connect(self._rule_task_progress)
        task.signals.complete.connect(self._template_approval_ready)
        task.signals.cancelled.connect(self._rule_task_cancelled)
        task.signals.failed.connect(self._rule_task_failed)
        self.pool.start(task); return True

    def _approve_reusable_tag_template(self,report,*,selected_result_ids=()):
        candidate=report.candidate
        selected_result_ids=tuple(map(str,selected_result_ids))
        if QMessageBox.question(
            self,"Approve Reusable Tag Template",
            f"Approve {candidate.template} as a reusable Rules & Automation template?\n\n"
            f"Included reviewed values: {len(report.included_value_ids):,}\n"
            f"Excluded reviewed values: {len(report.excluded_value_ids):,}\n"
            f"Held expansion results retained for later review: {len(report.held_value_ids):,}\n\n"
            f"Ready results selected for later creation planning: {len(selected_result_ids):,}\n\n"
            "This saves one disabled, versioned template. It does not create or move Tags, create Categories, enable generation or fact expansion, or change Image Knowledge, TXT, Metadata, pixels, or locations."
        )!=QMessageBox.Yes:
            return False
        if not self._start_rule_task(
            "Revalidating and saving the reviewed reusable Tag template…",
            cancellable=True,context="Reusable Tag template approval",
        ):
            return False
        tag_path=self.state.knowledge.path
        ignore_values=tuple(
            str(record.get("tag") or "")
            for record in self.repository.combined().get("ignore_rules",())
        )

        def approve(progress,cancelled):
            tags=TagDatabase(tag_path, rule_pair_admission=_rules_admission_for_knowledge(tag_path))
            categories=CategoryRegistry(
                DEFAULTS_DIR/"categories.json",CATEGORIES_PATH,
            )
            foundation=FoundationKnowledge(
                DEFAULTS_DIR/"modifiers.json",DEFAULTS_DIR/"concepts.json",
            )
            policy=AllowedDimensionPolicyService(
                tags,categories,foundation,
                default_path=DEFAULTS_DIR/"relationships.json",
                user_path=RELATIONSHIPS_PATH,
            )
            compositions=ModifierCompositionService(
                tags,categories,foundation,workspace=policy.workspace,
            )
            current=TagTemplateExpansionService(
                tags,categories,foundation,policy_resolver=policy,
                composition_resolver=compositions,ignore_values=ignore_values,
            ).preview(candidate,progress,cancelled)
            if current.approval_fingerprint!=report.approval_fingerprint:
                raise RuntimeError(
                    "This reusable Tag family expansion changed after review. Preview it again before approval."
                )
            if cancelled():raise InterruptedError()
            repository=AutomationRuleRepository(
                DEFAULTS_DIR/"automation_rules.json",
                USER_DATA_DIR/"automation_rules.json",
                rule_pair_admission=RulePairAdmission(USER_DATA_DIR),
            )
            return TagTemplateStoreService(repository).approve(
                current,selected_result_ids=selected_result_ids,
            )

        task=CancelableProgressCallTask(approve); self._task=task
        task.signals.progress.connect(self._rule_task_progress)
        task.signals.complete.connect(self._template_approval_ready)
        task.signals.cancelled.connect(self._rule_task_cancelled)
        task.signals.failed.connect(self._rule_task_failed)
        self.pool.start(task)
        return True

    def _template_approval_ready(self,result):
        record=result.record
        state=(
            "Approved" if result.created else
            "Updated" if result.updated else "Already approved"
        )
        recovery=(
            f" Recovery backup: {result.backup_path}."
            if result.backup_path is not None else ""
        )
        self._finish_rule_task(
            f"{state} {record.template} v{record.version} with {len(record.selected_result_ids):,} result(s) selected for later creation planning. Automatic generation remains off; no Tags or Image Knowledge changed.{recovery}"
        )
        self.repository.load(); self.refresh(reload=False)
        self._notify_background_work_finished()

    def refresh(self,*,reload=True):
        if reload: self.repository.load(); self.state.knowledge.load()
        self._refresh_fact_expansion_settings()
        self._refresh_external_write_settings()
        self._refresh_intent_migration()
        self._refresh_projection_rule_repairs()
        self._refresh_template_organization_history()
        self._refresh_value_binding_history()
        self.rule_tree.clear(); self.value_tree.clear(); self.template_tree.clear()
        self._template_storage_issue=""
        self._template_records_by_key={}
        self.template_storage_review.hide()
        variable_rows=sorted(
            self.repository.variables_with_origin(),
            key=lambda item:(item[1].casefold(),item[0]),
        )
        variable_items={}
        for origin,name,values in variable_rows:
            value_text=(
                ", ".join(values)
                if len(values)<=5 else
                ", ".join(values[:3])+f" · +{len(values)-3:,} more"
            )
            item=QTreeWidgetItem((name,value_text,"Reviewing…",""))
            item.setData(0,Qt.UserRole,("variable",origin,name))
            item.setToolTip(0,origin+" compatibility knowledge")
            item.setToolTip(1,", ".join(values))
            self.value_tree.addTopLevelItem(item)
            variable_items[(origin,AutomationEngine._variable_key(name))]=item
        try:
            self.value_binding_categories.load()
            service=self.value_binding_migration.audit_service
            report=service.audit(self.repository.combined(),variable_rows)
            self._value_authority_service=service
            self._value_authority_report=report
            reviewed=0
            held=0
            current_decisions={}
            for family in report.families:
                decision=self.value_binding_migration.authority_decision(
                    family.name,
                )
                current=bool(
                    decision
                    and list(decision.get("reviewed_consumers",()))
                        ==list(family.consumers)
                    and (
                        decision.get("decision")=="retire_unused"
                        and not family.values and not family.consumers
                        or decision.get("decision")=="retain_distinct"
                        and list(decision.get("legacy_values",()))
                            ==[value.value for value in family.values]
                        and decision.get("source_origin")==family.origin
                    )
                )
                current_decisions[(family.origin,family.name)]=(decision,current)
                if current:reviewed+=1
                else:held+=family.held_count
            self.value_authority_status.setText(
                f"Read-only authority review: {len(report.families):,} legacy "
                f"list(s), {reviewed:,} explicit authority decision(s), "
                f"{held:,} value(s) held for review. Matching and persistence "
                "are unchanged until a reviewed decision is applied."
            )
            for family in report.families:
                item=variable_items.get((family.origin,family.name))
                if item is None:continue
                decision,decision_current=current_decisions[
                    (family.origin,family.name)
                ]
                if decision_current:
                    item.setText(2,(
                        "Retired legacy list"
                        if decision.get("decision")=="retire_unused" else
                        "Retained lexical authority"
                    ))
                else:item.setText(2,family.classification_label)
                consumer_text=(
                    "No current rule"
                    if not family.consumers else
                    family.consumers[0]
                    if len(family.consumers)==1 else
                    f"{len(family.consumers):,} rules"
                )
                item.setText(3,consumer_text)
                item.setToolTip(3,", ".join(family.consumers))
                details=[]
                for value in family.values:
                    if value.outcome in {"typed_tag","stable_tag"}:continue
                    details.append(f"{value.value}: {value.outcome.replace('_',' ')}")
                item.setToolTip(
                    2,
                    (
                        "A reviewed authority decision is saved. "
                        f"Rationale: {decision.get('rationale','')}"
                        if decision_current else
                        "Legacy strings remain authoritative for wildcard matching. "
                    )
                    +(
                        " Held values: "+"; ".join(details)
                        if details else
                        (
                            ""
                            if decision_current else
                            "Every value has one live Tag identity; migration still requires review."
                        )
                    ),
                )
        except (OSError,RuntimeError,ValueError) as exc:
            self._value_authority_service=None
            self._value_authority_report=None
            self.value_authority_status.setText(
                "Legacy value authority review is unavailable: "+str(exc)
            )
            for item in variable_items.values():item.setText(2,"Review unavailable")
        self._variable_selection_changed()
        try:
            templates=TagTemplateStoreService(self.repository).records_with_origin()
            for origin,record in templates:
                anchor=(" + ".join(record.anchor_labels) if record.anchor_labels else "Unavailable")
                dimension=(
                    " + ".join(
                        str(item.get("family_label") or item.get("family_id") or "")
                        for item in record.slot_records
                    ) if record.supported else
                    f"Newer format v{record.format_version}"
                )
                selected=(f"{len(record.selected_result_ids):,}" if record.supported else "Held")
                item=QTreeWidgetItem((
                    record.template,anchor,dimension,selected,f"v{record.version}",
                ))
                item.setData(0,Qt.UserRole,("tag_template",origin,record.id))
                self._template_records_by_key[(origin,record.id)]=record
                tooltip=(
                    f"{origin} template; {len(record.slot_records):,} typed selector(s); {len(record.selected_result_ids):,} ready results selected; generation {'on' if record.generation_enabled else 'off'}; association suggestions {'on' if record.fact_expansion_enabled else 'off'}; projection {'on' if record.projection_rule.get('enabled',False) else 'off'}."
                    +(
                        " automatic generation and fact expansion are off."
                        if not record.generation_enabled and not record.fact_expansion_enabled
                        else ""
                    )
                    if record.supported else
                    f"{origin} template uses newer format v{record.format_version}. It is preserved read-only and cannot be overwritten by this CollectiGen version."
                )
                item.setToolTip(0,tooltip)
                self.template_tree.addTopLevelItem(item)
        except ValueError as exc:
            item=QTreeWidgetItem(("Template storage needs review","","","Held"))
            item.setToolTip(0,str(exc)); self.template_tree.addTopLevelItem(item)
            self._template_storage_issue=str(exc)
            self.template_storage_review.show()
        for origin,kind,index,record in self.repository.rules_with_origin():
            intent=explicit_rule_intent(kind,record)
            if kind=="ignore_rules":
                when=str(record.get("tag",""))
                if intent=="reject_incoming_evidence": result="Suppress exact incoming observation"
                elif intent=="training_projection": result="Omit from training projection"
                elif intent=="image_correction_review": result="Open correction review"
                else: result="Review required"
            elif kind=="wildcard_rules":
                remove=record.get("remove",[]); remove=[remove] if isinstance(remove,str) else remove; when=f"{record.get('pattern','')} + {', '.join(remove)}"; result=record.get("pattern","")
            else: when=", ".join(record.get("remove",[])); result=record.get("replace","")
            if intent: label=RULE_INTENT_LABELS[intent]
            elif origin=="Default": label=RULE_INTENT_LABELS["reject_incoming_evidence" if kind=="ignore_rules" else "training_projection"]
            else: label="Legacy intent not classified"
            item=QTreeWidgetItem((label,when,str(result),ORIGIN_LABELS.get(origin,origin))); item.setData(0,Qt.UserRole,("repository",origin,kind,index))
            details=f"{origin} rule storage"; rating=str(record.get("content_rating","")).strip()
            if rating: details+=f"; content rating: {rating}"
            item.setToolTip(3,details); self.rule_tree.addTopLevelItem(item)
        for rule in sorted(self.state.knowledge.merge_rules.values(),key=lambda value:value.id):
            intent=explicit_rule_intent("merge_rules",rule.to_dict())
            label=RULE_INTENT_LABELS[intent] if intent else "Legacy intent not classified"
            item=QTreeWidgetItem((label,", ".join(rule.source_tags),rule.target_tag,"System Knowledge")); item.setData(0,Qt.UserRole,("modern",rule.id)); item.setToolTip(3,"Reviewed reusable System Knowledge"); self.rule_tree.addTopLevelItem(item)
        self._reload_conflict_projection()
        for tree in (self.rule_tree,self.value_tree,self.template_tree,self.diagnostic_conflict_tree):
            for column in range(tree.columnCount()): tree.resizeColumnToContents(column)
        self.filter_rules()
        self._rule_selection_changed()
        self._template_selection_changed()
        self._focus_conflict_tree()

    def _reload_conflict_projection(self):
        if self._shutdown_requested:
            return
        self._conflict_projection_retries=0
        self._queue_conflict_projection()

    def _queue_conflict_projection(self):
        selected=self.diagnostic_conflict_tree.currentItem()
        if selected is not None and not selected.isHidden():
            self._conflict_focus_relationship_id=str(selected.data(0,Qt.UserRole) or "")
        if self._conflict_projection_task is not None:
            self._conflict_projection_task.cancel()
        self._conflict_projection_task=None
        self._conflict_projection=None
        self._conflict_projection_generation+=1
        self.diagnostic_conflict_tree.clear()
        self.edit_conflict_rule.setEnabled(False)
        self.conflict_retry.hide()
        self.conflict_loading_status.setText("Loading diagnostic conflict rules…")
        self.conflict_loading_status.show()
        if self._defer_expensive_initialization:
            QTimer.singleShot(0,self._start_conflict_projection)
        else:
            self._populate_conflict_projection(prepare_automation_conflict_projection(self.state))

    def paintEvent(self,event):
        super().paintEvent(event)
        if not self._conflict_initial_paint_done:
            self._conflict_initial_paint_done=True
            if self._defer_expensive_initialization:
                QTimer.singleShot(0,self._start_conflict_projection)

    def _start_conflict_projection(self):
        if self._shutdown_requested or self._conflict_projection_task is not None or self._conflict_projection is not None:
            return
        if getattr(self.state.jobs,"shutting_down",False):
            return
        if not getattr(self,"_conflict_initial_paint_done",True):
            # The first CPU projection must not contend with initial layout
            # and painting. paintEvent schedules it after that paint returns.
            return
        state=self.state
        task=CoordinatedCallTask(
            state.jobs,"Loading diagnostic conflict rules",
            lambda job:prepare_automation_conflict_projection(state,job),
            resource="cpu",key=f"automation:conflict-projection:{id(self)}:{self._conflict_projection_generation}",
        )
        self._conflict_projection_task=task
        task.signals.complete.connect(lambda result,owner=task:self._conflict_projection_complete(owner,result))
        task.signals.failed.connect(lambda message,owner=task:self._conflict_projection_failed(owner,message))
        task.signals.cancelled.connect(lambda owner=task:self._conflict_projection_failed(owner,"Loading was cancelled."))
        task.start()

    def _conflict_projection_complete(self,task,result):
        if task is not self._conflict_projection_task or self._shutdown_requested:
            return
        if not result.is_current(self.state):
            if self._conflict_projection_retries<1:
                self._conflict_projection_retries+=1
                self._queue_conflict_projection()
            else:
                self._conflict_projection_failed(task,"Rules changed while loading. Retry when your changes are saved.")
            return
        self._conflict_projection_task=None
        self._populate_conflict_projection(result)
        if self._pending_conflict_editor_open:
            QTimer.singleShot(0,self._open_pending_conflict_editor)

    def _populate_conflict_projection(self,projection):
        self._conflict_projection=projection
        self.diagnostic_conflict_tree.clear()
        for row in projection.rows:
            item=QTreeWidgetItem(row.columns)
            item.setData(0,Qt.UserRole,row.relationship_id)
            item.setData(0,Qt.UserRole+1,row.tag_ids)
            item.setData(0,Qt.UserRole+2,row.disabled)
            item.setToolTip(0,row.tooltip)
            self.diagnostic_conflict_tree.addTopLevelItem(item)
        self.conflict_loading_status.setText("")
        self.conflict_loading_status.hide()
        self.conflict_retry.hide()
        self._filter_conflict_rules()
        for column in range(self.diagnostic_conflict_tree.columnCount()):
            self.diagnostic_conflict_tree.resizeColumnToContents(column)
        self._focus_conflict_tree()

    def _conflict_projection_failed(self,task,message):
        if task is not self._conflict_projection_task or self._shutdown_requested:
            return
        self._conflict_projection_task=None
        self._pending_conflict_editor_open=False
        self.conflict_loading_status.setText("Conflict rules could not be loaded. Retry to load the current list.")
        self.conflict_loading_status.show()
        self.conflict_retry.show()

    def _open_pending_conflict_editor(self):
        if self._shutdown_requested or not self._pending_conflict_editor_open:
            return
        if self._conflict_projection is None:
            return
        if not self._conflict_projection.is_current(self.state):
            self._pending_conflict_editor_open=False
            self._conflict_projection=None
            self.diagnostic_conflict_tree.clear()
            self.edit_conflict_rule.setEnabled(False)
            self.conflict_loading_status.setText("Rules changed. Reload the list, then choose the rule to edit.")
            self.conflict_loading_status.show()
            self.conflict_retry.show()
            return
        self._pending_conflict_editor_open=False
        self.open_conflict_rule_workspace()

    def _filter_conflict_rules(self, *_args):
        if not hasattr(self, "diagnostic_conflict_tree"):
            return
        query = self.conflict_search.text().strip().casefold()
        lifecycle = self.conflict_lifecycle.currentData()
        for index in range(self.diagnostic_conflict_tree.topLevelItemCount()):
            item = self.diagnostic_conflict_tree.topLevelItem(index)
            disabled = bool(item.data(0, Qt.UserRole + 2))
            text = " ".join(item.text(column) for column in range(item.columnCount())) + " " + item.toolTip(0)
            visible = (not query or query in text.casefold()) and (
                lifecycle == "all" or (lifecycle == "disabled") == disabled
            )
            item.setHidden(not visible)
            if not visible and item.isSelected():
                item.setSelected(False)
        self._conflict_selection_changed()

    def _conflict_selection_changed(self):
        if hasattr(self, "edit_conflict_rule"):
            current = self.diagnostic_conflict_tree.currentItem()
            self.edit_conflict_rule.setEnabled(bool(current and current.isSelected() and not current.isHidden()))

    def _focus_conflict_tree(self):
        tree=getattr(self,"diagnostic_conflict_tree",None)
        if tree is None:return
        relationship_id=str(self._conflict_focus_relationship_id or "")
        tag_id=str(self._conflict_focus_tag_id or "")
        scoped=bool(relationship_id or tag_id)
        if scoped:
            tree.clearSelection()
            tree.setCurrentItem(None)
        if tag_id and self._conflict_projection is not None:
            record=self.state.knowledge.tags.get(tag_id)
            if record is not None:
                self.conflict_lifecycle.setCurrentIndex(self.conflict_lifecycle.findData("all"))
                self.conflict_search.setText(record.display)
        selected=None
        for index in range(tree.topLevelItemCount()):
            item=tree.topLevelItem(index)
            if relationship_id and str(item.data(0,Qt.UserRole) or "")==relationship_id:
                selected=item; break
            if tag_id and tag_id in tuple(item.data(0,Qt.UserRole+1) or ()):
                selected=selected or item
        if selected is not None:
            if selected.isHidden():
                self.conflict_search.clear()
                self.conflict_lifecycle.setCurrentIndex(self.conflict_lifecycle.findData("all"))
            tree.setCurrentItem(selected)
            tree.scrollToItem(selected)
            if scoped and self._conflict_projection is not None:
                self.conflict_loading_status.hide()
        elif scoped and self._conflict_projection is not None:
            record=self.state.knowledge.tags.get(tag_id)
            name=record.display if record is not None else "this SDXL Tag"
            self.conflict_loading_status.setText(
                f"No conflict rule includes {name}. Choose New Conflict Rule to add one."
                if tag_id else
                "The requested conflict rule is not in the current list. Choose a current rule or create one."
            )
            self.conflict_loading_status.show()
            self.conflict_retry.hide()
            self.edit_conflict_rule.setEnabled(False)

    def open_conflict_rule_workspace(self,*,start_new=False):
        if start_new:
            self._pending_conflict_editor_open=False
        current=self.diagnostic_conflict_tree.currentItem()
        relationship_id=(
            str(current.data(0,Qt.UserRole) or "")
            if current is not None and not start_new else ""
        )
        if not relationship_id and not start_new:
            relationship_id=str(self._conflict_focus_relationship_id or "")
        if not relationship_id and not start_new:
            return
        categories=getattr(self.state.knowledge,"_categories",None)
        if not isinstance(categories,CategoryRegistry):
            categories=CategoryRegistry(
                DEFAULTS_DIR/"categories.json",CATEGORIES_PATH,
            )
        try:
            categories.load()
            dialog=ConflictRuleWorkspaceDialog(
                self.state.knowledge,categories,self,
                focus_relationship_id=relationship_id,
                focus_tag_id=self._conflict_focus_tag_id,
                start_new=bool(start_new),
            )
        except Exception as exc:
            QMessageBox.warning(
                self,"Conflict Rules Unavailable",
                "The canonical conflict store could not be opened and was left unchanged.\n\n"
                +str(exc),
            )
            return
        dialog.exec()
        if not dialog.changed:
            return
        self._conflict_focus_relationship_id=(
            dialog.current.id if dialog.current is not None else relationship_id
        )
        self.state.events.publish("knowledge_changed")
        self.knowledge_changed.emit()
        self.refresh(reload=False)
        self.header.set_status(
            "Diagnostic conflict policy saved with verified recovery. No contextual job or outward write ran."
        )

    def _selected_tag_template_record(self):
        item=self.template_tree.currentItem()
        data=item.data(0,Qt.UserRole) if item is not None else None
        if not (
            isinstance(data,tuple) and len(data)==3 and data[0]=="tag_template"
        ):
            return None
        return self._template_records_by_key.get((data[1],data[2]))

    def _template_selection_changed(self):
        if not hasattr(self,"review_template_button"):return
        record=self._selected_tag_template_record()
        available=self._task is None and record is not None and record.supported
        for button in (
            self.edit_template_button,self.review_template_button,
            self.preview_template_organization_button,
            self.create_projection_rule_button,self.review_template_policy_button,
        ):
            button.setEnabled(available)
        self.run_template_generation_button.setEnabled(bool(
            available and record is not None
            and bool(getattr(record,"generation_enabled",False))
        ))
        if record is None:
            tooltip="Select one reviewed reusable Tag template."
        elif not record.supported:
            tooltip=(
                f"This template uses newer format v{record.format_version} and is preserved read-only."
            )
        elif self._task is not None:
            tooltip="Wait for the current Automation work to finish safely."
        else:
            tooltip=(
                "Revalidate the saved family and reopen its reviewed selection."
            )
        self.review_template_button.setToolTip(tooltip)
        self.edit_template_button.setToolTip(tooltip)
        self.preview_template_organization_button.setToolTip(
            tooltip if not available else
            "Build a fresh read-only Category destination and selected-result plan."
        )
        self.create_projection_rule_button.setToolTip(
            tooltip if not available else
            "Revalidate and create the saved result scope, then publish one parameterized stable-ID projection rule."
        )
        self.review_template_policy_button.setToolTip(
            tooltip if not available else
            "Review generation and owner-scoped association-suggestion policies independently."
        )
        self.run_template_generation_button.setToolTip(
            "Run the separately reviewed bounded generation policy."
            if available and record is not None
            and bool(getattr(record,"generation_enabled",False)) else
            "Enable generation in Review Policies before running it."
        )

    def filter_rules(self,*_):
        query=self.rule_search.text().strip().casefold() if hasattr(self,"rule_search") else ""
        visible_count=0
        for index in range(self.rule_tree.topLevelItemCount()):
            item=self.rule_tree.topLevelItem(index)
            text=" ".join(item.text(column) for column in range(item.columnCount())).casefold()
            key=item.data(0,Qt.UserRole)
            kind=key[2] if key and key[0]=="repository" else "merge_rules"
            goal=getattr(self,"_rule_goal",None)
            outside_goal=(goal=="screen" and kind!="ignore_rules") or (goal=="captions" and kind=="ignore_rules")
            hidden=outside_goal or bool(query and query not in text)
            if hidden and item.isSelected():item.setSelected(False)
            item.setHidden(hidden)
            visible_count+=not hidden
        if hasattr(self,"rule_scope_status"):
            self.rule_scope_status.setVisible(not visible_count)
            if not visible_count:
                self.rule_scope_status.setText(
                    "No matching rules. Clear the filter to see this goal's rules."
                    if query else
                    "No screening rules yet. Choose Add Screening Rule to review one known Tag."
                    if getattr(self,"_rule_goal",None)=="screen" else
                    "No caption rules yet. Choose Add Caption Rule to review a match and result."
                    if getattr(self,"_rule_goal",None)=="captions" else
                    "No rules in this view. Choose another goal or browse all rules."
                )
        self._rule_selection_changed()

    def engine(self):
        return active_automation_engine(self.state.knowledge)

    def selected_rule(self):
        selected=self.selected_rules()
        return selected[0] if len(selected)==1 else None

    def selected_rules(self):
        return [
            item.data(0,Qt.UserRole)
            for item in self.rule_tree.selectedItems()
            if item.data(0,Qt.UserRole) and not item.isHidden()
            and self.rule_inventory.isVisible()
        ]

    @staticmethod
    def _compatible_rule_selection(keys):
        kinds={"system" if key[0]=="modern" else "automation" for key in keys}
        return len(kinds)<=1

    def _rule_selection_changed(self):
        if not hasattr(self,"delete_rule_button"):return
        items=self.rule_tree.selectedItems(); keys=[
            item.data(0,Qt.UserRole) for item in items
            if item.data(0,Qt.UserRole) and not item.isHidden()
            and self.rule_inventory.isVisible()
        ]
        count=len(keys)
        available=self._task is None
        compatible=self._compatible_rule_selection(keys)
        self.edit_rule_button.setEnabled(available and count==1)
        self.delete_rule_button.setEnabled(available and count>0 and compatible)
        self.delete_rule_button.setText(
            f"Delete {count} Rules…" if count>1 else "Delete Rule…"
        )
        if not count:
            status="No rules selected"
            tooltip="Select one rule to edit, or use Ctrl/Shift to select several compatible rules for deletion."
        elif not compatible:
            status=(
                f"{count:,} rules selected across Automation rules and System Knowledge; "
                "select from one source at a time to delete."
            )
            tooltip="The complete selection spans separate protected authorities, so no subset will be deleted."
        elif keys[0][0]=="modern":
            status=f"{count:,} System Knowledge rule{'s' if count!=1 else ''} selected"
            tooltip="Delete applies to the complete selected System Knowledge rule set."
        else:
            user=sum(key[1]=="User" for key in keys); built_in=count-user
            parts=[]
            if user:parts.append(f"{user:,} My rule{'s' if user!=1 else ''}")
            if built_in:parts.append(f"{built_in:,} built-in rule{'s' if built_in!=1 else ''}")
            status=f"{count:,} Automation rule{'s' if count!=1 else ''} selected: "+", ".join(parts)
            tooltip="Delete removes selected My rules and disables selected built-in rules in one verified save."
        self.rule_selection_status.setText(status)
        self.rule_selection_status.setToolTip(tooltip)
        self.delete_rule_button.setToolTip(tooltip)

    def add_rule(self): self.edit_rule(new=True)

    def _refresh_intent_migration(self):
        candidates=self.intent_migration.candidates(); undoable=self.intent_migration.undoable_operation()
        recovery_message=(
            self._intent_migration_recovery_error
            or self._intent_migration_recovery_notice
        )
        self.intent_migration_panel.setVisible(bool(candidates or undoable or recovery_message))
        self.intent_migration_review.setVisible(bool(candidates))
        self.intent_migration_undo.setVisible(bool(undoable))
        if self._intent_migration_recovery_error:
            self.intent_migration_status.setText(
                "Interrupted legacy-rule migration needs attention: "
                f"{self._intent_migration_recovery_error}"
            )
        elif self._intent_migration_recovery_notice:
            self.intent_migration_status.setText(
                self._intent_migration_recovery_notice
            )
        elif candidates:
            self.intent_migration_status.setText(
                f"{len(candidates):,} older rule(s) need an explicit meaning before future Automation can use them safely."
            )
        elif undoable:
            self.intent_migration_status.setText("Legacy rule meanings are classified. The last migration remains recoverable.")
        else:
            self.intent_migration_status.clear()

    def review_rule_intent_migration(self):
        candidates=self.intent_migration.candidates()
        if not candidates:
            QMessageBox.information(self,"No legacy rules need review","Every current user rule already records an explicit meaning."); return
        dialog=RuleIntentMigrationDialog(self.intent_migration,candidates,self)
        if dialog.exec()!=QDialog.Accepted:return
        selections=dialog.selections()
        try:self.intent_migration.apply(selections)
        except Exception as exc:
            QMessageBox.critical(self,"Rule intent was not migrated",str(exc)); return
        self.state.events.publish("knowledge_changed"); self.knowledge_changed.emit(); self.refresh()
        self.header.set_status(
            f"Migrated {len(selections):,} rule meaning(s). Image Knowledge was unchanged; restore remains available."
        )

    def undo_rule_intent_migration(self):
        if QMessageBox.question(
            self,"Restore rule intent",
            "Restore the exact rule records from before the last intent migration? Image Knowledge will remain unchanged.",
        )!=QMessageBox.Yes:return
        try:self.intent_migration.undo_last()
        except Exception as exc:
            QMessageBox.critical(self,"Rule intent was not restored",str(exc)); return
        self.state.events.publish("knowledge_changed"); self.knowledge_changed.emit(); self.refresh()
        self.header.set_status("Restored the exact pre-migration rule records. Image Knowledge was unchanged.")

    def _refresh_projection_rule_repairs(self):
        candidates=self.projection_repairs.candidates()
        undoable=self.projection_repairs.undoable_operation()
        self.projection_repair_panel.setVisible(bool(candidates or undoable))
        self.projection_repair_review.setVisible(bool(candidates))
        self.projection_repair_undo.setVisible(bool(undoable))
        if candidates:
            self.projection_repair_status.setText(
                f"{len(candidates):,} training-projection rule(s) repeat a "
                "prerequisite already handled by a related rule."
            )
        elif undoable:
            self.projection_repair_status.setText(
                "Related projection rules are repaired. The last repair remains recoverable."
            )
        else:
            self.projection_repair_status.clear()

    def review_projection_rule_repairs(self):
        candidates=self.projection_repairs.candidates()
        if not candidates:
            QMessageBox.information(
                self,"No related rule repairs",
                "Current training-projection rules have no safely composable "
                "prerequisites to repair.",
            )
            return
        dialog=ProjectionRuleRepairDialog(candidates,self)
        if dialog.exec()!=QDialog.Accepted:return
        selected=dialog.selected_ids()
        try:self.projection_repairs.apply(selected)
        except Exception as exc:
            QMessageBox.critical(
                self,"Related rules were not repaired",str(exc)
            )
            return
        self.state.events.publish("knowledge_changed")
        self.knowledge_changed.emit(); self.refresh()
        self.header.set_status(
            f"Repaired {len(selected):,} related projection rule(s). "
            "Image Knowledge was unchanged; exact restore remains available."
        )

    def undo_projection_rule_repair(self):
        if QMessageBox.question(
            self,"Restore related rule definitions",
            "Restore the exact rule records from before the last related-rule "
            "repair? Image Knowledge will remain unchanged.",
        )!=QMessageBox.Yes:return
        try:self.projection_repairs.undo_last()
        except Exception as exc:
            QMessageBox.critical(
                self,"Related rules were not restored",str(exc)
            )
            return
        self.state.events.publish("knowledge_changed")
        self.knowledge_changed.emit(); self.refresh()
        self.header.set_status(
            "Restored the exact pre-repair rule records. Image Knowledge was unchanged."
        )

    def edit_rule(self,new=False,suggested=None):
        key=None if new else self.selected_rule(); rule_type="Merge SDXL Tags"; sources=[]; target=""
        selected_record=None
        if new and getattr(self,"_rule_goal",None)=="screen":
            rule_type="Always ignore SDXL Tag"
        if suggested: sources=[suggested[0],suggested[1]]; target=suggested[1]
        if not new and not key: return
        if key and key[0]=="modern":
            record=self.state.knowledge.merge_rules[key[1]]; selected_record=deepcopy(record); sources=list(record.source_tags); target=record.target_tag
        elif key:
            _,origin,kind,index=key
            store=self.repository.defaults if origin=="Default" else self.repository.user
            record=store.data[kind][index]; selected_record=deepcopy(record); rule_type="Always ignore SDXL Tag" if kind=="ignore_rules" else "Merge SDXL Tags"
            if kind=="ignore_rules":
                sources=[str(record.get("tag",""))]
            elif kind=="wildcard_rules":
                remove=record.get("remove",[]); remove=[remove] if isinstance(remove,str) else list(remove); target=str(record.get("pattern","")); sources=[target,*remove]
            else: sources=list(record.get("remove",[])); target=str(record.get("replace",""))
        combined=self.repository.combined(); templates=[]
        for kind in ("merge_rules","wildcard_rules","combine_rules"):
            for record in combined[kind]:
                remove=record.get("remove",[]); templates.extend([remove] if isinstance(remove,str) else remove)
                templates.append(record.get("pattern") or record.get("replace") or "")
        screening=self._rule_goal=="screen" and (new or key is not None and key[0]=="repository" and key[2]=="ignore_rules")
        original_record=selected_record if screening and key else None
        saved_screening=[False]; screening_failed=[False]
        def save_screening(selected_type,source_values,_result):
            if selected_type!="Always ignore SDXL Tag":
                QMessageBox.warning(dialog,"Choose a screening rule","This guided path saves incoming-Tag screening rules. Browse All Rules to create a different rule type.")
                return False
            try:
                self.repository.load()
                if key is None:
                    self.repository.add_rule("ignore_rules",source_values,"")
                else:
                    _,origin,kind,_index=key
                    store=self.repository.defaults if origin=="Default" else self.repository.user
                    matches=[index for index,candidate in enumerate(store.data[kind]) if candidate==original_record]
                    if len(matches)!=1 or not any((row_origin,row_kind,row_index)==(origin,kind,matches[0])
                        for row_origin,row_kind,row_index,_record in self.repository.rules_with_origin()):
                        raise RuntimeError("This saved rule changed or is no longer available. Your choices remain here; cancel and reopen the current rule before replacing it.")
                    self.repository.replace_rule(origin,kind,matches[0],"ignore_rules",source_values,"")
            except Exception as exc:
                screening_failed[0]=True
                QMessageBox.critical(dialog,"Screening rule was not saved",f"{exc}\n\nYour choices remain in this editor. Correct the problem and try Save again, or Cancel without changing the rule.")
                return False
            saved_screening[0]=True
            return True
        saved_general=[False]; general_failed=[False]; saved_analysis=[None]
        def save_general(selected_type,source_values,result):
            analysis=None
            if selected_type=="Merge SDXL Tags":
                exclude_rule_id=key[1] if key and key[0]=="modern" else ""
                exclude_rule_key=(
                    AutomationEngine.projection_rule_key(sources,target)
                    if key and key[0]!="modern" else None
                )
                analysis=self.projection_repairs.analyze_rule(
                    source_values,result,
                    exclude_rule_id=exclude_rule_id,
                    exclude_rule_key=exclude_rule_key,
                )
                if analysis.errors:
                    QMessageBox.warning(dialog,"Cannot save projection rule","\n\n".join(analysis.errors))
                    return False
                if analysis.warnings:
                    QMessageBox.warning(
                        dialog,"Related rule needs review",
                        "\n\n".join(analysis.warnings)
                        + "\n\nEdit the existing related rule instead of creating "
                        "two results for the same matching tags.",
                    )
                    return False
                if analysis.duplicate:
                    QMessageBox.information(
                        dialog,"Rule already exists",
                        "An active training-projection rule already covers these "
                        "matching tags and this result.",
                    )
                    return False
                source_values=list(analysis.sources)
            repository_kind=(
                "ignore_rules" if selected_type=="Always ignore SDXL Tag"
                else "merge_rules"
            )
            repository_destination=_rule_destination_is_repository(
                selected_type,source_values,result,
            )
            source_is_repository=bool(key and key[0]=="repository")
            source_is_modern=bool(key and key[0]=="modern")
            if key and source_is_repository!=repository_destination:
                QMessageBox.warning(
                    dialog,"Rule type conversion needs recovery support",
                    "This edit would move the rule between Automation rules and "
                    "System Knowledge. The original is unchanged and your choices "
                    "remain here, but this two-store conversion cannot be saved "
                    "safely yet. Cancel to keep the original rule; do not delete it "
                    "as a workaround.",
                )
                return False
            if repository_destination and repository_kind=="merge_rules":
                missing=[value for value in [*source_values,result]
                         if "{" not in value and self.state.knowledge.find(value) is None]
                if missing:
                    if hasattr(dialog,"refresh_missing_tags"):
                        dialog.refresh_missing_tags()
                    QMessageBox.warning(dialog,"Create these Tags first",
                        "The rule is still a draft. Create each missing Tag using "
                        "Create Selected Tag below, then review and save this rule separately. "
                        "Missing: " + ", ".join(missing))
                    return False
            try:
                if source_is_repository:
                    _,origin,kind,index=key
                    store=self.repository.defaults if origin=="Default" else self.repository.user
                    if index>=len(store.data[kind]) or store.data[kind][index]!=selected_record:
                        raise RuntimeError(
                            "This saved rule changed after it was opened. Cancel "
                            "and reopen the current rule before saving."
                        )
                    self.repository.replace_rule(
                        origin,kind,index,repository_kind,source_values,result,
                    )
                elif source_is_modern:
                    self.state.knowledge.replace_merge_rule_verified(
                        key[1],selected_record,source_values,result,
                    )
                elif repository_destination:
                    self.repository.add_rule(repository_kind,source_values,result)
                else:
                    self.state.knowledge.add_merge_rule_verified(
                        source_values,result,
                        description="Created as automatic in Rules & Automation",
                        require_review=False,
                    )
            except Exception as exc:
                general_failed[0]=True
                QMessageBox.critical(
                    dialog,"Cannot save rule",
                    f"{exc}\n\nYour choices remain here. Correct the problem "
                    "and try Save again, or Cancel without changing the rule.",
                )
                return False
            saved_general[0]=True; saved_analysis[0]=analysis
            return True
        dialog=RuleEditorDialog(
            self,rule_type=rule_type,sources=sources,target=target,
            knowledge=self.state.knowledge,categories=self.value_binding_categories,known_tags=[
                (
                    record.display,
                    self.state.knowledge.effective_aliases(record),record.id,
                )
                for record in self.state.knowledge.tags.values()
            ],templates=templates,
            template_variables=combined["variables"],
            save_callback=save_screening if screening else save_general,
        )
        if hasattr(dialog,"configure_missing_tags"):
            def missing_tag_names():
                selected_type,source_values,result=dialog.values()
                if (selected_type!="Merge SDXL Tags"
                    or not _rule_destination_is_repository(
                        selected_type,source_values,result,
                    )
                    or key is not None and key[0]!="repository"):
                    return ()
                return tuple(dict.fromkeys(
                    value for value in [*source_values,result]
                    if value and "{" not in value
                    and self.state.knowledge.find(value) is None
                ))

            def tag_saved(record):
                self.state.knowledge.load()
                if record.display.casefold() not in {
                    value.casefold() for value in dialog.known_choices
                }:
                    dialog.known_choices.append(record.display)
                    aliases=self.state.knowledge.effective_aliases(record)
                    dialog.source_choice.addItem(record.display,record.id,search_terms=aliases)
                    dialog.target.addItem(record.display,record.id,search_terms=aliases)
                dialog.source_tags_editor.set_values(dialog.source_tags_editor.values())

            dialog.configure_missing_tags(
                missing_tag_names,
                lambda name,editor:bool(self._create_rule_tag and self._create_rule_tag(name,editor)),
                tag_saved,
            )
        if screening and hasattr(dialog,"rule_type"):
            dialog.rule_type.setEnabled(False)
            dialog.setWindowTitle("Screen Incoming Tag")
            dialog.save_button.setText("Save Screening Rule")
            dialog.template_edit_toggle.hide()
            dialog.help_text.setText("Choose one known SDXL Tag. Template-based rules remain available through Browse All Rules.")
        elif key and hasattr(dialog,"rule_type"):
            def explain_authority_change(*_args):
                selected_type,source_values,result=dialog.values()
                repository_destination=_rule_destination_is_repository(
                    selected_type,source_values,result,
                )
                if (key[0]=="repository") == repository_destination:
                    dialog.type_change_notice.hide()
                    dialog.save_button.setEnabled(not dialog._saving and not dialog._saved_once)
                    dialog.save_button.setToolTip("")
                    return
                if key[0]=="modern":
                    correction=(
                        "Switch back to Merge SDXL Tags to edit this rule."
                        if selected_type=="Always ignore SDXL Tag" else
                        "Remove the template to edit this rule in System Knowledge."
                    )
                    message=("This draft would move a System Knowledge rule into "
                             "Automation rules. Safe conversion is not available yet. "
                             + correction + " The original is unchanged.")
                else:
                    message=(
                        "This draft would move an Automation rule into "
                        "System Knowledge. Safe conversion is not available yet. "
                        "Keep a template or choose Always ignore SDXL Tag to "
                        "edit within Automation rules. The original is unchanged."
                    )
                dialog.type_change_notice.setText(message)
                dialog.type_change_notice.show()
                dialog.save_button.setEnabled(False)
                dialog.save_button.setToolTip(message)
            dialog.rule_type.currentTextChanged.connect(explain_authority_change)
            dialog.target.currentTextChanged.connect(explain_authority_change)
            for signal in (dialog.sources.model().rowsInserted,
                           dialog.sources.model().rowsRemoved,
                           dialog.sources.model().dataChanged):
                signal.connect(explain_authority_change)
            explain_authority_change()
        if dialog.exec()!=QDialog.Accepted:
            if screening_failed[0] or general_failed[0]:self.refresh()
            return
        if saved_screening[0]:
            self.refresh(); self._publish_projection_authority_changed()
            return
        if not saved_general[0]:return
        self.refresh()
        analysis=saved_analysis[0]
        if analysis is not None and analysis.changed:
            removed=", ".join(analysis.redundant_sources)
            self.header.set_status(
                f"Saved the combined projection rule without redundant "
                f"prerequisite(s): {removed}. Existing related rules establish "
                "that reduction."
            )
        self._publish_projection_authority_changed()

    def suggest_rules(self):
        images=list(getattr(self.state.dataset,"writable_images",self.state.dataset.images))
        if not images: QMessageBox.information(self,"Open libraries first","Choose or refresh one or more included libraries before looking for merge suggestions."); return
        if not self._start_rule_task(
            "Looking for repeated SDXL Tag patterns…",cancellable=True,
            context="Merge suggestions",
        ): return
        service=AutomationRuleAuditService(self.engine())
        task=CancelableProgressCallTask(lambda progress,cancelled:service.suggest_merges(images,progress,cancelled)); self._task=task
        task.signals.progress.connect(self._rule_task_progress)
        task.signals.complete.connect(self.rule_suggestions_ready)
        task.signals.cancelled.connect(self._rule_task_cancelled)
        task.signals.failed.connect(self._rule_task_failed)
        self.pool.start(task)

    def rule_suggestions_ready(self,candidates):
        if getattr(self._task,"is_cancelled",lambda:False)():
            self._rule_task_cancelled()
            return
        self._finish_rule_task()
        if self._shutdown_requested:
            self._notify_background_work_finished()
            return
        self.suggestion_tree.clear()
        for candidate in candidates:
            sources=f"{candidate.broad_tag} + {candidate.specific_tag}"; examples=", ".join(candidate.examples)
            item=QTreeWidgetItem((sources,candidate.specific_tag,f"{candidate.image_count:,}",examples)); item.setData(0,Qt.UserRole,(candidate.broad_tag,candidate.specific_tag)); self.suggestion_tree.addTopLevelItem(item)
        self.suggestion_summary.setText(f"Merge suggestions: {len(candidates):,}. Review a suggestion before adding it.")
        if candidates:self.rule_tree.clearSelection()
        self._suggestions_visible=bool(candidates)
        self._sync_rule_suggestions_view()
        if candidates: self.suggestion_tree.setCurrentItem(self.suggestion_tree.topLevelItem(0))
        self.header.set_status(
            "Merge suggestions are ready" if candidates and self._rule_goal in ("captions","all")
            else "Caption merge suggestions are ready; open Build Training Captions to review them" if candidates
            else "No new repeated broad/specific SDXL Tag patterns were found"
        )
        self._notify_background_work_finished()

    def use_rule_suggestion(self):
        if self._rule_goal not in ("captions","all") or not self._suggestions_visible:
            return
        item=self.suggestion_tree.currentItem()
        if not item: return
        suggested=item.data(0,Qt.UserRole); self.hide_rule_suggestions(); self.edit_rule(new=True,suggested=suggested)

    def hide_rule_suggestions(self):
        self._suggestions_visible=False
        self._sync_rule_suggestions_view()

    def delete_rule(self):
        keys=self.selected_rules()
        if not keys or not self._compatible_rule_selection(keys):return
        count=len(keys)
        if keys[0][0]=="modern":
            question=(
                f"Delete {count:,} selected System Knowledge rule{'s' if count!=1 else ''}?\n\n"
                "This removes only the selected reusable projection rules. "
                "No Image Knowledge, Training TXT, Metadata, or image files will change. "
                "A verified System Knowledge backup will be created before saving."
            )
        else:
            user=sum(key[1]=="User" for key in keys); built_in=count-user; consequences=[]
            if user:consequences.append(f"permanently delete {user:,} My rule{'s' if user!=1 else ''}")
            if built_in:consequences.append(f"disable {built_in:,} built-in rule{'s' if built_in!=1 else ''}")
            question=(
                f"Delete {count:,} selected Automation rule{'s' if count!=1 else ''}?\n\n"
                f"This will {' and '.join(consequences)}. No Image Knowledge, Training TXT, "
                "Metadata, or image files will change. A verified Automation-rule backup "
                "will be created before saving."
            )
        if QMessageBox.question(self,"Delete selected rules",question)!=QMessageBox.Yes:return
        try:
            if keys[0][0]=="modern":
                self.state.knowledge.delete_merge_rules_verified(key[1] for key in keys)
            else:
                self.repository.delete_rules_verified(key[1:] for key in keys)
        except Exception as exc:
            QMessageBox.critical(self,"Rules were not deleted",str(exc)); self.refresh(); return
        self.refresh()
        self.header.set_status(
            f"Deleted {count:,} selected rule{'s' if count!=1 else ''}. "
            "Image data and exported files were unchanged."
        )
        self._publish_projection_authority_changed()

    def selected_variable(self):
        item=self.value_tree.currentItem(); return item.data(0,Qt.UserRole) if item else None

    def _variable_selection_changed(self):
        ready=(
            self.selected_variable() is not None
            and getattr(self,"_value_authority_report",None) is not None
        )
        self.preview_value_migration_button.setEnabled(ready)
        selected=self.selected_variable()
        family=next((
            item for item in getattr(
                getattr(self,"_value_authority_report",None),"families",(),
            )
            if selected is not None
            and item.origin==selected[1]
            and item.name==AutomationEngine._variable_key(selected[2])
        ),None)
        self.review_value_authority_button.setEnabled(
            bool(ready and family is not None and family.values)
        )

    def review_variable_authority(self):
        selected=self.selected_variable()
        report=getattr(self,"_value_authority_report",None)
        if selected is None or report is None:return
        _,origin,name=selected
        key=AutomationEngine._variable_key(name)
        family=next((
            item for item in report.families
            if item.origin==origin and item.name==key
        ),None)
        if family is None:return
        dialog=ReusableValueAuthorityDecisionDialog(family,self)
        if dialog.exec()!=QDialog.Accepted:return
        try:
            preview=self.value_binding_migration.plan_authority_decision(
                self.repository.combined(),family,
                decision=dialog.decision(),rationale=dialog.rationale(),
                reviewed_values=dialog.reviewed_values(),
            )
        except (RuntimeError,ValueError) as exc:
            QMessageBox.critical(
                self,"Authority decision unavailable",str(exc),
            ); return
        if not preview.ready:
            QMessageBox.critical(
                self,"Authority decision incomplete","\n".join(preview.blockers),
            ); return
        action=(
            "Retire this unused list from effective Automation values"
            if preview.decision=="retire_unused" else
            "Retain these strings as a distinct lexical wildcard type"
        )
        if QMessageBox.question(
            self,"Apply reusable-value authority decision",
            f"{action} for {{{preview.family.name}}}?\n\n"
            "The reviewed rationale and exact values are recorded in My "
            "Automation rules through one verified transaction. Tags and Image "
            "Knowledge remain unchanged, and an exact restore is available.",
        )!=QMessageBox.Yes:return
        previous=self.value_binding_migration.authority_decision(
            preview.family.name,
        )
        try:
            operation_id=self.value_binding_migration.apply_authority_decision(
                preview,
            )
        except Exception as exc:
            QMessageBox.critical(
                self,"Authority decision was not saved",str(exc),
            ); self.refresh(); return
        self.state.events.publish("knowledge_changed")
        self.knowledge_changed.emit(); self.refresh()
        self.header.set_status(
            (
                "The current reusable-value authority decision was already saved and verified."
                if previous is not None and previous.get("operation_id")==operation_id
                else
                f"Saved the reviewed {{{preview.family.name}}} authority decision. "
                "Tags and Image Knowledge were unchanged; exact restore is available."
            )
        )

    def preview_variable_migration(self):
        selected=self.selected_variable()
        report=getattr(self,"_value_authority_report",None)
        service=getattr(self,"_value_authority_service",None)
        if selected is None or report is None or service is None:return
        _,origin,name=selected
        key=AutomationEngine._variable_key(name)
        family=next((
            item for item in report.families
            if item.origin==origin and item.name==key
        ),None)
        if family is None:return
        try:
            preview=service.preview_migration(self.repository.combined(),family)
        except (RuntimeError,ValueError) as exc:
            QMessageBox.critical(
                self,"Compatibility preview unavailable",str(exc),
            )
            return
        if family.classification=="stable_tag_review":
            semantic_dialog=ReusableValueSemanticReviewDialog(preview,self)
            if semantic_dialog.exec()!=QDialog.Accepted:return
            try:
                preview=service.preview_migration(
                    self.repository.combined(),family,
                    reviewed_stable_values=semantic_dialog.reviewed_values(),
                )
            except (RuntimeError,ValueError) as exc:
                QMessageBox.critical(
                    self,"Stable Tag meaning review changed",str(exc),
                )
                return
        dialog=ReusableValueMigrationPreviewDialog(preview,self)
        if dialog.exec()!=QDialog.Accepted:return
        if QMessageBox.question(
            self,"Apply stable Tag bindings",
            f"Save {len(preview.proposed_bindings):,} reviewed stable Tag "
            f"binding(s) for {{{preview.family.name}}}?\n\n"
            "The exact legacy strings and matching rules remain unchanged. "
            "Only companion metadata is saved to My Automation rules in one "
            "verified transaction. A backup and exact restore remain available; "
            "Tags and Image Knowledge are not changed.",
        )!=QMessageBox.Yes:return
        previous=self.value_binding_migration.binding_record(
            preview.family.name,
        )
        try:
            operation_id=self.value_binding_migration.apply(preview)
        except Exception as exc:
            QMessageBox.critical(
                self,"Stable Tag bindings were not saved",str(exc),
            )
            self.refresh(); return
        self.state.events.publish("knowledge_changed")
        self.knowledge_changed.emit(); self.refresh()
        self.header.set_status(
            (
                "Current stable Tag bindings were already saved and verified."
                if previous is not None and previous.get("operation_id")==operation_id
                else
                f"Saved {len(preview.proposed_bindings):,} stable Tag binding(s). "
                "Legacy strings and Image Knowledge were unchanged; exact restore is available."
            )
        )

    def _refresh_value_binding_history(self):
        undoable=self.value_binding_migration.undoable_operation()
        message=(
            self._value_binding_recovery_error
            or self._value_binding_recovery_notice
        )
        self.value_binding_history_panel.setVisible(bool(undoable or message))
        self.value_binding_undo.setVisible(bool(undoable))
        if self._value_binding_recovery_error:
            self.value_binding_history_status.setText(
                "Interrupted reusable-value authority recovery needs attention: "+
                self._value_binding_recovery_error
            )
        elif self._value_binding_recovery_notice:
            self.value_binding_history_status.setText(
                self._value_binding_recovery_notice
            )
        elif undoable:
            family=str(undoable.metadata.get("family") or "legacy values")
            label=(
                "Reusable-value authority decision"
                if undoable.kind=="automation_variable_authority_decision" else
                "Stable Tag companion decision"
            )
            self.value_binding_history_status.setText(
                f"{label} for {{{family}}} is saved and remains exactly recoverable."
            )
        else:self.value_binding_history_status.clear()

    def undo_variable_binding_migration(self):
        if QMessageBox.question(
            self,"Restore reusable-value authority",
            "Restore the exact My Automation rules payload from before the last "
            "reusable-value authority decision? Tags and Image Knowledge remain "
            "unchanged.",
        )!=QMessageBox.Yes:return
        try:self.value_binding_migration.undo_last()
        except Exception as exc:
            QMessageBox.critical(
                self,"Reusable-value authority was not restored",str(exc),
            ); return
        self.state.events.publish("knowledge_changed")
        self.knowledge_changed.emit(); self.refresh()
        self.header.set_status(
            "Restored the exact pre-decision Automation payload. Tags and Image "
            "Knowledge were unchanged."
        )

    def add_variable(self): self.edit_variable(new=True)

    def edit_variable(self,new=False):
        selected=None if new else self.selected_variable()
        if not new and not selected: return
        old="" if not selected else selected[2]
        current=list(self.repository.combined()["variables"].get(old,[])) if selected else []
        row=self.value_tree.currentItem() if selected else None
        consumers=row.toolTip(3) if row is not None else ""
        def save(name,values):
            clean_name=name.strip().strip("{}")
            if not clean_name:raise ValueError("Enter a list name.")
            if not selected and any(
                AutomationEngine._variable_key(name)==AutomationEngine._variable_key(clean_name)
                for name in self.repository.combined()["variables"]
            ):
                raise ValueError(f"Reusable values for ‘{clean_name}’ already exist. Select that row and choose Edit.")
            if selected:
                self.repository.edit_variable(
                    selected[1],old,clean_name,values,expected_values=current,
                )
            else:self.repository.set_variable(clean_name,values)
        dialog=ReusableValueEditorDialog(
            self,name=old,values=current,consumers=consumers,save_callback=save,
        )
        accepted=dialog.exec()==QDialog.Accepted
        if not accepted and not dialog.save_failed:return
        selected_name=(dialog.name_input.text().strip().strip("{}") if accepted else old)
        self.refresh()
        if accepted:
            self.header.set_status(f"Saved legacy value list ‘{selected_name}’. Rules using its wildcard will read the new values.")
            self._publish_projection_authority_changed()
        for index in range(self.value_tree.topLevelItemCount()):
            item=self.value_tree.topLevelItem(index)
            if item.data(0,Qt.UserRole)[2]==selected_name:
                self.value_tree.setCurrentItem(item);break

    def delete_variable(self):
        selected=self.selected_variable()
        if not selected: return
        _,origin,name=selected
        if QMessageBox.question(self,"Delete reusable values",f"Delete ‘{name}’? Rules using {{{name}}} will no longer match.")==QMessageBox.Yes:
            try:self.repository.delete_variable(origin,name)
            except Exception as exc:
                QMessageBox.critical(self,"Values were not deleted",str(exc)); self.refresh(); return
            self.refresh()
            self._publish_projection_authority_changed()

    def _publish_projection_authority_changed(self):
        self.state.events.publish("knowledge_changed")
        self.knowledge_changed.emit()

    def undo(self):
        changes=self.checkpoint.changes()
        if not changes:
            self.legacy_recovery_panel.hide()
            return
        if QMessageBox.question(self,"Undo automation batch",f"Restore {len(changes):,} image(s) to their state before the last automation batch?")!=QMessageBox.Yes: return
        if not self._start_rule_task(
            "Restoring the historical Automation checkpoint…",
            cancellable=False,context="Legacy recovery",
        ): return
        def work(progress):
            for index,change in enumerate(reversed(changes),1): self.engine().restore(change,self.state.knowledge); progress(index,len(changes),Path(change.image).name)
            self.checkpoint.clear(); return len(changes)
        task=ProgressCallTask(work); self._task=task
        task.signals.progress.connect(self._rule_task_progress)
        task.signals.complete.connect(self.undo_ready)
        task.signals.failed.connect(self._rule_task_failed)
        self.pool.start(task)

    def undo_ready(self,count):
        self._finish_rule_task(
            f"Restored {int(count):,} image(s) from the historical Automation checkpoint."
        )
        self.legacy_recovery_panel.hide()
        if not self._shutdown_requested:self.refresh_requested.emit()
        self._notify_background_work_finished()

    def _start_rule_task(self,message,*,cancellable,context):
        if self._task is not None:
            show_operation_in_progress(
                self,self._rule_task_context or "Automation work",
            )
            return False
        started,active=self.state.begin_long_operation(
            "automation",message or "Automation work",
        )
        if not started:
            show_operation_in_progress(self,active)
            return False
        self._rule_task_context=str(context)
        self.rule_task_status.setText(message)
        self.template_task_status.setText(message)
        for button in (
            self.suggest_button,self.add_rule_button,self.edit_rule_button,
            self.delete_rule_button,self.legacy_recovery_button,
            self.discover_templates_button,self.build_template_button,
            self.edit_template_button,self.review_template_button,
            self.preview_template_organization_button,
            self.create_projection_rule_button,self.review_template_policy_button,
            self.run_template_generation_button,
            self.template_organization_undo,
        ):
            button.setEnabled(False)
        self.rule_cancel_button.setVisible(cancellable)
        self.rule_cancel_button.setEnabled(cancellable)
        self.template_cancel_button.setVisible(cancellable)
        self.template_cancel_button.setEnabled(cancellable)
        return True

    def _finish_rule_task(self,message=""):
        self.state.finish_long_operation("automation")
        self._task=None
        self._rule_task_context=""
        for button in (
            self.suggest_button,self.add_rule_button,self.edit_rule_button,
            self.delete_rule_button,self.legacy_recovery_button,
            self.discover_templates_button,self.build_template_button,
            self.edit_template_button,self.review_template_button,
            self.preview_template_organization_button,
            self.create_projection_rule_button,self.review_template_policy_button,
            self.run_template_generation_button,
            self.template_organization_undo,
        ):
            button.setEnabled(True)
        self._rule_selection_changed()
        self._template_selection_changed()
        self.rule_cancel_button.hide()
        self.template_cancel_button.hide()
        if message:
            self.rule_task_status.setText(message)
            self.template_task_status.setText(message)
        self.legacy_recovery_panel.setVisible(self.checkpoint.exists())

    def _rule_task_progress(self,index,total,name):
        self.state.update_long_operation("automation",index,total,name)
        prefix=f"{self._rule_task_context}: " if self._rule_task_context else ""
        message=f"{prefix}{index:,} of {total:,}: {name}"
        self.rule_task_status.setText(message)
        self.template_task_status.setText(message)

    def cancel_rule_task(self):
        if hasattr(self._task,"cancel"):
            self.state.request_long_operation_cancel("automation")
            self.rule_cancel_button.setEnabled(False)
            self.template_cancel_button.setEnabled(False)
            message=(
                "Cancellation is honored until the verified template save begins; an atomic save already in progress will finish safely…"
                if self._rule_task_context in {
                    "Reusable Tag template approval","Structured reusable Tag template approval",
                    "Reusable Tag organization apply",
                } else
                "Stopping safely after the current System Knowledge Tag…"
                if self._rule_task_context in {
                    "Reusable Tag family discovery","Reusable Tag family expansion preview",
                    "Reusable Tag organization preview",
                } else
                "Stopping safely after the current image…"
            )
            self.rule_task_status.setText(message)
            self.template_task_status.setText(message)
            self._task.cancel()

    def _review_template_storage_issue(self):
        if not getattr(self,"_template_storage_issue",""):return
        QMessageBox.warning(
            self,"Reusable Tag Template Storage Is Held",
            f"{self._template_storage_issue}\n\n"
            f"User Automation storage: {self.repository.user.path}\n"
            f"Shipped Automation storage: {self.repository.defaults.path}\n\n"
            "CollectiGen will not write reviewed templates while this collection is ambiguous. Restore a verified Automation backup or open the file with a compatible newer CollectiGen version; no Tag, Category, or Image Knowledge record was changed.",
        )

    def _rule_task_cancelled(self):
        message=(
            "Stopped safely. No System Knowledge or Image Knowledge changed."
            if self._rule_task_context in {
                "Reusable Tag family discovery","Reusable Tag family expansion preview",
                "Reusable Tag template approval","Structured reusable Tag template approval",
                "Reusable Tag organization preview",
                "Reusable Tag organization apply",
            } else
            "Stopped safely. No rules were changed."
        )
        self._finish_rule_task(message)
        self._notify_background_work_finished()

    def _rule_task_failed(self,message):
        context=self._rule_task_context or "Automation work"
        if context in {"Reusable Tag template approval","Structured reusable Tag template approval"}:
            self._finish_rule_task(
                "Reusable Tag template approval stopped. The Automation repository was left or restored unchanged; no Tags or Image Knowledge changed."
            )
        elif context=="Reusable Tag organization apply":
            self._finish_rule_task(
                "Reusable Tag organization stopped. The atomic System Knowledge transaction was left or restored unchanged; Image Knowledge and files were not touched."
            )
        elif context=="Reusable Tag organization restore":
            self._finish_rule_task(
                "Reusable Tag organization restore stopped without overwriting newer System Knowledge."
            )
        elif context in {
            "Reusable Tag family discovery","Reusable Tag family expansion preview",
            "Reusable Tag organization preview",
        }:
            self._finish_rule_task(
                ("Reusable Tag organization preview stopped. "
                 if context=="Reusable Tag organization preview" else
                 "Reusable Tag family expansion preview stopped. "
                 if context=="Reusable Tag family expansion preview" else
                 "Reusable Tag family discovery stopped. ")
                +"No System Knowledge or Image Knowledge changed."
            )
        else:
            self._finish_rule_task(
                f"{context} stopped. Historical recovery remains available when needed."
            )
        if not self._shutdown_requested:
            QMessageBox.critical(self,"Automation stopped",message)
        self._notify_background_work_finished()

    def has_active_background_work(self) -> bool:
        return (
            getattr(self,"_task",None) is not None
            or getattr(self,"_capability_task",None) is not None
            or getattr(self,"_conflict_projection_task",None) is not None
        )

    def request_background_shutdown(self) -> bool:
        self._shutdown_requested=True
        active=False
        self._pending_conflict_editor_open=False
        if self._conflict_projection_task is not None:
            active=True
            self._conflict_projection_task.cancel()
            self._conflict_projection_task=None
            QTimer.singleShot(0,self._notify_background_work_finished)
        capability_task=getattr(self,"_capability_task",None)
        if capability_task is not None:
            active=True; capability_task.cancel(); self._capability_task=None
            QTimer.singleShot(0,self._notify_background_work_finished)
        if self._task is None:return active
        active=True
        if hasattr(self._task,"cancel"):
            self.state.request_long_operation_cancel("automation")
            self._task.cancel()
            message="Closing after Automation reaches a safe stopping point…"
            self.rule_task_status.setText(message)
            self.template_task_status.setText(message)
        else:
            message="Closing after historical recovery finishes safely…"
            self.rule_task_status.setText(message)
            self.template_task_status.setText(message)
        return active

    def _notify_background_work_finished(self):
        if not self.has_active_background_work():
            self.background_work_finished.emit()
