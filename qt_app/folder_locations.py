"""The single Setup folder editor, embedded or opened from a workflow."""
from copy import deepcopy
from PySide6.QtCore import QEvent, QObject, Signal, Qt
from PySide6.QtWidgets import (QDialog, QDialogButtonBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QLineEdit, QPushButton, QScrollArea, QComboBox,
    QTableWidget, QHeaderView, QAbstractItemView, QVBoxLayout, QWidget, QFileDialog,
    QInputDialog, QCheckBox, QSizePolicy, QStackedWidget, QFrame, QListWidget)

from core.folder_locations import (APP_FOLDERS, IMAGE_APP_FOLDERS, ANALYSIS_APP_FOLDERS,
    RESIZE_FOLDERS, protected_roots,
    save_app_folders, save_version_folders, validate_version_folders,
    validate_model_folder_separation, save_history_folders)
from core.history_configuration import (configuration, sources, source_signature,
    stage_binding, HistoryConfigurationService)
from core.model_resource_registry import ModelResourceRegistry, RESOURCE_ROLES
from core.model_workspace import ModelWorkspace
from core.model_organization import FAMILY_DESTINATIONS, destination_path, family_destination_template
from core.paths import MODEL_RESOURCES_PATH, USER_DATA_DIR
from core.resolution_workflow import load_resolution_settings
from .components import compact_button, guard_scrollable_value_controls
from .structured_history_locations import StructuredHistoryLocationsEditor
from .model_storage_policy_editor import ModelStoragePolicyEditor


MODEL_PURPOSE_LABELS = {
    "checkpoint": "Generation checkpoint", "unet": "Diffusion model",
    "text_encoder": "Text encoder", "vae": "VAE", "lora": "LoRA adapter",
    "embedding": "Embedding", "extension": "Extension",
    "vision_encoder": "Vision encoder", "control_model": "Control model",
    "upscaler": "Upscaler", "video_companion": "Video companion",
    "tagger": "Image tagger", "captioner": "Image captioner",
}


def model_folder_summary(values, family_destinations=None):
    if not isinstance(values, dict):
        return "Review the saved model folders in Setup."
    if not values and not family_destinations:
        return "No overrides. Use the default model folder."
    rows = []
    for key, path in (family_destinations or {}).items():
        rows.append(f"{key.replace(':', ' · ')}: {path}")
    for key, path in values.items():
        role, _, family = str(key).partition(":")
        rows.append(f"{MODEL_PURPOSE_LABELS.get(role, role)} · {family or 'All families'}: {path}")
    return "\n".join(rows)


class ModelFolderOverrides(QWidget):
    """Stage the existing role[:family] mapping without requiring JSON editing."""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setAccessibleName("Model purpose and family folder overrides")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        actions = QHBoxLayout()
        self.add_button = compact_button(QPushButton("Add Folder Override…"))
        self.remove_button = compact_button(QPushButton("Remove Selected"))
        self.add_button.clicked.connect(lambda: self.add_override())
        self.remove_button.clicked.connect(self.remove_selected)
        actions.addWidget(self.add_button)
        actions.addWidget(self.remove_button)
        actions.addStretch(1)
        layout.addLayout(actions)
        self.table = QTableWidget(0, 3)
        self.table.setAccessibleName("Model folder overrides")
        self.table.setHorizontalHeaderLabels(("Model purpose", "Family (optional)", "Folder"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.verticalHeader().setDefaultSectionSize(44)
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Interactive)
        self.table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.table.setColumnWidth(0, 210)
        self.table.setColumnWidth(1, 165)
        self.table.setMinimumHeight(130)
        self.table.setMaximumHeight(270)
        guard_scrollable_value_controls(self.table)
        self.table.itemSelectionChanged.connect(self._update_actions)
        layout.addWidget(self.table)
        self.empty = QLabel("No overrides. Future model acquisitions use the default model folder.")
        self.empty.setWordWrap(True)
        layout.addWidget(self.empty)
        self._update_actions()
        self.setFocusProxy(self.add_button)

    def _update_actions(self):
        self.add_button.setEnabled(self.table.rowCount() < 100)
        self.remove_button.setEnabled(self.table.currentRow() >= 0)
        self.empty.setVisible(self.table.rowCount() == 0)

    def add_override(self, role="", family="", folder="", *, focus=True):
        if self.table.rowCount() >= 100:
            raise ValueError("Use at most 100 model folder overrides.")
        row = self.table.rowCount()
        self.table.insertRow(row)
        purpose = QComboBox()
        purpose.setAccessibleName("Model purpose")
        purpose.addItem("Choose purpose…", "")
        for key in RESOURCE_ROLES:
            purpose.addItem(MODEL_PURPOSE_LABELS.get(key, key), key)
        if role and purpose.findData(role) < 0:
            purpose.addItem("Unsupported: " + role, role)
        purpose.setCurrentIndex(max(0, purpose.findData(role)))
        family_field = QLineEdit(family)
        family_field.setAccessibleName("Optional model family")
        family_field.setPlaceholderText("All families")
        family_field.setToolTip("Leave empty for all families, or use the exact model family, such as SDXL. Family names are case-sensitive.")
        path_row = QWidget()
        path_layout = QHBoxLayout(path_row)
        path_layout.setContentsMargins(2, 0, 2, 0)
        path_field = QLineEdit(folder)
        path_field.setAccessibleName("Model override folder")
        path_field.setToolTip(folder)
        path_field.textChanged.connect(path_field.setToolTip)
        browse = compact_button(QPushButton("Browse…"))
        browse.setAccessibleName("Browse model override folder")
        browse.clicked.connect(lambda: self._browse(path_field))
        path_layout.addWidget(path_field, 1)
        path_layout.addWidget(browse)
        self.table.setCellWidget(row, 0, purpose)
        self.table.setCellWidget(row, 1, family_field)
        self.table.setCellWidget(row, 2, path_row)
        for control in (purpose, family_field, path_field):
            (control.currentIndexChanged if isinstance(control, QComboBox) else control.textChanged).connect(self.changed.emit)
        purpose.installEventFilter(self.table._scroll_wheel_guard)
        for control in (purpose, family_field, path_field, browse):
            control.installEventFilter(self)
        if focus:
            self.table.setCurrentCell(row, 0)
            purpose.setFocus()
        self._update_actions()
        self.changed.emit()

    def eventFilter(self, watched, event):
        if event.type() == QEvent.FocusIn:
            for row in range(self.table.rowCount()):
                for column in range(3):
                    cell = self.table.cellWidget(row, column)
                    if cell is watched or (cell is not None and cell.isAncestorOf(watched)):
                        self.table.setCurrentCell(row, column)
        return super().eventFilter(watched, event)

    def _browse(self, field):
        selected = QFileDialog.getExistingDirectory(self, "Choose Model Override Folder", field.text())
        if selected:
            field.setText(selected)

    def remove_selected(self):
        if self.table.currentRow() >= 0:
            self.table.removeRow(self.table.currentRow())
        self._update_actions()
        self.changed.emit()

    def rows(self):
        return tuple((self.table.cellWidget(row, 0).currentData(),
                      self.table.cellWidget(row, 1).text(),
                      self.table.cellWidget(row, 2).findChild(QLineEdit).text())
                     for row in range(self.table.rowCount()))

    def set_overrides(self, values):
        if (not isinstance(values, dict) or len(values) > 100
                or any(not isinstance(key, str) or not isinstance(value, str)
                       or (":" in key and not key.partition(":")[2])
                       for key, value in values.items())):
            raise ValueError("Saved model folder overrides cannot be displayed safely; the original settings were preserved.")
        self.table.setRowCount(0)
        for key, value in values.items():
            role, _, family = key.partition(":")
            self.add_override(role, family, value, focus=False)
        self._update_actions()

    def mapping(self):
        result = {}
        for index, (role, family, folder) in enumerate(self.rows(), 1):
            if role not in RESOURCE_ROLES:
                raise ValueError(f"Choose a supported model purpose in row {index}.")
            if family != family.strip():
                raise ValueError(f"Use an exact family name without surrounding spaces in row {index}.")
            key = role + (":" + family if family else "")
            if key in result:
                raise ValueError(f"Rows repeat {MODEL_PURPOSE_LABELS.get(role, role)} / {family or 'All families'}. Keep one folder for each purpose and family.")
            if not folder.strip():
                raise ValueError(f"Choose a folder in row {index}, or remove the override to use the default folder.")
            result[key] = folder.strip()
        return result


class FamilyDestinationEditor(QWidget):
    """Stage explicit family roots; rows are paths, not compatibility claims."""
    changed = Signal()

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        self.table = QTableWidget(0, 2)
        self.table.setAccessibleName("Configured model family destinations")
        self.table.setHorizontalHeaderLabels(("Family or purpose", "Destination folder"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.table.setMinimumHeight(240)
        layout.addWidget(self.table)
        actions = QHBoxLayout()
        add = compact_button(QPushButton("Add Another Family…"))
        add.clicked.connect(self.add_family)
        browse = compact_button(QPushButton("Browse Selected Destination…"))
        browse.clicked.connect(self.browse_selected)
        remove = compact_button(QPushButton("Remove Selected Destination"))
        remove.clicked.connect(self.remove_selected)
        actions.addWidget(add)
        actions.addWidget(browse)
        actions.addWidget(remove)
        actions.addStretch(1)
        layout.addLayout(actions)

    def add_family(self):
        media, accepted = QInputDialog.getItem(self, "Model Media", "Model media", ("Images", "Video"), 0, False)
        if not accepted:
            return
        family, accepted = QInputDialog.getText(self, "Model Family", "Exact family name")
        if not accepted or not family.strip():
            return
        key = media + ":" + family.strip()
        if key in dict(self.rows()):
            self.table.setCurrentCell(next(i for i, (name, _) in enumerate(self.rows()) if name == key), 1)
            return
        values = dict(self.rows())
        values[key] = ""
        self.set_destinations(values)
        self.table.setCurrentCell(self.table.rowCount() - 1, 1)
        self.table.cellWidget(self.table.rowCount() - 1, 1).setFocus()

    def remove_selected(self):
        if self.table.currentRow() >= 0:
            self.table.removeRow(self.table.currentRow())
            self.changed.emit()

    def browse_selected(self):
        row = self.table.currentRow()
        if row < 0:
            return
        field = self.table.cellWidget(row, 1)
        selected = QFileDialog.getExistingDirectory(self, "Choose Family Destination", field.text())
        if selected:
            field.setText(selected)

    def set_destinations(self, values):
        if not isinstance(values, dict) or len(values) > 100:
            raise ValueError("Saved family destinations cannot be displayed safely.")
        self.table.setRowCount(0)
        for key, path in values.items():
            if not isinstance(key, str) or not isinstance(path, str):
                raise ValueError("Saved family destinations cannot be displayed safely.")
            row = self.table.rowCount()
            self.table.insertRow(row)
            name = QLabel(key)
            name.setAccessibleName("Configured family or purpose")
            field = QLineEdit(path)
            field.setAccessibleName(key + " destination")
            field.setToolTip(path)
            field.textChanged.connect(field.setToolTip)
            field.textChanged.connect(self.changed.emit)
            self.table.setCellWidget(row, 0, name)
            self.table.setCellWidget(row, 1, field)
        self.changed.emit()

    def rows(self):
        return tuple((self.table.cellWidget(row, 0).text(),
                      self.table.cellWidget(row, 1).text())
                     for row in range(self.table.rowCount()))

    def mapping(self):
        result = {}
        for key, path in self.rows():
            if not path.strip():
                raise ValueError(f"Choose a destination for {key}.")
            if key in result:
                raise ValueError(f"Family destination {key} is repeated.")
            result[key] = path.strip()
        return result


class _FolderEvents(QObject):
    changed = Signal(object)


_events = None


def folder_events():
    global _events
    if _events is None:
        _events = _FolderEvents()
    return _events


class HistoryLocationsEditor(QWidget):
    """Explicit source enrollment, with independent drafts preserved on selection."""
    availability_changed = Signal(bool)

    def __init__(self, settings, parent=None):
        super().__init__(parent)
        self.settings = settings
        self._selected = ""
        self._loading = False
        self._drafts = {}
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        note = QLabel("Choose each source that should keep saved Image Knowledge history. Select private storage on this computer; history may contain private image information. Saving these choices does not inspect or move source files.")
        note.setWordWrap(True)
        form.addRow(note)
        self.source = QComboBox()
        self.source.setAccessibleName("History source")
        form.addRow("Source", self.source)
        self.summary = QLabel()
        self.summary.setTextFormat(Qt.PlainText)
        self.summary.setWordWrap(True)
        form.addRow(self.summary)
        self.zone = QLineEdit()
        self.history = QLineEdit()
        self._browse_buttons = []
        for field, label in ((self.zone, "Private storage zone"), (self.history, "History folder")):
            field.setAccessibleName(label)
            field.setPlaceholderText("Choose an explicit absolute folder")
            row = QHBoxLayout()
            row.addWidget(field, 1)
            button = compact_button(QPushButton("Browse…"))
            button.setAccessibleName("Browse " + label.lower())
            self._browse_buttons.append(button)
            button.clicked.connect(lambda checked=False, target=field: self._browse(target))
            row.addWidget(button)
            form.addRow(label, row)
        self.private = QCheckBox("This zone is private, machine-local, and is not shared or synchronized")
        self.enabled = QCheckBox("Enable saved-data history for this source")
        form.addRow(self.private)
        form.addRow(self.enabled)
        self.state = QLabel()
        self.state.setTextFormat(Qt.PlainText)
        self.state.setWordWrap(True)
        form.addRow(self.state)
        self.retained = QLabel()
        self.retained.setWordWrap(True)
        form.addRow(self.retained)
        self.source.currentIndexChanged.connect(self._select)
        guard_scrollable_value_controls(self)

    def _browse(self, field):
        chosen = QFileDialog.getExistingDirectory(self, "Choose " + field.accessibleName(), field.text())
        if chosen:
            field.setText(chosen)

    def _form(self):
        return (self.zone.text(), self.history.text(), self.private.isChecked(), self.enabled.isChecked())

    def values(self):
        values = dict(self._drafts)
        if self._selected:
            values[self._selected] = self._form()
        return values

    def _select(self):
        if self._loading:
            return
        if self._selected:
            self._drafts[self._selected] = self._form()
        self._selected = self.source.currentData() or ""
        zone, history, private, enabled = self._drafts.get(self._selected, ("", "", False, False))
        self.zone.setText(zone)
        self.history.setText(history)
        self.private.setChecked(private)
        self.enabled.setChecked(enabled)
        source = next((row for row in self._sources if row["source_key"] == self._selected), {})
        self.summary.setText(f"{source.get('role', '').replace('_', ' ').title()} · {source.get('root') or 'Configure this source folder first.'}")
        if self.error:
            self.state.setText(self.error)
        elif self.settings is not None:
            result = HistoryConfigurationService(self.settings).resolve(source_key=self._selected)
            self.state.setText(result.status.replace("_", " ").title() + ": " + result.reason)

    def reload(self):
        self.expected = deepcopy(getattr(self.settings, "history_storage", {}))
        self.expected_sources = source_signature(self.settings) if self.settings else []
        self._sources = sources(self.settings) if self.settings else []
        self.error = ""
        try:
            parsed = configuration(self.expected)
        except ValueError as exc:
            self.error = str(exc)
            parsed = {"zones": {}, "bindings": {}}
        selected = self._selected
        self._selected = ""
        self._drafts = {}
        self._loading = True
        self.source.clear()
        for row in self._sources:
            key = row["source_key"]
            self.source.addItem(row["name"], key)
            binding = next((item for item in parsed["bindings"].values() if item["source_key"] == key and not item["retired"]), None)
            zone = parsed["zones"].get(binding["zone_id"], {}) if binding else {}
            self._drafts[key] = (zone.get("zone_root", ""), binding["history_root"] if binding else "", bool(binding), binding["enabled"] if binding else False)
        self.source.setCurrentIndex(max(0, self.source.findData(selected)))
        self._loading = False
        self._select()
        self.baseline = self.values()
        count = sum(item["retired"] or not item["enabled"] for item in parsed["bindings"].values())
        self.retained.setText("Retained stores cannot be interpreted by this version. The original configuration remains unchanged." if self.error else
            f"{count} disabled or retired stores remain protected. Changing a source or storage folder starts a separate store; previous history stays in place. Nothing is migrated or deleted.")
        for field in (self.zone, self.history, self.private, self.enabled, *self._browse_buttons):
            field.setEnabled(not self.error)
        self.availability_changed.emit(not self.error)

    def staged_configuration(self):
        if self.error:
            raise ValueError(self.error)
        value = deepcopy(self.expected)
        for key, fields in self.values().items():
            if fields != self.baseline[key]:
                zone, history, private, enabled = fields
                value = stage_binding(self.settings, value, key, zone.strip(), history.strip(), private_local=private, enabled=enabled)
        return value


class FolderLocationsWidget(QWidget):
    saved = Signal()
    model_section_requested = Signal(str)

    def __init__(self, settings, parent=None, *, registry=None, resolution_path=None,
                 extra_protected_roots=(), settings_path=None, task_navigation=False):
        super().__init__(parent)
        self.settings = settings
        self.registry = registry or ModelResourceRegistry(MODEL_RESOURCES_PATH)
        self.resolution_path = resolution_path
        self.settings_path = settings_path
        self.extra_protected_roots = tuple(extra_protected_roots)
        self.fields = {}
        self.field_forms = {}
        self.groups = {}
        self._app_expected = {}
        self._model_token = None
        self._model_save_failed = False
        self._resolution = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        note = QLabel("Set persistent folders here. Changes apply to future work; existing files and recovery records stay in place. Each group saves separately.")
        note.setWordWrap(True)
        self.intro_note = note
        layout.addWidget(note)
        self.task_navigation = task_navigation
        if task_navigation:
            note.setText("Choose a folder task. Existing files stay in place. New model folders and Fast storage share one Save; other groups save separately.")
            task_row = QHBoxLayout()
            self.task_nav = QListWidget()
            self.task_nav.setAccessibleName("Folder location tasks")
            self.task_nav.setFixedWidth(190)
            self.task_nav.setWordWrap(True)
            self.task_nav.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            for label in ("Image folders & backups", "Image History & Versions",
                          "Global Data History", "Resize version folders",
                          "New model folders", "Fast storage", "Analysis model folder"):
                self.task_nav.addItem(label)
            task_row.addWidget(self.task_nav)
            self.task_content = QStackedWidget()
            task_row.addWidget(self.task_content, 1)
            layout.addLayout(task_row, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        self.group_status = {}
        for group_key, title, specs in (
            ("app", "Images and backups", IMAGE_APP_FOLDERS),
            ("history", "History & Versions", ()),
            ("structured_history", "Global Data History", ()),
            ("analysis", "AI analysis model storage", ANALYSIS_APP_FOLDERS),
            ("models", "Model locations", (("default_destination", "Default model folder"),)),
            ("resize", "Protected resize versions", RESIZE_FOLDERS),
        ):
            group = QGroupBox(title.replace("&", "&&"))
            if group_key == "models":
                model_group_layout = QVBoxLayout(group)
                self.model_section_picker = QComboBox()
                self.model_section_picker.setAccessibleName("Model locations task")
                self.model_section_picker.addItem("New model folders", "folders")
                self.model_section_picker.addItem("Fast storage", "fast")
                model_group_layout.addWidget(self.model_section_picker)
                self.model_sections = QStackedWidget()
                self.model_sections.setMinimumHeight(280)
                self.model_section_forms = {}
                for section_key in ("folders", "fast"):
                    section_scroll = QScrollArea()
                    section_scroll.setWidgetResizable(True)
                    section_scroll.setFrameShape(QFrame.NoFrame)
                    section_content = QWidget()
                    section_form = QFormLayout(section_content)
                    section_scroll.setWidget(section_content)
                    self.model_sections.addWidget(section_scroll)
                    self.model_section_forms[section_key] = section_form
                self.model_section_picker.currentIndexChanged.connect(
                    self.model_sections.setCurrentIndex
                )
                model_group_layout.addWidget(self.model_sections, 1)
                form = self.model_section_forms["folders"]
            else:
                group_layout = QVBoxLayout(group)
                group_scroll = QScrollArea()
                group_scroll.setWidgetResizable(True)
                group_scroll.setFrameShape(QFrame.NoFrame)
                group_content = QWidget()
                form = QFormLayout(group_content)
                group_scroll.setWidget(group_content)
                group_layout.addWidget(group_scroll, 1)
            self.groups[group_key] = group
            if group_key == "history":
                self.history_editor = HistoryLocationsEditor(settings)
                form.addRow(self.history_editor)
            if group_key == "structured_history":
                self.structured_history_editor = StructuredHistoryLocationsEditor(settings, settings_path=settings_path)
                form.addRow(self.structured_history_editor)
            for key, label in specs:
                field = QLineEdit()
                field.setAccessibleName(label)
                field.setMinimumWidth(100)
                self.fields[key] = field
                row = QHBoxLayout()
                row.addWidget(field, 1)
                browse = QPushButton("Browse…")
                browse.clicked.connect(lambda checked=False, k=key: self.choose(k))
                row.addWidget(browse)
                form.addRow(label, row)
                self.field_forms[key] = (form, row)
                if key == "default_destination":
                    self.default_model_label = form.labelForField(row)
            if group_key == "models":
                self.storage_policy = ModelStoragePolicyEditor(role_labels=MODEL_PURPOSE_LABELS)
                self.storage_policy.mode_changed.connect(self._storage_mode_changed)
                self.model_section_forms["fast"].addRow(self.storage_policy)
                preview_intro = QLabel("Where new models will go. Choose a family to see separate main-model, LoRA, VAE and text-encoder destinations. Existing files stay where they are.")
                preview_intro.setWordWrap(True)
                form.addRow(preview_intro)
                self.model_preview_family = QComboBox()
                self.model_preview_family.setAccessibleName("Preview model family destinations")
                for key in FAMILY_DESTINATIONS:
                    if ":" in key or key == "Shared":
                        self.model_preview_family.addItem(key.replace(":", " / "), key)
                form.addRow("Example family", self.model_preview_family)
                self.model_destination_preview = QLabel()
                self.model_destination_preview.setAccessibleName("New model destination preview")
                self.model_destination_preview.setTextFormat(Qt.PlainText)
                self.model_destination_preview.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Maximum)
                self.model_destination_preview.setWordWrap(True)
                form.addRow(self.model_destination_preview)
                self.folder_details_button = compact_button(QPushButton("Customize destination folders"))
                self.folder_details_button.setCheckable(True)
                form.addRow(self.folder_details_button)
                self.folder_details = QWidget()
                details = QVBoxLayout(self.folder_details)
                details.setContentsMargins(0, 0, 0, 0)
                hint = QLabel("Advanced: exact role/family override, then configured family folder, then legacy role override, then default. Folder placement does not prove generator compatibility.")
                hint.setWordWrap(True)
                details.addWidget(hint)
                self.family_root = QLineEdit()
                self.family_root.setAccessibleName("Family layout root")
                self.family_root.setPlaceholderText("Absolute folder for a new Images / Video family layout")
                family_row = QHBoxLayout()
                family_row.addWidget(self.family_root, 1)
                family_browse = compact_button(QPushButton("Browse…"))
                family_browse.clicked.connect(self.choose_family_root)
                family_row.addWidget(family_browse)
                details.addWidget(QLabel("Organize new acquisitions under"))
                details.addLayout(family_row)
                template = compact_button(QPushButton("Preview Image / Video Family Folders"))
                template.clicked.connect(self.stage_family_template)
                details.addWidget(template)
                self.family_destinations = FamilyDestinationEditor()
                details.addWidget(self.family_destinations)
                self.overrides = ModelFolderOverrides()
                details.addWidget(self.overrides)
                self.folder_details_button.target = self.folder_details
                self.folder_details_button.toggled.connect(self.folder_details.setVisible)
                self.folder_details.hide()
                form.addRow(self.folder_details)
                self.model_preview_family.currentIndexChanged.connect(self._update_model_destination_preview)
                self.fields["default_destination"].textChanged.connect(self._update_model_destination_preview)
                self.family_destinations.changed.connect(self._update_model_destination_preview)
                self.overrides.changed.connect(self._update_model_destination_preview)
            if group_key == "analysis":
                hint = QLabel("Managed tagger and captioner packs use this existing analysis-model setting. It is separate from future acquisition destinations and from folders already indexed in place.")
                hint.setWordWrap(True)
                form.addRow(hint)
            if group_key == "resize":
                self.fields["output_root"].setPlaceholderText(str(USER_DATA_DIR / "resize_versions"))
                hint = QLabel("Changing an automatic destination turns that direction off. Review and enable it again in Automatic resizing. Keep earlier folders to recover their saved versions.")
                hint.setWordWrap(True)
                form.addRow(hint)
            save = QPushButton("Save " + {"history": "History Locations", "structured_history": "Global History Locations", "app": "Storage Folders", "analysis": "Analysis Model Folder", "models": "Model Settings", "resize": "Resize Folders"}[group_key])
            save.clicked.connect(lambda checked=False, g=group_key: self.save_group(g))
            if group_key == "history":
                self.history_editor.availability_changed.connect(save.setEnabled)
            if group_key == "structured_history":
                self.structured_history_editor.availability_changed.connect(save.setEnabled)
            group_actions = QHBoxLayout()
            group_actions.addStretch(1)
            reload_local = QPushButton("Discard Changes")
            reload_local.setAccessibleName("Discard " + title + " changes")
            reload_local.setToolTip("Reload this group's saved folders; other drafts are kept.")
            reload_local.clicked.connect(lambda checked=False, g=group_key: self.reload_group(g))
            group_actions.addWidget(reload_local)
            group_actions.addWidget(save)
            if group_key == "models":
                save_scope = QLabel(
                    "Save Model Settings saves new model folders and Fast storage together. "
                    "Switching tasks keeps both drafts; saving moves no files."
                )
                save_scope.setWordWrap(True)
                save_scope.setObjectName("mutedLabel")
                model_group_layout.addWidget(save_scope)
                model_group_layout.addLayout(group_actions)
            else:
                group_layout.addLayout(group_actions)
            local_status = QLabel()
            local_status.setWordWrap(True)
            local_status.setTextFormat(Qt.PlainText)
            local_status.setAccessibleName(title + " status")
            self.group_status[group_key] = local_status
            if group_key == "models":
                model_group_layout.addWidget(local_status)
                guard_scrollable_value_controls(group)
            else:
                group_layout.addWidget(local_status)
                guard_scrollable_value_controls(group_content)
            if task_navigation:
                self.task_content.addWidget(group)
            else:
                layout.addWidget(group)
        layout.addWidget(self.status)
        if task_navigation:
            self.status.hide()
            self.model_section_picker.hide()
            self.task_nav.currentRowChanged.connect(self._select_task_row)
            self.model_section_requested.connect(
                lambda section: self.task_nav.setCurrentRow(5 if section == "fast" else 4)
            )
            self.task_nav.setCurrentRow(0)
        for field in (self.fields["default_destination"], self.storage_policy.fast_root,
                      self.family_root):
            field.textChanged.connect(self._model_draft_edited)
        for field in (self.storage_policy.limit, self.storage_policy.reserve):
            field.valueChanged.connect(self._model_draft_edited)
        for field in (self.storage_policy.mode, self.storage_policy.default_policy,
                      self.storage_policy.automatic_cleanup):
            field.currentIndexChanged.connect(self._model_draft_edited)
        self.storage_policy.no_fixed_cap.toggled.connect(self._model_draft_edited)
        self.family_destinations.changed.connect(self._model_draft_edited)
        self.overrides.changed.connect(self._model_draft_edited)
        self.reload()
        folder_events().changed.connect(self._external_change)

    def _select_task_row(self, row):
        if row < 0 or not self.task_navigation:
            return
        group, model_section = (
            ("app", None), ("history", None), ("structured_history", None),
            ("resize", None), ("models", "folders"), ("models", "fast"),
            ("analysis", None),
        )[row]
        self.task_content.setCurrentWidget(self.groups[group])
        if model_section is not None:
            self.select_model_section(model_section)

    def select_group_task(self, group):
        if not self.task_navigation:
            return
        row = {"app": 0, "history": 1, "structured_history": 2,
               "resize": 3, "analysis": 6}.get(group)
        if group == "models":
            row = 5 if self.model_section_picker.currentData() == "fast" else 4
        if row is not None:
            self.task_nav.setCurrentRow(row)

    def _model_draft_edited(self, *_args) -> None:
        if self._model_save_failed:
            self._model_save_failed = False
            message = "Draft updated. Save Model Settings to check and save it."
            self.group_status["models"].setText(message)
            self.status.setText(message)

    def select_model_section(self, section: str) -> None:
        index = self.model_section_picker.findData(section)
        if index < 0:
            raise ValueError(f"Unknown model locations section: {section}")
        self.model_section_picker.setCurrentIndex(index)
        self.model_section_requested.emit(section)

    def choose_family_root(self):
        selected = QFileDialog.getExistingDirectory(self, "Choose Model Organization Root", self.family_root.text())
        if selected:
            self.family_root.setText(selected)

    def _storage_mode_changed(self, mode):
        label = "Archive folder" if mode == "tiered" else "Default model folder"
        self.default_model_label.setText(label)
        self.fields["default_destination"].setAccessibleName(label)
        self._update_model_destination_preview()

    def _update_model_destination_preview(self, *_):
        if not hasattr(self, "model_destination_preview"):
            return
        key = self.model_preview_family.currentData() or "Images:SDXL"
        media, family = key.split(":", 1) if ":" in key else ("Images", key)
        workspace = {
            "default_destination": self.fields["default_destination"].text().strip(),
            "family_destinations": {name: path.strip() for name, path in self.family_destinations.rows() if path.strip()},
            "destination_overrides": {role + (":" + selected if selected else ""): path.strip()
                for role, selected, path in self.overrides.rows() if role in RESOURCE_ROLES and path.strip()},
        }
        lines = []
        for label, role in (("Main model", "checkpoint"), ("LoRA", "lora"),
                            ("VAE", "vae"), ("Text encoder", "text_encoder")):
            path, origin = destination_path(workspace, role=role, family=family, media_type=media)
            lines.append(f"{label}: {path or 'Not configured'} ({origin})")
        if self.storage_policy.mode.currentData() == "tiered":
            lines.append("Prepared Fast copies use the separate Fast folder; Archive originals remain in the destinations above.")
        self.model_destination_preview.setText("\n".join(lines))

    def _set_status(self, group, message):
        self.status.setText(message)
        self.group_status[group].setText(message)

    def reload_group(self, group):
        """Refresh one folder group, retaining drafts elsewhere."""
        try:
            if group in ("app", "analysis"):
                specs = IMAGE_APP_FOLDERS if group == "app" else ANALYSIS_APP_FOLDERS
                for key, _ in specs:
                    value = str(getattr(self.settings, key, "") or "")
                    self.fields[key].setText(value)
                    self._app_expected[key] = value
            elif group == "history":
                from core.settings import DEFAULT_SETTINGS_PATH
                self.settings.reload_history_storage(self.settings_path or DEFAULT_SETTINGS_PATH)
                self.history_editor.reload()
            elif group == "structured_history":
                from core.settings import DEFAULT_SETTINGS_PATH
                self.settings.reload_structured_history_storage(self.settings_path or DEFAULT_SETTINGS_PATH)
                self.structured_history_editor.reload()
            elif group == "models":
                snapshot, token = self.registry.snapshot()
                values = snapshot.get("workspace", {})
                self.storage_policy.validate(values)
                old_overrides = dict((role + (":" + family if family else ""), folder)
                                     for role, family, folder in self.overrides.rows())
                old_families = dict(self.family_destinations.rows())
                try:
                    self.overrides.set_overrides(values.get("destination_overrides", {}))
                    self.family_destinations.set_destinations(values.get("family_destinations", {}))
                except (ValueError, RuntimeError):
                    self.overrides.set_overrides(old_overrides)
                    self.family_destinations.set_destinations(old_families)
                    raise
                self.fields["default_destination"].setText(values.get("default_destination", ""))
                self.family_root.setText(values.get("default_destination", ""))
                self.storage_policy.load(values)
                self._model_token = token
                self._update_model_destination_preview()
            elif group == "resize":
                resolution = load_resolution_settings(self.resolution_path)
                for key, _ in RESIZE_FOLDERS:
                    self.fields[key].setText(resolution.get(key, ""))
                self._resolution = resolution
            else:
                raise ValueError("Choose a folder group to reload.")
            self._saved_values[group] = self._group_values(group)
            if group == "models":
                self._model_save_failed = False
            self._set_status(group, "Saved folder choices reloaded for this group. Other drafts remain unchanged.")
        except (OSError, ValueError, RuntimeError) as exc:
            self._set_status(group, str(exc) + " This group's draft was left in place.")

    def stage_family_template(self):
        try:
            root = self.family_root.text().strip() or self.fields["default_destination"].text().strip()
            values = family_destination_template(root)
            values.update(self.family_destinations.mapping())
            self.family_destinations.set_destinations(values)
            if not self.fields["default_destination"].text().strip():
                self.fields["default_destination"].setText(values["Unclassified"])
            self._set_status("models", "Missing family paths staged; existing choices kept. Review each path, then Save Model Settings. No directories were created.")
        except ValueError as exc:
            self._set_status("models", str(exc))

    def _external_change(self, source):
        if source is self:
            return
        if (source.settings is not self.settings or source.registry.path != self.registry.path
                or source.resolution_path != self.resolution_path):
            return
        if self.dirty_groups():
            self.status.setText("Saved folders changed elsewhere. Your pending edits remain here; discard the affected group's changes before saving conflicting changes.")
            for group in self.dirty_groups():
                self.group_status[group].setText("Saved folders changed elsewhere. Your draft remains here; use this group's Discard Changes action if you want to reload it.")
        else:
            self.reload()
            self.saved.emit()

    def _group_values(self, group):
        if group == "history":
            return self.history_editor.values()
        if group == "structured_history":
            return self.structured_history_editor.values()
        keys = {"app": IMAGE_APP_FOLDERS, "analysis": ANALYSIS_APP_FOLDERS,
                "resize": RESIZE_FOLDERS,
                "models": (("default_destination", ""),)}[group]
        values = {key: self.fields[key].text() for key, _ in keys}
        if group == "models":
            values["overrides"] = self.overrides.rows()
            values["family_destinations"] = self.family_destinations.rows()
            values["storage_policy"] = self.storage_policy.values()
        return values

    def dirty_groups(self):
        return [group for group in self.groups
                if self._group_values(group) != self._saved_values[group]]

    def confirm_leave(self):
        groups = self.dirty_groups()
        if not groups:
            return True
        from PySide6.QtWidgets import QMessageBox
        choice = QMessageBox.question(self, "Unsaved Folder Changes",
            "Save the changed folder groups before leaving? Existing files will stay in place.",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel, QMessageBox.Cancel)
        if choice == QMessageBox.Cancel:
            return False
        if choice == QMessageBox.Discard:
            self.reload()
            return True
        for group in groups:
            if not self.save_group(group):
                self.select_group_task(group)
                QMessageBox.warning(self, "Folder Changes Not Saved",
                    self.group_status[group].text() + "\n\nYour draft is still here. Correct it and save, or choose Discard Changes before leaving.")
                return False
        return True

    def reload(self):
        if self.settings is not None:
            self._app_expected = {key: str(getattr(self.settings, key, "") or "") for key, _ in APP_FOLDERS}
            for key, value in self._app_expected.items():
                self.fields[key].setText(value)
        self.groups["app"].setEnabled(self.settings is not None)
        self.groups["analysis"].setEnabled(self.settings is not None)
        self.groups["history"].setEnabled(self.settings is not None)
        self.history_editor.reload()
        self.groups["structured_history"].setEnabled(self.settings is not None)
        self.structured_history_editor.reload()
        errors = []
        try:
            snapshot, self._model_token = self.registry.snapshot()
            values = snapshot.get("workspace", {})
            self.storage_policy.validate(values)
            self.fields["default_destination"].setText(values.get("default_destination", ""))
            self.overrides.set_overrides(values.get("destination_overrides", {}))
            self.family_destinations.set_destinations(values.get("family_destinations", {}))
            self.family_root.setText(values.get("default_destination", ""))
            self.storage_policy.load(values)
            self._update_model_destination_preview()
        except (OSError, ValueError, RuntimeError) as exc:
            self._model_token = None
            errors.append(str(exc))
        try:
            self._resolution = load_resolution_settings(self.resolution_path)
            for key, _ in RESIZE_FOLDERS:
                self.fields[key].setText(self._resolution.get(key, ""))
        except (OSError, ValueError, RuntimeError) as exc:
            self._resolution = None
            errors.append(str(exc))
        self.status.setText(" ".join(errors) or "Recycle Bin must be machine-local and separate from libraries, incoming images, Problems and backups.")
        for label in self.group_status.values():
            label.clear()
        self._saved_values = {group: self._group_values(group) for group in self.groups}

    def choose(self, key):
        selected = QFileDialog.getExistingDirectory(self, "Choose " + self.fields[key].accessibleName(), self.fields[key].text())
        if selected:
            self.fields[key].setText(selected)

    def save_group(self, group):
        failure_section = "folders"
        try:
            warnings = ()
            if group in ("app", "analysis"):
                specs = IMAGE_APP_FOLDERS if group == "app" else ANALYSIS_APP_FOLDERS
                values = {key: self.fields[key].text() for key, _ in specs}
                if all(values[key] == self._app_expected[key] for key in values):
                    return True
                merged = {key: str(getattr(self.settings, key, "") or "") for key, _ in APP_FOLDERS}
                merged.update(values)
                # Prevent an edited image/backup/model role from engulfing saved versions.
                resize = load_resolution_settings(self.resolution_path)
                snapshot, _ = self.registry.snapshot()
                validate_model_folder_separation(merged, self.settings.library_registry().profiles,
                                                 snapshot.get("workspace", {}), snapshot=snapshot)
                roots = self.extra_protected_roots + tuple(p.root for p in self.settings.library_registry().profiles) + tuple(v for v in merged.values() if v)
                validate_version_folders({key: resize.get(key, "") for key, _ in RESIZE_FOLDERS}, roots)
                validation = save_app_folders(self.settings, values, expected=self._app_expected)
                warnings = validation.warnings
                self._app_expected = {key: str(getattr(self.settings, key, "") or "") for key, _ in APP_FOLDERS}
            elif group == "history":
                editor = self.history_editor
                if editor.values() == editor.baseline and not editor.error:
                    return True
                save_history_folders(self.settings, editor.staged_configuration(), expected=editor.expected,
                    expected_sources=editor.expected_sources, registry=self.registry,
                    resolution_path=self.resolution_path, settings_path=self.settings_path)
                editor.reload()
            elif group == "structured_history":
                from core.folder_locations import save_structured_history_folders
                editor = self.structured_history_editor
                if editor.values() == editor.baseline and not editor.error:
                    return True
                save_structured_history_folders(self.settings, editor.staged_configuration(), expected=editor.expected,
                    expected_authority=editor.expected_authority, registry=self.registry,
                    resolution_path=self.resolution_path, settings_path=self.settings_path)
                editor.reload()
            elif group == "models":
                if self._model_token is None:
                    raise ValueError("Reload the model folders before saving.")
                overrides = self.overrides.mapping()
                families = self.family_destinations.mapping()
                failure_section = "fast"
                policy_values = self.storage_policy.values()
                self.storage_policy.validate({
                    "storage_policy": {"schema_version": 1, **policy_values}
                })
                failure_section = "folders"
                resize = load_resolution_settings(self.resolution_path)
                roots = self.extra_protected_roots + protected_roots(self.settings) + tuple(resize.get(key, "") for key, _ in RESIZE_FOLDERS if resize.get(key))
                # Analysis models and acquired models may deliberately share storage.
                roots = tuple(root for root in roots if root != getattr(self.settings, "ai_models_directory", None))
                ModelWorkspace(self.registry, protected_roots=roots).save_destinations(
                    self.fields["default_destination"].text().strip(), overrides,
                    expected_registry=self._model_token, family_destinations=families,
                    storage_policy=policy_values,
                    managed_roots=tuple(p for p in (getattr(self.settings, "ai_models_directory", ""),) if p))
                _, self._model_token = self.registry.snapshot()
            else:
                if self._resolution is None:
                    raise ValueError("Reload the resize folders before saving.")
                snapshot, _ = self.registry.snapshot()
                from core.model_storage_policy import model_storage_roots
                roots = self.extra_protected_roots + protected_roots(self.settings) + model_storage_roots(snapshot)
                self._resolution = save_version_folders(
                    {key: self.fields[key].text() for key, _ in RESIZE_FOLDERS},
                    path=self.resolution_path, expected=self._resolution, roots=roots)
            self._set_status(group, "Folders saved. Existing files stay in place." + (
                " Changed automatic directions are off until reviewed again." if group == "resize" else "")
                + (" " + " ".join(warnings) if warnings else ""))
            self._saved_values[group] = self._group_values(group)
            if group == "models":
                self._model_save_failed = False
            self.saved.emit()
            folder_events().changed.emit(self)
            return True
        except (OSError, ValueError, RuntimeError) as exc:
            self._set_status(group, str(exc) + " Nothing was saved. Correct this task and try again, or Discard Changes.")
            if group == "models":
                self._model_save_failed = True
                message = str(exc).lower()
                if any(term in message for term in ("fast", "cache", "reserve", "free space", "storage policy")):
                    failure_section = "fast"
                self.select_model_section(failure_section)
            return False


class FolderLocationsDialog(QDialog):
    def reject(self):
        if self.editor.confirm_leave():
            super().reject()

    def closeEvent(self, event):
        if self.editor.confirm_leave():
            event.accept()
        else:
            event.ignore()


def open_folder_locations(parent, *, focus="", settings=None, registry=None, resolution_path=None, extra_protected_roots=(), focus_group_only=False):
    """Open the canonical Setup section without losing a modal workflow's draft."""
    host = parent
    while settings is None and host is not None:
        settings = getattr(host, "settings", None) or getattr(getattr(host, "state", None), "settings", None)
        host = host.parentWidget()
    dialog = FolderLocationsDialog(parent)
    dialog.setWindowTitle("Setup · Libraries & Storage")
    dialog.resize(900, 760)
    layout = QVBoxLayout(dialog)
    editor = FolderLocationsWidget(settings, dialog, registry=registry,
                                   resolution_path=resolution_path,
                                   extra_protected_roots=extra_protected_roots,
                                   task_navigation=not focus_group_only)
    dialog.editor = editor
    if focus_group_only and focus == "ingest_directory":
        for key, group in editor.groups.items():
            group.setVisible(key == "app")
        editor.groups["app"].setTitle("Incoming images")
        for key, _label in IMAGE_APP_FOLDERS:
            if key != "ingest_directory":
                form, row = editor.field_forms[key]
                form.setRowVisible(row, False)
        editor.intro_note.setText(
            "Choose and save where incoming images arrive. Other storage folders remain available in Setup."
        )
        if editor.status.text().startswith("Recycle Bin must"):
            editor.status.setText("Saving this folder moves no images. Return to your pending image action for a fresh preview.")
        dialog.setWindowTitle("Setup · Incoming Images")
        dialog.resize(760,360)
    elif editor.task_navigation:
        for row, specs in ((0, IMAGE_APP_FOLDERS), (3, RESIZE_FOLDERS),
                           (6, ANALYSIS_APP_FOLDERS)):
            if focus in {key for key, _label in specs}:
                editor.task_nav.setCurrentRow(row)
                break
        else:
            if focus == "default_destination":
                editor.task_nav.setCurrentRow(4)
    layout.addWidget(editor, 1)
    buttons = QDialogButtonBox(QDialogButtonBox.Close)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    if focus in editor.fields:
        from PySide6.QtCore import QTimer
        def reveal_focus():
            field = editor.fields[focus]
            field.setFocus(Qt.OtherFocusReason)
            ancestor = field.parentWidget()
            while ancestor is not None:
                if isinstance(ancestor, QScrollArea):
                    ancestor.ensureWidgetVisible(field, 16, 16)
                ancestor = ancestor.parentWidget()
        QTimer.singleShot(0, reveal_focus)
    dialog.exec()
    dialog.deleteLater()
