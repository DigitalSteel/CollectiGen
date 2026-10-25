"""Local authored prompt presets and wildcard preview for a generation draft."""
from __future__ import annotations

from copy import deepcopy
import json
from pathlib import Path
import re

from PySide6.QtCore import Qt, Signal, QSignalBlocker
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDialogButtonBox, QFileDialog,
    QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QListWidget,
    QMessageBox, QPlainTextEdit, QPushButton, QSplitter, QTabWidget, QVBoxLayout, QWidget)

from core.external_write_transaction import write_verified_json
from core.generation_prompt_templates import (PromptPreset, SETTING_KEYS,
    preview_preset_application, resolve_prompts)
from core.training_source_io import _assert_unredirected
from .tooltips import wrapped_tooltip


_SETTING_LABELS = {"seed": "Seed", "steps": "Steps", "cfg_scale": "Guidance",
                   "width": "Width", "height": "Height", "sampler": "Sampler",
                   "scheduler": "Scheduler", "denoising_strength": "Change strength"}


class GenerationPromptTemplatesDialog(QDialog):
    """Return a reviewed request/context only after Preview then Apply."""

    background_work_finished = Signal()

    def __init__(self, current_request: dict, *, initial_context: dict | None = None, parent=None,
                 jobs=None, storage_root=None, collection=None, swarm_import_service=None,
                 settings=None, settings_path=None):
        super().__init__(parent)
        if not isinstance(current_request, dict) or not isinstance(initial_context, (dict, type(None))):
            raise ValueError("Provide a generation draft and optional prompt context.")
        self.current_request = deepcopy(current_request)
        self.initial_context = deepcopy(initial_context or {})
        self.jobs, self.storage_root = jobs, storage_root
        self.settings, self.settings_path = settings, settings_path
        self._editor_baseline = None
        self.collection, self.swarm_import_service = collection, swarm_import_service
        if self.collection is None and storage_root is not None:
            from core.generation_saved_work import PromptPresetCollection
            self.collection = PromptPresetCollection(storage_root)
        self._task = self._child = None
        self._closing = False
        self._close_parent = None
        self._collection_item_id = self._collection_digest = None
        self._collection_loaded_preset = None
        self._editing_schema_version = 1
        self._editing_preset = None
        self.collection_saved = False
        self.presets = {}
        for value in self.initial_context.get("presets", ()):
            preset = PromptPreset.from_dict(value)
            if preset.preset_id in self.presets:
                raise ValueError("Prompt context repeats a preset identifier.")
            self.presets[preset.preset_id] = preset
        self.catalog = deepcopy(self.initial_context.get("catalog", {}))
        if not isinstance(self.catalog, dict):
            raise ValueError("Prompt wildcard catalog must be a mapping.")
        self._editing_settings = self._current_settings()
        self._editing_extra_fields = {}
        self._selected_wildcard = None
        self._preview_payload = None
        self._building = True
        self.setWindowTitle("Prompt Presets and Wildcards")
        self.resize(1260, 820)
        self.setMinimumSize(920, 620)
        self._build_ui()
        self._fill_presets()
        self._fill_wildcards()
        self.new_preset()
        self._building = False
        self.invalidate_preview()
        self.workflow_tabs.setCurrentIndex(0)
        self._update_io_actions()

    def _current_settings(self):
        return {key: deepcopy(value) for key, value in self.current_request.items()
                if key in SETTING_KEYS and value is not None}

    def _build_ui(self):
        outer = QVBoxLayout(self)
        intro = QLabel("Choose a saved prompt or write one here. Preview every prompt and setting change before using it in this draft. Wildcards resolve locally with a recorded seed.")
        intro.setWordWrap(True)
        outer.addWidget(intro)
        from .prompt_preset_browser import PromptPresetBrowser
        self.workflow_tabs = QTabWidget()
        browse_page = QWidget(); browse_layout = QVBoxLayout(browse_page)
        self.preset_browser = PromptPresetBrowser(settings=self.settings, settings_path=self.settings_path)
        self.preset_list = self.preset_browser.browser.cards
        browse_layout.addWidget(self.preset_browser, 1)
        browse_actions = QGridLayout()
        self.new_button = QPushButton("Create New from Draft")
        self.new_button.clicked.connect(self._request_new_preset)
        self.save_button = QPushButton("Save in This Draft")
        self.save_button.clicked.connect(self.save_preset)
        self.open_collection_button = QPushButton("Open Collection…")
        self.open_collection_button.clicked.connect(self.open_collection)
        self.save_collection_button = QPushButton("Save to Collection…")
        self.save_collection_button.clicked.connect(self.save_to_collection)
        self.swarm_import_button = QPushButton("Import from SwarmUI…")
        self.swarm_import_button.clicked.connect(self.import_from_swarm)
        self.open_collection_button.setEnabled(self.collection is not None and self.jobs is not None)
        self.save_collection_button.setEnabled(self.collection is not None and self.jobs is not None)
        self.swarm_import_button.setEnabled(self.swarm_import_service is not None and self.jobs is not None)
        self.import_button = QPushButton("Import Local JSON…")
        self.import_button.clicked.connect(self.import_json)
        self.export_button = QPushButton("Export Local JSON…")
        self.export_button.clicked.connect(self.export_json)
        self.open_preset_button = QPushButton("Open Selected in Editor")
        self.open_preset_button.clicked.connect(self._open_selected_preset)
        self.preset_browser.openRequested.connect(self._open_selected_preset)
        self.preset_browser.selectionChanged.connect(self._browse_selection_changed)
        for index, button in enumerate((self.new_button, self.open_collection_button, self.swarm_import_button,
                                         self.import_button, self.export_button, self.open_preset_button)):
            browse_actions.addWidget(button, index // 3, index % 3)
        browse_layout.addLayout(browse_actions)
        self.workflow_tabs.addTab(browse_page, "Draft Presets")
        editor_page = QWidget(); editor_layout = QVBoxLayout(editor_page)
        editor_actions = QHBoxLayout()
        self.editing_label = QLabel("Editing: New preset from this draft")
        self.editing_label.setTextFormat(Qt.PlainText)
        editor_actions.addWidget(self.editing_label, 1)
        editor_actions.addWidget(self.save_button); editor_actions.addWidget(self.save_collection_button)
        editor_layout.addLayout(editor_actions)
        self.tabs = QTabWidget()
        self.tabs.addTab(self._preset_editor(), "Prompt and settings")
        self.tabs.addTab(self._wildcard_editor(), "Wildcards")
        editor_layout.addWidget(self.tabs, 1)
        self.workflow_tabs.addTab(editor_page, "Edit Preset")
        outer.addWidget(self.workflow_tabs, 1)
        controls = QHBoxLayout()
        controls.addWidget(QLabel("Wildcard seed"))
        self.seed = QLineEdit(str(self.current_request.get("seed") if self.current_request.get("seed") is not None else 0))
        self.seed.setAccessibleName("Wildcard resolution seed")
        self.seed.setMaximumWidth(190)
        self.seed.textChanged.connect(self.invalidate_preview)
        controls.addWidget(self.seed)
        self.preview_button = QPushButton("Preview Exact Changes")
        self.preview_button.clicked.connect(self.preview_changes)
        controls.addWidget(self.preview_button)
        controls.addStretch(1)
        editor_layout.addLayout(controls)
        self.preview_text = QPlainTextEdit()
        self.preview_text.setReadOnly(True)
        self.preview_text.setAccessibleName("Exact prompt and settings preview")
        self.preview_text.setPlaceholderText("Preview the authored and resolved prompts before applying.")
        self.preview_details = QPlainTextEdit()
        self.preview_details.setReadOnly(True)
        self.preview_details.setAccessibleName("Technical preview details")
        self.preview_tabs = QTabWidget()
        self.preview_tabs.addTab(self.preview_text, "Changes to review")
        self.preview_tabs.addTab(self.preview_details, "Technical details")
        self.preview_tabs.setMaximumHeight(215)
        editor_layout.addWidget(self.preview_tabs)
        self.status = QLabel("Edit the draft, then Preview Exact Changes. Apply only uses that preview.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        outer.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.apply_button = buttons.addButton("Apply Preview to Draft", QDialogButtonBox.AcceptRole)
        self.apply_button.setEnabled(False)
        self.apply_button.clicked.connect(self.apply_preview)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self.workflow_tabs.currentChanged.connect(lambda _index: self._update_io_actions())

    def _preset_editor(self):
        panel = QWidget()
        layout = QVBoxLayout(panel)
        form = QFormLayout()
        self.preset_id = QLineEdit()
        self.preset_id.setPlaceholderText("portrait_evening")
        self.preset_name = QLineEdit()
        self.preset_name.setPlaceholderText("Evening portrait")
        form.addRow("Preset ID", self.preset_id)
        form.addRow("Name", self.preset_name)
        self.preset_id.textChanged.connect(self.invalidate_preview)
        self.preset_name.textChanged.connect(self.invalidate_preview)
        layout.addLayout(form)
        prompts = QHBoxLayout()
        for title, attribute in (("Positive prompt", "positive_prompt"),
                                 ("Negative prompt", "negative_prompt")):
            column = QVBoxLayout()
            column.addWidget(QLabel(title))
            editor = QPlainTextEdit()
            editor.setAccessibleName(f"Preset {title.lower()}")
            editor.setMaximumHeight(135)
            editor.textChanged.connect(self.invalidate_preview)
            setattr(self, attribute, editor)
            column.addWidget(editor)
            mode = QComboBox()
            for label, value in (("Keep current", "keep"), ("Append to current", "append"), ("Replace current", "replace")):
                mode.addItem(label, value)
            mode.setAccessibleName(f"How to apply {title.lower()}")
            mode.currentIndexChanged.connect(self.invalidate_preview)
            setattr(self, attribute.replace("prompt", "mode"), mode)
            column.addWidget(mode)
            prompts.addLayout(column, 1)
        layout.addLayout(prompts)
        layout.addWidget(QLabel("Use preset settings only where checked. Current model binding stays unchanged."))
        grid = QGridLayout()
        self.setting_checks = {}
        for index, key in enumerate(_SETTING_LABELS):
            checkbox = QCheckBox(_SETTING_LABELS[key])
            checkbox.setAccessibleName(f"Apply preset {key}")
            checkbox.toggled.connect(self.invalidate_preview)
            grid.addWidget(checkbox, index // 4, index % 4)
            self.setting_checks[key] = checkbox
        layout.addLayout(grid)
        self.held_fields = QPlainTextEdit(); self.held_fields.setReadOnly(True); self.held_fields.setMaximumHeight(65)
        self.held_fields.setAccessibleName("Unsupported source fields omitted from application")
        self.held_fields.setVisible(False)
        layout.addWidget(self.held_fields)
        self.acknowledge_omissions = QCheckBox("Apply only the supported fields; leave the listed unsupported fields out")
        self.acknowledge_omissions.toggled.connect(self.invalidate_preview)
        self.acknowledge_omissions.setVisible(False); layout.addWidget(self.acknowledge_omissions)
        layout.addStretch(1)
        return panel

    def _wildcard_editor(self):
        panel = QWidget()
        layout = QHBoxLayout(panel)
        left = QVBoxLayout()
        left.addWidget(QLabel("Named wildcards"))
        self.wildcard_list = QListWidget()
        self.wildcard_list.setAccessibleName("Named wildcard choices")
        self.wildcard_list.currentRowChanged.connect(self._select_wildcard)
        left.addWidget(self.wildcard_list, 1)
        self.new_wildcard_button = QPushButton("New Wildcard")
        self.new_wildcard_button.clicked.connect(self.new_wildcard)
        left.addWidget(self.new_wildcard_button)
        layout.addLayout(left, 1)
        right = QVBoxLayout()
        hint = QLabel("Use <wildcard:name> in either prompt. Put one possible replacement on each line. Preview records every chosen value.")
        hint.setWordWrap(True)
        right.addWidget(hint)
        right.addWidget(QLabel("Wildcard name"))
        self.wildcard_name = QLineEdit()
        self.wildcard_name.setPlaceholderText("lighting")
        self.wildcard_name.textChanged.connect(self.invalidate_preview)
        right.addWidget(self.wildcard_name)
        right.addWidget(QLabel("Choices · one per line"))
        self.wildcard_choices = QPlainTextEdit()
        self.wildcard_choices.setAccessibleName("Wildcard replacement choices")
        self.wildcard_choices.textChanged.connect(self.invalidate_preview)
        right.addWidget(self.wildcard_choices, 1)
        self.save_wildcard_button = QPushButton("Save Wildcard in Draft")
        self.save_wildcard_button.clicked.connect(self.save_wildcard)
        right.addWidget(self.save_wildcard_button)
        layout.addLayout(right, 2)
        return panel

    def invalidate_preview(self, *_):
        if getattr(self, "_building", False):
            return
        self._preview_payload = None
        if hasattr(self, "apply_button"):
            self.apply_button.setEnabled(False)
        if hasattr(self, "preview_text"):
            self.preview_text.clear()
            self.preview_details.clear()
        if hasattr(self, "status"):
            self.status.setText("Draft changed. Preview Exact Changes again before applying.")

    def _fill_presets(self):
        from .prompt_preset_browser import draft_preset_descriptor
        self.preset_browser.set_rows(tuple(draft_preset_descriptor(item) for item in
            sorted(self.presets.values(), key=lambda value: (value.name.casefold(), value.preset_id))))

    def _browse_selection_changed(self):
        self.open_preset_button.setEnabled(self._io_idle() and self.preset_browser.selected_descriptor() is not None)

    def _editor_values(self):
        return (self.preset_id.text(), self.preset_name.text(), self.positive_prompt.toPlainText(), self.negative_prompt.toPlainText(),
                self.positive_mode.currentData(), self.negative_mode.currentData(),
                tuple(key for key, check in self.setting_checks.items() if check.isChecked()), self.acknowledge_omissions.isChecked())

    def _allow_editor_replacement(self):
        if self._editor_baseline is None or self._editor_values() == self._editor_baseline:
            return True
        return QMessageBox.question(self, "Keep your preset edits?",
            "This editor has changed preset text or application choices. Cancel to keep editing, or discard these choices to open another preset.",
            QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel) == QMessageBox.Discard

    def _open_selected_preset(self):
        selected = self.preset_browser.selected_descriptor()
        if not self._io_idle() or selected is None or not self._allow_editor_replacement(): return
        self._load_preset(selected.resource_id)

    def _request_new_preset(self):
        if not self._io_idle() or not self._allow_editor_replacement(): return
        self.new_preset(); self.workflow_tabs.setCurrentIndex(1)

    def _load_preset(self, identity):
        preset = self.presets[identity]
        if self._collection_loaded_preset is not None and self._collection_loaded_preset.preset_id != preset.preset_id:
            self._collection_item_id = self._collection_digest = None
            self._collection_loaded_preset = None
        self._building = True
        self.preset_id.setText(preset.preset_id)
        self.preset_name.setText(preset.name)
        self.positive_prompt.setPlainText(preset.positive_prompt)
        self.negative_prompt.setPlainText(preset.negative_prompt)
        self._editing_settings = deepcopy(preset.settings)
        self._editing_extra_fields = deepcopy(preset.extra_fields)
        self._editing_schema_version = preset.schema_version
        self._editing_preset = preset
        self._update_settings()
        self._update_import_modes(preset)
        self._building = False
        self._editor_baseline = self._editor_values()
        self.editing_label.setText("Editing: " + preset.name[:100])
        self.editing_label.setToolTip(wrapped_tooltip(preset.name))
        self.workflow_tabs.setCurrentIndex(1)
        self.invalidate_preview()

    def _update_settings(self):
        for key, checkbox in self.setting_checks.items():
            available = key in self._editing_settings
            checkbox.setEnabled(available)
            checkbox.setChecked(False)
            checkbox.setText(f"{_SETTING_LABELS[key]}: {self._editing_settings[key]}" if available else _SETTING_LABELS[key])

    def new_preset(self):
        self._building = True
        self.preset_browser.clear_selection()
        self.preset_id.clear()
        self.preset_name.clear()
        self.positive_prompt.setPlainText(str(self.initial_context.get("authored_positive_prompt",
                                               self.current_request.get("positive_prompt", ""))))
        self.negative_prompt.setPlainText(str(self.initial_context.get("authored_negative_prompt",
                                               self.current_request.get("negative_prompt", ""))))
        self._editing_settings = self._current_settings()
        self._editing_extra_fields = {}
        self._editing_schema_version = 1
        self._editing_preset = None
        self._collection_item_id = self._collection_digest = None
        self._collection_loaded_preset = None
        self._update_settings()
        self._update_import_modes(None)
        self._building = False
        self._editor_baseline = self._editor_values()
        self.editing_label.setText("Editing: New preset from this draft")
        self.editing_label.setToolTip("")
        self.invalidate_preview()

    def _draft_preset(self):
        if self._editing_schema_version not in {1, 2} and self._editing_preset is not None:
            return self._editing_preset
        name = self.preset_name.text().strip() or "Current draft"
        identifier = self.preset_id.text().strip() or re.sub(r"[^a-z0-9_.-]+", "_", name.casefold()).strip("_") or "current_draft"
        return PromptPreset.from_dict({**deepcopy(self._editing_extra_fields),
            "schema_version": self._editing_schema_version, "preset_id": identifier,
            "name": name, "positive_prompt": self.positive_prompt.toPlainText(),
            "negative_prompt": self.negative_prompt.toPlainText(),
            "settings": deepcopy(self._editing_settings)})

    def save_preset(self):
        try:
            positive_mode, negative_mode = self.positive_mode.currentData(), self.negative_mode.currentData()
            selected_settings = tuple(key for key, check in self.setting_checks.items() if check.isChecked())
            omissions = self.acknowledge_omissions.isChecked()
            preset = self._draft_preset()
            # This command deliberately captures the current request's scalar settings.
            if preset.schema_version == 1:
                preset = PromptPreset.from_dict({**preset.to_dict(), "settings": self._current_settings()})
            self.presets[preset.preset_id] = preset
            self._fill_presets()
            self.preset_browser.select_resource(preset.preset_id)
            self._load_preset(preset.preset_id)
            for combo, mode in ((self.positive_mode, positive_mode), (self.negative_mode, negative_mode)):
                if combo.findData(mode) >= 0: combo.setCurrentIndex(combo.findData(mode))
            for key in selected_settings:
                if self.setting_checks[key].isEnabled(): self.setting_checks[key].setChecked(True)
            self.acknowledge_omissions.setChecked(omissions)
            self.invalidate_preview()
            self.status.setText(f"Saved {preset.name} in this draft. Preview before applying it.")
        except ValueError as exc:
            self.invalidate_preview()
            self.status.setText(str(exc))

    def _fill_wildcards(self):
        self.wildcard_list.blockSignals(True)
        self.wildcard_list.clear()
        self.wildcard_list.addItems(sorted(self.catalog))
        self.wildcard_list.blockSignals(False)

    def _saved_bundle(self):
        catalog = self._draft_catalog()
        resolve_prompts("", "", catalog, seed=0)  # Validate the complete catalog.
        return {"schema_version": 1,
                "presets": [item.to_dict() for item in self.presets.values()],
                "catalog": catalog}

    def export_json(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export Prompt Presets and Wildcards",
                                              "prompt-presets.json", "JSON (*.json)")
        if not path:
            return
        try:
            target = Path(path).absolute()
            _assert_unredirected(target)
            if target.exists() and QMessageBox.question(
                    self, "Replace Existing Export?",
                    f"Replace the selected local JSON file?\n{target}",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
                self.status.setText("Export cancelled; the existing file was not changed.")
                return
            bundle = self._saved_bundle()
            _assert_unredirected(target)
            write_verified_json(target, bundle, label="Prompt preset export")
            self.status.setText("Prompt presets and wildcards exported with read-back verification.")
        except (OSError, RuntimeError, ValueError) as exc:
            self.status.setText(f"Export was not completed: {exc}")

    def import_json(self):
        if not self._allow_editor_replacement(): return
        path, _ = QFileDialog.getOpenFileName(self, "Import Prompt Presets and Wildcards",
                                              "", "JSON (*.json)")
        if not path:
            return
        try:
            source = Path(path).absolute()
            _assert_unredirected(source)
            if source.stat().st_size > 2 * 1024 * 1024:
                raise ValueError("The selected prompt JSON exceeds the 2 MiB import limit.")
            with source.open("rb") as handle:
                raw = handle.read(2 * 1024 * 1024 + 1)
            _assert_unredirected(source)
            if len(raw) > 2 * 1024 * 1024:
                raise ValueError("The selected prompt JSON exceeds the 2 MiB import limit.")
            bundle = json.loads(raw)
            if (not isinstance(bundle, dict) or bundle.get("schema_version") != 1
                    or not isinstance(bundle.get("presets"), list)
                    or len(bundle["presets"]) > 256
                    or not isinstance(bundle.get("catalog"), dict)):
                raise ValueError("The selected file is not a supported prompt bundle.")
            imported = {}
            for value in bundle["presets"]:
                preset = PromptPreset.from_dict(value)
                if preset.preset_id in imported:
                    raise ValueError("The selected file repeats a preset identifier.")
                imported[preset.preset_id] = preset
            resolve_prompts("", "", bundle["catalog"], seed=0)
            self.presets = imported
            self.catalog = deepcopy(bundle["catalog"])
            self._fill_presets()
            self._fill_wildcards()
            self.new_preset()
            self.new_wildcard()
            self.status.setText("Imported prompt presets and wildcards into this draft. Preview before applying.")
        except (OSError, RuntimeError, ValueError, UnicodeError) as exc:
            self.status.setText(f"Import was not completed: {exc}")

    def _select_wildcard(self, row):
        if row < 0:
            return
        name = self.wildcard_list.item(row).text()
        self._selected_wildcard = name
        self._building = True
        self.wildcard_name.setText(name)
        self.wildcard_choices.setPlainText("\n".join(self.catalog[name]))
        self._building = False
        self.invalidate_preview()

    def new_wildcard(self):
        self._selected_wildcard = None
        self._building = True
        self.wildcard_list.clearSelection()
        self.wildcard_name.clear()
        self.wildcard_choices.clear()
        self._building = False
        self.invalidate_preview()

    def _draft_catalog(self):
        catalog = deepcopy(self.catalog)
        name = self.wildcard_name.text().strip()
        choices_text = self.wildcard_choices.toPlainText()
        if name or choices_text:
            if self._selected_wildcard and self._selected_wildcard != name:
                catalog.pop(self._selected_wildcard, None)
            catalog[name] = choices_text.splitlines()
        return catalog

    def save_wildcard(self):
        try:
            catalog = self._draft_catalog()
            resolve_prompts("", "", catalog, seed=0)  # Validate without sampling.
            self.catalog = catalog
            self._fill_wildcards()
            name = self.wildcard_name.text().strip()
            self._selected_wildcard = name
            self.wildcard_list.setCurrentRow(sorted(self.catalog).index(name))
            self.invalidate_preview()
            self.status.setText(f"Saved wildcard {name} in this draft. Preview before applying it.")
        except ValueError as exc:
            self.invalidate_preview()
            self.status.setText(str(exc))

    def preview_changes(self):
        self.workflow_tabs.setCurrentIndex(1)
        self._preview_payload = None
        self.apply_button.setEnabled(False)
        try:
            preset = self._draft_preset()
            seed = int(self.seed.text().strip())
            preview = preview_preset_application(preset, self.current_request,
                positive_mode=self.positive_mode.currentData(),
                negative_mode=self.negative_mode.currentData(),
                setting_keys=tuple(key for key, checkbox in self.setting_checks.items()
                                   if checkbox.isChecked()), acknowledged_omissions=self.acknowledge_omissions.isChecked())
            catalog = self._draft_catalog()
            resolution = resolve_prompts(preview["authored_positive_prompt"],
                preview["authored_negative_prompt"], catalog, seed=seed, preset=preset)
            request = deepcopy(preview["proposed_request"])
            request["positive_prompt"] = resolution["resolved_positive_prompt"]
            request["negative_prompt"] = resolution["resolved_negative_prompt"]
            context = deepcopy(self.initial_context)
            snapshots = {**self.presets, preset.preset_id: preset}
            context.update(presets=[item.to_dict() for item in snapshots.values()],
                           catalog=catalog, **resolution,
                           preset_id=preset.preset_id,
                           preset_application={"positive_mode": self.positive_mode.currentData(),
                               "negative_mode": self.negative_mode.currentData(),
                               "setting_keys": [key for key, checkbox in self.setting_checks.items() if checkbox.isChecked()],
                               "omitted_fields": list(preview.get("omitted_fields", ())),
                               "acknowledged_omissions": self.acknowledge_omissions.isChecked()})
            self._preview_payload = {"request": request, "context": context}
            before = {key: self.current_request.get(key) for key in
                      ("positive_prompt", "negative_prompt", *_SETTING_LABELS)}
            after = {key: request.get(key) for key in before}
            details = {"resolved_after": after,
                       "authored_after": {"positive_prompt": resolution["authored_positive_prompt"],
                                          "negative_prompt": resolution["authored_negative_prompt"]},
                       "before": before, "setting_changes": preview["changes"],
                       "wildcard_seed": seed, "resolved_tokens": resolution["resolved_tokens"],
                       "omitted_fields": list(preview.get("omitted_fields", ()))}
            lines = []
            for key, label, mode in (("positive_prompt", "Positive prompt", self.positive_mode),
                                     ("negative_prompt", "Negative prompt", self.negative_mode)):
                lines.extend((f"{label} · {mode.currentText()}",
                              "Before:", before[key] or "(empty)",
                              "After:", after[key] or "(empty)", ""))
            lines.append("Selected settings")
            selected = context["preset_application"]["setting_keys"]
            for key in selected:
                old = before[key] if before[key] is not None else "Not set"
                new = after[key] if after[key] is not None else "Not set"
                lines.append(f"{_SETTING_LABELS[key]}: {old} → {new}")
            if not selected:
                lines.append("None. Current settings stay unchanged.")
            lines.extend(("", "Omitted source fields"))
            lines.extend(details["omitted_fields"] or ["None."])
            lines.extend(("", "Wildcard choices"))
            if resolution.get("catalog_applied") is False:
                lines.append("No wildcard expansion. Imported prompt text is preserved exactly.")
            else:
                lines.append(f"Seed: {seed}")
                for token in resolution["resolved_tokens"]:
                    lines.extend((f"{token['name']} · choice {token['choice_index'] + 1}:", token["choice"]))
                if not resolution["resolved_tokens"]:
                    lines.append("No wildcard choices used.")
            self.preview_text.setPlainText("\n".join(lines))
            self.preview_details.setPlainText(json.dumps(details, ensure_ascii=False, indent=2))
            self.preview_tabs.setCurrentIndex(0)
            self.status.setText("Exact prompt and setting preview is ready. Apply uses this reviewed state only.")
            self.apply_button.setEnabled(True)
        except (ValueError, TypeError) as exc:
            self.preview_text.clear()
            self.preview_details.clear()
            self.status.setText(str(exc))

    def apply_preview(self):
        if self._preview_payload is not None and self.apply_button.isEnabled():
            self.accept()

    def result_payload(self):
        return deepcopy(self._preview_payload) if self.result() == QDialog.Accepted else None

    def _update_import_modes(self, preset):
        from core.generation_prompt_templates import preset_application_options
        future = preset is not None and preset.schema_version not in {1, 2}
        options = ({"positive_prompt_modes": ("keep",), "negative_prompt_modes": ("keep",),
            "held_fields": (f"Preset version {preset.schema_version} is retained but cannot be applied.",)} if future
            else preset_application_options(preset)) if preset is not None else {
            "positive_prompt_modes": ("keep", "append", "replace"),
            "negative_prompt_modes": ("keep", "append", "replace"), "held_fields": ()}
        labels = {"keep": "Keep current", "append": "Append to current", "replace": "Replace current", "compose": "Compose with current prompt"}
        for side in ("positive", "negative"):
            combo = getattr(self, side + "_mode")
            prior = combo.currentData()
            modes = options[side + "_prompt_modes"]
            with QSignalBlocker(combo):
                combo.clear()
                for mode in modes: combo.addItem(labels[mode], mode)
                combo.setCurrentIndex(max(0, combo.findData(prior)))
            getattr(self, side + "_prompt").setReadOnly(future or preset is not None and preset.schema_version == 2 and modes == ("keep",))
        self.preset_id.setReadOnly(future or preset is not None and preset.schema_version == 2)
        self.preset_name.setReadOnly(future)
        self.preview_button.setEnabled(not future)
        if future:
            for checkbox in self.setting_checks.values(): checkbox.setEnabled(False)
        held = options["held_fields"]
        self.held_fields.setPlainText("Unsupported source fields retained as evidence:\n" + "\n".join(held) if held else "")
        self.held_fields.setVisible(bool(held))
        self.acknowledge_omissions.setVisible(bool(held) and not future); self.acknowledge_omissions.setChecked(False)

    def _io_idle(self): return self._task is None and self._child is None and not self._closing

    def _update_io_actions(self):
        idle = self._io_idle()
        for widget in (self.tabs, self.new_button, self.save_button, self.import_button,
                       self.export_button, self.seed, self.preview_button): widget.setEnabled(idle)
        self.preset_browser.set_busy(not idle)
        self._browse_selection_changed()
        self.preview_button.setEnabled(idle and self._editing_schema_version in {1, 2})
        for button in (self.open_collection_button, self.save_collection_button):
            button.setEnabled(idle and self.collection is not None and self.jobs is not None)
        self.swarm_import_button.setEnabled(idle and self.swarm_import_service is not None and self.jobs is not None)
        self.apply_button.setEnabled(idle and self._preview_payload is not None and self.workflow_tabs.currentIndex() == 1)

    def _track_child(self, child, completed):
        if not self._io_idle(): raise ValueError("Finish the current preset action first.")
        self._child = child
        def finished(result):
            if self._child is not child: return
            self._child = None
            if self._closing: self._finish_close(); return
            self._update_io_actions()
            completed(result)
        child.finished.connect(finished)
        self._update_io_actions(); child.setWindowModality(Qt.WindowModal); child.show()

    def open_collection(self):
        if not self._io_idle() or self.collection is None or self.jobs is None: return
        from .generation_saved_work_dialog import SavedGenerationWorkDialog
        browser = SavedGenerationWorkDialog(self.jobs, self.collection.storage_root, "prompt_presets", parent=self,
            settings=self.settings, settings_path=self.settings_path)
        def opened(result):
            if result != QDialog.Accepted: return
            if not self._allow_editor_replacement(): return
            if browser.requested_action == "create": self.new_preset(); self.workflow_tabs.setCurrentIndex(1); return
            if browser.selected_path:
                item_id = Path(browser.selected_path).stem
                self._run_collection("Open prompt preset", lambda cancelled: self.collection.load(item_id),
                    lambda value: self._loaded_collection(item_id, value))
        self._track_child(browser, opened)

    def _loaded_collection(self, item_id, value):
        preset = value["preset"]
        self._collection_item_id, self._collection_digest = item_id, value["storage_digest"]
        self._collection_loaded_preset = preset
        self.presets[preset.preset_id] = preset
        self._fill_presets()
        self.preset_browser.select_resource(preset.preset_id)
        self._load_preset(preset.preset_id)
        self.status.setText("Opened an exact collection snapshot. Choose prompt behavior and settings, then preview; the current draft is unchanged.")

    @staticmethod
    def _preset_summary(preset):
        if preset is None: return "No existing collection item."
        settings = "\n".join(f"{_SETTING_LABELS.get(key, key)}: {value}" for key, value in preset.settings.items()) or "No scalar settings"
        return f"Name: {preset.name}\nPositive: {preset.positive_prompt}\nNegative: {preset.negative_prompt}\n{settings}"

    def save_to_collection(self):
        if not self._io_idle() or self.collection is None or self.jobs is None: return
        try: preset = self._draft_preset()
        except (ValueError, TypeError) as exc: self.status.setText(str(exc)); return
        review_dialog = QDialog(self); review_dialog.setWindowTitle("Save Prompt Preset to Collection")
        review_dialog.resize(880, 640)
        layout = QVBoxLayout(review_dialog)
        note = QLabel("This saves a reusable collection item. It does not apply changes to the current generation draft.")
        note.setWordWrap(True); layout.addWidget(note)
        action = QComboBox()
        if self._collection_item_id:
            action.addItem("Keep the collection item unchanged", "keep_local")
            action.addItem("Update this exact collection item", "update")
            action.addItem("Save a separate copy", "copy")
        else:
            action.addItem("Create a new collection item", "create")
            action.addItem("Save a separate copy with a new ID", "copy")
        layout.addWidget(action)
        text = QPlainTextEdit(); text.setReadOnly(True); text.setAccessibleName("Collection before and after review")
        layout.addWidget(text, 1)
        review_dialog.status_label = QLabel("Choose a save action, then preview its exact change.")
        review_dialog.status_label.setWordWrap(True); review_dialog.status_label.setTextFormat(Qt.PlainText)
        layout.addWidget(review_dialog.status_label)
        buttons = QHBoxLayout(); prepare = QPushButton("Preview Collection Save"); confirm = QPushButton("Confirm Collection Save")
        confirm.setEnabled(False); close = QPushButton("Back to Draft")
        for button in (prepare, confirm, close): buttons.addWidget(button)
        layout.addLayout(buttons)
        review_dialog.action, review_dialog.preview_text = action, text
        review_dialog.preview_button, review_dialog.confirm_button = prepare, confirm
        state = {"review": None}
        def invalidate(*_): state["review"] = None; confirm.setEnabled(False); text.clear()
        action.currentIndexChanged.connect(invalidate)
        def ready(review):
            state["review"] = review
            after = PromptPreset.from_dict(review["after"])
            text.setPlainText("CURRENT COLLECTION ITEM\n" + self._preset_summary(self._collection_loaded_preset if action.currentData() in {"update", "keep_local"} else None)
                + "\n\nAFTER REVIEWED SAVE\n" + self._preset_summary(after))
            confirm.setEnabled(True)
        def preview_save():
            if self._task is not None: return
            invalidate()
            selected_action = action.currentData()
            self._run_collection("Review collection save", lambda cancelled: self.collection.preview_save(preset,
                action=selected_action, item_id=self._collection_item_id,
                expected_digest=self._collection_digest), ready, child=review_dialog)
        def saved(result):
            self.collection_saved = self.collection_saved or result["state"] == "saved"
            self._collection_item_id, self._collection_digest = result["item_id"], result["storage_digest"]
            self._collection_loaded_preset = PromptPreset.from_dict(state["review"]["after"])
            self.status.setText("Collection save completed. Cancelling this editor will keep the saved collection item and leave the generation draft unchanged.")
            review_dialog.accept()
        def save():
            if state["review"] is None or self._task is not None: return
            if self._draft_preset().content_sha256 != preset.content_sha256:
                invalidate(); self.status.setText("The editor changed; close this review and prepare another collection save."); return
            confirm.setEnabled(False)
            self._run_collection("Save reviewed collection item", lambda cancelled: self.collection.apply(state["review"], confirmed=True, cancelled=cancelled), saved, child=review_dialog)
        prepare.clicked.connect(preview_save); confirm.clicked.connect(save)
        close.clicked.connect(lambda: review_dialog.reject() if self._task is None else None)
        self._track_child(review_dialog, lambda result: None)

    def import_from_swarm(self):
        if not self._io_idle() or self.swarm_import_service is None or self.jobs is None: return
        from .swarm_preset_import_dialog import SwarmPresetImportDialog
        child = SwarmPresetImportDialog(self.jobs, service=self.swarm_import_service, parent=self)
        imported = []
        child.imported.connect(imported.append)
        self._track_child(child, lambda result: self.open_collection() if imported else None)

    def _run_collection(self, title, work, completed, *, child=None):
        if self._task is not None or self.jobs is None: return
        from .workers import CoordinatedCallTask
        from .job_progress import bind_job_progress
        result_holder = []
        def run(job):
            result = work(lambda: job.cancel_requested)
            result_holder.append(result)
            return result
        task = CoordinatedCallTask(self.jobs, title, run, resource="io")
        self._task = task; self.status.setText(title + "…")
        binding = bind_job_progress(self, task, title=title, unit="items")
        def finish(value=None, error=""):
            if self._task is not task: return
            self._task = None
            if child is not None: child.setEnabled(True)
            if error:
                message = error + " Open the collection to review any interrupted save."
                self.status.setText(message)
                if child is not None and hasattr(child, "status_label"): child.status_label.setText(message)
            else:
                try: completed(value)
                except (ValueError, RuntimeError, OSError) as exc: self.status.setText(str(exc))
            if self._closing: self._finish_close()
            else: self._update_io_actions()
            self.background_work_finished.emit()
        task.signals.complete.connect(lambda value: finish(value))
        task.signals.failed.connect(lambda error: finish(error=error))
        task.signals.cancelled.connect(lambda: finish(result_holder[0]) if result_holder else finish(error="Stopped safely."))
        if child is not None: child.setEnabled(False)
        self._update_io_actions()
        try: task.start()
        except Exception as exc: binding.finish(task); finish(error=str(exc))

    def has_active_background_work(self):
        return self._task is not None or bool(self._child and getattr(self._child, "has_active_background_work", lambda: False)())

    def request_background_shutdown(self):
        self.reject(); return self.has_active_background_work()

    def request_parent_close(self, continuation):
        pending = self._task is not None or self._child is not None
        self._close_parent = continuation if pending else None
        self.reject(); return not pending

    def _finish_close(self):
        if self._task is not None or self._child is not None: return
        continuation, self._close_parent = self._close_parent, None
        QDialog.reject(self)
        if continuation: continuation()

    def reject(self):
        self._closing = True
        if self._task is not None and self._task.job_id: self.jobs.cancel(self._task.job_id)
        if self._child is not None:
            self._child.reject()
        self._finish_close()

    def closeEvent(self, event):
        if self._task is not None or self._child is not None:
            self.reject(); event.ignore()
        else: super().closeEvent(event)
