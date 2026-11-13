"""Local Manager appearance editor; shared module settings keep their authority."""
from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QApplication, QCheckBox, QComboBox, QFormLayout,
    QGroupBox, QHBoxLayout, QLabel, QPushButton, QVBoxLayout)
from core.manager_appearance_settings import AppearanceSettingsError
from qt_app.theme import THEMES, apply_theme
from qt_app.tooltips import apply_tooltip_policy
from .ui_cards import compact_action


def restore_appearance(app, store, *, theme="system", color_blind_mode=False):
    """Normal entry uses this same bounded load; invalid settings never get replaced."""
    snapshot = error = None
    show_tooltips = True
    if store is not None:
        try:
            snapshot = store.observe()
        except AppearanceSettingsError as problem:
            error = str(problem)
        if snapshot is not None:
            theme, color_blind_mode = snapshot.theme, snapshot.color_blind_mode
            show_tooltips = snapshot.show_tooltips
    apply_theme(app, theme, color_blind_mode)
    apply_tooltip_policy(app, show_tooltips)
    return snapshot, error, theme, color_blind_mode, show_tooltips


class AppearancePanel(QGroupBox):
    applied = Signal()

    def __init__(self, store=None, snapshot=None, *, error=None, theme="system",
                 color_blind_mode=False, show_tooltips=True, parent=None):
        super().__init__("Appearance and accessibility", parent)
        self.setObjectName("managerAppearancePanel")
        self.store, self.snapshot = store, snapshot
        layout = QVBoxLayout(self)
        hint = QLabel("Choose how Suite Manager looks. Save applies your choices now and remembers them for next time. Other modules keep their own settings.")
        hint.setWordWrap(True); layout.addWidget(hint)
        form = QFormLayout()
        self.theme = QComboBox(); self.theme.setObjectName("managerTheme")
        for key, label in THEMES.items(): self.theme.addItem(label, key)
        form.addRow("Color theme", self.theme)
        self.palette = QCheckBox("Use color-blind-friendly status colors and border patterns")
        self.palette.setObjectName("managerColorBlind")
        self.tooltips = QCheckBox("Show contextual help tooltips")
        self.tooltips.setObjectName("managerTooltips")
        form.addRow(self.palette); form.addRow(self.tooltips); layout.addLayout(form)
        self.status = QLabel(); self.status.setWordWrap(True); self.status.setTextFormat(Qt.PlainText)
        self.status.setObjectName("managerAppearanceStatus"); layout.addWidget(self.status)
        buttons = QHBoxLayout()
        self.save_button = compact_action(QPushButton("Save appearance"))
        self.save_button.setObjectName("managerAppearanceSave"); self.save_button.setProperty("primary", True)
        self.reload_button = compact_action(QPushButton("Reload saved choices"))
        self.reload_button.setObjectName("managerAppearanceReload")
        buttons.addWidget(self.save_button); buttons.addWidget(self.reload_button); buttons.addStretch(1)
        layout.addLayout(buttons)
        self._fallback = dict(ui_theme=theme, color_blind_mode=color_blind_mode, show_tooltips=show_tooltips)
        self._render()
        self.status.setText("Saved choices loaded." if snapshot else
            "Settings could not be read. Restore access, then reload saved choices." if error else
            "Appearance changes are unavailable in this session." if store is None else "Choose your appearance.")
        self.save_button.clicked.connect(self.save)
        self.reload_button.clicked.connect(self.reload)
        self.theme.currentIndexChanged.connect(self._dirty)
        self.palette.toggled.connect(self._dirty); self.tooltips.toggled.connect(self._dirty)

    def _render(self):
        values = self.snapshot.values if self.snapshot else self._fallback
        controls = ((self.theme, "ui_theme"), (self.palette, "color_blind_mode"), (self.tooltips, "show_tooltips"))
        for control, key in controls:
            control.blockSignals(True)
            if key == "ui_theme": control.setCurrentIndex(max(0, self.theme.findData(values[key])))
            else: control.setChecked(values[key])
            control.blockSignals(False)
            managed = self.snapshot is not None and key in self.snapshot.managed_fields
            control.setEnabled(self.snapshot is not None and not managed)
            control.setToolTip("Controlled by shared settings; change it through that workflow." if managed else "")
        self.save_button.setEnabled(False)
        self.reload_button.setEnabled(self.store is not None)

    def _choices(self):
        return dict(ui_theme=self.theme.currentData(), color_blind_mode=self.palette.isChecked(),
                    show_tooltips=self.tooltips.isChecked())

    def _dirty(self):
        changed = self.snapshot is not None and self._choices() != self.snapshot.values
        self.save_button.setEnabled(changed)
        if changed: self.status.setText("Unsaved appearance choices. Save to apply them.")
        elif self.snapshot is not None: self.status.setText("Current choices match the saved appearance.")

    def _apply(self):
        app = QApplication.instance()
        apply_theme(app, self.snapshot.theme, self.snapshot.color_blind_mode)
        apply_tooltip_policy(app, self.snapshot.show_tooltips)
        self.applied.emit()

    def save(self):
        if self.store is None or self.snapshot is None: return
        changes = {key: value for key, value in self._choices().items() if value != self.snapshot.values[key]}
        if not changes: return
        try:
            saved = self.store.save(changes, expected=self.snapshot)
        except AppearanceSettingsError as error:
            self._render()
            self.status.setText("Appearance was not saved; your current appearance is unchanged. "
                + ("Settings changed elsewhere. Reload saved choices before trying again."
                   if str(error) == "settings_changed" else
                   "Restore settings access or resolve shared-settings control, then reload and try again."))
            return
        self.snapshot = saved
        self._apply(); self._render(); self.status.setText("Appearance saved and applied.")

    def reload(self):
        if self.store is None: return
        try:
            saved = self.store.observe()
        except AppearanceSettingsError:
            self.status.setText("Saved choices could not be read. Your current appearance is unchanged. Restore settings access and try again.")
            return
        self.snapshot = saved
        self._apply(); self._render(); self.status.setText("Saved appearance reloaded and applied.")
