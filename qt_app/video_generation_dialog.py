"""Retained, explicit T2V/I2V workspace for an owned local Comfy runtime."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import datetime, timezone
from fractions import Fraction
import json
from pathlib import Path
from types import SimpleNamespace
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QFileDialog, QFormLayout, QGroupBox,
    QGridLayout, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QTabBar,
    QSizePolicy, QSpinBox, QVBoxLayout, QWidget,
)

from core.comfy_video_runtime import (
    RuntimeConfig, RuntimeModel, preview_runtime, start_runtime, stop_runtime,
    verify_runtime,
)
from core.comfy_video_resources import QUALIFIED_MODELS
from core.generation_execution import digest
from core.image_record import ImageRecord
from core.model_resource_registry import ModelResourceError
from core.model_organization import normalize_model_family
from core.model_selection import protected_model_roots
from core.training_source_io import read_stable_training_source
from core.video_generation_draft import VideoAuthoringDraft, load_video_draft, save_video_draft
from core.video_runtime_preferences import load_video_runtime_preferences, save_video_runtime_preferences
from core.video_decoder import verify_video_output
from core.video_generation import ImageSourceReference, Rational, VideoGenerationRequest, VideoSourceAsset
from core.video_generation_execution import VideoExecutionService
from core.video_output_goal import validate_video_output_goal
from core.video_source_image import VideoSourceImage, prepare_video_source
from core.generation_output_guidance import plan_output_resolution
from .components import DisclosureButton
from .workers import CoordinatedCallTask
from . import gallery


class _RuntimeDetailLabel(QLabel):
    """Keep changing wrapped runtime details tall enough inside a scrolled form."""
    def setText(self, text):
        super().setText(text)
        self._fit_height()

    def resizeEvent(self, event):
        super().resizeEvent(event)
        self._fit_height()

    def _fit_height(self):
        required = max(self.fontMetrics().height(), self.heightForWidth(max(1, self.width())))
        if self.minimumHeight() != required:
            self.setMinimumHeight(required)


class _ModelIdentityField(QLineEdit):
    """Copyable exact identity in the optional model-details disclosure."""

    def __init__(self):
        super().__init__("None selected")
        self.setReadOnly(True)
        self.setText("None selected")

    def setText(self, identity):
        super().setText(identity)
        self.setCursorPosition(0)
        self.setToolTip(identity if identity != "None selected" else
            "Choose a registered model to see its exact resource identity.")


def _wan_recipe_declarations(component):
    """The current Video recipe's selectable identity, roles and family."""
    sha256 = dict(QUALIFIED_MODELS)[component]
    roles = {"transformer": ("unet", "checkpoint"),
             "text_encoder": ("text_encoder",), "vae": ("vae",)}[component]
    return tuple(("sha256:" + sha256, role, "Wan2.2") for role in roles)


def _wan_recipe_selected_role(component, resource_id, record):
    if not isinstance(record, dict) or resource_id != _wan_recipe_declarations(component)[0][0]:
        return ""
    for role in ("unet", "checkpoint") if component == "transformer" else (component,):
        families = {row.get("family") for row in record.get("declarations", ())
                    if isinstance(row, dict) and row.get("role") == role
                    and normalize_model_family(row.get("family")) == "Wan2.2"}
        if families:
            return role if len(families) == 1 else ""
    return ""


def _wan_recipe_choice_matches(component, resource_id, record):
    return bool(_wan_recipe_selected_role(component, resource_id, record))


class VideoGenerationDialog(QDialog):
    """Reviewable video draft; all hashing, decoding and runtime work is off-UI."""

    workspace_closed = Signal()
    draft_save_finished = Signal()
    background_work_finished = Signal()
    ingest_requested = Signal(object)
    frame_ingest_requested = Signal(object)
    job_finished = Signal(object)

    def __init__(self, jobs, registry, settings, storage_root, parent=None, *,
                 adapter_factory=None, verify_output=None, pinned_wan_resource_id="",
                 runtime_binding=None, decoder_lifetime=None):
        super().__init__(parent)
        self.jobs, self.registry, self.settings = jobs, registry, settings
        self.storage_root = Path(storage_root).absolute()
        self.adapter_factory = adapter_factory
        self.pinned_wan_resource_id = pinned_wan_resource_id
        self.runtime_binding, self.decoder_lifetime = runtime_binding, decoder_lifetime
        self._video_output_preview = None
        self._verify_output = verify_output or (lambda path, request: verify_video_output(
            path, request, allowed_root=self.storage_root / "jobs",
            runtime_binding=self.runtime_binding, decoder_lifetime=self.decoder_lifetime))
        self.service = VideoExecutionService(self.storage_root / "jobs", jobs,
            verify_output=self._verify_output, registry=self.registry,
            protected_roots=protected_model_roots(self.settings))
        self._task = None
        self._task_label = ""
        self._task_cancelable = False
        self._source_path: Path | None = None
        self._source_raw: bytes | None = None
        self._source_ref: ImageSourceReference | None = None
        self._source_image: QImage | None = None
        self._source_snapshot: dict | None = None
        self._frame_reference_provider = None
        self._source_conversion: VideoSourceImage | None = None
        self._source_preview_key = None
        self._models = {"transformer": "", "text_encoder": "", "vae": ""}
        self._model_locations = {}
        self._model_roles = {"transformer": "unet", "text_encoder": "text_encoder", "vae": "vae"}
        self._runtime_preview = None
        self._runtime_receipt = None
        self._runtime_stop_only = False
        self._runtime_storage_dialog = None
        self._runtime_storage_locator = None
        self._runtime_storage_status = None
        self._runtime_lifecycle = None
        self._runtime_start_uncertain = False
        self._adapter = None
        self._job_adapter = None
        self._prepared = None
        self._request = None
        self._submitted_job = None
        self._job_id = ""
        self._verified_receipt = None
        self._memory_context_revision = 0
        self._job_memory_context_revision = None
        self._loaded_request: VideoGenerationRequest | None = None
        self._loaded_authoring: VideoAuthoringDraft | None = None
        self._loaded_future: VideoGenerationRequest | None = None
        self._loaded_display_snapshot = None
        self._clean_draft_snapshot = None
        self._clean_setup_snapshot = None
        self._setup_digest = None
        self._draft_close_pending = False
        self._discarded_close_snapshot = None
        self._closed = False
        self._preview_player = None
        self._finishing_dialog = None
        self._pack_dialog = None
        self._shutdown_requested = False
        self._shutdown_notified = False
        self._shutdown_cancelled_task = ""
        self._shutdown_cancelled_job = ""
        self._shutdown_reconcile_needed = False
        self._shutdown_reconcile_failed = False
        self._shutdown_stop_failed = False
        self._shutdown_player_requested = False
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(100)
        self._shutdown_timer.timeout.connect(self._advance_shutdown)
        self._job_timer = QTimer(self)
        self._job_timer.setInterval(300)
        self._job_timer.timeout.connect(self._poll_job)
        self.job_finished.connect(self._job_terminal)
        self.setWindowTitle("Generate Video")
        self.setMinimumSize(900, 700)
        outer = QVBoxLayout(self)
        heading = QLabel("Generate a short, silent video")
        heading.setObjectName("sectionTitle")
        outer.addWidget(heading)
        intro = QLabel("Use one exact Wan2.2 TI2V-5B transformer, UMT5 text encoder and VAE in an isolated local Comfy process. Review the graph and any source crop before submitting. Verified clips return to Ingest unreviewed.")
        intro.setWordWrap(True)
        outer.addWidget(intro)
        self.section_bar = QTabBar()
        for title in ("Request and Output", "Model Components", "Local Video Setup"):
            self.section_bar.addTab(title)
        self.section_bar.setExpanding(False)
        self.section_bar.setAccessibleName("Video setup sections")
        outer.addWidget(self.section_bar)
        save_scope = QLabel(
            "Save Draft keeps the request and model choices together. Save Local Setup keeps local paths and limits separately."
        )
        save_scope.setWordWrap(True)
        save_scope.setTextFormat(Qt.PlainText)
        outer.addWidget(save_scope)
        self.setup_jump_button = QPushButton("Set Up Local Video…")
        self.setup_jump_button.setToolTip("Go to the existing ComfyUI and Python setup fields in this draft.")
        outer.addWidget(self.setup_jump_button, alignment=Qt.AlignLeft)
        scroll = QScrollArea()
        self.scroll = scroll
        scroll.setWidgetResizable(True)
        content = QWidget()
        form_stack = QVBoxLayout(content)
        scroll.setWidget(content)
        outer.addWidget(scroll, 1)

        request_box = QGroupBox("Video request")
        request_form = QFormLayout(request_box)
        self.request_form = request_form
        self.task_choice = QComboBox()
        self.task_choice.addItem("Text to video", "t2v")
        self.task_choice.addItem("Image to video", "i2v")
        self.task_choice.currentIndexChanged.connect(self._task_changed)
        request_form.addRow("Task", self.task_choice)
        self.source_button = QPushButton("Choose source image…")
        self.source_button.clicked.connect(self.choose_source)
        self.source_label = QLabel("No source image chosen")
        self.source_label.setWordWrap(True)
        safety_policy = gallery._content_safety_policy
        self.source_preview = gallery.ProtectedImageView(policy=safety_policy,
            settings=settings if safety_policy is not None else SimpleNamespace(gallery_blur_unknown=True))
        self.source_preview.clear("Source preview")
        self.source_preview.setMinimumSize(240, 140)
        self.native_source_preview = gallery.ProtectedImageView(policy=safety_policy,
            settings=settings if safety_policy is not None else SimpleNamespace(gallery_blur_unknown=True))
        self.native_source_preview.clear("Native frame input preview")
        self.native_source_preview.setMinimumSize(240, 140)
        self.native_source_label = QLabel("Choose a source to review the native crop and resize.")
        self.native_source_label.setWordWrap(True)
        self.native_source_label.setTextFormat(Qt.PlainText)
        source_row = QHBoxLayout()
        source_row.addWidget(self.source_button)
        source_row.addWidget(self.source_label, 1)
        self._source_still_row = request_form.rowCount()
        request_form.addRow("Source still", source_row)
        previews = QHBoxLayout()
        original_column = QVBoxLayout()
        original_column.addWidget(QLabel("Original source · unchanged"))
        original_column.addWidget(self.source_preview)
        previews.addLayout(original_column, 1)
        native_column = QVBoxLayout()
        native_column.addWidget(QLabel("Native frame input · crop and resize preview"))
        native_column.addWidget(self.native_source_preview)
        native_column.addWidget(self.native_source_label)
        previews.addLayout(native_column, 1)
        self.source_review = QWidget()
        self.source_review.setLayout(previews)
        self._source_review_row = request_form.rowCount()
        request_form.addRow("Source review", self.source_review)
        self.loaded_draft_note = QLabel()
        self.loaded_draft_note.setWordWrap(True)
        self.loaded_draft_note.setTextFormat(Qt.PlainText)
        self.loaded_draft_note.hide()
        self._loaded_draft_row = request_form.rowCount()
        request_form.addRow("Loaded draft", self.loaded_draft_note)
        request_form.setRowVisible(self._loaded_draft_row, False)
        self.positive = QPlainTextEdit()
        self.positive.setPlaceholderText("Describe the visible scene and motion")
        self.positive.setMaximumHeight(95)
        self.negative = QPlainTextEdit()
        self.negative.setMaximumHeight(65)
        request_form.addRow("Prompt", self.positive)
        self.request_summary = QLabel()
        self.request_summary.setWordWrap(True)
        self.request_summary.setTextFormat(Qt.PlainText)
        request_form.addRow(self.request_summary)
        request_details = QWidget()
        details_form = QFormLayout(request_details)
        details_form.addRow("Negative prompt", self.negative)
        self.motion = QLineEdit()
        self.camera = QLineEdit()
        self.motion.setPlaceholderText("Optional planning note; separate native control unavailable")
        self.camera.setPlaceholderText("Optional planning note; separate native control unavailable")
        details_form.addRow("Motion intent", self.motion)
        details_form.addRow("Camera intent", self.camera)
        self.append_intent = QCheckBox("Append motion and camera wording to the prompt I review")
        self.append_intent.setToolTip("The native 5B graph has no separate motion or camera control. This edits prompt text only when checked.")
        details_form.addRow("Prompt mapping", self.append_intent)
        self.frame_width = QSpinBox(); self.frame_width.setRange(32, 1280); self.frame_width.setSingleStep(32); self.frame_width.setValue(1280)
        self.frame_height = QSpinBox(); self.frame_height.setRange(32, 1280); self.frame_height.setSingleStep(32); self.frame_height.setValue(704)
        self.frames = QSpinBox(); self.frames.setRange(1, 49); self.frames.setSingleStep(4); self.frames.setValue(49)
        self.fps = QSpinBox(); self.fps.setRange(1, 120); self.fps.setValue(24)
        self.seed = QLineEdit("0")
        self.seed.setPlaceholderText("Exact nonnegative integer seed")
        for label, widget in (("Width", self.frame_width), ("Height", self.frame_height),
                              ("Frames (4n+1)", self.frames), ("Frames per second", self.fps),
                              ("Seed", self.seed)):
            details_form.addRow(label, widget)
        native_limit = QLabel("Qualified native Wan 5B recipe: up to 49 frames and a 1280 × 704 pixel canvas "
            "(either side up to 1280). Longer or larger native clips need separate memory qualification; "
            "the desired final size below can use faithful resizing.")
        native_limit.setWordWrap(True); native_limit.setTextFormat(Qt.PlainText)
        details_form.addRow(native_limit)
        self.request_details_button = DisclosureButton("Customize Video Request",
            "Hide Video Request Settings", request_details, self)
        request_form.addRow(self.request_details_button)
        request_form.addRow(request_details)
        form_stack.addWidget(request_box)

        output_box = QGroupBox("Desired final video output")
        output_form = QFormLayout(output_box)
        self.video_final_width = QSpinBox(); self.video_final_height = QSpinBox()
        for control in (self.video_final_width, self.video_final_height):
            control.setRange(0, 4096); control.setSingleStep(32)
            control.setSpecialValueText("Same as native clip")
        self.video_detail_preference = QComboBox()
        self.video_detail_preference.addItem("Preserve frames · faithful resize", "faithful")
        self.video_detail_preference.addItem("Invent details · generative upscale", "generative")
        self.video_output_cap_mib = QSpinBox()
        self.video_output_cap_mib.setRange(1, 256); self.video_output_cap_mib.setValue(256)
        self.video_reserve_mib = QSpinBox()
        self.video_reserve_mib.setRange(2, 4096); self.video_reserve_mib.setValue(512)
        self.video_output_button = QPushButton("Preview Recommended Output")
        self.video_output_button.clicked.connect(self.preview_video_output_plan)
        self.video_output_overview = QLabel()
        self.video_output_overview.setWordWrap(True)
        self.video_output_overview.setTextFormat(Qt.PlainText)
        self.video_output_summary = QLabel("Native clip size is separate from a desired final size. A faithful local resize can fit and black-pad a verified clip.")
        self.video_output_summary.setWordWrap(True); self.video_output_summary.setTextFormat(Qt.PlainText)
        output_form.addRow(self.video_output_overview)
        output_details = QWidget()
        output_details_form = QFormLayout(output_details)
        output_details_form.addRow("Final width", self.video_final_width)
        output_details_form.addRow("Final height", self.video_final_height)
        output_details_form.addRow("Detail preference", self.video_detail_preference)
        output_details_form.addRow("Requested output cap (MiB)", self.video_output_cap_mib)
        output_details_form.addRow("Retained storage reserve (MiB)", self.video_reserve_mib)
        output_details_form.addRow(self.video_output_button)
        output_details_form.addRow(self.video_output_summary)
        self.output_details_button = DisclosureButton("Customize Final Output",
            "Hide Final Output Settings", output_details, self)
        output_form.addRow(self.output_details_button)
        output_form.addRow(output_details)
        form_stack.addWidget(output_box)

        models_box = QGroupBox("Registered model components")
        models_form = QFormLayout(models_box)
        self.model_names = {}
        self.model_labels = {}
        self.model_buttons = {}
        exact_models = QWidget()
        exact_models_form = QFormLayout(exact_models)
        for role, label in (("transformer", "Wan2.2 TI2V-5B transformer"),
                            ("text_encoder", "UMT5 text encoder"), ("vae", "Wan2.2 VAE")):
            button = QPushButton("Choose registered model…")
            button.clicked.connect(lambda _checked=False, item=role: self.choose_model(item))
            name = _RuntimeDetailLabel("None selected")
            name.setWordWrap(True); name.setTextFormat(Qt.PlainText)
            name.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
            name.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Minimum)
            name.setToolTip("Name from Models, not a fresh file or runtime check. Inspect Exact Model IDs for the saved identity.")
            value = _ModelIdentityField()
            row = QHBoxLayout(); row.addWidget(button); row.addWidget(name, 1)
            models_form.addRow(label, row)
            exact_models_form.addRow(f"{label} exact ID", value)
            self.model_names[role] = name
            self.model_labels[role] = value
            self.model_buttons[role] = button
        self.model_exact_button = DisclosureButton("Inspect Exact Model IDs",
            "Hide Exact Model IDs", exact_models, self)
        models_form.addRow(self.model_exact_button)
        models_form.addRow(exact_models)
        form_stack.addWidget(models_box)

        runtime_box = QGroupBox("Set up local video generation")
        self.runtime_box = runtime_box
        runtime_form = QFormLayout(runtime_box)
        self.runtime_summary = QLabel()
        self.runtime_summary.setWordWrap(True)
        self.runtime_summary.setTextFormat(Qt.PlainText)
        runtime_form.addRow(self.runtime_summary)
        runtime_settings = QWidget()
        settings_form = QFormLayout(runtime_settings)
        self.setup_guide = QLabel(
            "Choose the three registered components in this workspace. For the local runtime, "
            "choose an existing ComfyUI source and its Python, then a new dedicated workspace "
            "for this app. Preview Setup checks the exact files; Start Isolated Runtime is a separate action.")
        self.setup_guide.setWordWrap(True)
        self.setup_guide.setTextFormat(Qt.PlainText)
        settings_form.addRow(self.setup_guide)
        self.transformer_role = QComboBox()
        self.transformer_role.addItem("UNet / diffusion transformer", "unet")
        self.transformer_role.addItem("Checkpoint", "checkpoint")
        self.transformer_role.currentIndexChanged.connect(self._transformer_role_changed)
        settings_form.addRow("Transformer registry role", self.transformer_role)
        settings_form.setRowVisible(self.transformer_role, False)  # Recipe role follows the exact declaration.
        self.source_root = QLineEdit()
        self.source_root.setPlaceholderText("Choose an existing ComfyUI source folder")
        self.source_root.setToolTip("Select the existing ComfyUI source. CollectiGen uses a separate dedicated workspace for its runtime.")
        self.python_executable = QLineEdit()
        self.python_executable.setPlaceholderText("Choose its existing Python executable")
        self.python_executable.setToolTip("Select the Python executable already used by that ComfyUI installation.")
        self.python_runtime_root = QLineEdit()
        self.python_runtime_root.setPlaceholderText("Root containing that Python executable")
        self.python_runtime_root.setToolTip("Choose the existing Python environment root that contains the selected executable.")
        self.workspace_root = QLineEdit()
        self.workspace_root.setPlaceholderText("New, dedicated workspace folder")
        self.workspace_root.setToolTip("Choose a new workspace for this app's isolated runtime; it must not overwrite the existing ComfyUI source.")
        self.suggest_workspace_button = QPushButton("Use suggested dedicated workspace")
        self.suggest_workspace_button.clicked.connect(
            lambda: self.workspace_root.setText(str(self.storage_root / "comfy-owned-workspace")))
        self.port = QSpinBox(); self.port.setRange(1024, 65535); self.port.setValue(8189)
        self.port.setToolTip("Loopback-only port for the isolated local runtime. Change it if this port is already in use.")
        self.free_gib = QSpinBox(); self.free_gib.setRange(1, 1024); self.free_gib.setValue(4)
        self.free_gib.setToolTip("Minimum free disk space required before preparing the dedicated runtime workspace.")
        self.runtime_browse_buttons = []
        for label, edit, folder in (("Comfy source", self.source_root, True),
                                    ("Python executable", self.python_executable, False),
                                    ("Python runtime root", self.python_runtime_root, True),
                                    ("New owned workspace", self.workspace_root, True)):
            row = QHBoxLayout(); row.addWidget(edit, 1)
            browse = QPushButton("Browse…")
            self.runtime_browse_buttons.append(browse)
            browse.clicked.connect(lambda _checked=False, e=edit, f=folder: self._browse_runtime(e, f))
            row.addWidget(browse)
            settings_form.addRow(label, row)
        settings_form.addRow("Loopback port", self.port)
        settings_form.addRow("Minimum free disk space (GiB)", self.free_gib)
        settings_form.addRow(self.suggest_workspace_button)
        setup_actions = QHBoxLayout()
        self.save_setup_button = QPushButton("Save Local Setup")
        self.reload_setup_button = QPushButton("Reload Saved Setup")
        self.save_setup_button.clicked.connect(lambda: self.save_local_setup())
        self.reload_setup_button.clicked.connect(self.reload_local_setup)
        setup_actions.addWidget(self.save_setup_button)
        setup_actions.addWidget(self.reload_setup_button)
        setup_actions.addStretch(1)
        settings_form.addRow(setup_actions)
        setup_note = QLabel("Saved setup remembers these local paths and limits only. Video prompts, source images and model choices stay in the draft.")
        setup_note.setWordWrap(True); setup_note.setTextFormat(Qt.PlainText)
        settings_form.addRow(setup_note)
        self.runtime_settings_button = DisclosureButton("Configure Local Video…",
            "Hide Local Video Settings", runtime_settings, self)
        runtime_form.addRow(self.runtime_settings_button)
        runtime_form.addRow(runtime_settings)
        qualification = QLabel("This first recipe is qualified for 16 GiB-class NVIDIA GPUs. Its peak VRAM need is not measured here; total capacity and current free VRAM do not prove a request will fit.")
        qualification.setWordWrap(True)
        runtime_form.addRow(qualification)
        self.memory_status = QLabel("Current free VRAM: unknown until an exact native graph is prepared. Peak requirement: unknown.")
        self.memory_status.setWordWrap(True)
        self.memory_status.setTextFormat(Qt.PlainText)
        runtime_form.addRow("Video GPU memory", self.memory_status)
        technical_details = QWidget()
        technical_form = QFormLayout(technical_details)
        self.pack_button = QPushButton("Offline Dependency Pack…")
        self.pack_button.clicked.connect(self.open_dependency_pack)
        technical_form.addRow(self.pack_button)
        self.model_paths = _RuntimeDetailLabel("Selected model paths appear after Preview Setup.")
        self.model_paths.setWordWrap(True)
        self.model_paths.setTextFormat(Qt.PlainText)
        self.model_paths.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        technical_form.addRow("Exact component paths", self.model_paths)
        self.runtime_technical_button = DisclosureButton("Inspect Runtime Details",
            "Hide Runtime Details", technical_details, self)
        runtime_form.addRow(self.runtime_technical_button)
        runtime_form.addRow(technical_details)
        storage_controls = QWidget()
        storage_actions = QHBoxLayout(storage_controls)
        self.prepare_storage_button = QPushButton("Prepare Model Storage…")
        self.recover_storage_button = QPushButton("Recover Model Preparation…")
        self.prepare_storage_button.clicked.connect(self.prepare_model_storage)
        self.recover_storage_button.clicked.connect(self.recover_model_storage)
        storage_actions.addWidget(self.prepare_storage_button)
        storage_actions.addWidget(self.recover_storage_button)
        self.storage_options_button = DisclosureButton("Model Storage Options…",
            "Hide Model Storage Options", storage_controls, self)
        runtime_form.addRow(self.storage_options_button)
        runtime_form.addRow(storage_controls)
        self.storage_status = _RuntimeDetailLabel("Review model storage before starting a runtime that uses Fast storage.")
        self.storage_status.setWordWrap(True)
        self.storage_status.setTextFormat(Qt.PlainText)
        self.storage_status.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Minimum)
        runtime_form.addRow(self.storage_status)
        form_stack.addWidget(runtime_box)
        self.setup_jump_button.clicked.connect(self.show_local_setup)

        draft_controls = QWidget()
        draft_row = QHBoxLayout(draft_controls)
        self.save_button = QPushButton("Save Draft…")
        self.load_button = QPushButton("Load Draft…")
        self.new_button = QPushButton("New Draft")
        for button, slot in ((self.save_button, self.save_draft),
                             (self.load_button, self.load_draft), (self.new_button, self.new_draft)):
            button.clicked.connect(lambda _checked=False, action=slot: action())
        draft_row.addWidget(self.load_button)
        draft_row.addWidget(self.new_button)
        draft_row.addStretch(1)
        form_stack.addWidget(draft_controls)
        self._section_widgets = ((request_box, output_box, draft_controls),
                                 (models_box,), (runtime_box,))
        form_stack.addStretch(1)
        self.section_bar.currentChanged.connect(self._show_section)
        self._show_section(0)

        self.review = QPlainTextEdit()
        self.review.setReadOnly(True)
        self.review.setMinimumHeight(160)
        self.review.setPlaceholderText("The exact request, component hashes, graph and translation report appear here before submission.")
        outer.addWidget(self.review)
        self.review_loss = QCheckBox("I reviewed the displayed source crop and untranslated intent")
        self.review_loss.toggled.connect(self._update_actions)
        outer.addWidget(self.review_loss)
        actions = QGridLayout()
        self.preview_setup_button = QPushButton("Preview Setup")
        self.start_button = QPushButton("Start Isolated Runtime")
        self.prepare_button = QPushButton("Prepare Video")
        self.submit_button = QPushButton("Submit Reviewed Video")
        self.cancel_job_button = QPushButton("Cancel Owned Job")
        self.recover_button = QPushButton("Recover Job")
        self.recover_button.setToolTip(
            "Recover the previously submitted video job, not the currently edited draft."
        )
        self.preview_video_button = QPushButton("Preview Video…")
        self.finish_video_button = QPushButton("Finish Video…")
        self.ingest_button = QPushButton("Send Verified Video to Ingest")
        self.stop_button = QPushButton("Stop Runtime")
        self.close_button = QPushButton("Close Workspace")
        self.cancel_step_button = QPushButton("Cancel Current Step")
        self.cancel_step_button.clicked.connect(self.cancel_current_step)
        for button, slot in ((self.preview_setup_button, self.preview_setup),
            (self.start_button, self.start_runtime), (self.prepare_button, self.prepare_video),
            (self.submit_button, self.submit_video), (self.cancel_job_button, self.cancel_job),
            (self.recover_button, self.recover_job), (self.preview_video_button, self.preview_video),
            (self.finish_video_button, self.finish_video),
            (self.ingest_button, self.send_to_ingest), (self.stop_button, self.stop_runtime),
            (self.close_button, self.close_workspace)):
            button.clicked.connect(slot)
        self._action_layout = actions
        self._action_buttons = (self.preview_setup_button, self.start_button,
            self.prepare_button, self.submit_button, self.cancel_job_button,
            self.recover_button, self.preview_video_button, self.ingest_button,
            self.finish_video_button, self.stop_button, self.cancel_step_button,
            self.save_button)
        self._shown_action_buttons = ()
        self.more_actions_button = QPushButton("More Video Actions…")
        self.more_actions_button.setCheckable(True)
        self.more_actions_button.setToolTip(
            "Show all video-stage actions and their current availability without starting work."
        )
        self.more_actions_button.toggled.connect(self._update_action_presentation)
        outer.addLayout(actions)
        self.status = QLabel("Choose registered components and an existing local runtime, then Preview Setup.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        outer.addWidget(self.status)
        for widget in (self.positive, self.negative, self.motion, self.camera, self.seed,
                       self.source_root, self.python_executable, self.python_runtime_root,
                       self.workspace_root):
            if isinstance(widget, QPlainTextEdit):
                widget.textChanged.connect(self._invalidate)
            else:
                widget.textChanged.connect(self._invalidate)
        for widget in (self.frame_width, self.frame_height, self.frames, self.fps, self.port, self.free_gib):
            widget.valueChanged.connect(self._invalidate)
        for widget in (self.video_final_width, self.video_final_height,
                       self.video_output_cap_mib, self.video_reserve_mib):
            widget.valueChanged.connect(self._invalidate)
        self.video_detail_preference.currentIndexChanged.connect(self._invalidate)
        self.append_intent.toggled.connect(self._invalidate)
        self._task_changed()
        try:
            self._apply_local_setup()
        except Exception as exc:
            self._set_status(f"Saved local video setup was not loaded: {exc}")
        self._clean_draft_snapshot = self._draft_display_snapshot()
        self._clean_setup_snapshot = self._setup_snapshot()
        self._update_actions()

    def _busy(self):
        return self._task is not None or self._runtime_storage_dialog is not None

    def _set_status(self, value):
        self.status.setText(str(value)[:2000])

    def _run(self, label, operation, completed, *, resource="io", cancelable=True):
        if self._busy() or (self._shutdown_requested and label not in {
                "Reconcile exact video job", "Stop owned Comfy runtime"}):
            return
        task = CoordinatedCallTask(self.jobs, label, lambda job: operation(), resource=resource)
        self._task = task
        self._task_label = label
        self._task_cancelable = cancelable
        task.signals.complete.connect(lambda result: self._finished_task(task, completed, result))
        task.signals.failed.connect(lambda error: self._failed_task(task, error))
        task.signals.cancelled.connect(lambda: self._failed_task(task, "Cancelled"))
        task.signals.progress.connect(lambda _n, _t, message: self._set_status(message))
        self._set_status(label + "…")
        self._update_actions()
        try:
            task.start()
        except Exception as exc:
            self._failed_task(task, str(exc))

    def _finished_task(self, task, completed, result):
        if self._task is not task:
            return
        label = self._task_label
        self._task = None
        self._task_label = ""
        self._task_cancelable = False
        try:
            completed(result)
        except Exception as exc:
            self._set_status(f"{exc}")
            if label in {"Save video draft", "Save local video setup"}:
                self._draft_close_pending = False
            if label == "Start isolated Comfy runtime" and getattr(result, "process", None) is not None:
                self._runtime_receipt = result
                self._clear_memory_status()
            elif label == "Submit reviewed video" and getattr(result, "id", None):
                self._submitted_job = result
                self._job_id = result.id
            if self._shutdown_requested:
                if label == "Reconcile exact video job":
                    self._shutdown_reconcile_needed = False
                    self._shutdown_reconcile_failed = True
                elif label == "Stop owned Comfy runtime":
                    self._shutdown_stop_failed = True
        self._update_actions()
        self._advance_shutdown()

    def _failed_task(self, task, error):
        if self._task is task:
            label = self._task_label
            self._task = None
            self._task_label = ""
            self._task_cancelable = False
            message = str(error)
            if label == "Preview isolated Comfy setup":
                for role, name in (("transformer", "transformer"),
                                   ("text_encoder", "text encoder"),
                                   ("vae", "VAE")):
                    if f"Selected {role} model hash differs from the reviewed identity." in message:
                        message = (f"The selected {name} file changed. Your video draft and other model choices "
                                   f"are unchanged. Use Choose registered model… on the {name} row to "
                                   "verify an exact copy, then Preview Setup again.")
                        self.section_bar.setCurrentIndex(1)
                        break
            self._set_status(message)
            if label in {"Save video draft", "Save local video setup"}:
                self._draft_close_pending = False
            if label == "Start isolated Comfy runtime":
                if self._runtime_storage_locator is not None:
                    self._set_status(f"{error}\nThe exact model preparation is retained. Use Stop Runtime or Recover Model Preparation; no model reservation was released.")
                from core.comfy_video_runtime import get_held_runtime_recovery
                preview = getattr(self, "_runtime_start_preview", None)
                held = get_held_runtime_recovery(preview) if preview is not None else None
                self._runtime_start_preview = None
                if held is not None:
                    self._runtime_receipt = held
                    self._runtime_stop_only = True
                    self._clear_memory_status()
                    self._set_status(f"{error}\nThe owned runtime may still be running. "
                                     "Use Stop Runtime to retry stopping it before starting another draft.")
            if self._shutdown_requested:
                if label == "Reconcile exact video job":
                    self._shutdown_reconcile_needed = False
                    self._shutdown_reconcile_failed = True
                elif label == "Stop owned Comfy runtime":
                    self._shutdown_stop_failed = True
            self._update_actions()
            self._advance_shutdown()

    def cancel_current_step(self):
        if self._task is None or not self._task_cancelable:
            return
        self._task.cancel()
        self._task = None
        self._task_cancelable = False
        self._set_status("Read-only step cancelled. No runtime or video job was stopped.")
        self._update_actions()

    def _invalidate(self, *_args):
        self._clear_memory_status()
        self._prepared = None
        self._request = None
        self._runtime_preview = None
        self._adapter = None
        self._video_output_preview = None
        self._refresh_source_transform_preview()
        self.review_loss.setChecked(False)
        self._refresh_request_summary()
        self._refresh_video_output_summary()
        if self._job_id:
            self._set_status(
                "Draft changed. Result actions still refer to the previously submitted video; "
                "save this draft separately."
                if self._verified_receipt is not None else
                "Draft changed. Recover Job checks the previously submitted video; "
                "prepare and review this draft before another submission."
            )
        self._update_actions()

    def _refresh_request_summary(self):
        if self._loaded_future is not None:
            self.request_summary.setText(
                "Exact saved request retained. Some fields cannot be represented here; inspect the loaded draft before starting a supported new request.")
            return
        frames, fps = self.frames.value(), self.fps.value()
        seconds = (frames - 1) / fps
        duration = f"{seconds:.1f}".rstrip("0").rstrip(".")
        self.request_summary.setText(
            f"Native clip: {self.frame_width.value()} × {self.frame_height.value()} · "
            f"{frames} frames at {fps} fps (about {duration} s) · seed {self.seed.text().strip() or 'not set'}. "
            "Customize timing, size, seed and optional prompt wording if needed.")

    def _clear_memory_status(self):
        self._memory_context_revision += 1
        if hasattr(self, "memory_status"):
            self.memory_status.setText(
                "Free VRAM for this draft is unknown. Prepare the exact native graph to observe it; peak requirement remains unknown.")

    def _video_output_goal(self):
        width, height = self.video_final_width.value(), self.video_final_height.value()
        if not width and not height:
            return None
        if not width or not height:
            raise ValueError("Set both desired final video dimensions or leave both as native.")
        if (width < 64 or height < 64 or width > 4096 or height > 4096
                or width % 2 or height % 2 or width * height > 8_294_400):
            raise ValueError("Final video dimensions must be even, 64–4096 per side, and at most 8,294,400 pixels.")
        return {"final_width": width, "final_height": height,
                "detail_preference": self.video_detail_preference.currentData()}

    def _final_stage_review(self, goal):
        if goal is None:
            return {"kind": "none", "reason": "Native clip is the final output."}
        width, height = goal["final_width"], goal["final_height"]
        native_width, native_height = self.frame_width.value(), self.frame_height.value()
        if (width, height) == (native_width, native_height):
            return {"kind": "none", "reason": "Requested final pixels equal the native clip; no later stage runs."}
        if goal["detail_preference"] != "faithful":
            return {"kind": "unavailable_generative", "reason":
                "No qualified generative video upscale is available."}
        scale = min(Fraction(width, native_width), Fraction(height, native_height))
        fitted_width = max(1, int(native_width * scale))
        fitted_height = max(1, int(native_height * scale))
        return {"kind": "faithful_resize", "source": [native_width, native_height],
            "final": [width, height], "fitted": [fitted_width, fitted_height],
            "pad_left": (width - fitted_width) // 2,
            "pad_top": (height - fitted_height) // 2,
            "aspect_policy": "fit_center_black_pad", "interpolation": "Pillow LANCZOS",
            "detail": "Preserves the verified clip appearance; does not invent detail."}

    def _refresh_video_output_summary(self):
        if self._loaded_future is not None:
            self.video_output_overview.setText(
                "Exact saved output request retained; inspect the loaded draft before starting a supported request.")
            self.video_output_summary.setText(
                "Exact saved output request retained; these controls cannot represent every field. Inspect the loaded draft.")
            return
        try:
            goal = self._video_output_goal()
        except ValueError as exc:
            self.video_output_overview.setText(f"{exc} Customize Final Output to correct it.")
            self.video_output_summary.setText(str(exc)); return
        native = f"{self.frame_width.value()} × {self.frame_height.value()}"
        if goal is None:
            self.video_output_overview.setText(f"Final output: same {native} pixels as the native clip. Customize if you need another size.")
            self.video_output_summary.setText(f"Native clip {native}; no separate final-size goal. Memory evidence unknown until an exact runtime assessment.")
            return
        text = f"Native clip {native} → desired final {goal['final_width']} × {goal['final_height']}. "
        stage = self._final_stage_review(goal)
        if stage["kind"] == "faithful_resize":
            self.video_output_overview.setText(
                f"Final output: {goal['final_width']} × {goal['final_height']} after a faithful resize of the verified native clip.")
            text += (f"After native verification, fit {stage['fitted'][0]} × {stage['fitted'][1]} "
                f"inside the final canvas, center black-pad (left {stage['pad_left']}, "
                f"top {stage['pad_top']}), then verify the separate MP4. "
                "LANCZOS preserves appearance; it does not invent detail. "
                f"Requested output cap {self.video_output_cap_mib.value()} MiB; "
                f"retained reserve {self.video_reserve_mib.value()} MiB. ")
        elif stage["kind"] == "unavailable_generative":
            self.video_output_overview.setText(
                f"Desired final size: {goal['final_width']} × {goal['final_height']}; generative video upscale is unavailable.")
            text += "Generative video upscale is unavailable; choose faithful resize to execute. "
        else:
            self.video_output_overview.setText(
                f"Final output: {goal['final_width']} × {goal['final_height']} native pixels; no separate resize stage.")
            text += "Same native pixels; no final stage runs and detail preference has no effect. "
        result = self._video_output_preview
        if result:
            text += f"Memory: {result['vram']['evidence']} — {result['vram']['message']}"
            if result["messages"]:
                text += " " + " ".join(result["messages"])
        else:
            text += "Preview size and memory evidence."
        self.video_output_summary.setText(text)

    def preview_video_output_plan(self):
        try:
            goal = self._video_output_goal()
            if goal is None:
                raise ValueError("Choose a distinct desired final video size first.")
            selected = self._models["transformer"]
            pinned = bool(self.pinned_wan_resource_id and selected == self.pinned_wan_resource_id)
            evidence = {"resource_id": selected, "evidence_kind": "exact_model" if pinned else "unknown",
                        "verified": pinned,
                        "catalog_id": "wan-ai/wan2.2-ti2v-5b" if pinned else "",
                        "version": "TI2V-5B" if pinned else ""}
            result = plan_output_resolution(goal["final_width"], goal["final_height"],
                model_evidence=evidence, installed_stages={},
                detail_preference=goal["detail_preference"], modality="video",
                frame_count=self.frames.value(), fps=self.fps.value())
        except ValueError as exc:
            self._set_status(str(exc)); return
        self._video_output_preview = result
        self._refresh_video_output_summary()
        stage = self._final_stage_review(goal)
        self._set_status("Faithful fit-and-black-pad final stage is reviewable after native verification."
            if stage["kind"] == "faithful_resize" else
            "The requested final size matches native pixels; no later stage runs."
            if stage["kind"] == "none" else
            "Generative video upscale remains unavailable; choose faithful resize to execute.")

    def _transformer_role_changed(self, *_args):
        role = self.transformer_role.currentData()
        if role != self._model_roles["transformer"]:
            self._model_roles["transformer"] = role
            self._invalidate()

    def _task_changed(self, *_args):
        i2v = self.task_choice.currentData() == "i2v"
        self.request_form.setRowVisible(self._source_still_row, i2v)
        self.request_form.setRowVisible(self._source_review_row, i2v)
        self._invalidate()

    @staticmethod
    def _native_crop_rect(old_width, old_height, new_width, new_height):
        """Mirror installed Comfy common_upscale(..., 'bilinear', 'center') crop."""
        if min(old_width, old_height, new_width, new_height) <= 0:
            raise ValueError("Video source and native dimensions must be positive.")
        old_aspect, new_aspect = old_width / old_height, new_width / new_height
        x = y = 0
        if old_aspect > new_aspect:
            x = round((old_width - old_width * (new_aspect / old_aspect)) / 2)
        elif old_aspect < new_aspect:
            y = round((old_height - old_height * (old_aspect / new_aspect)) / 2)
        width, height = old_width - 2 * x, old_height - 2 * y
        if width <= 0 or height <= 0:
            raise ValueError("The native center crop would remove the entire selected image; choose another canvas.")
        return x, y, width, height

    def _refresh_source_transform_preview(self):
        image, source = self._source_image, self._source_ref
        if image is None or source is None:
            self._source_preview_key = None
            self.native_source_preview.clear("Native frame input preview")
            self.native_source_label.setText("Choose a source to review the native crop and resize.")
            return
        target_width, target_height = self.frame_width.value(), self.frame_height.value()
        key = (source.sha256, image.width(), image.height(), target_width, target_height)
        if key == self._source_preview_key:
            return
        try:
            x, y, width, height = self._native_crop_rect(image.width(), image.height(),
                target_width, target_height)
        except ValueError as exc:
            self.native_source_preview.clear("Native crop unavailable")
            self.native_source_label.setText(str(exc))
            self._source_preview_key = None
            return
        scale = min(1.0, 512 / max(target_width, target_height))
        preview_width = max(1, round(target_width * scale))
        preview_height = max(1, round(target_height * scale))
        cropped = image.copy(x, y, width, height)
        preview = cropped.scaled(preview_width, preview_height,
            Qt.IgnoreAspectRatio, Qt.SmoothTransformation)
        self.native_source_preview.set_image(QPixmap.fromImage(preview), self._source_path)
        self.native_source_label.setText(
            f"Native input: crop ({x}, {y}) to {width} × {height}, then bilinear resize to "
            f"{target_width} × {target_height}. Original bytes stay unchanged; displayed preview pixels "
            "may differ slightly from native Torch interpolation.")
        self._source_preview_key = key

    def _browse_runtime(self, edit, folder):
        if self._runtime_storage_locator is not None or self._runtime_receipt is not None or self._busy() or self._shutdown_requested:
            return
        if folder:
            chosen = QFileDialog.getExistingDirectory(self, "Choose existing folder", edit.text())
        else:
            chosen, _ = QFileDialog.getOpenFileName(self, "Choose Python executable", edit.text(), "Executable (*.exe)")
        if chosen:
            edit.setText(chosen)

    def _setup_snapshot(self):
        return (self.source_root.text().strip(), self.python_executable.text().strip(),
            self.python_runtime_root.text().strip(), self.workspace_root.text().strip(),
            self.port.value(), self.free_gib.value(), self.transformer_role.currentData())

    def _refresh_runtime_summary(self):
        if self._runtime_receipt is not None:
            summary = "Owned local runtime started. Prepare the video request; local setup is locked until it stops."
        elif self._runtime_start_uncertain:
            summary = "Runtime startup is uncertain. Use the visible Stop action before another setup attempt."
        elif self._runtime_storage_locator is not None:
            summary = "Model preparation is retained. Review Model Recovery before changing the local setup."
        elif self._runtime_preview is not None:
            summary = ("Exact local files and selected models passed Preview Setup. "
                + ("Review Fast model storage before a separate runtime start."
                   if self._runtime_preview.config.protected_loader else
                   "Starting the isolated runtime is a separate action."))
        else:
            paths = (self.source_root, self.python_executable,
                     self.python_runtime_root, self.workspace_root)
            entered = sum(bool(field.text().strip()) for field in paths)
            selected = sum(bool(resource_id) for resource_id in self._models.values())
            summary = (f"Local setup: {entered} of 4 paths entered; {selected} of 3 model components chosen. "
                "Choose registered components and Configure Local Video. "
                "Entered values are not verified until Preview Setup.")
        self.runtime_summary.setText(summary)

    def _show_section(self, index: int) -> None:
        for section, widgets in enumerate(self._section_widgets):
            for widget in widgets:
                widget.setVisible(section == index)
        self.scroll.verticalScrollBar().setValue(0)

    def show_local_setup(self):
        self.section_bar.setCurrentIndex(2)
        if self._runtime_storage_locator is not None:
            self.storage_options_button.setChecked(True)
            target = self.recover_storage_button
        else:
            self.runtime_settings_button.setChecked(True)
            target = next((field for field in (self.source_root, self.python_executable,
                self.python_runtime_root, self.workspace_root) if not field.text().strip()),
                self.source_root)
        self.scroll.ensureWidgetVisible(target)
        target.setFocus()

    def _setup_fields(self):
        source_root, python_executable, python_runtime_root, workspace_root, port, free_gib, transformer_role = self._setup_snapshot()
        return dict(source_root=source_root, python_executable=python_executable,
            python_runtime_root=python_runtime_root, workspace_root=workspace_root,
            port=port, free_gib=free_gib, transformer_role=transformer_role)

    def _apply_local_setup(self):
        fields, self._setup_digest = load_video_runtime_preferences(self.storage_root)
        if fields is None:
            return
        self.source_root.setText(fields["source_root"])
        self.python_executable.setText(fields["python_executable"])
        self.python_runtime_root.setText(fields["python_runtime_root"])
        self.workspace_root.setText(fields["workspace_root"])
        self.port.setValue(fields["port"])
        self.free_gib.setValue(fields["free_gib"])
        self.transformer_role.setCurrentIndex(self.transformer_role.findData(fields["transformer_role"]))
        self._clean_setup_snapshot = self._setup_snapshot()

    def save_local_setup(self, *, on_saved=None):
        fields = self._setup_fields()
        snapshot = self._setup_snapshot()
        expected = self._setup_digest
        def complete(digest):
            self._setup_digest = digest
            if self._setup_snapshot() == snapshot:
                self._clean_setup_snapshot = snapshot
            self._set_status("Local video setup saved. Preview Setup to validate its current files and models.")
            if callable(on_saved):
                on_saved()
        self._run("Save local video setup",
            lambda: save_video_runtime_preferences(self.storage_root, fields,
                expected_sha256=expected), complete, cancelable=False)
        return True

    def reload_local_setup(self):
        if self._runtime_storage_locator is not None or self._runtime_receipt is not None:
            self._set_status("Close the retained preparation through recovery before changing runtime setup.")
            return
        if self._setup_snapshot() != self._clean_setup_snapshot:
            choice = QMessageBox.question(self, "Reload saved video setup",
                "Discard unsaved local setup changes and reload the saved values?",
                QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel)
            if choice != QMessageBox.Discard:
                return
        try:
            self._apply_local_setup()
        except Exception as exc:
            self._set_status(f"Saved local video setup was not loaded: {exc}")
            return
        self._clean_setup_snapshot = self._setup_snapshot()
        self._set_status("Saved local video setup loaded. Preview Setup to validate its current files and models.")

    def choose_source(self):
        chosen, _ = QFileDialog.getOpenFileName(self, "Choose one source image", "",
            "Still images (*.png *.jpg *.jpeg *.webp)")
        if chosen:
            self.attach_source(Path(chosen))

    def attach_source(self, path, *, on_attached=None, frame_reference_provider=None):
        """Attach one explicitly selected still; notify only after successful review state."""
        path = Path(path).absolute()
        if path.suffix.casefold() not in {".png", ".jpg", ".jpeg", ".webp"}:
            self._set_status("Choose a PNG, JPEG or WebP source image.")
            return
        def read():
            raw, state = read_stable_training_source(path)
            converted = prepare_video_source(raw, path.suffix)
            identity, lineage = self._selected_source_snapshot(path, converted,
                frame_reference_provider=frame_reference_provider)
            if identity["collection"][0]["image_sha256"] != state["sha256"]:
                raise ValueError("The selected source changed while attaching it.")
            return converted, identity, lineage
        def complete(value):
            converted, identity, lineage = value
            image = QImage.fromData(converted.png_bytes)
            if image.isNull():
                raise ValueError("The derived source PNG cannot be decoded.")
            self._source_path, self._source_raw = path, converted.png_bytes
            self._source_conversion = converted
            self._frame_reference_provider = frame_reference_provider
            self._source_snapshot = lineage
            self._source_ref = ImageSourceReference(converted.png_sha256,
                len(converted.png_bytes), digest(identity))
            self._source_image = image
            self.source_preview.set_image(QPixmap.fromImage(image), path)
            self._source_preview_key = None
            suffix = " · converted to PNG" if converted.original_format != "PNG" else ""
            self.source_label.setText(f"{path.name} · {image.width()} × {image.height()} · original SHA-256 {converted.original_sha256[:12]}…{suffix}")
            records = lineage["generation_records"]
            if len(records) == 1:
                if not self.positive.toPlainText().strip():
                    self.positive.setPlainText(records[0].get("positive_prompt", ""))
                if not self.negative.toPlainText().strip():
                    self.negative.setPlainText(records[0].get("negative_prompt", ""))
            self._invalidate()
            self._set_status("Source attached. Multiple recorded prompts were retained; enter the video prompt you want."
                if len(records) > 1 else
                "Source attached. Review its editable prompt and exact source lineage before submission.")
            if callable(on_attached):
                on_attached(self)
        self._run("Read selected video source", read, complete)

    @staticmethod
    def _selected_source_snapshot(path, converted, *, frame_reference_provider=None):
        """Freeze selected ImageRecord generation evidence with canonical state."""
        identity = VideoGenerationDialog._current_source_identity(path, converted,
            frame_reference_provider=frame_reference_provider)
        record = ImageRecord(path)
        lineage = {"source_state_sha256": digest(identity),
            "source_image_sha256": identity["collection"][0]["image_sha256"],
            "source_knowledge_sha256": identity["collection"][0]["knowledge_sha256"],
            "source_caption_sha256": identity.get("caption_sha256"),
            "source_conversion": converted.lineage(),
            "derived_frame": identity.get("derived_frame"),
            "generation_records": [item.to_dict() for item in record.annotation.generation_records],
            "standard_metadata": record.annotation.standard_metadata}
        if identity != VideoGenerationDialog._current_source_identity(path, converted,
                frame_reference_provider=frame_reference_provider):
            raise ValueError("The selected source knowledge changed while attaching it.")
        if len(json.dumps(lineage, sort_keys=True,
                          allow_nan=False).encode("utf-8")) > 16_384:
            raise ValueError("This source has more generation evidence than the current bounded video lineage supports. The original record is unchanged.")
        return identity, lineage

    @staticmethod
    def _current_source_identity(path, converted=None, *, frame_reference_provider=None):
        from .generation_workflow import _source_identity
        identity = _source_identity((path,), frame_reference_provider=frame_reference_provider)
        caption = path.with_suffix(".txt")
        if caption.is_file():
            _, evidence = read_stable_training_source(caption)
            identity["caption_sha256"] = evidence["sha256"]
        if converted is not None:
            identity["source_conversion"] = converted.lineage()
        return identity

    def choose_model(self, component):
        if self._runtime_storage_locator is not None or self._runtime_receipt is not None or self._busy():
            return
        from .model_selector_dialog import ModelSelectorDialog
        role = "" if component == "transformer" else self._model_roles[component]
        records = {}
        try:
            snapshot, _revision = self.registry.snapshot()
            if isinstance(snapshot, dict) and isinstance(snapshot.get("resources"), dict):
                records = snapshot["resources"]
        except (OSError, ValueError, TypeError, ModelResourceError):
            pass
        retained = []
        for slot, resource_id in self._models.items():
            if slot == component or not resource_id:
                continue
            record = records.get(resource_id, {})
            selected_role = (_wan_recipe_selected_role(slot, resource_id, record)
                             or self._model_roles[slot])
            declarations = record.get("declarations", ()) if isinstance(record, dict) else ()
            families = {item.get("family", "") for item in declarations
                if isinstance(item, dict) and item.get("role") == selected_role
                and isinstance(item.get("family"), str) and item.get("family")}
            retained.append({"resource_id": resource_id, "role": selected_role,
                             "family": next(iter(families)) if len(families) == 1 else ""})
        selected_transformer = self._models["transformer"]
        transformer_role = (_wan_recipe_selected_role("transformer", selected_transformer,
            records.get(selected_transformer, {})) or self._model_roles["transformer"])
        selector = ModelSelectorDialog(self.registry, self.jobs, role=role,
            settings=self.settings, parent=self, task_focused=True,
            allowed_declarations=_wan_recipe_declarations(component),
            choice_scope_label=("This first Video workflow uses one exact qualified "
                f"Wan2.2 TI2V-5B {dict(transformer='transformer', text_encoder='text encoder', vae='VAE')[component]}."),
            current_choice_label=(f"{dict(transformer='Transformer', text_encoder='Text encoder', vae='VAE')[component]}: "
                                  f"{self.model_names[component].text()}"),
            selected_resources=tuple(retained),
            required_roles=((transformer_role, "text_encoder", "vae")
                            if component != "transformer" else ("text_encoder", "vae")))
        if selector.exec() == QDialog.Accepted and selector.selected_resource_id:
            self._models[component] = selector.selected_resource_id
            path = getattr(selector, "selected_path", "")
            if path:
                self._model_locations[component] = (selector.selected_resource_id, str(path))
            else:
                self._model_locations.pop(component, None)
            self._refresh_model_displays()
            self._invalidate()

    def _refresh_model_displays(self):
        """Project path-free names only; exact IDs remain the request authority."""
        from .generation_workflow import _safe_model_label
        records = {}
        if any(self._models.values()):
            try:
                snapshot, _revision = self.registry.snapshot()
                records = snapshot.get("resources", {}) if isinstance(snapshot, dict) else {}
            except (OSError, ValueError, TypeError, ModelResourceError):
                pass
        for slot, resource_id in self._models.items():
            self.model_labels[slot].setText(resource_id or "None selected")
            if not resource_id:
                self.model_names[slot].setText("None selected")
                continue
            record = records.get(resource_id) if isinstance(records, dict) else None
            if not isinstance(record, dict):
                self.model_names[slot].setText("Saved model name unavailable in Models")
                continue
            user = record.get("user", {})
            custom = user.get("display_name") if isinstance(user, dict) else None
            locations = record.get("locations", ())
            chosen = self._model_locations.get(slot)
            selected_path = (chosen[1] if isinstance(chosen, (tuple, list)) and
                len(chosen) == 2 and chosen[0] == resource_id and isinstance(chosen[1], str)
                else "")
            registered_paths = []
            if isinstance(locations, (list, tuple)):
                registered_paths = [item["path"] for item in locations
                    if isinstance(item, dict) and isinstance(item.get("path"), str)]
            if selected_path and not any(path.casefold() == selected_path.casefold()
                    for path in registered_paths):
                selected_path = ""
            names = [custom, Path(selected_path).name if selected_path else ""]
            names.extend(Path(path).name for path in registered_paths)
            name = next((value for value in names if _safe_model_label(value)), None)
            displayed = name or "Registered model name unavailable"
            if not _wan_recipe_choice_matches(slot, resource_id, record):
                displayed += " — not usable by this Wan recipe"
            self.model_names[slot].setText(displayed)

    def _model_config(self, fields):
        value, _revision = self.registry.snapshot()
        selected = {}
        for role, resource_id in fields["models"].items():
            if not resource_id:
                raise ValueError(f"Choose the registered {role} component.")
            record = value.get("resources", {}).get(resource_id)
            allowed_roles = {"transformer": {"unet", "checkpoint"},
                "text_encoder": {"text_encoder"}, "vae": {"vae"}}[role]
            if not isinstance(record, dict) or not any(
                    row.get("role") in allowed_roles for row in record.get("declarations", [])):
                raise ValueError(f"The {role} registry declaration changed or is missing.")
            if not _wan_recipe_choice_matches(role, resource_id, record):
                raise ValueError(f"This Video recipe needs the exact qualified Wan2.2 TI2V-5B "
                                 f"{role.replace('_', ' ')}. Choose that registered model before Preview Setup.")
            locations = record.get("locations", [])
            chosen = fields.get("model_locations", {}).get(role)
            if chosen is not None:
                if (not isinstance(chosen, (tuple, list)) or len(chosen) != 2
                        or chosen[0] != resource_id or not isinstance(chosen[1], str) or not chosen[1]
                        or not any(isinstance(row, dict) and isinstance(row.get("path"), str)
                            and Path(row["path"]) == Path(chosen[1]) for row in locations)):
                    raise ValueError(f"The selected {role} location changed. Choose that model again before previewing setup.")
                path = chosen[1]
            elif len(locations) == 1 and isinstance(locations[0].get("path"), str):
                path = locations[0]["path"]
            else:
                raise ValueError(f"The {role} location is missing or ambiguous. Choose that model again to verify one registered copy for this setup.")
            selected[role] = RuntimeModel(role, path, resource_id.removeprefix("sha256:"))
        from core.model_storage_policy import load_policy
        tiered = load_policy(value.get("workspace", {})).mode == "tiered"
        return RuntimeConfig(fields["source_root"], fields["python_executable"],
            fields["python_runtime_root"], selected["transformer"],
            selected["text_encoder"], selected["vae"], fields["workspace_root"],
            fields["port"], fields["free_gib"] * 1024**3, protected_loader=tiered)

    def _runtime_setup_fields(self):
        return {"models": self._models.copy(), "model_locations": self._model_locations.copy(),
            "source_root": self.source_root.text().strip(),
            "python_executable": self.python_executable.text().strip(),
            "python_runtime_root": self.python_runtime_root.text().strip(),
            "workspace_root": self.workspace_root.text().strip(),
            "port": self.port.value(), "free_gib": self.free_gib.value()}

    def preview_setup(self):
        if self._runtime_storage_locator is not None or self._runtime_receipt is not None:
            self._set_status("Close the retained preparation through recovery before changing runtime setup.")
            return
        fields = self._runtime_setup_fields()
        def prepare():
            return preview_runtime(self._model_config(fields), protected_roots=protected_model_roots(self.settings))
        def complete(preview):
            self._runtime_preview = preview
            self.model_paths.setText("\n".join(
                f"{role}: {item['path']} · SHA-256 {item['sha256']}" for role, item in preview.model_manifest.items()))
            self.section_bar.setCurrentIndex(2)
            self.runtime_technical_button.setChecked(True)
            if preview.config.protected_loader:
                self.storage_options_button.setChecked(True)
            self._set_status("Exact source, Python and all three model files verified. " +
                ("Choose Prepare Model Storage before starting this Fast-storage runtime." if preview.config.protected_loader else
                 "Review the paths and start this dedicated runtime deliberately."))
        self._run("Preview isolated Comfy setup", prepare, complete)

    def prepare_model_storage(self):
        if self._busy() or self._runtime_receipt is not None or self._runtime_start_uncertain:
            return
        if self._runtime_storage_locator is not None:
            self.recover_model_storage()
            return
        fields = self._runtime_setup_fields()
        self._run("Review video model selection", lambda: self._model_config(fields),
            lambda config: self._open_runtime_storage(config=config))

    def recover_model_storage(self):
        if self._busy() or self._runtime_receipt is not None:
            return
        locator = self._runtime_storage_locator
        if locator is None:
            chosen, _ = QFileDialog.getOpenFileName(self, "Recover Exact Video Model Preparation",
                str(self.storage_root / "runtime-preparations"), "Video runtime owner (comfy-runtime-owner.json)")
            if not chosen:
                return
            locator = Path(chosen)
        self._open_runtime_storage(locator=locator)

    def _open_runtime_storage(self, *, config=None, locator=None):
        if self._busy() or self._closed or self._shutdown_requested:
            return
        from .video_runtime_preparation_dialog import VideoRuntimePreparationDialog
        from .model_companion_workflow import resource_boundaries
        from core.model_workspace import _path, _pinned_parent
        snapshot, _ = self.registry.snapshot()
        protected, managed = resource_boundaries(snapshot, self.settings, protected_model_roots(self.settings))
        container = _path(self.storage_root / "runtime-preparations", (*protected, *managed))
        if config is not None:
            pending, ancestor = [], container
            while not ancestor.exists():
                pending.append(ancestor)
                ancestor = _path(ancestor.parent, (*protected, *managed))
            with _pinned_parent(ancestor, (*protected, *managed)):
                for directory in reversed(pending):
                    _path(directory, (*protected, *managed)).mkdir(exist_ok=True)
        child = VideoRuntimePreparationDialog(self.jobs, self.registry, self.settings, parent=self,
            config=deepcopy(config), locator=locator, storage_root=container,
            protected_roots=protected_model_roots(self.settings))
        self._runtime_storage_dialog = child
        self._runtime_storage_status = None
        child.locator_changed.connect(self._retain_runtime_storage_locator)
        child.state_changed.connect(self._runtime_storage_state_changed)
        child.background_work_finished.connect(self._advance_shutdown)
        child.finished.connect(lambda result: self._runtime_storage_finished(child, result))
        if locator is not None:
            self._retain_runtime_storage_locator(str(locator))
        self._update_actions()
        child.open()

    def _retain_runtime_storage_locator(self, locator):
        if locator:
            self._runtime_storage_locator = Path(locator)
            self.storage_status.setText("Retained model preparation: " + str(locator))
            self._update_actions()

    def _runtime_storage_state_changed(self, status):
        from .video_runtime_preparation_dialog import runtime_storage_state_title
        self.storage_status.setText(f"Model preparation: {runtime_storage_state_title(status.state)}\n{status.message}\n{status.locator}")
        if status.state in {"stopped", "discarded"}:
            self._runtime_storage_locator = None
            self._runtime_storage_status = None
            self._runtime_lifecycle = None
            self._runtime_start_uncertain = False
            self._runtime_preview = None
        else:
            self._runtime_storage_locator = Path(status.locator)
            if status.state != "bound":
                self._runtime_storage_status = None
            if status.state in {"bound", "prepared"}:
                self._runtime_start_uncertain = False
        self._update_actions()

    def _runtime_storage_finished(self, child, result):
        if self._runtime_storage_dialog is not child:
            return
        self._runtime_storage_dialog = None
        if result == QDialog.DialogCode.Accepted and child.selected_status is not None:
            self._apply_prepared_config(child.selected_config)
            self._runtime_lifecycle = child.selected_lifecycle
            self._runtime_storage_status = child.selected_status
            self._runtime_storage_locator = Path(child.selected_status.locator)
            self._runtime_start_uncertain = False
            self._set_status("The complete model set is prepared and protected. Start this dedicated runtime when ready.")
        self._update_actions()
        self._advance_shutdown()

    def _apply_prepared_config(self, config):
        if not isinstance(config, RuntimeConfig) or not config.protected_loader:
            raise ValueError("Read the exact prepared runtime configuration before using its models.")
        for key in ("source_root", "python_executable", "python_runtime_root", "workspace_root"):
            getattr(self, key).setText(str(getattr(config, key)))
        self.port.setValue(config.port)
        self.free_gib.setValue(max(1, (config.required_free_bytes + 1024**3 - 1) // 1024**3))
        for slot in ("transformer", "text_encoder", "vae"):
            model = getattr(config, slot)
            identity = "sha256:" + model.sha256
            self._models[slot] = identity
            self._model_locations[slot] = (identity, str(model.path))
        self._refresh_model_displays()
        self.model_paths.setText("\n".join(f"{slot}: {getattr(config, slot).path}"
            for slot in ("transformer", "text_encoder", "vae")))
        self._clean_setup_snapshot = self._setup_snapshot()

    def start_runtime(self):
        if self._busy() or self._runtime_receipt is not None or self._shutdown_requested:
            return
        if self._runtime_storage_locator is not None:
            if self._runtime_lifecycle is None or self._runtime_storage_status is None or self._runtime_start_uncertain:
                self._set_status("Recover the retained model preparation before starting its runtime.")
                return
            self._runtime_start_uncertain = True
            locator, lifecycle = self._runtime_storage_locator, self._runtime_lifecycle
            def start_prepared():
                self._check_runtime_boundaries()
                return lifecycle.start(locator, confirmed=True)
            self._run("Start isolated Comfy runtime", start_prepared,
                self._runtime_started, cancelable=False)
            return
        if self._runtime_preview is None:
            self._set_status("Preview Setup first."); return
        preview = self._runtime_preview
        if preview.config.protected_loader:
            self._set_status("Use Prepare Model Storage to review and protect the complete Fast-storage set first.")
            return
        self._runtime_start_preview = preview
        def start_manual():
            from core.model_storage_policy import load_policy
            if load_policy(self.registry.snapshot()[0].get("workspace", {})).mode == "tiered":
                raise ValueError("Storage policy changed. Prepare Model Storage before starting this runtime.")
            return start_runtime(preview, confirmed=True)
        self._run("Start isolated Comfy runtime",
                  start_manual,
                  self._runtime_started, cancelable=False)

    def _runtime_started(self, receipt):
        self._runtime_start_preview = None
        self._runtime_start_uncertain = False
        self._runtime_receipt = receipt
        self._runtime_stop_only = False
        self._clear_memory_status()
        self._set_status("Owned local runtime started. Prepare a request; existing shared Comfy processes are untouched.")

    def _typed_request(self, *, require_runtime=True):
        if require_runtime and self._runtime_stop_only:
            raise ValueError("Stop the runtime from the failed startup before preparing another request.")
        if self._loaded_future is not None:
            raise ValueError("This draft contains request fields these controls cannot execute. Keep its exact saved version or start a supported new draft.")
        if require_runtime and self._runtime_receipt is None:
            raise ValueError("Start the reviewed isolated runtime first.")
        seed_text = self.seed.text().strip()
        if not seed_text.isdecimal():
            self.section_bar.setCurrentIndex(0)
            self.request_details_button.setChecked(True)
            self.seed.setFocus()
            raise ValueError("Enter an exact nonnegative integer seed.")
        prompt = self.positive.toPlainText().strip()
        motion, camera = self.motion.text().strip(), self.camera.text().strip()
        if self.append_intent.isChecked():
            prompt = "\n".join(filter(None, (prompt, motion, camera)))
            motion = camera = ""
        source = self._source_ref if self.task_choice.currentData() == "i2v" else None
        if self.task_choice.currentData() == "i2v" and source is None:
            raise ValueError("Choose one stable source image for image to video.")
        if source is not None and self._source_image is not None and (
                self._source_image.width(), self._source_image.height()) != (
                self.frame_width.value(), self.frame_height.value()):
            self._native_crop_rect(self._source_image.width(), self._source_image.height(),
                self.frame_width.value(), self.frame_height.value())
            from core.comfy_video_adapter import expected_resize_transform
            source = replace(source, transform_sha256=expected_resize_transform(
                source.sha256, self._source_image.width(), self._source_image.height(),
                self.frame_width.value(), self.frame_height.value()))
        if self.frame_width.value() % 32 or self.frame_height.value() % 32 or (self.frames.value() - 1) % 4:
            raise ValueError("Use dimensions divisible by 32 and 4n+1 frames.")
        if (self.frame_width.value() > 1280 or self.frame_height.value() > 1280
                or self.frame_width.value() * self.frame_height.value() > 1280 * 704
                or self.frames.value() > 49):
            raise ValueError("The qualified native recipe allows at most 49 frames and a 1280 × 704 "
                "pixel canvas, with either side at most 1280. Larger native requests need memory qualification.")
        output_goal = self._video_output_goal()
        distinct_final = bool(output_goal and (output_goal["final_width"], output_goal["final_height"]) != (
            self.frame_width.value(), self.frame_height.value()))
        if output_goal is not None and output_goal["detail_preference"] != "faithful":
            raise ValueError("Only faithful fit-and-black-pad resizing is available for a final video goal.")
        if distinct_final and self.video_reserve_mib.value() < 2 * self.video_output_cap_mib.value():
            raise ValueError("Retained storage reserve must be at least twice the requested output cap.")
        fields = dict(task=self.task_choice.currentData(),
            model_resource_id=self._models["transformer"], positive_prompt=prompt,
            negative_prompt=self.negative.toPlainText(), motion=motion, camera=camera,
            width=self.frame_width.value(), height=self.frame_height.value(), frame_count=self.frames.value(),
            fps=Rational(self.fps.value(), 1), seed=int(seed_text), source_image=source,
            resources=(self._models["text_encoder"], self._models["vae"]),
            extra_fields={"output_goal": output_goal} if output_goal is not None else {},
            expected_output_bytes=(self.video_output_cap_mib.value() * 1024**2
                if distinct_final else None),
            storage_budget_bytes=(self.video_reserve_mib.value() * 1024**2
                if distinct_final else None))
        baseline = self._loaded_request
        request = (replace(baseline, **fields) if baseline is not None else
            VideoGenerationRequest(goal="", intent="new", audio_policy="silent", **fields))
        validate_video_output_goal(request)
        if require_runtime:
            selected = self._runtime_receipt.preview.model_bindings
            if any(request_id != selected[role]["resource_id"] for role, request_id in (
                    ("transformer", self._models["transformer"]),
                    ("text_encoder", self._models["text_encoder"]),
                    ("vae", self._models["vae"]))):
                raise ValueError("Selected components differ from the running runtime. Stop it and Preview Setup again.")
        return request

    def _source_provider(self, reference):
        if (self._source_ref is None or self._source_path is None or self._source_conversion is None or
                (reference.sha256, reference.byte_length, reference.source_state_sha256) !=
                (self._source_ref.sha256, self._source_ref.byte_length,
                 self._source_ref.source_state_sha256)):
            raise ValueError("The exact selected source is no longer attached.")
        raw, state = read_stable_training_source(self._source_path)
        if state["sha256"] != self._source_conversion.original_sha256:
            raise ValueError("The source image changed; choose it again.")
        converted = prepare_video_source(raw, self._source_path.suffix)
        if (converted.png_sha256 != reference.sha256 or
                len(converted.png_bytes) != reference.byte_length or
                converted.recipe != self._source_conversion.recipe):
            raise ValueError("The derived source PNG changed; choose the original again.")
        return converted.png_bytes

    def _source_state_provider(self, reference):
        if (self._source_ref is None or self._source_path is None or self._source_conversion is None or
                (reference.sha256, reference.byte_length, reference.source_state_sha256) !=
                (self._source_ref.sha256, self._source_ref.byte_length,
                 self._source_ref.source_state_sha256)):
            raise ValueError("The exact source state is no longer attached.")
        raw, state = read_stable_training_source(self._source_path)
        if state["sha256"] != self._source_conversion.original_sha256:
            raise ValueError("The source image changed; choose it again.")
        converted = prepare_video_source(raw, self._source_path.suffix)
        current = self._current_source_identity(self._source_path, converted,
            frame_reference_provider=self._frame_reference_provider)
        if digest(current) != reference.source_state_sha256:
            raise ValueError("Source Image Knowledge changed; choose the source again.")
        return deepcopy(self._source_snapshot)

    def _make_adapter(self):
        factory = self.adapter_factory
        if factory is None:
            from core.comfy_video_adapter import NativeComfyVideoAdapter
            factory = NativeComfyVideoAdapter
        return factory(self._runtime_receipt,
            source_image_provider=self._source_provider,
            source_state_provider=self._source_state_provider)

    def prepare_video(self):
        try:
            draft = self._typed_request()
        except Exception as exc:
            self._set_status(str(exc)); return
        context_revision = self._memory_context_revision
        def prepare():
            verify_runtime(self._runtime_receipt)
            adapter = self._make_adapter()
            request = adapter.bind_request_versions(draft)
            prepared = adapter.prepare(request, reviewed_loss=False)
            return adapter, request, prepared, getattr(adapter, "prepared_memory_observation", None)
        def complete(value):
            if context_revision != self._memory_context_revision:
                self._set_status("The video draft changed during preparation. Prepare the current request again.")
                return
            self._adapter, self._request, self._prepared, observation = value
            self.memory_status.setText(self._memory_guidance(observation))
            graph = self._prepared["graph_plan"]
            self.review.setPlainText(json.dumps({"request": self._request.to_dict(),
                "resources": graph["manifest"]["resources"],
                "graph": graph["graph"], "translation_report": graph["translation_report"],
                "final_stage": self._final_stage_review(self._request.extra_fields.get("output_goal")),
                "requires_review": graph["requires_review"]}, indent=2, ensure_ascii=False))
            self.review_loss.setVisible(bool(graph["requires_review"]))
            self.review_loss.setChecked(False)
            self._set_status("Review this exact graph and any translation loss before submission.")
        self._run("Prepare exact video graph", prepare, complete)

    @staticmethod
    def _memory_guidance(observation, *, stage="graph preparation"):
        observed_at = observation.get("observed_at") if isinstance(observation, dict) else None
        try:
            measured_at = datetime.fromisoformat(observed_at)
            if measured_at.tzinfo is None:
                raise ValueError
            timestamp = measured_at.astimezone(timezone.utc).strftime("%Y-%m-%d %H:%M:%S UTC")
        except (TypeError, ValueError):
            timestamp = "time unavailable"
        available = (observation.get("available_vram_gib")
                     if isinstance(observation, dict) else None)
        if (isinstance(observation, dict) and observation.get("status") == "measured"
                and type(available) in (int, float) and available >= 0):
            return (f"Free VRAM observed at {stage} ({timestamp}): {available:.1f} GiB. "
                    "It can change; this recipe's peak requirement is unknown, so fit is not proven.")
        return (f"Free VRAM was unavailable at {stage} ({timestamp}). "
                "This recipe's peak requirement is unknown, so fit is not proven.")

    @staticmethod
    def _receipt_status(receipt, *, recovered=False):
        state = receipt.get("state")
        prefix = "Recovered exact video job state" if recovered else "Video job ended"
        if state == "failed" and receipt.get("failure_class") == "out_of_memory":
            return (f"{prefix}: failed. Native history reported memory exhaustion. "
                    "Close other GPU work and consider fewer 4n+1 frames within the "
                    "qualified 49-frame limit. Review the revised request before a new, "
                    "deliberate submission. No automatic retry or settings change was made.")
        if state == "failed" and receipt.get("failure_class") == "insufficient_vram":
            return (f"{prefix}: blocked before native submission because observed free "
                    "VRAM was zero. Close other GPU work, then review current VRAM and "
                    "the request before a new, deliberate submission. No automatic "
                    "retry or settings change was made.")
        if state == "outcome_unknown":
            return (f"{prefix}: outcome unknown. Recover this exact native job before "
                    "considering another submission.")
        return f"{prefix}: {state}."

    def submit_video(self):
        if self._prepared is None or self._adapter is None:
            self._set_status("Prepare and review a video first."); return
        graph = self._prepared["graph_plan"]
        if graph["requires_review"] and not self.review_loss.isChecked():
            self._set_status("Review and acknowledge the displayed source crop or untranslated intent."); return
        context_revision = self._memory_context_revision
        submitted_adapter = self._adapter
        def submit():
            verify_runtime(self._runtime_receipt)
            prepared = deepcopy(self._prepared)
            if graph["requires_review"]:
                prepared["reviewed_loss"] = True
            return self.service.submit(submitted_adapter, prepared, confirmed=True)
        def complete(job):
            self._submitted_job = job
            self._job_id = job.id
            self._job_adapter = submitted_adapter
            self._job_memory_context_revision = context_revision
            self.jobs.observe(job.id, lambda finished: self.job_finished.emit({
                "id": finished.id, "state": finished.state,
                "result": deepcopy(finished.result), "error": finished.error}))
            self._job_timer.start()
            self._set_status("Video job submitted. Its exact request and prompt identity are retained for recovery.")
        self._run("Submit reviewed video", submit, complete, cancelable=False)

    def _poll_job(self):
        job = self.jobs.get(self._job_id) if self._job_id else None
        if job is None:
            self._job_timer.stop(); self._advance_shutdown(); return
        if job.state in {"queued", "running", "paused"}:
            self._set_status(job.message or f"Video job {job.state}…")
            return
        self._job_timer.stop()
        self._set_status(f"Video job {job.state}. Recover the retained receipt if its result is not shown.")
        self._update_actions()
        self._advance_shutdown()

    def _job_terminal(self, snapshot):
        if snapshot.get("id") != self._job_id:
            return
        self._job_timer.stop()
        receipt = snapshot.get("result")
        self._verified_receipt = receipt if isinstance(receipt, dict) and receipt.get("state") == "verified" else None
        if (isinstance(receipt, dict) and receipt.get("memory_at_execution")
                and self._job_memory_context_revision == self._memory_context_revision):
            self.memory_status.setText(self._memory_guidance(
                receipt["memory_at_execution"], stage="final execution revalidation"))
        if self._verified_receipt:
            self._set_status("Video fully verified. Preview it or send it to Ingest.")
        elif isinstance(receipt, dict):
            self._set_status(self._receipt_status(receipt))
        else:
            self._set_status(snapshot.get("error") or "Video outcome needs exact receipt recovery.")
        self._update_actions()
        self._advance_shutdown()

    def cancel_job(self):
        if self._job_id:
            self.jobs.cancel(self._job_id)
            self._set_status("Cancellation requested for this owned video job. Its receipt remains available.")

    def recover_job(self):
        if not self._job_id or self._job_adapter is None:
            self._set_status("This workspace has no submitted video job available to recover."); return
        job_id, job_adapter = self._job_id, self._job_adapter
        def complete(receipt):
            self._verified_receipt = receipt if receipt.get("state") == "verified" else None
            if (receipt.get("memory_at_execution")
                    and self._job_memory_context_revision == self._memory_context_revision):
                self.memory_status.setText(self._memory_guidance(
                    receipt["memory_at_execution"], stage="final execution revalidation"))
            self._set_status(self._receipt_status(receipt, recovered=True))
        self._run("Recover exact video job", lambda: self.service.recover(job_id, job_adapter),
                  complete, cancelable=False)

    def preview_video(self):
        if self._verified_receipt is None:
            return
        output = self._verified_receipt["outputs"][0]
        asset = VideoSourceAsset.from_dict(output["source_asset"])
        from .video_preview_dialog import VideoPreviewDialog
        player = VideoPreviewDialog(output["path"], asset,
            allowed_root=self.storage_root / "jobs", source_image=self._source_image,
            source_path=self._source_path, settings=self.settings,
            runtime_binding=self.runtime_binding, decoder_lifetime=self.decoder_lifetime,
            parent=self)
        self._preview_player = player
        player.background_work_finished.connect(self._advance_shutdown)
        player.finished.connect(self._advance_shutdown)
        player.exec()
        if self._preview_player is player:
            self._preview_player = None
        self._advance_shutdown()

    def finish_video(self):
        if self._verified_receipt is None:
            return
        output = self._verified_receipt["outputs"][0]
        from .video_finishing_dialog import VideoFinishingDialog
        request = VideoGenerationRequest.from_dict(output["generation"]["request"])
        dialog = VideoFinishingDialog(self.jobs, output["path"],
            VideoSourceAsset.from_dict(output["source_asset"]), request,
            allowed_root=self.storage_root / "jobs", storage_root=self.storage_root,
            verified_source=lambda: self.service.verified_video_outputs(self._verified_receipt),
            settings=self.settings, parent=self)
        self._finishing_dialog = dialog
        dialog.background_work_finished.connect(self._advance_shutdown)
        dialog.ingest_requested.connect(self.ingest_requested.emit)
        dialog.frame_ingest_requested.connect(self.frame_ingest_requested.emit)
        dialog.remix_requested.connect(self._remix_finished_frame)
        dialog.animate_requested.connect(self._animate_finished_frame)
        dialog.exec()
        if self._finishing_dialog is dialog:
            self._finishing_dialog = None
        self._advance_shutdown()

    def _remix_finished_frame(self, handoff):
        from .generation_workflow import open_generation_workspace
        finishing = self._finishing_dialog
        open_generation_workspace(self, paths=(Path(handoff["path"]),), remix=True,
            force_new=True, initial_task="remix",
            on_opened=lambda _draft: finishing.close() if finishing else None,
            source_context={"frame_reference_provider": handoff["frame_reference_provider"]})

    def _animate_finished_frame(self, handoff):
        from .video_generation_workflow import open_video_generation_workspace
        finishing = self._finishing_dialog
        open_video_generation_workspace(self, source_path=Path(handoff["path"]),
            on_opened=lambda _draft: finishing.close() if finishing else None,
            frame_reference_provider=handoff["frame_reference_provider"])

    def send_to_ingest(self):
        if self._verified_receipt is None:
            return
        def complete(outputs):
            output = outputs[0]
            self.ingest_requested.emit({"receipt_path": output["output_receipt"]["path"],
                "source_path": output["path"], "service": self.service,
                "receipt": deepcopy(self._verified_receipt)})
            self._set_status("Verified video opened in Ingest for explicit review and copy.")
        self._run("Recheck verified video for Ingest",
                  lambda: self.service.verified_video_outputs(self._verified_receipt),
                  complete, cancelable=False)

    def stop_runtime(self):
        if self._runtime_receipt is None and not self._runtime_start_uncertain:
            return
        if self._active_job_state() in {"queued", "running", "paused"}:
            self._set_status("Finish or cancel the owned job before stopping its runtime."); return
        def complete(_):
            self._runtime_stopped()
            self._set_status("Owned runtime stopped. Draft and retained job evidence remain available.")
        self._run("Stop owned Comfy runtime", self._stop_exact_runtime,
                  complete, cancelable=False)

    def _stop_exact_runtime(self):
        if self._runtime_storage_locator is not None:
            if self._runtime_lifecycle is None:
                raise ValueError("Recover the retained model preparation before stopping its exact runtime.")
            self._check_runtime_boundaries()
            status = self._runtime_lifecycle.stop(self._runtime_storage_locator, confirmed=True)
            if status.state != "stopped":
                raise ValueError("The exact runtime is still held. Recover its retained preparation before continuing.")
            return status
        if self._runtime_start_uncertain or getattr(self._runtime_receipt, "protected_verifier", None) is not None:
            raise ValueError("The protected runtime owner is unavailable. Its reservation remains held for recovery.")
        return stop_runtime(self._runtime_receipt, confirmed=True)

    def _check_runtime_boundaries(self):
        from .model_companion_workflow import resource_boundaries
        snapshot, _ = self.registry.snapshot()
        current = resource_boundaries(snapshot, self.settings, protected_model_roots(self.settings))
        if current != (self._runtime_lifecycle.protected_roots, self._runtime_lifecycle.managed_roots):
            raise ValueError("Library or managed model boundaries changed. Recover the retained model preparation before continuing.")

    def _runtime_stopped(self):
        self._runtime_receipt = None
        self._runtime_stop_only = False
        self._runtime_preview = None
        self._runtime_storage_locator = None
        self._runtime_storage_status = None
        self._runtime_lifecycle = None
        self._runtime_start_uncertain = False
        self._adapter = None
        self._prepared = None
        self.storage_status.setText("Runtime stopped. Existing Fast copies and Keep Warm choices remain.")
        self._clear_memory_status()

    def _active_job_state(self):
        job = self.jobs.get(self._job_id) if self._job_id else None
        if job is None:
            job = self._submitted_job
        return str(getattr(job, "state", ""))

    def has_active_background_work(self) -> bool:
        """True while a worker, exact job, held process, or decoder remains owned."""
        player = self._preview_player
        finishing = self._finishing_dialog
        return bool(self._task is not None or self._runtime_receipt is not None or self._runtime_start_uncertain
            or (self._runtime_storage_dialog is not None and self._runtime_storage_dialog.has_active_background_work())
            or (self._pack_dialog is not None and self._pack_dialog.has_active_background_work())
            or self._active_job_state() in {"queued", "running", "paused"}
            or (player is not None and not getattr(player, "_finished_close", False))
            or (finishing is not None and not getattr(finishing, "_finished_close", False))
            or (self._shutdown_requested and self._shutdown_reconcile_needed))

    def request_background_shutdown(self) -> bool:
        """Drain exact owned work before the application closes its coordinator."""
        if not self._shutdown_requested:
            self._shutdown_requested = True
            self._set_status("Stopping this video's owned work and retaining its recovery evidence…")
        elif self._shutdown_stop_failed:
            # A timer tick cannot repeat a failed process stop. A new explicit
            # shutdown request may retry only this same held receipt.
            self._shutdown_stop_failed = False
        self._shutdown_timer.start()
        self._update_actions()
        self._advance_shutdown()
        return self.has_active_background_work()

    def resume_after_cancelled_shutdown(self) -> bool:
        """Restore editable draft controls after a later app-close guard declined."""
        if not self._shutdown_requested or self.has_active_background_work():
            return False
        self._shutdown_timer.stop()
        if self._pack_dialog is not None:
            self._pack_dialog.resume_after_cancelled_shutdown()
        self._shutdown_requested = False
        self._shutdown_notified = False
        self._shutdown_cancelled_task = ""
        self._shutdown_cancelled_job = ""
        self._shutdown_reconcile_needed = False
        self._shutdown_reconcile_failed = False
        self._shutdown_stop_failed = False
        self._shutdown_player_requested = False
        self._set_status("Application close was canceled. This video draft is editable; no runtime or job restarted.")
        self._update_actions()
        return True

    def _advance_shutdown(self):
        if not self._shutdown_requested or self._shutdown_notified:
            return
        if self._runtime_storage_dialog is not None:
            child = self._runtime_storage_dialog
            if child.has_active_background_work():
                child.request_background_shutdown()
                return
            child.reject()
            if self._runtime_storage_dialog is not None:
                return
            if self._shutdown_notified:
                return
        if self._pack_dialog is not None and self._pack_dialog.has_active_background_work():
            self._pack_dialog.request_background_shutdown()
            return
        player = self._preview_player
        if player is not None and not getattr(player, "_finished_close", False):
            if not self._shutdown_player_requested:
                self._shutdown_player_requested = True
                player.request_background_shutdown()
        finishing = self._finishing_dialog
        if finishing is not None and not getattr(finishing, "_finished_close", False):
            finishing.request_background_shutdown()
        if self._task is not None:
            task = self._task
            if (self._task_cancelable and task.job_id and
                    self._shutdown_cancelled_task != task.job_id):
                self._shutdown_cancelled_task = task.job_id
                try:
                    self.jobs.cancel(task.job_id)
                except Exception as exc:
                    self._set_status(f"Waiting for exact background work: {exc}")
            return  # Cancellation is a request; its observer must deliver terminal state.
        state = self._active_job_state()
        if state in {"queued", "running", "paused"}:
            self._shutdown_reconcile_needed = True
            if self._job_id and self._shutdown_cancelled_job != self._job_id:
                self._shutdown_cancelled_job = self._job_id
                try:
                    self.jobs.cancel(self._job_id)
                except Exception as exc:
                    self._set_status(f"Waiting for exact video job: {exc}")
            return
        if self._shutdown_reconcile_needed:
            if self._job_adapter is None or not self._job_id:
                self._shutdown_reconcile_failed = True
                self._shutdown_reconcile_needed = False
                self._set_status("Exact video job cannot be reconciled; its durable receipt remains for recovery.")
                # The held process still needs an exact owned shutdown.
                self._advance_shutdown()
                return
            def reconciled(receipt):
                self._shutdown_reconcile_needed = False
                self._verified_receipt = receipt if receipt.get("state") == "verified" else None
                self._set_status(f"Exact video job reconciled as {receipt['state']} before shutdown.")
            job_id, job_adapter = self._job_id, self._job_adapter
            self._run("Reconcile exact video job",
                lambda: self.service.recover(job_id, job_adapter),
                reconciled, cancelable=False)
            return
        if self._runtime_receipt is not None or self._runtime_start_uncertain:
            if self._shutdown_stop_failed:
                return  # Never claim completion while the owned process remains held.
            def stopped(_):
                self._runtime_stopped()
                self._set_status("Owned video runtime stopped; exact job remains for receipt recovery."
                    if self._shutdown_reconcile_failed else
                    "Owned video runtime stopped; retained job evidence remains available.")
            self._run("Stop owned Comfy runtime",
                self._stop_exact_runtime,
                stopped, cancelable=False)
            return
        if player is not None and not getattr(player, "_finished_close", False):
            return
        if finishing is not None and not getattr(finishing, "_finished_close", False):
            return
        self._shutdown_notified = True
        self._shutdown_timer.stop()
        self.background_work_finished.emit()

    def _draft_request(self):
        if (self._loaded_authoring is not None and
                self._loaded_display_snapshot == self._draft_display_snapshot()):
            return self._loaded_authoring
        if self._loaded_request is not None and (self._loaded_future is not None or
                self._loaded_display_snapshot == self._draft_display_snapshot()):
            return self._loaded_request
        try:
            return self._typed_request(require_runtime=False)
        except ValueError:
            return self._authoring_draft()

    def _authoring_draft(self):
        retained_source = (self._source_ref or
            (self._loaded_authoring.controls["source_image"] if self._loaded_authoring else None) or
            (self._loaded_request.source_image if self._loaded_request else None))
        if isinstance(retained_source, ImageSourceReference):
            retained_source = retained_source.to_dict()
        controls = {"task": self.task_choice.currentData(),
            "positive": self.positive.toPlainText(), "negative": self.negative.toPlainText(),
            "motion": self.motion.text(), "camera": self.camera.text(),
            "append_intent": self.append_intent.isChecked(),
            "width": self.frame_width.value(), "height": self.frame_height.value(),
            "frames": self.frames.value(), "fps": self.fps.value(), "seed": self.seed.text(),
            "final_width": self.video_final_width.value(),
            "final_height": self.video_final_height.value(),
            "detail_preference": self.video_detail_preference.currentData(),
            "output_cap_mib": self.video_output_cap_mib.value(),
            "reserve_mib": self.video_reserve_mib.value(),
            "models": dict(self._models),
            "source_image": retained_source if self.task_choice.currentData() == "i2v" else None}
        return VideoAuthoringDraft(controls, self._loaded_request)

    def _draft_display_snapshot(self):
        return (self.task_choice.currentData(), self.positive.toPlainText(),
            self.negative.toPlainText(), self.motion.text(), self.camera.text(),
            self.append_intent.isChecked(), self.frame_width.value(), self.frame_height.value(),
            self.frames.value(), self.fps.value(), self.seed.text(),
            self.video_final_width.value(), self.video_final_height.value(),
            self.video_detail_preference.currentData(), self.video_output_cap_mib.value(),
            self.video_reserve_mib.value(), tuple(sorted(self._models.items())),
            self._source_ref)

    def _draft_dirty(self):
        return self._draft_display_snapshot() != self._clean_draft_snapshot

    def _setup_dirty(self):
        return self._setup_snapshot() != self._clean_setup_snapshot

    def _loss_snapshot(self):
        return self._draft_display_snapshot(), self._setup_snapshot()

    def _confirm_draft_loss_choice(self):
        box = QMessageBox(self)
        box.setWindowTitle("Unsaved video work")
        if self._draft_dirty() and self._setup_dirty():
            box.setText("Save your video draft and local setup before leaving?")
        elif self._draft_dirty():
            box.setText("Save your video draft before leaving?")
        else:
            box.setText("Save your local video setup before leaving?")
        box.setInformativeText("Save writes changed draft settings to a new file and saves changed local runtime settings. "
            "Discard leaves existing saved files untouched. Cancel keeps this workspace open.")
        box.setIcon(QMessageBox.Warning)
        box.setStandardButtons(QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel)
        box.setDefaultButton(QMessageBox.Save)
        result = box.exec()
        return {QMessageBox.Save: "save", QMessageBox.Discard: "discard"}.get(result, "cancel")

    def _save_changes_then(self, completed):
        def finish():
            self._draft_close_pending = False
            if callable(completed):
                completed()
            else:
                self.draft_save_finished.emit()
        def save_setup():
            if self._setup_dirty():
                self.save_local_setup(on_saved=finish)
            else:
                finish()
        if self._draft_dirty():
            if not self.save_draft(on_saved=save_setup):
                self._draft_close_pending = False
                return False
        else:
            save_setup()
        return True

    def confirm_draft_loss(self) -> bool:
        """App-close preflight; False means cancelled, invalid, or save in flight."""
        if self._draft_close_pending or self._busy():
            return False
        if not (self._draft_dirty() or self._setup_dirty()) or self._loss_snapshot() == self._discarded_close_snapshot:
            return True
        choice = self._confirm_draft_loss_choice()
        if choice == "discard":
            self._discarded_close_snapshot = self._loss_snapshot()
            return True
        if choice != "save":
            return False
        self._draft_close_pending = True
        self._save_changes_then(None)
        return False

    def _guard_draft_loss(self, completed):
        if self._draft_close_pending or self._busy():
            return
        if not (self._draft_dirty() or self._setup_dirty()) or self._loss_snapshot() == self._discarded_close_snapshot:
            completed(); return
        choice = self._confirm_draft_loss_choice()
        if choice == "discard":
            self._discarded_close_snapshot = self._loss_snapshot()
            completed()
        elif choice == "save":
            self._draft_close_pending = True
            self._save_changes_then(completed)

    def save_draft(self, *, on_saved=None):
        try:
            request = self._draft_request()
        except Exception as exc:
            self._set_status(f"Video draft could not be saved: {exc}"); return False
        try:
            self.storage_root.mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            self._set_status(f"Video draft location is unavailable: {exc}"); return False
        path, _ = QFileDialog.getSaveFileName(self, "Save new video draft",
            str(self.storage_root / f"video-draft-{uuid4().hex}.json"), "Video draft (*.json)")
        if not path:
            return False
        target = Path(path).absolute()
        if target.exists() or target.suffix.casefold() != ".json":
            self._set_status("Choose a new .json filename. Existing drafts are never overwritten."); return False
        snapshot = self._draft_display_snapshot()
        def complete(value):
            if self._draft_display_snapshot() == snapshot:
                self._clean_draft_snapshot = snapshot
            self._set_status(f"Saved video draft: {value.name}")
            self._update_action_presentation()
            if callable(on_saved):
                on_saved()
        self._run("Save video draft", lambda: save_video_draft(target, request),
                  complete,
                  cancelable=False)
        return True

    def load_draft(self):
        if self._runtime_storage_locator is not None or self._busy():
            self._set_status("Close the retained preparation through recovery before loading another draft.")
            return
        self._guard_draft_loss(self._choose_draft_to_load)

    def _choose_draft_to_load(self):
        path, _ = QFileDialog.getOpenFileName(self, "Load video draft",
            str(self.storage_root), "Video draft (*.json)")
        if not path:
            return
        target = Path(path).absolute()
        self._run("Load video draft", lambda: load_video_draft(target), self._restore_draft)

    def _restore_draft(self, request):
        if self._runtime_receipt is not None or self._job_id or self._runtime_storage_locator is not None:
            raise ValueError("Stop the current runtime and use a separate video workspace for another draft.")
        if isinstance(request, VideoAuthoringDraft):
            return self._restore_authoring_draft(request)
        self._reset_draft()
        self.task_choice.setCurrentIndex(self.task_choice.findData(request.task))
        self.positive.setPlainText(request.positive_prompt)
        self.negative.setPlainText(request.negative_prompt)
        self.motion.setText(request.motion)
        self.camera.setText(request.camera)
        # Neutral temporal requests may exceed the qualified native recipe. Show their
        # values exactly for inspection; the unsupported guard below prevents execution.
        self.frame_width.setMaximum(max(1280, request.width))
        self.frame_height.setMaximum(max(1280, request.height))
        self.frames.setMaximum(max(49, request.frame_count))
        self.frame_width.setValue(request.width); self.frame_height.setValue(request.height)
        self.frames.setValue(request.frame_count)
        self.fps.setValue(int(request.fps.fraction))
        self.seed.setText(str(request.seed))
        output_goal = request.extra_fields.get("output_goal")
        recognized_goal = (isinstance(output_goal, dict) and
            set(output_goal) == {"final_width", "final_height", "detail_preference"} and
            all(type(output_goal.get(key)) is int and 64 <= output_goal[key] <= 4096
                and output_goal[key] % 2 == 0
                for key in ("final_width", "final_height")) and
            output_goal["final_width"] * output_goal["final_height"] <= 8_294_400 and
            output_goal.get("detail_preference") in {"faithful", "generative"})
        if recognized_goal:
            self.video_final_width.setValue(output_goal["final_width"])
            self.video_final_height.setValue(output_goal["final_height"])
            self.video_detail_preference.setCurrentIndex(max(0, self.video_detail_preference.findData(
                output_goal.get("detail_preference", "faithful"))))
        cap = request.expected_output_bytes
        reserve = request.storage_budget_bytes
        cap_mib = cap // 1024**2 if type(cap) is int and cap % 1024**2 == 0 else None
        reserve_mib = reserve // 1024**2 if type(reserve) is int and reserve % 1024**2 == 0 else None
        if cap_mib is not None and 1 <= cap_mib <= 256:
            self.video_output_cap_mib.setValue(cap_mib)
        if reserve_mib is not None and 2 <= reserve_mib <= 4096:
            self.video_reserve_mib.setValue(reserve_mib)
        self._models = {"transformer": request.model_resource_id,
            "text_encoder": request.resources[0] if len(request.resources) == 2 else "",
            "vae": request.resources[1] if len(request.resources) == 2 else ""}
        self._model_locations.clear()
        self._refresh_model_displays()
        if request.source_image is not None:
            self.source_label.setText("Source lineage retained; reattach the exact image before execution.")
        self._loaded_request = request
        unsupported = (not request.schema_supported or set(request.extra_fields) - {"output_goal"}
            or ("output_goal" in request.extra_fields and not recognized_goal)
            or (recognized_goal and output_goal["detail_preference"] != "faithful")
            or request.fps.extra_fields or request.fps.fraction.denominator != 1
            or not 1 <= request.fps.fraction <= 120
            or request.width < 32 or request.height < 32
            or request.width % 32 or request.height % 32
            or request.width > 1280 or request.height > 1280
            or request.width * request.height > 1280 * 704
            or request.frame_count > 49 or (request.frame_count - 1) % 4
            or request.intent != "new" or request.stages or request.overrides
            or request.translation_report
            or (cap is not None and (cap_mib is None or not 1 <= cap_mib <= 256))
            or (reserve is not None and (reserve_mib is None or not 2 <= reserve_mib <= 4096))
            or (recognized_goal and output_goal["detail_preference"] == "faithful"
                and (output_goal["final_width"], output_goal["final_height"]) !=
                (request.width, request.height) and
                (cap_mib is None or reserve_mib is None or reserve_mib < 2 * cap_mib))
            or (not recognized_goal and (cap is not None or reserve is not None))
            or (recognized_goal and (output_goal["final_width"], output_goal["final_height"])
                == (request.width, request.height) and (cap is not None or reserve is not None))
            or request.template_version or request.adapter_version or request.runtime_version
            or len(request.resources) not in {0, 2}
            or (request.source_image is not None and (
                request.source_image.extra_fields or request.source_image.schema_version != 1
                or request.source_image.schema_id != "collectigen.video-image-source.v1")))
        self._loaded_future = request if unsupported else None
        self._loaded_display_snapshot = self._draft_display_snapshot()
        self._clean_draft_snapshot = self._loaded_display_snapshot
        self.request_form.setRowVisible(self._loaded_draft_row, bool(unsupported))
        if unsupported:
            self.loaded_draft_note.setText("Exact request retained for inspect and save only. "
                f"Its FPS is {request.fps.numerator}/{request.fps.denominator}; "
                "one or more request fields cannot be represented by these controls. "
                "Start a new draft to use the current native recipe.")
            self.review.setPlainText(json.dumps(request.to_dict(), indent=2, ensure_ascii=False))
        self._refresh_request_summary()
        self._refresh_video_output_summary()
        self._set_status(
            "Exact draft retained for inspect and save; its fields cannot all execute in these controls."
            if self._loaded_future is not None else
            "Draft loaded. Reattach the exact source, then review models and local runtime with Preview Setup."
            if request.source_image is not None else
            "Draft loaded. Review models and local runtime with Preview Setup before execution."
        )
        self._update_actions()

    def _restore_authoring_draft(self, draft: VideoAuthoringDraft):
        if draft.baseline is not None:
            self._restore_draft(draft.baseline)
        else:
            self._reset_draft()
        values = draft.controls
        self.task_choice.setCurrentIndex(self.task_choice.findData(values["task"]))
        self.positive.setPlainText(values["positive"])
        self.negative.setPlainText(values["negative"])
        self.motion.setText(values["motion"])
        self.camera.setText(values["camera"])
        self.append_intent.setChecked(values["append_intent"])
        self.frame_width.setMaximum(max(1280, values["width"]))
        self.frame_height.setMaximum(max(1280, values["height"]))
        self.frames.setMaximum(max(49, values["frames"]))
        self.frame_width.setValue(values["width"])
        self.frame_height.setValue(values["height"])
        self.frames.setValue(values["frames"])
        self.fps.setValue(values["fps"])
        self.seed.setText(values["seed"])
        self.video_final_width.setValue(values["final_width"])
        self.video_final_height.setValue(values["final_height"])
        self.video_detail_preference.setCurrentIndex(
            self.video_detail_preference.findData(values["detail_preference"]))
        self.video_output_cap_mib.setValue(values["output_cap_mib"])
        self.video_reserve_mib.setValue(values["reserve_mib"])
        self._models = dict(values["models"])
        self._model_locations.clear()
        self._refresh_model_displays()
        if values["source_image"] is not None:
            self.source_label.setText("Source lineage retained; reattach the exact image before execution.")
        self._loaded_authoring = draft
        self._loaded_display_snapshot = self._draft_display_snapshot()
        self._clean_draft_snapshot = self._loaded_display_snapshot
        self._refresh_request_summary()
        self._refresh_video_output_summary()
        self._set_status("Unfinished draft loaded. Complete the missing source or model choices, "
            "then review local runtime with Preview Setup before execution.")
        self._update_actions()

    def new_draft(self):
        if self._runtime_receipt is not None or self._job_id or self._runtime_storage_locator is not None or self._busy():
            self._set_status("Stop the owned runtime and retain or recover its job before starting a new draft.")
            return
        self._guard_draft_loss(self._reset_draft)

    def _reset_draft(self):
        if self._runtime_receipt is not None or self._job_id or self._runtime_storage_locator is not None:
            self._set_status("Stop the owned runtime and retain or recover its job before starting a new draft.")
            return
        self._loaded_future = None
        self._loaded_request = None
        self._loaded_authoring = None
        self._loaded_display_snapshot = None
        self.request_form.setRowVisible(self._loaded_draft_row, False)
        self.review.clear()
        self._source_path = None; self._source_raw = None; self._source_ref = None
        self._source_snapshot = None; self._source_image = None; self._source_conversion = None
        self._frame_reference_provider = None
        self._source_preview_key = None
        self.source_preview.clear("Source preview")
        self.native_source_preview.clear("Native frame input preview")
        self.native_source_label.setText("Choose a source to review the native crop and resize.")
        self.source_label.setText("No source image chosen")
        self.task_choice.setCurrentIndex(0)
        self.positive.clear(); self.negative.clear(); self.motion.clear(); self.camera.clear()
        self.append_intent.setChecked(False)
        self.frame_width.setMaximum(1280); self.frame_height.setMaximum(1280)
        self.frames.setMaximum(49)
        self.frame_width.setValue(1280); self.frame_height.setValue(704)
        self.frames.setValue(49); self.fps.setValue(24); self.seed.setText("0")
        self.video_final_width.setValue(0); self.video_final_height.setValue(0)
        self.video_detail_preference.setCurrentIndex(0)
        self.video_output_cap_mib.setValue(256); self.video_reserve_mib.setValue(512)
        for role in self._models:
            self._models[role] = ""
        self._model_locations.clear()
        self._refresh_model_displays()
        self._invalidate()
        self._clean_draft_snapshot = self._draft_display_snapshot()

    def _finish_close_workspace(self):
        self._closed = True
        self._job_timer.stop()
        self.workspace_closed.emit()
        self.hide()

    def close_workspace(self):
        if self._shutdown_requested:
            self._set_status("Video shutdown is finishing exact owned work before the app closes."); return
        if self._busy():
            self._set_status("Wait for setup or verification to finish before closing."); return
        if self.has_active_background_work():
            self.hide()
            self._set_status("Workspace hidden; its owned background work continues. Reopen to monitor, recover or stop it.")
            return
        self._guard_draft_loss(self._finish_close_workspace)

    def closeEvent(self, event):
        event.ignore()
        self.close_workspace()

    def reject(self):
        self.close_workspace()

    def open_dependency_pack(self):
        if self._shutdown_requested:
            return
        if self._pack_dialog is None:
            from .video_dependency_pack_dialog import VideoDependencyPackDialog
            self._pack_dialog = VideoDependencyPackDialog(self.jobs, self.settings, self,
                selection_builder=self._build_pack_selection)
            self._pack_dialog.background_work_finished.connect(self._advance_shutdown)
        self._pack_dialog.show()
        self._pack_dialog.raise_()

    def _build_pack_selection(self, *, should_cancel=None):
        preview = self._runtime_preview or (self._runtime_receipt.preview if self._runtime_receipt else None)
        if preview is None:
            raise ValueError("Preview Setup to verify the runtime and models before building their offline pack.")
        from core.video_dependency_pack_selection import build_runtime_pack_selection
        return build_runtime_pack_selection(preview, should_cancel=should_cancel)

    def hideEvent(self, event):
        self.source_preview.prepare_for_privacy_capture()
        self.native_source_preview.prepare_for_privacy_capture()
        super().hideEvent(event)

    def prepare_for_privacy_capture(self):
        self.source_preview.prepare_for_privacy_capture()
        self.native_source_preview.prepare_for_privacy_capture()

    def _update_actions(self, *_args):
        self._refresh_runtime_summary()
        busy = self._busy()
        retained = self._runtime_storage_locator is not None
        if retained:
            self.storage_options_button.setChecked(True)
        setup_locked = retained or self._runtime_receipt is not None
        active = bool(self._job_id and self.jobs.get(self._job_id) and
            self.jobs.get(self._job_id).state in {"queued", "running", "paused"})
        self.preview_setup_button.setEnabled(not busy and not setup_locked
            and self._loaded_future is None)
        ready = (self._runtime_lifecycle is not None and self._runtime_storage_status is not None) if retained else (
            self._runtime_preview is not None and not getattr(self._runtime_preview.config, "protected_loader", False))
        self.start_button.setEnabled(not busy and ready and not self._runtime_start_uncertain and self._runtime_receipt is None)
        self.prepare_storage_button.setEnabled(not busy and not setup_locked and self._loaded_future is None)
        self.recover_storage_button.setEnabled(not busy and self._runtime_receipt is None)
        self.setup_jump_button.setText(
            "Review Model Recovery…" if retained else "Set Up Local Video…"
        )
        self.setup_jump_button.setToolTip(
            "Go to the retained model preparation and its recovery control."
            if retained else "Go to the existing ComfyUI and Python setup fields in this draft."
        )
        self.prepare_button.setEnabled(not busy and self._runtime_receipt is not None and not self._runtime_stop_only and not active
            and self._loaded_future is None)
        self.submit_button.setEnabled(not busy and self._prepared is not None and not active
            and (not self._prepared["graph_plan"]["requires_review"] or self.review_loss.isChecked()))
        self.cancel_job_button.setEnabled(not busy and active)
        self.recover_button.setEnabled(not busy and bool(self._job_id)
            and self._job_adapter is not None and not active
            and self._verified_receipt is None)
        self.preview_video_button.setEnabled(not busy and self._verified_receipt is not None)
        self.finish_video_button.setEnabled(not busy and self._verified_receipt is not None)
        self.ingest_button.setEnabled(not busy and self._verified_receipt is not None)
        self.stop_button.setEnabled(not busy and (self._runtime_receipt is not None or self._runtime_start_uncertain) and not active)
        self.cancel_step_button.setEnabled(busy and self._task_cancelable)
        self.review_loss.setVisible(bool(self._prepared and self._prepared["graph_plan"]["requires_review"]))
        self.review.setVisible(self._prepared is not None or self._loaded_future is not None)
        self.scroll.setEnabled(not busy and not active and self._loaded_future is None)
        for button in self.model_buttons.values():
            button.setEnabled(not busy and not active and not setup_locked)
        for widget in (self.transformer_role, self.source_root, self.python_executable,
                       self.python_runtime_root, self.workspace_root, self.port, self.free_gib):
            widget.setEnabled(not busy and not active and not setup_locked)
        for button in (self.suggest_workspace_button, self.save_setup_button,
                       self.reload_setup_button, *self.runtime_browse_buttons):
            button.setEnabled(not busy and not active and not setup_locked)
        self.save_button.setEnabled(not busy and not active)
        self.load_button.setEnabled(not busy and not active and not setup_locked
            and not self._job_id)
        self.new_button.setEnabled(not busy and not active and not setup_locked and not self._job_id)
        if self._shutdown_requested:
            self.scroll.setEnabled(False)
            for button in (self.preview_setup_button, self.start_button, self.prepare_button,
                           self.prepare_storage_button, self.recover_storage_button,
                           self.submit_button, self.cancel_job_button, self.recover_button,
                           self.preview_video_button, self.finish_video_button, self.ingest_button, self.stop_button,
                           self.cancel_step_button, self.save_button, self.load_button,
                           self.new_button, self.close_button, self.suggest_workspace_button,
                           self.save_setup_button, self.reload_setup_button,
                           *self.runtime_browse_buttons,
                           *self.model_buttons.values()):
                button.setEnabled(False)
        self._update_action_presentation()

    def _update_action_presentation(self, *_args):
        """Disclose the existing action buttons without changing their safety gates."""
        if self.more_actions_button.isChecked():
            chosen = list(self._action_buttons)
        else:
            chosen = []
            if self._busy():
                if self.cancel_step_button.isEnabled():
                    chosen.append(self.cancel_step_button)
            elif self.cancel_job_button.isEnabled():
                chosen.append(self.cancel_job_button)
            elif self.recover_button.isEnabled():
                chosen.append(self.recover_button)
            elif self.stop_button.isEnabled() and (
                self._runtime_start_uncertain or self._runtime_stop_only
            ):
                chosen.append(self.stop_button)
            elif self._verified_receipt is not None:
                chosen.extend((self.preview_video_button, self.finish_video_button,
                               self.ingest_button))
            elif self._prepared is not None:
                # Keep the acknowledgment gate visible even while Submit is disabled.
                chosen.append(self.submit_button)
            elif self.prepare_button.isEnabled():
                chosen.append(self.prepare_button)
            elif self.start_button.isEnabled():
                chosen.append(self.start_button)
            elif self._loaded_future is None and not self._runtime_storage_locator:
                chosen.append(self.preview_setup_button)
            for safety in (self.cancel_step_button, self.cancel_job_button,
                           self.recover_button, self.stop_button):
                if safety.isEnabled() and safety not in chosen:
                    chosen.append(safety)
            if self._verified_receipt is not None:
                for result in (self.preview_video_button, self.finish_video_button,
                               self.ingest_button):
                    if result not in chosen:
                        chosen.append(result)
            if (self.save_button.isEnabled() and
                    (self._draft_dirty() or self._loaded_authoring is not None
                     or self._loaded_future is not None)):
                chosen.append(self.save_button)
        self.more_actions_button.setText(
            "Fewer Video Actions" if self.more_actions_button.isChecked()
            else "More Video Actions…"
        )
        order = tuple((*chosen, self.more_actions_button, self.close_button))
        if order == self._shown_action_buttons:
            return
        for button in (*self._action_buttons, self.more_actions_button, self.close_button):
            self._action_layout.removeWidget(button)
            button.hide()
        for index, button in enumerate(order):
            self._action_layout.addWidget(button, index // 4, index % 4)
            button.show()
        self._shown_action_buttons = order
