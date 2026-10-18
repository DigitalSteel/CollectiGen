"""Shared resolution policy, exact resize preview and normal Ingest handoff."""
from __future__ import annotations

from dataclasses import asdict
from pathlib import Path

from PySide6.QtCore import QObject, Signal, Qt, QTimer
from PySide6.QtWidgets import (QButtonGroup, QCheckBox, QComboBox, QDialog, QDoubleSpinBox,
    QFileDialog, QFormLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton,
    QRadioButton, QScrollArea, QTabWidget, QTextEdit, QVBoxLayout, QWidget)

from core.image_derivatives import DerivativeIngestHandoff, DerivativeStore, preview_resize
from core.paths import CACHE_DIR, USER_DATA_DIR
from core.resolution_policy import DimensionCache, ResolutionInspector, ResolutionPolicy, calculate_target_dimensions
from core.resolution_workflow import load_resolution_settings, save_resolution_settings, execute_resize_batch, validate_resolution_access
from .command_bar import DialogCommandController, bind_source
from .components import DisclosureButton
from .workers import CoordinatedCallTask
from .workflow_context import WorkflowContextSummary
from .status_notifications import FooterMessageBox as QMessageBox


class _ResolutionEvents(QObject):
    policy_changed = Signal()


_events = None


def resolution_events():
    global _events
    if _events is None:
        _events = _ResolutionEvents()
    return _events


def _host(widget):
    while widget is not None:
        if getattr(getattr(widget, "state", None), "jobs", None) is not None:
            return widget
        widget = widget.parent()
    raise ValueError("Open resolution review from the application workspace.")


def _guard(host, paths, output_root=None, *, dimensions_only=False):
    from .gallery import content_safety_policy
    registry = host.state.settings.library_registry()
    validate_resolution_access(registry, paths,
        ingest_directory=getattr(host.state.settings, "ingest_directory", ""), output_root=output_root)
    safety = None if dimensions_only else content_safety_policy()
    for path in paths:
        if safety and safety.should_blur(path):
            raise ValueError("Review library and content-safety access before resizing these images.")


class ResolutionDialog(QDialog):
    ingest_requested = Signal(object)
    generative_requested = Signal(object)
    policy_saved = Signal()
    _completed = Signal(object)
    _failed = Signal(str)

    def __init__(self, jobs, paths=(), *, parent=None, settings_file=None, guard=lambda *_: None,
                 model_registry=None, protected_model_roots=(), inspection_guard=None, purpose=None):
        super().__init__(parent)
        self.jobs, self.paths = jobs, tuple(dict.fromkeys(Path(p) for p in paths))
        self.purpose = purpose or ("inspection" if self.paths else "settings")
        titles = {"inspection": "Review Image Dimensions", "resize": "Resize Images",
                  "settings": "Resolution Policy & Automatic Resizing"}
        self.setWindowTitle(titles[self.purpose])
        self.resize(820, 520) if self.purpose == "inspection" else self.resize(980, 740)
        self._resize_dialog = None
        self._generation_handoff_pending = False
        self._checking_model = False
        self.settings_file, self.guard = settings_file, guard
        self.inspection_guard = inspection_guard if inspection_guard is not None else guard
        self._inspection_state = "ready"
        self._inspection_summary = ""
        self.model_registry = model_registry
        self.protected_model_roots = tuple(protected_model_roots)
        self._task = None
        self._done = None
        self.plans, self.results = [], []
        self._closed = False
        self._completed.connect(self._receive, Qt.ConnectionType.QueuedConnection)
        self._failed.connect(self._failure, Qt.ConnectionType.QueuedConnection)
        layout = QVBoxLayout(self)
        title = QLabel(f"Images in scope: {len(self.paths):,}")
        title.setObjectName("mutedLabel")
        layout.addWidget(title)
        title.setVisible(self.purpose != "settings")
        help_text = QLabel("Resolution describes current pixel dimensions. Technical quality is not assessed. Originals are preserved.")
        if self.purpose == "settings":
            help_text.setText("Save Resolution Settings saves both preferred dimensions and automatic resizing settings across collections.")
        help_text.setWordWrap(True)
        layout.addWidget(help_text)
        help_text.setVisible(self.purpose != "resize")
        self.context_summary = WorkflowContextSummary(self)
        layout.addWidget(self.context_summary)
        # Each focused form already shows its scope, inputs, effects and actions.
        # Keep the shared state projection without duplicating those rows above it.
        self.context_summary.hide()
        self.tabs = QTabWidget()
        # The shared command bar owns mode navigation.  Retain QTabWidget only
        # as the compact content stack so existing page state stays intact.
        self.tabs.tabBar().hide()
        layout.addWidget(self.tabs, 1)
        policy_page = QWidget()
        form = QFormLayout(policy_page)
        self.lower_enabled = QCheckBox("Flag images below the lower cutoff")
        self.upper_enabled = QCheckBox("Flag images above the upper cutoff")
        self.lower_mp = self._spin(3)
        self.upscale_target_mp = self._spin(4)
        self.upper_mp = self._spin(9)
        self.downscale_target_mp = self._spin(6)
        form.addRow(self.lower_enabled)
        form.addRow("Lower cutoff (MP)", self.lower_mp)
        form.addRow("Upscale target (MP)", self.upscale_target_mp)
        form.addRow(self.upper_enabled)
        form.addRow("Upper cutoff (MP)", self.upper_mp)
        form.addRow("Downscale target (MP)", self.downscale_target_mp)
        note = QLabel("Targets preserve aspect ratio with integer-pixel rounding. Exact dimensions appear in the preview.\nPolicy changes reclassify the view without changing Image Knowledge or source files.")
        note.setWordWrap(True)
        form.addRow(note)
        self.save_button = QPushButton("Save Resolution Settings")
        self.save_button.clicked.connect(self.save_policy)
        form.addRow(self.save_button)
        self.tabs.addTab(policy_page, "Preferred resolution")

        inspect_page = QWidget()
        inspect_layout = QVBoxLayout(inspect_page)
        inspect_title = QLabel("Inspect dimensions")
        inspect_title.setObjectName("sectionTitle")
        inspect_layout.addWidget(inspect_title)
        inspect_help = QLabel(
            "Current pixel dimensions and your preferred range. "
            "Images stay unchanged and blurred images stay blurred."
        )
        inspect_help.setWordWrap(True)
        inspect_layout.addWidget(inspect_help)
        self.inspection_text = QTextEdit()
        self.inspection_text.setReadOnly(True)
        self.inspection_text.setAccessibleName("Inspected image dimensions")
        self.inspection_text.setPlaceholderText(
            "Inspect Dimensions lists the current dimensions and derived resolution state."
        )
        inspect_layout.addWidget(self.inspection_text, 1)
        self.inspect_button = QPushButton("Inspect Dimensions")
        self.inspect_button.clicked.connect(self.inspect)
        inspect_layout.addWidget(self.inspect_button, 0, Qt.AlignLeft)
        self.continue_resize_button = QPushButton("Continue to Resize…")
        self.continue_resize_button.setAccessibleDescription(
            "Open Resize Images to choose options and preview changes. This does not create image versions."
        )
        self.continue_resize_button.clicked.connect(self._continue_to_resize)
        self.tabs.addTab(inspect_page, "Inspect dimensions")

        resize_page = QWidget()
        resize_layout = QVBoxLayout(resize_page)
        resize_title = QLabel("Resize images")
        resize_title.setObjectName("sectionTitle")
        resize_layout.addWidget(resize_title)
        resize_help = QLabel(
            "Create new image versions for this input scope. Preview the exact "
            "destination and effects before creating them; originals stay unchanged."
        )
        resize_help.setWordWrap(True)
        resize_layout.addWidget(resize_help)
        choose_title = QLabel("1. Choose size and method")
        choose_title.setObjectName("sectionTitle")
        resize_layout.addWidget(choose_title)
        options = QFormLayout()
        self.method = QComboBox(self)
        self.method.addItem("Lanczos · local CPU resampling", "lanczos")
        self.method.addItem("AI upscale · local ESRGAN 4x Safetensors", "esrgan")
        self.method.addItem("Advanced generative resizing", "generative")
        self.method.hide()
        self.resize_methods = QButtonGroup(self)
        self.resize_method_buttons = {}
        for index, (key, label, detail) in enumerate((
            ("lanczos", "Simple Resize · Lanczos (CPU)",
             "Resize existing pixels locally on the CPU. No model needed; does not reconstruct missing detail."),
            ("esrgan", "AI Upscale · ESRGAN Model",
             "Enlarge with a selected local ESRGAN model. May invent fine detail; requires a compatible model and runtime."),
            ("generative", "Generative upscale · redraw at a larger size",
             "Continue in Generate with this image. It may redraw faces, text and other content; model and runtime readiness are checked there before any job."),
        )):
            button = QRadioButton(label)
            button.setAccessibleDescription(detail)
            self.resize_methods.addButton(button, index)
            self.resize_method_buttons[key] = button
            resize_layout.addWidget(button)
            description = QLabel(detail)
            description.setWordWrap(True)
            description.setContentsMargins(24, 0, 0, 6)
            resize_layout.addWidget(description)
        self.resize_methods.idClicked.connect(self.method.setCurrentIndex)
        self.resize_method_buttons["lanczos"].setChecked(True)
        self.model_path = QLineEdit()
        self.model_path.setPlaceholderText("Local ESRGAN 4x .safetensors file")
        self.model_control = QWidget()
        model_layout = QVBoxLayout(self.model_control)
        model_layout.setContentsMargins(0, 0, 0, 0)
        model_row = QHBoxLayout()
        model_layout.addLayout(model_row)
        model_row.setContentsMargins(0, 0, 0, 0)
        self.model_identity = QLabel("No upscaler selected")
        self.model_identity.setAccessibleName("Selected AI upscaler model")
        self.model_identity.setWordWrap(True)
        model_row.addWidget(self.model_identity, 1)
        self.model_browse = QPushButton("Choose Model…")
        self.model_browse.clicked.connect(self._choose_model)
        model_row.addWidget(self.model_browse)
        model_actions = QHBoxLayout()
        self.model_manage = QPushButton("Get Upscaler Models…")
        self.model_manage.clicked.connect(self._manage_upscalers)
        model_actions.addWidget(self.model_manage)
        self.model_check = QPushButton("Check Model && Runtime")
        self.model_check.clicked.connect(self._check_model)
        model_actions.addWidget(self.model_check)
        model_actions.addStretch(1)
        model_layout.addLayout(model_actions)
        self.model_status = QLabel()
        self.model_status.setWordWrap(True)
        model_layout.addWidget(self.model_status)
        self.model_file_details = QWidget()
        file_layout = QVBoxLayout(self.model_file_details)
        file_layout.setContentsMargins(0, 0, 0, 0)
        file_row = QHBoxLayout()
        file_row.addWidget(self.model_path, 1)
        self.model_file = QPushButton("Choose File…")
        self.model_file.clicked.connect(self._choose_model_file)
        file_row.addWidget(self.model_file)
        file_layout.addLayout(file_row)
        model_help = QLabel(
            "Use an existing local ESRGAN 4x Safetensors file (.safetensors). "
            ".pth files are not supported. Preview checks the exact model and runtime."
        )
        model_help.setWordWrap(True)
        file_layout.addWidget(model_help)
        self.model_file_toggle = DisclosureButton(
            "Model File and Details…", "Hide Model File Details", self.model_file_details,
        )
        model_actions.addWidget(self.model_file_toggle)
        model_layout.addWidget(self.model_file_details)
        self.model_label = QLabel("AI model")
        options.addRow(self.model_label, self.model_control)
        self.use_target = QCheckBox("Use a manual target for this selected batch")
        self.resize_options_form = options
        self.manual_target = self._spin(4)
        options.addRow(self.use_target)
        self.manual_target_control = QWidget()
        manual_target_layout = QHBoxLayout(self.manual_target_control)
        manual_target_layout.setContentsMargins(0, 0, 0, 0)
        manual_target_layout.addWidget(self.manual_target)
        self.manual_target_label = QLabel("Manual target (MP)")
        options.addRow(self.manual_target_label, self.manual_target_control)
        self.allow_derivative = QCheckBox("Allow a new reviewed version of an existing derivative")
        self.force = QCheckBox("Force a new version even when an identical completed result exists")
        self.output_root = QLineEdit(str(USER_DATA_DIR / "resize_versions"))
        root_row = QHBoxLayout()
        self.output_root_row = root_row
        root_row.addWidget(self.output_root, 1)
        self.output_root.setReadOnly(True)
        self.manual_folders_button = QPushButton("Setup Folders…")
        self.manual_folders_button.clicked.connect(self._choose_output)
        root_row.addWidget(self.manual_folders_button)
        options.addRow("Protected version folder", root_row)
        resize_layout.addLayout(options)
        self.resize_advanced = QWidget()
        advanced_layout = QVBoxLayout(self.resize_advanced)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.addWidget(self.allow_derivative)
        advanced_layout.addWidget(self.force)
        self.resize_advanced_toggle = DisclosureButton(
            "Show Advanced Resize Options…", "Hide Advanced Resize Options",
            self.resize_advanced,
        )
        resize_layout.addWidget(self.resize_advanced_toggle, 0, Qt.AlignLeft)
        resize_layout.addWidget(self.resize_advanced)
        self.preview_title = QLabel("2. Preview changes")
        self.preview_title.setObjectName("sectionTitle")
        resize_layout.addWidget(self.preview_title)
        self.preview_button = QPushButton("Preview Resize")
        self.preview_button.clicked.connect(self.preview)
        resize_layout.addWidget(self.preview_button, 0, Qt.AlignLeft)
        self.preview_text = QTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setAccessibleName("Exact resolution and resize preview")
        self.preview_text.setPlaceholderText("Choose your options, then Preview Resize to review the exact changes.")
        self.resize_preview_host = QWidget()
        self.resize_preview_layout = QVBoxLayout(self.resize_preview_host)
        self.resize_preview_layout.setContentsMargins(0, 0, 0, 0)
        resize_layout.addWidget(self.resize_preview_host, 1)
        self.resize_preview_layout.addWidget(self.preview_text)
        self.generative_guidance = QLabel(
            "Generate opens a separate source-bound draft; it does not resize or replace this image here. "
            "Review the larger target, change strength, exact model and readiness there. "
            "The original remains protected and any generated result returns through Ingest unreviewed."
        )
        self.generative_guidance.setWordWrap(True)
        resize_layout.addWidget(self.generative_guidance)
        self.create_title = QLabel("3. Create new image versions")
        self.create_title.setObjectName("sectionTitle")
        actions = QHBoxLayout()
        actions.addStretch(1)
        self.execute_button = QPushButton("Create Resized Versions")
        self.execute_button.setProperty("primary", True)
        actions.addWidget(self.execute_button)
        self.execute_button.clicked.connect(self.execute)
        self.continue_generate_button = QPushButton("Continue in Generate…")
        self.continue_generate_button.setProperty("primary", True)
        self.continue_generate_button.clicked.connect(self._continue_to_generate)
        actions.addWidget(self.continue_generate_button)
        self.resize_scroll = QScrollArea()
        self.resize_scroll.setWidgetResizable(True)
        self.resize_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.resize_scroll.setWidget(resize_page)
        resize_container = QWidget()
        resize_container_layout = QVBoxLayout(resize_container)
        resize_container_layout.addWidget(self.resize_scroll, 1)
        resize_container_layout.addWidget(self.create_title)
        resize_container_layout.addLayout(actions)
        self.tabs.addTab(resize_container, "Manual resize")

        automation_page = QWidget()
        auto_form = QFormLayout(automation_page)
        auto_note = QLabel("Automatic resizing uses the same verified derivative path after dimension inspection. Each direction needs an explicit destination. AI upscaling also requires a verified manual result with the exact model. Existing derivatives are skipped; originals are never replaced. Results remain available for normal Ingest review.")
        auto_note.setWordWrap(True)
        auto_form.addRow(auto_note)
        self.automatic_upscale = QCheckBox("Automatically upscale below-preferred originals")
        self.automatic_downscale = QCheckBox("Automatically downscale above-preferred originals")
        self.upscale_output_root = QLineEdit()
        self.downscale_output_root = QLineEdit()
        self.upscale_output_root.setReadOnly(True)
        self.downscale_output_root.setReadOnly(True)
        for direction in ("upscale", "downscale"):
            auto_form.addRow(getattr(self, "automatic_" + direction))
            auto_form.addRow(f"{direction.title()} version folder", getattr(self, direction + "_output_root"))
        self.automatic_folders_button = QPushButton("Setup Folders…")
        self.automatic_folders_button.clicked.connect(self._choose_output)
        auto_form.addRow(self.automatic_folders_button)
        self.automatic_method = QComboBox()
        self.automatic_method.addItem("Lanczos CPU", "lanczos")
        self.automatic_method.addItem("AI upscale using the selected local model", "esrgan")
        auto_form.addRow("Automatic upscale method", self.automatic_method)
        if self.purpose == "settings":
            self.automatic_model_label = QLabel("AI model")
            auto_form.addRow(self.automatic_model_label, self.model_control)
        self.auto_save = QPushButton("Save Resolution Settings")
        self.auto_save.clicked.connect(self.save_policy)
        auto_form.addRow(self.auto_save)
        self.tabs.addTab(automation_page, "Automatic resizing")

        saved_versions_page = QWidget()
        self.saved_versions_layout = QVBoxLayout(saved_versions_page)
        saved_intro = QLabel(
            "Load saved versions to inspect their exact recovery state. Results return "
            "to Ingest as unreviewed images; undo and restore apply only to the versions shown here."
        )
        saved_intro.setWordWrap(True)
        saved_intro.setObjectName("mutedLabel")
        self.saved_versions_layout.addWidget(saved_intro)
        self.saved_versions_summary = QLabel()
        self.saved_versions_summary.setWordWrap(True)
        self.saved_versions_summary.setObjectName("sectionTitle")
        self.saved_versions_layout.addWidget(self.saved_versions_summary)
        self.saved_preview_host = QWidget()
        self.saved_preview_layout = QVBoxLayout(self.saved_preview_host)
        self.saved_preview_layout.setContentsMargins(0, 0, 0, 0)
        self.saved_versions_text = QTextEdit()
        self.saved_versions_text.setReadOnly(True)
        self.saved_versions_text.setAccessibleName("Saved resize version results")
        self.saved_versions_text.setPlaceholderText("Load saved versions to inspect their state, or create versions from a reviewed resize preview.")
        self.saved_versions_text.setMaximumHeight(260)
        self.saved_preview_layout.addWidget(self.saved_versions_text)
        self.saved_versions_toggle = DisclosureButton(
            "Show Exact Saved Versions", "Hide Exact Saved Versions", self.saved_preview_host,
        )
        self.saved_versions_toggle.setToolTip("Inspect exact result paths, failures and saved-version identities.")
        self.saved_versions_layout.addWidget(self.saved_versions_toggle, 0, Qt.AlignLeft)
        self.saved_versions_layout.addWidget(self.saved_preview_host)
        self.tabs.addTab(saved_versions_page, "Saved versions")

        self.status = QLabel()
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        footer = QHBoxLayout()
        self.cancel_button = QPushButton("Cancel Work")
        self.cancel_button.clicked.connect(self.cancel)
        self.ingest_button = QPushButton("Add Results Through Ingest…")
        self.ingest_button.clicked.connect(self.ingest)
        self.undo_button = QPushButton("Undo Last Versions")
        self.undo_button.clicked.connect(self.undo)
        self.restore_button = QPushButton("Restore Last Versions")
        self.restore_button.clicked.connect(self.restore)
        self.recover_button = QPushButton("Recover Previewed Versions")
        self.recover_button.setToolTip(
            "Check whether a previously saved version of the current preview can be recovered. "
            "This does not create a new version."
        )
        self.recover_button.clicked.connect(self.recover)
        self.history_button = QPushButton("Load Saved Versions")
        self.history_button.clicked.connect(self.history)
        footer.addWidget(self.cancel_button)
        footer.addStretch(1)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.close)
        footer.addWidget(self.close_button)
        footer.addWidget(self.continue_resize_button)
        layout.addLayout(footer)
        self.saved_actions = QHBoxLayout()
        for button in (self.undo_button, self.restore_button, self.ingest_button):
            self.saved_actions.addWidget(button)
        self.saved_actions.addStretch(1)
        self.saved_versions_layout.addLayout(self.saved_actions)
        self.saved_versions_layout.addWidget(self.recover_button, 0, Qt.AlignLeft)
        self.saved_versions_layout.addStretch(1)
        self.undo_button.setToolTip("Undo the retained version. An already imported copy uses normal Ingest Undo.")
        self._resolution_groups = {
            "inspection": ("Inspect Dimensions",),
            "resize": ("Resize Images", "Saved Versions"),
            "settings": ("Policy", "Automatic Resizing"),
        }[self.purpose]
        history_group = "Automatic Resizing" if self.purpose == "settings" else "Saved Versions"
        commands = (
                bind_source(
                    self, "resolution.manual_folders", self.manual_folders_button,
                    "Resize Images", move=True,
                    keywords=("versions", "destination", "output folder"),
                    description="Choose the protected folder for reviewed manual resize versions.",
                ),
                bind_source(
                    self, "resolution.automatic_folders", self.automatic_folders_button,
                    "Automatic Resizing", move=True,
                    keywords=("versions", "destination", "output folder"),
                    description="Choose the protected folders required before automatic resizing can be enabled.",
                ),
                bind_source(
                    self, "resolution.history", self.history_button,
                    history_group, move=True,
                    keywords=("history", "recovery", "results"),
                    description="Load version history from the configured protected folders.",
                ),
            )
        self.command_controller = DialogCommandController(
            self, titles[self.purpose],
            [command for command in commands if command.group in self._resolution_groups],
            groups=self._resolution_groups,
            on_group_changed=self._show_command_group,
            initial_group=self._resolution_groups[0],
        )
        self.command_controller.bar.setVisible(self.purpose != "inspection")
        self._settings_error = False
        try:
            self.settings_value = load_resolution_settings(settings_file)
        except (OSError, ValueError) as exc:
            self.settings_value = {"policy": asdict(ResolutionPolicy())}
            self._settings_error = True
            self.status.setText(str(exc))
        for key, value in self.settings_value["policy"].items():
            control = getattr(self, key)
            control.setChecked(value) if isinstance(control, QCheckBox) else control.setValue(value)
        for key in ("automatic_upscale", "automatic_downscale"):
            getattr(self, key).setChecked(self.settings_value.get(key, False))
        for key in ("upscale_output_root", "downscale_output_root", "output_root"):
            if self.settings_value.get(key):
                getattr(self, key).setText(self.settings_value[key])
        self.model_path.setText(self.settings_value.get("ai_model_path", ""))
        self.automatic_method.setCurrentIndex(max(0, self.automatic_method.findData(self.settings_value.get("automatic_upscale_method", "lanczos"))))
        for control in (self.lower_enabled, self.upper_enabled, self.use_target, self.force, self.allow_derivative):
            control.toggled.connect(self.invalidate)
        for control in (self.lower_mp, self.upper_mp, self.upscale_target_mp, self.downscale_target_mp, self.manual_target):
            control.valueChanged.connect(self.invalidate)
        self.output_root.textChanged.connect(self.invalidate)
        self.model_path.textChanged.connect(self.invalidate)
        self.model_path.textChanged.connect(self._reset_model_status)
        self._reset_model_status()
        self.method.currentIndexChanged.connect(self.invalidate)
        self.method.currentIndexChanged.connect(self._sync_resize_option_visibility)
        self.automatic_upscale.toggled.connect(self._update_context_summary)
        self.automatic_downscale.toggled.connect(self._update_context_summary)
        self.automatic_method.currentIndexChanged.connect(self._update_context_summary)
        self.automatic_method.currentIndexChanged.connect(self._sync_resize_option_visibility)
        self.upscale_output_root.textChanged.connect(self._update_context_summary)
        self.downscale_output_root.textChanged.connect(self._update_context_summary)
        self.use_target.toggled.connect(self._sync_resize_option_visibility)
        self._sync_resize_option_visibility()
        self._update_actions()

    @staticmethod
    def _spin(value):
        control = QDoubleSpinBox()
        control.setRange(0.000001, 40)
        control.setDecimals(6)
        control.setValue(value)
        return control

    def _show_command_group(self, group):
        """Keep mode navigation in the ribbon while reuse keeps one content stack."""
        indexes = {
            "Policy": 0,
            "Inspect Dimensions": 1,
            "Resize Images": 2,
            "Automatic Resizing": 3,
            "Saved Versions": 4,
        }
        self.tabs.setCurrentIndex(indexes.get(group, 2))
        if group == "Saved Versions":
            self.saved_versions_toggle.setFocus()
        self._update_context_summary()

    def _continue_to_resize(self):
        """Reveal resize setup without previewing or creating any image versions."""
        if self._task is not None or not self.paths:
            return
        child = self._open_resize_dialog()
        if child is not None:
            child.command_controller.select_group("Resize Images")
            child.resize_method_buttons[child.method.currentData()].setFocus(Qt.OtherFocusReason)

    def _open_resize_dialog(self):
        """Retain a dedicated resize draft and forward its normal Ingest handoff."""
        if self._task is not None:
            return None
        child = self._resize_dialog
        if child is not None and child._task is not None:
            self.status.setText("Wait for the previous resize operation to finish cancelling.")
            return None
        if child is None:
            child = ResolutionDialog(self.jobs, self.paths, parent=self, purpose="resize",
                settings_file=self.settings_file, guard=self.guard,
                inspection_guard=self.inspection_guard, model_registry=self.model_registry,
                protected_model_roots=self.protected_model_roots)
            initial_method = getattr(self, "_initial_resize_method", None)
            if initial_method in {"lanczos", "esrgan"}:
                child.method.setCurrentIndex(child.method.findData(initial_method))
            child.ingest_requested.connect(self.ingest_requested.emit)
            child.generative_requested.connect(self.generative_requested.emit)
            child.setWindowModality(Qt.WindowModal)
            if self.purpose == "inspection":
                child.close_button.setText("Back to Dimensions")
            self._resize_dialog = child
        child._closed = False
        child.show()
        child.raise_()
        return child

    def _sync_resize_option_visibility(self, *_):
        generative = self.method.currentData() == "generative" and self.purpose == "resize"
        self.resize_options_form.setRowVisible(self.use_target, not generative)
        self.resize_options_form.setRowVisible(self.output_root_row, not generative)
        self.resize_advanced_toggle.setVisible(not generative)
        self.resize_advanced.setVisible(not generative and self.resize_advanced_toggle.isChecked())
        self.manual_folders_button.setVisible(not generative)
        selected = self.resize_method_buttons.get(self.method.currentData())
        if selected is not None:
            selected.setChecked(True)
        self.preview_title.setVisible(not generative)
        self.preview_button.setVisible(not generative)
        self.resize_preview_host.setVisible(not generative)
        self.generative_guidance.setVisible(generative)
        self.create_title.setText("2. Continue with a separate draft" if generative else
                                  "3. Create new image versions")
        self.execute_button.setVisible(not generative)
        self.continue_generate_button.setVisible(generative)
        method = self.automatic_method if self.purpose == "settings" else self.method
        show_model=method.currentData()=="esrgan"
        if self.purpose == "settings":
            self.automatic_model_label.setVisible(show_model)
        self.model_label.setVisible(show_model)
        self.model_control.setVisible(show_model)
        show_target=self.use_target.isChecked() and not generative
        self.manual_target_label.setVisible(show_target)
        self.manual_target_control.setVisible(show_target)

    def _continue_to_generate(self):
        if (self.purpose != "resize" or self.method.currentData() != "generative"
                or self._task is not None or self._generation_handoff_pending):
            return
        if len(self.paths) != 1:
            self.status.setText("Choose one image in Ingest or Dataset for generative upscale; this batch is unchanged.")
            return
        self._generation_handoff_pending = True
        self._update_actions()
        self.status.setText("Preparing a separate source-bound Generate draft. No image version or job was created here.")
        self.generative_requested.emit(self.paths[0])

    def _update_result_action_labels(self):
        verified = sum(result.get("state") == "verified" for result in self.results)
        undone = sum(result.get("state") == "undone" for result in self.results)
        self.ingest_button.setText(
            f"Add {verified:,} Verified Result{'s' if verified != 1 else ''} Through Ingest…"
            if verified else "Add Results Through Ingest…"
        )
        self.undo_button.setText(
            f"Undo {verified:,} Verified Version{'s' if verified != 1 else ''}"
            if verified else "Undo Last Versions"
        )
        self.restore_button.setText(
            f"Restore {undone:,} Undone Version{'s' if undone != 1 else ''}"
            if undone else "Restore Last Versions"
        )
        self.recover_button.setText(
            f"Recover {len(self.plans):,} Previewed Version{'s' if len(self.plans) != 1 else ''}"
            if self.plans else "Recover Previewed Versions"
        )
        if self.results:
            self.saved_versions_summary.setText(
                f"This view contains {verified:,} verified and {undone:,} undone saved version"
                f"{'s' if verified + undone != 1 else ''}."
            )
        elif self.plans:
            self.saved_versions_summary.setText(
                f"No saved result is loaded. Resize Images contains {len(self.plans):,} previewed version"
                f"{'s' if len(self.plans) != 1 else ''} and its recovery action."
            )
        else:
            self.saved_versions_summary.setText(
                "No saved versions are loaded. Use Load Saved Versions to inspect configured protected folders."
            )

    def _update_context_summary(self):
        """Project existing dialog state without changing its workflow authority."""
        controller = getattr(self, "command_controller", None)
        group = getattr(getattr(controller, "bar", None), "_group", "Inspect Dimensions")
        self.continue_resize_button.setVisible(group == "Inspect Dimensions")
        busy = self._task is not None
        if self.paths:
            using = f"{len(self.paths):,} selected image{'s' if len(self.paths) != 1 else ''}"
        else:
            using = "No selected images; policy and saved versions remain available"
        if group == "Inspect Dimensions":
            model = "Not applicable — inspection reads dimensions only"
        elif group == "Policy":
            model = "Not applicable — policy changes do not select a resize method"
        elif group == "Automatic Resizing":
            automatic_model = self.automatic_method.currentData()
            model = ("Automatic Lanczos · local CPU resampling · no model required"
                     if automatic_model == "lanczos" else
                     "Automatic AI upscale · local ESRGAN · requires the verified manual model")
        elif self.method.currentData() == "esrgan":
            model_path = self.model_path.text().strip()
            model = (f"AI upscale · local ESRGAN · {Path(model_path).name}"
                     if model_path else "AI upscale · local ESRGAN · choose a local Safetensors model")
        elif self.method.currentData() == "generative" and group == "Resize Images":
            model = "Generative upscale · model and runtime choice are reviewed in Generate"
        else:
            model = "Lanczos · local CPU resampling · no model required"
        destination = self.output_root.text().strip()
        destination_text = destination or "Choose a protected derivative-version folder"
        verified = sum(row.get("state") == "verified" for row in self.results)
        if verified:
            result = (f"{verified:,} verified version{'s' if verified != 1 else ''}; exact paths are in Saved Versions; "
                      "normal Ingest receives them unreviewed and originals stay unchanged")
        elif self.plans:
            result = (f"{len(self.plans):,} previewed new derivative{'s' if len(self.plans) != 1 else ''} at "
                      f"{destination_text}; Ingest review stays unreviewed; originals stay unchanged")
        elif group == "Inspect Dimensions":
            result = "Inspection is read-only; no version will be created"
        elif group == "Resize Images":
            result = f"New derivatives at {destination_text}; unreviewed in Ingest; originals stay unchanged"
        else:
            result = "No new version is planned in this section"
        if busy:
            readiness, next_step = "Working; controls are temporarily locked", "Wait for the current operation or cancel safely"
        elif not self.paths and group in {"Inspect Dimensions", "Resize Images"}:
            readiness, next_step = "Choose images in Ingest or Dataset first", "Return to the gallery and select an image scope"
        elif self.method.currentData() == "esrgan" and not self.model_path.text().strip() and group == "Resize Images":
            readiness, next_step = "AI preview needs an explicitly selected local model", "Choose a local ESRGAN Safetensors model"
        elif group == "Inspect Dimensions":
            if self._inspection_state == "complete":
                readiness, next_step = self._inspection_summary, "Review dimensions or Continue to Resize"
            elif self._inspection_state == "failed":
                readiness, next_step = self._inspection_summary, "Review the inspection message, then Retry Inspection"
            else:
                readiness, next_step = "Ready to inspect the selected scope", "Inspect Dimensions"
        elif group == "Resize Images":
            if self.method.currentData() == "generative":
                readiness = ("Choose one image for a separate generative draft" if len(self.paths) != 1 else
                             "A separate source-bound draft can be reviewed in Generate")
                next_step = "Continue in Generate" if len(self.paths) == 1 else "Return to the gallery and choose one image"
            elif self._settings_error:
                readiness, next_step = "Resolution settings need attention before a preview", "Review the settings error and correct it"
            elif self.plans and self.execute_button.isEnabled():
                readiness, next_step = "Reviewed derivatives are ready; originals remain unchanged", "Create Resized Versions"
            else:
                readiness, next_step = "Ready to review resize options", "Preview Resize" if self.paths else "Choose an image scope"
        elif group == "Automatic Resizing":
            permissions = []
            for direction in ("upscale", "downscale"):
                draft = getattr(self, "automatic_" + direction).isChecked()
                saved = bool(self.settings_value.get("automatic_" + direction))
                permissions.append(
                    f"{direction.title()} draft {'on' if draft else 'off'}, saved {'on' if saved else 'off'}"
                )
            readiness = "; ".join(permissions)
            next_step = "Review each direction and its saved permission"
            if any(getattr(self, "automatic_" + direction).isChecked() != bool(self.settings_value.get("automatic_" + direction))
                   for direction in ("upscale", "downscale")):
                next_step = "Save Automatic Resize Permissions"
        elif group == "Saved Versions":
            readiness, next_step = "Saved versions are separate from the current preview", "Add verified results through Ingest" if verified else "Load Saved Versions"
        else:
            readiness, next_step = "Policy changes are not image edits", "Save Resolution Policy"
        self.context_summary.update_context(using=using, model=model, result=result,
                                            readiness=readiness, next_step=next_step)
        self.context_summary.labels["result"].setToolTip(
            "Exact retained paths are shown in Saved Versions." if verified else destination
        )

    def policy(self):
        return ResolutionPolicy(self.lower_enabled.isChecked(), self.lower_mp.value(), self.upscale_target_mp.value(),
                                self.upper_enabled.isChecked(), self.upper_mp.value(), self.downscale_target_mp.value())

    def invalidate(self, *_):
        self.plans = []
        self._update_actions()

    def _update_actions(self):
        busy = self._task is not None
        supported_method = self.method.currentData() in {"lanczos", "esrgan"}
        self.tabs.setEnabled(not busy)
        self.command_controller.bar.tabs.setEnabled(not busy)
        for control in (
            self.model_browse, self.model_file, self.manual_folders_button,
            self.automatic_folders_button, self.model_manage, self.model_check,
        ):
            control.setEnabled(not busy)
        self.cancel_button.setEnabled(busy)
        self.cancel_button.setVisible(busy)
        self.continue_resize_button.setEnabled(bool(self.paths) and not busy)
        self.execute_button.setEnabled(bool(self.plans) and supported_method and not busy)
        self.preview_button.setEnabled(bool(self.paths) and supported_method and not busy)
        self.preview_button.setToolTip("" if supported_method else "Generative upscale uses a separate Generate draft.")
        self.continue_generate_button.setEnabled(len(self.paths) == 1 and not busy
                                                and not self._generation_handoff_pending)
        self.generative_guidance.setText(
            "Choose one image in Ingest or Dataset for generative upscale; this batch is unchanged."
            if len(self.paths) != 1 else
            "Generate opens a separate source-bound draft; it does not resize or replace this image here. "
            "Review the larger target, change strength, exact model and readiness there. "
            "The original remains protected and any generated result returns through Ingest unreviewed."
        )
        self.inspect_button.setEnabled(bool(self.paths) and not busy)
        self.ingest_button.setEnabled(any(r.get("state") == "verified" for r in self.results) and not busy)
        self.undo_button.setEnabled(self.ingest_button.isEnabled())
        self.restore_button.setEnabled(any(r.get("state") == "undone" for r in self.results) and not busy)
        self.recover_button.setEnabled(bool(self.plans) and not busy)
        self.recover_button.setVisible(bool(self.plans))
        self.history_button.setEnabled(not busy)
        self.model_check.setEnabled(bool(self.model_path.text().strip()) and not busy)
        self.save_button.setEnabled(not busy and not self._settings_error)
        self.auto_save.setEnabled(not busy and not self._settings_error)
        self._update_result_action_labels()
        self._update_context_summary()

    def _choose_output(self):
        if self._task is not None:
            self.status.setText("Wait for resize work to finish before changing folders.")
            return
        from .folder_locations import open_folder_locations
        open_folder_locations(self, focus="output_root", registry=self.model_registry,
                              resolution_path=self.settings_file,
                              extra_protected_roots=self.protected_model_roots)
        try:
            current = load_resolution_settings(self.settings_file)
            for key in ("output_root", "upscale_output_root", "downscale_output_root"):
                previous = self.settings_value.get(key, "")
                value = current.get(key, "")
                getattr(self, key).setText(value or (str(USER_DATA_DIR / "resize_versions") if key == "output_root" else ""))
                if key != "output_root" and previous != value:
                    getattr(self, "automatic_" + key.split("_")[0]).setChecked(False)
            # Keep unsaved policy controls, but refresh the storage revision.
            self.settings_value = current
            self.invalidate()
        except (OSError, ValueError, RuntimeError) as exc:
            self.status.setText(str(exc))

    def _choose_model(self):
        if self._task is not None:
            return
        from core.model_resource_registry import ModelResourceRegistry
        from core.paths import MODEL_RESOURCES_PATH
        from .model_selector_dialog import ModelSelectorDialog
        registry = self.model_registry or ModelResourceRegistry(MODEL_RESOURCES_PATH)
        dialog = ModelSelectorDialog(registry, self.jobs, parent=self, role="upscaler",
            protected_roots=self.protected_model_roots,
            current_choice_label=self.model_path.text().strip())
        if dialog.exec() == QDialog.Accepted:
            self.model_path.setText(str(dialog.selected_path))

    def _choose_model_file(self):
        if self._task is not None:
            return
        path, _ = QFileDialog.getOpenFileName(self, "Choose Local ESRGAN 4x Model", "", "Safetensors (*.safetensors)")
        if path:
            self.model_path.setText(path)

    def _manage_upscalers(self):
        if self._task is not None:
            return
        from core.model_resource_registry import ModelResourceRegistry
        from core.paths import MODEL_RESOURCES_PATH
        from .resolution_models_dialog import UpscalerModelsDialog
        registry = self.model_registry or ModelResourceRegistry(MODEL_RESOURCES_PATH)
        dialog = UpscalerModelsDialog(registry, self.jobs, parent=self,
            protected_roots=self.protected_model_roots)
        if dialog.exec() == QDialog.Accepted and dialog.selected_path:
            self.model_path.setText(dialog.selected_path)
            self._check_model()

    def _reset_model_status(self, *_):
        path = self.model_path.text().strip()
        self.model_identity.setText(Path(path).name if path else "No upscaler selected")
        self.model_identity.setToolTip(path)
        self.model_status.setText("Selected model has not been checked yet. Preview checks the exact model and runtime." if path
                                  else "Choose a registered upscaler, or use Get Upscaler Models to add one. Preview checks the selected model and runtime.")

    def _check_model(self):
        if self._task is not None or not self.model_path.text().strip():
            return
        path = self.model_path.text().strip()
        def work(_job):
            from core.model_workspace import _path
            from core.resolution_ai import inspect_ai_model
            return path, inspect_ai_model(_path(path, self.protected_model_roots))
        self._checking_model = True
        self.model_status.setText("Checking the selected model and local runtime…")
        self._run("Check AI upscaler model and runtime", work, self._show_model_check)

    def _show_model_check(self, result):
        self._checking_model = False
        path, spec = result
        if path != self.model_path.text().strip():
            self._reset_model_status()
            return
        runtime = spec["runtime"]
        message = (f"Compatible ESRGAN 4x model; {spec['device'].upper()} runtime available. Preview checks the exact model again."
                   if runtime["ready"] else f"Model format supported; runtime unavailable. {runtime['error']}")
        self.model_status.setText(message)
        self.status.setText(message)

    def save_policy(self):
        try:
            value = dict(self.settings_value)
            value["policy"] = asdict(self.policy())
            value["output_root"] = self.output_root.text().strip()
            value["automatic_upscale_method"] = self.automatic_method.currentData()
            value["ai_model_path"] = self.model_path.text().strip()
            if self.automatic_upscale.isChecked() and value["automatic_upscale_method"] == "esrgan" and not value["ai_model_path"]:
                raise ValueError("Choose a local AI model and load its verified manual result before enabling AI automation.")
            if self.automatic_upscale.isChecked() and value["automatic_upscale_method"] == "esrgan":
                spec = next((r.get("plan", {}).get("model_spec") for r in self.results
                             if r.get("state") == "verified" and r.get("plan", {}).get("model_spec", {}).get("path") == value["ai_model_path"]), None)
                spec = spec or value.get("automatic_ai_model_spec")
                if not spec or spec.get("path") != value["ai_model_path"]:
                    raise ValueError("Complete one verified manual AI resize with this model before enabling automatic AI upscaling.")
                value["automatic_ai_model_spec"] = spec
            for direction in ("upscale", "downscale"):
                enabled = getattr(self, "automatic_" + direction).isChecked()
                root = getattr(self, direction + "_output_root").text().strip()
                if enabled:
                    if not root:
                        raise ValueError("Choose a protected version folder for each enabled direction.")
                    self.guard((), root)
                value["automatic_" + direction] = enabled
                value[direction + "_output_root"] = root
            save_resolution_settings(value, self.settings_file, expected=self.settings_value)
            self.settings_value = value
            self.policy_saved.emit()
            resolution_events().policy_changed.emit()
            self.status.setText("Resolution policy and explicit automatic permissions saved. Source files and Image Knowledge are unchanged.")
        except (OSError, ValueError, RuntimeError) as exc:
            self.status.setText(str(exc))

    def _run(self, name, work, done, *, resource="io"):
        if self._task:
            return
        self._done = done
        task = CoordinatedCallTask(self.jobs, name, work, resource=resource, key=f"resolution:{id(self)}")
        self._task = task
        self.status.setText(f"{name}: queued; waiting for a background worker…")
        task.signals.complete.connect(self._completed.emit)
        task.signals.failed.connect(self._failed.emit)
        task.signals.cancelled.connect(lambda: self._failed.emit("Cancelled. Completed versions remain recoverable; originals are unchanged."))
        task.signals.progress.connect(lambda current, total, message: self.status.setText(f"{current:,}/{total:,} · {message}"))
        try:
            task.start()
        except Exception as exc:
            self._failure(str(exc))
        self._update_actions()

    def _receive(self, result):
        done = self._done
        self._task = self._done = None
        if self._closed:
            return
        self._update_actions()
        if done:
            done(result)
        self._update_actions()

    def _failure(self, message):
        self._task = self._done = None
        if not self._closed:
            if self._checking_model:
                self._checking_model = False
                self.model_status.setText(f"Model check did not complete: {message}")
            if self._inspection_state == "running":
                self._inspection_state = "failed"
                self._inspection_summary = "Inspection did not complete"
                self.inspection_text.setPlainText(f"Inspection did not complete.\n{message}\n\nRetry Inspection when ready.")
                self.inspect_button.setText("Retry Inspection")
            self.status.setText(message)
            self._update_actions()

    def _records(self, policy, job, *, dimensions_only=False):
        (self.inspection_guard if dimensions_only else self.guard)(self.paths)
        with DimensionCache(CACHE_DIR / "resolution.sqlite3") as cache:
            return ResolutionInspector(policy, cache).inspect(self.paths, cancelled=lambda: not job.checkpoint(), progress=job.update)

    def inspect(self):
        if self._closed or self._task is not None or not self.paths:
            return
        try:
            policy = self.policy()
        except ValueError as exc:
            self._inspection_state = "running"
            self._failure(str(exc)); return
        self._inspection_state = "running"
        self.inspection_text.setPlainText(
            f"Loading dimensions for {len(self.paths):,} images…\n"
            "Checking file freshness and reusing the Resolution filter's known dimensions."
        )
        self._run("Load image dimensions", lambda job: self._records(policy, job, dimensions_only=True), self._show_records)

    def _show_records(self, records):
        known = sum(bool(getattr(record, "cache_hit", False)) for record in records)
        unavailable = sum(record.width is None or record.height is None for record in records)
        self._inspection_state = "complete"
        self._inspection_summary = f"Inspection complete: {len(records):,} images; {unavailable:,} dimensions unavailable"
        self.inspect_button.setText("Refresh Dimensions")
        rows = [f"{r.path.name}: {r.width or '?'} × {r.height or '?'} · {r.megapixels or 0:.3f} MP · {r.state.value}"
                + (f" · {r.error or r.resize_readiness_reason}" if r.error or r.resize_readiness_reason else "") for r in records[:500]]
        self.inspection_text.setPlainText("\n".join(rows) + ("\nShowing the first 500; use the gallery Resolution filter for the complete scope." if len(records) > 500 else ""))
        self.status.setText(f"{self._inspection_summary}. Reused {known:,} unchanged results. Technical quality is not assessed.")
        self.command_controller.select_group("Inspect Dimensions")
        self._update_context_summary()

    def preview(self):
        if self.method.currentData() not in {"lanczos", "esrgan"}:
            self.status.setText("Use Continue in Generate for one image; this Resize preview creates no generative draft.")
            return
        try:
            policy = self.policy()
            root = self.output_root.text().strip()
            if not root:
                raise ValueError("Choose a protected version folder.")
            self.guard(self.paths, root)
            if len(self.paths) > 500:
                raise ValueError("Choose up to 500 images for one reviewed resize batch; resolution filtering supports the full library.")
        except (ValueError, RuntimeError, OSError) as exc:
            self.status.setText(str(exc)); return
        manual = self.manual_target.value() if self.use_target.isChecked() else None
        force, allow = self.force.isChecked(), self.allow_derivative.isChecked()
        model_path = self.model_path.text().strip() if self.method.currentData() == "esrgan" else ""
        if self.method.currentData() == "esrgan" and not model_path:
            self.status.setText("Choose a local ESRGAN 4x Safetensors model."); return
        def work(job):
            plans, skipped = [], []
            model_spec = None
            if model_path:
                from core.resolution_ai import inspect_ai_model
                model_spec = inspect_ai_model(model_path)
            for record in self._records(policy, job):
                if not job.checkpoint(): break
                try:
                    target = calculate_target_dimensions(record.width, record.height, manual) if manual else record.target
                    if target is None:
                        skipped.append(f"{record.path.name}: no resize needed or dimensions unavailable")
                        continue
                    plans.append(preview_resize(record.path, (target.width, target.height), asdict(policy), force=force, allow_derivative=allow, model_spec=model_spec))
                except (ValueError, OSError, RuntimeError) as exc:
                    skipped.append(f"{record.path.name}: {exc}")
            return plans, skipped
        self._run("Preview selected resizing", work, self._show_plans)

    def _show_plans(self, result):
        self.plans, skipped = result
        store = DerivativeStore(self.output_root.text())
        text = []
        for plan in self.plans:
            text.append(f"{Path(plan['source']).name}: {plan['source_size'][0]} × {plan['source_size'][1]} → "
                        f"{plan['target_size'][0]} × {plan['target_size'][1]} · {plan['scale']:.4f}×\n"
                        f"{plan['recipe']['adapter']} / revision {plan['recipe']['revision']} · PNG\n"
                        f"Destination: {store.destination(plan)}\n{plan['effects']}\n"
                        f"Storage estimate: {plan['estimated_bytes']/1024**2:.1f} MiB. {plan['gpu']}. {plan['time']}\n")
        text.extend(skipped)
        self.preview_text.setPlainText("\n".join(text))
        self.status.setText(f"{len(self.plans)} ready; {len(skipped)} skipped. Identical verified versions can be reused. Collisions refuse overwrite. Cancel stops before publishing the next version; recovery verifies retained bytes.")
        self.command_controller.select_group("Resize Images")

    def execute(self):
        if self.method.currentData() not in {"lanczos", "esrgan"}:
            self.status.setText("Use Continue in Generate for one image; no generative job starts from Resize.")
            return
        if not self.plans:
            return
        plans, root = tuple(self.plans), self.output_root.text()
        uses_ai = any("model_spec" in p for p in plans)
        def work(job):
            if uses_ai:
                self.jobs.prepare_ai_exclusive_work()
            return execute_resize_batch(plans, root, cancelled=lambda: not job.checkpoint(),
                progress=job.update, guard=lambda: self.guard(self.paths, root))
        self._run("Create resized versions", work, self._show_results, resource="gpu" if uses_ai else "io")

    def _show_results(self, results):
        self.results = results
        if self.purpose != "resize":
            child = self._open_resize_dialog()
            if child is not None:
                child._show_results(results)
                child._update_actions()
            return
        verified = sum(r.get("state") == "verified" for r in results)
        self.status.setText(f"{verified} verified versions; {len(results)-verified} cancelled, unavailable or failed. Originals preserved. Add verified results through normal Ingest.")
        self.saved_versions_text.setPlainText("\n\n".join(f"{r.get('state')}: {r.get('output', r.get('error', r.get('message', r.get('fingerprint', ''))))}" for r in results))
        self.command_controller.select_group("Saved Versions")

    def recover(self):
        store = DerivativeStore(self.output_root.text())
        keys = tuple(p["fingerprint"] for p in self.plans)
        self._run("Recover resize versions", lambda _job: [store.recover(k) for k in keys], self._show_results)

    def undo(self):
        rows = tuple(r for r in self.results if r.get("state") == "verified")
        self._run("Undo resize versions", lambda _job: [DerivativeStore(r["store_root"]).undo(r["fingerprint"]) for r in rows], self._show_results)

    def restore(self):
        rows = tuple(r for r in self.results if r.get("state") == "undone")
        self._run("Restore resize versions", lambda _job: [DerivativeStore(r["store_root"]).restore(r["fingerprint"]) for r in rows], self._show_results)

    def history(self):
        roots = {self.output_root.text().strip(), self.upscale_output_root.text().strip(), self.downscale_output_root.text().strip()} - {""}
        self._run("Load saved resize versions", lambda _job: [r for root in sorted(roots) for r in DerivativeStore(root).history()], self._show_results)

    def ingest(self):
        try:
            versions = [DerivativeIngestHandoff.from_verified_result(row)
                        for row in self.results if row.get("state") == "verified"]
            if not versions:
                raise ValueError("Choose a verified saved version before opening Ingest.")
            for version in versions:
                version.check_review_status()
        except (ValueError, OSError, RuntimeError, KeyError, TypeError) as exc:
            self.status.setText(str(exc) + " Saved Versions remains available for review.")
            return
        self.ingest_requested.emit(versions)

    def cancel(self):
        if self._task and self._task.job_id:
            self.jobs.cancel(self._task.job_id)

    def _prepare_close(self):
        if self._closed:
            return
        self._closed = True
        if self._resize_dialog is not None:
            self._resize_dialog.close()
        if self._task:
            self._task.cancel()

    def closeEvent(self, event):
        self._prepare_close()
        super().closeEvent(event)

    def reject(self):
        # Escape follows the same cancellation and child-lifetime path as Close.
        self._prepare_close()
        super().reject()


def open_resolution_workspace(parent, *, paths=(), purpose=None, initial_method=None):
    if initial_method not in {None, "lanczos", "esrgan"}:
        raise ValueError("Choose a supported initial resize method.")
    host = _host(parent)
    from core.model_selection import protected_model_roots
    dialog = ResolutionDialog(host.state.jobs, paths, parent=parent, purpose=purpose,
        guard=lambda paths, root=None: _guard(host, paths, root),
        inspection_guard=lambda paths: _guard(host, paths, dimensions_only=True),
        protected_model_roots=protected_model_roots(host.state.settings))
    dialog._initial_resize_method = initial_method
    def ingest(versions):
        window = host
        while window.parent() is not None and not hasattr(window, "navigation"):
            window = window.parent()
        if not hasattr(window, "navigation"):
            dialog.status.setText("Ingest is unavailable. The verified resize versions remain in Saved Versions.")
            return
        window.navigation.setCurrentRow(1)
        if not hasattr(window, "ingest_page"):
            dialog.status.setText("Ingest could not open. The verified resize versions remain in Saved Versions.")
            return
        dialog.close()
        QTimer.singleShot(0, lambda: window.ingest_page._start_ingest_import_preview(
            "files", [version.output for version in versions], preserve_source=True,
            verified_versions=tuple(versions)))
    dialog.ingest_requested.connect(ingest)
    def generative(source):
        from shiboken6 import isValid
        from .generation_workflow import open_generation_workspace
        active = dialog._resize_dialog or dialog
        window = host
        while window.parent() is not None and not hasattr(window, "navigation"):
            window = window.parent()
        origin = window.navigation.currentRow() if hasattr(window, "navigation") else None
        def unavailable(message):
            if isValid(active) and not active._closed:
                active._generation_handoff_pending = False
                active._update_actions()
                active.status.setText(message + " Your Resize choices are unchanged; no version was created here.")
        def opened(generation):
            if not isValid(active) or active._closed:
                return
            active._generation_handoff_pending = False
            active._update_actions()
            active.hide()
            dialog.hide()
            def return_to_resize():
                if not isValid(active) or active._closed:
                    return
                if origin is not None and hasattr(window, "navigation"):
                    window.navigation.setCurrentRow(origin)
                active.status.setText("Returned from Generate. Your Resize choices are unchanged; no version was created here.")
                if active is not dialog:
                    dialog.show()
                active.show(); active.raise_(); active.activateWindow()
            generation.workspace_closed.connect(return_to_resize)
        open_generation_workspace(dialog, paths=(Path(source),), remix=True,
                                  initial_task="upscale", on_opened=opened)
        task = getattr(window, "_generation_source_task", None)
        if task is not None:
            task.signals.failed.connect(lambda _error: unavailable("Generate could not prepare this source."))
            task.signals.cancelled.connect(lambda: unavailable("Generate source preparation was cancelled."))
        elif active._generation_handoff_pending:
            unavailable("Generate did not open; finish or close an existing generation workspace, then retry.")
    dialog.generative_requested.connect(generative)
    host._resolution_dialog = dialog
    dialog.show()
    if dialog.paths and dialog.purpose == "inspection":
        dialog.inspect()
    return dialog


def schedule_automatic_resizing(parent, records):
    """Called only after a scoped gallery inspection; independent opt-in directions."""
    host = _host(parent)
    if getattr(host, "_shutdown_requested", False):
        return
    try:
        value = load_resolution_settings()
        policy = ResolutionPolicy(**value["policy"])
    except (OSError, ValueError):
        return
    if not any(value.get("automatic_" + d) for d in ("upscale", "downscale")):
        return
    if getattr(host, "_automatic_resize_task", None):
        return
    selected = tuple(records)
    def current_permission(direction, root, path):
        if getattr(host, "_shutdown_requested", False):
            raise ValueError("Application is closing; automatic output withheld.")
        current = load_resolution_settings()
        if (current != value or not current.get("automatic_" + direction)
                or current.get(direction + "_output_root") != root):
            raise ValueError("Automatic resizing permission, destination or policy changed; pending output withheld.")
        _guard(host, (path,), root)
    def work(job):
        results = []
        uses_ai = value.get("automatic_upscale_method") == "esrgan"
        if uses_ai:
            host.state.jobs.prepare_ai_exclusive_work()
        for record in selected:
            if not job.checkpoint(): break
            target = policy.target_for(record.width, record.height)
            direction = "upscale" if policy.classify(record.width, record.height).value == "Below Preferred" else "downscale"
            if not target or not value.get("automatic_" + direction): continue
            root = value[direction + "_output_root"]
            try:
                current_permission(direction, root, record.path)
                model_spec = None
                if uses_ai and direction == "upscale":
                    from core.resolution_ai import inspect_ai_model
                    model_spec = inspect_ai_model(value.get("ai_model_path", ""))
                    if model_spec != value.get("automatic_ai_model_spec"):
                        raise ValueError("The automatic AI model or runtime differs from the verified manual selection. Review it again.")
                plan = preview_resize(record.path, (target.width, target.height), value["policy"], model_spec=model_spec)
                result = DerivativeStore(root).execute(plan, cancelled=lambda: not job.checkpoint(),
                    guard=lambda: current_permission(direction, root, record.path))
                results.append(result)
            except (ValueError, OSError, RuntimeError) as exc:
                results.append({"state": "skipped", "error": str(exc)})
            job.update(len(results), len(selected), "Automatic resize; originals protected")
        return results
    task = CoordinatedCallTask(host.state.jobs, "Automatic resolution resizing", work,
        resource="gpu" if value.get("automatic_upscale_method") == "esrgan" else "io", key="automatic-resolution")
    host._automatic_resize_task = task
    def complete(results):
        host._automatic_resize_task = None
        host._automatic_resize_results = results
        if getattr(host, "_shutdown_requested", False):
            return
        verified = sum(r.get("state") == "verified" for r in results)
        status_widget = getattr(host, "resolution_filter", None)
        if status_widget is not None:
            status_widget.status_changed.emit(
                f"Automatic resize: {verified} verified or reused, {len(results)-verified} unavailable or skipped. "
                "Review Resolution → Load Saved Versions opens recovery and Ingest handoff."
            )
        fresh = [r for r in results if r.get("state") == "verified" and not r.get("reused")]
        if fresh:
            dialog = open_resolution_workspace(parent, purpose="resize")
            dialog.results = fresh
            dialog._show_results(fresh)
            dialog._update_actions()
    task.signals.complete.connect(complete)
    task.signals.failed.connect(lambda error: complete([{"state": "failed", "error": error}]))
    task.signals.cancelled.connect(lambda: complete([{"state": "cancelled", "message": "Completed versions remain available through Load Saved Versions."}]))
    task.start()
