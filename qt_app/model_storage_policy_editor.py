"""Staged Fast/Archive preferences for the canonical Folder Locations editor."""
from copy import deepcopy

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QWidget, QVBoxLayout, QHBoxLayout, QFormLayout,
    QComboBox, QLineEdit, QDoubleSpinBox, QLabel, QPushButton, QFileDialog,
    QTreeWidget, QTreeWidgetItem, QInputDialog, QCheckBox)

from core.model_resource_registry import RESOURCE_ROLES
from core.model_organization import FAMILY_DESTINATIONS
from core.model_storage_policy import load_policy
from .components import compact_button, guard_scrollable_value_controls


POLICY_LABELS = (
    ("Load into Fast Storage When Needed", "on_demand"),
    ("Keep in Fast Storage (Keep Warm)", "keep_fast"),
    ("Keep in Archive Until I Choose to Prepare", "archive_only"),
)


class ModelStoragePolicyEditor(QWidget):
    """No IO or publication: the parent saves destinations and policy together."""
    mode_changed = Signal(str)

    def __init__(self, parent=None, *, role_labels=None):
        super().__init__(parent)
        self.role_labels = role_labels or {}
        self._baseline = {}
        self._numbers = {}
        self._overrides = {}
        self._loading = False
        self._tier_defaults_seeded = False
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setAlignment(Qt.AlignTop)
        form = QFormLayout()
        layout.addLayout(form)
        self.mode = QComboBox()
        self.mode.addItem("One model storage location", "single")
        self.mode.addItem("Archive + Fast Storage", "tiered")
        self.mode.setAccessibleName("Model storage mode")
        form.addRow("Storage mode", self.mode)
        self.tier_fields = QWidget()
        tier = QFormLayout(self.tier_fields)
        tier.setContentsMargins(0, 0, 0, 0)
        self.fast_root = QLineEdit()
        self.fast_root.setAccessibleName("Fast Storage folder")
        self.fast_root.setPlaceholderText("Choose a separate SSD folder for prepared copies")
        browse_row = QHBoxLayout()
        browse_row.addWidget(self.fast_root, 1)
        browse = compact_button(QPushButton("Browse…"))
        browse.clicked.connect(self._browse)
        browse_row.addWidget(browse)
        tier.addRow("Fast folder", browse_row)
        self.limit = self._size_control("Fast Storage capacity limit")
        self.reserve = self._size_control("Minimum free disk space")
        tier.addRow("Cache capacity", self.limit)
        self.capacity_note = QLabel("Maximum space for verified Fast copies, not Archive originals or GPU memory. Choose a positive limit for this SSD.")
        self.capacity_note.setWordWrap(True)
        tier.addRow(self.capacity_note)
        tier.addRow("Leave disk space free", self.reserve)
        self.reserve_note = QLabel("Minimum free space to preserve when preparing new copies. This is separate from cache capacity.")
        self.reserve_note.setWordWrap(True)
        tier.addRow(self.reserve_note)
        self.automatic_cleanup = QComboBox()
        self.automatic_cleanup.setAccessibleName("When Fast storage needs space")
        self.automatic_cleanup.addItem("Ask me to review cleanup", "off")
        self.automatic_cleanup.addItem("Make room using least-used models", "least_used")
        tier.addRow("When Fast storage needs space", self.automatic_cleanup)
        self.cleanup_note = QLabel("Optional: before preparing a complete supported model set, remove eligible Fast copies "
            "using recorded successful-use counts and recency. Keep Warm models, active or manual pins, uncertain files "
            "and Archive originals stay protected. Unknown use history needs manual review. Saving this choice deletes nothing; "
            "the next preparation shows the exact copies it would remove.")
        self.cleanup_note.setWordWrap(True); self.cleanup_note.setTextFormat(Qt.PlainText)
        tier.addRow(self.cleanup_note)
        self.automatic_cleanup.currentIndexChanged.connect(self._cleanup_changed)
        self.default_policy = self._policy_combo()
        self.default_policy.setAccessibleName("Default model storage policy")
        tier.addRow("All models by default", self.default_policy)
        note = QLabel("Preparing verifies Fast copies; Keep Warm retains them between sessions. "
            "Saving preferences moves or deletes nothing. SwarmUI folder changes have a separate review.")
        note.setWordWrap(True)
        tier.addRow(note)
        self.advanced_button = compact_button(QPushButton("Advanced Storage Options"))
        self.advanced_button.setCheckable(True)
        tier.addRow(self.advanced_button)
        self.advanced = QWidget()
        advanced = QVBoxLayout(self.advanced)
        advanced.setContentsMargins(0, 0, 0, 0)
        self.no_fixed_cap = QCheckBox("No fixed cache cap")
        self.no_fixed_cap.setAccessibleName("No fixed cache cap for Fast copies")
        self.no_fixed_cap.toggled.connect(self._cache_cap_changed)
        advanced.addWidget(self.no_fixed_cap)
        no_cap_note = QLabel("Fast copies may grow until the SSD's actual free space or the saved reserve stops preparation. Exact cleanup review and protected copies still apply.")
        no_cap_note.setWordWrap(True)
        advanced.addWidget(no_cap_note)
        explanation = QLabel("Priority: individual model → family → model type → default. "
            "Any saved individual overrides take priority. A Keep in Fast Storage policy prevents cleanup; "
            "it does not immediately copy the model.")
        explanation.setWordWrap(True)
        advanced.addWidget(explanation)
        self.rows = QTreeWidget()
        self.rows.setRootIsDecorated(False)
        self.rows.setHeaderLabels(("Scope", "Applies to", "Placement policy"))
        self.rows.setAccessibleName("Family and type storage policies")
        self.rows.setMinimumHeight(130)
        self.rows.setMaximumHeight(230)
        self.rows.setColumnWidth(0, 110)
        self.rows.setColumnWidth(1, 190)
        advanced.addWidget(self.rows)
        actions = QHBoxLayout()
        for label, group in (("Add Family Policy…", "family_policies"),
                             ("Add Model Type Policy…", "role_policies")):
            button = compact_button(QPushButton(label))
            button.clicked.connect(lambda checked=False, key=group: self._add(key))
            actions.addWidget(button)
        self.remove_button = compact_button(QPushButton("Remove Selected Policy"))
        self.remove_button.clicked.connect(self._remove)
        self.rows.itemSelectionChanged.connect(lambda: self.remove_button.setEnabled(self.rows.currentItem() is not None))
        actions.addWidget(self.remove_button)
        advanced.addLayout(actions)
        self.resource_summary = QLabel()
        self.resource_summary.setWordWrap(True)
        advanced.addWidget(self.resource_summary)
        self.advanced.hide()
        self.advanced_button.target = self.advanced
        self.advanced_button.toggled.connect(self.advanced.setVisible)
        tier.addRow(self.advanced)
        layout.addWidget(self.tier_fields)
        self.mode.currentIndexChanged.connect(self._mode_changed)
        guard_scrollable_value_controls(self)
        self.load({})

    @staticmethod
    def _size_control(name):
        control = QDoubleSpinBox()
        control.setRange(0, 1048576)
        control.setDecimals(3)
        control.setSuffix(" GiB")
        control.setAccessibleName(name)
        return control

    @staticmethod
    def _policy_combo(value="on_demand"):
        control = QComboBox()
        for label, key in POLICY_LABELS:
            control.addItem(label, key)
        control.setCurrentIndex(control.findData(value))
        return control

    def _cache_cap_changed(self, unlimited: bool) -> None:
        self.limit.setEnabled(not unlimited)
        self.capacity_note.setText(
            "No fixed cap is applied. The last positive limit is kept for "
            "switching back to a fixed cap."
            if unlimited else
            "Maximum space for verified Fast copies, not Archive originals "
            "or GPU memory. Choose a positive limit for this SSD."
        )

    def _mode_changed(self, *_):
        mode = self.mode.currentData()
        if mode == "tiered" and not self._loading and not self._tier_defaults_seeded:
            # Seed a usable first-time configuration. Saved numbers, including
            # an intentionally small reserve, are never silently changed.
            if self.limit.value() == 0:
                self.limit.setValue(100)
            if "minimum_free_bytes" not in self._baseline and self.reserve.value() == 0.25:
                self.reserve.setValue(20)
            self._tier_defaults_seeded = True
        self.tier_fields.setVisible(mode == "tiered")
        self.mode_changed.emit(mode)

    def _browse(self):
        selected = QFileDialog.getExistingDirectory(self, "Choose Fast Storage Folder", self.fast_root.text())
        if selected:
            self.fast_root.setText(selected)

    def _cleanup_changed(self, *_):
        self.cleanup_note.setVisible(self.automatic_cleanup.currentData() == "least_used")

    @staticmethod
    def validate(workspace):
        policy = load_policy(workspace)
        if len(policy.family_policies) + len(policy.role_policies) > 256:
            raise ValueError("Too many storage policy overrides for this editor; preserve them for review.")
        return policy

    def load(self, workspace):
        policy = self.validate(workspace)
        self._baseline = deepcopy(workspace.get("storage_policy", {}))
        self._loading = True
        self.mode.setCurrentIndex(self.mode.findData(policy.mode))
        self.fast_root.setText(policy.fast_root)
        for name, control, value in (("cache_limit_bytes", self.limit, policy.cache_limit_bytes),
                                      ("minimum_free_bytes", self.reserve, policy.minimum_free_bytes)):
            control.setValue(value / 2**30)
            self._numbers[name] = (control.value(), value)
        self.default_policy.setCurrentIndex(self.default_policy.findData(policy.default_policy))
        self.automatic_cleanup.setCurrentIndex(self.automatic_cleanup.findData(policy.automatic_cleanup))
        self.no_fixed_cap.setChecked(policy.cache_limit_mode == "unlimited")
        self._cleanup_changed()
        self._overrides = {"family_policies": dict(policy.family_policies), "role_policies": dict(policy.role_policies)}
        self.resource_summary.setText(f"{len(policy.resource_policies):,} individual model policies retained.")
        self._render()
        self._loading = False
        self._tier_defaults_seeded = policy.mode == "tiered"
        self._mode_changed()

    def values(self):
        """Return the edited subset, preserving exact byte counts until edited."""
        result = {"mode": self.mode.currentData(), "fast_root": self.fast_root.text().strip(),
                  "default_policy": self.default_policy.currentData()}
        if self._baseline.get("schema_version") == 2 or self.no_fixed_cap.isChecked():
            result["schema_version"] = 2
            result["cache_limit_mode"] = "unlimited" if self.no_fixed_cap.isChecked() else "fixed"
        # Preserve an absent legacy field until the user changes this opt-in.
        if "automatic_cleanup" in self._baseline or self.automatic_cleanup.currentData() != "off":
            result["automatic_cleanup"] = self.automatic_cleanup.currentData()
        for name, control in (("cache_limit_bytes", self.limit), ("minimum_free_bytes", self.reserve)):
            shown, original = self._numbers[name]
            result[name] = original if control.value() == shown else round(control.value() * 2**30)
        result.update(deepcopy(self._overrides))
        return result

    def set_override(self, group, key, policy):
        if group not in self._overrides or policy not in {key for _, key in POLICY_LABELS}:
            raise ValueError("Choose a supported family or type policy.")
        if group == "role_policies" and key not in RESOURCE_ROLES:
            raise ValueError("Choose a supported model type.")
        proposed = self.values()
        proposed[group][key] = policy
        proposed.setdefault("schema_version", 1)
        # Family canonicalization, duplicate aliases and bounds remain core-owned.
        candidate = load_policy({"storage_policy": {**proposed, "mode": "single"}})
        if len(candidate.family_policies) + len(candidate.role_policies) > 256:
            raise ValueError("Use at most 256 family and model type policy overrides.")
        self._overrides[group][key] = policy
        self._render()

    def _render(self):
        self.rows.clear()
        for group, values in self._overrides.items():
            for key, policy in sorted(values.items()):
                label = key.replace(":", " · ") if group == "family_policies" else self.role_labels.get(key, key)
                item = QTreeWidgetItem(["Family" if group == "family_policies" else "Model type", label, ""])
                item.setData(0, Qt.UserRole, (group, key))
                self.rows.addTopLevelItem(item)
                combo = self._policy_combo(policy)
                combo.setAccessibleName(label + " storage policy")
                combo.currentIndexChanged.connect(lambda _, g=group, k=key, c=combo: self._overrides[g].__setitem__(k, c.currentData()))
                self.rows.setItemWidget(item, 2, combo)
        self.remove_button.setEnabled(False)
        guard_scrollable_value_controls(self.rows)

    def _add(self, group):
        choices = list(FAMILY_DESTINATIONS) if group == "family_policies" else list(RESOURCE_ROLES)
        choices = [key for key in choices if key not in self._overrides[group]]
        labels = [key.replace(":", " · ") if group == "family_policies" else self.role_labels.get(key, key) for key in choices]
        if not choices:
            return
        selected, accepted = QInputDialog.getItem(self, "Add Placement Policy", "Applies to", labels, 0, False)
        if accepted:
            self.set_override(group, choices[labels.index(selected)], self.default_policy.currentData())

    def _remove(self):
        item = self.rows.currentItem()
        if item:
            group, key = item.data(0, Qt.UserRole)
            del self._overrides[group][key]
            self._render()
