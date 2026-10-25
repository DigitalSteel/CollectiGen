"""Review local SwarmUI preset text/settings before authoring one canonical setup."""
from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QPoint, Qt, QTimer
from PySide6.QtWidgets import (
    QFileDialog, QComboBox, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QListWidget, QPushButton, QScrollArea, QTabWidget, QTextEdit, QVBoxLayout, QWidget,
)
from shiboken6 import isValid

from core.swarm_setup_file_import import (
    compose_swarm_setup_presets, composed_swarm_setup_evidence,
    inspect_swarm_setup_composition, inspect_swarm_setup_composition_resources,
    inspect_swarm_setup_resource_references,
    read_swarm_setup_file, recheck_swarm_setup_file,
    select_swarm_setup_preset, selected_swarm_setup_evidence,
)
from core.generation_saved_work import save_named_recipe
from .components import DisclosureButton
from .generation_manual_setup_dialog import (ManualGenerationSetupDialog,
                                             choose_registered_setup_resource)
from .workers import CoordinatedCallTask


class SwarmSetupFileDialog(ManualGenerationSetupDialog):
    """Selected local-file translation; source review and save stay separate."""

    feedback_workflow = "Import SwarmUI preset as generation setup"

    def __init__(self, jobs, storage_root, *, registry=None, settings=None,
                 protected_model_roots=(), parent=None):
        self._source_task = None
        self._source_review = None
        self._applied_review = None
        self._applied_key = ""
        self._applied_keys = ()
        self._applied_choices = {}
        self._composition_keys = []
        self._composition_choices = {}
        self._conflict_controls = {}
        self._composition_base_summary = ""
        self._resource_choices = {}
        self._resource_source_choices = {}
        self._resource_reference_rows = {}
        self._resource_source_controls = {}
        self._operation_source_marker = None
        self._closing = False
        super().__init__(jobs, storage_root, registry=registry, settings=settings,
                         protected_model_roots=protected_model_roots,
                         pre_save_check=self._check_import_source, parent=parent)
        self.setWindowTitle("Import SwarmUI Preset as Generation Setup")
        self.heading.setText("Make a setup from SwarmUI presets")
        self.intro.setText(
            "Choose a source preset, then edit what to reuse. Combining presets and "
            "matching models are optional. Nothing generates or changes an open draft."
        )
        self.resize(960, 800)
        self.setup_section_title.hide()
        layout = self.layout()
        layout.removeWidget(self.setup_scroll)
        self.task_tabs = QTabWidget()
        self.task_tabs.setAccessibleName("SwarmUI preset import sections")
        layout.insertWidget(2, self.task_tabs, 1)
        self.source_scroll = QScrollArea()
        self.source_scroll.setWidgetResizable(True)
        self.source_scroll.setFrameShape(QScrollArea.NoFrame)
        source_page = QWidget()
        source_page_layout = QVBoxLayout(source_page)
        source_page_layout.setContentsMargins(0, 0, 0, 0)
        self.source_scroll.setWidget(source_page)
        self.task_tabs.addTab(self.source_scroll, "1. Source Presets")
        self.match_scroll = QScrollArea()
        self.match_scroll.setWidgetResizable(True)
        self.match_scroll.setFrameShape(QScrollArea.NoFrame)
        match_page = QWidget()
        match_layout = QVBoxLayout(match_page)
        match_layout.setContentsMargins(0, 0, 0, 0)
        self.match_scroll.setWidget(match_page)
        self.task_tabs.addTab(self.match_scroll, "2. Model Matches")
        self.task_tabs.addTab(self.setup_scroll, "3. Reusable Setup")

        source = QGroupBox("1. Choose a local SwarmUI preset")
        layout = QVBoxLayout(source)
        note = QLabel(
            "Choose a SwarmUI preset JSON export. Start with one preset, or optionally "
            "combine supported text and settings from several. The original file stays unchanged; "
            "nothing is generated or imported into a live account."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        buttons = QHBoxLayout()
        self.choose_file_button = QPushButton("Choose SwarmUI File…")
        self.refresh_file_button = QPushButton("Refresh This File")
        self.refresh_file_button.setEnabled(False)
        self.stop_file_button = QPushButton("Stop Reading")
        self.stop_file_button.hide()
        for button in (self.choose_file_button, self.refresh_file_button,
                       self.stop_file_button):
            buttons.addWidget(button)
        buttons.addStretch(1)
        layout.addLayout(buttons)
        self.source_path = QLabel("No file selected")
        self.source_path.setWordWrap(True)
        self.source_path.setTextFormat(Qt.PlainText)
        layout.addWidget(self.source_path)
        self.file_status = QLabel("Choose a file to see available presets.")
        self.file_status.setWordWrap(True)
        self.file_status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.file_status)
        self.preset_choice = QComboBox()
        self.preset_choice.setAccessibleName("SwarmUI preset to turn into a Generation Setup")
        self.preset_choice.hide()
        layout.addWidget(self.preset_choice)
        self.source_summary = QLabel()
        self.source_summary.setWordWrap(True)
        self.source_summary.setTextFormat(Qt.PlainText)
        self.source_summary.hide()
        layout.addWidget(self.source_summary)
        self.source_details = QTextEdit()
        self.source_details.setReadOnly(True)
        self.source_details.setFixedHeight(105)
        self.source_details.setAccessibleName("Exact imported fields and omissions")
        self.details_toggle = DisclosureButton(
            "Inspect Source and Omissions (optional)", "Hide Source and Omissions",
            self.source_details, self,
        )
        self.details_toggle.hide()
        layout.addWidget(self.details_toggle, 0, Qt.AlignLeft)
        layout.addWidget(self.source_details)
        self.use_preset_button = QPushButton("Use Selected Preset in Setup")
        self.use_preset_button.setEnabled(False)
        self.use_preset_button.hide()
        layout.addWidget(self.use_preset_button, 0, Qt.AlignLeft)
        compose_panel = QWidget()
        compose_layout = QVBoxLayout(compose_panel)
        compose_layout.setContentsMargins(12, 0, 0, 0)
        compose_hint = QLabel(
            "Combine supported literal text and settings in order. Source model/LoRA names, "
            "dynamic prompts and task-specific controls stay in the omission review. "
            "Choose every differing setting before copying the result to Reusable Setup."
        )
        compose_hint.setWordWrap(True)
        compose_layout.addWidget(compose_hint)
        compose_actions = QHBoxLayout()
        self.add_block_button = QPushButton("Add Chosen Preset")
        self.add_block_button.setToolTip("Add the preset selected above to the ordered combination, without changing the setup yet.")
        self.move_block_up_button = QPushButton("Move Up")
        self.move_block_down_button = QPushButton("Move Down")
        self.remove_block_button = QPushButton("Remove")
        self.remove_block_button.setToolTip("Remove the highlighted block from the proposed combination; authored setup fields stay unchanged.")
        for button in (self.add_block_button, self.move_block_up_button,
                       self.move_block_down_button, self.remove_block_button):
            compose_actions.addWidget(button)
        compose_actions.addStretch(1)
        compose_layout.addLayout(compose_actions)
        self.block_list = QListWidget()
        self.block_list.setFixedHeight(100)
        self.block_list.setAccessibleName("Ordered SwarmUI preset blocks for this setup")
        compose_layout.addWidget(self.block_list)
        self.composition_summary = QLabel("Add at least two presets to compose a setup.")
        self.composition_summary.setWordWrap(True)
        self.composition_summary.setTextFormat(Qt.PlainText)
        compose_layout.addWidget(self.composition_summary)
        self.composition_details = QTextEdit()
        self.composition_details.setReadOnly(True)
        self.composition_details.setFixedHeight(120)
        self.composition_details.setAccessibleName("Exact chosen SwarmUI blocks and omitted source fields")
        self.composition_details_toggle = DisclosureButton(
            "Inspect Chosen Blocks and Omissions (optional)",
            "Hide Chosen Blocks and Omissions", self.composition_details, self,
        )
        self.composition_details_toggle.hide()
        compose_layout.addWidget(self.composition_details_toggle, 0, Qt.AlignLeft)
        compose_layout.addWidget(self.composition_details)
        self.conflict_form = QFormLayout()
        compose_layout.addLayout(self.conflict_form)
        self.use_composition_button = QPushButton("Use Reviewed Combination in Setup")
        self.use_composition_button.setEnabled(False)
        compose_layout.addWidget(self.use_composition_button, 0, Qt.AlignLeft)
        self.compose_toggle = DisclosureButton(
            "Combine Presets (optional)", "Hide Preset Combination", compose_panel, self,
        )
        self.compose_toggle.hide()
        layout.addWidget(self.compose_toggle, 0, Qt.AlignLeft)
        layout.addWidget(compose_panel)
        source_page_layout.addWidget(source)
        source_page_layout.addStretch(1)
        resource_group = QGroupBox("Match source models (optional)")
        resource_layout = QVBoxLayout(resource_group)
        resource_hint = QLabel(
            "Source names do not prove model identity. Choose an exact registered copy for "
            "each model you want, or leave it unmatched. The original name remains in import details."
        )
        resource_hint.setWordWrap(True)
        resource_layout.addWidget(resource_hint)
        for field, label, role in (("model", "Main Model", "checkpoint"),
                                   ("vae", "VAE", "vae")):
            row = QWidget()
            row_layout = QHBoxLayout(row)
            row_layout.setContentsMargins(0, 0, 0, 0)
            source_label = QLabel()
            source_label.setTextFormat(Qt.PlainText)
            source_label.setWordWrap(True)
            source_label.setAccessibleName(f"SwarmUI {label} source name")
            source_choice = QComboBox()
            source_choice.setAccessibleName(f"Contributing SwarmUI preset for {label}")
            source_choice.setToolTip(
                "Choose which selected preset supplied the source name. This does not choose a model file."
            )
            source_choice.hide()
            source_choice.currentIndexChanged.connect(
                lambda _index, key=field: self._import_source_choice_changed(key)
            )
            choose = QPushButton(f"Choose Exact {label}…")
            choose.setEnabled(registry is not None)
            choose.clicked.connect(lambda _checked=False, key=field: self._choose_import_resource(key))
            clear = QPushButton("Leave Unmatched")
            clear.clicked.connect(lambda _checked=False, key=field: self._clear_import_resource(key))
            source_cell = QWidget()
            source_cell_layout = QVBoxLayout(source_cell)
            source_cell_layout.setContentsMargins(0, 0, 0, 0)
            source_cell_layout.addWidget(source_label)
            source_cell_layout.addWidget(source_choice)
            row_layout.addWidget(source_cell, 1)
            row_layout.addWidget(choose)
            row_layout.addWidget(clear)
            resource_layout.addWidget(row)
            self._resource_reference_rows[field] = (row, source_label, choose, clear, role)
            self._resource_source_controls[field] = source_choice
        self.source_resource_status = QLabel()
        self.source_resource_status.setTextFormat(Qt.PlainText)
        self.source_resource_status.setWordWrap(True)
        resource_layout.addWidget(self.source_resource_status)
        self.source_resource_group = resource_group
        self.source_resource_group.hide()
        self.no_resource_refs = QLabel(
            "Choose and use a source preset first. If it names a main model or VAE, "
            "you can match an exact registered copy here; matching is optional."
        )
        self.no_resource_refs.setWordWrap(True)
        match_layout.addWidget(self.no_resource_refs)
        match_layout.addWidget(resource_group)
        match_layout.addStretch(1)
        self.review_button.setEnabled(False)
        self.status.setText("Choose a source preset, then edit Reusable Setup; combining is optional.")
        self.choose_file_button.clicked.connect(self.choose_file)
        self.refresh_file_button.clicked.connect(self.refresh_file)
        self.stop_file_button.clicked.connect(self.stop_reading)
        self.preset_choice.currentIndexChanged.connect(self._show_selected)
        self.use_preset_button.clicked.connect(self.use_selected_preset)
        self.add_block_button.clicked.connect(self.add_selected_block)
        self.move_block_up_button.clicked.connect(lambda: self._move_block(-1))
        self.move_block_down_button.clicked.connect(lambda: self._move_block(1))
        self.remove_block_button.clicked.connect(self.remove_selected_block)
        self.block_list.currentRowChanged.connect(self._update_block_actions)
        self.use_composition_button.clicked.connect(self.use_composed_presets)

    def choose_file(self):
        if self._source_task is not None or self.save_in_progress:
            return
        path, _ = QFileDialog.getOpenFileName(
            self, "Choose a SwarmUI Preset Export", "", "JSON files (*.json)",
        )
        if path:
            self._read_file(Path(path))

    def refresh_file(self):
        if self._source_review is not None:
            self._read_file(self._source_review.path)

    def _read_file(self, path):
        if self._source_task is not None or self.save_in_progress:
            return
        self.task_tabs.setCurrentWidget(self.source_scroll)
        self.file_status.setText("Reading the selected local file…")
        self.choose_file_button.setEnabled(False)
        self.refresh_file_button.setEnabled(False)
        self.stop_file_button.show()
        self.use_preset_button.setEnabled(False)
        self.add_block_button.setEnabled(False)
        self.use_composition_button.setEnabled(False)
        self.review_button.setEnabled(False)
        self._invalidate()
        task = CoordinatedCallTask(
            self.jobs, "Read local SwarmUI preset export",
            lambda job: read_swarm_setup_file(
                path, should_cancel=lambda: job.cancel_requested,
            ), resource="io", key=f"swarm-setup-file:{id(self)}",
        )
        self._source_task = task
        task.signals.complete.connect(lambda review, active=task: self._read_complete(active, review))
        task.signals.failed.connect(lambda error, active=task: self._read_failed(active, str(error)))
        task.signals.cancelled.connect(lambda active=task: self._read_failed(active, "Reading stopped."))
        try:
            task.start()
        except Exception as exc:
            self._read_failed(task, str(exc) or "The file read could not start.")

    def _read_complete(self, task, review):
        if self._source_task is not task or self._closing:
            return
        self._source_task = None
        self._source_review = review
        self.source_resource_group.hide()
        self.no_resource_refs.show()
        self.no_resource_refs.setText(
            "Choose and use a source preset first. If it names a main model or VAE, "
            "you can match an exact registered copy here; matching is optional."
        )
        self.source_path.setText(str(review.path))
        self.file_status.setText(
            f"{len(review.presets):,} source presets found. Choose one to review or combine. "
            "Selected source and omissions are retained locally with a saved setup, but not included in native setup export."
        )
        self.preset_choice.blockSignals(True)
        self.preset_choice.clear()
        self.preset_choice.addItem("Choose one preset…", None)
        for preset in review.presets:
            self.preset_choice.addItem(preset.title, preset.key)
        self.preset_choice.setCurrentIndex(0)
        self.preset_choice.blockSignals(False)
        self.preset_choice.show()
        self.source_summary.hide()
        self.details_toggle.hide()
        self.use_preset_button.hide()
        available = {preset.key for preset in review.presets}
        omitted = [key for key in self._composition_keys if key not in available]
        self._composition_keys = [key for key in self._composition_keys if key in available]
        self._composition_choices = {
            field: key for field, key in self._composition_choices.items() if key in available
        }
        self.compose_toggle.setVisible(len(review.presets) > 1)
        self._refresh_composition()
        if omitted:
            self.file_status.setText(
                "Some chosen presets are no longer in this file. Your setup edits remain; "
                "review the new source and choose its blocks again."
            )
        self.choose_file_button.setEnabled(True)
        self.refresh_file_button.setEnabled(True)
        self.stop_file_button.hide()

    def _read_failed(self, task, error):
        if self._source_task is not task or self._closing:
            return
        self._source_task = None
        self.file_status.setText(
            f"No new source review was completed: {error} Authored setup text remains."
        )
        self.task_tabs.setCurrentWidget(self.source_scroll)
        self.choose_file_button.setEnabled(True)
        self.refresh_file_button.setEnabled(self._source_review is not None)
        self.stop_file_button.hide()
        self.review_button.setEnabled(self._applied_review is not None)
        self.add_block_button.setEnabled(self._source_review is not None)
        self._refresh_composition()

    def stop_reading(self):
        if self._source_task is not None:
            self._source_task.cancel()

    def _show_selected(self, _index):
        if self._source_review is None:
            return
        key = self.preset_choice.currentData()
        if not key:
            self.source_summary.hide()
            self.details_toggle.hide()
            self.use_preset_button.hide()
            self.add_block_button.setEnabled(False)
            return
        preset = select_swarm_setup_preset(self._source_review, key)
        supported = []
        if preset.prompt_state == "literal":
            supported.append("prompt text" if preset.positive_prompt else "empty prompt text")
        if preset.negative_prompt_state == "literal":
            supported.append("negative prompt (including explicit blank)")
        supported.extend(preset.settings)
        resource_refs = inspect_swarm_setup_resource_references(self._source_review, key)
        if resource_refs and self.registry is not None:
            supported.append("source models need an exact registered choice")
        held = len(preset.held_fields)
        excerpt = (preset.positive_prompt[:120] + ("…" if len(preset.positive_prompt) > 120 else "")
                   if preset.positive_prompt else "")
        self.source_summary.setText(
            f"Ready to review: {', '.join(supported) if supported else 'no usable fields'}. "
            + (f"Prompt: {excerpt}. " if excerpt else "")
            + f"{held:,} source {'field' if held == 1 else 'fields'} held or omitted; "
            "inspect details before saving."
        )
        self.source_summary.show()
        lines = [f"Source preset: {preset.title}",
                 f"Format: {self._source_review.source_dialect}",
                 "Only literal supported values can enter this setup."]
        if preset.prompt_state == "literal":
            lines.append("Prompt (literal): " + (preset.positive_prompt or "[blank]"))
        if preset.negative_prompt_state == "literal":
            lines.append("Negative prompt (literal): " +
                         (preset.negative_prompt if preset.negative_prompt else "[explicitly blank]"))
        lines.extend(f"{key_name}: {value}" for key_name, value in preset.settings.items())
        for field in preset.held_fields:
            lines.append(f"{field.field}: {field.reason}")
            if field.value and field.field != "preview_image":
                lines.append(f"  Original value (inert): {field.value}")
        self.source_details.setPlainText("\n".join(lines))
        self.details_toggle.show()
        self.use_preset_button.setVisible(True)
        self.use_preset_button.setEnabled(bool(supported))
        self.add_block_button.setEnabled(
            self._source_task is None and key not in self._composition_keys
            and len(self._composition_keys) < 8
        )
        if len(self._composition_keys) >= 2:
            self.use_preset_button.setText("Use the combination below, or remove blocks")
            self.use_preset_button.setEnabled(False)
        elif self._applied_review is not None:
            if self._applied_keys:
                self.use_preset_button.setText("Current setup uses combined presets")
                self.use_preset_button.setEnabled(False)
            elif self._applied_key == key:
                if self._applied_review is self._source_review:
                    self.use_preset_button.setText("Source Selected; Edit Reusable Setup")
                    self.use_preset_button.setEnabled(False)
                else:
                    self.use_preset_button.setText("Accept Current Source, Keep My Edits")
            else:
                self.use_preset_button.setText("Keep current setup; start a new import to switch")
                self.use_preset_button.setEnabled(False)
        else:
            self.use_preset_button.setText("Use Selected Preset in Setup")

    def add_selected_block(self):
        key = self.preset_choice.currentData()
        if (self._source_review is None or self._source_task is not None or not key
                or key in self._composition_keys or len(self._composition_keys) >= 8):
            return
        self._composition_keys.append(key)
        self._refresh_composition(selected=len(self._composition_keys) - 1)
        self._show_selected(0)
        if len(self._composition_keys) == 2:
            scroll = self.source_scroll
            target = self.use_composition_button
            def show_next_choice():
                if isValid(scroll) and isValid(target):
                    position = target.mapTo(scroll.widget(), QPoint()).y()
                    bar = scroll.verticalScrollBar()
                    bar.setValue(min(bar.maximum(), max(0, position - scroll.viewport().height() // 2)))
            QTimer.singleShot(50, show_next_choice)

    def _move_block(self, step):
        index = self.block_list.currentRow()
        target = index + step
        if index < 0 or target < 0 or target >= len(self._composition_keys):
            return
        self._composition_keys[index], self._composition_keys[target] = (
            self._composition_keys[target], self._composition_keys[index]
        )
        self._refresh_composition(selected=target)

    def remove_selected_block(self):
        index = self.block_list.currentRow()
        if index < 0 or index >= len(self._composition_keys):
            return
        self._composition_keys.pop(index)
        self._refresh_composition(selected=min(index, len(self._composition_keys) - 1))
        self._show_selected(0)

    def _update_block_actions(self, index):
        ready = self._source_task is None and not self.save_in_progress
        self.move_block_up_button.setEnabled(ready and index > 0)
        self.move_block_down_button.setEnabled(
            ready and 0 <= index < len(self._composition_keys) - 1
        )
        self.remove_block_button.setEnabled(ready and index >= 0)

    def _choice_changed(self, field, control):
        key = control.currentData()
        if key is None:
            self._composition_choices.pop(field, None)
        else:
            self._composition_choices[field] = key
        self._refresh_composition_choices()
        self._composition_edited()

    def _composition_edited(self):
        if self._applied_review is not None and (
                (self._applied_keys and (
                    self._applied_keys != tuple(self._composition_keys)
                    or self._applied_choices != self._composition_choices))
                or (self._applied_key and len(self._composition_keys) >= 2)):
            self.source_resource_group.setEnabled(False)
            self._invalidate()
            self.status.setText(
                "The combination changed. Use Reviewed Combination in Setup before reviewing or saving. "
                "Your authored fields remain unchanged."
            )

    def _refresh_composition_choices(self):
        if self._composition_base_summary:
            unresolved = [field for field, control in self._conflict_controls.items()
                          if control.currentData() is None]
            if unresolved:
                suffix = f"Choose {len(unresolved)} differing setting values below."
            elif self._conflict_controls:
                suffix = ("Chosen: " + ", ".join(
                    f"{field.replace('_', ' ')} from {control.currentData()}"
                    for field, control in self._conflict_controls.items()
                ) + ". Review the result below before saving.")
            else:
                suffix = "No differing supported settings; review the result below."
            self.composition_summary.setText(self._composition_base_summary + suffix)
        ready = (self._source_review is not None and self._source_task is None
                 and len(self._composition_keys) >= 2 and all(
                     control.currentData() is not None
                     for control in self._conflict_controls.values()
                 ))
        self.use_composition_button.setEnabled(ready)

    def _refresh_composition(self, *, selected=None):
        self.block_list.clear()
        for index, key in enumerate(self._composition_keys, 1):
            self.block_list.addItem(f"{index}. {key}")
        if selected is not None and selected >= 0:
            self.block_list.setCurrentRow(selected)
        self._update_block_actions(self.block_list.currentRow())
        while self.conflict_form.rowCount():
            self.conflict_form.removeRow(0)
        self._conflict_controls = {}
        self._composition_base_summary = ""
        self.use_composition_button.setEnabled(False)
        if self._source_review is None or len(self._composition_keys) < 2:
            self.composition_summary.setText("Add at least two presets to compose a setup.")
            self.composition_details_toggle.setChecked(False)
            self.composition_details_toggle.hide()
            self._composition_edited()
            return
        try:
            proposal = inspect_swarm_setup_composition(
                self._source_review, tuple(self._composition_keys),
            )
        except ValueError as exc:
            self.composition_summary.setText(str(exc))
            self.composition_details_toggle.setChecked(False)
            self.composition_details_toggle.hide()
            return
        lines = []
        for index, key in enumerate(self._composition_keys, 1):
            preset = select_swarm_setup_preset(self._source_review, key)
            lines.append(f"{index}. {key}")
            lines.append("  Literal prompt: " + (preset.positive_prompt or "none")
                         if preset.prompt_state == "literal" else
                         f"  Prompt: {preset.prompt_state}; not copied")
            if preset.negative_prompt_state == "literal":
                lines.append("  Negative: " + (preset.negative_prompt or "[explicitly blank]"))
            lines.extend(f"  {field}: {value}" for field, value in preset.settings.items())
            for held in preset.held_fields:
                lines.append(f"  Held {held.field}: {held.reason}")
                if held.value and held.field != "preview_image":
                    lines.append(f"    Original value (inert): {held.value}")
        self.composition_details.setPlainText("\n".join(lines))
        self.composition_details_toggle.show()
        self._composition_choices = {
            field: key for field, key in self._composition_choices.items()
            if field in proposal["conflicts"]
            and any(option["key"] == key for option in proposal["conflicts"][field])
        }
        prompt = proposal["positive_prompt"] or "none"
        excerpt = prompt[:180] + ("…" if len(prompt) > 180 else "")
        conflicts = proposal["conflicts"]
        self._composition_base_summary = (
            f"In order, prompt text: {excerpt}. "
            f"{len(proposal['held_fields'])} source fields remain held or omitted. "
        )
        labels = {"negative_prompt": "Negative prompt", "size": "Image size",
                  "steps": "Steps", "cfg_scale": "Guidance", "sampler": "Sampler",
                  "scheduler": "Scheduler"}
        for field, options in conflicts.items():
            control = QComboBox()
            control.setAccessibleName(f"Choose source for conflicting {labels.get(field, field)}")
            control.setToolTip("Choose which selected preset supplies this value; order alone never chooses a winner.")
            control.addItem("Choose source…", None)
            for option in options:
                value = option["value"]
                display = ("blank (clears on apply)" if field == "negative_prompt" and value == ""
                           else f"{value['width']} × {value['height']}" if field == "size"
                           else str(value))
                control.addItem(f"{option['key']}: {display}", option["key"])
            chosen = self._composition_choices.get(field)
            if chosen is not None:
                control.setCurrentIndex(next(index for index in range(control.count())
                                             if control.itemData(index) == chosen))
            control.currentIndexChanged.connect(
                lambda _index, name=field, widget=control: self._choice_changed(name, widget)
            )
            self.conflict_form.addRow(labels.get(field, field), control)
            self._conflict_controls[field] = control
        self._refresh_composition_choices()
        self._composition_edited()

    def use_composed_presets(self):
        if not self.use_composition_button.isEnabled() or self._source_review is None:
            return
        keys = tuple(self._composition_keys)
        choices = dict(self._composition_choices)
        try:
            mapped = compose_swarm_setup_presets(self._source_review, keys, choices)
        except ValueError as exc:
            self.status.setText(str(exc))
            return
        same_selection = (self._applied_keys == keys and self._applied_choices == choices
                          and self._applied_review is not None)
        same_source_file = (same_selection and
                            self._applied_review.source_sha256 == self._source_review.source_sha256)
        if not same_selection:
            self.name.setText(" + ".join(keys)[:120])
            self.part_fields["other"].setText(mapped["positive_prompt"] or "")
            self.negative_choice.setChecked(mapped["negative_prompt_state"] == "literal")
            if self.negative_choice.isChecked():
                self.negative.setPlainText(mapped["negative_prompt"] or "")
            values = mapped["settings"]
            self.size_choice.setChecked("width" in values and "height" in values)
            if self.size_choice.isChecked():
                self.width_spin.setValue(values["width"])
                self.height_spin.setValue(values["height"])
            for key_name, checkbox, field in (
                ("steps", self.steps_choice, self.steps),
                ("sampler", self.sampler_choice, self.sampler),
                ("scheduler", self.scheduler_choice, self.scheduler),
                ("cfg_scale", self.guidance_choice, self.guidance),
            ):
                checkbox.setChecked(key_name in values)
                if key_name in values:
                    value = values[key_name]
                    field.setValue(value) if hasattr(field, "setValue") else field.setText(value)
        self._applied_review = self._source_review
        self._applied_key = ""
        self._applied_keys = keys
        self._applied_choices = choices
        if not same_source_file:
            for field in tuple(self._resource_choices):
                self._clear_import_resource(field)
            self._resource_source_choices.clear()
        self._show_import_resource_references()
        self.source_resource_group.setEnabled(True)
        self.review_button.setEnabled(True)
        self._invalidate()
        self.status.setText(
            "Combination copied to Reusable Setup. Review it when ready; Model Matches is optional."
            if not same_selection else
            "Current combined source accepted; your authored setup edits were kept. Review before saving."
        )
        self.task_tabs.setCurrentWidget(self.setup_scroll)
        scroll = self.setup_scroll
        QTimer.singleShot(0, lambda: scroll.ensureWidgetVisible(self.part_fields["other"])
                          if isValid(scroll) and isValid(self.part_fields["other"]) else None)

    def use_selected_preset(self):
        if self._source_review is None or self._source_task is not None:
            return
        key = self.preset_choice.currentData()
        if (not key or self._applied_keys or len(self._composition_keys) >= 2
                or (self._applied_review is not None and key != self._applied_key)):
            return
        preset = select_swarm_setup_preset(self._source_review, key)
        same_preset = self._applied_review is not None and self._applied_key == key
        same_source_file = (same_preset and
                            self._applied_review.source_sha256 == self._source_review.source_sha256)
        if not same_preset:
            self.name.setText(preset.title[:120])
            if preset.prompt_state == "literal" and preset.positive_prompt:
                self.part_fields["other"].setText(preset.positive_prompt)
            if preset.negative_prompt_state == "literal":
                self.negative_choice.setChecked(True)
                self.negative.setPlainText(preset.negative_prompt or "")
            for key_name, checkbox, field in (
                ("width", self.size_choice, self.width_spin),
                ("height", self.size_choice, self.height_spin),
                ("steps", self.steps_choice, self.steps),
                ("sampler", self.sampler_choice, self.sampler),
                ("scheduler", self.scheduler_choice, self.scheduler),
                ("cfg_scale", self.guidance_choice, self.guidance),
            ):
                if key_name in preset.settings:
                    value = preset.settings[key_name]
                    field.setValue(value) if hasattr(field, "setValue") else field.setText(value)
                    checkbox.setChecked(True)
        self._applied_review = self._source_review
        self._applied_key = preset.key
        self._applied_keys = ()
        self._applied_choices = {}
        if not same_source_file:
            for field in tuple(self._resource_choices):
                self._clear_import_resource(field)
            self._resource_source_choices.clear()
        self._show_import_resource_references()
        self.source_resource_group.setEnabled(True)
        self.review_button.setEnabled(True)
        self._invalidate()
        self.status.setText(
            "Source copied to Reusable Setup. Review it when ready; Model Matches is optional."
        )
        if same_preset:
            self.file_status.setText("Current source accepted; authored setup edits were kept.")
        self.use_preset_button.setText("Source Selected; Edit Reusable Setup")
        self.use_preset_button.setEnabled(False)
        self.task_tabs.setCurrentWidget(self.setup_scroll)
        scroll = self.setup_scroll
        QTimer.singleShot(0, lambda: scroll.ensureWidgetVisible(self.part_fields["other"])
                          if isValid(scroll) and isValid(self.part_fields["other"]) else None)

    def _show_import_resource_references(self):
        references = self._available_resource_references()
        if not references:
            self.no_resource_refs.setText(
                "The selected source has no matchable main model or VAE names. "
                "You can still choose exact components under Customize Models and Settings "
                "in Reusable Setup."
            )
        for field, (row, label, choose, _clear, _role) in self._resource_reference_rows.items():
            options = [item for item in references if item["field"] == field]
            row.setVisible(bool(options))
            control = self._resource_source_controls[field]
            control.blockSignals(True)
            control.clear()
            control.addItem("Choose source preset…", None)
            if len(options) == 1:
                reference = options[0]
                self._resource_source_choices[field] = reference
                value = reference["source_value"].replace("\n", " ").replace("\r", " ")
                label.setText(f"Source {'main model' if field == 'model' else 'VAE'}: "
                              f"{value[:120]}" + ("…" if len(value) > 120 else ""))
                label.setToolTip(reference["source_value"])
                control.hide()
            elif options:
                display = "main model" if field == "model" else "VAE"
                distinct_names = {item["source_value"] for item in options}
                label.setText(
                    f"Different {display} source names. Choose one to match, or leave unmatched."
                    if len(distinct_names) > 1 else
                    f"Several presets name the same {display}. Choose one source to record, or leave unmatched."
                )
                label.setToolTip("")
                for reference in options:
                    value = reference["source_value"].replace("\n", " ").replace("\r", " ")
                    control.addItem(
                        f"{reference['preset_key']}: {value[:100]}" + ("…" if len(value) > 100 else ""),
                        (reference["preset_key"], reference["source_value"]),
                    )
                previous = self._resource_source_choices.get(field)
                selected = next((index for index, reference in enumerate(options, 1)
                                 if previous == reference), 0)
                control.setCurrentIndex(selected)
                if selected == 0:
                    self._resource_source_choices.pop(field, None)
                control.show()
            else:
                self._resource_source_choices.pop(field, None)
                control.hide()
            control.blockSignals(False)
            choose.setEnabled(self.registry is not None and field in self._resource_source_choices)
        self.source_resource_group.setVisible(bool(references))
        self.no_resource_refs.setVisible(not bool(references))
        self._update_import_resource_status()

    def _available_resource_references(self):
        if self._applied_keys:
            return inspect_swarm_setup_composition_resources(
                self._applied_review, self._applied_keys)
        if self._applied_key:
            return tuple({"preset_key": self._applied_key, **item} for item in
                         inspect_swarm_setup_resource_references(
                             self._applied_review, self._applied_key))
        return ()

    def _import_source_choice_changed(self, field):
        control = self._resource_source_controls[field]
        value = control.currentData()
        reference = next((item for item in self._available_resource_references()
                          if item["field"] == field
                          and (item["preset_key"], item["source_value"]) == value), None)
        if self._resource_source_choices.get(field) != reference:
            self._clear_import_resource(field, reset_source_choice=False)
        if reference is None:
            self._resource_source_choices.pop(field, None)
        else:
            self._resource_source_choices[field] = reference
        self._resource_reference_rows[field][2].setEnabled(
            self.registry is not None and reference is not None)
        self._update_import_resource_status()

    def _update_import_resource_status(self):
        if not hasattr(self, "source_resource_status"):
            return
        chosen = {(row["id"], row["role"], row["hashes"]["sha256"])
                  for row in self._chosen_resources()}
        active = [("main model" if field == "model" else "VAE")
                  for field, row in self._resource_choices.items()
                  if (row["id"], row["role"], row["hashes"]["sha256"]) in chosen]
        self.source_resource_status.setText(
            ("Exact choices in this setup: " + ", ".join(active) + ". ") if active else
            "No source model is matched in this setup. "
        )
        self.source_resource_status.setText(
            self.source_resource_status.text() +
            "Other source fields, including LoRA lists and weights, remain in import details."
        )

    def _choose_import_resource(self, field):
        if self.registry is None or self.save_in_progress:
            return
        reference = self._resource_source_choices.get(field)
        if reference is None:
            return
        main = next((row for row in self._chosen_resources() if row["role"] == "checkpoint"), None)
        try:
            row = choose_registered_setup_resource(
                self, self.registry, self.jobs, reference["role"],
                main_model_resource_id=main["id"] if main else "",
                settings=self.settings, protected_model_roots=self.protected_model_roots,
            )
        except (ValueError, RuntimeError) as exc:
            self.status.setText(str(exc))
            return
        if row is None:
            return  # Cancelling the picker keeps the previous exact choice.
        self._insert_resource(row)
        self._resource_choices[field] = row
        self._update_import_resource_status()

    def _clear_import_resource(self, field, *, reset_source_choice=True):
        previous = self._resource_choices.pop(field, None)
        if previous is not None:
            for index in reversed(range(self.resource_tree.topLevelItemCount())):
                item = self.resource_tree.topLevelItem(index)
                row = item.data(0, Qt.UserRole)
                if row["id"] == previous["id"] and row["role"] == previous["role"]:
                    self.resource_tree.takeTopLevelItem(index)
                    self._invalidate()
        control = self._resource_source_controls.get(field)
        if (reset_source_choice and control is not None and control.isVisible()
                and control.count() > 1):
            control.setCurrentIndex(0)
        self._update_import_resource_status()

    def _resolved_resources(self, recipe):
        if not (self._applied_key or self._applied_keys):
            return []
        resources = recipe.get("resources", []) if isinstance(recipe, dict) else []
        result = []
        for field, row in self._resource_choices.items():
            reference = self._resource_source_choices.get(field)
            if reference is None or not any(
                    selected["id"] == row["id"] and selected["role"] == row["role"]
                    and selected["hashes"]["sha256"] == row["hashes"]["sha256"]
                    for selected in resources):
                continue
            decision = {"field": field, "source_value": reference["source_value"],
                        "role": row["role"], "resource_id": row["id"],
                        "sha256": row["hashes"]["sha256"]}
            if self._applied_keys:
                decision["preset_key"] = reference["preset_key"]
            result.append(decision)
        return result

    def _invalidate(self, *_args):
        super()._invalidate(*_args)
        if hasattr(self, "source_resource_group"):
            self._update_import_resource_status()

    def _check_import_source(self, *, should_cancel=None):
        if self._applied_review is None:
            raise ValueError("Choose source presets before saving.")
        recheck_swarm_setup_file(self._applied_review, should_cancel=should_cancel)

    def _save_failed(self, task, error):
        active = self._task is task
        super()._save_failed(task, error)
        if active and error.startswith("SwarmUI preset file changed after review"):
            self.task_tabs.setCurrentWidget(self.source_scroll)
            self.save_button.setEnabled(False)
            self.status.setText(
                "The source file changed. Refresh This File and accept its source again; "
                "your edited setup remains."
            )

    def _publish_reviewed_recipe(self, recipe, operation_id):
        evidence = (composed_swarm_setup_evidence(
            self._applied_review, self._applied_keys, self._applied_choices,
            resolved_resources=self._resolved_resources(recipe),
        ) if self._applied_keys else
            selected_swarm_setup_evidence(
                self._applied_review, self._applied_key,
                resolved_resources=self._resolved_resources(recipe)))
        return save_named_recipe(self.storage_root, recipe, operation_id=operation_id,
                                 import_evidence=evidence)

    def save_reviewed(self):
        if self._applied_review is None or not (self._applied_key or self._applied_keys):
            return
        marker = (self._applied_review.source_sha256,
                  self._applied_review.source_identity, self._applied_key,
                  self._applied_keys, tuple(sorted(self._applied_choices.items())),
                  tuple(sorted((row["field"], row.get("preset_key", ""), row["source_value"],
                                row["resource_id"], row["sha256"])
                               for row in self._resolved_resources(self.recipe))))
        if marker != self._operation_source_marker:
            self._operation_recipe = None
            self._operation_source_marker = marker
        super().save_reviewed()

    def review(self):
        if self._applied_review is None or self._source_task is not None:
            self.status.setText("Choose a source preset or review a combination before this setup.")
            return
        if self._applied_review is not self._source_review:
            self.status.setText("This file was refreshed. Accept its current source review while keeping your edits before reviewing the setup.")
            return
        if ((self._applied_keys and (
                self._applied_keys != tuple(self._composition_keys)
                or self._applied_choices != self._composition_choices))
                or (self._applied_key and len(self._composition_keys) >= 2)):
            self.status.setText("Use Reviewed Combination in Setup before reviewing its changed order or settings.")
            return
        super().review()

    def reject(self):
        self._closing = True
        self.stop_reading()
        super().reject()
