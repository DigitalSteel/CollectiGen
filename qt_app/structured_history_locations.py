"""Explicit global-data history enrollment inside the shared Setup folder editor."""
from copy import deepcopy

from PySide6.QtCore import Signal, Qt
from PySide6.QtWidgets import (QWidget, QFormLayout, QLabel, QLineEdit,
    QHBoxLayout, QPushButton, QCheckBox, QFileDialog)

from core.settings import DEFAULT_SETTINGS_PATH
from core.structured_history_configuration import (DOMAIN_LABELS, configuration,
    authority_signature, stage_binding, StructuredHistoryConfigurationService)
from .components import compact_button


class StructuredHistoryLocationsEditor(QWidget):
    availability_changed = Signal(bool)

    def __init__(self, settings, parent=None, *, settings_path=None):
        super().__init__(parent)
        self.settings = settings
        self.settings_path = settings_path or DEFAULT_SETTINGS_PATH
        form = QFormLayout(self)
        form.setContentsMargins(0, 0, 0, 0)
        note = QLabel("Keep saved history for selected global data in this profile. "
            "This is separate from image history. Choose private storage on this computer; "
            "saved policies and collections may contain private information.")
        note.setWordWrap(True)
        form.addRow(note)
        self.domains = {}
        for domain, label in DOMAIN_LABELS.items():
            field = QCheckBox(label)
            field.setAccessibleName("Record history for " + label)
            self.domains[domain] = field
            form.addRow(field)
        self.zone = QLineEdit()
        self.history = QLineEdit()
        self._browse_buttons = []
        for field, label in ((self.zone, "Private storage zone"), (self.history, "Global history folder")):
            field.setAccessibleName(label)
            field.setPlaceholderText("Choose an explicit absolute folder")
            row = QHBoxLayout()
            row.addWidget(field, 1)
            button = compact_button(QPushButton("Browse…"))
            button.setAccessibleName("Browse " + label.lower())
            button.clicked.connect(lambda checked=False, target=field: self._browse(target))
            self._browse_buttons.append(button)
            row.addWidget(button)
            form.addRow(label, row)
        self.private = QCheckBox("This zone is private, machine-local, and is not shared or synchronized")
        self.enabled = QCheckBox("Enable saved history for the selected global data")
        form.addRow(self.private)
        form.addRow(self.enabled)
        self.state = QLabel()
        self.state.setTextFormat(Qt.PlainText)
        self.state.setWordWrap(True)
        form.addRow(self.state)
        self.retained = QLabel()
        self.retained.setWordWrap(True)
        form.addRow(self.retained)

    def _browse(self, field):
        selected = QFileDialog.getExistingDirectory(self, "Choose " + field.accessibleName(), field.text())
        if selected:
            field.setText(selected)

    def values(self):
        return (self.zone.text(), self.history.text(), self.private.isChecked(),
            self.enabled.isChecked(), tuple(key for key, field in self.domains.items() if field.isChecked()))

    def reload(self):
        self.expected = deepcopy(getattr(self.settings, "structured_history_storage", {}))
        self.expected_authority = authority_signature(self.settings, self.settings_path) if self.settings else ""
        self.error = ""
        try:
            parsed = configuration(self.expected)
        except (ValueError, TypeError) as exc:
            self.error = str(exc)
            parsed = {"bindings": {}, "zones": {}}
        binding = next((row for row in parsed["bindings"].values() if not row["retired"]), None)
        zone = parsed["zones"].get(binding["zone_id"], {}) if binding else {}
        self.zone.setText(zone.get("zone_root", ""))
        self.history.setText(binding["history_root"] if binding else "")
        self.private.setChecked(bool(binding))
        self.enabled.setChecked(bool(binding and binding["enabled"]))
        for key, field in self.domains.items():
            field.setChecked(bool(binding and binding["domains"].get(key, {}).get("enabled")))
        self.baseline = self.values()
        if self.error:
            self.state.setText(self.error)
        elif self.settings:
            resolver = StructuredHistoryConfigurationService(self.settings, settings_path=self.settings_path)
            states = []
            for key, label in DOMAIN_LABELS.items():
                result = resolver.resolve(key)
                states.append(label + " — " + result.status.replace("_", " ").title() + ": " + result.reason)
            self.state.setText("Saved configuration:\n" + "\n".join(states))
        count = sum(row["retired"] or not row["enabled"] for row in parsed["bindings"].values())
        self.retained.setText("Retained stores cannot be interpreted by this version. Saved configuration remains unchanged."
            if self.error else f"{count} disabled or retired stores remain protected. "
            "Changing the profile or storage location starts a separate store. Existing history stays in place; nothing is migrated or deleted.")
        for field in (*self.domains.values(), self.zone, self.history, self.private, self.enabled, *self._browse_buttons):
            field.setEnabled(not self.error)
        self.availability_changed.emit(not self.error)

    def staged_configuration(self):
        if self.error:
            raise ValueError(self.error)
        if self.values() == self.baseline:
            return deepcopy(self.expected)
        zone, history, private, enabled, domains = self.values()
        return stage_binding(self.settings, self.expected, self.settings_path,
            zone.strip(), history.strip(), domains=domains, private_local=private, enabled=enabled)
