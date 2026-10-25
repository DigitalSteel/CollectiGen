from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, QPoint
from PySide6.QtWidgets import (
    QComboBox, QDialogButtonBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QScrollArea, QSpinBox, QTabWidget, QTextEdit, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.model_workflow_guidance import (
    GenerationAdapterAvailability, GenerationConfigurationRecommendation, GuidanceRequest,
    MachineCapabilityProfile, ModelWorkflowGuidanceService, ModelWorkflowKnowledgeError,
    ResourceBinding, WorkflowGuidancePlan, validate_generation_configuration,
)
from core.training_configuration import TrainingConfiguration, validate_training_configuration
from core.training_presets import preview_training_preset
from .modal_feedback import FeedbackEnabledDialog


class ModelWorkflowGuidanceDialog(FeedbackEnabledDialog):
    """Goal-to-plan preview. It never installs resources or starts a job."""

    feedback_workflow = "Model-aware workflow guidance"

    def __init__(
        self,
        service: ModelWorkflowGuidanceService,
        machine: MachineCapabilityProfile,
        *,
        registry_snapshot=None,
        verified_resource_ids=(),
        available_adapters=(),
        available_generation_adapters=(),
        current_configuration: TrainingConfiguration | None = None,
        current_generation_configuration: GenerationConfigurationRecommendation | None = None,
        caption_previews=(),
        image_count: int = 0,
        workflow_kind: str = "training",
        allow_workflow_switch: bool = True,
        registry=None,
        jobs=None,
        protected_roots=(),
        selection_context="",
        main_model_path="",
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.service = service
        self.machine = machine
        self.registry_snapshot = registry_snapshot
        self.verified_resource_ids = tuple(verified_resource_ids)
        self.available_adapters = tuple(available_adapters)
        self.available_generation_adapters = tuple(available_generation_adapters)
        self.current_configuration = current_configuration
        self.current_generation_configuration = current_generation_configuration
        self._generation_restore_requested = False
        self.registry = registry
        self.jobs = jobs
        self._guided_generation = workflow_kind == "generation" and not allow_workflow_switch
        self.protected_roots = tuple(protected_roots)
        self.main_model_path = str(main_model_path).strip()
        self.resource_bindings: dict[str, str] = {}
        self.caption_previews = tuple(
            (str(label)[:160], str(caption)[:8_000])
            for label, caption in tuple(caption_previews)[:20]
        )
        self.image_count = max(0, int(image_count))
        self.plan: WorkflowGuidancePlan | None = None
        self._accepted_configuration: TrainingConfiguration | None = None
        self._accepted_generation_configuration: GenerationConfigurationRecommendation | None = None
        self._accepted_profile = None
        self._pending_update_path: Path | None = None
        self._pending_update_fingerprint = ""
        self._pending_update_source_fingerprint = ""
        self._knowledge_recovery_pending = False
        self.setWindowTitle("Plan for a Model and This Computer")
        self.resize(920, 720)
        self.setMaximumHeight(900)
        outer_layout = QVBoxLayout(self)
        self.content_scroll = QScrollArea()
        self.content_scroll.setWidgetResizable(True)
        self.content_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.content_scroll.setAccessibleName("Scrollable model guidance content")
        content = QWidget()
        layout = QVBoxLayout(content)
        self.content_scroll.setWidget(content)
        outer_layout.addWidget(self.content_scroll, 1)

        title = QLabel("Describe the image you want, then review a recommendation"
            if self._guided_generation else "Describe the result you want, then inspect a compatible plan")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        intro = QLabel(
            "This reviews settings for your Generate draft. It does not download models, prepare files, or start generation."
            if self._guided_generation else
            "CollectiGen combines dated model evidence with Setup's machine snapshot. "
            "This dialog previews captions and settings; it does not download, install, or launch anything."
        )
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.selection_context=QLabel(str(selection_context))
        self.selection_context.setWordWrap(True)
        self.selection_context.setTextFormat(Qt.PlainText)
        self.selection_context.setVisible(bool(selection_context))
        layout.addWidget(self.selection_context)

        form = QFormLayout()
        self._form = form
        self.workflow = QComboBox()
        self.workflow.addItem("Training", "training")
        self.workflow.addItem("Generation", "generation")
        workflow_index = self.workflow.findData(workflow_kind)
        if workflow_index < 0:
            raise ValueError("workflow_kind must be training or generation")
        self.workflow.setCurrentIndex(workflow_index)
        self.workflow.setEnabled(bool(allow_workflow_switch))
        self.workflow.setAccessibleName("Workflow kind")
        form.addRow("Workflow", self.workflow)
        self.goal = QLineEdit()
        self.goal.setMaxLength(500)
        self.goal.setPlaceholderText("For example: a studio portrait with soft window light"
            if self._guided_generation else "For example: teach this character across varied settings")
        self.goal.setAccessibleName("Workflow goal")
        form.addRow("Goal", self.goal)
        self.goal_kind = QComboBox()
        for label, value in (
            ("Character", "character"), ("Style", "style"), ("Product", "product"),
            ("Outfit", "outfit"), ("Action", "action"), ("Concept", "concept"),
            ("Video", "video"),
        ):
            self.goal_kind.addItem(label, value)
        form.addRow("Result type", self.goal_kind)
        self.target = QComboBox()
        self.target.setAccessibleName("Target model and exact version")
        form.addRow("Target", self.target)
        if self._guided_generation:
            form.labelForField(self.target).setText("Model version")
        self.priority = QComboBox()
        self.priority.addItem("Balanced", "balanced")
        self.priority.addItem("Faster turnaround", "turnaround")
        self.priority.addItem("Lower resource use", "lower_resources")
        self.priority.addItem("Fidelity priority", "fidelity")
        form.addRow("Priority", self.priority)
        self.base_model_id = QLineEdit()
        self.base_model_id.setPlaceholderText("Exact sha256: identity from the model workspace")
        self.base_model_id.setAccessibleName("Exact registered base model identity")
        if current_configuration is not None:
            self.base_model_id.setText(current_configuration.base_model_id)
        form.addRow("Local model identity", self.base_model_id)
        layout.addLayout(form)

        resources = QGroupBox("Model resources" if self._guided_generation else "Companion resources for this workflow")
        resources_layout = QVBoxLayout(resources)
        self.customize_model_set = QPushButton("Customize Model Set and Details…")
        self.customize_model_set.setCheckable(True)
        self.customize_model_set.setAccessibleName("Customize model set and recommendation details")
        self.customize_model_set.toggled.connect(self._set_customize_visible)
        if self._guided_generation:
            resources_layout.addWidget(self.customize_model_set, alignment=Qt.AlignLeft)
        else:
            self.customize_model_set.hide()
        self.companion_tree = QTreeWidget()
        self.companion_tree.setHeaderLabels(["Slot", "Role", "Required model", "Selected identity", "Readiness"])
        self.companion_tree.setRootIsDecorated(False)
        self.companion_tree.setMaximumHeight(150)
        self.companion_tree.setAccessibleName("Companion resource requirements and selections")
        resources_layout.addWidget(self.companion_tree)
        self.companion_empty_status = QLabel(
            "The catalog lists no companion files for this model version. "
            "The selected model and generator still need separate checks."
        )
        self.companion_empty_status.setWordWrap(True)
        self.companion_empty_status.setTextFormat(Qt.PlainText)
        self.companion_empty_status.hide()
        resources_layout.addWidget(self.companion_empty_status)
        self.binding_controls = QWidget()
        binding_row = QHBoxLayout(self.binding_controls)
        binding_row.setContentsMargins(0, 0, 0, 0)
        self.companion_slot = QComboBox()
        self.companion_slot.setAccessibleName("Companion slot")
        self.companion_id = QLineEdit()
        self.companion_id.setPlaceholderText("Exact registered resource identity")
        self.companion_id.setAccessibleName("Exact companion resource identity")
        self.choose_companion_button = QPushButton("Choose in Models…")
        self.choose_companion_button.clicked.connect(self._choose_companion)
        binding_row.addWidget(self.companion_slot)
        binding_row.addWidget(self.companion_id, 1)
        binding_row.addWidget(self.choose_companion_button)
        resources_layout.addWidget(self.binding_controls)
        self.required_resources_button = QPushButton("Check Required Model Files…" if self._guided_generation
            else "Review Required Resources and Acquisition…")
        self.required_resources_button.clicked.connect(self._open_required_resources)
        self.required_resources_button.setEnabled(registry is not None and jobs is not None)
        resources_layout.addWidget(self.required_resources_button)
        layout.addWidget(resources)
        self.preview_button = QPushButton("Review Recommendation" if self._guided_generation else "Build Preview")
        self.preview_button.setProperty("primary", True)
        self.preview_button.clicked.connect(self.build_preview)
        layout.addWidget(self.preview_button)
        self.action_status = QLabel()
        self.action_status.setWordWrap(True)
        self.action_status.setTextFormat(Qt.PlainText)
        self.action_status.hide()
        layout.addWidget(self.action_status)

        self.tabs = QTabWidget()
        overview = QWidget(); overview_layout = QVBoxLayout(overview)
        status_group = QGroupBox("Evidence and readiness are separate")
        status_layout = QFormLayout(status_group)
        self.catalog_state = QLabel("Choose a goal and target.")
        self.adapter_state = QLabel("Not evaluated")
        self.machine_state = QLabel("Not evaluated")
        for widget in (self.catalog_state, self.adapter_state, self.machine_state):
            widget.setWordWrap(True); widget.setTextFormat(Qt.PlainText)
        status_layout.addRow("Catalog", self.catalog_state)
        status_layout.addRow("Tested adapter", self.adapter_state)
        status_layout.addRow("This machine", self.machine_state)
        overview_layout.addWidget(status_group)
        self.explanation = QLabel("No plan has been built.")
        self.explanation.setWordWrap(True); self.explanation.setTextFormat(Qt.PlainText)
        overview_layout.addWidget(self.explanation)
        self.issues = QTreeWidget()
        self.issues.setHeaderLabels(["State", "What needs attention", "Next action"])
        self.issues.setRootIsDecorated(False)
        self.issues.setAccessibleName("Model workflow prerequisites")
        self.issues.setColumnWidth(0, 110); self.issues.setColumnWidth(1, 340)
        overview_layout.addWidget(self.issues, 1)
        self.tabs.addTab(overview, "Plan")

        dataset = QWidget(); dataset_layout = QVBoxLayout(dataset)
        self.dataset_preview = QTextEdit()
        self.dataset_preview.setReadOnly(True)
        self.dataset_preview.setAccessibleName("Version-aware dataset and caption preview")
        self.dataset_preview.setPlainText(
            "The selected caption profile will appear here. Every confirmed Image Knowledge fact remains in its authority record even when this projection omits it."
        )
        dataset_layout.addWidget(self.dataset_preview)
        self.tabs.addTab(dataset, "Dataset Preview")

        generation = QWidget(); generation_layout = QVBoxLayout(generation)
        self.generation_preview = QTextEdit()
        self.generation_preview.setReadOnly(True)
        self.generation_preview.setAccessibleName("Generation recommendation preview")
        self.generation_preview.setPlainText(
            "Choose Generation and build a preview. Applying a recommendation only revises the neutral request; it does not connect or run a generator."
        )
        generation_layout.addWidget(self.generation_preview)
        self._generation_restore_row = QHBoxLayout()
        self._generation_restore_row.addStretch(1)
        generation_layout.addLayout(self._generation_restore_row)
        self.tabs.addTab(generation, "Generation Preview")

        evidence = QWidget(); evidence_layout = QVBoxLayout(evidence)
        self.evidence_text = QTextEdit(); self.evidence_text.setReadOnly(True)
        self.evidence_text.setAccessibleName("Dated model workflow sources")
        self.evidence_text.setPlainText("Build a preview to inspect its dated source attribution.")
        evidence_layout.addWidget(self.evidence_text, 1)
        update_row = QHBoxLayout()
        self.preview_update_button = QPushButton("Preview Knowledge File…")
        self.preview_update_button.clicked.connect(self._choose_update_file)
        self.apply_update_button = QPushButton("Apply Validated Update")
        self.apply_update_button.clicked.connect(self._apply_update_file)
        self.apply_update_button.setEnabled(False)
        self.recover_update_button = QPushButton("Recover / Reload Knowledge")
        self.recover_update_button.clicked.connect(self._recover_update)
        update_row.addWidget(self.preview_update_button); update_row.addWidget(self.apply_update_button)
        update_row.addWidget(self.recover_update_button); update_row.addStretch(1)
        evidence_layout.addLayout(update_row)
        self.update_status = QLabel(
            "Optional local JSON updates are validated before changing your saved knowledge overlay. The shipped baseline stays unchanged."
        )
        self.update_status.setWordWrap(True); self.update_status.setTextFormat(Qt.PlainText)
        evidence_layout.addWidget(self.update_status)
        self._knowledge_sources_tab = self.tabs.addTab(evidence, "Sources && Updates")

        expert = QWidget(); expert_layout = QVBoxLayout(expert)
        self.expert_intro = QLabel(
            "These are the same settings used by the beginner preview. Changes remain explicit and reversible."
        )
        self.expert_intro.setWordWrap(True); expert_layout.addWidget(self.expert_intro)
        expert_form = QFormLayout()
        self.resolution = QSpinBox(); self.resolution.setRange(256, 2048); self.resolution.setSingleStep(64)
        self.batch_size = QSpinBox(); self.batch_size.setRange(1, 64)
        self.accumulation = QSpinBox(); self.accumulation.setRange(1, 128)
        self.precision = QComboBox()
        for value in ("bf16", "fp16", "float32"):
            self.precision.addItem(value.upper(), value)
        expert_form.addRow("Target resolution", self.resolution)
        expert_form.addRow("Batch size", self.batch_size)
        expert_form.addRow("Gradient accumulation", self.accumulation)
        expert_form.addRow("Precision request", self.precision)
        expert_layout.addLayout(expert_form)
        expert_layout.addWidget(QLabel("Reason for expert changes"))
        self.override_reason = QTextEdit(); self.override_reason.setFixedHeight(70)
        self.override_reason.setAccessibleName("Model workflow override reason")
        expert_layout.addWidget(self.override_reason)
        row = QHBoxLayout()
        self._expert_restore_row = row
        self.restore_button = QPushButton("Restore Recommendation")
        self.restore_button.clicked.connect(self.restore_recommendation)
        row.addWidget(self.restore_button); row.addStretch(1)
        expert_layout.addLayout(row)
        self.expert_status = QLabel("Build a preview to edit its exact configuration.")
        self.expert_status.setWordWrap(True); expert_layout.addWidget(self.expert_status)
        expert_layout.addStretch(1)
        self.tabs.addTab(expert, "Expert Settings")
        if self._guided_generation:
            self.tabs.setTabText(0, "Recommendation")
        self.tabs.currentChanged.connect(self._reveal_generation_preview)
        layout.addWidget(self.tabs, 1)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.use_button = QPushButton("Use This Plan")
        self.use_button.setProperty("primary", True)
        self.use_button.clicked.connect(self._accept_plan)
        self.buttons.addButton(self.use_button, QDialogButtonBox.AcceptRole)
        self.buttons.rejected.connect(self.reject)
        outer_layout.addWidget(self.buttons)
        for widget in (self.goal, self.base_model_id):
            widget.textChanged.connect(self._input_changed)
        self.workflow.currentIndexChanged.connect(self._refresh_targets)
        self.goal_kind.currentIndexChanged.connect(self._refresh_targets)
        self.target.currentIndexChanged.connect(self._target_changed)
        self.priority.currentIndexChanged.connect(self._input_changed)
        self.companion_slot.currentIndexChanged.connect(self._load_binding)
        self.companion_id.textChanged.connect(self._binding_changed)
        for widget in (self.resolution, self.batch_size, self.accumulation):
            widget.valueChanged.connect(self._expert_changed)
        self.precision.currentIndexChanged.connect(self._expert_changed)
        self.override_reason.textChanged.connect(self._expert_changed)
        self._refresh_targets()
        if self._guided_generation:
            self._form.setRowVisible(self.workflow, False)
            self._set_customize_visible(False)
        self._set_expert_enabled(False)
        recovery = self.service.update_recovery_state()
        if recovery.pending:
            self._knowledge_recovery_pending = True
            self.update_status.setText(recovery.summary)
            self._show_knowledge_recovery()
        self._update_actions()

    def _set_customize_visible(self, visible: bool) -> None:
        if not self._guided_generation:
            return
        self._form.setRowVisible(self.base_model_id, visible)
        has_companions = bool(self._current_requirements())
        self.companion_tree.setVisible(visible and has_companions)
        self.binding_controls.setVisible(visible and has_companions)
        self.companion_empty_status.setVisible(visible and not has_companions)
        if not visible and self.tabs.currentIndex() != 0:
            self.tabs.setCurrentIndex(0)
        for index in range(1, self.tabs.count()):
            self.tabs.setTabVisible(index, visible)

    def _reveal_generation_preview(self, index: int) -> None:
        if self._guided_generation and self.tabs.tabText(index) == "Generation Preview":
            QTimer.singleShot(0, self._scroll_generation_choice_into_view)

    def _scroll_generation_choice_into_view(self) -> None:
        if not self.isVisible() or self.tabs.currentWidget() != self.generation_preview.parentWidget():
            return
        viewport = self.content_scroll.viewport()
        bottom = self.restore_button.mapTo(viewport, QPoint(0, self.restore_button.height())).y()
        overflow = bottom + 12 - viewport.height()
        if overflow > 0:
            bar = self.content_scroll.verticalScrollBar()
            bar.setValue(bar.value() + overflow)

    def _refresh_targets(self, *_args) -> None:
        previous = self.target.currentData()
        self.target.blockSignals(True); self.target.clear()
        for record in self.service.targets(str(self.goal_kind.currentData())):
            self.target.addItem(
                f"{record['family']} {record['version']} · {record['variant']}", record["id"],
            )
        index = self.target.findData(previous)
        if index >= 0:
            self.target.setCurrentIndex(index)
        self.target.blockSignals(False)
        self._target_changed()

    def _target_changed(self, *_args) -> None:
        self._refresh_resource_slots()
        self._input_changed()

    def _current_requirements(self) -> tuple[dict, ...]:
        record_id = str(self.target.currentData() or "")
        record = next((item for item in self.service.targets() if item["id"] == record_id), None)
        if record is None:
            return ()
        workflow = str(self.workflow.currentData() or "training")
        return tuple(
            item for item in record.get("companion_requirements", ())
            if workflow in item.get("applies_to", ())
        )

    def _refresh_resource_slots(self) -> None:
        previous = str(self.companion_slot.currentData() or "")
        requirements = self._current_requirements()
        slots = {str(item["slot"]) for item in requirements}
        self.resource_bindings = {
            slot: resource_id for slot, resource_id in self.resource_bindings.items() if slot in slots
        }
        self.companion_slot.blockSignals(True)
        self.companion_slot.clear()
        for item in requirements:
            label = f"{str(item['slot']).replace('_', ' ').title()} · {item['role']}"
            self.companion_slot.addItem(label, str(item["slot"]))
        index = self.companion_slot.findData(previous)
        self.companion_slot.setCurrentIndex(index if index >= 0 else (0 if requirements else -1))
        self.companion_slot.blockSignals(False)
        self._load_binding()
        self.choose_companion_button.setEnabled(bool(requirements and self.registry is not None and self.jobs is not None))
        self.companion_id.setEnabled(bool(requirements))
        if self._guided_generation:
            self.required_resources_button.setVisible(bool(requirements))
        self._render_companions(None)
        if self._guided_generation:
            self._set_customize_visible(self.customize_model_set.isChecked())

    def _load_binding(self, *_args) -> None:
        slot = str(self.companion_slot.currentData() or "")
        self.companion_id.blockSignals(True)
        self.companion_id.setText(self.resource_bindings.get(slot, ""))
        self.companion_id.blockSignals(False)

    def _binding_changed(self, *_args) -> None:
        slot = str(self.companion_slot.currentData() or "")
        if not slot:
            return
        resource_id = self.companion_id.text().strip()
        if resource_id:
            self.resource_bindings[slot] = resource_id
        else:
            self.resource_bindings.pop(slot, None)
        self._render_companions(None)
        self._input_changed()

    def set_resource_binding(self, slot: str, resource_id: str) -> None:
        """Set one explicit identity supplied by a host or synthetic test."""
        known = {str(item["slot"]) for item in self._current_requirements()}
        if slot not in known:
            raise ValueError(f"Unknown companion slot: {slot}")
        self.resource_bindings[str(slot)] = str(resource_id).strip()
        if self.companion_slot.currentData() == slot:
            self._load_binding()
        self._render_companions(None)
        self._input_changed()

    def _open_required_resources(self):
        identity = self.base_model_id.text().strip()
        if self.registry is None or self.jobs is None or not identity.startswith("sha256:"):
            if self._guided_generation:
                self.customize_model_set.setChecked(True)
            self.base_model_id.setFocus(); return
        from core.model_companion_readiness import CompanionChoice, CompanionReadinessRequest
        from .model_companion_workflow import open_companion_review
        context = (identity, str(self.workflow.currentData() or "training"),
            str(self.target.currentData() or ""), self.goal.text())
        main_choices = ()
        record = (self.registry_snapshot or {}).get("resources", {}).get(identity, {})
        locations = [
            location for location in record.get("locations", ())
            if location.get("path") == self.main_model_path
            and not location.get("cache_entry_id")
        ]
        declarations = {
            (declaration.get("role"), declaration.get("family"))
            for declaration in record.get("declarations", ())
            if declaration.get("role") == "checkpoint"
        }
        if self._guided_generation and self.main_model_path and len(locations) == 1 and len(declarations) == 1:
            role, family = next(iter(declarations))
            main_choices = (CompanionChoice("main", identity, self.main_model_path, role, family),)
        request = CompanionReadinessRequest(identity,
            workflow_kind=context[1], target_id=context[2], choices=main_choices)

        def returned(report):
            child = getattr(self, "_companion_dialog", None)
            if (child is None or child.request != report.request or
                    context != (self.base_model_id.text().strip(), str(self.workflow.currentData() or "training"),
                        str(self.target.currentData() or ""), self.goal.text())):
                return False
            if not self._check_current_knowledge(): return False
            snapshot, token = self.registry.snapshot()
            if token != report.registry_token: return False
            fresh = child.service.project(report.request, snapshot=snapshot, registry_token=token)
            if (fresh.catalog_token != report.catalog_token or fresh.profile_status != report.profile_status):
                return False
            requirements = {str(item["slot"]): item for item in self._current_requirements()}
            rows = {row.slot: row for row in fresh.rows}
            selected = {}
            for choice in report.request.choices:
                if choice.slot == "main": continue
                requirement, row = requirements.get(choice.slot), rows.get(choice.slot)
                if (requirement is None or row is None or row.resource_id != choice.resource_id
                        or row.path != choice.path or row.role != choice.role or row.family != choice.family
                        or row.availability != "registered" or row.compatibility == "mismatch"
                        or requirement["role"] != choice.role or requirement["family"] != choice.family):
                    return False
                selected[choice.slot] = choice.resource_id
            if not selected: return False
            self.resource_bindings = {**self.resource_bindings, **selected}
            self.verified_resource_ids = tuple(value for value in self.verified_resource_ids
                if value not in selected.values())
            self.registry_snapshot = snapshot
            self._load_binding()
            self._input_changed()
            self.explanation.setText("Registered resource choices returned. Build a fresh preview; file verification and generator readiness are still separate checks.")
            return True

        self._companion_dialog = open_companion_review(self, self.registry, self.jobs,
            identity, protected_roots=self.protected_roots, initial_request=request,
            on_guidance_choices=returned)

    def _request_companion_close(self, continuation):
        dialog = getattr(self, "_companion_dialog", None)
        return dialog.request_parent_close(continuation) if dialog is not None and dialog.isVisible() else True

    def accept(self):
        if self._request_companion_close(self.accept): super().accept()

    def reject(self):
        if self._request_companion_close(self.reject): super().reject()

    def closeEvent(self, event):
        dialog = getattr(self, "_companion_dialog", None)
        if dialog is not None and dialog.isVisible():
            self.reject(); event.ignore(); return
        super().closeEvent(event)

    def _choose_companion(self) -> None:
        slot = str(self.companion_slot.currentData() or "")
        requirement = next((item for item in self._current_requirements() if item["slot"] == slot), None)
        if requirement is None or self.registry is None or self.jobs is None:
            return
        from .model_selector_dialog import ModelSelectorDialog
        dialog = ModelSelectorDialog(
            self.registry, self.jobs, str(requirement["family"]), self,
            role=str(requirement["role"]), protected_roots=self.protected_roots,
            selected_resources=tuple(self.resource_bindings.values()), allow_manage=True,
        )
        if not dialog.exec() or not dialog.selected_resource_id:
            return
        self.resource_bindings[slot] = dialog.selected_resource_id
        self.verified_resource_ids = tuple(dict.fromkeys((*self.verified_resource_ids, dialog.selected_resource_id)))
        try:
            self.registry_snapshot = self.registry.snapshot()[0]
        except (AttributeError, OSError, ValueError):
            pass
        self._load_binding()
        self._input_changed()

    def _render_companions(self, plan: WorkflowGuidancePlan | None) -> None:
        self.companion_tree.clear()
        requirements = self._current_requirements()
        assessments = {item.slot: item for item in plan.companion_assessments} if plan else {}
        if not requirements:
            if self._guided_generation:
                return
            self.companion_tree.addTopLevelItem(QTreeWidgetItem([
                "Unknown", "—", "No catalog requirements recorded; review the exact profile.", "—", "Unknown",
            ]))
            return
        for requirement in requirements:
            slot = str(requirement["slot"])
            assessment = assessments.get(slot)
            state = (
                assessment.summary if assessment is not None
                else "Build a preview to assess this exact selection."
            )
            self.companion_tree.addTopLevelItem(QTreeWidgetItem([
                slot.replace("_", " ").title(), str(requirement["role"]).replace("_", " ").title(),
                f"{requirement['family']} · {requirement['variant']} · {requirement['necessity']}",
                self.resource_bindings.get(slot, "Not selected"), state,
            ]))

    def _input_changed(self, *_args) -> None:
        self.plan = None
        self._generation_restore_requested = False
        self._accepted_configuration = None
        self._accepted_generation_configuration = None
        self._accepted_profile = None
        self.explanation.setText("Inputs changed. Review a fresh recommendation; saved expert choices have not been altered."
            if self._guided_generation else
            "Inputs changed. Build a fresh preview; saved expert choices have not been altered.")
        self._set_expert_context(str(self.workflow.currentData() or "training") == "generation")
        self._set_expert_enabled(False)
        self.generation_preview.setPlainText("Inputs changed. Review a fresh recommendation before choosing request changes.")
        self._update_actions()

    def build_preview(self) -> None:
        if not self._check_current_knowledge(): return
        if self._knowledge_recovery_pending:
            self.explanation.setText("Recover / Reload Knowledge in Sources & Updates before building a fresh plan.")
            return
        try:
            request = GuidanceRequest(
                self.goal.text(), str(self.goal_kind.currentData()), str(self.target.currentData() or ""),
                str(self.priority.currentData()), self.base_model_id.text().strip(),
                workflow_kind=str(self.workflow.currentData() or "training"),
                resource_bindings=tuple(
                    ResourceBinding(slot, resource_id)
                    for slot, resource_id in sorted(self.resource_bindings.items())
                ),
            )
            self.plan = self.service.recommend(
                request, self.machine, image_count=self.image_count,
                registry_snapshot=self.registry_snapshot,
                verified_resource_ids=self.verified_resource_ids,
                available_adapters=self.available_adapters,
                current_configuration=self.current_configuration,
                available_generation_adapters=self.available_generation_adapters,
                current_generation_configuration=(None if self._generation_restore_requested
                    else self.current_generation_configuration),
            )
        except ModelWorkflowKnowledgeError as exc:
            self.plan = None; self.explanation.setText(str(exc)); self._update_actions(); return
        plan = self.plan
        self.catalog_state.setText(f"Known in offline catalog · {plan.catalog_status} · {plan.family} {plan.version}")
        self.adapter_state.setText(plan.adapter_status.replace("_", " ").title())
        self.machine_state.setText(plan.machine_status.replace("_", " ").title())
        self.explanation.setText(plan.explanation)
        self._set_expert_context(plan.request.workflow_kind == "generation")
        self._render_companions(plan)
        record = next(item for item in self.service.targets() if item["id"] == plan.record_id)
        evidence_lines = [
            f"Catalog: {self.service.catalog['catalog_id']} v{self.service.catalog['catalog_version']} · as of {self.service.catalog['as_of']}",
            f"Exact target label: {plan.record_id} · {plan.family} {plan.version}", "",
        ]
        for source in record["sources"]:
            revision_state = source.get("revision_status", "dated_moving_reference")
            evidence_lines.extend((
                str(source["url"]),
                f"Applicable source revision: {source['applicable_revision']}",
                f"Revision evidence: {revision_state.replace('_', ' ')} · checked {source['checked_on']}", "",
            ))
        self.evidence_text.setPlainText("\n".join(evidence_lines).rstrip())
        self.issues.clear()
        for issue in plan.issues:
            item = QTreeWidgetItem([issue.level.title(), issue.summary, issue.direct_fix])
            item.setToolTip(1, issue.summary); item.setToolTip(2, issue.direct_fix)
            self.issues.addTopLevelItem(item)
        if not plan.issues:
            self.issues.addTopLevelItem(QTreeWidgetItem(["Preview", "No blocking issue in this snapshot.", "Recheck exact resources before any separate launch."]))
        if plan.request.workflow_kind == "generation":
            configuration = plan.generation_configuration
            if configuration is None:
                self.generation_preview.setPlainText(
                    "No admitted generation recipe is available for this exact target.\n\n"
                    + "\n".join(plan.limitations)
                )
            else:
                runtime = (
                    f"{configuration.runtime_version} · {configuration.runtime_fingerprint}"
                    if configuration.runtime_version and configuration.runtime_fingerprint
                    else "Not captured; exact local probe is still required"
                )
                settings = configuration.request_changes()
                evidence = dict(configuration.setting_evidence)
                self.generation_preview.setPlainText(
                    f"Recipe: {configuration.recipe_id} v{configuration.recipe_version}\n"
                    f"Process: {configuration.process.replace('_', ' ')}\n"
                    f"Target: {configuration.target_model_family} {configuration.target_model_version}\n"
                    f"Runtime identity: {runtime}\n\n"
                    "Current request / recommended baseline / pending choice:\n"
                    + self._generation_comparison(configuration, plan.recommended_generation_configuration)
                    + "\n\nPending neutral request changes (applied only by Use Generation Recommendation):\n"
                    + "\n".join(
                        f"• {key.replace('_', ' ')}: {value if value not in (None, '') else 'clear'}"
                        f" · {evidence.get(key, 'user-preserved').replace('_', ' ')}"
                        for key, value in settings.items()
                    )
                    + "\n\nThis is a recommendation for deliberate request revision. Connection, preparation, and execution retain their separate checks.\n\n"
                    + "\n".join(plan.limitations)
                )
                if self._generation_restore_requested:
                    self.generation_preview.append(
                        "\nRecommended values are pending only. Cancel keeps the current Request unchanged.")
            self.dataset_preview.setPlainText(
                "Generation guidance does not change captions or confirmed Image Knowledge. "
                "Use the Generation Preview tab to inspect the exact neutral request changes."
            )
            self._set_expert_enabled(False)
        elif plan.caption_profile is None:
            self.dataset_preview.setPlainText(
                "No image-caption projection is defined. Video clip and temporal data remain under VID-01.\n\n"
                + "\n".join(plan.limitations)
            )
            self._set_expert_enabled(False)
        else:
            profile = plan.caption_profile
            preview_text = (
                f"Profile: {profile.name}\nProfile ID/version: {profile.profile_id} v{profile.version}\n"
                f"Target: {profile.target_model_family} {profile.target_model_version}\n"
                f"Projection: {profile.caption_strategy}\nToken budget: {profile.token_budget or 'not declared'}\n\n"
                "This recipe projects a caption from the same confirmed Image Knowledge. It does not delete, "
                "downgrade, or reclassify any source fact. Exact tokenizer counts and training quality remain unavailable.\n\n"
                + "\n".join(plan.limitations)
            )
            if self.caption_previews:
                preview_text += "\n\nCurrent reviewed plan captions (already projected by the training workflow):\n"
                preview_text += "\n".join(f"• {label}: {caption}" for label, caption in self.caption_previews)
            self.dataset_preview.setPlainText(preview_text)
            self._load_configuration(plan.configuration)
            self._set_expert_enabled(plan.configuration is not None)
        self._update_actions()

    def _set_expert_enabled(self, enabled: bool) -> None:
        for widget in (self.resolution, self.batch_size, self.accumulation, self.precision, self.override_reason, self.restore_button):
            widget.setEnabled(enabled)

    def _generation_comparison(self, pending, recommended):
        current = self.current_generation_configuration
        pending_changes = pending.request_changes()
        recommended_changes = recommended.request_changes() if recommended is not None else {}
        def display(value):
            return "Clear / default" if value in (None, "") else str(value)
        lines = []
        for field, label in (("width", "Width"), ("height", "Height"), ("steps", "Steps"),
                ("cfg_scale", "CFG scale"), ("sampler", "Sampler"), ("scheduler", "Scheduler")):
            original = getattr(current, field) if current is not None else None
            before = display(original) if current is not None else "Not provided"
            baseline = display(recommended_changes[field]) if field in recommended_changes else "Keep current (not specified)"
            after = display(pending_changes[field]) if field in pending_changes else before
            lines.append(f"{label}: current {before} | recommended {baseline} | pending {after}")
        return "\n".join(lines)

    def _set_expert_context(self, generation: bool) -> None:
        source, destination = ((self._expert_restore_row, self._generation_restore_row) if generation
            else (self._generation_restore_row, self._expert_restore_row))
        source.removeWidget(self.restore_button)
        destination.insertWidget(0, self.restore_button)
        if generation:
            self.expert_intro.setText(
                "Generation sampling settings are read-only in this guidance preview. Apply the recommendation, "
                "then review or edit width, height, steps, CFG scale, sampler, and scheduler in the existing Generation Request. "
                "Generation Preview compares the current request with the recommendation and offers deliberate restoration."
            )
            self.expert_status.setText(
                "Use Generation Recommendation returns explicit request changes; it does not prepare or run them."
            )
        else:
            self.expert_intro.setText(
                "These are the same settings used by the beginner preview. Changes remain explicit and reversible."
            )
            if self.plan is None:
                self.expert_status.setText("Build a preview to edit its exact configuration.")

    def _load_configuration(self, configuration: TrainingConfiguration | None) -> None:
        if configuration is None:
            return
        for widget in (self.resolution, self.batch_size, self.accumulation, self.precision, self.override_reason):
            widget.blockSignals(True)
        self.resolution.setValue(configuration.target_resolution)
        self.batch_size.setValue(configuration.batch_size)
        self.accumulation.setValue(configuration.gradient_accumulation)
        self.precision.setCurrentIndex(max(0, self.precision.findData(configuration.precision)))
        self.override_reason.setPlainText(configuration.override_reason)
        for widget in (self.resolution, self.batch_size, self.accumulation, self.precision, self.override_reason):
            widget.blockSignals(False)
        self._expert_changed()

    def configuration(self) -> TrainingConfiguration | None:
        if self.plan is None or self.plan.configuration is None:
            return None
        return replace(
            self.plan.configuration,
            target_resolution=self.resolution.value(), batch_size=self.batch_size.value(),
            gradient_accumulation=self.accumulation.value(), precision=str(self.precision.currentData()),
            override_reason=self.override_reason.toPlainText().strip(),
        )

    def selected_configuration(self) -> TrainingConfiguration | None:
        """Return only the configuration deliberately accepted by the user."""
        return self._accepted_configuration

    def selected_profile(self):
        """Return the exact profile paired with the accepted configuration."""
        return self._accepted_profile

    def selected_generation_configuration(self) -> GenerationConfigurationRecommendation | None:
        """Return only the generation recommendation deliberately accepted by the user."""
        return self._accepted_generation_configuration

    def _choose_update_file(self) -> None:
        filename, _ = QFileDialog.getOpenFileName(
            self, "Preview Model Knowledge File", "", "JSON knowledge files (*.json)",
        )
        if filename:
            self.preview_update_path(filename)

    def preview_update_path(self, filename: str) -> None:
        self._pending_update_path = None; self.apply_update_button.setEnabled(False)
        try:
            preview = self.service.preview_update_file(filename)
            fingerprint = self.service.catalog_write_token()
            source_fingerprint = self.service.source_file_fingerprint(filename)
        except (OSError, ModelWorkflowKnowledgeError) as exc:
            self.update_status.setText(f"Knowledge file rejected: {exc}")
            self._check_current_knowledge()
            return
        detail = "; ".join(preview.issues) if preview.issues else "No stale or conflicting records were found."
        if not preview.changed:
            self.update_status.setText(f"Validated file contains no applicable change. {detail}"); return
        self._pending_update_path = Path(filename)
        self._pending_update_fingerprint = fingerprint
        self._pending_update_source_fingerprint = source_fingerprint
        writable = self.service.update_path is not None
        self.apply_update_button.setEnabled(writable)
        self.update_status.setText(
            f"Validated update preview: catalog v{preview.payload['catalog_version']} as of {preview.payload['as_of']}. "
            f"User notes, overrides, and unknown fields remain local. {detail}"
            + ("" if writable else " Configure a user-data knowledge path before applying; the shipped baseline is read-only.")
        )

    def _apply_update_file(self) -> None:
        if self._pending_update_path is None:
            return
        self._invalidate_knowledge_plan()
        try:
            result = self.service.apply_update_file(
                self._pending_update_path,
                expected_catalog_sha256=self._pending_update_fingerprint,
                expected_incoming_sha256=self._pending_update_source_fingerprint,
            )
        except (OSError, ValueError, RuntimeError) as exc:
            self._knowledge_recovery_pending = True
            self._pending_update_path = None
            self.update_status.setText(f"Update outcome needs review: {exc}. Use Recover / Reload Knowledge to check saved evidence before continuing.")
            self._show_knowledge_recovery()
            self.apply_update_button.setEnabled(False); self._update_actions(); return
        self._pending_update_path = None; self.apply_update_button.setEnabled(False)
        self._knowledge_recovery_pending = False
        self.update_status.setText(
            ("Applied and read back" if result.changed else "Unchanged; reloaded")
            + f" catalog v{result.payload['catalog_version']}. Build a fresh preview to use its evidence."
        )
        self._refresh_targets()

    def _invalidate_knowledge_plan(self):
        configuration = self.configuration()
        if configuration is not None:
            self.current_configuration = configuration
        self._input_changed()

    def _check_current_knowledge(self):
        try:
            self.service.require_current_catalog()
        except (OSError, ValueError, RuntimeError) as exc:
            self._invalidate_knowledge_plan(); self._knowledge_recovery_pending = True
            self.update_status.setText(str(exc)); self._show_knowledge_recovery(); self._update_actions()
            return False
        return True

    def _show_knowledge_recovery(self):
        self.explanation.setText("Knowledge update needs review. Use Recover / Reload Knowledge below; expert choices are retained.")
        if self._guided_generation:
            self.customize_model_set.setChecked(True)
        self.tabs.setCurrentIndex(self._knowledge_sources_tab)
        self.content_scroll.ensureWidgetVisible(self.recover_update_button)

    def _recover_update(self):
        self._invalidate_knowledge_plan()
        self._pending_update_path = None; self.apply_update_button.setEnabled(False)
        outcome = self.service.reconcile_update()
        self._knowledge_recovery_pending = outcome.pending
        self.update_status.setText(outcome.summary + ("" if outcome.pending else " Build a fresh preview; your expert choices are retained."))
        if not outcome.pending:
            self._refresh_targets()
            self.catalog_state.setText(f"Reloaded offline catalog v{self.service.catalog['catalog_version']} · fresh plan required")
            self.evidence_text.setPlainText("Knowledge reloaded. Build a fresh preview to inspect the applicable dated source evidence.")
        self._update_actions()

    def restore_recommendation(self) -> None:
        if self.plan is None:
            return
        if self.plan.request.workflow_kind == "generation":
            if not self.restore_button.isEnabled() or not self._check_current_knowledge(): return
            self._generation_restore_requested = True
            self.build_preview()
            return
        self.plan = replace(self.plan, configuration=self.plan.recommended_configuration)
        self._load_configuration(self.plan.recommended_configuration)

    def _expert_changed(self, *_args) -> None:
        configuration = self.configuration()
        if configuration is None or self.plan is None:
            self.expert_status.setText("Build a preview to edit its exact configuration."); self._update_actions(); return
        issues = validate_training_configuration(
            configuration, profile_family=self.plan.family, profile_version=self.plan.version,
        )
        blocking = [issue for issue in issues if issue.level == "blocking"]
        if blocking:
            self.expert_status.setText(f"Needs attention: {blocking[0].summary} {blocking[0].direct_fix}")
        elif configuration.overridden_fields:
            self.expert_status.setText(
                "Expert changes are recorded: " + ", ".join(configuration.overridden_fields)
                + ". Restore Recommendation reverses them."
            )
        else:
            self.expert_status.setText("Using the complete recommended baseline.")
        self._update_actions()

    def _update_actions(self) -> None:
        configuration = self.configuration()
        if str(self.workflow.currentData() or "training") == "generation":
            self.use_button.setText("Use Generation Recommendation")
            generation = self.plan.generation_configuration if self.plan is not None else None
            valid_configuration = generation is not None and not any(
                issue.level == "blocking" for issue in validate_generation_configuration(
                    generation, family=self.plan.family, version=self.plan.version,
                )
            )
            recommended = self.plan.recommended_generation_configuration if self.plan is not None else None
            self.restore_button.setEnabled(not self._knowledge_recovery_pending
                and bool(self.plan and self.plan.can_apply and recommended is not None)
                and not any(issue.level == "blocking" for issue in validate_generation_configuration(
                    recommended, family=self.plan.family, version=self.plan.version)))
            if self.plan is None:
                self.action_status.hide()
                self.use_button.setToolTip("Review a recommendation before applying any change to the Generate draft.")
            elif not valid_configuration:
                message = ("No applicable generation settings are available for this model version. "
                    "The resource choices remain in this preview; Cancel returns to the unchanged Generate draft."
                    if generation is None else
                    "The generation settings have a blocking issue. Review Generation Preview before applying; Cancel keeps the Generate draft unchanged.")
                self.action_status.setText(message)
                self.action_status.show()
                self.use_button.setToolTip(message)
            else:
                self.action_status.hide()
                self.use_button.setToolTip("Apply only this reviewed request change; exact adapter preflight remains required.")
        else:
            self.action_status.hide()
            valid_configuration = configuration is not None and not any(
                issue.level == "blocking" for issue in validate_training_configuration(
                    configuration,
                    profile_family=self.plan.family if self.plan else "",
                    profile_version=self.plan.version if self.plan else "",
                )
            )
            compatible=False
            if configuration is not None:
                try:
                    preview=preview_training_preset(configuration,preset_id="current",
                        training_image_count=getattr(self.plan,"planned_image_upper_bound",None))
                    compatible=preview.native_status=="native_candidate"
                except ValueError:
                    pass
            self.use_button.setText("Use Local Training Starting Point" if compatible
                                    else "Use for External Training")
            self.use_button.setToolTip(
                "Apply settings to the training plan. Exact resources, package and launch still need review."
                if compatible else
                "Retain a portable plan for an external trainer. These settings are not a runnable local preset."
            )
        self.preview_button.setEnabled(not self._knowledge_recovery_pending and bool(self.goal.text().strip()) and self.target.count() > 0)
        self.use_button.setEnabled(not self._knowledge_recovery_pending and bool(self.plan and self.plan.can_apply and valid_configuration))

    def _accept_plan(self) -> None:
        if not self._check_current_knowledge(): return
        if self.plan is not None and self.plan.request.workflow_kind == "generation":
            if not self.use_button.isEnabled() or self.plan.generation_configuration is None:
                return
            self._accepted_generation_configuration = self.plan.generation_configuration
            self.accept()
            return
        configuration = self.configuration()
        if configuration is None or not self.use_button.isEnabled():
            return
        self._accepted_configuration = configuration
        self._accepted_profile = self.plan.caption_profile if self.plan else None
        self.accept()
