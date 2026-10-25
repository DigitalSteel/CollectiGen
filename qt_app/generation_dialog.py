"""Offline-first review surface for a generation request.

The dialog deliberately owns only the reviewable request.  Connection probing,
translation and execution are supplied by the generation adapters so this UI
remains useful when no generator is installed or running.
"""
from __future__ import annotations

import json
import hashlib
from math import gcd
from copy import deepcopy
from pathlib import Path
from typing import Any
from uuid import uuid4

from PySide6.QtCore import QTimer, Qt, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFileDialog, QFormLayout,
    QGridLayout, QGroupBox, QHBoxLayout, QHeaderView, QInputDialog, QLabel, QLineEdit, QMessageBox, QPushButton, QScrollArea,
    QSizePolicy, QSpinBox, QSplitter, QTabWidget, QTextEdit, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.annotation import GenerationRecord, GenerationInput, GenerationStage, GenerationResource
from core.generation_planning import (
    GenerationPlan, GenerationPlanError, assess_readiness, export_plan, load_plan, load_plan_with_digest,
    outfit_association_scope, save_plan,
)
from core.generation_output_guidance import (
    plan_image_output_resolution, exact_image_base, image_size_choices, IMAGE_ASPECT_RATIOS,
)
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask
from .workflow_context import WorkflowContextSummary
from .tag_editor import TagChoiceChip
from .gallery import ProtectedImageView


def _plain_json(value: Any) -> str:
    if hasattr(value, "to_dict"):
        value = value.to_dict()
    return json.dumps(value, indent=2, sort_keys=True, ensure_ascii=False, default=str)


def _mapping(value: Any) -> dict[str, Any]:
    return dict(value) if isinstance(value, dict) else {}


def _fragment(value: Any) -> dict[str, Any]:
    """Normalize caller display data into the planning contract without guessing inclusion."""
    raw = _mapping(value)
    group = str(raw.get("group") or raw.get("kind") or "other").casefold()
    known_groups = {
        "character_wearer", "character_wearer_outfit", "character_associated",
        "character_associated_outfit", "character_other", "generic_outfit",
        "composition", "style", "model_trigger", "compatible_setting", "seed",
        "prompt_fragment", "other",
    }
    return {
        **raw,
        "id": str(raw.get("id") or raw.get("label") or "fragment"),
        "text": str(raw.get("text") or raw.get("label") or ""),
        "group": group if group in known_groups else "other",
        "reviewed": bool(raw.get("reviewed", False)),
        "included": bool(raw.get("included", False)),
    }


class GenerationDialog(FeedbackEnabledDialog):
    """Build, inspect, save and explicitly submit one immutable generation plan."""

    feedback_workflow = "Generation request"
    ingest_requested = Signal(object)
    ingest_folder_requested = Signal()
    result_action_requested = Signal(object)
    setup_requested = Signal()
    image_setup_requested = Signal(object)
    training_comparison_return_requested = Signal()
    workspace_closed = Signal()
    background_work_finished = Signal()
    draft_save_finished = Signal()
    _io_completed = Signal(object)
    _io_failed = Signal(object)
    _submission_completed = Signal(object)

    def __init__(
        self,
        jobs,
        *,
        registry=None,
        storage_root: Path,
        source_record: GenerationRecord | None = None,
        source_identity: dict | None = None,
        knowledge_fragments=(),
        characters=(),
        outfits=(),
        source_current_provider=None,
        source_image_provider=None,
        source_image_bytes=None,
        protected_model_roots=(),
        settings=None,
        ingest_history=None,
        setup_available=False,
        initial_endpoint="",
        connection_issue="",
        prompt_knowledge=None,
        swarm_preset_service=None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self.jobs = jobs
        self.registry = registry
        self.protected_model_roots = tuple(protected_model_roots)
        self.settings = settings
        self.ingest_history = ingest_history
        self.setup_available = setup_available
        self.connection_issue = str(connection_issue or "")
        if initial_endpoint:
            from core.generation_transport import local_endpoint
            initial_endpoint = local_endpoint(initial_endpoint)
        self.initial_endpoint = initial_endpoint or "http://127.0.0.1:7801"
        self.remix_source = False
        self.prompt_knowledge = prompt_knowledge
        self.swarm_preset_service = swarm_preset_service
        self.storage_root = Path(storage_root)
        self.source_record = GenerationRecord.from_dict(source_record.to_dict()) if source_record else None
        self.source_identity = _mapping(source_identity)
        self.source_current = dict(self.source_identity)
        self.source_current_provider = source_current_provider
        self.source_image_provider = source_image_provider
        self.source_image_bytes = source_image_bytes
        self._primary_source_identity = deepcopy(self.source_identity)
        self._primary_current_provider = source_current_provider
        self._additional_references = []
        self.ingest_identity: dict[str, Any] = {}
        initial_fragments = tuple(_fragment(item) for item in knowledge_fragments)
        self.characters = tuple(_mapping(item) for item in characters)
        self.outfits = tuple(_mapping(item) for item in outfits)
        self._outfit_scope_cache: dict[str, dict[str, Any]] = {}
        self.plan = GenerationPlan.create(
            self.source_record, intent="reproduce" if source_record else "new",
            source_identity=self.source_identity,
        ).revise({"knowledge_fragments": list(initial_fragments)}, source="dialog")
        # A selected image is still a new request when it has no usable
        # recorded seed. Give that draft one concrete, replayable seed just as
        # a source-free request receives; never replace a supported source seed.
        if type(self.plan.request.seed) is not int or not 0 <= self.plan.request.seed < 2**63:
            self.plan = self.plan.choose_seed("random")
        self._initial_plan_digest = self.plan.digest
        self._advanced_synced_text: str | None = None
        self._advanced_base_digest = self.plan.digest
        from core.generation_saved_work import new_draft_path
        self._draft_path = new_draft_path(self.storage_root)
        self._draft_saved_digest = "" if source_record or self.source_identity else self.plan.digest
        self._draft_close_pending = False
        self._draft_close_workspace = False
        self._draft_save_error = False
        self._draft_after_save = None
        self.adapter = None
        self.binding: dict[str, Any] | None = None
        self.prepared: dict[str, Any] | None = None
        self.receipt: dict[str, Any] | None = None
        self._ingest_issue = ""
        self._ingest_folder_recovery = False
        self._outcome_drafts: dict[str, tuple[bool, str, str, str, dict[str, Any]]] = {}
        self._save_tokens: dict[Path, str] = {}
        self._pending: dict[str, tuple[Any, int, Any]] = {}
        self._failure_callbacks: dict[str, Any] = {}
        self._generation = 0
        self._closed = False
        self._submitted_job_id = ""
        self._submitted_progress = None
        self._cancellation_requested = False
        self._shutdown_requested = False
        self._background_shutdown_notified = False
        self._submission_terminal_seen = True
        self._submission_terminal_state = ""
        self._submission_generation = 0
        self._history_context = False
        self._output_preview = None
        self._output_preview_plan = None
        self._experiment_dialog = None
        self._finishing_dialogs = []
        self._saved_work_dialog = None
        self._prompt_templates_dialog = None
        self._progress_timer = QTimer(self); self._progress_timer.setInterval(150); self._progress_timer.timeout.connect(self._poll_submission)
        self._io_timer = QTimer(self); self._io_timer.setInterval(75); self._io_timer.timeout.connect(self._poll_io)
        self._io_completed.connect(self._deliver_io_completion)
        self._io_failed.connect(self._deliver_io_failure)
        self._submission_completed.connect(self._receive_submission_completion)

        self.setWindowTitle("Plan Image Generation")
        self.resize(1000, 860)
        layout = QVBoxLayout(self)
        heading = QLabel("Review a complete request before any local generator receives it")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        self.workflow_context = WorkflowContextSummary()
        layout.addWidget(self.workflow_context)
        self.status = QLabel(self.connection_issue or "You can save or export this request without a generator connection.")
        self.status.setWordWrap(True); self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        self.draft_status = QLabel("New draft · edits are saved locally for Resume Image Draft.")
        self.draft_status.setWordWrap(True); self.draft_status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.draft_status)
        self.training_comparison_return = QWidget()
        return_row = QHBoxLayout(self.training_comparison_return)
        return_row.setContentsMargins(0,0,0,0)
        self.training_comparison_return_button = QPushButton("Return to Training Comparison")
        self.training_comparison_return_button.setToolTip(
            "Return to the open Evaluation review. Its Use Reviewed Generate Settings action "
            "separately chooses whether to freeze this request as a comparison template."
        )
        self.training_comparison_return_button.clicked.connect(
            self.training_comparison_return_requested.emit
        )
        return_row.addWidget(self.training_comparison_return_button)
        note = QLabel("Returning does not save a comparison or start generation.")
        note.setWordWrap(True)
        return_row.addWidget(note,1)
        self.training_comparison_return.hide()
        layout.addWidget(self.training_comparison_return)

        self.tabs = QTabWidget()
        self.tabs.addTab(self._scroll(self._build_request_tab()), "Request")
        self.tabs.addTab(self._scroll(self._build_review_tab()), "Review")
        self.tabs.addTab(self._build_advanced_tab(), "Advanced")
        connection = self._build_connection_tab()
        self.tabs.addTab(self._scroll(self._build_results_tab()), "Results && Queue")
        self._readiness_tab_index = self.tabs.addTab(self._scroll(connection), "Readiness")
        body = QHBoxLayout()
        body.addWidget(self.tabs, 1)
        self.source_panel = self._build_source_panel()
        self.source_scroll = QScrollArea()
        self.source_scroll.setWidgetResizable(True)
        self.source_scroll.setMaximumWidth(308)
        self.source_scroll.setMinimumWidth(290)
        self.source_scroll.setWidget(self.source_panel)
        body.addWidget(self.source_scroll)
        layout.addLayout(body, 1)
        self.next_step_button = QPushButton("Show Next Step…")
        self.next_step_button.setToolTip("Open the next decision without changing the plan or starting work.")
        self.next_step_button.clicked.connect(self._show_next_step)

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.close_button = buttons.button(QDialogButtonBox.Cancel)
        self.close_button.setText("Close Workspace")
        self.save_button = QPushButton("Save Draft As…")
        self.load_button = QPushButton("Open Draft…")
        self.export_button = QPushButton("Export Request…")
        for button, callback in ((self.save_button, self._choose_save), (self.load_button, self._choose_load), (self.export_button, self._choose_export)):
            button.clicked.connect(callback); buttons.addButton(button, QDialogButtonBox.ActionRole)
        self.submit_button = QPushButton("Submit Reviewed Request")
        self.submit_button.setProperty("primary", True); self.submit_button.clicked.connect(self.submit_reviewed)
        buttons.addButton(self.submit_button, QDialogButtonBox.AcceptRole)
        buttons.addButton(self.next_step_button, QDialogButtonBox.ActionRole)
        self.more_draft_actions_button = QPushButton("More Draft Actions…")
        self.more_draft_actions_button.setCheckable(True)
        self.more_draft_actions_button.setToolTip(
            "Show named draft save, open, and export actions without changing the request."
        )
        self.more_draft_actions_button.toggled.connect(self._update_draft_action_presentation)
        buttons.addButton(self.more_draft_actions_button, QDialogButtonBox.ActionRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_from_plan()
        if not self.outfits and self.fragment_tree.topLevelItemCount() == 0:
            self.advanced_tasks.setCurrentIndex(1)
        self.tabs.currentChanged.connect(self._tab_changed)
        self._draft_timer = QTimer(self)
        self._draft_timer.setInterval(5000)
        self._draft_timer.timeout.connect(self._autosave_draft)
        self._draft_timer.start()

    def _draft_inputs_valid(self):
        try:
            return (self._request_field_values() == self._displayed_request
                    and bool(self.final_width.value()) == bool(self.final_height.value())
                    and json.loads(self.advanced_json.toPlainText()) == self.plan.to_dict())
        except (ValueError, TypeError):
            return False

    def _autosave_draft(self, *, closing=False):
        if self._closed or self._shutdown_requested or self._pending or self._execution_active():
            return
        if not closing and (not self.isVisible() or self._draft_save_error):
            return
        if not self._draft_inputs_valid():
            self.draft_status.setText("Unsaved edits · complete invalid values or apply the Advanced changes.")
            return
        if self.plan.digest == self._draft_saved_digest:
            return
        snapshot = self.plan
        self.draft_status.setText("Saving draft locally…")
        def saved(result):
            self._remember_saved(*result)
            self._draft_saved_digest = snapshot.digest
            self._draft_save_error = False
            self.draft_status.setText("Draft saved locally · use Resume Image Draft in Generate to reopen it.")
            if self._shutdown_requested:
                self._draft_close_pending = self._draft_close_workspace = False
                self._draft_after_save = None
                return
            if self._draft_close_pending:
                self._draft_close_pending = False
                if self._draft_close_workspace:
                    self._draft_close_workspace = False
                    self.reject()
                elif self._draft_after_save is not None:
                    callback = self._draft_after_save
                    self._draft_after_save = None
                    callback()
                else:
                    self.draft_save_finished.emit()
        self._run("autosave", lambda: self._save_path(self._draft_path, snapshot), saved)

    def confirm_draft_loss(self, after_saved=None):
        """Retain valid work before close; incomplete edits require an explicit choice."""
        if self._draft_close_pending:
            return False
        if not self._draft_inputs_valid() or self._draft_save_error:
            choice = QMessageBox.warning(self, "Unsaved Image Draft",
                "This draft has incomplete edits or could not be saved. Cancel to correct it or use Save Draft As. "
                "Discard closes it without saving these changes; earlier saved copies remain available.",
                QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel)
            if choice != QMessageBox.Discard:
                return False
            self._draft_saved_digest = self.plan.digest
            self._draft_save_error = False
            self._sync_advanced_json_from_plan(force=True)
            self._refresh_from_plan()
            return True
        if self.plan.digest == self._draft_saved_digest:
            return True
        if self.has_active_background_work():
            return False
        self._draft_close_pending = True
        self._draft_after_save = after_saved
        self._autosave_draft(closing=True)
        return False

    def resume_saved_draft(self):
        from .generation_saved_work_dialog import SavedGenerationWorkDialog
        if self._pending or not self.confirm_draft_loss(after_saved=self.resume_saved_draft):
            return
        browser = SavedGenerationWorkDialog(self.jobs, self.storage_root, "drafts", parent=self)
        self._saved_work_dialog = browser
        accepted = browser.exec() == QDialog.Accepted
        if not browser.has_active_background_work(): self._saved_work_dialog = None
        if accepted and browser.selected_path:
            target = Path(browser.selected_path)
            self._run("load", lambda: load_plan_with_digest(target),
                      lambda value: self._apply_loaded_plan(value[0], target, value[1]))

    def _build_source_panel(self):
        panel = QGroupBox("Source Image")
        panel.setMaximumWidth(280)
        box = QVBoxLayout(panel)
        self.source_preview = ProtectedImageView(settings=self.settings)
        self.source_preview.clear("Choose an image to Remix from Ingest or Dataset.")
        self.source_preview.setMinimumSize(240, 190)
        box.addWidget(self.source_preview)
        self.reconnect_source_button = QPushButton("Reconnect Source Images…")
        self.reconnect_source_button.clicked.connect(self.reconnect_sources)
        box.addWidget(self.reconnect_source_button)
        self.image_task = QComboBox()
        self.image_task.setAccessibleName("Image-guided task")
        self.image_task.addItem("Remix whole image", "remix")
        self.image_task.addItem("Edit painted area", "masked")
        self.image_task.addItem("Generative upscale", "upscale")
        self.image_task.addItem("Expand canvas", "outpaint")
        self.image_task.addItem("Use as visual reference", "reference_image")
        self.image_task.addItem("Guide with ControlNet", "control_image")
        box.addWidget(self.image_task)
        self.repair_intent = QComboBox()
        self.repair_intent.setAccessibleName("Remix repair approach")
        for label, key in (("Repair a painted area", "area"), ("Light touch-up", "touchup"),
                           ("Rework the image", "rework"), ("Reimagine from reference", "reimagine")):
            self.repair_intent.addItem(label, key)
        box.addWidget(self.repair_intent)
        self.apply_repair_button = QPushButton("Use This Repair Approach")
        self.apply_repair_button.clicked.connect(self.apply_repair_approach)
        box.addWidget(self.apply_repair_button)
        self.remix_strength = QDoubleSpinBox()
        self.remix_strength.setRange(.01, 1.0)
        self.remix_strength.setSingleStep(.05)
        self.remix_strength.setDecimals(2)
        self.remix_strength.setKeyboardTracking(False)
        self.remix_strength.setValue(.5)
        self.remix_strength.setAccessibleName("Amount of change")
        strength_form = QFormLayout()
        strength_form.addRow("Amount of change", self.remix_strength)
        box.addLayout(strength_form)
        self.control_models_button = QPushButton("Choose ControlNet…")
        self.control_models_button.clicked.connect(lambda: self.choose_registered_resource("control_model"))
        box.addWidget(self.control_models_button)
        self.control_help = QLabel("Use a prepared control image. Preprocessing is None; no detector or extra weights are downloaded. ControlNet guides structure; a visual reference gives looser guidance.")
        self.control_help.setWordWrap(True)
        box.addWidget(self.control_help)
        self.references_button = QPushButton("Choose More References…")
        self.references_button.clicked.connect(self.choose_reference_images)
        self.clear_references_button = QPushButton("Use First Reference Only")
        self.clear_references_button.clicked.connect(self.clear_reference_images)
        self.references_summary = QLabel()
        self.references_summary.setWordWrap(True); self.references_summary.setTextFormat(Qt.PlainText)
        self.reference_cards = QWidget(); self.reference_cards_layout = QVBoxLayout(self.reference_cards)
        self.reference_cards_layout.setContentsMargins(0, 0, 0, 0)
        self._reference_cards_key = None
        box.addWidget(self.references_button); box.addWidget(self.clear_references_button)
        box.addWidget(self.references_summary)
        box.addWidget(self.reference_cards)
        explanation = QLabel("Lower values stay closer to the source. Higher values redraw more. Generative upscale may change details.")
        explanation.setWordWrap(True)
        box.addWidget(explanation)
        self.upscale_factor = QComboBox()
        self.upscale_factor.addItem("1.5× dimensions", 1.5)
        self.upscale_factor.addItem("2× dimensions", 2.0)
        self.upscale_factor.setAccessibleName("Generative upscale size")
        box.addWidget(self.upscale_factor)
        self.edit_mask_button = QPushButton("Paint Editable Area…")
        self.edit_mask_button.clicked.connect(self.edit_mask)
        box.addWidget(self.edit_mask_button)
        self.canvas_controls = QWidget()
        canvas_form = QFormLayout(self.canvas_controls)
        self.canvas_borders = {}
        for edge in ("left", "top", "right", "bottom"):
            field = QSpinBox(); field.setRange(0, 1024); field.setSingleStep(8); field.setValue(128)
            field.setAccessibleName("Canvas " + edge + " border pixels")
            canvas_form.addRow(edge.title() + " pixels", field)
            self.canvas_borders[edge] = field
            field.valueChanged.connect(self._image_task_changed)
        box.addWidget(self.canvas_controls)
        self.source_detail = QLabel()
        self.source_detail.setWordWrap(True)
        self.source_detail.setTextFormat(Qt.PlainText)
        box.addWidget(self.source_detail)
        box.addStretch(1)
        self.image_task.currentIndexChanged.connect(self._image_task_changed)
        self.remix_strength.valueChanged.connect(self._image_task_changed)
        self.upscale_factor.currentIndexChanged.connect(self._image_task_changed)
        panel.setVisible(bool(self.source_image_bytes))
        return panel

    def apply_repair_approach(self):
        if self._pending or self._execution_active() or not self.source_image_bytes:
            return
        approach = self.repair_intent.currentData()
        mode, strength = {"area": ("masked", .5), "touchup": ("remix", .25),
                          "rework": ("remix", .65), "reimagine": ("reference_image", .5)}[approach]
        self.remix_strength.blockSignals(True)
        self.remix_strength.setValue(strength)
        self.remix_strength.blockSignals(False)
        self.image_task.blockSignals(True)
        self.image_task.setCurrentIndex(self.image_task.findData(mode))
        self.image_task.blockSignals(False)
        context = deepcopy(self.plan.target_context)
        context["repair_intent"] = {"approach": approach, "original_unchanged": True,
            "acceptance": "Return candidates to Ingest unreviewed; review before resolving the problem."}
        self.plan = self.plan.revise({"target_context": context}, source="repair_approach")
        self._image_task_changed()
        self.status.setText("Repair approach selected. These strengths are starting points, not model-specific quality guarantees. "
                            "Describe the desired correction in the prompt; paint an area to protect the rest exactly.")

    def apply_image_origin_intent(self, intent: str) -> None:
        """Stage a chosen variation goal in the canonical source-bound draft."""
        strengths = {"close": .25, "transform": .65}
        if intent not in strengths or not self.remix_source or not self.source_image_bytes:
            raise ValueError("Choose a readable image and a supported variation goal.")
        self.remix_strength.setValue(strengths[intent])
        context = deepcopy(self.plan.target_context)
        context["image_origin_intent"] = intent
        self.plan = self.plan.revise({"target_context": context}, source="image_origin_goal",
            consequence="Chose an editable image-change starting point; original unchanged")
        self._refresh_from_plan(
            "Starting amount of change applied. This is not a model-specific quality guarantee. "
            "Describe the desired result, review the exact model and settings, then prepare deliberately."
        )

    def configure_remix(self):
        """Turn an explicitly selected image into one auditable image input."""
        if not self.source_image_bytes or not callable(self.source_image_provider):
            raise ValueError("Select a readable source image before starting Remix.")
        self.remix_source = True
        self._image_task_changed()

    def _mask_payload(self):
        for stage in self.plan.request.stages:
            for item in stage.inputs:
                if item.kind == "mask":
                    return item.extras.get("mask_payload")
        return None

    def _retained_mask(self):
        draft = self.plan.target_context.get("image_edit_draft", {})
        outpaint = any(stage.extras.get("image_task") == "outpaint" for stage in self.plan.request.stages)
        return (None if outpaint else self._mask_payload()) or (draft.get("mask_payload") if isinstance(draft, dict) else None)

    def _image_task_changed(self, *_args):
        if getattr(self, "_refreshing", False) or not self.remix_source or self._pending or self._execution_active():
            return
        try:
            identity = self._primary_source_identity["collection"]
            if len(identity) != 1:
                raise ValueError("Remix needs exactly one source image.")
            sha = identity[0]["image_sha256"]
            if hashlib.sha256(self.source_image_bytes).hexdigest() != sha:
                raise ValueError("Reload the selected image: its bytes differ from the saved source identity.")
            source = QImage.fromData(self.source_image_bytes)
            if source.isNull():
                raise ValueError("The selected image cannot be decoded.")
            task = self.image_task.currentData()
            context = self.plan.target_context
            draft = dict(context.get("image_edit_draft", {}))
            resources = list(self.plan.request.resources)
            controls = [r for r in resources if r.role == "controlnet"]
            if task != "control_image" and controls:
                draft["control_resources"] = [r.to_dict() for r in controls]
                resources = [r for r in resources if r.role != "controlnet"]
            elif task == "control_image" and not controls and draft.get("control_resources"):
                resources += [GenerationResource.from_dict(row) for row in draft["control_resources"]]
            controls = [r for r in resources if r.role == "controlnet"]
            strength = self.remix_strength.value()
            inputs = [GenerationInput(kind="init_image", reference="sha256:" + sha,
                strength=strength, extras={"sha256": sha})]
            mask = self._retained_mask()
            if task == "masked" and mask:
                inputs.append(GenerationInput(kind="mask", reference="sha256:" + mask["mask_sha256"],
                    extras={"mask_payload": mask}))
            scale = float(self.upscale_factor.currentData()) if task == "upscale" else 1
            width, height = source.width(), source.height()
            if task == "outpaint":
                from core.generation_outpainting import CanvasExpansion, expanded_canvas
                expansion = CanvasExpansion(sha, width, height, **{edge: field.value() for edge, field in self.canvas_borders.items()})
                canvas = expanded_canvas(self.source_image_bytes, expansion)
                inputs[0].extras["canvas_expansion"] = expansion.to_dict()
                outpaint_mask = canvas["mask"]
                inputs.append(GenerationInput(kind="mask", reference="sha256:" + outpaint_mask.mask_sha256,
                    extras={"mask_payload": outpaint_mask.to_dict()}))
                width, height = canvas["width"], canvas["height"]
            # Swarm's supported dimensions are reviewed in the normal adapter preflight.
            if task == "upscale":
                width, height = round(width * scale / 8) * 8, round(height * scale / 8) * 8
            if self.sender() is self.remix_strength:
                width = self.plan.request.width or width
                height = self.plan.request.height or height
            request = {"stages": [GenerationStage(kind="first_pass", label=str(task),
                inputs=inputs, extras={"image_task": task}).to_dict()],
                "width": width, "height": height, "denoising_strength": strength}
            request["resources"] = [r.to_dict() for r in resources]
            if task in {"reference_image", "control_image"}:
                extras = {"sha256": sha, "source_width": width, "source_height": height,
                          "target_width": width, "target_height": height, "resize": None, "extras": {}}
                if task == "control_image":
                    extras.update(resource_id=("sha256:" + controls[0].hashes["sha256"].removeprefix("sha256:") if len(controls) == 1 else ""),
                                  strength=strength, start=0.0, end=1.0, preprocessor="None")
                request.update(denoising_strength=None, stages=[GenerationStage(kind="first_pass", label="Image guidance",
                    inputs=[GenerationInput(kind=task, reference="sha256:"+sha, extras=extras)],
                    extras={"image_task":"guided"}).to_dict()])
                if task == "reference_image":
                    for reference in self._additional_references:
                        extra = {"sha256": reference["sha256"], "source_width": reference["width"],
                            "source_height": reference["height"], "target_width": reference["width"],
                            "target_height": reference["height"], "resize": None, "extras": {}}
                        request["stages"][0]["inputs"].append(GenerationInput(kind="reference_image",
                            reference="sha256:" + reference["sha256"], extras=extra).to_dict())
            from core.generation_reference_inputs import rebase_reference_identity
            selected_identity = deepcopy(self._primary_source_identity)
            if task == "reference_image":
                selected_identity["collection"] += [{"image_sha256": r["sha256"], "knowledge_sha256": None}
                                                     for r in self._additional_references]
            self.plan = rebase_reference_identity(self.plan, selected_identity)
            self.source_identity = selected_identity
            self.source_current_provider = self._reference_current if len(selected_identity["collection"]) > 1 else self._primary_current_provider
            if mask:
                draft["mask_payload"] = mask
            if task == "outpaint":
                draft["canvas_expansion"] = expansion.to_dict()
            context["image_edit_draft"] = draft
            self.plan = self.plan.revise({"intent": "variation", "request": request, "target_context": context}, source="user",
                consequence="Selected image-guided task, exact source, dimensions and amount of change")
            self.prepared = None
            self.receipt = None
            self._refresh_from_plan("Source attached. Review the prompt, model and local connection before preparing.")
        except (ValueError, KeyError, TypeError) as exc:
            self.prepared = None
            self.status.setText(str(exc))

    def choose_reference_images(self):
        if self._pending or self._execution_active() or not self.source_image_bytes: return
        paths, _ = QFileDialog.getOpenFileNames(self, "Choose Up to Three Additional Visual References", "", "PNG images (*.png)")
        if not paths: return
        if len(paths) + len(self._additional_references) > 3:
            self.status.setText("Four images total are supported. Remove or replace an existing reference before adding more."); return
        from core.generation_reference_inputs import read_reference
        first = hashlib.sha256(self.source_image_bytes).hexdigest()
        retained = list(self._additional_references)
        def prepare():
            values = [read_reference(path) for path in paths]
            if len({first, *(r["sha256"] for r in retained + values)}) != len(retained) + len(values) + 1:
                raise ValueError("Each reference must contain a different image.")
            return retained + values
        def done(values):
            self._additional_references = values
            self._image_task_changed()
        self._run("select-references", prepare, done)

    def remove_reference_image(self, sha):
        if self._pending or self._execution_active(): return
        self._additional_references = [row for row in self._additional_references if row["sha256"] != sha]
        self._image_task_changed()

    def replace_reference_image(self, sha):
        if self._pending or self._execution_active(): return
        path, _ = QFileDialog.getOpenFileName(self, "Replace This Reference", "", "PNG images (*.png)")
        if not path: return
        from core.generation_reference_inputs import read_reference
        retained = list(self._additional_references)
        def prepare():
            value = read_reference(path)
            other_hashes = {hashlib.sha256(self.source_image_bytes).hexdigest(),
                *(row["sha256"] for row in retained if row["sha256"] != sha)}
            if value["sha256"] in other_hashes: raise ValueError("Each reference must contain a different image.")
            return [value if row["sha256"] == sha else row for row in retained]
        def done(values):
            self._additional_references = values; self._image_task_changed()
        self._run("replace-reference", prepare, done)

    def _refresh_reference_cards(self):
        key = tuple((row["sha256"], str(row["path"])) for row in self._additional_references)
        if self._reference_cards_key == key: return
        self._reference_cards_key = key
        while self.reference_cards_layout.count():
            item = self.reference_cards_layout.takeAt(0)
            if item.widget(): item.widget().deleteLater()
        for index, row in enumerate(self._additional_references, 2):
            card = QGroupBox(f"Reference {index}"); layout = QVBoxLayout(card)
            preview = ProtectedImageView(settings=self.settings)
            preview.setFixedHeight(230)
            preview.set_image(QPixmap.fromImage(QImage.fromData(row["bytes"])), row["path"])
            layout.addWidget(preview)
            name = QLabel(Path(row["path"]).name); name.setWordWrap(True); name.setTextFormat(Qt.PlainText)
            layout.addWidget(name)
            actions = QHBoxLayout(); replace = QPushButton("Replace…"); remove = QPushButton("Remove")
            replace.clicked.connect(lambda _checked=False, sha=row["sha256"]: self.replace_reference_image(sha))
            remove.clicked.connect(lambda _checked=False, sha=row["sha256"]: self.remove_reference_image(sha))
            actions.addWidget(replace); actions.addWidget(remove); layout.addLayout(actions)
            self.reference_cards_layout.addWidget(card)

    def reconnect_sources(self):
        if self._pending or self._execution_active(): return
        expected = self.plan.source_identity
        if expected.get("derived_frame"):
            self.status.setText("Reopen this extracted frame from its verified video result, then Open Draft to restore the request and frame lineage.")
            return
        files, _ = QFileDialog.getOpenFileNames(self, "Reconnect Exact Original Images", "", "Images (*.png *.jpg *.jpeg *.webp)")
        if not files: return
        collection = expected.get("collection", [])
        if not 1 <= len(collection) <= 4 or len(files) != len(collection):
            self.status.setText(f"Choose all {len(collection)} exact original source images together.")
            return
        from core.training_source_io import read_stable_training_source
        from core.generation_reference_inputs import read_reference
        from .generation_workflow import _source_identity
        def prepare():
            selected = {}
            for file in files:
                raw, evidence = read_stable_training_source(Path(file))
                selected[evidence["sha256"]] = (Path(file), raw)
            ordered = [selected.get(row.get("image_sha256")) for row in collection]
            if any(item is None for item in ordered):
                raise ValueError("These images do not match the saved source hashes. Choose the original files.")
            primary_path, raw = ordered[0]
            primary_identity = _source_identity((primary_path,))
            references = [read_reference(item[0]) for item in ordered[1:]]
            current = deepcopy(primary_identity)
            current["collection"].extend({"image_sha256": row["sha256"], "knowledge_sha256": None} for row in references)
            if current != expected:
                raise ValueError("The source image or its saved Image Knowledge changed. Reopen Remix from the original image to review a new draft.")
            def primary_current(): return _source_identity((primary_path,))
            def primary_image():
                data, evidence = read_stable_training_source(primary_path)
                return {"bytes": data, "sha256": evidence["sha256"]}
            return primary_identity, primary_current, primary_image, raw, references
        def done(value):
            self._primary_source_identity, self._primary_current_provider, self.source_image_provider, self.source_image_bytes, self._additional_references = value
            self.source_current_provider = self._reference_current
            self.source_current = deepcopy(expected)
            self.prepared = self.binding = None
            self._refresh_from_plan("Exact source images reconnected. Check the model and prepare this draft again.")
        self._run("reconnect-sources", prepare, done)

    def clear_reference_images(self):
        if self._pending or self._execution_active(): return
        self._additional_references = []
        self._image_task_changed()

    def _reference_image(self, sha):
        from core.generation_reference_inputs import read_reference
        row = next((r for r in self._additional_references if r["sha256"] == sha), None)
        if row is None:
            raise ValueError("Reconnect every exact additional reference image before generation.")
        current = read_reference(row["path"])
        if current["sha256"] != sha:
            raise ValueError("An additional reference changed. Select and review the images again.")
        return {"bytes": current["bytes"], "sha256": sha}

    def _reference_current(self):
        if not callable(self._primary_current_provider):
            raise ValueError("Reconnect the original reference image before generation.")
        current = deepcopy(self._primary_current_provider())
        if current != self._primary_source_identity:
            raise ValueError("The original reference image changed.")
        for row in self._additional_references:
            self._reference_image(row["sha256"])
            current["collection"].append({"image_sha256": row["sha256"], "knowledge_sha256": None})
        return current

    def edit_mask(self):
        if not self.source_image_bytes or self._pending or self._execution_active():
            return
        from core.generation_masks import MaskPayload
        from .generation_mask_dialog import GenerationMaskDialog
        existing = self._retained_mask()
        try:
            mask = MaskPayload.from_dict(existing) if existing else None
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        dialog = GenerationMaskDialog(self.source_image_bytes, parent=self,
            initial_mask_png=mask.mask_png if mask else None, settings=self.settings)
        if mask:
            dialog.feather.setValue(mask.feather_px)
            dialog.crop_context.setChecked(mask.crop_region is not None)
            dialog.crop_padding.setValue(mask.crop_padding_px)
        dialog.exec()
        payload = dialog.result_payload()
        if payload is None:
            return
        stages = [stage.to_dict() for stage in self.plan.request.stages]
        if not stages:
            return
        stages[0]["inputs"] = [item for item in stages[0]["inputs"] if item.get("kind") != "mask"] + [
            GenerationInput(kind="mask", reference="sha256:" + payload.mask_sha256,
                extras={"mask_payload": payload.to_dict()}).to_dict()]
        context = self.plan.target_context
        context["image_edit_draft"] = {**context.get("image_edit_draft", {}), "mask_payload": payload.to_dict()}
        try:
            self.plan = self.plan.revise({"stages": stages, "target_context": context}, source="user", consequence="Applied the reviewed painted area")
        except (GenerationPlanError, ValueError) as exc:
            self.status.setText(str(exc) + " The previous draft remains unchanged.")
            return
        self.prepared = None
        self._refresh_from_plan("Mask applied to this draft. Review and prepare before submitting.")

    def _refresh_source_panel(self):
        stages = self.plan.request.stages
        linked = any(item.kind in {"init_image", "control_image", "reference_image"} for stage in stages for item in stage.inputs)
        self.remix_source = linked or self.remix_source
        self.source_panel.setVisible(linked)
        self.source_scroll.setVisible(linked)
        task = str(stages[0].extras.get("image_task", "remix")) if linked else "remix"
        if task == "guided":
            task = stages[0].inputs[0].kind
        self.image_task.blockSignals(True)
        self.image_task.setCurrentIndex(max(0, self.image_task.findData(task)))
        self.image_task.blockSignals(False)
        self.source_task_summary.setText(
            f"Image task: {self.image_task.currentText()}. The original image stays unchanged. "
            + ("Upscaling may redraw details. " if task == "upscale" else "")
            + "Review the prompt, model and output before preparing."
        )
        self.source_task_summary.setVisible(linked)
        self._request_form.setRowVisible(self.intent, not linked)
        self.remix_strength.blockSignals(True)
        self.remix_strength.setValue(stages[0].inputs[0].extras.get("strength", .5) if task == "control_image"
                                     else self.plan.request.denoising_strength or .5)
        self.remix_strength.blockSignals(False)
        self.edit_mask_button.setVisible(task == "masked")
        self.canvas_controls.setVisible(task == "outpaint")
        self.control_models_button.setVisible(task == "control_image")
        self.control_help.setVisible(task in {"control_image", "reference_image"})
        for widget in (self.references_button, self.clear_references_button, self.references_summary, self.reference_cards):
            widget.setVisible(task == "reference_image")
        self._refresh_reference_cards()
        reference_count = sum(item.kind == "reference_image" for stage in stages for item in stage.inputs)
        self.references_summary.setText(f"{reference_count} visual reference(s). Up to four images give combined visual guidance; separate identity/style control is not supported by this adapter.")
        if task == "outpaint":
            for stage in self.plan.request.stages:
                for item in stage.inputs:
                    expansion = item.extras.get("canvas_expansion", {})
                    if expansion:
                        for edge, field in self.canvas_borders.items():
                            field.blockSignals(True); field.setValue(expansion[edge]); field.blockSignals(False)
        self.upscale_factor.setVisible(task == "upscale")
        origin_intent = self.plan.target_context.get("image_origin_intent")
        guided_change = origin_intent in {"close", "transform"}
        self.repair_intent.setVisible(task != "upscale" and not guided_change)
        self.apply_repair_button.setVisible(task != "upscale" and not guided_change)
        exact = bool(self.source_image_bytes and len(self.plan.source_identity.get("collection", [])) >= 1
            and hashlib.sha256(self.source_image_bytes).hexdigest() == self.plan.source_identity["collection"][0].get("image_sha256"))
        self.reconnect_source_button.setVisible(linked and not exact)
        self.reconnect_source_button.setEnabled(not self._pending and not self._execution_active())
        for widget in (self.image_task, self.remix_strength, self.edit_mask_button, self.upscale_factor):
            widget.setEnabled(linked and exact and not self._pending and not self._execution_active())
        self.remix_strength.setEnabled(task != "reference_image" and linked and exact and not self._pending and not self._execution_active())
        self.references_button.setEnabled(exact and not self._pending and not self._execution_active())
        self.clear_references_button.setEnabled(exact and reference_count > 1 and not self._pending and not self._execution_active())
        for widget in (self.apply_repair_button, self.repair_intent, self.control_models_button):
            widget.setEnabled(linked and exact and not self._pending and not self._execution_active())
        if exact:
            image = QImage.fromData(self.source_image_bytes)
            self.source_preview.set_image(QPixmap.fromImage(image))
            self.source_preview.show()
            message = f"Source: {image.width()} × {image.height()}\nOutput: {self.plan.request.width or '—'} × {self.plan.request.height or '—'}"
        else:
            self.source_preview.clear()
            message = "Reconnect Source Images to resume this saved draft. Original pixels and Image Knowledge must still match."
        repair = self.plan.target_context.get("repair_context")
        if isinstance(repair, dict) and isinstance(repair.get("reason"), str) and repair["reason"]:
            message += "\nProblem: " + repair["reason"][:4096]
        if origin_intent in {"close", "transform"}:
            label = "Close variation" if origin_intent == "close" else "Substantial change"
            message += f"\nStarting choice: {label} (current settings may differ; not a guarantee)"
        if task == "masked":
            message += "\nMask attached; black areas stay protected." if self._mask_payload() else "\nPaint an editable area before preparing."
        self.source_detail.setText(message)

    def _build_request_tab(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        form = QFormLayout()
        self.source_task_summary = QLabel()
        self.source_task_summary.setWordWrap(True)
        self.source_task_summary.setTextFormat(Qt.PlainText)
        self.source_task_summary.setAccessibleName("Selected image task and consequence")
        self.source_task_summary.hide()
        layout.addWidget(self.source_task_summary)
        self.intent = QComboBox()
        for label, value in (("Reproduce recorded generation", "reproduce"), ("Create a variation", "variation"), ("Create something new", "new")):
            self.intent.addItem(label, value)
        self.goal = QLineEdit(); self.goal.setMaxLength(500)
        self.goal.setPlaceholderText("Optional planning note; not sent to the generator")
        self.positive = QTextEdit(); self.positive.setFixedHeight(90); self.positive.setAccessibleName("Image prompt sent to generator")
        self.positive.setPlaceholderText("Describe the image you want to generate. This text is sent to the generator.")
        self.negative = QTextEdit(); self.negative.setFixedHeight(70); self.negative.setAccessibleName("Negative prompt")
        self.negative.setPlaceholderText("Optional details to avoid in the image")
        self.model = QLineEdit(); self.model.setPlaceholderText("Exact generator model name")
        self.model_hash = QLineEdit(); self.model_hash.setPlaceholderText("Known full SHA-256 from Models")
        form.addRow("Intent", self.intent)
        form.addRow("Image prompt", self.positive)
        self.request_details_toggle = QCheckBox("Optional prompt details (planning note and things to avoid)")
        self.request_details_toggle.setAccessibleDescription(
            "Reveal a planning note that is not sent to the generator and optional negative-prompt text."
        )
        form.addRow(self.request_details_toggle)
        form.addRow("Planning note (not sent)", self.goal)
        form.addRow("Avoid in the image", self.negative)
        for field in (self.goal, self.negative):
            form.setRowVisible(field, False)
        def show_request_details(visible):
            for field in (self.goal, self.negative):
                form.setRowVisible(field, visible)
        self.request_details_toggle.toggled.connect(show_request_details)
        suggestions = QGroupBox("Search Prompt Suggestions")
        suggestion_layout = QVBoxLayout(suggestions)
        search_row = QHBoxLayout()
        self.prompt_tag_search = QLineEdit()
        self.prompt_tag_search.setPlaceholderText("Search known SDXL Tags and aliases…")
        self.prompt_tag_search.setAccessibleName("Search known Tags for prompt suggestions")
        self.prompt_tag_target = QComboBox()
        self.prompt_tag_target.addItem("Positive prompt", "positive")
        self.prompt_tag_target.addItem("Negative prompt", "negative")
        search_row.addWidget(self.prompt_tag_search, 1)
        search_row.addWidget(self.prompt_tag_target)
        suggestion_layout.addLayout(search_row)
        self.prompt_tag_status = QLabel("Type at least two characters to search the shared Tag vocabulary. Suggestions do not change Image Knowledge.")
        self.prompt_tag_status.setWordWrap(True)
        suggestion_layout.addWidget(self.prompt_tag_status)
        self.prompt_tag_container = QWidget()
        self.prompt_tag_grid = QGridLayout(self.prompt_tag_container)
        self.prompt_tag_grid.setContentsMargins(0, 0, 0, 0)
        suggestion_layout.addWidget(self.prompt_tag_container)
        self.prompt_tag_timer = QTimer(self)
        self.prompt_tag_timer.setSingleShot(True)
        self.prompt_tag_timer.setInterval(180)
        self.prompt_tag_timer.timeout.connect(self._refresh_prompt_tags)
        self.prompt_tag_search.textChanged.connect(lambda:self.prompt_tag_timer.start())
        self.prompt_templates_button = QPushButton("Presets and Wildcards…")
        self.prompt_templates_button.clicked.connect(self.edit_prompt_templates)
        suggestion_layout.addWidget(self.prompt_templates_button, 0, Qt.AlignRight)
        self.regional_prompts_button = QPushButton("Place Regional Prompts…")
        self.regional_prompts_button.setToolTip("Guide up to two rectangular areas of a new SDXL image. Choose the base size first; execution requires supported local Swarm.")
        self.regional_prompts_button.clicked.connect(self.edit_regional_prompts)
        suggestion_layout.addWidget(self.regional_prompts_button, 0, Qt.AlignRight)
        model_row = QHBoxLayout()
        self.main_model_choice = QLabel()
        self.main_model_choice.setWordWrap(True); self.main_model_choice.setTextFormat(Qt.PlainText)
        self.choose_resource_button = QPushButton("Choose Main Model…")
        self.choose_resource_button.setToolTip("Choose the checkpoint that creates the image. A new choice replaces only the main model.")
        self.choose_resource_button.clicked.connect(lambda: self.choose_registered_resource("checkpoint"))
        model_row.addWidget(self.main_model_choice, 1); model_row.addWidget(self.choose_resource_button)
        form.addRow("Main model", model_row)
        form.addRow("Exact model name", self.model); form.addRow("Model hash", self.model_hash)
        seed_row = QWidget(); seed_layout = QHBoxLayout(seed_row); seed_layout.setContentsMargins(0, 0, 0, 0)
        self.seed_mode = QComboBox(); self.seed_mode.setAccessibleName("Seed choice")
        for label, mode in (("Same Seed", "same"), ("Specific Seed", "specific"), ("Random Seed", "random")):
            self.seed_mode.addItem(label, mode)
        self.seed = QLineEdit(); self.seed.setPlaceholderText("Enter a whole seed, including 0")
        self.seed.setAccessibleName("Exact generation seed")
        self.random_seed_button = QPushButton("New Random Seed")
        self.random_seed_button.setAutoDefault(False)
        self.random_seed_button.clicked.connect(lambda: self._choose_seed("random"))
        seed_layout.addWidget(self.seed_mode); seed_layout.addWidget(self.seed, 1); seed_layout.addWidget(self.random_seed_button)
        form.addRow("Seed", seed_row)
        self.seed_detail = QLabel(); self.seed_detail.setWordWrap(True); self.seed_detail.setTextFormat(Qt.PlainText)
        form.addRow(self.seed_detail)
        self.seed_mode.currentIndexChanged.connect(lambda: self._choose_seed(str(self.seed_mode.currentData())))
        self._request_form = form
        layout.addLayout(form)
        self.prompt_helpers_toggle = QCheckBox(
            "Show prompt helpers (Tags, prompt-text presets, regions)"
        )
        self.prompt_helpers_toggle.setAccessibleDescription(
            "Reveal optional prompt aids without changing this request."
        )
        suggestions.hide()
        self.prompt_helpers_toggle.toggled.connect(suggestions.setVisible)
        layout.addWidget(self.prompt_helpers_toggle)
        layout.addWidget(suggestions)
        output_group = QGroupBox("Desired final output")
        output_form = QFormLayout(output_group)
        self.generation_goal_preset = QComboBox()
        for label, key in (("Quick Preview · model-sized image, no finishing", "quick_preview"),
                           ("Balanced · faithful final size", "balanced"),
                           ("Final Output · choose how to finish", "final_output"),
                           ("Lower Memory · smaller base, faithful final size", "lower_memory")):
            self.generation_goal_preset.addItem(label, key)
        self.generation_goal_preset.setCurrentIndex(2)
        self.generation_goal_preset.setAccessibleName("Generation goal preset")
        self.output_preset = QComboBox()
        for label, size in (("Choose a final size", None), ("Square · 1024 × 1024", (1024, 1024)),
                            ("Portrait · 832 × 1216", (832, 1216)),
                            ("Landscape · 1216 × 832", (1216, 832)),
                            ("Widescreen · 1344 × 768", (1344, 768)),
                            ("Custom", "custom")):
            self.output_preset.addItem(label, size)
        self.output_aspect = QComboBox()
        self.output_aspect.addItem("Custom aspect", None)
        for label, ratio in IMAGE_ASPECT_RATIOS:
            self.output_aspect.addItem(label, ratio)
        self.output_aspect.setAccessibleName("Final image aspect ratio")
        self.output_swap = QPushButton("Swap Width / Height")
        self.output_swap.setAutoDefault(False)
        self.output_swap.setToolTip("Swap the desired final dimensions; preview again before changing generation size.")
        self.final_width = QSpinBox(); self.final_height = QSpinBox()
        for control in (self.final_width, self.final_height):
            control.setRange(0, 8192); control.setSingleStep(64)
            control.setSpecialValueText("Choose size")
        self.output_preference = QComboBox()
        self.output_preference.addItem("Resize without adding detail · Lanczos", "faithful")
        self.output_preference.addItem("Refine at generation size, then resize", "generative")
        self.output_preview_button = QPushButton("Preview Recommended Plan")
        self.output_apply_button = QPushButton("Use Recommended Settings")
        self.output_summary = QLabel("Choose a desired final size; generator base size remains separate.")
        self.output_summary.setWordWrap(True); self.output_summary.setTextFormat(Qt.PlainText)
        output_summary_policy = self.output_summary.sizePolicy()
        output_summary_policy.setVerticalPolicy(QSizePolicy.Minimum)
        self.output_summary.setSizePolicy(output_summary_policy)
        self.output_details_toggle = QCheckBox("Show plan and memory details")
        self.output_details = QTextEdit()
        self.output_details.setReadOnly(True)
        self.output_details.setAccessibleName("Output plan and memory details")
        self.output_details.setFixedHeight(160)
        self.output_details.setVisible(False)
        self.output_details_toggle.toggled.connect(self.output_details.setVisible)
        output_form.addRow("Goal preset", self.generation_goal_preset)
        aspect_row = QHBoxLayout(); aspect_row.addWidget(self.output_aspect, 1); aspect_row.addWidget(self.output_swap)
        output_form.addRow("Aspect ratio", aspect_row)
        output_form.addRow("Final size", self.output_preset)
        self.output_customization = QCheckBox("Customize exact sizes and finishing")
        self.output_customization.setAccessibleDescription(
            "Reveal exact final and generation pixels and the finishing method without changing the draft."
        )
        output_form.addRow(self.output_customization)
        final_row = QHBoxLayout(); final_row.addWidget(self.final_width); final_row.addWidget(QLabel("×")); final_row.addWidget(self.final_height)
        self.final_width.setAccessibleName("Desired final width")
        self.final_height.setAccessibleName("Desired final height")
        output_form.addRow("Final pixels", final_row)
        self.size_width = QSpinBox(); self.size_height = QSpinBox()
        for control, label in ((self.size_width, "Generator base width"), (self.size_height, "Generator base height")):
            control.setRange(0, 16384); control.setSingleStep(64)
            control.setSpecialValueText("Unspecified"); control.setAccessibleName(label)
        base_row = QHBoxLayout(); base_row.addWidget(self.size_width); base_row.addWidget(QLabel("×")); base_row.addWidget(self.size_height)
        output_form.addRow("Generation pixels", base_row)
        base_help = QLabel("Generation pixels control the model's workload. Final pixels are the size you want to keep. Preview and apply a recommendation, or edit the generation size directly.")
        base_help.setWordWrap(True); output_form.addRow(base_help)
        output_form.addRow("Detail preference", self.output_preference)
        actions = QHBoxLayout(); actions.addWidget(self.output_preview_button); actions.addWidget(self.output_apply_button)
        output_form.addRow(actions)
        output_form.addRow(self.output_summary)
        output_form.addRow(self.output_details_toggle)
        output_form.addRow(self.output_details)
        custom_rows = (final_row, base_row, base_help, self.output_preference)
        for row in custom_rows:
            output_form.setRowVisible(row, False)
        def show_custom_output(visible):
            for row in custom_rows:
                output_form.setRowVisible(row, visible)
        self.output_customization.toggled.connect(show_custom_output)
        self.output_preset.currentIndexChanged.connect(self._select_output_preset)
        self.output_aspect.currentIndexChanged.connect(self._select_output_aspect)
        self.output_swap.clicked.connect(self._swap_output_orientation)
        self.final_width.valueChanged.connect(self._output_goal_changed)
        self.final_height.valueChanged.connect(self._output_goal_changed)
        self.output_preference.currentIndexChanged.connect(self._output_goal_changed)
        self.generation_goal_preset.currentIndexChanged.connect(self._output_goal_changed)
        self.output_preview_button.clicked.connect(self.preview_output_plan)
        self.output_apply_button.clicked.connect(self.apply_best_base)
        additions = QGroupBox("Optional model additions")
        additions_layout = QVBoxLayout(additions)
        additions_help = QLabel("LoRAs add a trained style or subject. A VAE controls image decoding; usually the main model's default is enough. Choose compatible additions only when needed.")
        additions_help.setWordWrap(True); additions_layout.addWidget(additions_help)
        addition_actions = QHBoxLayout()
        self.add_lora_button = QPushButton("Add LoRA…")
        self.add_lora_button.clicked.connect(lambda: self.choose_registered_resource("lora"))
        self.choose_vae_button = QPushButton("Choose VAE…")
        self.choose_vae_button.setToolTip("Choose an optional VAE override. Replaces an earlier VAE choice, keeping the main model and LoRAs.")
        self.choose_vae_button.clicked.connect(lambda: self.choose_registered_resource("vae"))
        self.review_models_button = QPushButton("Review / Remove Models…")
        self.review_models_button.clicked.connect(lambda: self.tabs.setCurrentIndex(1))
        for button in (self.add_lora_button, self.choose_vae_button, self.review_models_button):
            addition_actions.addWidget(button)
        addition_actions.addStretch(1); additions_layout.addLayout(addition_actions)
        self.additions_summary = QLabel()
        self.additions_summary.setWordWrap(True); self.additions_summary.setTextFormat(Qt.PlainText)
        self.optional_model_toggle = QCheckBox("Customize optional models (LoRAs and VAE)")
        self.optional_model_toggle.setAccessibleDescription(
            "Reveal optional model choices; selected additions remain summarized above."
        )
        self.optional_model_toggle.toggled.connect(additions.setVisible)
        additions.hide()
        guidance_row = QHBoxLayout()
        self.refresh_model_details_button = QPushButton("Refresh Model Details")
        self.refresh_model_details_button.clicked.connect(self.refresh_model_details)
        self.model_guidance_button = QPushButton("Recommended Setup…")
        self.model_guidance_button.clicked.connect(self.preview_model_guidance)
        guidance_row.addWidget(self.model_guidance_button)
        guidance_row.addWidget(self.refresh_model_details_button)
        self.model_setup_status = QLabel("Choose a main model, then review its requirements and available trigger words.")
        self.model_setup_status.setWordWrap(True)
        self.model_setup_status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.model_setup_status)
        self.model_trigger_container = QWidget()
        self.model_trigger_grid = QGridLayout(self.model_trigger_container)
        self.model_trigger_grid.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(self.model_trigger_container)
        guidance_row.addStretch(1)
        layout.addLayout(guidance_row)
        layout.addWidget(output_group)
        layout.addWidget(self.additions_summary)
        layout.addWidget(self.optional_model_toggle)
        layout.addWidget(additions)
        self.request_tools_toggle = QCheckBox("More: Generation Setups and comparisons")
        self.request_tools_toggle.setAccessibleDescription(
            "Reveal setup save and apply actions and alternative request comparisons."
        )
        exploration_widget = QWidget()
        exploration = QHBoxLayout(exploration_widget)
        exploration.setContentsMargins(0, 0, 0, 0)
        self.experiment_button = QPushButton("Alternatives and Compare…")
        self.experiment_button.clicked.connect(self.open_experiment)
        self.save_recipe_button = QPushButton("Save This Setup…")
        self.save_recipe_button.clicked.connect(self.save_recipe)
        self.load_recipe_button = QPushButton("Apply Saved Setup…")
        self.load_recipe_button.clicked.connect(self.load_recipe)
        for button in (self.experiment_button, self.save_recipe_button, self.load_recipe_button):
            exploration.addWidget(button)
        exploration.addStretch(1)
        self.request_tools_toggle.toggled.connect(exploration_widget.setVisible)
        layout.addWidget(self.request_tools_toggle)
        layout.addWidget(exploration_widget)
        exploration_widget.hide()
        self.other_components = QWidget()
        resource_row = QHBoxLayout(self.other_components)
        resource_row.setContentsMargins(0, 0, 0, 0)
        self.resource_role = QComboBox()
        self.resource_role.setAccessibleName("Other model component type")
        from core.model_workspace import RESOURCE_ROLES
        for role in RESOURCE_ROLES:
            if role in {"tagger", "captioner", "checkpoint", "lora", "vae"}:
                continue
            self.resource_role.addItem(role.replace("_", " ").title(), role)
        self.choose_other_resource_button = QPushButton("Add Component…")
        self.choose_other_resource_button.clicked.connect(lambda: self.choose_registered_resource(str(self.resource_role.currentData())))
        resource_row.addWidget(QLabel("Other component"))
        resource_row.addWidget(self.resource_role)
        resource_row.addWidget(self.choose_other_resource_button)
        resource_row.addStretch(1)
        self.expert_settings = QCheckBox("Show Expert Settings")
        self.expert_settings.setAccessibleDescription("Reveal exact model identity and sampling controls without changing any values.")
        layout.addWidget(self.expert_settings)
        layout.addWidget(self.other_components)
        sampling = QGroupBox("Sampling") ; sampling_form = QFormLayout(sampling)
        self.sampling_group = sampling
        self.steps = QSpinBox(); self.steps.setRange(0, 10000)
        for field in (self.size_width, self.size_height, self.steps):
            field.setSpecialValueText("Unspecified")
        self.cfg_scale = QLineEdit(); self.cfg_scale.setPlaceholderText("CFG scale")
        self.sampler = QLineEdit(); self.scheduler = QLineEdit()
        self.sampling_notice = QLabel()
        self.sampling_notice.setWordWrap(True); self.sampling_notice.setTextFormat(Qt.PlainText)
        sampling_form.addRow(self.sampling_notice)
        sampling_form.addRow("Steps", self.steps); sampling_form.addRow("CFG scale", self.cfg_scale)
        sampling_form.addRow("Sampler", self.sampler); sampling_form.addRow("Scheduler", self.scheduler)
        layout.addWidget(sampling); layout.addStretch(1)
        self.expert_settings.toggled.connect(self._show_expert_settings)
        self._show_expert_settings(False)
        self.intent.currentIndexChanged.connect(self._edit)
        for field in (self.goal, self.model, self.model_hash, self.seed, self.cfg_scale, self.sampler, self.scheduler): field.textChanged.connect(self._edit)
        for field in (self.positive, self.negative): field.textChanged.connect(self._edit)
        self.size_width.valueChanged.connect(self._edit); self.size_height.valueChanged.connect(self._edit); self.steps.valueChanged.connect(self._edit)
        return page

    def _refresh_prompt_tags(self) -> None:
        while self.prompt_tag_grid.count():
            item = self.prompt_tag_grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()
        query = self.prompt_tag_search.text().strip().casefold()
        knowledge = self.prompt_knowledge
        if len(query) < 2 or knowledge is None:
            self.prompt_tag_status.setText("Type at least two characters to search the shared Tag vocabulary. Suggestions do not change Image Knowledge.")
            return
        matches = []
        for record in knowledge.tags.values():
            names = knowledge.identity_names(record)
            if any(query in str(name).casefold() for name in names):
                matches.append(record)
                if len(matches) >= 40:
                    break
        for index, record in enumerate(matches):
            chip = TagChoiceChip(record.display, category_label="Prompt suggestion",
                editable=False, selectable=True, stable_id=record.id,
                parent=self.prompt_tag_container)
            chip.setAccessibleName(f"Add {record.display} to {self.prompt_tag_target.currentText()}")
            chip.selection_requested.connect(lambda value, _modifiers:self._add_prompt_tag(value))
            self.prompt_tag_grid.addWidget(chip, index // 5, index % 5)
        self.prompt_tag_status.setText(f"{len(matches)} matching Tags shown. Choose a chip to add its text to the selected prompt.")

    def _add_prompt_tag(self, value: str) -> None:
        editor = self.negative if self.prompt_tag_target.currentData() == "negative" else self.positive
        current = editor.toPlainText().rstrip()
        editor.setPlainText(f"{current}, {value}" if current else value)
        self.status.setText(f"Added {value} to the {self.prompt_tag_target.currentText().lower()}. Review the exact request before submission.")

    def edit_regional_prompts(self):
        from PySide6.QtWidgets import QDialog
        if self._pending or self._execution_active():
            return
        request = self.plan.request
        if not request.width or not request.height:
            self.status.setText("Choose and apply a generator base size before placing regional prompts.")
            self.tabs.setCurrentIndex(0)
            self.output_preset.setFocus()
            return
        from .generation_regions_dialog import GenerationRegionsDialog
        try:
            dialog = GenerationRegionsDialog(self, global_prompt=request.positive_prompt,
                width=request.width, height=request.height,
                spec=self.plan.target_context.get("regional_prompt"))
        except (ValueError, TypeError, KeyError, AttributeError):
            dialog = GenerationRegionsDialog(self, global_prompt=request.positive_prompt,
                width=request.width, height=request.height)
            dialog.status.setText("The saved regional settings are invalid. Rebuild the rectangles, or apply an empty layout to remove them. Cancel preserves the saved settings.")
        if dialog.exec() != QDialog.DialogCode.Accepted:
            return
        context = self.plan.target_context
        spec = dialog.accepted_spec
        if spec and spec["regions"]:
            context["regional_prompt"] = spec
        else:
            context.pop("regional_prompt", None)
        self.plan = self.plan.revise({"target_context": context}, source="user",
            consequence="Reviewed rectangular prompts for the current base canvas")
        self.prepared = self.receipt = None
        self._generation += 1
        self._refresh_from_plan("Regional prompts updated. Review the request and check exact model/adapter readiness before submitting.")

    def edit_prompt_templates(self):
        if self._pending or self._execution_active():
            return
        from .generation_prompt_templates_dialog import GenerationPromptTemplatesDialog
        from core.swarm_preset_source import make_swarm_preset_import_service
        before = self.plan.to_dict()
        service = self.swarm_preset_service
        if service is None:
            service = make_swarm_preset_import_service(self.storage_root)
        dialog = GenerationPromptTemplatesDialog(self.plan.request.to_dict(),
            initial_context=self.plan.target_context.get("prompt_templates", {}), parent=self,
            jobs=self.jobs, storage_root=self.storage_root, swarm_import_service=service,
            settings=self.settings)
        self._prompt_templates_dialog = dialog
        dialog.background_work_finished.connect(self._notify_background_work_finished)
        try:
            dialog.exec()
        finally:
            self._prompt_templates_dialog = None
            self._notify_background_work_finished()
        result = dialog.result_payload()
        if result is None:
            return
        if self.plan.to_dict() != before or self._shutdown_requested or self._closed:
            self.status.setText("The generation draft changed while presets were open. Reopen the preset and review it again; collection saves remain available.")
            return
        context = self.plan.target_context
        context["prompt_templates"] = result["context"]
        try:
            current_request = self.plan.request.to_dict()
            changes = {key: value for key, value in result["request"].items()
                       if key not in current_request or value != current_request[key]}
            self.plan = self.plan.revise({"request": changes, "target_context": context},
                source="prompt_templates", consequence="Applied reviewed preset and exact deterministic wildcard resolution")
        except (GenerationPlanError, ValueError) as exc:
            self.status.setText(str(exc) + " The previous request remains unchanged.")
            return
        self.prepared = self.receipt = None
        self._generation += 1
        self._refresh_from_plan("Applied the previewed prompt. Authored text and wildcard choices are retained with this plan.")

    def refresh_model_details(self):
        if self.registry is None or self._pending or self._execution_active():
            return
        request = self.plan.request
        resource_id = "sha256:" + request.model_hash.removeprefix("sha256:") if request.model_hash else ""
        selected = {item.role: item.id for item in request.resources if item.role in {"vae", "text_encoder"}}
        identities = {item.id for item in request.resources} | {resource_id}
        def prepare():
            from core.model_selection import ModelSelectionService
            from core.model_setup_recommendations import recommend_model_setup
            from core.model_workflow_guidance import ModelWorkflowGuidanceService
            snapshot, revision = self.registry.snapshot()
            catalog = ModelWorkflowGuidanceService(update_path=self.registry.path.with_name("model_workflow_knowledge.json"))
            suggestion = recommend_model_setup(snapshot, resource_id, catalog=catalog,
                current_request=request.to_dict(), selected_companions=selected) if resource_id in snapshot["resources"] else None
            # Project this captured registry revision so cards and requirements agree.
            from types import SimpleNamespace
            rows, _ = ModelSelectionService(SimpleNamespace(snapshot=lambda: (snapshot, revision))).project()
            triggers = tuple(dict.fromkeys((row.title or row.resource_id, word)
                for row in rows if row.resource_id in identities for word in row.provider_triggers))
            return suggestion, triggers
        def show(result):
            suggestion, triggers = result
            lines = []
            if suggestion is not None:
                lines.append(f"{suggestion.family or 'Family unknown'} · {suggestion.family_source}")
                lines.extend(f"{slot['slot'].replace('_', ' ').title()}: {slot['state'].replace('_', ' ').title()}"
                    + (f" · {slot['selected_status']}" if slot.get('selected_status') else "") for slot in suggestion.companion_slots)
                lines.extend(suggestion.blockers)
                if suggestion.preserved_fields:
                    lines.append("Your settings are retained: " + ", ".join(suggestion.preserved_fields))
            else:
                lines.append("Choose a registered main model to see its requirements.")
            lines.append("Trigger words below are provider suggestions attached to the selected exact model files; choose a chip to add it to the prompt.")
            self.model_setup_status.setText("\n".join(lines))
            while self.model_trigger_grid.count():
                item = self.model_trigger_grid.takeAt(0)
                if item.widget() is not None:
                    item.widget().deleteLater()
            for index, (title, word) in enumerate(triggers[:40]):
                chip = TagChoiceChip(word, category_label="Model trigger", editable=False, selectable=True,
                    stable_id=f"model-trigger-{index}", parent=self.model_trigger_container)
                chip.setToolTip(title)
                chip.selection_requested.connect(lambda value, _modifiers: self._add_prompt_tag(value))
                self.model_trigger_grid.addWidget(chip, index // 5, index % 5)
        self._run("model_details", prepare, show)

    def _show_expert_settings(self, visible: bool) -> None:
        self.sampling_group.setVisible(visible)
        self.other_components.setVisible(visible)
        self._request_form.setRowVisible(self.model, visible)
        self._request_form.setRowVisible(self.model_hash, visible)

    def _refresh_seed_controls(self) -> None:
        source = self.plan.source
        known = source is not None and type(source.seed) is int and 0 <= source.seed < 2**63
        self.seed_mode.model().item(0).setEnabled(known)
        self.seed_mode.setItemData(0, "Use the source image's recorded seed." if known else "The source image has no supported recorded seed.", Qt.ToolTipRole)
        mode = self.plan.seed_choice
        self.seed_mode.blockSignals(True); self.seed_mode.setCurrentIndex(self.seed_mode.findData(mode)); self.seed_mode.blockSignals(False)
        self.seed.setReadOnly(mode != "specific")
        self.random_seed_button.setVisible(mode == "random")
        self.seed_detail.setText({
            "same": "Uses the source image's recorded seed. Other settings still affect the result.",
            "specific": "Enter an exact seed. Same Seed is unavailable without a recorded source seed." if not known else "Enter an exact seed, or choose Same Seed to restore the source seed.",
            "random": "This random seed is saved with this plan. Choose New Random Seed for another variation.",
        }[mode])

    def _choose_seed(self, mode: str) -> None:
        if getattr(self, "_refreshing", False):
            return
        try:
            value = int(self.seed.text().strip()) if mode == "specific" and self.seed.text().strip() else None
            self.plan = self.plan.choose_seed(mode, value)
        except (GenerationPlanError, ValueError) as exc:
            self._refresh_seed_controls()
            self.status.setText(str(exc))
            return
        self.prepared = None; self.receipt = None
        self._generation += 1
        self.seed.blockSignals(True); self.seed.setText(str(self.plan.request.seed)); self.seed.blockSignals(False)
        self._displayed_request["seed"] = self.plan.request.seed
        self._refresh_seed_controls()
        self._refresh_review("Seed choice updated. Review and prepare this request before submitting.")
        if mode == "specific":
            self.seed.setFocus(Qt.OtherFocusReason)

    def _seed_input_valid(self) -> bool:
        try:
            value = int(self.seed.text().strip())
        except ValueError:
            return False
        return 0 <= value < 2**63 and value == self.plan.request.seed

    def _ordinary_input_issue(self) -> tuple[str, QWidget | None]:
        """Keep visible, unapplied Request edits out of checks and execution."""
        seed_is_optional_and_empty = self.plan.request.seed is None and not self.seed.text().strip()
        if not self._seed_input_valid() and not seed_is_optional_and_empty:
            target = self.seed if self.seed_mode.currentData() == "specific" else self.seed_mode
            return "Choose a valid seed before checking or using this request.", target
        try:
            displayed = self._request_field_values()
        except ValueError:
            return "Complete CFG scale as a number before checking or using this request.", self.cfg_scale
        fields = {
            "positive_prompt": ("Image prompt", self.positive),
            "negative_prompt": ("Things to avoid", self.negative),
            "model": ("Model name", self.model),
            "model_hash": ("Model identity", self.model_hash),
            "width": ("Base width", self.size_width),
            "height": ("Base height", self.size_height),
            "steps": ("Steps", self.steps),
            "cfg_scale": ("CFG scale", self.cfg_scale),
            "sampler": ("Sampler", self.sampler),
            "scheduler": ("Scheduler", self.scheduler),
        }
        for key, (label, target) in fields.items():
            if displayed[key] != self._displayed_request[key]:
                return (f"Correct {label} before checking or using this request; "
                    "the saved plan still has the previous value."), target
        return "", None

    def _chosen_model_summary(self) -> str:
        request = self.plan.request
        checkpoints = [item for item in request.resources if item.role == "checkpoint"]
        name = (self._display_resource(checkpoints[-1]) or "Selected checkpoint") if checkpoints else request.model
        companions = sum(item.role != "checkpoint" for item in request.resources)
        label = (name or "No checkpoint chosen") + (" · checkpoint" if name else "")
        if companions:
            label += f" · Companion resources: {companions}; see Review"
        return label

    def _display_resource(self, resource) -> str:
        from .generation_workflow import _resource_display_label
        return _resource_display_label(resource, self.plan.target_context)

    def preview_model_guidance(self):
        from core.model_workflow_guidance import ModelWorkflowGuidanceService, machine_capability_from_setup
        from core.model_setup_recommendations import recommend_model_setup
        current_request=self.plan.request.to_dict()
        identity="sha256:"+self.plan.request.model_hash.removeprefix("sha256:") if self.plan.request.model_hash else ""
        def prepare():
            snapshot = self.registry.snapshot()[0] if self.registry is not None else {"resources": {}}
            machine = machine_capability_from_setup(self.settings, self.jobs)
            local = self.registry.path.with_name("model_workflow_knowledge.json") if self.registry is not None else None
            service=ModelWorkflowGuidanceService(update_path=local)
            selected = {item.role: item.id for item in self.plan.request.resources if item.role in {"vae", "text_encoder"}}
            suggestion=recommend_model_setup(snapshot,identity,catalog=service.catalog,current_request=current_request,
                selected_companions=selected) if identity in snapshot["resources"] else None
            return service, machine, snapshot, suggestion
        self._run("model-guidance", prepare, self._model_guidance_ready)

    def _model_guidance_ready(self, prepared):
        from PySide6.QtWidgets import QDialog
        from core.model_workflow_guidance import GenerationConfigurationRecommendation
        from .model_workflow_guidance_dialog import ModelWorkflowGuidanceDialog
        service, machine, snapshot = prepared[:3]
        suggestion=prepared[3] if len(prepared)>3 else None
        context=f"Selected model: {self._chosen_model_summary()}"
        if suggestion is not None:
            context += f"\nSuggested family: {suggestion.family or 'unknown'} · {suggestion.family_source}."
        request = self.plan.request
        current = GenerationConfigurationRecommendation.from_request(
            width=request.width or 1024, height=request.height or 1024, steps=request.steps or 30,
            cfg_scale=request.cfg_scale, sampler=request.sampler or "", scheduler=request.scheduler or "")
        dialog = ModelWorkflowGuidanceDialog(service, machine, registry_snapshot=snapshot,
            registry=self.registry, jobs=self.jobs, protected_roots=self.protected_model_roots,
            current_generation_configuration=current, workflow_kind="generation", allow_workflow_switch=False,
            selection_context=context,main_model_path=self.local_model_path.text(),parent=self)
        if suggestion is not None and suggestion.catalog_choices:
            choices={item["catalog_record_id"] for item in suggestion.catalog_choices}
            for index in reversed(range(dialog.target.count())):
                if dialog.target.itemData(index) not in choices:
                    dialog.target.removeItem(index)
        dialog.goal.setText(self.plan.goal)
        if request.model_hash and "sha256:" + request.model_hash in snapshot["resources"]:
            dialog.base_model_id.setText("sha256:" + request.model_hash)
        if dialog.exec() == QDialog.Accepted:
            self.apply_model_guidance(dialog.plan, dialog.selected_generation_configuration())

    def apply_model_guidance(self, guidance, configuration, *, expected_registry_revision=None) -> bool:
        """Apply one reviewed preview to a new request revision, preserving source evidence."""
        if configuration is None or guidance is None or guidance.request.workflow_kind != "generation":
            return False
        from .generation_workflow import _registered_resource_with_label, _updated_resource_labels
        resources = [item.to_dict() for item in self.plan.request.resources]
        changes = configuration.request_changes()
        try:
            if (expected_registry_revision is not None and
                    (self.registry is None or self.registry.snapshot()[1] != expected_registry_revision)):
                raise ValueError("Registered models changed. Review a fresh recommendation before applying it.")
            chosen = [(guidance.request.base_model_id, "checkpoint")] if guidance.request.base_model_id else []
            requirements = {item.slot: item for item in guidance.companion_requirements}
            for binding in guidance.request.resource_bindings:
                requirement = requirements.get(binding.slot)
                if requirement is None:
                    raise ValueError("A companion requirement changed; reopen Model Guidance.")
                chosen.append((binding.resource_id, requirement.role))
            if chosen and self.registry is None:
                raise ValueError("Open this planner from Models or Dataset to apply exact registered model choices.")
            selected_labels = []
            for identity, role in chosen:
                resource, label = _registered_resource_with_label(self.registry, identity, selected_role=role)
                resources = [item for item in resources if not (
                    item.get("role") == role and
                    (role in {"checkpoint", "vae", "controlnet"} or item.get("id") == resource.id))]
                resources.append(resource.to_dict())
                selected_labels.append((resource.to_dict(), label))
                if role == "checkpoint":
                    changes.update(model="", model_hash=resource.hashes["sha256"])
            changes["resources"] = resources
            context = _updated_resource_labels(self.plan.target_context, resources)
            for resource, label in selected_labels:
                context = _updated_resource_labels(context, resources, selected=resource, label=label)
            context["model_guidance"] = {"recipe_id": configuration.recipe_id,
                "recipe_version": configuration.recipe_version, "family": configuration.target_model_family,
                "model_version": configuration.target_model_version,
                "record_id": getattr(guidance, "record_id", ""),
                "source_urls": list(getattr(guidance, "source_urls", ())),
                "authority": "reviewed_configuration_advice", "execution_readiness": "preflight_required",
                "resource_bindings": [{"slot": item.slot, "resource_id": item.resource_id} for item in guidance.request.resource_bindings]}
            if (expected_registry_revision is not None and
                    self.registry.snapshot()[1] != expected_registry_revision):
                raise ValueError("Registered models changed. Review a fresh recommendation before applying it.")
            self.plan = self.plan.revise({"request": changes, "target_context": context, "goal": guidance.request.goal}, source="model_guidance",
                consequence="Applied reviewed model-specific generation settings and selected resources; exact adapter preflight remains required")
        except (ValueError, RuntimeError, GenerationPlanError) as exc:
            self.status.setText(str(exc)); return False
        self.binding = self.prepared = self.receipt = None
        self.local_model_name.clear()
        if guidance.request.base_model_id:
            self.local_model_path.clear()
        self._generation += 1
        self._refresh_from_plan("Applied reviewed generation guidance. Review resource requirements and run the exact adapter preflight before submission.")
        return True

    def choose_registered_resource(self, requested_role: str = "checkpoint") -> None:
        if self.registry is None:
            self.status.setText("Open this planner from Models or Dataset to use the shared registry.")
            return
        from .model_selector_dialog import ModelSelectorDialog
        from .generation_workflow import _registered_resource_with_label
        selections = tuple({"resource_id": item.id, "role": item.role,
            "family": item.compatibility[0] if len(item.compatibility) == 1 else ""}
            for item in self.plan.request.resources if item.id
            and not (requested_role in {"checkpoint", "vae"} and item.role == requested_role))
        dialog = ModelSelectorDialog(self.registry, self.jobs, parent=self,
            role=requested_role,
            main_model_resource_id=("sha256:" + self.plan.request.model_hash.removeprefix("sha256:")
                                    if self.plan.request.model_hash else ""),
            settings=self.settings,
            protected_roots=self.protected_model_roots,
            selected_resources=selections, required_roles=("checkpoint",),
            current_choice_label=self._chosen_model_summary())
        if dialog.exec() != QDialog.Accepted:
            return
        identity, path = dialog.selected_resource_id, dialog.selected_path
        role, family = dialog.selected_role, dialog.selected_family
        self._run("selected-resource", lambda: _registered_resource_with_label(self.registry, identity,
            selected_role=role, selected_family=family),
            lambda result: self._apply_selected_resource(result[0], path, display_label=result[1]))

    def _apply_selected_resource(self, resource, path, *, display_label="") -> None:
        from .generation_workflow import _updated_resource_labels
        if resource.role == "control_model":
            resource.role = "controlnet"
        digest = resource.hashes["sha256"]
        if resource.role == "lora" and not resource.weights:
            resource.weights = {"model": 1.0, "text_encoder": 1.0}
        if resource.role != "checkpoint" and any(
            item.role == resource.role and item.hashes.get("sha256") == digest
            for item in self.plan.request.resources
        ):
            self.status.setText("This resource is already in the request. Select it in Review to remove it first if you want a different choice.")
            return
        resources = [item.to_dict() for item in self.plan.request.resources
            if not (resource.role in {"checkpoint", "vae", "controlnet"} and item.role == resource.role)]
        resources.append(resource.to_dict())
        changes = {"resources": resources,
            "target_context": _updated_resource_labels(self.plan.target_context, resources,
                selected=resource.to_dict(), label=display_label)}
        if resource.role == "checkpoint":
            changes["model_hash"] = resource.hashes["sha256"]
            # A registry display name is not the generator's executable name.
            changes["model"] = ""
            self.local_model_path.setText(str(path))
            self.local_model_name.clear()
        self.plan = self.plan.revise(changes, source="registered_model",
            consequence="Explicit resource selection; adapter support must be checked")
        if resource.role == "checkpoint":
            self._invalidate_output_plan()
        self.binding = None
        self.prepared = None
        self.receipt = None
        self._generation += 1
        self._refresh_from_plan("Exact resource selected. Review all resources and companion requirements before preparing; selection starts no job.")
        if resource.role == "controlnet" and self.image_task.currentData() == "control_image":
            self._image_task_changed()
        self.model_setup_status.setText("Model selection changed. Existing prompt and sampling choices are retained; refresh model details to review companion requirements.")
        while self.model_trigger_grid.count():
            item = self.model_trigger_grid.takeAt(0)
            if item.widget() is not None:
                item.widget().deleteLater()

    def remove_selected_resource(self) -> None:
        from .generation_workflow import _updated_resource_labels
        if not self.remove_resource_button.isEnabled():
            return
        item = self.resource_tree.currentItem()
        index = item.data(0, Qt.UserRole) if item is not None else None
        if type(index) is not int or not 0 <= index < len(self.plan.request.resources):
            return
        removed = self.plan.request.resources[index]
        resources = [resource.to_dict() for position, resource in enumerate(self.plan.request.resources)
            if position != index]
        changes = {"resources": resources,
            "target_context": _updated_resource_labels(self.plan.target_context, resources)}
        if removed.role == "checkpoint":
            changes.update(model="", model_hash="")
        try:
            revised = self.plan.revise(changes, source="resource_removed",
                consequence="Explicitly removed one selected resource; adapter readiness must be checked again")
        except (ValueError, RuntimeError, GenerationPlanError) as exc:
            self.status.setText(str(exc))
            return
        self.plan = revised
        if removed.role == "checkpoint":
            for field in (self.local_model_name, self.local_model_path):
                field.blockSignals(True)
                field.clear()
                field.blockSignals(False)
        self.binding = self.prepared = self.receipt = None
        self._generation += 1
        self._refresh_from_plan("Selected resource removed. Review the remaining resources and prepare the request again.")

    def _build_review_tab(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        note = QLabel("The source stays unchanged. This tab shows every deliberate difference and any translation loss before submission.")
        note.setWordWrap(True); layout.addWidget(note)
        splitter = QSplitter(Qt.Vertical)
        self.resource_tree = QTreeWidget()
        self.resource_tree.setHeaderLabels(["Selected role", "Resource", "SHA-256 / evidence reference"])
        self.resource_tree.setColumnWidth(0, 130)
        self.resource_tree.setColumnWidth(1, 250)
        self.resource_tree.setRootIsDecorated(False)
        self.resource_tree.setAccessibleName("Complete selected resource set")
        resource_panel = QWidget()
        resource_layout = QVBoxLayout(resource_panel)
        resource_layout.setContentsMargins(0, 0, 0, 0)
        resource_layout.addWidget(self.resource_tree, 1)
        resource_actions = QHBoxLayout()
        resource_actions.addStretch(1)
        self.remove_resource_button = QPushButton("Remove Selected Resource")
        self.remove_resource_button.clicked.connect(self.remove_selected_resource)
        resource_actions.addWidget(self.remove_resource_button)
        resource_layout.addLayout(resource_actions)
        splitter.addWidget(resource_panel)
        self.resource_tree.itemSelectionChanged.connect(self._update_actions)
        self.resource_tree.itemSelectionChanged.connect(self._show_component_settings)
        component_form = QFormLayout()
        self.lora_model_weight = QDoubleSpinBox()
        self.lora_text_weight = QDoubleSpinBox()
        for weight in (self.lora_model_weight, self.lora_text_weight):
            weight.setRange(-10, 10); weight.setDecimals(3); weight.setSingleStep(.05)
        self.encoder_slot = QComboBox()
        self.encoder_slot.addItem("Choose text encoder purpose", "")
        for label, value in (("CLIP-L", "cliplmodel"), ("T5 / UMT5", "t5xxlmodel"),
                             ("Qwen (Anima)", "qwenmodel"), ("Mistral (Flux.2 Dev)", "mistralmodel")):
            self.encoder_slot.addItem(label, value)
        component_form.addRow("LoRA image weight", self.lora_model_weight)
        component_form.addRow("LoRA text weight", self.lora_text_weight)
        component_form.addRow("Text encoder purpose", self.encoder_slot)
        self.apply_component_button = QPushButton("Apply Selected Component Settings")
        self.apply_component_button.clicked.connect(self._apply_component_settings)
        component_form.addRow(self.apply_component_button)
        resource_layout.addLayout(component_form)
        self.diff_tree = QTreeWidget(); self.diff_tree.setHeaderLabels(["Changed setting", "Before", "After", "Reason"]); self.diff_tree.setRootIsDecorated(False)
        self.diff_tree.setAccessibleName("Exact request changes")
        splitter.addWidget(self.diff_tree)
        self.readiness = QTextEdit(); self.readiness.setReadOnly(True); self.readiness.setAccessibleName("Readiness and translation loss")
        splitter.addWidget(self.readiness); splitter.setSizes([190, 210, 160]); layout.addWidget(splitter, 1)
        self.safety_ack = QCheckBox("I reviewed the prompts, inputs, output destination, and content-safety consequences.")
        self.safety_ack.toggled.connect(self._safety_changed); layout.addWidget(self.safety_ack)
        return page

    def _show_component_settings(self):
        item = self.resource_tree.currentItem()
        index = item.data(0, Qt.UserRole) if item is not None else None
        resource = self.plan.request.resources[index] if type(index) is int and 0 <= index < len(self.plan.request.resources) else None
        lora = resource is not None and resource.role == "lora"
        encoder = resource is not None and resource.role == "text_encoder"
        if not hasattr(self, "lora_model_weight"):
            return
        self.lora_model_weight.setEnabled(lora and not self._pending)
        self.lora_text_weight.setEnabled(lora and not self._pending)
        self.encoder_slot.setEnabled(encoder and not self._pending)
        self.apply_component_button.setEnabled((lora or encoder) and not self._pending and not self._execution_active())
        if lora:
            self.lora_model_weight.setValue(float(resource.weights.get("model", 1)))
            self.lora_text_weight.setValue(float(resource.weights.get("text_encoder", 1)))
        if encoder:
            self.encoder_slot.setCurrentIndex(max(0, self.encoder_slot.findData(resource.provenance.get("swarm_parameter", ""))))

    def _apply_component_settings(self):
        if self._pending or self._execution_active():
            return
        item = self.resource_tree.currentItem()
        index = item.data(0, Qt.UserRole) if item is not None else None
        resources = [resource.to_dict() for resource in self.plan.request.resources]
        if type(index) is not int or not 0 <= index < len(resources):
            return
        resource = resources[index]
        if resource["role"] == "lora":
            resource["weights"] = {"model": self.lora_model_weight.value(), "text_encoder": self.lora_text_weight.value()}
        elif resource["role"] == "text_encoder":
            if not self.encoder_slot.currentData():
                self.status.setText("Choose the purpose of this text encoder before applying.")
                return
            resource["provenance"]["swarm_parameter"] = self.encoder_slot.currentData()
        else:
            return
        self.plan = self.plan.revise({"resources": resources}, source="component_settings",
            consequence="Explicit component weight or encoder purpose changed")
        self.binding = self.prepared = self.receipt = None
        self._generation += 1
        self._refresh_from_plan("Component settings saved. Bind the selected models and prepare again.")

    @staticmethod
    def _scroll(page: QWidget) -> QScrollArea:
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(page); return scroll

    def _build_advanced_tab(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        self.advanced_tasks = QTabWidget()
        layout.addWidget(self.advanced_tasks, 1)
        fragments_page = QWidget(); fragments_layout = QVBoxLayout(fragments_page)
        fragments_intro = QLabel(
            "Choose reviewed image or Outfit text for the Image prompt. Checking a detail does not change the request until you apply it.")
        fragments_intro.setWordWrap(True); fragments_layout.addWidget(fragments_intro)
        self.outfit_browser_widget = QWidget()
        outfit_browser = QHBoxLayout(self.outfit_browser_widget)
        outfit_browser.setContentsMargins(0, 0, 0, 0)
        self.wearer_label = QLabel("Browse Outfits For")
        self.wearer_choice = QComboBox(); self.wearer_choice.addItem("Choose a character", "")
        for character in self.characters:
            identity = str(character.get("id") or character.get("character_id") or "")
            self.wearer_choice.addItem(str(character.get("name") or character.get("label") or identity), identity)
        self.outfit_scope_label = QLabel("Show")
        self.outfit_scope_choice = QComboBox()
        for label, value in (("Associated with this character", "associated"),
                             ("Associated with other characters", "other"),
                             ("Generic outfits", "generic")):
            self.outfit_scope_choice.addItem(label, value)
        if not self.characters:
            self.outfit_scope_choice.setCurrentIndex(self.outfit_scope_choice.findData("generic"))
        self.wearer_choice.currentIndexChanged.connect(self._refresh_outfit_fragments)
        self.outfit_scope_choice.currentIndexChanged.connect(self._refresh_outfit_fragments)
        for widget in (self.wearer_label, self.wearer_choice, self.outfit_scope_label, self.outfit_scope_choice):
            outfit_browser.addWidget(widget)
        outfit_browser.addStretch(1)
        fragments_layout.addWidget(self.outfit_browser_widget)
        self.outfit_scope_note = QLabel()
        self.outfit_scope_note.setWordWrap(True)
        fragments_layout.addWidget(self.outfit_scope_note)
        self.fragment_tree = QTreeWidget(); self.fragment_tree.setHeaderLabels(["Include", "Fragment", "Kind"]); self.fragment_tree.setAccessibleName("Knowledge fragments")
        self.fragment_tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.fragment_tree.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.fragment_tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.fragment_tree.itemChanged.connect(self._fragments_changed)
        fragments_layout.addWidget(self.fragment_tree, 1)
        self.fragment_empty_note = QLabel()
        self.fragment_empty_note.setWordWrap(True)
        self.fragment_empty_note.hide()
        fragments_layout.addWidget(self.fragment_empty_note)
        self.fragment_actions_widget = QWidget()
        fragment_actions = QHBoxLayout(self.fragment_actions_widget)
        fragment_actions.setContentsMargins(0, 0, 0, 0)
        self.apply_fragments_button = QPushButton("Apply Selected Fragments")
        self.apply_fragments_button.clicked.connect(self.apply_selected_fragments)
        fragment_actions.addWidget(self.apply_fragments_button); fragment_actions.addStretch(1)
        fragments_layout.addWidget(self.fragment_actions_widget)
        self.advanced_tasks.addTab(fragments_page, "Reusable Prompt Text")

        json_page = QWidget(); json_layout = QVBoxLayout(json_page)
        intro = QLabel("Edit the complete generator-neutral request, including stages, resources and unknown fields. This replaces the current request only after review.")
        intro.setWordWrap(True); json_layout.addWidget(intro)
        self.advanced_json = QTextEdit(); self.advanced_json.setAccessibleName("Full neutral generation request")
        self.advanced_json.textChanged.connect(self._advanced_json_changed)
        json_layout.addWidget(self.advanced_json, 1)
        self.advanced_pending_note = QLabel()
        self.advanced_pending_note.setWordWrap(True)
        self.advanced_pending_note.setTextFormat(Qt.PlainText)
        self.advanced_pending_note.hide()
        json_layout.addWidget(self.advanced_pending_note)
        advanced_actions = QHBoxLayout()
        advanced_actions.addStretch(1)
        self.discard_advanced_button = QPushButton("Discard Pending JSON…")
        self.discard_advanced_button.clicked.connect(self.discard_advanced_json)
        self.discard_advanced_button.hide()
        advanced_actions.addWidget(self.discard_advanced_button)
        self.apply_advanced_button = QPushButton("Apply Validated JSON")
        self.apply_advanced_button.clicked.connect(self.apply_advanced_json)
        advanced_actions.addWidget(self.apply_advanced_button)
        json_layout.addLayout(advanced_actions)
        self.advanced_tasks.addTab(json_page, "Full Request JSON")
        return page

    def _build_connection_tab(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        note = QLabel("Check your generator, selected model and request before generating. Checks do not start generation. Manage the generator in Setup.")
        note.setWordWrap(True); layout.addWidget(note)
        self.alternatives_return_button = QPushButton("Return to Saved Alternatives")
        self.alternatives_return_button.clicked.connect(self.return_to_alternatives)
        self.alternatives_return_button.hide()
        layout.addWidget(self.alternatives_return_button, 0, Qt.AlignLeft)
        self.setup_button = QPushButton("Save Plan and Open Setup…")
        self.setup_button.setAutoDefault(False)
        self.setup_button.setToolTip("Save this request, then open local generation setup. Return here to check and prepare it again.")
        self.setup_button.clicked.connect(self._choose_setup)
        self.setup_button.setVisible(self.setup_available)
        layout.addWidget(self.setup_button, 0, Qt.AlignRight)
        manual = QWidget(); form = QFormLayout(manual)
        self.endpoint = QLineEdit(self.initial_endpoint); self.endpoint.setAccessibleName("Local generator endpoint")
        self.endpoint.setReadOnly(self.setup_available)
        self.local_model_name = QLineEdit(); self.local_model_name.setPlaceholderText("Exact model name reported by the generator")
        self.local_model_path = QLineEdit(); self.local_model_path.setPlaceholderText("User-chosen local .safetensors file")
        form.addRow("Local endpoint", self.endpoint); form.addRow("Generator model name", self.local_model_name); form.addRow("Local safe tensor file", self.local_model_path)
        self.endpoint.textChanged.connect(self._connection_changed)
        for field in (self.local_model_name, self.local_model_path):
            field.textChanged.connect(self._binding_changed)
        self.manual_connection = QCheckBox("Show technical connection and model details")
        self.manual_connection.toggled.connect(manual.setVisible)
        layout.addWidget(self.manual_connection); layout.addWidget(manual); manual.hide()
        row = QHBoxLayout(); self.check_button = QPushButton("Check Generator"); self.bind_button = QPushButton("Check Model"); self.prepare_button = QPushButton("Check Reviewed Request")
        for button, callback in ((self.check_button, self.check_connection), (self.bind_button, self._check_model_readiness), (self.prepare_button, self.prepare_request)):
            button.clicked.connect(callback); row.addWidget(button)
        row.addStretch(1); layout.addLayout(row)
        self.find_model_button = QPushButton("Find Selected Models in Generator")
        self.find_model_button.clicked.connect(self.find_selected_model)
        layout.addWidget(self.find_model_button)
        self.find_model_button.hide()
        self.memory_status = QLabel("GPU memory fit is unproven. Prepare a request to observe current free memory; it is checked again when queued work starts.")
        self.memory_status.setWordWrap(True); self.memory_status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.memory_status)
        self.connection_detail = QTextEdit(); self.connection_detail.setReadOnly(True); self.connection_detail.setAccessibleName("Local adapter capability probe")
        layout.addWidget(self.connection_detail, 1)
        return page

    def _build_results_tab(self) -> QWidget:
        page = QWidget(); layout = QVBoxLayout(page)
        self.results_experiment_button = QPushButton("Alternatives and Compare…")
        self.results_experiment_button.clicked.connect(self.open_experiment)
        self.results_status = QLabel("Generate an image or open recent jobs. Verified results can be finished, remixed or sent to Ingest.")
        self.results_status.setWordWrap(True); self.results_status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.results_status)
        self.cancel_button = QPushButton("Cancel Submitted Work"); self.cancel_button.clicked.connect(self.cancel_submission); self.cancel_button.setEnabled(False)
        layout.addWidget(self.cancel_button, 0, Qt.AlignLeft)
        self.memory_recovery_help = QLabel()
        self.memory_recovery_help.setWordWrap(True)
        self.memory_recovery_help.setTextFormat(Qt.PlainText)
        self.memory_recovery_help.hide()
        self.memory_recovery_button = QPushButton("Review Memory Recovery")
        self.memory_recovery_button.clicked.connect(self.review_memory_recovery)
        self.memory_recovery_button.hide()
        layout.addWidget(self.memory_recovery_help)
        layout.addWidget(self.memory_recovery_button, 0, Qt.AlignLeft)
        outcome = QGroupBox("Review a Completed Outcome")
        outcome_form = QFormLayout(outcome)
        self.outcome_kind = QComboBox()
        self.outcome_kind.addItem("Useful", "useful"); self.outcome_kind.addItem("Failed", "failed"); self.outcome_kind.addItem("Mixed", "mixed")
        self.outcome_goal = QLineEdit(); self.outcome_goal.setPlaceholderText("What this result says about the selected goal")
        self.outcome_reasons = QTextEdit(); self.outcome_reasons.setFixedHeight(54); self.outcome_reasons.setPlaceholderText("Specific reasons for this outcome")
        outcome_form.addRow("Outcome", self.outcome_kind); outcome_form.addRow("Goal details", self.outcome_goal); outcome_form.addRow("Reasons", self.outcome_reasons)
        self.outcome_toggle = QCheckBox("Record feedback after reviewing this result in Ingest")
        self.outcome_group = outcome
        self.outcome_toggle.toggled.connect(self._update_actions)
        outcome.hide()
        outcome_actions = QVBoxLayout()
        self.record_outcome_button = QPushButton("Record Outcome Review")
        self.record_outcome_button.clicked.connect(self.record_outcome)
        self.ingest_button = QPushButton("Send Verified Results to Ingest")
        self.ingest_button.clicked.connect(self.send_verified_to_ingest); self.ingest_button.setEnabled(False)
        self.ingest_folder_recovery = QWidget()
        folder_recovery_row = QHBoxLayout(self.ingest_folder_recovery)
        folder_recovery_row.setContentsMargins(0, 0, 0, 0)
        self.ingest_folder_recovery_note = QLabel(
            "No results were imported. Save an Incoming Images folder, then send this verified result again.")
        self.ingest_folder_recovery_note.setWordWrap(True)
        self.ingest_folder_button = QPushButton("Choose Ingest Folder…")
        self.ingest_folder_button.clicked.connect(lambda: self.ingest_folder_requested.emit())
        folder_recovery_row.addWidget(self.ingest_folder_button)
        folder_recovery_row.addWidget(self.ingest_folder_recovery_note, 1)
        self.ingest_folder_recovery.hide()
        self.preview_result_button = QPushButton("Preview Verified Result")
        self.preview_result_button.clicked.connect(self.preview_verified_result)
        self.result_preview = ProtectedImageView(settings=self.settings)
        self.result_preview.setMinimumHeight(220)
        self.result_preview.hide()
        self.choose_ingested_button = QPushButton("Choose Ingested Result…")
        self.choose_ingested_button.clicked.connect(self._choose_ingested_result)
        self.history_choice = QComboBox(); self.history_button = QPushButton("Refresh Recent Jobs")
        self.history_choice.activated.connect(self._select_history)
        self.recover_button = QPushButton("Open or Recover Selected Job")
        self.history_button.clicked.connect(self.load_history); self.recover_button.clicked.connect(self.recover_selected)
        history_row = QHBoxLayout(); history_row.addWidget(self.history_button); history_row.addWidget(self.history_choice, 1); history_row.addWidget(self.recover_button)
        self.opened_result_status = QLabel()
        self.opened_result_status.setWordWrap(True)
        self.opened_result_status.setTextFormat(Qt.PlainText)
        handoff_row = QHBoxLayout(); handoff_row.addWidget(self.preview_result_button); handoff_row.addWidget(self.ingest_button); handoff_row.addStretch(1)
        outcome_form.addRow(self.choose_ingested_button, self.record_outcome_button)
        self.finish_result_button = QPushButton("Finish Result…")
        self.finish_result_button.clicked.connect(self.finish_result)
        handoff_row.insertWidget(1, self.finish_result_button)
        outcome_actions.addLayout(history_row)
        outcome_actions.addWidget(self.opened_result_status)
        outcome_actions.addWidget(self.result_preview)
        outcome_actions.addLayout(handoff_row)
        outcome_actions.addWidget(self.ingest_folder_recovery)
        iteration_row = QHBoxLayout()
        self.result_action = QComboBox()
        for label, action in (("Repeat with same seed", "same_seed"), ("Try a new seed", "new_seed"),
                              ("Remix this result", "remix"), ("Generatively upscale this result", "upscale")):
            self.result_action.addItem(label, action)
        self.result_action_button = QPushButton("Create Draft from Verified Result")
        self.result_action_button.clicked.connect(self.create_result_draft)
        iteration_row.addWidget(self.result_action)
        iteration_row.addWidget(self.result_action_button)
        iteration_row.addStretch(1)
        outcome_actions.addLayout(iteration_row)
        layout.addLayout(outcome_actions)
        layout.addWidget(self.outcome_toggle); layout.addWidget(outcome)
        layout.addWidget(self.results_experiment_button, 0, Qt.AlignLeft)
        layout.addStretch(1)
        return page

    def _check_model_readiness(self):
        if self.registry is not None and self.plan.request.model_hash:
            self.find_selected_model()
        else:
            self.manual_connection.setChecked(True)
            if self.local_model_name.text().strip() and self.local_model_path.text().strip():
                self.bind_local_model()
            else:
                self.status.setText("Choose a registered Main Model on Request, or enter the exact local model details shown here.")
                self.local_model_name.setFocus(Qt.OtherFocusReason)

    def _request_field_values(self) -> dict[str, Any]:
        seed_text = self.seed.text().strip(); cfg_text = self.cfg_scale.text().strip()
        return {
            "positive_prompt": self.positive.toPlainText(), "negative_prompt": self.negative.toPlainText(),
            "model": self.model.text().strip(), "model_hash": self.model_hash.text().strip(),
            "width": self.size_width.value() or None, "height": self.size_height.value() or None,
            "seed": int(seed_text) if seed_text else None, "steps": self.steps.value() or None,
            "cfg_scale": float(cfg_text) if cfg_text else None,
            "sampler": self.sampler.text().strip(), "scheduler": self.scheduler.text().strip(),
        }

    def _edit(self, *_args) -> None:
        self._refresh_request_details_summary()
        try:
            displayed = self._request_field_values()
        except ValueError:
            self.prepared = None
            self.status.setText("Seed and CFG scale need complete numbers. This edit was not applied; the previous valid draft remains saved.")
            self._update_actions(); return
        # Controls may display a fallback or clamp an advanced value. Only an
        # explicit field edit may replace its exact value in the neutral plan.
        request = {key: value for key, value in displayed.items()
                   if value != self._displayed_request[key]}
        prior_model_and_base = (self.plan.request.model_hash, self.plan.request.width, self.plan.request.height)
        try:
            plan = self.plan
            if "seed" in request:
                plan = plan.choose_seed("specific", request.pop("seed"))
            self.plan = plan.revise({"intent": self.intent.currentData(), "goal": self.goal.text().strip(), "request": request}, source="user", consequence="Request fields changed")
            if (self.plan.request.model_hash, self.plan.request.width, self.plan.request.height) != prior_model_and_base:
                self._invalidate_output_plan()
        except (GenerationPlanError, ValueError) as exc:
            self.prepared = None
            self.status.setText(f"{exc} Correct the visible Request value before checking or using this draft.")
            self._update_actions(); return
        self._displayed_request = displayed
        self._refresh_seed_controls()
        self.prepared = None; self.receipt = None; self._refresh_review("Inputs changed. Review and prepare this revised request before submitting.", fragments=False)

    def _refresh_request_details_summary(self) -> None:
        entered = int(bool(self.goal.text().strip())) + int(bool(self.negative.toPlainText().strip()))
        label = "Optional prompt details (planning note and things to avoid)"
        self.request_details_toggle.setText(f"{label} · {entered} entered" if entered else label)

    def _fragments_changed(self, item, column: int) -> None:
        if column != 0 or getattr(self, "_refreshing", False): return
        fragment = _mapping(item.data(0, Qt.UserRole))
        identity = str(fragment.get("id") or "")
        if not identity: return
        checked = item.checkState(0) == Qt.Checked
        known = {str(value.get("id")) for value in self.plan.knowledge_fragments}
        if identity in known:
            self.plan = self.plan.revise({"knowledge_selection": {identity: checked}}, source="user",
                consequence="Knowledge fragment selection changed")
        elif checked:
            self.plan = self.plan.revise({"knowledge_fragments": [*self.plan.knowledge_fragments,
                {**fragment, "included": True}]}, source="user", consequence="Knowledge fragment selection changed")
        else:
            return
        self.prepared = None; self._refresh_review("Knowledge selection changed. Prepare again before submitting.", fragments=False)

    def apply_selected_fragments(self) -> None:
        try:
            revised = self.plan.apply_fragments(role="positive")
        except ValueError as exc:
            self.status.setText(str(exc)); return
        if revised is self.plan:
            self.status.setText("No new reviewed fragment text to add. Select a detail that is not already in the Image prompt.")
            return
        self.plan = revised
        self.prepared = None
        self._refresh_from_plan("Selected reviewed fragments were appended to the positive prompt. Review the exact diff.")

    def _refresh_outfit_fragments(self, *_args) -> None:
        self._refresh_review(fragments=True)

    @staticmethod
    def _outfit_candidate(raw: dict[str, Any]) -> dict[str, Any]:
        associations = raw.get("associated_character_ids", raw.get("character_ids", ()))
        return _fragment({**raw,
            "id": raw.get("id") or raw.get("outfit_id"),
            "text": raw.get("text") or raw.get("prompt_fragment") or raw.get("name") or raw.get("label") or "",
            "group": raw.get("planning_group") or ("character_associated_outfit" if associations else "generic_outfit"),
            "reviewed": bool(raw.get("reviewed", False)), "included": False,
            "source": raw.get("source") or "outfit_catalog",
        })

    def _visible_fragments(self) -> tuple[list[dict[str, Any]], str]:
        plan_fragments = {str(value.get("id")): value for value in self.plan.knowledge_fragments}
        if not self.outfits:
            return list(self.plan.knowledge_fragments), ""
        character_id = str(self.wearer_choice.currentData() or "")
        if character_id not in self._outfit_scope_cache:
            try:
                if len(self._outfit_scope_cache) >= 4:
                    self._outfit_scope_cache.clear()
                self._outfit_scope_cache[character_id] = outfit_association_scope(
                    character_id, self.characters, self.outfits)
            except GenerationPlanError as exc:
                return list(self.plan.knowledge_fragments), f"Outfit browsing is unavailable: {exc}"
        scope = self._outfit_scope_cache[character_id]
        selected_group = str(self.outfit_scope_choice.currentData() or "associated")
        visible_catalog = scope["groups"].get(selected_group, [])
        catalog_ids = {str(value.get("id")) for value in self.outfits}
        visible_ids = {str(value.get("id")) for value in visible_catalog}
        fragments = [value for value in self.plan.knowledge_fragments
                     if str(value.get("id")) not in catalog_ids]
        fragments.extend(plan_fragments.get(str(value.get("id"))) or self._outfit_candidate(value)
                         for value in visible_catalog)
        hidden_checked = sum(bool(value.get("included")) for identity, value in plan_fragments.items()
                             if identity in catalog_ids and identity not in visible_ids)
        notes = ["Browsing does not change your request. Checked reviewed details are applied only when you choose Apply."]
        if self.characters and not character_id and selected_group == "associated":
            notes.append("Choose a character to see associated outfits, or use Show for other or generic outfits.")
        if hidden_checked:
            notes.append(f"{hidden_checked} checked outfit detail(s) are in other browse groups and will also be applied.")
        if scope["unavailable_associations"]:
            notes.append("Some outfits reference characters unavailable in this catalog; review their association before reuse.")
        return fragments, " ".join(notes)

    def _safety_changed(self, checked: bool) -> None:
        policy = {"policy_version": 1, "state": "allowed" if checked else "review_required", "reviewed": bool(checked)}
        self.plan = self.plan.revise({"safety_policy": policy}, source="user", consequence="Content-safety acknowledgement changed")
        self.prepared = None
        self._refresh_review("Content-safety acknowledgement changed. Prepare again before submitting.")

    def _select_output_preset(self, *_args) -> None:
        if getattr(self, "_refreshing", False):
            return
        size = self.output_preset.currentData()
        if isinstance(size, (tuple, list)) and len(size) == 2:
            for field, value in ((self.final_width, size[0]), (self.final_height, size[1])):
                field.blockSignals(True); field.setValue(value); field.blockSignals(False)
            self._output_goal_changed()
        elif size is None:
            for field in (self.final_width, self.final_height):
                field.blockSignals(True); field.setValue(0); field.blockSignals(False)
            self._output_goal_changed()
        elif size == "custom":
            self.output_customization.setChecked(True)
            self.final_width.setFocus(Qt.OtherFocusReason)

    def _sync_output_selectors(self) -> None:
        """Derive selector state from pixels; never traverse a registry on edits."""
        width, height = self.final_width.value(), self.final_height.value()
        divisor = gcd(width, height) if width and height else 0
        ratio = (width // divisor, height // divisor) if divisor else None
        self.output_aspect.blockSignals(True)
        self.output_aspect.setCurrentIndex(next((index for index in range(1, self.output_aspect.count())
            if tuple(self.output_aspect.itemData(index)) == ratio), 0))
        self.output_aspect.blockSignals(False)
        evidence = self._local_size_evidence()
        choices = image_size_choices(ratio, model_evidence=evidence)
        self.output_preset.blockSignals(True)
        self.output_preset.clear(); self.output_preset.addItem("Choose a final size", None)
        if choices:
            for label, size in choices:
                self.output_preset.addItem(label, size)
        else:
            # These are target pixels, not a model recommendation. The explicit
            # preview resolves family evidence and refreshes the choices.
            for label, size in (("Square · 1024 × 1024", (1024, 1024)),
                                ("Portrait · 832 × 1216", (832, 1216)),
                                ("Landscape · 1216 × 832", (1216, 832)),
                                ("Widescreen · 1344 × 768", (1344, 768))):
                self.output_preset.addItem(label, size)
        self.output_preset.addItem("Custom", "custom")
        index = next((index for index in range(1, self.output_preset.count() - 1)
            if tuple(self.output_preset.itemData(index)) == (width, height)), -1) if width and height else 0
        self.output_preset.setCurrentIndex(index if index >= 0 else self.output_preset.findData("custom"))
        self.output_preset.blockSignals(False)
        self.output_swap.setEnabled(bool(width and height) and not self._pending)

    def _select_output_aspect(self, *_args) -> None:
        if getattr(self, "_refreshing", False):
            return
        ratio = self.output_aspect.currentData()
        if not ratio:
            self.final_width.setFocus(Qt.OtherFocusReason)
            return
        evidence = self._local_size_evidence()
        choices = image_size_choices(tuple(ratio), model_evidence=evidence)
        if choices:
            width, height = choices[0][1]
        else:
            # An explicit aspect choice proposes target pixels only. No request
            # dimensions change until the user previews and applies guidance.
            factor = max(64, 1024 // max(ratio) // 64 * 64)
            width, height = ratio[0] * factor, ratio[1] * factor
        for field, value in ((self.final_width, width), (self.final_height, height)):
            field.blockSignals(True); field.setValue(value); field.blockSignals(False)
        self._output_goal_changed()

    def _local_size_evidence(self):
        """Use draft declarations for automatic advice, never IO or admission."""
        cached = getattr(self, "_output_choice_evidence", {})
        if cached and getattr(self, "_output_choice_model", None) == self.plan.request.model_hash:
            return {**cached, "evidence_kind": "declared_family", "verified": False}
        from core.model_organization import normalize_model_family
        checkpoints = [row for row in self.plan.request.resources if row.role == "checkpoint"]
        if len(checkpoints) != 1:
            return {}
        families = {normalize_model_family(value) for value in checkpoints[0].compatibility}
        families.discard("")
        return {"family": next(iter(families)), "evidence_kind": "declared_family"} if len(families) == 1 else {}

    def _swap_output_orientation(self) -> None:
        width, height = self.final_width.value(), self.final_height.value()
        if not width or not height:
            return
        self.final_width.blockSignals(True); self.final_height.blockSignals(True)
        self.final_width.setValue(height); self.final_height.setValue(width)
        self.final_width.blockSignals(False); self.final_height.blockSignals(False)
        self._output_goal_changed()

    def _output_goal_changed(self, *_args) -> None:
        if getattr(self, "_refreshing", False):
            return
        final = (self.final_width.value(), self.final_height.value())
        self._sync_output_selectors()
        if self.sender() in (self.final_width, self.final_height):
            selected = self.output_preset.currentData()
            if isinstance(selected, (tuple, list)) and tuple(selected) != final:
                self.output_preset.blockSignals(True)
                self.output_preset.setCurrentIndex(self.output_preset.findData("custom"))
                self.output_preset.blockSignals(False)
        context = self.plan.target_context
        previous = context.get("output_goal")
        context.pop("output_plan", None)
        if min(final) >= 64:
            goal = {"schema_id": "collectigen.output-goal.v1", "final_width": final[0],
                    "final_height": final[1], "detail_preference": self.output_preference.currentData(),
                    "preset": self.generation_goal_preset.currentData()}
            if goal == previous:
                return
            context["output_goal"] = goal
        elif previous is not None:
            context.pop("output_goal", None)
        else:
            self._refresh_output_summary(); return
        self.plan = self.plan.revise({"target_context": context}, source="output_goal",
            consequence="Desired final pixels and detail preference selected separately from generator base size")
        self._output_preview = self._output_preview_plan = None
        self.prepared = self.receipt = None
        self._refresh_review("Final output goal changed. Preview its base and any required follow-up stage.")

    def _invalidate_output_plan(self) -> None:
        self._output_preview = self._output_preview_plan = None
        self._output_choice_evidence = {}
        context = self.plan.target_context
        goal = context.get("output_goal")
        had_pipeline = context.pop("output_plan", None) is not None
        if isinstance(goal, dict) and "recommendation" in goal:
            context["output_goal"] = {key: value for key, value in goal.items() if key != "recommendation"}
        if had_pipeline or (isinstance(goal, dict) and "recommendation" in goal):
            self.plan = self.plan.revise({"target_context": context}, source="output_goal",
                consequence="Model or base changed; earlier output recommendation is stale")

    @staticmethod
    def _output_model_evidence(snapshot, plan, binding):
        from core.model_organization import normalize_model_family, configured_family_hints
        checkpoint = next((item for item in reversed(plan.request.resources)
                           if item.role == "checkpoint" and item.hashes.get("sha256")), None)
        sha = checkpoint.hashes["sha256"] if checkpoint else str(plan.request.model_hash or "").removeprefix("sha256:")
        resource_id = "sha256:" + sha if len(sha) == 64 else ""
        record = snapshot.get("resources", {}).get(resource_id, {}) if isinstance(snapshot, dict) else {}
        declarations = record.get("declarations", ()) if isinstance(record, dict) else ()
        families = {normalize_model_family(row.get("family", "")) for row in declarations
                    if isinstance(row, dict) and row.get("role") == "checkpoint" and row.get("family")}
        families.discard("")
        family, kind = "", "unknown"
        if len(families) == 1:
            family = next(iter(families))
            kind = ("verified_family" if binding and binding.get("resource_id") == resource_id
                    else "declared_family")
        elif not families and isinstance(record, dict):
            paths = tuple(str(row["path"]) for row in record.get("locations", ())
                          if isinstance(row, dict) and row.get("path"))
            hints = configured_family_hints(snapshot.get("workspace", {}), paths)
            if len(hints) == 1 and hints[0][0] == "Images":
                family, kind = hints[0][1], "folder_hint"
        return {"resource_id": resource_id, "family": family, "evidence_kind": kind,
                "verified": kind == "verified_family"}

    def preview_output_plan(self) -> None:
        goal = self.plan.target_context.get("output_goal")
        if not isinstance(goal, dict):
            self.status.setText("Choose both final pixel dimensions first."); return
        plan, binding, adapter = self.plan, deepcopy(self.binding), self.adapter
        def preview():
            snapshot = self.registry.snapshot()[0] if self.registry is not None else {}
            evidence = self._output_model_evidence(snapshot, plan, binding)
            # Observe an already-connected adapter; offline preview never starts one.
            observe = getattr(adapter, "observe_resources", None)
            machine = {"status": "unknown", "message": "Connect the local generator to check current free GPU memory."}
            if callable(observe) and getattr(adapter, "capabilities", None):
                try:
                    machine = observe()
                except Exception:
                    machine = {"status": "unknown", "message": "Current free GPU memory could not be read. Check the local connection and preview again."}
            result = plan_image_output_resolution(goal["final_width"], goal["final_height"],
                model_evidence=evidence, installed_stages={"resize": {"installed": True,
                    "supported": True, "adapter_id": "collectigen.lanczos.v1"}},
                detail_preference=goal["detail_preference"], modality="image", machine=machine,
                goal_preset=goal.get("preset", "final_output"),
                current_base=({"width": plan.request.width, "height": plan.request.height}
                    if plan.request.width and plan.request.height else None))
            result["resource_observation"] = machine
            result["selection_evidence"] = evidence
            if result.get("goal_preset") == "lower_memory" and binding:
                exact = self._exact_aspect_base(goal["final_width"], goal["final_height"],
                    result["base"]["width"] * result["base"]["height"])
                if exact:
                    result["base"] = {"width": exact[0], "height": exact[1]}
                    result["stages"][0].update(width=exact[0], height=exact[1])
                else:
                    result["can_execute"] = False
                    result["messages"].append("Choose a simpler final aspect ratio: no aligned smaller base preserves this exact aspect.")
            if result["detail_preference"] == "generative" and binding:
                exact = self._exact_aspect_base(goal["final_width"], goal["final_height"],
                    result["base"]["width"] * result["base"]["height"])
                if exact:
                    try:
                        from core.generation_refinement import prepare_refinement
                        candidate = {"schema_id": "collectigen.output-plan.v1", "kind": "generative_refine",
                            "base_width": exact[0], "base_height": exact[1], "refine_width": exact[0],
                            "refine_height": exact[1], "final_width": goal["final_width"],
                            "final_height": goal["final_height"], "strength": .25, "steps": 40, "reviewed": True}
                        checked = prepare_refinement(candidate, width=exact[0], height=exact[1], output_goal=goal,
                            binding=binding, capabilities=getattr(self.adapter, "capabilities", None) or {})
                        result["base"] = {"width": exact[0], "height": exact[1]}
                        result["stages"] = [{"kind": kind, "width": exact[0], "height": exact[1],
                            "available": True, "action": "", "resource_id": binding["resource_id"]}
                            for kind in ("base", "native_refine")]
                        if checked["resize"]:
                            result["stages"].append({"kind": "faithful_resize", "width": goal["final_width"],
                                "height": goal["final_height"], "available": True, "action": "", "resource_id": ""})
                        result["messages"].append("Add detail at the model-sized canvas, then enlarge faithfully; final-size diffusion is not requested.")
                        result["can_execute"] = (result["model_evidence"] in {"exact_model", "verified_family"}
                            and result["vram"]["assessment"] != "limited")
                    except ValueError as exc:
                        result["can_execute"] = False
                        result["application_blocker"] = (str(exc) + " Choose Resize without adding detail to keep this final size, "
                            "or select a model and connection with qualified refinement. Model-based AI upscaling is reviewed separately from the finished result.")
                        result["messages"].append(result["application_blocker"])
            elif result["detail_preference"] == "generative":
                result["application_blocker"] = "Check the main model in Readiness before reviewing refinement, or choose Resize without adding detail."
                result["messages"].append(result["application_blocker"])
            return result
        def done(result):
            if self.plan.to_dict() != plan.to_dict() or self.binding != binding or self.adapter is not adapter:
                self.status.setText("Model or output goal changed during preview. Preview again."); return
            self._output_preview = result
            self._output_preview_plan = self.plan.to_dict()
            self._output_choice_evidence = result["selection_evidence"]
            self._output_choice_model = self.plan.request.model_hash
            self._sync_output_selectors()
            self._refresh_output_summary()
            self.status.setText("Review the recommended generation size, finishing stages and memory estimate, then choose Use Recommended Settings.")
            self._update_actions()
        self._run("output-plan", preview, done)

    def apply_best_base(self) -> None:
        result = self._output_preview
        if result is None or self._output_preview_plan != self.plan.to_dict():
            self.status.setText("Preview the current model and final output goal again before applying a base."); return
        if result["model_evidence"] not in {"exact_model", "verified_family"}:
            self.status.setText("Bind and verify one exact registered model family before applying a recommended base. Folder and declaration hints are advisory only."); return
        if result["vram"]["assessment"] == "limited":
            self.status.setText("The reviewed memory evidence cannot support this plan. Free GPU memory or review a smaller request, then preview again."); return
        if result.get("application_blocker"):
            self.status.setText(result["application_blocker"]); return
        goal = {**self.plan.target_context["output_goal"],
                "final_width": result["final"]["width"], "final_height": result["final"]["height"],
                "detail_preference": result["detail_preference"]}
        final_width, final_height = goal["final_width"], goal["final_height"]
        base = result["base"]
        if goal["detail_preference"] in {"faithful", "generative"} and result.get("goal_preset") != "quick_preview":
            exact = self._exact_aspect_base(final_width, final_height,
                                            base["width"] * base["height"])
            if exact is None:
                self.status.setText("No aligned base preserves this final aspect within the reviewed family budget. Choose another final size."); return
            base = {"width": exact[0], "height": exact[1]}
        context = self.plan.target_context
        context["output_goal"] = {**goal, "recommendation": deepcopy(result)}
        context.pop("output_plan", None)
        if goal["detail_preference"] == "generative":
            proposed = {"schema_id": "collectigen.output-plan.v1", "kind": "generative_refine",
                "base_width": base["width"], "base_height": base["height"],
                "refine_width": base["width"], "refine_height": base["height"],
                "final_width": final_width, "final_height": final_height,
                "strength": .25, "steps": 40, "reviewed": True}
            try:
                from core.generation_refinement import prepare_refinement
                prepare_refinement(proposed, width=base["width"], height=base["height"],
                    output_goal=goal, binding=self.binding or {},
                    capabilities=getattr(self.adapter, "capabilities", None) or {})
                if any(item.kind == "mask" for stage in self.plan.request.stages for item in stage.inputs):
                    raise ValueError("A painted repair cannot also change protected pixels through refinement.")
            except ValueError as exc:
                self.status.setText(str(exc)); return
            context["output_plan"] = proposed
        if (goal["detail_preference"] == "faithful" and (base["width"], base["height"]) !=
                (final_width, final_height)):
            task = (str(self.plan.request.stages[0].extras.get("image_task") or "new")
                    if self.plan.request.stages else "new")
            if (final_width * final_height > 32_000_000 or
                    self.plan.intent not in {"new", "variation"} or
                    task not in {"new", "remix", "guided"} or
                    any(item.kind == "mask" for stage in self.plan.request.stages for item in stage.inputs) or
                    any(stage.extras.get("image_task") == "outpaint" for stage in self.plan.request.stages)):
                self.status.setText("Faithful final resize is limited to a bounded new or unmasked Remix request. Keep the goal saved; no follow-up was applied."); return
            context["output_plan"] = {"schema_id": "collectigen.output-plan.v1",
                "kind": "faithful_resize", "base_width": base["width"],
                "base_height": base["height"], "final_width": final_width,
                "final_height": final_height, "filter": "lanczos", "reviewed": True}
        self.plan = self.plan.revise({"request": {"width": base["width"], "height": base["height"]},
            "target_context": context}, source="output_goal",
            consequence="Applied reviewed model-sized base while retaining the desired final canvas")
        self._output_preview = self._output_preview_plan = None
        self.prepared = self.receipt = None
        message = ("Applied the reviewed base and faithful Lanczos final resize; review exact adapter preflight before submission."
                   if context.get("output_plan", {}).get("kind") == "faithful_resize" else
                   "Applied a second detail pass at the model-sized canvas, followed by faithful final resizing if needed. Review strength 0.25 and 40 refiner steps in Advanced before preparing."
                   if context.get("output_plan", {}).get("kind") == "generative_refine" else
                   "Applied the reviewed model-sized output without a finishing pass. Review and prepare before submitting.")
        self._refresh_from_plan(message)

    @staticmethod
    def _exact_aspect_base(final_width: int, final_height: int, pixel_budget: int):
        return exact_image_base(final_width, final_height, pixel_budget)

    def _output_followup_blocker(self) -> str:
        if bool(self.final_width.value()) != bool(self.final_height.value()):
            return "Complete both final width and final height, or clear both to use the model's base size."
        goal = self.plan.target_context.get("output_goal")
        if goal is None:
            return ""
        if (not isinstance(goal, dict) or goal.get("schema_id") != "collectigen.output-goal.v1"
                or type(goal.get("final_width")) is not int or type(goal.get("final_height")) is not int
                or not 64 <= goal["final_width"] <= 8192
                or not 64 <= goal["final_height"] <= 8192
                or goal["final_width"] * goal["final_height"] > 64_000_000
                or goal.get("detail_preference") not in {"faithful", "generative"}):
            return "The saved final-output goal is invalid; choose and review a supported size again."
        output_plan = self.plan.target_context.get("output_plan")
        if isinstance(output_plan, dict) and output_plan.get("kind") == "generative_refine":
            try:
                from core.generation_refinement import prepare_refinement
                prepare_refinement(output_plan, width=self.plan.request.width, height=self.plan.request.height,
                    output_goal=goal, binding=self.binding or {}, capabilities=getattr(self.adapter, "capabilities", None) or {})
                return ""
            except ValueError as exc:
                return str(exc)
        if (self.plan.request.width, self.plan.request.height) != (
                goal.get("final_width"), goal.get("final_height")):
            base_width, base_height = self.plan.request.width, self.plan.request.height
            exact = {"schema_id": "collectigen.output-plan.v1", "kind": "faithful_resize",
                "base_width": base_width, "base_height": base_height,
                "final_width": goal.get("final_width"), "final_height": goal.get("final_height"),
                "filter": "lanczos", "reviewed": True}
            if (self.plan.target_context.get("output_plan") == exact
                    and goal.get("detail_preference") == "faithful"
                    and type(base_width) is int and type(base_height) is int
                    and base_width * goal["final_height"] == base_height * goal["final_width"]):
                return ""
            return "Desired final pixels differ from the generator base. No executable final-size stage is attached; keep the goal saved and review a supported follow-up before preparing."
        return ""

    def _refresh_output_summary(self) -> None:
        goal = self.plan.target_context.get("output_goal")
        self.output_details.clear()
        self.output_details_toggle.setVisible(bool(goal))
        self.output_details.setVisible(bool(goal) and self.output_details_toggle.isChecked())
        if goal is None:
            request = self.plan.request
            base = (f"Generator base: {request.width} × {request.height}. "
                    if request.width and request.height else "Generator base size is unspecified. ")
            self.output_summary.setText(base + "Choose a desired final size; exact generation pixels remain available in Customize.")
            return
        if self._output_followup_blocker().startswith("The saved final-output goal is invalid"):
            self.output_summary.setText("The saved final-output goal is invalid; choose both pixel dimensions and review it again.")
            return
        request = self.plan.request
        base = f"{request.width} × {request.height}" if request.width and request.height else "unspecified"
        final = f"{goal.get('final_width')} × {goal.get('final_height')}"
        text = f"Generator base {base} → desired final {final}. "
        compact = None
        result = self._output_preview
        if result and self._output_preview_plan == self.plan.to_dict():
            recommended = result["base"]
            compact = [text.strip(),
                f"Recommended base {recommended['width']} × {recommended['height']} ({result['model_evidence'].replace('_', ' ')}). "
                f"Peak GPU memory: {result['vram']['evidence']}."]
            text += f"Recommended base {recommended['width']} × {recommended['height']} ({result['model_evidence'].replace('_', ' ')}). "
            stage_names = {"resize": "Lanczos resize", "faithful_resize": "Lanczos resize",
                "generative_upscale": "detail refinement", "native_refine": "detail refinement"}
            text += "Stages: " + ", ".join(
                "base requires a Readiness check" if item["kind"] == "base" and not item["available"] else
                f"{stage_names.get(item['kind'], item['kind'].replace('_', ' '))} {'available' if item['available'] else 'not qualified'}"
                for item in result["stages"]) + ". Size guidance does not grant execution readiness. "
            vram = result["vram"]
            text += f"\nPeak GPU memory: {vram['evidence']}. "
            if vram["evidence"] != "unknown" or vram["assessment"] == "limited":
                text += vram["message"] + " "
            observation = result.get("resource_observation", {})
            text += " " + self._memory_observation_text(observation)
            if result.get("goal_preset") == "quick_preview" and result["model_evidence"] in {"exact_model", "verified_family"}:
                preview_effect = f"Applying this preview sets the final output to {result['final']['width']} × {result['final']['height']}."
                text += " " + preview_effect
                compact.append(preview_effect)
            if result["messages"]:
                text += " " + " ".join(result["messages"])
            held = result.get("application_blocker", "")
            if held:
                compact.append(held.split(". ", 1)[0] + ".")
                compact.append("Choose a standard aspect ratio or smaller final size; no crop or padding is applied."
                    if held.startswith("No 64-pixel") else
                    "Choose Resize without adding detail, or review the model and connection in Readiness.")
            elif result["vram"]["assessment"] == "limited":
                compact.append("Memory evidence limits this plan. Review the memory details, then free GPU memory or choose a smaller request.")
            else:
                compact.append("Review the plan and memory details, then apply. Readiness checks are still required before generation.")
        else:
            try:
                advice = plan_image_output_resolution(goal["final_width"], goal["final_height"],
                    model_evidence=self._local_size_evidence(), installed_stages={},
                    detail_preference=goal.get("detail_preference", "faithful"),
                    goal_preset=goal.get("preset", "final_output"),
                    current_base=({"width": request.width, "height": request.height}
                        if request.width and request.height else None))
                if advice["model_evidence"] != "unknown":
                    proposed = advice["base"]
                    text += (f"Local family suggestion: {proposed['width']} × {proposed['height']} generation pixels. "
                        "This is draft guidance, not verified model quality or memory fit. ")
                if advice["application_blocker"]:
                    text += advice["application_blocker"] + " "
            except (KeyError, ValueError):
                text += "Choose a bounded final size and a standard aspect ratio for local sizing advice. "
            text += "Preview Recommended Plan to check the model and finishing stages, then explicitly apply. Peak GPU memory is unknown until qualified evidence is available."
        blocker = self._output_followup_blocker()
        if blocker:
            if not (result and self._output_preview_plan == self.plan.to_dict()):
                text += " " + blocker
        else:
            output_plan = self.plan.target_context.get("output_plan")
            if isinstance(output_plan, dict) and output_plan.get("kind") == "generative_refine":
                from core.generation_refinement import prepare_refinement
                refinement = prepare_refinement(output_plan, width=self.plan.request.width,
                    height=self.plan.request.height, output_goal=goal, binding=self.binding or {},
                    capabilities=getattr(self.adapter, "capabilities", None) or {})
                refined = f"{output_plan['refine_width']} × {output_plan['refine_height']}"
                text += (f" Generate at {base}, then run a second detail pass at {refined} "
                         f"(strength {output_plan['strength']}, {refinement['effective_steps']} effective steps); "
                         "this generative pass may change image details.")
                if (output_plan["refine_width"], output_plan["refine_height"]) != (
                        output_plan["final_width"], output_plan["final_height"]):
                    text += f" Resize that result with Lanczos to {final}."
            elif isinstance(output_plan, dict) and output_plan.get("kind") == "faithful_resize":
                text += f" Generate at {base}, then resize with Lanczos to {final}; this preserves appearance and does not invent detail."
        self.output_details.setPlainText(text)
        self.output_summary.setText("\n".join(compact) if compact else text)

    def _refresh_from_plan(self, message: str = "") -> None:
        self._refreshing = True
        request = self.plan.request
        for widget, value in ((self.goal, self.plan.goal), (self.positive, request.positive_prompt), (self.negative, request.negative_prompt), (self.model, request.model), (self.model_hash, request.model_hash), (self.seed, request.seed), (self.cfg_scale, request.cfg_scale), (self.sampler, request.sampler), (self.scheduler, request.scheduler)):
            text = "" if value is None else str(value)
            widget.blockSignals(True); widget.setPlainText(text) if isinstance(widget, QTextEdit) else widget.setText(text); widget.blockSignals(False)
        self.intent.blockSignals(True); self.intent.setCurrentIndex(max(0, self.intent.findData(self.plan.intent))); self.intent.blockSignals(False)
        reproduce = self.intent.model().item(self.intent.findData("reproduce"))
        reproduce.setEnabled(self.plan.source is not None)
        reproduce.setToolTip(
            "Requires an existing recorded generation to replay."
            if self.plan.source is None else "Replay the existing recorded generation."
        )
        for widget, value in ((self.size_width, request.width), (self.size_height, request.height), (self.steps, request.steps)):
            widget.blockSignals(True); widget.setValue(int(value or 0)); widget.blockSignals(False)
        output_goal = self.plan.target_context.get("output_goal", {})
        final = (output_goal.get("final_width", 0), output_goal.get("final_height", 0)) if isinstance(output_goal, dict) else (0, 0)
        final = tuple(value if type(value) is int and 0 <= value <= 8192 else 0 for value in final)
        for widget, value in ((self.final_width, final[0]), (self.final_height, final[1])):
            widget.blockSignals(True); widget.setValue(int(value or 0)); widget.blockSignals(False)
        self.output_preference.blockSignals(True)
        self.output_preference.setCurrentIndex(max(0, self.output_preference.findData(
            output_goal.get("detail_preference", "faithful") if isinstance(output_goal, dict) else "faithful")))
        self.output_preference.blockSignals(False)
        self.generation_goal_preset.blockSignals(True)
        self.generation_goal_preset.setCurrentIndex(max(0, self.generation_goal_preset.findData(
            output_goal.get("preset", "final_output") if isinstance(output_goal, dict) else "final_output")))
        self.generation_goal_preset.blockSignals(False)
        self._sync_output_selectors()
        self.safety_ack.blockSignals(True); self.safety_ack.setChecked(bool(self.plan.safety_policy.get("reviewed"))); self.safety_ack.blockSignals(False)
        self._refresh_seed_controls()
        self._refresh_source_panel()
        self._displayed_request = self._request_field_values()
        self._refresh_request_details_summary()
        self._refresh_output_summary()
        self._refresh_review(message, fields=False)

    def _refresh_review(self, message: str = "", *, fields: bool = True, fragments: bool = True) -> None:
        if (self.receipt is None and self._submission_terminal_seen
                and self._submission_state() in {"complete", "failed", "cancelled"}):
            self._submitted_progress = None
            self._submitted_job_id = ""
        request = self.plan.request
        checkpoints = [item for item in request.resources if item.role == "checkpoint"]
        self.main_model_choice.setText(
            (self._display_resource(checkpoints[-1]) or "Selected checkpoint") if checkpoints else request.model or "Choose the model that will create your image.")
        self.refresh_model_details_button.setVisible(bool(request.model_hash and self.registry is not None))
        if request.model_hash and self.model_setup_status.text().startswith("Choose a main model"):
            self.model_setup_status.setText(
                "Main model selected. Review Recommended Setup or refresh its recorded requirements and trigger words. Generator readiness is checked separately."
            )
        elif not request.model_hash and self.model_setup_status.text().startswith("Main model selected"):
            self.model_setup_status.setText(
                "Choose a main model, then review its requirements and available trigger words."
            )
        additions = [item for item in request.resources if item.role != "checkpoint"]
        labels = {"lora": "LoRA", "vae": "VAE"}
        selected = [f"{labels.get(item.role, item.role.replace('_', ' '))}: {self._display_resource(item) or item.id}" for item in additions[:4]]
        if len(additions) > 4: selected.append(f"{len(additions) - 4} more; see Review")
        self.additions_summary.setText("Selected: " + "; ".join(selected) if selected else "No optional additions selected.")
        outside = [f"{label}: {value}" for label, value, widget in (
            ("Width", request.width, self.size_width), ("Height", request.height, self.size_height),
            ("Steps", request.steps, self.steps),
        ) if value is not None and int(value) != widget.value()]
        self.sampling_notice.setText(
            "Saved values differ from these controls: " + "; ".join(outside)
            + ". Untouched values stay exact. Inspect or edit them in Advanced; adapter preflight still applies."
            if outside else "")
        self.sampling_notice.setVisible(bool(outside))
        self.resource_tree.clear()
        for index, resource in enumerate(self.plan.request.resources):
            identity = resource.hashes.get("sha256")
            label = "sha256:" + identity if identity else "Unverified reference: " + resource.id
            item = QTreeWidgetItem([resource.role.replace("_", " "), self._display_resource(resource), label])
            item.setData(0, Qt.UserRole, index)
            item.setToolTip(2, label)
            self.resource_tree.addTopLevelItem(item)
        if not self.plan.request.resources:
            self.resource_tree.addTopLevelItem(QTreeWidgetItem(["No models selected", "Choose Main Model on Request", "Companions and adapter support need review"]))
        self.diff_tree.clear()
        for change in self.plan.diff():
            self.diff_tree.addTopLevelItem(QTreeWidgetItem([str(change.get("path", "")), str(change.get("before", "")), str(change.get("after", "")), str(change.get("consequence", ""))]))
        if not self.plan.diff(): self.diff_tree.addTopLevelItem(QTreeWidgetItem(["Source retained", "", "", "No deliberate change yet."]))
        # Keystrokes must never traverse the registry or inspect model files.
        readiness = assess_readiness(self.plan, "swarmui", registry=None, capabilities=None)
        self.readiness.setPlainText(_plain_json(readiness))
        self._sync_advanced_json_from_plan()
        if fragments:
            self.fragment_tree.blockSignals(True); self.fragment_tree.clear()
            visible_fragments, scope_note = self._visible_fragments()
            selected = {str(value.get("id")) for value in self.plan.knowledge_fragments if value.get("included")}
            for fragment in visible_fragments:
                full_text = str(fragment.get("text") or fragment.get("label") or fragment.get("id") or "Knowledge fragment")
                compact_text = " ".join(full_text.split())
                display = compact_text if len(compact_text) <= 96 else compact_text[:95].rstrip() + "…"
                kind = str(fragment.get("kind") or "")
                kind_display = kind if len(kind) <= 24 else kind[:23].rstrip() + "…"
                node = QTreeWidgetItem(["", display, kind_display])
                node.setFlags(node.flags() | Qt.ItemIsUserCheckable); node.setData(0, Qt.UserRole, fragment)
                node.setToolTip(1, full_text); node.setData(1, Qt.AccessibleTextRole, full_text)
                node.setToolTip(2, kind); node.setData(2, Qt.AccessibleTextRole, kind)
                node.setCheckState(0, Qt.Checked if str(fragment.get("id")) in selected else Qt.Unchecked); self.fragment_tree.addTopLevelItem(node)
            self.fragment_tree.blockSignals(False)
            self.outfit_scope_note.setText(scope_note)
        self._refreshing = False
        has_fragments = self.fragment_tree.topLevelItemCount() > 0
        self.fragment_tree.setVisible(has_fragments)
        self.fragment_empty_note.setVisible(not has_fragments)
        self.fragment_empty_note.setText(
            "No outfit details are in this browse group. Choose another character or Show group; checked details in other groups stay selected."
            if self.outfits else
            "No prompt fragments are available in this draft. Enter an Image prompt on Request, or start from an image with Tags or Descriptions.")
        self.apply_fragments_button.setEnabled(self.plan.apply_fragments() is not self.plan)
        self.fragment_actions_widget.setVisible(has_fragments or self.apply_fragments_button.isEnabled())
        self.outfit_browser_widget.setVisible(bool(self.outfits))
        self.outfit_scope_note.setVisible(bool(self.outfits))
        self.wearer_label.setVisible(bool(self.outfits and self.characters))
        self.wearer_choice.setVisible(bool(self.outfits and self.characters))
        self.apply_fragments_button.setToolTip("Select a reviewed detail not already in the Image prompt." if not self.apply_fragments_button.isEnabled() else "Append the checked reviewed details to the Image prompt.")
        self._refresh_output_summary()
        if message: self.status.setText(message)
        self._update_actions()

    def _update_actions(self) -> None:
        if (not self._pending and not self._draft_save_error
                and (self.plan.digest != self._draft_saved_digest or not self._draft_inputs_valid())):
            self.draft_status.setText("Unsaved changes · this draft saves automatically while open and before closing."
                if self._draft_inputs_valid() else "Unsaved edits · complete invalid values or apply the Advanced changes.")
        prepared_valid = bool(self.prepared and self.prepared.get("valid", self.prepared.get("request") is not None))
        active = self._execution_active()
        self.close_button.setText("Hide Workspace" if self._execution_active() else "Close Workspace")
        submitted_state = self._submission_state()
        self.cancel_button.setVisible(bool("submit" in self._pending or submitted_state in
            {"queued", "running", "submitting", "cancelling"}))
        self.cancel_button.setEnabled(bool(self._submitted_job_id and not self._cancellation_requested
            and submitted_state in {"queued", "running", "submitting"}))
        source_ready = not self.remix_source or (callable(self.source_image_provider) and any(
            item.kind in {"init_image", "reference_image", "control_image"} for stage in self.plan.request.stages for item in stage.inputs))
        output_blocker = self._output_followup_blocker()
        memory_blocker = self._memory_submission_blocker()
        input_issue, _target = self._ordinary_input_issue()
        self.submit_button.setEnabled(prepared_valid and self._seed_input_valid() and not output_blocker and not memory_blocker and not input_issue and self.safety_ack.isChecked() and not self._pending and not active and source_ready)
        self.submit_button.setToolTip("Reconnect the exact source image before submitting Remix." if not source_ready else input_issue or memory_blocker)
        idle = not self._pending and not active
        if (getattr(self, "_memory_observation_plan_digest", None) != self.plan.digest
                or getattr(self, "_memory_observation_adapter", None) is not self.adapter):
            self.memory_status.setText("No current observation for this request and connection. Prepare again to check free GPU memory; exact request fit remains unproven.")
        elif hasattr(self, "_memory_observation"):
            self.memory_status.setText(self._memory_observation_text(self._memory_observation)
                + "\n" + getattr(self, "_memory_assessment", {}).get("message", "")
                + "\n" + getattr(self, "_memory_cost_message", ""))
        # Keep the exact reviewed draft stable while I/O or execution owns it.
        # Results and readiness remain navigable for progress, cancellation and checks.
        for index in range(3):
            self.tabs.widget(index).setEnabled(idle)
        for field in (self.endpoint, self.local_model_name, self.local_model_path):
            field.setEnabled(idle)
        for button in (self.load_button, self.history_button,
                       self.choose_ingested_button, self.record_outcome_button):
            button.setEnabled(idle)
        self.history_choice.setEnabled(idle)
        selected_job = self.history_choice.currentData()
        self.recover_button.setEnabled(idle and isinstance(selected_job, dict)
                                       and bool(selected_job.get("job_id")))
        selection_needs_open = self._selected_job_needs_open()
        if selection_needs_open:
            self.opened_result_status.setText(
                f"Selected: {self.history_choice.currentText()}. Open or recover this job before using result actions."
            )
        elif self.receipt:
            self.opened_result_status.setText(
                f"Opened result: {self._opened_result_name()}. Result actions use this job."
            )
        elif self.history_choice.count():
            self.opened_result_status.setText("Choose a recent job and open or recover it to review its result.")
        self.opened_result_status.setVisible(bool(self.receipt or self.history_choice.count()))
        outcome_available = bool(self.receipt and not selection_needs_open)
        self.outcome_toggle.setVisible(outcome_available)
        self.outcome_toggle.setEnabled(outcome_available and idle)
        self.outcome_group.setVisible(outcome_available and self.outcome_toggle.isChecked())
        self.record_outcome_button.setEnabled(outcome_available and idle)
        self.bind_button.setEnabled(bool(self.adapter) and idle)
        self.find_model_button.setEnabled(bool(self.adapter and self.registry and self.plan.request.model_hash) and idle)
        self.refresh_model_details_button.setEnabled(self.registry is not None and idle)
        self.model_trigger_container.setEnabled(idle)
        self.prompt_templates_button.setEnabled(idle)
        self.regional_prompts_button.setEnabled(idle)
        regions = self.plan.target_context.get("regional_prompt", {})
        self.regional_prompts_button.setText("Review Regional Prompts…" if isinstance(regions, dict) and regions.get("regions")
                                            else "Place Regional Prompts…")
        self.prepare_button.setEnabled(bool(self.adapter and self.binding) and self._seed_input_valid() and not output_blocker and not input_issue and idle)
        self.prepare_button.setToolTip(input_issue or output_blocker)
        self.output_preview_button.setEnabled(bool(self.plan.target_context.get("output_goal")) and idle)
        self.output_apply_button.setEnabled(bool(self._output_preview is not None and self._output_preview_plan == self.plan.to_dict()) and idle)
        if self._output_preview and self._output_preview.get("application_blocker"):
            self.output_apply_button.setEnabled(False)
        self.output_apply_button.setToolTip((self._output_preview or {}).get("application_blocker", ""))
        self.check_button.setEnabled(idle)
        self.choose_resource_button.setEnabled(self.registry is not None and not self._pending and not active)
        for button in (self.add_lora_button, self.choose_vae_button, self.choose_other_resource_button):
            button.setEnabled(self.registry is not None and not self._pending and not active)
        self.resource_role.setEnabled(not self._pending and not active)
        self.model_guidance_button.setEnabled(not self._pending and not active)
        self.seed_mode.setEnabled(not self._pending and not active)
        self.seed.setEnabled(not self._pending and not active)
        self.random_seed_button.setEnabled(not self._pending and not active)
        seed_matches_draft = self._seed_input_valid() or (self.plan.request.seed is None and not self.seed.text().strip())
        self.setup_button.setEnabled(self.setup_available and seed_matches_draft and not input_issue and not self._pending and not active)
        self.setup_button.setToolTip(input_issue or "Save this request, then open local generation setup. Return here to check and prepare it again.")
        self.save_button.setEnabled(seed_matches_draft and not input_issue and not self._pending)
        self.export_button.setEnabled(seed_matches_draft and not input_issue and not self._pending)
        self.ingest_button.setEnabled(bool(self.receipt and self.receipt.get("state") == "verified")
                                      and idle and not self._ingest_issue and not selection_needs_open)
        self.ingest_folder_recovery.setVisible(self._ingest_folder_recovery and
            bool(self.receipt and self.receipt.get("state") == "verified") and not selection_needs_open)
        self.ingest_folder_button.setEnabled(idle)
        self.preview_result_button.setEnabled(self.ingest_button.isEnabled())
        self.finish_result_button.setEnabled(self.ingest_button.isEnabled())
        self.result_action_button.setEnabled(self.ingest_button.isEnabled())
        self.result_action.setEnabled(self.ingest_button.isEnabled())
        memory_failure = bool(self.receipt and self.receipt.get("failure_class") in {"out_of_memory", "resource_limited"})
        recovery_ready = bool(self.receipt and (self.receipt.get("proven_unsubmitted") is True
            or self.receipt.get("server_stopped") is True))
        self.memory_recovery_help.setVisible(memory_failure)
        self.memory_recovery_button.setVisible(memory_failure)
        self.memory_recovery_button.setEnabled(memory_failure and recovery_ready and idle)
        if memory_failure:
            self.memory_recovery_help.setText(
                "The generator ran out of memory, or GPU memory was unavailable. Your request and failure receipt are retained. "
                "Close other memory-heavy work or preview the Lower Memory preset's smaller base canvas. "
                "Quick Preview only skips finishing; it does not reduce base-pass memory. "
                "Smaller sizes can affect quality; no preset guarantees memory fit. "
                + ("Review and prepare a revised request before submitting; nothing retries automatically."
                   if recovery_ready else "Recover the selected job and confirm the generator has stopped before trying again."))
        if not self.receipt or selection_needs_open:
            self.result_preview.hide()
        selected = self.resource_tree.currentItem()
        self.remove_resource_button.setEnabled(
            not self._pending and not active and selected is not None
            and type(selected.data(0, Qt.UserRole)) is int
        )
        self._update_workflow_context(prepared_valid=prepared_valid, active=active,
            input_issue=input_issue)
        self._update_draft_action_presentation()
        self.source_panel.setEnabled(idle)
        self.experiment_button.setEnabled(not self._pending and (idle or bool(self._experiment_dialog)))
        self.results_experiment_button.setEnabled(self.experiment_button.isEnabled())
        self.save_recipe_button.setEnabled(idle and not input_issue)
        self.save_recipe_button.setToolTip(input_issue)
        self.load_recipe_button.setEnabled(idle)

    def _update_draft_action_presentation(self, *_args) -> None:
        """Keep the current decision visible while retaining explicit file tools."""
        expanded = self.more_draft_actions_button.isChecked()
        has_work = (self.plan.digest != self._initial_plan_digest
                    or bool(self.source_record or self.source_identity))
        self.save_button.setVisible(expanded or self._draft_save_error
                                    or (has_work and self.save_button.isEnabled()))
        self.load_button.setVisible(expanded)
        self.export_button.setVisible(expanded)
        review_stage = (bool(self.prepared and self.prepared.get(
            "valid", self.prepared.get("request") is not None))
            or self._next_step_tab == 1 or self.submit_button.isEnabled())
        self.submit_button.setVisible(
            review_stage and not self._pending and not self._execution_active()
        )
        self.next_step_button.setVisible(not self.submit_button.isEnabled())
        self.more_draft_actions_button.setText(
            "Fewer Draft Actions" if expanded else "More Draft Actions…"
        )

    def open_experiment(self):
        if self._pending or self._shutdown_requested: return
        if self._experiment_dialog is None:
            if self._execution_active(): return
            from .generation_experiment_dialog import GenerationExperimentDialog
            self._experiment_dialog = GenerationExperimentDialog(self)
        self._experiment_dialog.refresh_actions()
        self.alternatives_return_button.hide()
        self._experiment_dialog.show(); self._experiment_dialog.raise_()

    def review_alternatives_readiness(self):
        group = self._experiment_dialog
        if group is None or group.busy() or not group.experiment:
            return
        group.hide()
        self.tabs.setCurrentIndex(self._readiness_tab_index)
        self.alternatives_return_button.show()
        self.alternatives_return_button.setFocus()

    def return_to_alternatives(self):
        if self._experiment_dialog is None or not self._experiment_dialog.experiment:
            self.alternatives_return_button.hide()
            return
        self.open_experiment()

    def finish_result(self):
        if (self._pending or self._execution_active() or self._selected_job_needs_open()
                or not self.receipt or self.receipt.get("state") != "verified"):
            return
        from core.generation_execution import GenerationExecutionService
        service = GenerationExecutionService(self.storage_root, self.jobs)
        receipt = deepcopy(self.receipt)
        def verified_source():
            return service.verified_output_paths(deepcopy(receipt))
        def done(paths):
            if self.receipt != receipt or self._selected_job_needs_open():
                raise ValueError("The selected result changed; open finishing again.")
            from .generation_finishing_dialog import GenerationFinishingDialog
            plan = GenerationPlan.from_dict(receipt["prepared"]["plan"])
            goal = plan.target_context.get("deferred_finishing", {}).get("output_goal",
                plan.target_context.get("output_goal", {}))
            child = GenerationFinishingDialog(self.jobs, self.storage_root, paths, verified_source,
                parent=self, settings=self.settings, initial_goals={Path(path).absolute(): goal for path in paths})
            child.ingest_requested.connect(self.ingest_requested.emit)
            def refine(value):
                self.result_action_requested.emit({"action": "remix", "plan": None, "paths": [value["path"]],
                    "finish_context": {"output_goal": value["output_goal"]}, "finish_source_plan": plan,
                    "finish_strength": value["strength"], "verified_source": verified_source,
                    "on_opened": child.close})
            child.refine_requested.connect(refine)
            child.background_work_finished.connect(self._notify_background_work_finished)
            child.background_work_finished.connect(self._update_actions)
            self._finishing_dialogs.append(child)
            child.show(); child.inspect_winner()
        self._run("finish-result", verified_source, done)

    def save_recipe(self):
        if self._pending or self._execution_active(): return
        input_issue, _target = self._ordinary_input_issue()
        if input_issue:
            self.status.setText(input_issue)
            return
        from core.generation_recipes import make_recipe
        from core.generation_saved_work import save_named_recipe
        from .generation_setup_save import SetupSaveDialog
        options = SetupSaveDialog(self); options.setWindowTitle("Save Reusable Generation Setup")
        layout = QVBoxLayout(options); form = QFormLayout()
        name_field = QLineEdit(); form.addRow("Setup name", name_field)
        include_prompts = QCheckBox("Include my positive and negative prompts as optional templates")
        form.addRow(include_prompts); layout.addLayout(form)
        explanation = QLabel("Save the current model components, sampling and finishing choices for another request. Prompts are optional. Source images and raw metadata are never copied into a setup.")
        explanation.setWordWrap(True); layout.addWidget(explanation)
        customize = QPushButton("Customize Saved Parts…")
        customize.setCheckable(True)
        customize.setAccessibleDescription("Show which supported parts of this request the reusable setup will include.")
        layout.addWidget(customize, 0, Qt.AlignLeft)
        parts = QWidget(); part_layout = QVBoxLayout(parts); part_layout.setContentsMargins(16, 0, 0, 0)
        include_model = QCheckBox("Model and LoRA choices")
        include_sampling = QCheckBox("Sampling and image size")
        include_finishing = QCheckBox("Finishing preferences")
        for choice in (include_model, include_sampling, include_finishing):
            choice.setChecked(True); part_layout.addWidget(choice)
        parts.hide(); customize.toggled.connect(parts.setVisible); layout.addWidget(parts)
        validation = QLabel(); validation.setWordWrap(True); validation.setObjectName("warningLabel")
        validation.hide(); layout.addWidget(validation)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        save_operation = {"recipe": None, "id": ""}
        def accept_checked():
            if not name_field.text().strip():
                validation.setText("Give this setup a name before saving.")
            elif not any(choice.isChecked() for choice in
                         (include_prompts, include_model, include_sampling, include_finishing)):
                validation.setText("Choose at least one part to save.")
            else:
                try:
                    recipe = make_recipe(name_field.text(), self.plan,
                        include_prompts=include_prompts.isChecked(), include_model=include_model.isChecked(),
                        include_sampling=include_sampling.isChecked(), include_finishing=include_finishing.isChecked())
                except ValueError as exc:
                    validation.setText(str(exc))
                else:
                    if recipe != save_operation["recipe"]:
                        save_operation.update(recipe=recipe, id=uuid4().hex)
                    options.save_in_progress = True
                    options.setEnabled(False)
                    def saved(_path):
                        options.save_in_progress = False
                        options.setEnabled(True)
                        options.accept()
                        self.status.setText(
                            "Setup saved. Use Saved Setup in Generate for a new image, or Apply Saved Setup here to merge into this draft."
                        )
                    def failed(error):
                        options.save_in_progress = False
                        options.setEnabled(True)
                        validation.setText(f"Setup was not confirmed saved: {error} Review storage and retry here.")
                        validation.show()
                    self._run("save-recipe", lambda: save_named_recipe(
                        self.storage_root, recipe, operation_id=save_operation["id"]), saved, failed)
                    return
            validation.show()
        buttons.accepted.connect(accept_checked); buttons.rejected.connect(options.reject); layout.addWidget(buttons)
        options.exec()

    def start_setup_from_image(self):
        """Review saved image evidence into the existing reusable setup store."""
        if self._pending or self._execution_active(): return
        existing=getattr(self,"_image_setup_dialog",None)
        if existing is not None:
            if existing.isVisible():
                existing.raise_(); existing.activateWindow()
            return
        from .generation_image_setup_dialog import ImageGenerationSetupDialog
        from core.generation_saved_work import save_named_recipe
        records = tuple(getattr(self, "source_records", ()))
        dialog = ImageGenerationSetupDialog(
            records, self.plan.knowledge_fragments, parent=self, jobs=self.jobs,
            settings=self.settings,
            suggestion_source=getattr(self, "setup_suggestion_source", None),
            source_identity=deepcopy(self.source_identity),
            source_current_provider=self.source_current_provider,
            registry=self.registry,
            protected_model_roots=self.protected_model_roots,
        )
        identity = deepcopy(self.source_identity)
        save_operation = {"recipe": None, "id": ""}
        def save_reviewed(recipe):
            if callable(self.source_current_provider) and self.source_current_provider() != identity:
                dialog.save_button.setEnabled(False)
                dialog.review_status.setText("The selected image changed. Close this review, reselect the image, and start a fresh setup; nothing was saved.")
                return
            if recipe != save_operation["recipe"]:
                save_operation.update(recipe=recipe, id=uuid4().hex)
            dialog.save_in_progress = True
            dialog.setEnabled(False)
            def save_checked():
                if callable(self.source_current_provider) and self.source_current_provider() != identity:
                    raise ValueError("The selected image changed. Reopen it and review a fresh setup before saving.")
                return save_named_recipe(self.storage_root, recipe, operation_id=save_operation["id"])
            def saved(_path):
                dialog.save_in_progress = False
                dialog.setEnabled(True)
                dialog.accept()
                self.status.setText("Setup saved. Choose Use Saved Setup in Generate for a new image, "
                                    "then review its model and readiness before submitting.")
            def failed(error):
                dialog.save_in_progress = False
                dialog.setEnabled(True)
                if "selected image changed" in error:
                    dialog.save_button.setEnabled(False)
                    dialog.review_status.setText(error + " Close and reselect the image; nothing was saved.")
                else:
                    dialog.save_button.setEnabled(dialog.recipe is not None)
                    dialog.review_status.setText(f"Setup was not confirmed saved: {error} Your choices remain here; retry Save Setup.")
            self._run("save-image-recipe", save_checked, saved, failed)
        dialog.save_callback = save_reviewed
        self._image_setup_dialog=dialog
        dialog.setup_requested.connect(lambda: self.image_setup_requested.emit(dialog))
        def close_review():
            if self._image_setup_dialog is dialog:
                dialog.reject()
        def finished(_result):
            if self._image_setup_dialog is dialog:
                self._image_setup_dialog=None
            self.workspace_closed.disconnect(close_review)
        self.workspace_closed.connect(close_review)
        dialog.finished.connect(finished)
        dialog.open()

    def load_recipe(self, *, selected_path=None, on_unapplied=None):
        if self._pending or self._execution_active(): return
        from core.generation_recipes import load_recipe
        if selected_path is None:
            from .generation_saved_work_dialog import SavedGenerationWorkDialog
            browser = SavedGenerationWorkDialog(
                self.jobs, self.storage_root, "recipes", parent=self,
                registry=self.registry, settings=self.settings,
                protected_model_roots=self.protected_model_roots,
            )
            self._saved_work_dialog = browser
            browser.background_work_finished.connect(self._notify_background_work_finished)
            accepted = browser.exec() == QDialog.Accepted
            self._saved_work_dialog = None
            if not accepted or self._shutdown_requested or not browser.selected_path: return
            path = browser.selected_path
        else:
            if self._shutdown_requested: return
            path = Path(selected_path)
        current = self.plan
        def inspect_resources(recipe):
            notes = []
            recoverable = []
            resources = recipe.get("resources", [])
            if self.registry is not None:
                for resource in resources:
                    full_hash = resource["hashes"].get("sha256")
                    label = resource.get("display_label") or resource.get("name") or resource["role"]
                    if not full_hash:
                        notes.append(f"{label}: exact identity is missing.")
                        continue
                    role = "control_model" if resource["role"] == "controlnet" else resource["role"]
                    identity = "sha256:" + full_hash.removeprefix("sha256:")
                    result = self.registry.resolve(identity, role=role)
                    if result.status != "identity_verified":
                        notes.append(f"{label} ({resource['role']}): {result.status.replace('_', ' ')}. {result.reason}")
                    if result.status == "unavailable" and (identity, role) not in {
                            (item[0], item[1]) for item in recoverable}:
                        recoverable.append((identity, role, label))
            return tuple(notes), recoverable
        def prepare():
            recipe = load_recipe(path)
            notes, recoverable = inspect_resources(recipe)
            return recipe, notes, recoverable
        def done(value):
            if self.plan.digest != current.digest: raise ValueError("Request changed; preview this setup again.")
            from .generation_recipe_dialog import GenerationRecipeDialog
            from .model_workspace_dialog import ExactModelCopyDialog
            from shiboken6 import isValid
            review_state = {"recoverable": tuple(value[2] or ())}
            def locate_exact():
                selected = dialog.selected_recoverable_resource()
                if (not selected or selected not in {(item[0], item[1]) for item in review_state["recoverable"]}
                        or self.registry is None):
                    return
                if self.plan.digest != current.digest:
                    dialog.set_resource_review(("Request changed. Close and reopen this setup to review it again.",))
                    dialog.apply_button.setEnabled(False)
                    return
                try:
                    revision = self.registry.snapshot()[1]
                except Exception as exc:
                    dialog.set_resource_review((f"Model registry needs attention: {exc}",),
                                               recoverable_resources=review_state["recoverable"])
                    return
                identity, role = selected
                copy = ExactModelCopyDialog(
                    self, identity, revision, self.protected_model_roots,
                    parent=dialog, back_label="Back to Setup Review",
                )
                copy.exec()
                if not copy.attached or not isValid(dialog) or not dialog.isVisible():
                    return
                dialog.mark_location_saved()
                dialog.set_location_rechecking(True)
                def recheck():
                    try:
                        return inspect_resources(value[0])
                    except Exception as exc:
                        return (f"Exact location was saved, but its fresh check needs attention: {exc}",), review_state["recoverable"]
                def checked(result):
                    if not isValid(dialog) or not dialog.isVisible():
                        return
                    if self.plan.digest != current.digest:
                        dialog.set_resource_review(("Request changed. Close and reopen this setup to review it again.",))
                        dialog.apply_button.setEnabled(False)
                        return
                    review_state["recoverable"] = tuple(result[1])
                    dialog.set_location_rechecking(False)
                    dialog.set_resource_review(
                        result[0] or ("Exact model locations rechecked. Generator readiness remains separate.",),
                        recoverable_resources=result[1],
                    )
                self._run("recheck-recipe-location", recheck, checked)
                if "recheck-recipe-location" not in self._pending:
                    dialog.set_location_rechecking(False)
                    dialog.set_resource_review(("Could not start the fresh location check. Reopen this setup to retry.",),
                                               recoverable_resources=review_state["recoverable"])
            dialog = GenerationRecipeDialog(
                value[0], current, resource_notes=value[1],
                recoverable_resources=value[2], locate_exact=locate_exact, parent=self,
            )
            accepted = dialog.exec() == QDialog.Accepted
            if accepted:
                if self.plan.digest != current.digest:
                    self.status.setText("Request changed. Reopen the saved setup before applying it.")
                    return
                self.plan = dialog.selected_plan; self.prepared = None; self.binding = None; self.receipt = None
                self._output_preview = self._output_preview_plan = None
                self._refresh_from_plan("Setup applied. Review the source controls, bind exact resources and prepare again before running.")
                request = self.plan.request
                has_main_model = bool(request.model or request.model_hash or any(
                    resource.role == "checkpoint" for resource in request.resources
                ))
                self.model_setup_status.setText(
                    "A main model is recorded in this draft. Refresh Model Details to check "
                    "whether it is registered and review any requirements or trigger words. "
                    "Generator readiness is separate."
                    if has_main_model else
                    "This setup did not select a main model. Choose a registered model, then "
                    "refresh its requirements and trigger words."
                )
                while self.model_trigger_grid.count():
                    item = self.model_trigger_grid.takeAt(0)
                    if item.widget() is not None:
                        item.widget().deleteLater()
            elif dialog.location_saved:
                self.status.setText("Exact model location saved in Models; the Generate request was not changed.")
            if not accepted and callable(on_unapplied):
                on_unapplied(None, dialog.location_saved)
        if callable(on_unapplied):
            self._run("load-recipe", prepare, done, lambda error: on_unapplied(error, False))
        else:
            self._run("load-recipe", prepare, done)

    def _reviewing_retained_work(self) -> bool:
        return bool(self._history_context and self.tabs.currentIndex() == 3 and self.history_choice.count()
                    and not self.adapter and not self.binding and not self.prepared)

    def preview_verified_result(self):
        if (not self.receipt or self.receipt.get("state") != "verified" or self._pending
                or self._execution_active() or self._selected_job_needs_open()):
            return
        from core.generation_execution import GenerationExecutionService
        from core.training_source_io import read_stable_training_source
        receipt = deepcopy(self.receipt)
        def prepare():
            service = GenerationExecutionService(self.storage_root, self.jobs)
            output = receipt["outputs"][0]
            paths = service.verified_output_paths(receipt)
            raw, metadata = read_stable_training_source(Path(paths[0]))
            if metadata["sha256"] != output["sha256"]:
                raise ValueError("The result changed while its preview was loading. Recover the job before reuse.")
            return raw
        def show(raw):
            if self.receipt != receipt or self._selected_job_needs_open():
                self.status.setText("The selected job changed before its preview opened. Open it and preview again.")
                return
            image = QImage.fromData(raw)
            if image.isNull():
                self.status.setText("The verified result could not be displayed.")
                return
            self.result_preview.set_image(QPixmap.fromImage(image), receipt["outputs"][0]["path"])
            self.result_preview.show()
            self.tabs.widget(3).ensureWidgetVisible(self.result_preview)
            self.status.setText("Verified result preview. Send it to Ingest when ready to review it.")
        self._run("result_preview", prepare, show)

    def create_result_draft(self):
        if (not self.receipt or self.receipt.get("state") != "verified" or self._pending
                or self._execution_active() or self._selected_job_needs_open()):
            return
        from core.generation_execution import GenerationExecutionService
        receipt = deepcopy(self.receipt)
        action = str(self.result_action.currentData())
        def prepare():
            paths = GenerationExecutionService(self.storage_root, self.jobs).verified_output_paths(receipt)
            plan = None
            if action in {"same_seed", "new_seed"}:
                import secrets
                original = GenerationPlan.from_dict(receipt["prepared"]["plan"])
                if original.request.seed is None:
                    raise ValueError("This retained request has no exact seed to repeat.")
                new_seed = None
                if action == "new_seed":
                    new_seed = secrets.randbelow(2**63 - 1)
                    if new_seed == original.request.seed:
                        new_seed = (new_seed + 1) % (2**63)
                plan = original.fork(seed=new_seed)
                if plan.source_identity and plan.source_identity != self.source_identity:
                    raise ValueError("Reopen this request from its original source before repeating it; its source is not attached here.")
            return {"action": action, "paths": paths, "plan": plan}
        self._run("result_draft", prepare, self.result_action_requested.emit)

    def _tab_changed(self, index: int) -> None:
        if index == 0:
            self._history_context = False
        self._update_actions()

    def _selected_job_needs_open(self) -> bool:
        """A cached history row never grants another receipt's result actions."""
        selected = self.history_choice.currentData()
        if not isinstance(selected, dict):
            return False
        selected_id = str(selected.get("job_id") or "")
        opened_id = str((self.receipt or {}).get("job_id") or "")
        return bool(selected_id and selected_id != opened_id)

    def _opened_result_name(self) -> str:
        receipt = self.receipt or {}
        plan = _mapping(_mapping(receipt.get("prepared")).get("plan"))
        request = _mapping(plan.get("request"))
        name = str(plan.get("goal") or request.get("positive_prompt") or
                   receipt.get("job_id") or "current job").replace("\n", " ")
        return name[:70]

    def _select_history(self, *_args) -> None:
        self._history_context = True
        self.result_preview.clear("Preview this opened job after its result is checked")
        self.result_preview.hide()
        self.status.setText(
            "Selection changed; no job was opened and no result action ran."
            if self._selected_job_needs_open() else
            "Opened job selected again; its old preview was cleared. Preview to recheck the image."
        )
        self._update_actions()

    def _update_workflow_context(self, *, prepared_valid: bool, active: bool,
                                 input_issue: str = "") -> None:
        identity = self.plan.source_identity
        collection = identity.get("collection")
        count = len(collection) if isinstance(collection, list) else int(bool(identity or self.plan.source))
        using = (f"Image references: {count:,}; source pixels are not included automatically."
                 if count else "New goal; no source images selected.")
        if any(item.kind == "init_image" for stage in self.plan.request.stages for item in stage.inputs):
            using = ("One exact source image is attached for image-guided generation."
                     if callable(self.source_image_provider) else "Saved image reference; reconnect the exact original to continue.")
        result = "New images; originals unchanged. Verified results go to Ingest as unreviewed."
        job_state = str(getattr(self._submitted_progress, "state", ""))
        if "submit" in self._pending:
            state, next_step, tab = "Submitting the reviewed request…", "Wait for submission; local generation is being started.", 3
        elif self._pending:
            state, next_step, tab = "Checking the current request…", "Wait for the current check; the plan is retained.", 4
        elif active and (self._cancellation_requested or job_state == "cancelling"):
            state, next_step, tab = "Cancellation requested; waiting for the final outcome.", "Wait for the verified outcome before sending results to Ingest.", 3
        elif active:
            state, next_step, tab = "Generation is in progress.", "Review progress or cancel in Results & Queue.", 3
        elif self.tabs.currentIndex() == 3 and self._selected_job_needs_open():
            state, next_step, tab = "The selected recent job is not open.", "Open or Recover Selected Job before using its result.", 3
        elif self._ingest_issue and self.receipt:
            state, next_step, tab = "The retained result needs review.", "Recover Selected Job to recheck the saved result before Ingest.", 3
        elif self.ingest_button.isEnabled():
            state, next_step, tab = "Verified results are available.", "Preview, Finish Result or Send to Ingest in Results & Queue.", 3
        elif job_state in {"failed", "cancelled", "complete"}:
            state, next_step, tab = f"Generation ended: {job_state}; no verified result is selected.", "Review retained jobs, or prepare a revised request.", 3
        elif self._reviewing_retained_work():
            state, next_step, tab = "Retained generation jobs are available.", "Select a retained job and recover its receipt, or open Request to plan a new image.", 3
        elif self.remix_source and not callable(self.source_image_provider):
            state, next_step, tab = "This draft needs its original source images.", "Choose Reconnect Source Images in the source panel.", 0
        elif not self.plan.request.positive_prompt:
            state, next_step, tab = "Describe the image before generation.", "Enter an Image prompt on Request. A planning note is optional and is not sent to the generator.", 0
        elif not self._seed_input_valid():
            state, next_step, tab = "Choose a valid seed before generation.", "Choose Same Seed, enter a Specific Seed, or choose Random Seed on Request.", 0
        elif input_issue:
            state, next_step, tab = "A visible Request value needs correction.", input_issue, 0
        elif not (self.plan.request.model or any(r.role == "checkpoint" for r in self.plan.request.resources)):
            state, next_step, tab = "A main model is needed before generation.", "Choose Main Model on Request. Expert Settings also accepts an exact generator model name.", 0
        elif self._output_followup_blocker():
            state, next_step, tab = self._output_followup_blocker(), "Review Desired final output on Request; preview and apply a supported recommendation, or use the model's base size.", 0
        elif self.adapter is None:
            state, next_step, tab = "Your generator has not been checked.", "Choose Check Generator on Readiness. Generator settings are in Setup.", 4
        elif not self.binding:
            state, next_step, tab = "The selected model needs checking.", "Choose Check Model on Readiness.", 4
        elif not self.safety_ack.isChecked():
            state, next_step, tab = "Your review is still required.", "Review prompts, inputs and consequences, then confirm your review.", 1
        elif not prepared_valid:
            state, next_step, tab = "This request needs a readiness check.", "Choose Check Reviewed Request on Readiness and resolve any blockers.", 4
        else:
            state, next_step, tab = "Ready for deliberate submission.", "Submit Reviewed Request starts local generation.", 1
        self.workflow_context.update_context(using=using, model=self._chosen_model_summary(),
            result=result, readiness=state, next_step=next_step)
        self._next_step_tab = tab
        self.results_status.setText(self.status.text() if active else
            "Open or recover the selected job before using its result." if self._selected_job_needs_open() else
            "Verified results are ready. Preview, finish, remix or send them to Ingest." if self.ingest_button.isEnabled() else
            "Open a recent job or an alternatives group to review results and recover interrupted work.")
        self.results_status.setVisible(active)

    def _show_next_step(self) -> None:
        """Reveal and focus a decision, without invoking its action."""
        self.tabs.setCurrentIndex(self._next_step_tab)
        input_issue, input_target = self._ordinary_input_issue()
        job_state = str(getattr(self._submitted_progress, "state", ""))
        if job_state in {"queued", "running", "submitting", "cancelling"} or "submit" in self._pending:
            target = self.cancel_button
        elif self._pending:
            target = self.connection_detail
        elif self._next_step_tab == 3 and self._selected_job_needs_open():
            target = self.recover_button
        elif self.ingest_button.isEnabled():
            target = self.ingest_button
        elif job_state in {"failed", "cancelled", "complete"}:
            target = self.history_button
        elif self._reviewing_retained_work():
            target = self.recover_button
        elif self.remix_source and not callable(self.source_image_provider):
            target = self.reconnect_source_button
            self.source_scroll.ensureWidgetVisible(target)
        elif self._next_step_tab == 0:
            if not self.plan.request.positive_prompt:
                target = self.positive
            elif not self._seed_input_valid():
                target = self.seed if self.seed_mode.currentData() == "specific" else self.seed_mode
            elif input_issue:
                target = input_target
                if target in (self.cfg_scale, self.model, self.model_hash,
                              self.steps, self.sampler, self.scheduler):
                    self.expert_settings.setChecked(True)
                elif target in (self.size_width, self.size_height):
                    self.output_customization.setChecked(True)
                elif target in (self.goal, self.negative):
                    self.request_details_toggle.setChecked(True)
            elif (self.plan.request.model or any(r.role == "checkpoint" for r in self.plan.request.resources)) and self._output_followup_blocker():
                self.output_customization.setChecked(True)
                target = self.final_height if self.final_width.value() and not self.final_height.value() else self.final_width
            else:
                target = self.choose_resource_button if self.choose_resource_button.isEnabled() else self.model
            if target is self.model:
                self.expert_settings.setChecked(True)
        elif self.adapter is None:
            target = self.check_button
        elif not self.binding:
            target = self.bind_button
        elif not self.safety_ack.isChecked():
            target = self.safety_ack
        elif not self.submit_button.isEnabled():
            target = self.prepare_button
        else:
            target = self.submit_button
        scroll = self.tabs.currentWidget()
        if isinstance(scroll, QScrollArea) and scroll.isAncestorOf(target):
            reveal_after_change = target in (
                self.final_width, self.final_height, self.cfg_scale, self.model,
                self.model_hash, self.steps, self.sampler, self.scheduler,
                self.goal, self.negative,
            )
            if reveal_after_change:
                content = scroll.widget()
                if content is not None and content.layout() is not None:
                    content.layout().activate()
                scroll.ensureWidgetVisible(target, 0, 24)
            else:
                scroll.ensureWidgetVisible(target)
        target.setFocus(Qt.OtherFocusReason)
        if isinstance(scroll, QScrollArea) and scroll.isAncestorOf(target) and reveal_after_change:
            # Disclosures can change the Request height after this click returns.
            # Repeat once only in the same page; later navigation must win.
            def reveal_after_layout():
                if not self._closed and self.tabs.currentWidget() is scroll and target.isVisible():
                    scroll.ensureWidgetVisible(target, 0, 24)
            QTimer.singleShot(0, reveal_after_layout)

    def _run(self, key: str, call, done, failed=None) -> None:
        if self._pending or self._shutdown_requested:
            if failed is not None:
                failed("The generation workspace is busy or closing. Retry after its current work finishes.")
            return
        generation = self._generation = self._generation + 1
        task = CoordinatedCallTask(
            self.jobs, f"Generation dialog: {key}", lambda _job: call(),
            resource="io", key=f"generation-dialog:{id(self)}:{generation}:{key}",
        )
        self._pending[key] = (task, generation, done)
        if failed is not None:
            self._failure_callbacks[key] = failed
        task.signals.complete.connect(
            lambda result, task=task, key=key, generation=generation, done=done:
            self._io_completed.emit((key, task, generation, done, result))
        )
        task.signals.failed.connect(
            lambda error, task=task, key=key, generation=generation:
            self._io_failed.emit((key, task, generation, str(error)))
        )
        task.signals.cancelled.connect(
            lambda task=task, key=key, generation=generation:
            self._io_failed.emit((key, task, generation, "The local operation was cancelled."))
        )
        try:
            task.start()
        except Exception as exc:
            self._pending.pop(key, None)
            self._failure_callbacks.pop(key, None)
            if key == "autosave":
                self._draft_close_pending = self._draft_close_workspace = False
                self._draft_after_save = None
                self._draft_save_error = True
                self.draft_status.setText("Draft could not be saved. Edits remain open; use Save Draft As.")
            self.status.setText(str(exc) or "The shared job coordinator is unavailable.")
            if failed is not None:
                failed(str(exc) or "The shared job coordinator is unavailable.")
            self._update_actions()
            self._notify_background_work_finished()
            return
        self._update_actions()

    def _deliver_io_completion(self, payload: object) -> None:
        key, task, generation, done, result = payload
        saved = self._pending.get(key)
        if saved is None or saved[0] is not task:
            return
        self._pending.pop(key, None)
        self._failure_callbacks.pop(key, None)
        if not self._closed and generation == self._generation and (
                not self._shutdown_requested or key in {"submit", "recovery", "autosave"}):
            done(result)
        self._update_actions()
        self._notify_background_work_finished()

    def _deliver_io_failure(self, payload: object) -> None:
        key, task, generation, error = payload
        saved = self._pending.get(key)
        if saved is None or saved[0] is not task:
            return
        self._pending.pop(key, None)
        failed = self._failure_callbacks.pop(key, None)
        if key == "autosave":
            self._draft_close_pending = self._draft_close_workspace = False
            self._draft_after_save = None
            self._draft_save_error = True
            self.draft_status.setText("Draft could not be saved. Your edits remain open. Use Save Draft As or retry closing.")
        if key == "submit":
            self._submission_terminal_seen = True
        if not self._closed and generation == self._generation:
            if key == "ingest":
                self._ingest_issue = error or "The retained result could not be verified."
            if key == "history":
                self.status.setText("Retained generation history is unavailable. Review local storage and retry Load Retained Jobs.")
            else:
                self.status.setText(error or f"{str(key).title()} did not complete.")
            if failed is not None:
                failed(error or f"{str(key).title()} did not complete.")
        self._update_actions()
        self._notify_background_work_finished()

    def _poll_io(self) -> None:
        # Completion is delivered by CoordinatedCallTask before the manager
        # clears its result. This timer remains a harmless compatibility hook.
        if not self._pending:
            self._io_timer.stop()

    def check_connection(self) -> None:
        if self._pending or self._execution_active(): return
        if self.connection_issue:
            self.status.setText(self.connection_issue)
            return
        from core.generation_execution import GenerationExecutionService, SwarmLocalAdapter
        endpoint = self.endpoint.text().strip()
        if not endpoint.startswith("http://127.0.0.1") and not endpoint.startswith("http://localhost"):
            self.status.setText("Choose a localhost endpoint before checking a generator."); return
        try:
            adapter = SwarmLocalAdapter(endpoint, memory_evidence_provider=
                GenerationExecutionService(self.storage_root, self.jobs).memory_evidence_provider())
        except ValueError as exc:
            self.status.setText(str(exc)); return
        self.adapter = None; self.binding = None; self.prepared = None
        self.status.setText("Checking the selected local connection…")
        def done(probe):
            self.adapter = adapter
            context = self.plan.target_context
            context.update(adapter_id=probe.get("adapter_id", ""), adapter_version=probe.get("version", ""), fingerprint=probe.get("fingerprint", ""))
            self.plan = self.plan.revise({"target_context": context}, source="probe", consequence="Local adapter identity checked")
            self.connection_detail.setPlainText(_plain_json(probe)); self.status.setText("Local connection checked. Bind an exact local model before preparing."); self._refresh_from_plan()
        self._run("probe", adapter.probe, done)

    def bind_local_model(self) -> None:
        if self._pending or self._execution_active() or self.adapter is None: return
        name, path = self.local_model_name.text().strip(), Path(self.local_model_path.text().strip())
        if not name or path.suffix.casefold() != ".safetensors":
            self.status.setText("Choose the exact generator model name and a local .safetensors file."); return
        self.status.setText("Binding the selected local model…")
        adapter = self.adapter
        self.binding = None; self.prepared = None
        self._run("binding", lambda: adapter.bind_model(name, path, self.registry), self._bound)

    def find_selected_model(self):
        if self._pending or self._execution_active() or self.adapter is None:
            self.status.setText("Check the local connection first, then find the selected main model.")
            return
        if not self.plan.request.model_hash or self.registry is None:
            self.status.setText("Choose a main model from Models first.")
            return
        adapter = self.adapter
        resource_id = "sha256:" + self.plan.request.model_hash.removeprefix("sha256:")
        selected = list(self.plan.request.resources)
        if not any(item.role == "checkpoint" for item in selected):
            selected.insert(0, GenerationResource(id=resource_id, role="checkpoint", hashes={"sha256": resource_id.removeprefix("sha256:")}))
        if any(item.role == "text_encoder" and not item.provenance.get("swarm_parameter") for item in selected):
            self.status.setText("In Review, select each text encoder and choose its purpose before binding.")
            self.tabs.setCurrentIndex(1)
            return
        self.binding = None
        self.prepared = None
        bindings = {}
        def next_resource(index):
            if index == len(selected):
                mains = [item for item in selected if item.role == "checkpoint"]
                if len(mains) != 1:
                    self.status.setText("Choose exactly one main model.")
                    return
                self.plan = self.plan.revise({"resources": [item.to_dict() for item in selected]}, source="binding")
                controls = [item for item in selected if item.role == "controlnet"]
                bundle = {**bindings[mains[0].id], "components": bindings}
                if len(controls) == 1:
                    bundle["conditioning"] = bindings[controls[0].id]
                self._bound(bundle)
                return
            item = selected[index]
            identity = "sha256:" + item.hashes.get("sha256", "").removeprefix("sha256:")
            self._run("model_discovery", lambda: adapter.discover_registered_model(identity, self.registry, role=item.role),
                lambda result: discovered(result, item, index))
        def discovered(result, item, index):
            names = [str(row["name"]) for row in result["candidates"]]
            if not names:
                self.status.setText("SwarmUI has no cached exact match for this model. Check its model folders and metadata, then retry.")
                return
            name = names[0]
            if len(names) > 1:
                name, accepted = QInputDialog.getItem(self, "Choose model location", "Several SwarmUI names have the same tensor bytes:", names, 0, False)
                if not accepted:
                    return
            identity = "sha256:" + item.hashes.get("sha256", "").removeprefix("sha256:")
            def bound(value):
                bindings[item.id] = value
                next_resource(index + 1)
            self._run("binding", lambda: adapter.bind_model(name, Path(result["path"]), self.registry,
                expected_resource_id=identity, role=item.role, slot=item.provenance.get("swarm_parameter", "")), bound)
        next_resource(0)

    def _bound(self, binding: Any) -> None:
        self.binding = _mapping(binding)
        resource_id = str(self.binding.get("resource_id", "")).removeprefix("sha256:")
        self.plan = self.plan.revise({"request": {"model": self.binding.get("name", ""), "model_hash": resource_id}}, source="binding", consequence="Exact local model binding selected")
        self._invalidate_output_plan()
        self.prepared = None; self.connection_detail.setPlainText(_plain_json(binding)); self._refresh_from_plan("Exact local model is bound. Review its visible name and hash, then prepare.")

    def prepare_request(self) -> None:
        if self._pending or self._execution_active(): return
        input_issue, _target = self._ordinary_input_issue()
        if input_issue:
            self.status.setText(input_issue); self._update_actions(); return
        blocker = self._output_followup_blocker()
        if blocker:
            self.status.setText(blocker); return
        if self.adapter is None or not self.binding: return
        if not self._seed_input_valid():
            self.status.setText("Choose a valid seed before preparing this request.")
            return
        self.status.setText("Preparing the reviewed request…")
        plan, binding, adapter = self.plan, deepcopy(self.binding), self.adapter
        self.prepared = None
        def prepare():
            current = self.source_current_provider() if callable(self.source_current_provider) else self.source_current
            if plan.source is not None and not callable(self.source_current_provider):
                raise ValueError("Reload the source identity before local execution.")
            adapter.source_current_provider = self.source_current_provider
            adapter.source_image_provider = self.source_image_provider
            adapter.reference_image_provider = self._reference_image
            result = adapter.prepare(plan, binding, self.registry, source_current=current)
            observation = None
            observe = getattr(adapter, "observe_resources", None)
            if callable(observe):
                try:
                    observation = observe()
                except Exception:
                    pass
            try:
                from core.generation_execution import GenerationExecutionService
                cost = GenerationExecutionService(self.storage_root, self.jobs).resource_guidance(result, observation)
            except Exception:
                cost = {"message": "Time and retained-file size estimates are unavailable."}
            assess = getattr(adapter, "assess_resources", None)
            if callable(assess):
                assessment = assess(result, observation)
            else:
                from core.generation_resource_guard import assess_memory_evidence
                assessment = assess_memory_evidence([], {}, observation)
            return result, observation, cost, assessment
        def done(value):
            if self.plan.to_dict() != plan.to_dict() or self.binding != binding or self.adapter is not adapter:
                self.status.setText("The request changed during preflight. Review and prepare it again.")
                return
            result, observation, cost, assessment = value
            self._memory_observation_plan_digest = self.plan.digest
            self._memory_observation_adapter = adapter
            self._memory_observation = deepcopy(observation)
            self._memory_assessment = deepcopy(assessment)
            self._memory_cost_message = cost["message"]
            self.memory_status.setText(self._memory_observation_text(observation) + "\n" + assessment["message"] + "\n" + cost["message"])
            self._prepared(result)
        self._run("prepare", prepare, done)

    @staticmethod
    def _memory_observation_text(observation):
        from core.generation_resource_guard import review_memory_observation
        observation = observation if isinstance(observation, dict) else {}
        current = review_memory_observation(observation)
        if current["reason"] == "current":
            return (f"Last observed free GPU memory: {current['available_vram_gib']:.2f} GiB "
                f"at {observation['observed_at']}. This reading expires after 30 seconds; fit is unproven. "
                "Memory and applicable evidence are checked again when queued work starts.")
        return current["message"] + " Exact request fit is unproven; queued work checks resources again."

    def _memory_submission_blocker(self):
        if (getattr(self, "_memory_observation_plan_digest", None) != self.plan.digest
                or getattr(self, "_memory_observation_adapter", None) is not self.adapter):
            return ""
        assessment = getattr(self, "_memory_assessment", {})
        if assessment.get("assessment") == "limited":
            # Aging a refusal must not silently enable Submit. Prepare refreshes
            # both the request and evidence; queue start independently rechecks.
            return assessment["message"] + " Use Check Reviewed Request again after resolving this limit."
        return ""

    def _prepared(self, prepared: Any) -> None:
        self.prepared = _mapping(prepared)
        valid = bool(self.prepared and self.prepared.get("valid", self.prepared.get("request") is not None))
        self.connection_detail.setPlainText(_plain_json(prepared))
        self.status.setText("Prepared request is ready for explicit submission; GPU memory fit remains unproven. Review the memory observation in Readiness." if valid else "Preflight blocked this request. Review the reported blockers before preparing again.")
        if valid and self._memory_submission_blocker():
            self.status.setText(self._memory_submission_blocker())
        if valid and self.binding:
            from core.generation_execution import GenerationExecutionService
            service = GenerationExecutionService(self.storage_root, self.jobs)
            self._run("guidance", lambda: service.guidance(resource_id=self.binding["resource_id"], adapter_version=self.prepared["capabilities"]["version"], goal=self.plan.goal), self._guidance_ready)

    def _guidance_ready(self, guidance: Any) -> None:
        value = _mapping(guidance)
        self.connection_detail.append("\nOutcome guidance (read-only):\n" + _plain_json(value))

    def _connection_changed(self, *_args) -> None:
        self.adapter = None; self.binding = None; self.prepared = None
        self._invalidate_output_plan()
        self._generation += 1; self.status.setText("Connection details changed. Check and bind again before preparing.")
        self._update_actions()

    def _binding_changed(self, *_args) -> None:
        self.binding = None; self.prepared = None
        self._invalidate_output_plan()
        self._generation += 1
        self.status.setText("Model details changed. Bind the exact local model before preparing.")
        self._update_actions()

    def _advanced_json_pending(self) -> bool:
        return (self._advanced_synced_text is not None and
            self.advanced_json.toPlainText() != self._advanced_synced_text)

    def _advanced_json_changed(self) -> None:
        self._update_advanced_pending()
        self._update_actions()

    def _update_advanced_pending(self) -> None:
        pending = self._advanced_json_pending()
        stale = pending and self._advanced_base_digest != self.plan.digest
        self.advanced_pending_note.setVisible(pending)
        self.discard_advanced_button.setVisible(pending)
        self.apply_advanced_button.setEnabled(pending)
        self.apply_advanced_button.setText(
            "Review Replacement…" if stale else "Apply Validated JSON")
        self.advanced_pending_note.setText(
            "Pending Advanced JSON is retained, but the current request changed after editing began. "
            "Review before replacing current choices, or discard this pending text. "
            "It is not saved or used for generation."
            if stale else
            "Pending Advanced JSON is not yet applied or saved. Apply it deliberately or discard it; "
            "the current request remains unchanged.")

    def _sync_advanced_json_from_plan(self, *, force=False) -> None:
        if self._advanced_json_pending() and not force:
            self._update_advanced_pending()
            return
        text = _plain_json(self.plan.to_dict())
        self.advanced_json.blockSignals(True)
        self.advanced_json.setPlainText(text)
        self.advanced_json.blockSignals(False)
        self._advanced_synced_text = text
        self._advanced_base_digest = self.plan.digest
        self._update_advanced_pending()

    def discard_advanced_json(self) -> None:
        if not self._advanced_json_pending():
            return
        choice = QMessageBox.warning(self, "Discard pending Advanced JSON?",
            "Replace only the unapplied Advanced JSON text with the current request? "
            "Other request edits remain in place.",
            QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel)
        if choice != QMessageBox.Discard:
            return
        self._sync_advanced_json_from_plan(force=True)
        self.status.setText("Pending Advanced JSON discarded. The current request is unchanged.")
        self._update_actions()

    def apply_advanced_json(self) -> None:
        try:
            supplied = json.loads(self.advanced_json.toPlainText())
            if not isinstance(supplied, dict):
                raise ValueError("Advanced JSON must be an object.")
            current = self.plan.to_dict()
            immutable = (
                "source_record", "source_identity", "source_fingerprint",
                "source_identity_fingerprint",
            )
            for key in immutable:
                if key in supplied and supplied[key] != current.get(key):
                    raise ValueError("The immutable source record or identity cannot be changed in advanced JSON.")
            full_plan = "request" in supplied
            request = supplied.get("request") if full_plan else supplied
            if not isinstance(request, dict):
                raise ValueError("The neutral request must be an object.")
            for key in ("raw", "source", "source_field"):
                if key in request and request[key] != current["request"].get(key):
                    raise ValueError("The immutable raw generator payload and source references cannot be changed in advanced JSON.")
            # The planner keeps these source-evidence fields in every revision;
            # equal displayed JSON is evidence, not an editable request field.
            request = {key: value for key, value in request.items() if key not in {"raw", "source", "source_field"}}
            if self._advanced_json_pending() and self._advanced_base_digest != self.plan.digest:
                choice = QMessageBox.warning(self, "Replace newer request choices?",
                    "The request changed after you began editing this Advanced JSON. Applying the pending "
                    "full-plan text may replace those newer choices. Choose Yes to replace the current "
                    "request or Cancel to keep both for review; nothing is merged automatically.",
                    QMessageBox.Yes | QMessageBox.Cancel, QMessageBox.Cancel)
                if choice != QMessageBox.Yes:
                    self.status.setText("Pending Advanced JSON retained. Review newer request choices before replacing them.")
                    return
            changes: dict[str, Any] = {"request": request}
            if full_plan:
                for key in ("goal", "intent", "knowledge_fragments", "safety_policy", "target_context"):
                    if key in supplied:
                        changes[key] = supplied[key]
            self.plan = self.plan.revise(
                changes, source="advanced_json",
                consequence="Validated neutral JSON edits applied; derived fingerprints and audit digest were rebuilt.",
            )
        except (GenerationPlanError, ValueError, TypeError, json.JSONDecodeError) as exc:
            self.status.setText(f"Advanced JSON was not applied: {exc}"); return
        self.prepared = None; self.binding = None
        self._sync_advanced_json_from_plan(force=True)
        self._refresh_from_plan("Validated neutral JSON edits were applied with a new audit diff. Bind and prepare again before submitting.")

    def submit_reviewed(self) -> None:
        if self._shutdown_requested:
            return
        if self.remix_source and (not callable(self.source_image_provider) or not any(
                item.kind in {"init_image", "reference_image", "control_image"} for stage in self.plan.request.stages for item in stage.inputs)):
            self.status.setText("This Remix draft cannot be submitted until its exact source image is reconnected.")
            return
        if self._pending or self._execution_active(): return
        input_issue, _target = self._ordinary_input_issue()
        if input_issue:
            self.status.setText(input_issue); self._update_actions(); return
        if not self.prepared or not self.safety_ack.isChecked() or not self._seed_input_valid(): return
        if self._memory_submission_blocker():
            self.status.setText(self._memory_submission_blocker()); return
        from core.generation_execution import GenerationExecutionService
        service = GenerationExecutionService(self.storage_root, self.jobs)
        self._cancellation_requested = False
        self._submission_terminal_seen = False
        self._submission_terminal_state = ""
        self._submission_generation = self._generation + 1
        submission_generation = self._submission_generation
        self._submitted_progress = None
        self._submitted_job_id = ""
        adapter, prepared = self.adapter, deepcopy(self.prepared)
        self.receipt = None; self._ingest_issue = ""; self._ingest_folder_recovery = False
        self.status.setText("Submitting the reviewed request…")
        def listener(job) -> None:
            # The coordinator drops completed results after notifying observers.
            # Capture the durable receipt now and marshal it onto the Qt thread.
            self._submission_completed.emit({
                "manager_job_id": str(getattr(job, "id", "")),
                "submission_generation": submission_generation,
                "state": str(getattr(job, "state", "")),
                "receipt": getattr(job, "result", None),
                "error": str(getattr(job, "error", "")),
            })
        def done(progress):
            self._submitted_progress = progress
            self._submitted_job_id = str(getattr(progress, "id", "") or getattr(progress, "job_id", "") or _mapping(progress).get("job_id", ""))
            self.cancel_button.setEnabled(bool(self._submitted_job_id)); self._progress_timer.start()
            if self._shutdown_requested:
                self._request_submitted_shutdown_cancel()
            if self.receipt is None:
                self.status.setText("Request submitted. Progress remains attached to its durable receipt.")
            self._poll_submission()
        self._run("submit", lambda: service.submit(adapter, prepared, confirmed=True, listener=listener), done)

    def _receive_submission_completion(self, event: object) -> None:
        if self._closed:
            return
        payload = _mapping(event)
        if payload.get("submission_generation", self._submission_generation) != self._submission_generation:
            return
        if (self._submitted_job_id and payload.get("manager_job_id")
                and payload["manager_job_id"] != self._submitted_job_id):
            return
        if payload.get("state") in {"complete", "failed", "cancelled"}:
            self._submission_terminal_seen = True
            self._submission_terminal_state = payload["state"]
        receipt = payload.get("receipt")
        if isinstance(receipt, dict):
            self.receive_receipt(receipt)
            self._notify_background_work_finished()
            return
        if payload.get("state") in {"failed", "cancelled"}:
            self.status.setText(payload.get("error") or "Local generation ended without a verified receipt. Load retained jobs to recover its current receipt.")
        self._notify_background_work_finished()

    def _poll_submission(self) -> None:
        job = self._submitted_progress
        getter = getattr(self.jobs, "get", None)
        if callable(getter) and self._submitted_job_id:
            job = getter(self._submitted_job_id) or job
        if job is None: return
        self._submitted_progress = job
        state = self._submission_state()
        message = str(getattr(job, "message", ""))
        current, total = getattr(job, "current", 0), getattr(job, "total", 0)
        if (message or total) and not (state == "complete" and self.receipt):
            self.status.setText(f"{state.title()}: {message or 'Working locally'}" + (f" ({current}/{total})" if total else ""))
        if state in {"complete", "failed", "cancelled"}:
            self._progress_timer.stop(); self.cancel_button.setEnabled(False)
            if state == "failed": self.status.setText(str(getattr(job, "error", "")) or "Local generation failed; inspect its retained receipt.")
        self._update_actions()
        self._notify_background_work_finished()

    def cancel_submission(self) -> None:
        cancel = getattr(self.jobs, "cancel", None)
        if callable(cancel) and self._submitted_job_id:
            cancel(self._submitted_job_id)
            self._cancellation_requested = True
        self.cancel_button.setEnabled(False); self.status.setText("Cancellation requested. The eventual outcome will still be verified before Ingest.")
        self._update_actions()

    def _request_submitted_shutdown_cancel(self) -> None:
        if (not self._submitted_job_id or self._cancellation_requested
                or self._submission_state() not in
                {"queued", "running", "submitting", "cancelling"}):
            return
        cancel = getattr(self.jobs, "cancel", None)
        if callable(cancel):
            cancel(self._submitted_job_id)
            self._cancellation_requested = True
            self.cancel_button.setEnabled(False)
            self.status.setText("Closing after the submitted image job reaches its retained outcome…")

    # Pure entry points used by callers and tests. UI file pickers use worker wrappers.
    @staticmethod
    def _file_digest(path: Path) -> str:
        return hashlib.sha256(path.read_bytes()).hexdigest()

    def _save_path(self, path: str | Path, plan: GenerationPlan | None = None) -> tuple[Path, str]:
        target = Path(path).absolute()
        digest = save_plan(target, plan or self.plan, expected_digest=self._save_tokens.get(target))
        return target, digest

    def _remember_saved(self, target: Path, digest: str) -> None:
        self._save_tokens[target.absolute()] = digest

    def save_to(self, path: str | Path):
        target, digest = self._save_path(path)
        self._remember_saved(target, digest)
        self._draft_saved_digest = self.plan.digest
        self._draft_save_error = False
        return digest

    def _apply_loaded_plan(self, plan: GenerationPlan, path: str | Path, digest: str | None = None) -> GenerationPlan:
        target = Path(path).absolute()
        if digest is None:
            checked, digest = load_plan_with_digest(target)
            if checked.digest != plan.digest:
                raise ValueError("The draft changed while loading. Open it again before editing.")
        self.plan = plan; self.prepared = None; self.binding = None; self.receipt = None; self.adapter = None
        self._sync_advanced_json_from_plan(force=True)
        self._output_preview = self._output_preview_plan = None
        self.remix_source = any(item.kind == "init_image" for stage in plan.request.stages for item in stage.inputs)
        self._history_context = False
        self._remember_saved(target, digest)
        self._draft_saved_digest = plan.digest
        self._draft_save_error = False
        self.draft_status.setText("Saved draft loaded · changes will be saved locally for Resume Image Draft.")
        self._generation += 1; self._refresh_from_plan("Loaded plan. Review and prepare it again before submitting.")
        return self.plan

    def load_from(self, path: str | Path):
        target = Path(path).absolute()
        plan, token = load_plan_with_digest(target)
        return self._apply_loaded_plan(plan, target, token)
    def export_bytes(self) -> bytes: return export_plan(self.plan)
    def receive_receipt(self, receipt: dict[str, Any]) -> None:
        previous_id = str((self.receipt or {}).get("job_id") or "")
        next_id = str(_mapping(receipt).get("job_id") or "")
        if previous_id != next_id:
            if previous_id:
                self._outcome_drafts[previous_id] = (
                    self.outcome_toggle.isChecked(), str(self.outcome_kind.currentData()),
                    self.outcome_goal.text(), self.outcome_reasons.toPlainText(),
                    deepcopy(self.ingest_identity),
                )
            draft = self._outcome_drafts.get(next_id)
            self.outcome_toggle.blockSignals(True)
            self.outcome_toggle.setChecked(bool(draft and draft[0]))
            self.outcome_toggle.blockSignals(False)
            self.outcome_kind.setCurrentIndex(max(0, self.outcome_kind.findData(draft[1])) if draft else 0)
            self.outcome_goal.setText(draft[2] if draft else "")
            self.outcome_reasons.setPlainText(draft[3] if draft else "")
            self.ingest_identity = deepcopy(draft[4]) if draft else {}
        self.result_preview.clear("Preview this opened job after its result is checked")
        self.result_preview.hide()
        self.receipt = _mapping(receipt)
        self._ingest_issue = ""
        self._ingest_folder_recovery = False
        outputs = self.receipt.get("outputs") or []
        if self.receipt.get("state") == "verified" and outputs:
            self.status.setText("Verified results are ready to send through Ingest.")
        else: self.status.setText("The generator outcome needs review before results can enter Ingest.")
        self._update_actions()

    def review_memory_recovery(self) -> None:
        """Return to editable choices without revising or resubmitting failed work."""
        receipt = self.receipt or {}
        if (self._pending or self._execution_active()
                or receipt.get("failure_class") not in {"out_of_memory", "resource_limited"}
                or not (receipt.get("proven_unsubmitted") is True or receipt.get("server_stopped") is True)):
            return
        self.tabs.setCurrentIndex(0)
        self.generation_goal_preset.setFocus()
        page = self.tabs.widget(0)
        if isinstance(page, QScrollArea):
            page.ensureWidgetVisible(self.output_summary)
        self.status.setText("Request retained. Close other memory-heavy work or choose Lower Memory and preview its smaller base and faithful final size. Quick Preview only skips finishing. Review every change and prepare again before submitting.")

    def record_outcome(self) -> None:
        if self._selected_job_needs_open() or self._pending:
            self.status.setText("Open or recover the selected job before recording its outcome.")
            return
        if not self.receipt:
            self.status.setText("A retained generator receipt is needed before recording an outcome review."); return
        job_id = str(self.receipt.get("job_id") or self._submitted_job_id)
        if not job_id:
            self.status.setText("The receipt has no durable job identity to review."); return
        from core.generation_execution import GenerationExecutionService
        service = GenerationExecutionService(self.storage_root, self.jobs)
        details = self.outcome_goal.text().strip()
        reasons = self.outcome_reasons.toPlainText().strip()
        outcome = str(self.outcome_kind.currentData())
        identity = {**self.ingest_identity, "goal_detail": details}
        self.status.setText("Recording the explicit outcome review…")
        self._run(
            "outcome",
            lambda: service.record_outcome(
                job_id, outcome, reasons,
                ingest_identity=identity, ingest_history=self.ingest_history,
            ),
            lambda outcome: self.status.setText("Outcome review recorded for the verified Ingest copy."),
        )
    def send_verified_to_ingest(self) -> None:
        if (self._pending or self._ingest_issue or self._selected_job_needs_open()
                or not self.receipt or self.receipt.get("state") != "verified"):
            return
        from core.generation_execution import GenerationExecutionService
        service = GenerationExecutionService(self.storage_root, self.jobs)
        receipt = deepcopy(self.receipt)
        self.set_ingest_folder_recovery(False)

        def checked(paths):
            if self.receipt != receipt or self._selected_job_needs_open():
                self.status.setText("The selected result changed. Review it before sending to Ingest.")
                return
            self.ingest_requested.emit(paths)

        self.status.setText("Rechecking the verified image and Image Knowledge before Ingest…")
        self._run("ingest", lambda: service.verified_output_paths(receipt), checked)

    def set_ingest_folder_recovery(self, needed: bool) -> None:
        self._ingest_folder_recovery = bool(needed)
        self._update_actions()

    def set_source_current(self, identity: dict[str, Any]) -> None:
        """Supply a freshly resolved source identity before local preparation."""
        self.source_current = _mapping(identity)
        self.prepared = None; self._update_actions()

    def set_ingest_identity(self, identity: dict[str, Any]) -> None:
        """Receive normal-Ingest's successful identity mapping for an outcome receipt."""
        self.ingest_identity = _mapping(identity)

    def _choose_ingested_result(self) -> None:
        path, _ = QFileDialog.getOpenFileName(self, "Choose Ingested Result", "", "Images (*.png)")
        if path:
            self.set_ingest_identity({"path": path})
            self.status.setText("Selected a result to check. Record Outcome Review verifies its Ingest transfer, lineage and bytes.")

    def load_history(self, *, on_open: bool = False) -> None:
        from core.generation_execution import GenerationExecutionService
        service = GenerationExecutionService(self.storage_root, self.jobs)
        def done(receipts):
            previous = self.history_choice.currentData()
            previous_id = str(previous.get("job_id") or "") if isinstance(previous, dict) else ""
            opened_id = str((self.receipt or {}).get("job_id") or "")
            self.history_choice.clear()
            for receipt in receipts:
                request = receipt.get("prepared", {}).get("plan", {}).get("request", {})
                plan = receipt.get("prepared", {}).get("plan", {})
                title = str(plan.get("goal") or request.get("positive_prompt") or "Image generation").replace("\n", " ")[:70]
                self.history_choice.addItem(f"{title} · {receipt.get('state', 'unknown').replace('_', ' ')}", receipt)
            if receipts:
                for preferred_id in (previous_id, opened_id):
                    index = next((index for index in range(self.history_choice.count())
                        if preferred_id and isinstance(self.history_choice.itemData(index), dict)
                        and str(self.history_choice.itemData(index).get("job_id") or "") == preferred_id), None)
                    if index is not None:
                        self.history_choice.setCurrentIndex(index)
                        break
                if self._selected_job_needs_open():
                    self.result_preview.clear("Open the selected job before previewing it")
                    self.result_preview.hide()
                self._history_context = True
                self.status.setText("Retained generation jobs loaded. Select one to recover its current receipt.")
                if on_open:
                    self.tabs.setCurrentIndex(3)
                    scroll = self.tabs.currentWidget()
                    if isinstance(scroll, QScrollArea):
                        scroll.ensureWidgetVisible(self.history_choice)
                    self.history_choice.setFocus(Qt.OtherFocusReason)
            else:
                self.status.setText("No retained generation jobs are available. Start a new request, or load a saved plan.")
        self._run("history", service.history, done)

    def recover_selected(self) -> None:
        receipt = self.history_choice.currentData()
        if not isinstance(receipt, dict) or not receipt.get("job_id"):
            self.status.setText("Load and select a retained job first."); return
        from core.generation_execution import GenerationExecutionService
        service = GenerationExecutionService(self.storage_root, self.jobs)
        self.status.setText("Reconciling the retained local session without resubmitting…")
        expected_id = str(receipt["job_id"])
        def opened(current):
            if str(_mapping(current).get("job_id") or "") != expected_id:
                self.status.setText("Recovery returned a different job. Select and open the intended job again.")
                return
            self.receive_receipt(current)
        self._run("recovery", lambda: service.recover(expected_id, adapter=self.adapter), opened,
                  lambda error: self.status.setText(
                      f"Could not open the selected job. {error} Retry Open or Recover Selected Job."))

    def _execution_active(self) -> bool:
        return (any(child.has_active_background_work() for child in self._finishing_dialogs)
            or bool(self._experiment_dialog and self._experiment_dialog.busy()) or "submit" in self._pending or self._submission_state() in {
            "queued", "running", "submitting", "cancelling",
        } or bool(self._submitted_job_id and not self._submission_terminal_seen))

    def _submission_state(self) -> str:
        if self._submission_terminal_seen and self._submission_terminal_state:
            return self._submission_terminal_state
        return str(getattr(self._submitted_progress, "state", ""))

    def has_active_background_work(self) -> bool:
        if self._prompt_templates_dialog and self._prompt_templates_dialog.has_active_background_work():
            return True
        if self._saved_work_dialog and self._saved_work_dialog.has_active_background_work():
            return True
        if any(child.has_active_background_work() for child in self._finishing_dialogs):
            return True
        if self._experiment_dialog and self._experiment_dialog.busy():
            return True
        if self._pending:
            return True
        if self._submitted_job_id:
            state = self._submission_state()
            return state not in {"complete", "failed", "cancelled"} or not self._submission_terminal_seen
        return False

    def request_background_shutdown(self) -> bool:
        """Wait for real coordinator completion; cancel only the owned submitted job."""
        self._shutdown_requested = True
        self._background_shutdown_notified = False
        if self._prompt_templates_dialog:
            self._prompt_templates_dialog.request_background_shutdown()
        if self._saved_work_dialog:
            self._saved_work_dialog.request_background_shutdown()
        if self._experiment_dialog:
            self._experiment_dialog.request_shutdown()
        for child in self._finishing_dialogs:
            child.request_background_shutdown()
        self._request_submitted_shutdown_cancel()
        self._notify_background_work_finished()
        return not self.has_active_background_work()

    def resume_after_cancelled_shutdown(self) -> bool:
        """Restore editing if another workspace vetoed application close."""
        if self.has_active_background_work():
            return False
        self._shutdown_requested = False
        self._background_shutdown_notified = False
        self._update_actions()
        return True

    def _notify_background_work_finished(self) -> None:
        if (self._shutdown_requested and not self._background_shutdown_notified
                and not self.has_active_background_work()):
            self._background_shutdown_notified = True
            self.background_work_finished.emit()

    def _finish_workspace(self) -> None:
        if self._closed or self.has_active_background_work():
            return
        self._draft_timer.stop()
        self.workspace_closed.emit()
        self._closed = True; self._generation += 1; self._io_timer.stop(); self._progress_timer.stop()

    def reject(self) -> None:
        if self.has_active_background_work():
            self.hide()
            return
        self._draft_close_workspace = True
        if not self.confirm_draft_loss():
            if not self._draft_close_pending: self._draft_close_workspace = False
            return
        self._finish_workspace()
        super().reject()

    def closeEvent(self, event) -> None:
        if self.has_active_background_work():
            event.ignore()
            self.hide()
            return
        self._draft_close_workspace = True
        if not self.confirm_draft_loss():
            if not self._draft_close_pending: self._draft_close_workspace = False
            event.ignore()
            return
        self._finish_workspace()
        super().closeEvent(event)

    def _choose_setup(self) -> None:
        """Publish the existing plan before leaving; never initiate runtime work."""
        if not self.setup_button.isEnabled():
            return
        try:
            if self._request_field_values() != self._displayed_request:
                raise ValueError("Complete valid request values before opening Setup.")
            if json.loads(self.advanced_json.toPlainText()) != self.plan.to_dict():
                raise ValueError("Apply or discard the pending Advanced changes before opening Setup.")
        except (ValueError, TypeError) as exc:
            self.status.setText(str(exc) or "Complete the request values before opening Setup.")
            return
        path, _ = QFileDialog.getSaveFileName(
            self, "Save Generation Plan Before Setup",
            str(self.storage_root / "generation-plan.json"), "Generation plan (*.json)",
        )
        if not path:
            return
        snapshot = self.plan
        def saved(result):
            self._remember_saved(*result)
            self._draft_saved_digest = snapshot.digest
            self._draft_save_error = False
            try:
                if self._request_field_values() != self._displayed_request:
                    raise ValueError("Complete valid request values and save again before opening Setup.")
                current = self.plan.to_dict()
                if current != snapshot.to_dict() or json.loads(self.advanced_json.toPlainText()) != current:
                    raise ValueError("The request changed while saving. Save the current plan again before opening Setup.")
            except (ValueError, TypeError) as exc:
                self.status.setText(str(exc))
                return
            self.adapter = None; self.binding = None; self.prepared = None
            self.status.setText("Plan saved. Return from Setup to check and prepare this request again.")
            self.setup_requested.emit()
        self._run("save-for-setup", lambda: self._save_path(path, snapshot), saved)

    def resume_from_setup(self, endpoint: str = "", connection_issue: str = "") -> None:
        """Reuse the exact draft and source guard, with fresh execution review."""
        if endpoint:
            from core.generation_transport import local_endpoint
            self.endpoint.setText(local_endpoint(endpoint))
        self.connection_issue = str(connection_issue or "")
        self.adapter = None; self.binding = None; self.prepared = None
        self._generation += 1
        self.readiness.setPlainText(_plain_json(assess_readiness(
            self.plan, "swarmui", registry=None, capabilities=None,
        )))
        self.connection_detail.clear()
        self.status.setText(self.connection_issue or "Returned from Setup. On Readiness, check your generator, model and saved request again.")
        self._update_actions()
        self.tabs.setCurrentIndex(4)
        self.show(); self.raise_(); self.activateWindow()
        self.tabs.currentWidget().ensureWidgetVisible(self.check_button)
        self.check_button.setFocus(Qt.OtherFocusReason)

    def _choose_save(self):
        if not self._draft_inputs_valid():
            self.status.setText("Complete invalid values or apply the Advanced edits before saving this draft.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Save Generation Plan", str(self.storage_root / "generation-plan.json"), "Generation plan (*.json)")
        if path:
            snapshot = self.plan
            def saved(result):
                self._remember_saved(*result)
                self._draft_saved_digest = snapshot.digest
                self._draft_save_error = False
                self.draft_status.setText("Draft saved to your chosen file · reopen it with Open Draft.")
                self.status.setText("Draft saved with a read-back check.")
            self._run("save", lambda: self._save_path(path, snapshot), saved)
    def _choose_load(self):
        if not self.confirm_draft_loss(after_saved=self._choose_load): return
        path, _ = QFileDialog.getOpenFileName(self, "Load Generation Plan", str(self.storage_root), "Generation plan (*.json)")
        if path:
            target = Path(path).absolute()
            self._run("load", lambda: load_plan_with_digest(target), lambda result: self._apply_loaded_plan(result[0], target, result[1]))
    def _choose_export(self):
        if not self._draft_inputs_valid():
            self.status.setText("Complete invalid values or apply the Advanced edits before exporting.")
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export Generation Request", str(self.storage_root / "generation-request.json"), "JSON (*.json)")
        if path:
            snapshot = self.plan
            self._run("export", lambda: self._save_path(path, snapshot), lambda result: (self._remember_saved(*result), self.status.setText("Complete request exported with recovery evidence.")))
