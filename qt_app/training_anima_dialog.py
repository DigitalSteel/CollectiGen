"""Review an exact Anima Base DiT-only LoRA run through shared job ownership."""
from __future__ import annotations

from pathlib import Path
from threading import Event
from uuid import uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractSpinBox, QCheckBox, QDialogButtonBox, QFileDialog, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QScrollArea,
    QSpinBox, QDoubleSpinBox, QStackedWidget, QTabBar, QToolButton, QVBoxLayout, QWidget)

from core.training_anima import (AnimaSettings, AnimaTrainingService,
    DEFAULT_QUALIFICATION, anima_optimizer_steps)
from .components import bind_status_footer, refresh_status_footer
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask

ISSUE_MESSAGES = {
    "selected_anima_resource_boundary_held": "A selected model path overlaps a protected library, backup or managed-resource folder. Choose a permitted standalone model location in Models before preparing this run.",
    "anima_resource_preparation_changed": "The retained model preparation or storage policy changed. Reopen its exact review, finish recovery, or explicitly discard it before preparing again.",
    "anima_runtime_not_qualified": "Native Anima qualification is missing or changed. Complete and verify the separate generated-data trial before ordinary training.",
    "trainer_runtime_prerequisites_unavailable": "The selected trainer environment does not match the pinned kohya runtime profile. Review exact software locations in Setup.",
    "trainer_source_or_runtime_changed": "The selected trainer source, Python base, or software inventory changed. Recheck Setup and prepare a fresh selection.",
    "selected_anima_resource_unavailable": "Register the exact Anima base, Qwen text encoder and VAE under their roles in Models & Guidance.",
    "embedded_llm_adapter_unverified": "The Anima base lacks the required embedded LLM adapter layout. Select the approved Base v1.0 bytes.",
    "package_unavailable_or_changed": "The selected package is unavailable or changed. Reopen and verify the original package.",
    "package_anima_base_profile_mismatch": "The package target or resolution differs from Anima Base v1.0. Review a new package.",
    "package_training_settings_mismatch": "The package repeats, batch, accumulation, seed or learning rate differs from this run. Review a new package.",
    "package_optimizer_steps_mismatch": "Optimizer steps differ from the immutable package exposure request. Review a new package.",
    "package_training_settings_unavailable": "The package training settings are incomplete. Rebuild the package from a reviewed plan.",
}


def anima_service_for_owner(registry, owner=None):
    """Carry the shell's model boundaries into new and retained training reviews."""
    from .model_companion_workflow import resource_boundaries
    settings = None
    current = owner
    while current is not None:
        settings = getattr(current, "settings", None)
        if settings is None: settings = getattr(getattr(current, "state", None), "settings", None)
        if settings is not None: break
        current = current.parent() if callable(getattr(current, "parent", None)) else None
    protected, managed = resource_boundaries(registry.snapshot()[0], settings)
    return AnimaTrainingService(registry, protected_roots=protected, managed_roots=managed)


def _package_defaults(package):
    if not getattr(package, "valid", False) or type(getattr(package, "manifest", None)) is not dict:
        raise ValueError("Choose a current verified Anima package.")
    manifest = package.manifest
    config, counts = manifest.get("training_configuration"), manifest.get("counts")
    if type(config) is not dict or type(counts) is not dict:
        raise ValueError("The Anima package configuration is unavailable.")
    target = config.get("target", {})
    if (type(target) is not dict or str(target.get("model_family", "")).casefold() != "anima"
            or str(target.get("model_version", "")).casefold() not in {"base v1.0", "base-v1.0", "base-1.0", "1.0"}):
        raise ValueError("This package does not target Anima Base v1.0.")
    image, weighting, optimization = (config.get(name, {}) for name in
        ("image_preparation", "weighting", "optimization_request"))
    if not all(type(row) is dict for row in (image, weighting, optimization)):
        raise ValueError("The Anima package settings are incomplete.")
    training, repeats, epochs = counts.get("training"), weighting.get("repeats"), weighting.get("epochs")
    batch, accumulation = optimization.get("batch_size"), optimization.get("gradient_accumulation")
    if any(type(value) is not int or value < 1 for value in (training, repeats, epochs, batch, accumulation)):
        raise ValueError("The package exposure count is unavailable.")
    steps_per_epoch, steps = anima_optimizer_steps(
        training, repeats, epochs, batch, accumulation)
    if not 1 <= steps <= 2000:
        raise ValueError("The package requests more than 2,000 optimizer steps; revise its settings before Anima preparation.")
    settings = AnimaSettings(max_steps=steps, resolution=image.get("target_resolution"),
        rank=4, seed=optimization.get("seed"),
        learning_rate=float(optimization.get("learning_rate")), timeout_seconds=28800,
        batch_size=batch, gradient_accumulation=accumulation, repeats=repeats)
    settings.validate()
    explanation = (f"Package: {training:,} training images × {repeats:,} repeats "
        f"at batch {batch:,} yield {training * repeats:,} batches per epoch. "
        f"Accumulation {accumulation:,} rounds within each epoch to {steps_per_epoch:,} "
        f"optimizer {'step' if steps_per_epoch == 1 else 'steps'} per epoch "
        f"× {epochs:,} epochs = {steps:,} requested steps. "
        "The adapter preserves those package values, seed, learning rate and resolution. "
        "LoRA rank 4 and an 8-hour deadline are adapter proposals. Change package settings by reviewing a new package.")
    return settings, explanation


class AnimaTrainingDialog(FeedbackEnabledDialog):
    """Normal training only; no qualification-trial bypass or resource discovery."""
    resourceRegistered = Signal(str)
    reviewRetained = Signal(object)
    trainerSetupRequested = Signal()
    feedback_workflow = "Anima local training"

    def __init__(self, package, registry, jobs, *, saved_setup=None, service=None,
                 qualification_path=None, parent=None):
        super().__init__(parent)
        self.package, self.registry, self.jobs = package, registry, jobs
        self.service = service if service is not None else anima_service_for_owner(registry, parent)
        self.saved_setup = saved_setup
        self.qualification_path = Path(qualification_path or DEFAULT_QUALIFICATION)
        self._selection = self._preview = self._prepared = self._outcome = self._verified = None
        self._cache_review = self._resource_preparation = self._resource_receipt = None
        self._restoring_resources = False
        self._task = None
        self._operation = ""
        self._registered = False
        self._run_attempted = False
        self._closing = False
        self._generation = 0
        self._stop_event = Event()
        try:
            defaults, explanation = _package_defaults(package)
            self._package_issue = ""
        except (TypeError, ValueError) as exc:
            defaults, explanation, self._package_issue = AnimaSettings(), str(exc), str(exc)
        self.setWindowTitle("Prepare Anima Base Training")
        self.resize(940, 760)
        outer = QVBoxLayout(self)
        title = QLabel("Train Anima Base LoRA on this computer")
        title.setObjectName("sectionTitle"); outer.addWidget(title)
        intro = QLabel("Check the local setup, review the training settings, then prepare the exact model resources. Starting the GPU run is a separate action.")
        intro.setWordWrap(True); outer.addWidget(intro)
        self.sections = QTabBar()
        for name in ("Local Setup", "Training Settings", "Model Preparation"):
            self.sections.addTab(name)
        self.sections.setAccessibleName("Anima training tasks")
        outer.addWidget(self.sections)
        self.section_pages = QStackedWidget()
        self.sections.currentChanged.connect(self.section_pages.setCurrentIndex)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        self.sections.currentChanged.connect(lambda _index: scroll.verticalScrollBar().setValue(0))
        body = QWidget(); form_layout = QVBoxLayout(body)
        form_layout.addWidget(self.section_pages)
        form_layout.addStretch(1)
        scroll.setWidget(body); outer.addWidget(scroll, 1)
        setup_page = QWidget(); setup_layout = QVBoxLayout(setup_page)
        settings_page = QWidget(); settings_layout = QVBoxLayout(settings_page)
        storage_page = QWidget(); storage_page_layout = QVBoxLayout(storage_page)
        for page in (setup_page, settings_page, storage_page):
            self.section_pages.addWidget(page)
        settings_layout.addWidget(self._label(
            "The reviewed package fixes resolution, steps, batch, repeats, seed and learning rate. "
            "Review a new package to change them. Only the run options below can be adjusted here."))
        self.memory_note = self._label(
            "GPU memory: Unknown for this Anima run. The reviewed settings and device capacity are not "
            "a measured peak or a no-OOM guarantee. If a run fails from memory pressure, retain its "
            "review and choose a new package or supported settings before another launch.")
        self.memory_note.setObjectName("anima_memory_guidance")
        self.memory_note.setToolTip(self.memory_note.text())
        self.memory_note.setAccessibleDescription(self.memory_note.toolTip())
        self.memory_note.setText(
            "GPU memory: Unknown for this run. These settings do not guarantee it will fit.")
        saved = QGroupBox("Local trainer Setup")
        saved_layout = QVBoxLayout(saved)
        self.saved_button = QPushButton("Use Saved Training Setup")
        self.saved_button.clicked.connect(self._load_saved)
        saved_layout.addWidget(self.saved_button)
        self.open_trainer_setup_button = QPushButton("Open Trainer Setup…")
        self.open_trainer_setup_button.clicked.connect(self.trainerSetupRequested)
        self.open_trainer_setup_button.setVisible(callable(saved_setup))
        saved_layout.addWidget(self.open_trainer_setup_button)
        saved_layout.addWidget(self._label("Use a reviewed trainer from Setup, or open Setup to choose one. You can also enter software folders manually below."))
        self.setup_guidance = self._label("")
        self.setup_guidance.setObjectName("anima_setup_guidance")
        self.setup_guidance.hide()
        saved_layout.addWidget(self.setup_guidance)
        setup_layout.addWidget(saved)
        run_locations = QGroupBox("Run destination")
        run_form = QFormLayout(run_locations)
        locations = QGroupBox("Manual trainer software folders")
        location_form = QFormLayout(locations)
        self.fields = {}
        self._choose_buttons = []
        for key, label in (("source_root", "Trainer source"), ("environment_root", "Trainer environment"),
                           ("python_base_root", "Standalone Python base"), ("output_root", "Save runs in")):
            line = QLineEdit(); line.setAccessibleName(label)
            choose = QPushButton("Choose…")
            self._choose_buttons.append(choose)
            choose.clicked.connect(lambda _checked=False, target=key: self._choose(target))
            row = QWidget(); row_layout = QHBoxLayout(row); row_layout.setContentsMargins(0, 0, 0, 0)
            row_layout.addWidget(line, 1); row_layout.addWidget(choose)
            (run_form if key == "output_root" else location_form).addRow(label, row)
            self.fields[key] = line
            line.textChanged.connect(self._changed)
        self.run_name = QLineEdit(); self.run_name.setAccessibleName("Anima run name")
        self.run_name.setPlaceholderText("e.g. anima-portrait-01")
        self.run_name.textChanged.connect(self._changed)
        run_form.addRow("New run name", self.run_name)
        setup_layout.addWidget(run_locations)
        self.manual_locations_button = QToolButton()
        self.manual_locations_button.setCheckable(True)
        self.manual_locations_button.setText("Show Manual Trainer Folders")
        self.manual_locations_button.toggled.connect(locations.setVisible)
        self.manual_locations_button.toggled.connect(lambda expanded:
            self.manual_locations_button.setText(
                "Hide Manual Trainer Folders" if expanded else "Show Manual Trainer Folders"))
        setup_layout.addWidget(self.manual_locations_button, 0, Qt.AlignLeft)
        setup_layout.addWidget(locations)
        locations.hide()
        settings_group = QGroupBox("Adjust for this run")
        settings_form = QFormLayout(settings_group)
        fixed_group = QGroupBox("Fixed package settings")
        fixed_form = QFormLayout(fixed_group)
        self.package_note = QLabel(explanation); self.package_note.setWordWrap(True)
        self.package_note.setTextFormat(Qt.PlainText)
        fixed_form.addRow(self.package_note)
        self.steps = QSpinBox(); self.steps.setRange(1, 2000); self.steps.setValue(defaults.max_steps)
        self.steps.setAccessibleName("Maximum optimizer steps")
        self.resolution = QSpinBox(); self.resolution.setRange(256, 512); self.resolution.setSingleStep(128)
        self.resolution.setValue(defaults.resolution)
        self.rank = QSpinBox(); self.rank.setRange(4, 8); self.rank.setSingleStep(4); self.rank.setValue(defaults.rank)
        # QSpinBox is signed-32-bit; this exact integer range is safe in a double.
        self.seed = QDoubleSpinBox(); self.seed.setDecimals(0)
        self.seed.setRange(0, 4_294_967_295); self.seed.setValue(defaults.seed)
        self.learning_rate = QDoubleSpinBox(); self.learning_rate.setDecimals(7)
        self.learning_rate.setRange(0.0000001, 0.0001); self.learning_rate.setSingleStep(0.000001)
        self.learning_rate.setValue(defaults.learning_rate)
        self.deadline = QSpinBox(); self.deadline.setRange(1, 480); self.deadline.setSuffix(" minutes")
        self.deadline.setValue(defaults.timeout_seconds // 60)
        self.batch_size = QSpinBox(); self.batch_size.setRange(1, 1); self.batch_size.setValue(defaults.batch_size)
        self.accumulation = QSpinBox(); self.accumulation.setRange(1, 8)
        self.accumulation.setValue(defaults.gradient_accumulation)
        self.repeats = QSpinBox(); self.repeats.setRange(1, 16); self.repeats.setValue(defaults.repeats)
        for fixed in (self.steps, self.resolution, self.seed, self.learning_rate,
                      self.batch_size, self.accumulation, self.repeats):
            fixed.setReadOnly(True)
            fixed.setButtonSymbols(QAbstractSpinBox.NoButtons)
        for label, widget in (("Optimizer steps", self.steps), ("Image resolution", self.resolution),
                              ("Batch size", self.batch_size), ("Gradient accumulation", self.accumulation),
                              ("Repeats", self.repeats), ("LoRA rank", self.rank), ("Seed", self.seed),
                              ("Learning rate", self.learning_rate), ("Process deadline", self.deadline)):
            widget.setMaximumWidth(260)
            (settings_form if widget in (self.rank, self.deadline) else fixed_form).addRow(label, widget)
            widget.valueChanged.connect(self._changed)
        settings_layout.addWidget(settings_group)
        settings_layout.addWidget(self.memory_note)
        self.fixed_settings_button = QToolButton()
        self.fixed_settings_button.setCheckable(True)
        self.fixed_settings_button.setText("Show Fixed Package Settings")
        self.fixed_settings_button.toggled.connect(fixed_group.setVisible)
        self.fixed_settings_button.toggled.connect(lambda expanded:
            self.fixed_settings_button.setText(
                "Hide Fixed Package Settings" if expanded else "Show Fixed Package Settings"))
        settings_layout.addWidget(self.fixed_settings_button, 0, Qt.AlignLeft)
        settings_layout.addWidget(fixed_group)
        fixed_group.hide()
        self.storage_group = QGroupBox("Model storage")
        storage_layout = QVBoxLayout(self.storage_group)
        storage_layout.addWidget(self._label("Check Readiness reviews storage without copying. Prepare Exact Run may copy the exact model and required companions under your Fast-storage policy. Archive originals stay in place; protected copies remain available through run and result review."))
        self.storage_cleanup_summary = self._label("")
        self.storage_cleanup_summary.hide(); storage_layout.addWidget(self.storage_cleanup_summary)
        self.storage_details = QPlainTextEdit(); self.storage_details.setReadOnly(True)
        self.storage_details.setAccessibleName("Exact training resources and storage preparation")
        self.storage_details.setMaximumHeight(160)
        self.storage_details.setPlainText("No model storage review yet.")
        storage_layout.addWidget(self.storage_details)
        storage_actions = QHBoxLayout()
        self.open_resources_button = QPushButton("Open Preparation…")
        self.open_resources_button.clicked.connect(self._choose_resources)
        self.retry_resources_button = QPushButton("Retry Model Preparation")
        self.retry_resources_button.clicked.connect(self._retry_resources)
        self.discard_resources_button = QPushButton("Discard Preparation…")
        self.discard_resources_button.clicked.connect(self._discard_resources)
        for button in (self.open_resources_button, self.retry_resources_button, self.discard_resources_button):
            storage_actions.addWidget(button)
        storage_layout.addLayout(storage_actions)
        has_storage_review = callable(getattr(self.service, "cache_review", None))
        self.sections.setTabVisible(2, has_storage_review)
        storage_page_layout.addWidget(self.storage_group)
        for layout in (setup_layout, settings_layout, storage_page_layout):
            layout.addStretch(1)
        self.acknowledge = QCheckBox("I reviewed the exact package settings, adapter proposals, and local GPU run")
        self.acknowledge.toggled.connect(self._actions)
        self.review = self._label("Load Setup and check readiness. Qualification is required before ordinary training.")
        self.review.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        outer.addWidget(self.review)
        outer.addWidget(self.acknowledge)
        self.status = self._label(self._package_issue or "No model or GPU work has started.")
        outer.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Close); self.buttons.rejected.connect(self.reject)
        self.preview_button = self._action("Check Readiness", self._refresh)
        self.open_button = self._action("Open Retained Run…", self._choose_retained)
        self.prepare_button = self._action("Prepare Exact Run", self._prepare)
        self.start_button = self._action("Start Reviewed Run", self._start_run, primary=True)
        self.stop_button = self._action("Stop Run", self._stop)
        self.recover_button = self._action("Check Recovery", self._recover)
        self.verify_button = self._action("Verify Stopped Output", self._verify)
        self.register_button = self._action("Register Verified LoRA", self._register)
        outer.addWidget(self.buttons)
        bind_status_footer(self, self.status, title="Anima training")
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self._actions()

    @classmethod
    def from_retained_review(cls, review_path, registry, jobs, *, service=None, parent=None):
        """Standalone Training entry: select one exact review, then verify its package off-thread."""
        dialog = cls(None, registry, jobs, service=service, parent=parent)
        dialog.open_retained_review(review_path)
        return dialog

    def _label(self, value):
        label = QLabel(value); label.setWordWrap(True); label.setTextFormat(Qt.PlainText)
        return label

    def _action(self, label, callback, *, primary=False):
        button = QPushButton(label); button.clicked.connect(callback)
        if primary: button.setProperty("primary", True)
        self.buttons.addButton(button, QDialogButtonBox.ActionRole)
        return button

    def _choose(self, key):
        selected = QFileDialog.getExistingDirectory(self, "Choose " + key.replace("_", " "),
            self.fields[key].text())
        if selected: self.fields[key].setText(selected)

    def _changed(self, *_):
        if self._restoring_resources: return
        self._generation += 1
        self._selection = self._preview = self._prepared = self._outcome = self._verified = None
        if self._resource_receipt is None:
            self._cache_review = self._resource_preparation = None
        self._registered = False
        self.acknowledge.setChecked(False)
        self.review.setText("Selection changed. Check readiness again before preparing a run.")
        self._actions()

    def _settings(self):
        value = AnimaSettings(max_steps=self.steps.value(), resolution=self.resolution.value(),
            rank=self.rank.value(), seed=int(self.seed.value()), learning_rate=float(self.learning_rate.value()),
            timeout_seconds=self.deadline.value() * 60, batch_size=self.batch_size.value(),
            gradient_accumulation=self.accumulation.value(), repeats=self.repeats.value())
        value.validate()
        return value

    def _request(self):
        values = {key: Path(line.text().strip()) for key, line in self.fields.items()}
        if any(not path.is_absolute() for path in values.values()):
            raise ValueError("Choose absolute local trainer and output folders.")
        name = self.run_name.text().strip()
        if not name:
            raise ValueError("Choose a new run name.")
        return {**{key: str(path) for key, path in values.items()},
            "python": str(values["environment_root"] / "Scripts/python.exe"), "run_name": name}

    def _task_start(self, label, operation, complete, *, on_failed=None):
        if self._task is not None or self._closing:
            return
        task = CoordinatedCallTask(self.jobs, label, operation, resource="io")
        self._task = task
        self._operation = label
        self.status.setText(label + "…")
        from .job_progress import bind_job_progress
        progress_binding = bind_job_progress(self, task, title=label,
            unit="MiB" if label in {"Preparing exact Anima run", "Checking exact Anima readiness", "Recovering exact model preparation"} else "items",
            cancel_button=self.stop_button if label == "Running Anima training" else None)
        self._actions()
        task.signals.complete.connect(lambda result: self._task_done(task, complete, result))
        task.signals.failed.connect(lambda error: self._task_done(task, on_failed or self._failed, error))
        task.signals.cancelled.connect(lambda: self._task_done(task, self._failed, "Operation stopped. Retained evidence remains in the run workspace."))
        try: task.start()
        except Exception as exc:
            progress_binding.finish(task)
            self._task_done(task, on_failed or self._failed, str(exc))

    def _task_done(self, task, complete, result):
        if task is not self._task: return
        self._task = None
        self._operation = ""
        complete(result)
        self._actions()
        if self._closing:
            super().reject()

    def _publish_review(self):
        if self._prepared is not None:
            self.reviewRetained.emit({"prepared": self._prepared,
                "outcome": self._outcome, "registered": self._registered})

    def _failed(self, error):
        message = str(error)[:1000]
        if self._resource_receipt is not None:
            self._show_storage()
            message += " The model preparation remains retained. Retry its recovery or explicitly discard it."
        if self._run_attempted and self._prepared is not None and self._outcome is None:
            message += " Start remains held. Check Recovery to recheck the exact retained review and process before another run."
            self.review.setText(message)
        self.status.setText(message)

    def trainer_setup_saved(self):
        """Offer an explicit reload after Setup saved, without changing this run."""
        self.setup_guidance.setText(
            "Trainer Setup was saved. Choose Use Saved Training Setup to load it into "
            "this run; your entered run choices are still here."
        )
        self.setup_guidance.show()
        self.review.setText(
            "No trainer is loaded into this run. Check Readiness after loading one."
        )
        self.status.setText("Setup saved")
        refresh_status_footer(self)
        self._actions()

    def trainer_setup_cancelled(self):
        """Keep this run editable after leaving trainer resources unsaved."""
        self.setup_guidance.setText(
            "Trainer Setup was not saved. Your run choices are still here. "
            "Open Trainer Setup to try again, or show Manual Trainer Folders."
        )
        self.setup_guidance.show()
        self.review.setText(
            "Run choices unchanged. Readiness was not checked."
        )
        self.status.setText("Setup not saved")
        refresh_status_footer(self)
        self._actions()

    def _load_saved(self):
        if not callable(self.saved_setup):
            self._failed("No saved trainer Setup is available. Choose local trainer folders below."); return
        generation = self._generation
        def work(job):
            return self.saved_setup(should_cancel=lambda: job.cancel_requested,
                progress=lambda *_args: None)
        def done(value):
            if generation != self._generation:
                self._failed("Selections changed while Setup loaded; current choices were kept."); return
            required = {"source_root", "environment_root", "python_base_root", "output_root", "run_name"}
            if type(value) is not dict or not required <= value.keys():
                self.setup_guidance.setText(
                    "Saved trainer Setup is incomplete. Open Trainer Setup to repair it, "
                    "then select Use Saved Training Setup again. Your run choices were kept.")
                self.setup_guidance.show()
                self._failed("Saved trainer Setup is incomplete."); return
            pending_output = self.fields["output_root"].text()
            pending_run_name = self.run_name.text()
            for key in self.fields:
                self.fields[key].setText(
                    pending_output if key == "output_root" and pending_output else str(value[key])
                )
            self.run_name.setText(pending_run_name or str(value["run_name"]))
            self.setup_guidance.hide()
            self.review.setText(
                "Local trainer loaded. Check Readiness to review this training package "
                "before preparing a run."
            )
            self.status.setText(
                "Saved trainer loaded; run choices were kept."
            )
        def failed(error):
            self.setup_guidance.setText(
                f"Saved trainer unavailable: {str(error)[:300]}\n"
                "Open Trainer Setup to choose or repair one, then use the saved setup again. "
                "Your run choices were kept.")
            self.setup_guidance.show()
            self._failed("Saved trainer unavailable. Open Trainer Setup in Local Setup.")
        self._task_start("Loading saved local trainer", work, done, on_failed=failed)

    def _refresh(self):
        if self._package_issue: self._failed(self._package_issue); return
        try:
            request, settings = self._request(), self._settings()
        except (TypeError, ValueError) as exc:
            self._failed(exc); return
        generation = self._generation
        def work(job):
            selection = self.service.prepare_selection(Path(self.package.destination), request,
                Path(request["output_root"]) / (".anima-selection-" + uuid4().hex),
                should_cancel=lambda: job.cancel_requested)
            preview = self.service.preview(selection, settings,
                qualification_path=self.qualification_path,
                should_cancel=lambda: job.cancel_requested)
            cache_review = (self.service.cache_review(selection, settings,
                should_cancel=lambda: job.cancel_requested, progress=self._resource_progress(job))
                if callable(getattr(self.service, "cache_review", None)) else None)
            return selection, preview, cache_review
        def done(value):
            if generation != self._generation:
                self._failed("Selections changed during readiness. Check again."); return
            self._selection, self._preview, self._cache_review = value
            self._show_storage()
            self._prepared = self._outcome = self._verified = None
            if self._preview.ready:
                self.review.setText("Exact package, source, software inventories, selected model bytes, and local qualification match. Review settings and prepare this run. Resume is unsupported; output quality is unmeasured.")
                self.status.setText("Ready for preparation. No GPU work has started.")
            else:
                issues = "\n".join("• " + ISSUE_MESSAGES.get(issue, issue.replace("_", " "))
                    for issue in self._preview.issues)
                self.review.setText("Held:\n" + issues)
                self.status.setText("Anima training is not ready.")
        self._task_start("Checking exact Anima readiness", work, done)

    def _prepare(self):
        if self._preview is None or not self._preview.ready: return
        try: request, settings = self._request(), self._settings()
        except ValueError as exc: self._failed(exc); return
        generation = self._generation
        copy_resources = self._cache_review is not None and self._resource_preparation is None
        if copy_resources:
            if not self._cache_review.ready: return
            self._resource_receipt = self._selection.environment_inventory.parent / "anima-resources.json"
        def work(job):
            resources = self._resource_preparation
            preview = self._preview
            if copy_resources:
                resources = self.service.prepare_resources(self._cache_review, self._resource_receipt.parent,
                    should_cancel=lambda: job.cancel_requested, progress=self._resource_progress(job))
                if resources.status != "ready": return resources, None, None
                preview = self.service.preview(resources.selection, settings,
                    qualification_path=self.qualification_path, should_cancel=lambda: job.cancel_requested)
                if not preview.ready: return resources, preview, None
            prepared = self.service.prepare(preview, Path(request["output_root"]),
                request["run_name"], qualification_path=self.qualification_path,
                should_cancel=lambda: job.cancel_requested)
            return resources, preview, prepared
        def done(value):
            if generation != self._generation:
                self._failed("Selections changed during preparation. The retained preparation needs review."); return
            resources, preview, prepared = value
            if resources is not None:
                self._resource_preparation = resources
                self._selection = resources.selection
            if preview is not None: self._preview = preview
            self._prepared = prepared
            self._show_storage()
            if prepared is None:
                self.review.setText("Model preparation remains protected. Review the storage result and readiness before preparing the exact run.")
                self.status.setText(resources.message if resources is not None else "Preparation needs review.")
                return
            self.review.setText("Prepared exact run: " + str(prepared.plan.destination) +
                "\nFingerprint: " + prepared.plan.fingerprint +
                "\nRetained review: " + str(prepared.preparation_root / "anima-review.json") +
                "\nStart will use local GPU; no checkpoint resume or output-quality claim is available.")
            self.status.setText("Run prepared. Check the exact destination, settings and consequences before Start.")
            self._publish_review()
        self._task_start("Preparing exact Anima run", work, done)

    def _resource_progress(self, job):
        def report(current, total, message):
            units = max(1, (total + 2**20 - 1) // 2**20) if total else 0
            job.update(units if total and current == total else current // 2**20, units, message)
        return report

    def _show_storage(self):
        capacity = getattr(self._cache_review, "capacity", None)
        victims = getattr(capacity, "victims", ())
        show_cleanup = bool(victims and self._resource_receipt is None)
        self.storage_cleanup_summary.setVisible(show_cleanup)
        self.storage_cleanup_summary.setText(f"Fast copies to remove for this run: {len(victims)}. "
            "Archive originals remain. Review their paths and usage below before preparing." if show_cleanup else "")
        lines = []
        if self._cache_review is not None:
            review = self._cache_review
            lines.extend((review.message, f"New copy bytes: {review.new_bytes:,}"))
            from .model_capacity_review import capacity_review_text
            capacity_text = capacity_review_text(getattr(review, "capacity", None))
            if capacity_text: lines.append(capacity_text)
            for row in review.rows:
                lines.append(f"{row.source} → {row.destination}\n{row.status} · {row.policy} · {row.bytes:,} bytes")
        if self._resource_preparation is not None:
            lines.append(self._resource_preparation.message)
        if self._resource_receipt is not None:
            lines.append("Retained preparation: " + str(self._resource_receipt))
            lines.append("Closing this window retains protection. Discard Preparation explicitly closes this attempt; it never deletes model or output files.")
        self.storage_details.setPlainText("\n\n".join(lines) or "Single-location use. Models stay in place.")
        if self._resource_receipt is not None and self._prepared is None:
            self.sections.setCurrentIndex(2)

    def _choose_resources(self):
        path, _ = QFileDialog.getOpenFileName(self, "Open Retained Model Preparation", "",
            "Anima resources (anima-resources.json)")
        if path: self.open_resource_preparation(path)

    def open_resource_preparation(self, path):
        path = Path(path)
        if not path.is_absolute() or self._task is not None or self._resource_receipt is not None: return
        self._resource_receipt = path
        def work(job):
            resources = self.service.open_resources(path, should_cancel=lambda: job.cancel_requested)
            package = self.service.packages.verify(resources.selection.package_path,
                should_cancel=lambda: job.cancel_requested)
            if not package.valid: raise ValueError("The preparation's original package is unavailable or changed.")
            defaults, explanation = _package_defaults(package)
            preview = (self.service.preview(resources.selection, resources.settings,
                qualification_path=self.qualification_path, should_cancel=lambda: job.cancel_requested)
                if resources.status == "ready" else None)
            return resources, package, explanation, preview
        def done(value):
            resources, package, explanation, preview = value
            self.package, self._package_issue = package, ""
            self.package_note.setText(explanation)
            if getattr(resources, "review_path", None) is not None:
                self.open_retained_review(resources.review_path)
                return
            selected, settings = resources.selection, resources.settings
            self._restoring_resources = True
            try:
                for key, value in (("source_root", selected.source_path), ("environment_root", selected.environment_root),
                    ("python_base_root", selected.python_base_root), ("output_root", path.parent.parent)):
                    self.fields[key].setText(str(value))
                self.run_name.setText("anima-" + uuid4().hex[:8])
                for control, value in ((self.steps, settings.max_steps), (self.resolution, settings.resolution),
                    (self.rank, settings.rank), (self.seed, settings.seed), (self.learning_rate, settings.learning_rate),
                    (self.deadline, settings.timeout_seconds // 60), (self.batch_size, settings.batch_size),
                    (self.accumulation, settings.gradient_accumulation), (self.repeats, settings.repeats)):
                    control.setValue(value)
            finally: self._restoring_resources = False
            self._selection, self._preview, self._resource_preparation = selected, preview, resources
            self._show_storage()
            self.review.setText("Retained model preparation reopened. No training was started. Review readiness before preparing the exact run.")
            self.status.setText(resources.message)
        self._task_start("Opening retained model preparation", work, done)

    def _retry_resources(self):
        if self._resource_receipt is None or self._task is not None: return
        path = self._resource_receipt
        def work(job):
            resources = self.service.recover_resources(path, should_cancel=lambda: job.cancel_requested,
                progress=self._resource_progress(job))
            preview = (self.service.preview(resources.selection, resources.settings,
                qualification_path=self.qualification_path, should_cancel=lambda: job.cancel_requested)
                if resources.status == "ready" else None)
            return resources, preview
        def done(value):
            resources, preview = value
            if getattr(resources, "review_path", None) is not None:
                self.open_retained_review(resources.review_path)
                return
            self._selection, self._resource_preparation, self._preview = resources.selection, resources, preview
            self._show_storage(); self.status.setText(resources.message)
        self._task_start("Recovering exact model preparation", work, done)

    def _discard_resources(self):
        if self._resource_receipt is None or self._task is not None: return
        if QMessageBox.question(self, "Discard This Preparation?",
            "Close this preparation so it cannot start or register an output later. Model and output files remain. "
            "Protection is released only when the owner proves no run or result consumer still needs it.",
            QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes: return
        def work(job):
            return self.service.cache_discard(self._prepared or self._resource_receipt, outcome=self._outcome)
        def done(value):
            status = value if isinstance(value, str) else value.get("status") if isinstance(value, dict) else value.status
            if status in {"released", "not_created"}:
                self._resource_receipt = self._resource_preparation = self._cache_review = None
                self._run_attempted = False
                self._changed()
                self._show_storage()
                self.status.setText("Preparation closed. Model and output files remain; check readiness for a new run.")
            else:
                self.status.setText("Preparation ownership is still uncertain. Files remain protected; review the retained run and recovery.")
        self._task_start("Closing exact model preparation", work, done)

    def _choose_retained(self):
        selected, _filter = QFileDialog.getOpenFileName(self, "Open Retained Anima Run",
            "", "Anima review (anima-review.json)")
        if selected:
            self.open_retained_review(Path(selected))

    def open_retained_review(self, path):
        """Review one selected local receipt after restart, without launching."""
        path = Path(path)
        if not path.is_absolute():
            self._failed("Choose an absolute retained Anima review path."); return
        def work(job):
            prepared, outcome, qualification = self.service.open_review(path)
            verification = None
            explanation = ""
            if self.package is None:
                verification = self.service.packages.verify(
                    prepared.preview.selection.package_path,
                    should_cancel=lambda: job.cancel_requested)
                if (not verification.valid or verification.package_fingerprint !=
                        prepared.preview.package_fingerprint):
                    raise ValueError("The retained run's original package is unavailable or changed.")
                _defaults, explanation = _package_defaults(verification)
            receipt = getattr(prepared.preview.selection, "execution_receipt", "")
            resources = self.service.open_resources(receipt, should_cancel=lambda: job.cancel_requested) if receipt else None
            return prepared, outcome, qualification, verification, explanation, resources
        def done(value):
            prepared, outcome, qualification, verification, explanation, resources = value
            if verification is not None:
                self.package = verification
                self._package_issue = ""
                self.package_note.setText(explanation)
            if prepared.preview.selection.package_path != Path(self.package.destination):
                self._failed("The retained run belongs to a different selected package."); return
            selected, settings = prepared.preview.selection, prepared.preview.settings
            for key, path_value in (("source_root", selected.source_path),
                    ("environment_root", selected.environment_root),
                    ("python_base_root", selected.python_base_root),
                    ("output_root", Path(prepared.plan.root))):
                self.fields[key].setText(str(path_value))
            self.run_name.setText(prepared.plan.name)
            for control, value_ in ((self.steps, settings.max_steps),
                    (self.resolution, settings.resolution), (self.rank, settings.rank),
                    (self.seed, settings.seed), (self.learning_rate, settings.learning_rate),
                    (self.deadline, settings.timeout_seconds // 60),
                    (self.batch_size, settings.batch_size),
                    (self.accumulation, settings.gradient_accumulation),
                    (self.repeats, settings.repeats)):
                control.setValue(value_)
            self.qualification_path = qualification
            self._selection, self._preview, self._prepared = selected, prepared.preview, prepared
            receipt = getattr(selected, "execution_receipt", "")
            self._resource_receipt = Path(receipt) if receipt else None
            self._resource_preparation, self._cache_review = resources, None
            self._show_storage()
            self._outcome, self._verified = outcome, None
            self._run_attempted = outcome is not None or resources is not None and (resources.phase != "bound" or resources.status != "ready")
            if outcome is None and self._run_attempted:
                self.review.setText("The retained execution owner has not proved this run is ready to start. Keep its preparation and check recovery; nothing was restarted.")
                self.status.setText("Execution ownership needs review. Missing process output is not proof of a safe restart.")
            elif outcome is None:
                self.review.setText("Retained preparation reopened. Confirm its exact fingerprint and current qualification before Start: " + prepared.plan.fingerprint)
                self.status.setText("No native run was started for this retained preparation.")
            else:
                self.review.setText("Retained Anima run: " + outcome.status +
                    ". Review recovery or verify a stopped output. Nothing was restarted.")
                self.status.setText(outcome.message[:1000])
            self._publish_review()
        self._task_start("Opening retained Anima review", work, done)

    def _start_run(self):
        if self._prepared is None or self._run_attempted or not self.acknowledge.isChecked(): return
        self._run_attempted = True
        self._stop_event.clear()
        prepared = self._prepared
        def work(job):
            return self.service.execute(prepared, prepared.plan.fingerprint, self.jobs,
                qualification_path=self.qualification_path,
                should_cancel=lambda: self._stop_event.is_set() or job.cancel_requested)
        def done(outcome):
            if outcome.status == "held":
                self._run_attempted = False; self._outcome = None
                self.review.setText("The GPU was unavailable before submission. The exact preparation remains protected; retry Start when ready.")
                self.status.setText(outcome.message[:1000]); self._publish_review(); return
            self._outcome = outcome
            self.status.setText(outcome.message[:1000])
            if outcome.status == "exited_unverified":
                self.review.setText("The process stopped. Verify the exact output and input receipts before registration.")
            else:
                self.review.setText("The run did not produce a verified model. Retain its workspace; Check Recovery can confirm stopped ownership without restarting training.")
            self._publish_review()
        self._task_start("Running Anima training", work, done)

    def _stop(self):
        self._stop_event.set()
        self.status.setText("Stopping the owned process tree; waiting for a verified stop…")

    def _recover(self):
        if self._prepared is None: return
        if self._outcome is None:
            if not self._run_attempted: return
            prepared = self._prepared
            def inspect(job):
                restored, outcome, _qualification = self.service.open_review(
                    prepared.preparation_root / "anima-review.json")
                if (restored.plan.fingerprint != prepared.plan.fingerprint
                        or restored.preparation_root != prepared.preparation_root
                        or restored.preview.selection.package_path != prepared.preview.selection.package_path):
                    raise ValueError("The retained Anima review belongs to another preparation.")
                if outcome is None:
                    receipt = getattr(restored.preview.selection, "execution_receipt", "")
                    if receipt:
                        return self.service.open_resources(receipt, should_cancel=lambda: job.cancel_requested)
                    return None
                if job.cancel_requested:
                    raise InterruptedError("Recovery check cancelled.")
                return self.service.recover(restored, outcome.journal_tip)
            def checked(value):
                if hasattr(value, "phase"):
                    self._resource_preparation = value
                    if value.phase == "bound" and value.status == "ready":
                        self._run_attempted = False
                        self.review.setText("The owner confirms this exact run is unsubmitted. Its preparation remains protected; retry Start when ready.")
                        self.status.setText("Exact unsubmitted preparation restored. No training restarted.")
                    else:
                        self.review.setText("The execution owner has not proved a safe restart or release. Keep the retained preparation and recovery evidence.")
                        self.status.setText("Execution ownership remains uncertain; files stay protected.")
                    self._show_storage(); self._publish_review()
                elif value is None:
                    retained = self._prepared
                    self._selection = self._preview = self._prepared = None
                    self._run_attempted = False
                    self.acknowledge.setChecked(False)
                    self.review.setText("No process reservation was found for this exact retained preparation. Return to Setup, check the selected resources, and prepare a new run name.")
                    self.status.setText("No run launched. The original preparation remains on disk; a new readiness check is required.")
                    self.reviewRetained.emit({"prepared": retained, "outcome": None, "registered": False})
                else:
                    self._outcome = value
                    self.status.setText(value.message[:1000])
                    self.review.setText("Exact retained process checked. No training was restarted; review the stopped output or preserve this run's recovery state.")
                    self._publish_review()
            self._task_start("Checking retained Anima launch and process", inspect, checked)
            return
        if not self._outcome.journal_tip: return
        def work(job):
            return self.service.recover(self._prepared, self._outcome.journal_tip)
        def done(value):
            self._outcome = value
            self.status.setText(value.message[:1000])
            self.review.setText("Recovery checked the stopped process tree. It did not restart training or register an output.")
            self._publish_review()
        self._task_start("Checking retained Anima recovery", work, done)

    def _verify(self):
        if self._prepared is None or self._outcome is None: return
        def work(job):
            return self.service.verify_result(self._prepared, self._outcome,
                should_cancel=lambda: job.cancel_requested)
        def done(verified):
            self._verified = verified
            self.status.setText("Exact output and process evidence verified. Registration still requires a separate action.")
            self.review.setText("Verified Anima LoRA: " + verified.output_id +
                "\nDiT tensor count: " + str(verified.tensor_count) +
                "\nOutput quality: unmeasured. The selected package and source files remain unchanged.")
        self._task_start("Verifying stopped Anima output", work, done)

    def _register(self):
        if self._verified is None: return
        def work(job):
            expected = self.registry.snapshot()[1]
            return self.service.register(self._verified, expected,
                qualification_path=self.qualification_path,
                should_cancel=lambda: job.cancel_requested)
        def done(resource_id):
            self._registered = True
            self.status.setText("Verified Anima LoRA registered with exact package and model lineage.")
            self.resourceRegistered.emit(resource_id)
            self._publish_review()
        self._task_start("Registering verified Anima LoRA", work, done)

    def _actions(self, *_):
        busy = self._task is not None
        running = busy and self._operation == "Running Anima training"
        protected = self._resource_receipt is not None
        try:
            self._request()
            request_complete = True
        except ValueError:
            request_complete = False
        for widget in (*self.fields.values(), *self._choose_buttons, self.run_name,
                       self.rank, self.deadline):
            widget.setEnabled(not busy and not self._run_attempted and not protected)
        self.saved_button.setEnabled(not busy and not self._run_attempted and not protected and callable(self.saved_setup))
        self.open_trainer_setup_button.setEnabled(not busy and not self._run_attempted and not protected and callable(self.saved_setup))
        self.open_button.setEnabled(not busy and not protected)
        self.open_resources_button.setEnabled(not busy and not protected and not self._run_attempted)
        self.retry_resources_button.setEnabled(not busy and protected and not self._run_attempted and self._prepared is None)
        self.discard_resources_button.setEnabled(not busy and protected)
        self.retry_resources_button.setVisible(protected and self._prepared is None)
        self.discard_resources_button.setVisible(protected)
        self.preview_button.setEnabled(not busy and not self._run_attempted and not protected
            and not self._package_issue and request_complete)
        self.preview_button.setToolTip("Check the selected package, trainer and model resources."
            if request_complete else
            "Use Saved Training Setup or enter local trainer folders and a run name first.")
        storage_ready = ((not protected and (self._cache_review is None or self._cache_review.ready))
            or protected and getattr(self._resource_preparation, "status", "") == "ready")
        capacity = getattr(self._cache_review, "capacity", None)
        self.prepare_button.setText("Make Room and Prepare" if not protected and capacity and capacity.victims
            else "Prepare Exact Run")
        self.prepare_button.setEnabled(not busy and not self._run_attempted and storage_ready and bool(getattr(self._preview, "ready", False)))
        self.start_button.setEnabled(not busy and not self._run_attempted and self._prepared is not None and self._outcome is None
            and self.acknowledge.isChecked())
        self.stop_button.setEnabled(running)
        missing_outcome = self._run_attempted and self._prepared is not None and self._outcome is None
        self.recover_button.setEnabled(not busy and (missing_outcome or
            self._outcome is not None and bool(self._outcome.journal_tip)
            and self._outcome.status != "exited_unverified"))
        self.verify_button.setEnabled(not busy and self._outcome is not None
            and self._outcome.status == "exited_unverified" and self._verified is None)
        self.register_button.setEnabled(not busy and self._verified is not None and not self._registered)
        self.prepare_button.setVisible(bool(getattr(self._preview, "ready", False)) and self._prepared is None)
        self.start_button.setVisible(self._prepared is not None and self._outcome is None
            and not self._run_attempted and not running)
        self.stop_button.setVisible(running)
        self.recover_button.setVisible(missing_outcome or
            self._outcome is not None and self._outcome.status != "exited_unverified")
        self.verify_button.setVisible(self._outcome is not None and self._outcome.status == "exited_unverified" and self._verified is None)
        self.register_button.setVisible(self._verified is not None and not self._registered)

    def reject(self):
        self._closing = True
        if self._task is not None:
            self._stop_event.set()
            if self._operation != "Running Anima training" and self._task.job_id:
                self.jobs.cancel(self._task.job_id)
            self.status.setText("Stopping current work before closing…")
            return
        super().reject()

    def closeEvent(self, event):
        if self._task is not None:
            event.ignore(); self.reject()
        else:
            super().closeEvent(event)
