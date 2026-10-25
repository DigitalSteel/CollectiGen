"""Source-free authoring of one reviewed, reusable Generation Setup."""
from __future__ import annotations

from copy import deepcopy
from uuid import uuid4

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox, QDialog, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QScrollArea, QSpinBox, QTextEdit, QTreeWidget,
    QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.generation_recipes import make_manual_recipe, portable_recipe_resource, render_prompt_parts
from core.generation_saved_work import save_named_recipe
from .components import DisclosureButton
from .generation_setup_save import SetupSaveDialog
from .workers import CoordinatedCallTask


_PARTS = (("character", "Character traits"), ("outfit", "Outfit details"),
          ("scene", "Scene elements"),
          ("pose", "Pose or action"), ("style", "Style"), ("other", "Other prompt text"))


def choose_registered_setup_resource(parent, registry, jobs, role, *,
                                     main_model_resource_id="", settings=None,
                                     protected_model_roots=()):
    """Use the shared exact-role picker for either Generation Setup composer."""
    from .model_selector_dialog import ModelSelectorDialog
    from .generation_workflow import _registered_resource_with_label

    picker = ModelSelectorDialog(
        registry, jobs, parent=parent, role=role,
        main_model_resource_id=main_model_resource_id,
        settings=settings, protected_roots=tuple(protected_model_roots),
    )
    if picker.exec() != QDialog.Accepted:
        return None
    resource, label = _registered_resource_with_label(
        registry, picker.selected_resource_id, selected_role=picker.selected_role,
        selected_family=picker.selected_family,
    )
    row = portable_recipe_resource(resource, display_label=label)
    if row["role"] == "lora" and not row["weights"]:
        row["weights"] = {"model": 1.0, "text_encoder": 1.0}
    return row


class ManualGenerationSetupDialog(SetupSaveDialog):
    """One canonical setup draft; choosing resources never edits a request."""

    feedback_workflow = "Create generation setup"

    def __init__(self, jobs, storage_root, *, registry=None, settings=None,
                 protected_model_roots=(), pre_save_check=None, part_order=None,
                 parent=None):
        super().__init__(parent)
        self.jobs = jobs
        self.storage_root = storage_root
        self.registry = registry
        self.settings = settings
        self.protected_model_roots = tuple(protected_model_roots)
        self.pre_save_check = pre_save_check
        self.recipe = None
        self.saved_path = None
        self._task = None
        self._operation_recipe = None
        self._operation_id = ""
        self._save_attempt = 0
        self.setWindowTitle("Create Generation Setup")
        self.resize(860, 660)
        layout = QVBoxLayout(self)
        heading = QLabel("Make a reusable starting point")
        heading.setObjectName("sectionTitle")
        heading.setTextFormat(Qt.PlainText)
        heading.setWordWrap(True)
        self.heading = heading
        layout.addWidget(heading)
        intro = QLabel("Describe what to keep in future images. All parts are optional; no image, generator, or model is needed to save prompt-only ideas. Nothing runs or changes an open draft.")
        self.intro = intro
        intro.setWordWrap(True)
        layout.addWidget(intro)

        scroll = QScrollArea()
        self.setup_scroll = scroll
        scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        body = QWidget()
        body_layout = QVBoxLayout(body)
        body_layout.setContentsMargins(0, 0, 0, 0)
        self.setup_body_layout = body_layout
        self.setup_section_title = QLabel("Reusable setup")
        self.setup_section_title.setObjectName("sectionTitle")
        body_layout.addWidget(self.setup_section_title)
        name_form = QFormLayout()
        self.name_form = name_form
        self.name = QLineEdit()
        self.name.setPlaceholderText("For example, Rainy portrait style")
        self.name.setAccessibleName("Generation setup name")
        name_form.addRow("Setup name", self.name)
        body_layout.addLayout(name_form)
        self.parts_panel = QWidget()
        form = QFormLayout(self.parts_panel)
        self.part_fields = {}
        labels = dict(_PARTS)
        ordered_parts = (tuple(part_order) if part_order is not None else
                         tuple(kind for kind, _label in _PARTS))
        if set(ordered_parts) != set(labels) or len(ordered_parts) != len(labels):
            raise ValueError("A Generation Setup must show each editable prompt part once.")
        for kind in ordered_parts:
            label = labels[kind]
            field = QLineEdit()
            field.setAccessibleName(label + " to reuse")
            field.setPlaceholderText("Optional")
            self.part_fields[kind] = field
            form.addRow(label, field)
            field.textChanged.connect(self._invalidate)
        body_layout.addWidget(self.parts_panel)
        self.name.textChanged.connect(self._invalidate)

        advanced = QWidget()
        advanced_layout = QVBoxLayout(advanced)
        advanced_layout.setContentsMargins(18, 0, 0, 0)
        advanced_hint = QLabel("Only checked exact components and settings enter this setup. Unselected parts of a later request are retained and checked again before generation.")
        advanced_hint.setWordWrap(True)
        advanced_layout.addWidget(advanced_hint)
        resource_actions = QHBoxLayout()
        self.choose_checkpoint = QPushButton("Choose Model…")
        self.add_lora = QPushButton("Add LoRA…")
        self.choose_vae = QPushButton("Choose VAE…")
        for button, role in ((self.choose_checkpoint, "checkpoint"),
                             (self.add_lora, "lora"), (self.choose_vae, "vae")):
            button.setEnabled(registry is not None)
            button.clicked.connect(lambda _checked=False, value=role: self._choose_resource(value))
            resource_actions.addWidget(button)
        resource_actions.addStretch(1)
        advanced_layout.addLayout(resource_actions)
        if registry is None:
            unavailable = QLabel("Model choices are unavailable here; a text-only setup still works. Register models in Models first.")
            unavailable.setWordWrap(True)
            advanced_layout.addWidget(unavailable)
        self.resource_tree = QTreeWidget()
        self.resource_tree.setHeaderLabels(("Keep", "Exact component", "Image strength", "Text strength"))
        self.resource_tree.setRootIsDecorated(False)
        self.resource_tree.setAccessibleName("Exact model components for this setup")
        self.resource_tree.setFixedHeight(140)
        self.resource_tree.setColumnWidth(0, 58)
        self.resource_tree.setColumnWidth(1, 330)
        self.resource_tree.setColumnWidth(2, 135)
        self.resource_tree.header().setStretchLastSection(True)
        self.resource_tree.itemChanged.connect(self._invalidate)
        advanced_layout.addWidget(self.resource_tree)
        self.size_choice = QCheckBox("Include image size")
        self.width_spin = QSpinBox(); self.height_spin = QSpinBox()
        for field in (self.width_spin, self.height_spin):
            field.setRange(64, 8192); field.setSingleStep(64); field.setValue(1024)
            field.valueChanged.connect(self._invalidate)
        size_row = QHBoxLayout()
        size_row.addWidget(self.size_choice)
        size_row.addWidget(self.width_spin); size_row.addWidget(QLabel("×"))
        size_row.addWidget(self.height_spin); size_row.addStretch(1)
        advanced_layout.addLayout(size_row)
        settings_form = QFormLayout()
        self.steps_choice = QCheckBox("Include steps")
        self.steps = QSpinBox(); self.steps.setRange(1, 200); self.steps.setValue(30)
        step_row = QHBoxLayout(); step_row.addWidget(self.steps_choice); step_row.addWidget(self.steps); step_row.addStretch(1)
        settings_form.addRow(step_row)
        self.sampler_choice = QCheckBox("Include sampler")
        self.sampler = QLineEdit(); self.sampler.setPlaceholderText("Exact adapter name")
        sampler_row = QHBoxLayout(); sampler_row.addWidget(self.sampler_choice); sampler_row.addWidget(self.sampler)
        settings_form.addRow(sampler_row)
        self.scheduler_choice = QCheckBox("Include scheduler")
        self.scheduler = QLineEdit(); self.scheduler.setPlaceholderText("Exact adapter name")
        scheduler_row = QHBoxLayout(); scheduler_row.addWidget(self.scheduler_choice); scheduler_row.addWidget(self.scheduler)
        settings_form.addRow(scheduler_row)
        self.guidance_choice = QCheckBox("Include guidance")
        self.guidance = QDoubleSpinBox(); self.guidance.setRange(0, 50); self.guidance.setDecimals(2); self.guidance.setValue(7)
        guidance_row = QHBoxLayout(); guidance_row.addWidget(self.guidance_choice); guidance_row.addWidget(self.guidance); guidance_row.addStretch(1)
        settings_form.addRow(guidance_row)
        advanced_layout.addLayout(settings_form)
        self.negative_choice = QCheckBox("Include negative prompt (blank clears it on apply)")
        self.negative = QTextEdit(); self.negative.setMaximumHeight(75)
        self.negative.setAccessibleName("Negative prompt to reuse")
        advanced_layout.addWidget(self.negative_choice)
        advanced_layout.addWidget(self.negative)
        for control in (self.size_choice, self.steps_choice, self.sampler_choice,
                        self.scheduler_choice, self.guidance_choice, self.negative_choice):
            control.toggled.connect(self._invalidate)
        for control in (self.steps, self.guidance):
            control.valueChanged.connect(self._invalidate)
        for control in (self.sampler, self.scheduler):
            control.textChanged.connect(self._invalidate)
        self.negative.textChanged.connect(self._invalidate)
        self.advanced_toggle = DisclosureButton("Customize Models and Settings",
            "Hide Models and Settings", advanced, self)
        body_layout.addWidget(self.advanced_toggle, 0, Qt.AlignLeft)
        body_layout.addWidget(advanced)
        body_layout.addStretch(1)
        scroll.setWidget(body)
        layout.addWidget(scroll, 1)

        self.review_button = QPushButton("Review Setup")
        self.review_button.clicked.connect(self.review)
        layout.addWidget(self.review_button, 0, Qt.AlignLeft)
        self.preview = QTextEdit()
        self.preview.setReadOnly(True)
        self.preview.setAccessibleName("Proposed reusable setup summary")
        self.preview.setFixedHeight(105)
        layout.addWidget(self.preview)
        self.preview.hide()
        self.status = QLabel("Enter a name and at least one part, then review before saving.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        self.save_button = self.buttons.button(QDialogButtonBox.Save)
        self.save_button.setText("Save Reviewed Setup")
        self.save_button.setEnabled(False)
        self.save_button.clicked.connect(self.save_reviewed)
        self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    def _invalidate(self, *_args):
        self.recipe = None
        self.save_button.setEnabled(False)
        self.preview.clear()
        self.preview.hide()
        self.status.setText("Review the changed setup before saving.")

    def _choose_resource(self, role):
        if self.registry is None or self.save_in_progress:
            return
        main = next((self.resource_tree.topLevelItem(index).data(0, Qt.UserRole)
                     for index in range(self.resource_tree.topLevelItemCount())
                     if self.resource_tree.topLevelItem(index).data(0, Qt.UserRole)["role"] == "checkpoint"), None)
        try:
            row = choose_registered_setup_resource(
                self, self.registry, self.jobs, role,
                main_model_resource_id=main["id"] if main else "",
                settings=self.settings, protected_model_roots=self.protected_model_roots,
            )
        except (ValueError, RuntimeError) as exc:
            self.status.setText(str(exc))
            return
        if row is None:
            return
        self._insert_resource(row)

    def _insert_resource(self, row):
        """Put an explicitly picked exact resource in the one setup draft."""
        for index in reversed(range(self.resource_tree.topLevelItemCount())):
            existing = self.resource_tree.topLevelItem(index).data(0, Qt.UserRole)
            if (existing["role"] == row["role"] and row["role"] in {"checkpoint", "vae"}) or (
                    existing["role"] == row["role"] == "lora" and existing["id"] == row["id"]):
                self.resource_tree.takeTopLevelItem(index)
        item = QTreeWidgetItem(("", row.get("display_label") or row["name"], "", ""))
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(0, Qt.Checked)
        item.setData(0, Qt.UserRole, row)
        item.setToolTip(1, row.get("display_label") or row["name"])
        self.resource_tree.addTopLevelItem(item)
        if row["role"] == "lora":
            for column, key in ((2, "model"), (3, "text_encoder")):
                weight = QDoubleSpinBox()
                weight.setRange(-10, 10); weight.setDecimals(3); weight.setSingleStep(.05)
                weight.setValue(float(row["weights"].get(key, 1)))
                weight.valueChanged.connect(self._invalidate)
                self.resource_tree.setItemWidget(item, column, weight)
        self.advanced_toggle.setChecked(True)
        self._invalidate()

    def _chosen_resources(self):
        rows = []
        for index in range(self.resource_tree.topLevelItemCount()):
            item = self.resource_tree.topLevelItem(index)
            if item.checkState(0) != Qt.Checked:
                continue
            row = deepcopy(item.data(0, Qt.UserRole))
            if row["role"] == "lora":
                row["weights"] = {key: self.resource_tree.itemWidget(item, column).value()
                                  for column, key in ((2, "model"), (3, "text_encoder"))}
            rows.append(row)
        return rows

    def review(self):
        parts = [{"kind": kind, "text": field.text()}
                 for kind, field in self.part_fields.items() if field.text().strip()]
        resources = self._chosen_resources()
        settings = {}
        checkpoint = next((row for row in resources if row["role"] == "checkpoint"), None)
        if checkpoint:
            settings.update(model="", model_hash=checkpoint["hashes"]["sha256"])
        if self.size_choice.isChecked():
            settings.update(width=self.width_spin.value(), height=self.height_spin.value())
        if self.steps_choice.isChecked(): settings["steps"] = self.steps.value()
        if self.sampler_choice.isChecked(): settings["sampler"] = self.sampler.text().strip()
        if self.scheduler_choice.isChecked(): settings["scheduler"] = self.scheduler.text().strip()
        if self.guidance_choice.isChecked(): settings["cfg_scale"] = self.guidance.value()
        if self.sampler_choice.isChecked() and not settings["sampler"]:
            self.status.setText("Enter a sampler name or uncheck Include sampler."); return
        if self.scheduler_choice.isChecked() and not settings["scheduler"]:
            self.status.setText("Enter a scheduler name or uncheck Include scheduler."); return
        negative = self.negative.toPlainText() if self.negative_choice.isChecked() else None
        try:
            recipe = make_manual_recipe(self.name.text(), parts, negative_prompt=negative,
                settings=settings, resources=resources)
        except ValueError as exc:
            self.status.setText(str(exc))
            self.save_button.setEnabled(False)
            return
        self.recipe = recipe
        rendered = render_prompt_parts(recipe["prompt_parts"])
        lines = ["Prompt to reuse: " + (rendered or "none"),
                 "Negative prompt: " + ("replace with " + (negative or "blank") if negative is not None else "keep the new request's value"),
                 f"Exact components: {len(resources)} selected; omitted components of a later request remain",
                 "Other settings: " + (", ".join(key for key in settings if key not in {"model", "model_hash"}) or "none")]
        self.preview.setPlainText("\n".join(lines))
        self.preview.show()
        self.status.setText("Ready to save. Applying later needs a separate review and fresh model/readiness checks; no generation starts here.")
        self.save_button.setEnabled(True)

    def save_reviewed(self):
        if self.recipe is None or self.save_in_progress:
            return
        recipe = deepcopy(self.recipe)
        if recipe != self._operation_recipe:
            self._operation_recipe = deepcopy(recipe)
            self._operation_id = uuid4().hex
        self.save_in_progress = True
        self.setEnabled(False)
        self.status.setText("Saving the reviewed setup…")
        self._save_attempt += 1
        def publish(job):
            if self.pre_save_check is not None:
                self.pre_save_check(should_cancel=lambda: bool(getattr(job, "cancel_requested", False)))
            if getattr(job, "cancel_requested", False):
                raise InterruptedError("The setup save was cancelled.")
            return self._publish_reviewed_recipe(recipe, self._operation_id)
        task = CoordinatedCallTask(self.jobs, "Save generation setup", publish,
            resource="io", key=f"manual-generation-setup:{id(self)}:{self._operation_id}:{self._save_attempt}")
        self._task = task
        task.signals.complete.connect(lambda path, active=task: self._save_complete(active, path))
        task.signals.failed.connect(lambda error, active=task: self._save_failed(active, str(error)))
        task.signals.cancelled.connect(lambda active=task: self._save_failed(active, "The local save was cancelled."))
        try:
            task.start()
        except Exception as exc:
            self._save_failed(task, str(exc) or "The shared job coordinator is unavailable.")

    def _publish_reviewed_recipe(self, recipe, operation_id):
        return save_named_recipe(self.storage_root, recipe, operation_id=operation_id)

    def _save_complete(self, task, path):
        if self._task is not task:
            return
        self._task = None
        self.saved_path = path
        self.save_in_progress = False
        self.setEnabled(True)
        self.accept()

    def _save_failed(self, task, error):
        if self._task is not task:
            return
        self._task = None
        self.save_in_progress = False
        self.setEnabled(True)
        self.status.setText("Setup was not confirmed saved: " + error + " Your reviewed choices remain; retry Save Reviewed Setup.")
        self.save_button.setEnabled(self.recipe is not None)
