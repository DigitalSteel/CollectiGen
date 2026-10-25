"""Embeddable Manager Settings workspace; shell/ribbon wiring is host-owned."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QWidget, QLabel, QPushButton, QCheckBox, QComboBox,
    QLineEdit, QFormLayout, QHBoxLayout, QTableWidget, QTableWidgetItem,
    QHeaderView, QAbstractItemView, QGroupBox, QVBoxLayout, QScrollArea)
from suite_core.manager.ui_cards import workspace_layout, compact_action, disclosure_panel
from .shared_settings_controller import GROUP_LABELS


def display_value(group, values):
    if values is None:
        return "Unavailable"
    if group == "appearance.theme":
        return values["ui_theme"].title()
    if group == "appearance.accessibility":
        return "; ".join(f"{name}: {'On' if values[key] else 'Off'}" for key, name in
                         (("color_blind_mode", "Color-blind palette"), ("show_tooltips", "Tooltips")))
    profiles = values["library_profiles"]
    scope = values["library_scope_ids"]
    return "; ".join([f"{p.get('name', p['stable_id'])}: {p['root']}" for p in profiles] +
        ["Scope: ALL libraries" if not scope else "Scope: " + ", ".join(scope),
         "Incoming: " + (values["ingest_directory"] or "Not set")])


class SharedSettingsPanel(QWidget):
    def __init__(self, controller, parent=None, *, submit=None):
        super().__init__(parent)
        self.controller = controller
        self.submit = submit  # Original Manager executor, never created here.
        self.setObjectName("sharedSettingsPanel")
        layout = workspace_layout(self)
        heading = QLabel("Update settings across modules")
        heading.setObjectName("cardTitle")
        layout.addWidget(heading)
        hint = QLabel("Choose enrolled recipients and review one batch. Independent settings stay unchanged unless you explicitly replace them. Saved values may still need an app reload or restart.")
        hint.setWordWrap(True); hint.setTextFormat(Qt.PlainText)
        layout.addWidget(hint)
        self.recipient_buttons = {}
        recipients = QGroupBox("Enrolled recipients"); recipients_layout = QVBoxLayout(recipients)
        for cap in controller.capabilities:
            check = QCheckBox(f"{cap.app_id} — {cap.profile_id} ({cap.host_id})")
            check.toggled.connect(lambda enabled, binding=cap.binding: self.select(binding, enabled))
            self.recipient_buttons[cap.binding] = check
            recipients_layout.addWidget(check)
        if not controller.capabilities:
            recipients_layout.addWidget(QLabel("No enrolled recipients supplied. Standalone apps remain independent."))
        layout.addWidget(recipients)
        self.editor = QWidget(); self.form = QFormLayout(self.editor)
        self.theme = None; self.palette = None; self.tooltips = None
        if "appearance.theme" in controller.values:
            self.theme = QComboBox(); self.theme.addItems(["system", "light", "dark", "midnight"])
            self.theme.setCurrentText(controller.values["appearance.theme"]["ui_theme"])
            self.theme.currentTextChanged.connect(lambda value: self.edit("appearance.theme", {"ui_theme": value}))
            self.form.addRow("Shared theme", self.theme)
        if "appearance.accessibility" in controller.values:
            self.palette = QCheckBox("Color-blind palette"); self.tooltips = QCheckBox("Show tooltips")
            for check, key in ((self.palette, "color_blind_mode"), (self.tooltips, "show_tooltips")):
                check.setChecked(controller.values["appearance.accessibility"][key])
                check.toggled.connect(self.edit_accessibility); self.form.addRow(check)
        self.location_controls = {}
        self.scope_valid = True
        self.active = None
        if "library.locations" in controller.values:
            locations = QWidget(); form = QFormLayout(locations)
            self.active = QComboBox()
            value = controller.values["library.locations"]
            for profile in value["library_profiles"]:
                identity = profile["stable_id"]
                self.active.addItem(profile.get("name", identity), identity)
                root = QLineEdit(profile["root"])
                root.setAccessibleName(f"Shared root for {profile.get('name', identity)}")
                root.textEdited.connect(self.edit_locations)
                form.addRow(profile.get("name", identity), root)
                self.location_controls[identity] = root
            self.active.setCurrentIndex(self.active.findData(value["active_library_id"]))
            self.active.currentIndexChanged.connect(self.edit_locations)
            form.addRow("Active library", self.active)
            self.ingest = QLineEdit(value["ingest_directory"]); self.ingest.textEdited.connect(self.edit_locations)
            form.addRow("Incoming images", self.ingest)
            self.all_libraries = QCheckBox("Include ALL libraries")
            self.all_libraries.setChecked(not value["library_scope_ids"])
            form.addRow(self.all_libraries)
            self.scope_controls = {}
            for profile in value["library_profiles"]:
                check = QCheckBox(profile.get("name", profile["stable_id"]))
                check.setChecked(profile["stable_id"] in value["library_scope_ids"])
                check.setEnabled(not self.all_libraries.isChecked())
                check.toggled.connect(self.edit_locations)
                form.addRow("Included", check); self.scope_controls[profile["stable_id"]] = check
            self.all_libraries.toggled.connect(self.edit_locations)
            note = QLabel("Repoint configuration only. Existing library identities and roles are preserved; physical moves require the separate migration workflow.")
            note.setWordWrap(True); form.addRow(note)
            self.form.addRow(disclosure_panel("Library locations", locations))
        layout.addWidget(self.editor)
        advanced = QWidget(); advanced_layout = QVBoxLayout(advanced)
        self.replace_groups = {}
        for group in controller.values:
            check = QCheckBox("Replace independent values / reset to shared: " + GROUP_LABELS[group])
            check.toggled.connect(self.invalidate)
            advanced_layout.addWidget(check); self.replace_groups[group] = check
        layout.addWidget(disclosure_panel("Override and reset options", advanced))
        self.table = QTableWidget(0, 7)
        self.table.setHorizontalHeaderLabels(["Recipient", "Setting", "Effective value", "Origin", "Shared value", "Review", "State / recovery"])
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.horizontalHeader().setSectionResizeMode(QHeaderView.Interactive)
        self.table.horizontalHeader().setStretchLastSection(True)
        for column, width in enumerate((150, 115, 180, 105, 180, 105)):
            self.table.setColumnWidth(column, width)
        self.table.setMinimumHeight(180)
        self.table.setAccessibleName("Shared settings batch preview and recipient outcomes")
        layout.addWidget(self.table, 1)
        self.status = QLabel(); self.status.setWordWrap(True); self.status.setTextFormat(Qt.PlainText)
        self.status.setAccessibleName("Shared settings status"); layout.addWidget(self.status)
        self.availability = QLabel()
        self.availability.setWordWrap(True); self.availability.setObjectName("sharedSettingsApplyAvailability")
        layout.addWidget(self.availability)
        actions = QHBoxLayout()
        self.refresh = compact_action(QPushButton("Refresh"))
        self.current = compact_action(QPushButton("Use current shared values"))
        self.preview = compact_action(QPushButton("Preview update"))
        self.apply = compact_action(QPushButton("Apply reviewed batch")); self.apply.setProperty("primary", True)
        self.cancel = compact_action(QPushButton("Cancel remaining"))
        for button in (self.refresh, self.current, self.preview, self.apply, self.cancel): actions.addWidget(button)
        actions.addStretch(); layout.addLayout(actions)
        self.refresh.clicked.connect(lambda: self.perform(controller.refresh))
        self.current.clicked.connect(self.use_current)
        self.preview.clicked.connect(self.preview_batch)
        self.apply.clicked.connect(self.apply_batch)
        self.cancel.clicked.connect(controller.cancel)
        self.timer = QTimer(self); self.timer.setInterval(40); self.timer.timeout.connect(self.poll)
        self.timer.start(); self.render()

    def perform(self, action):
        try:
            action()
        except (OSError, ValueError):
            self.controller.plan = None
            self.controller.message = "Review held. Check the selected settings and refresh; no recipients written."
        self.render()

    def select(self, binding, enabled):
        self.controller.select(binding, enabled); self.render()

    def invalidate(self, *_):
        self.controller.invalidate(); self.render()

    def edit(self, group, value):
        self.perform(lambda: self.controller.edit_group(group, value))

    def edit_accessibility(self, *_):
        self.edit("appearance.accessibility", {"color_blind_mode": self.palette.isChecked(),
                                              "show_tooltips": self.tooltips.isChecked()})

    def edit_locations(self, *_):
        value = self.controller.values["library.locations"].copy()
        value["library_profiles"] = [{**p, "root": self.location_controls[p["stable_id"]].text()}
                                      for p in value["library_profiles"]]
        value["active_library_id"] = self.active.currentData() or ""
        root = next((p["root"] for p in value["library_profiles"] if p["stable_id"] == value["active_library_id"]), "")
        value.update(dataset_directory=root, working_directory=root, ingest_directory=self.ingest.text(),
                     library_scope_ids=[] if self.all_libraries.isChecked() else
                     [key for key, check in self.scope_controls.items() if check.isChecked()])
        self.scope_valid = self.all_libraries.isChecked() or bool(value["library_scope_ids"])
        for check in self.scope_controls.values(): check.setEnabled(not self.all_libraries.isChecked())
        self.edit("library.locations", value)

    def use_current(self):
        self.perform(self.controller.use_current)
        self.sync_editor_from_values()

    @property
    def editor_dirty(self):
        # Empty serialized scope means ALL. An unchecked ALL with no choices is
        # a deliberate incomplete editor state, not a clean ALL-library draft.
        return not self.scope_valid

    def sync_editor_from_values(self):
        """Synchronize visible controls from detached controller values, no I/O."""
        # Entries are intentionally fixed to the supplied finite group set.
        if self.theme:
            self.theme.blockSignals(True); self.theme.setCurrentText(self.controller.values["appearance.theme"]["ui_theme"]); self.theme.blockSignals(False)
        if self.palette:
            for check, key in ((self.palette, "color_blind_mode"), (self.tooltips, "show_tooltips")):
                check.blockSignals(True); check.setChecked(self.controller.values["appearance.accessibility"][key]); check.blockSignals(False)
        if self.active:
            value = self.controller.values["library.locations"]
            for p in value["library_profiles"]:
                if p["stable_id"] in self.location_controls: self.location_controls[p["stable_id"]].setText(p["root"])
            self.active.blockSignals(True); self.active.setCurrentIndex(self.active.findData(value["active_library_id"])); self.active.blockSignals(False)
            self.ingest.setText(value["ingest_directory"])
            for check in [self.all_libraries, *self.scope_controls.values()]: check.blockSignals(True)
            self.all_libraries.setChecked(not value["library_scope_ids"])
            for identity, check in self.scope_controls.items(): check.setChecked(identity in value["library_scope_ids"])
            for check in [self.all_libraries, *self.scope_controls.values()]: check.blockSignals(False)
            self.scope_valid = self.all_libraries.isChecked() or bool(value["library_scope_ids"])
            for check in self.scope_controls.values(): check.setEnabled(not self.all_libraries.isChecked())
        self.render()

    def preview_batch(self):
        if not self.scope_valid:
            self.controller.invalidate()
            self.controller.message = "Choose included libraries or explicitly include ALL libraries before preview."
            self.render()
            return
        self.perform(lambda: self.controller.preview(tuple(g for g, c in self.replace_groups.items() if c.isChecked())))

    def apply_batch(self):
        if self.submit is not None:
            self.controller.start_apply(self.submit)
        self.render()

    def poll(self):
        if self.controller.poll(): self.render()

    def render(self):
        busy = self.controller.close_held or self.controller.closed
        self.editor.setEnabled(not busy)
        for check in [*self.recipient_buttons.values(), *self.replace_groups.values()]: check.setEnabled(not busy)
        for button in (self.refresh, self.current, self.preview): button.setEnabled(not busy)
        self.apply.setEnabled(self.controller.can_apply and not busy and self.submit is not None)
        self.apply.setVisible(self.controller.contract_ready)
        self.availability.setVisible(not self.controller.contract_ready)
        self.availability.setText("Review only. Updates stay unavailable until shared settings and app reload support are approved. Your draft is retained in Manager.")
        self.cancel.setEnabled(self.controller.close_held)
        self.status.setText("Manager close is pending. Draft retained; Cancel pending close in Manager to continue." if self.controller.closed else "Applying reviewed batch. Cancellation stops remaining recipients after the current transaction settles." if busy else self.controller.message)
        if busy: return  # Worker owns controller state until completion.
        for binding, check in self.recipient_buttons.items():
            check.blockSignals(True); check.setChecked(binding in self.controller.selected); check.blockSignals(False)
        rows = self.controller.rows(); self.table.setRowCount(len(rows))
        for index, row in enumerate(rows):
            recovery = " Recovery: original settings history." if row["recovery"] else ""
            cells = (row["recipient"] + " / " + row["profile"], GROUP_LABELS[row["group"]],
                display_value(row["group"], row["effective"]), row["origin"],
                display_value(row["group"], row["desired"]),
                "Replace / inherit" if row["changes"] else "Retain", row["status"] + recovery)
            for column, value in enumerate(cells):
                item = QTableWidgetItem(value); item.setToolTip(value + ("\n" + row["reason"] if row["reason"] else ""))
                self.table.setItem(index, column, item)

    def closeEvent(self, event):
        if self.controller.close_held:
            self.controller.cancel(); event.ignore()
        else:
            self.timer.stop(); event.accept()


def create_shared_settings_workspace(controller, parent=None, *, submit=None):
    """One scrollable settings task; host places it in existing navigation."""
    scroll = QScrollArea(parent); scroll.setWidgetResizable(True)
    scroll.setWidget(SharedSettingsPanel(controller, submit=submit))
    return scroll
