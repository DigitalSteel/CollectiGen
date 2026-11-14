"""Caption choices for one retained Manager image-setup review; no runtime work."""
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QDoubleSpinBox, QFileDialog,
    QFormLayout, QHBoxLayout, QLabel, QLayout, QLineEdit, QPushButton, QScrollArea, QSpinBox,
    QTextEdit, QVBoxLayout, QWidget)

from core.ai_interrogation import PLAIN_LANGUAGE_MODELS
from core.settings import FLORENCE2_MODEL_ID
from core.manager_description_settings import (DescriptionSettingsError, ManagerDescriptionSettings, NUMERIC_FIELDS)


class DescriptionSetupDialog(QDialog):
    def __init__(self, path, parent=None, *, allowed=lambda: True, components_available=False):
        super().__init__(parent)
        self.service = ManagerDescriptionSettings(path)
        self.allowed = allowed
        self.snapshot = self.saved_snapshot = None
        self._picker_open = False
        self.components_requested = False
        self.setWindowTitle("Local Image Description Settings")
        self.resize(720, 580)
        layout = QVBoxLayout(self)
        heading = QLabel("Choose the local model for image suggestions")
        heading.setObjectName("sectionTitle"); heading.setWordWrap(True); layout.addWidget(heading)
        note = QLabel(
            "Reuse your installed caption model. Save returns to the same unfinished Generation Setup. "
            "Opening, saving and returning do not download a model or start analysis. "
            + ("To install missing components or download the selected model, save these choices "
               "and open component setup below. Each download requires a separate confirmation."
               if components_available else "Caption component setup is unavailable in this session. "
               "Saved text and manual authoring remain available."))
        note.setWordWrap(True); layout.addWidget(note)
        body = QWidget(); form = QFormLayout(body)
        form.setSizeConstraint(QLayout.SetMinAndMaxSize)
        self.model = QComboBox()
        for identity, (label, _folder, description) in PLAIN_LANGUAGE_MODELS.items():
            self.model.addItem(label, identity)
            self.model.setItemData(self.model.count() - 1, description, Qt.ToolTipRole)
        form.addRow("Caption model", self.model)
        directory_row = QWidget(); row = QHBoxLayout(directory_row); row.setContentsMargins(0,0,0,0)
        self.directory = QLineEdit(); self.directory.setAccessibleName("Installed caption models parent folder")
        self.browse = QPushButton("Choose Folder…"); self.browse.clicked.connect(self.choose_directory)
        row.addWidget(self.directory,1); row.addWidget(self.browse); form.addRow("Models parent folder",directory_row)
        self.location_note = QLabel(); self.location_note.setTextFormat(Qt.PlainText); self.location_note.setWordWrap(True)
        form.addRow(self.location_note)
        self.model.currentIndexChanged.connect(self.refresh_location)
        self.directory.textChanged.connect(self.refresh_location)
        self.quantization = QComboBox(); self.quantization.addItem("BF16 — full precision", "bf16"); self.quantization.addItem("NF4 — lower memory", "nf4")
        form.addRow("JoyCaption precision", self.quantization)
        self.components_button = QPushButton("Save Choices and Set Up Components...")
        self.components_button.setVisible(components_available)
        self.components_button.clicked.connect(self.save_for_components)
        form.addRow(self.components_button)
        self.expert_button = QPushButton("Advanced Description Controls")
        self.expert_button.setCheckable(True); form.addRow(self.expert_button)
        self.expert = QWidget(); expert_form = QFormLayout(self.expert)
        expert_form.setSizeConstraint(QLayout.SetMinimumSize)
        self.controls = {}
        for key, (label, low, high, step, kind) in NUMERIC_FIELDS.items():
            control = QSpinBox() if kind is int else QDoubleSpinBox()
            control.setRange(low,high); control.setSingleStep(step); control.setAccessibleName(label)
            control.setMinimumHeight(control.fontMetrics().height() + 10)
            expert_form.addRow(label,control); self.controls[key] = control
        self.sample = QCheckBox("Use varied/random wording (sampling)"); expert_form.addRow(self.sample)
        self.prompt = QTextEdit(); self.prompt.setAcceptRichText(False); self.prompt.setMinimumHeight(80); self.prompt.setMaximumHeight(100)
        expert_form.addRow("Default caption prompt",self.prompt)
        self.expert.hide(); self.expert_button.toggled.connect(self.expert.setVisible); form.addRow(self.expert)
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setWidget(body); layout.addWidget(scroll,1)
        self.status = QLabel(); self.status.setTextFormat(Qt.PlainText); self.status.setWordWrap(True); layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.cancel_button = QPushButton("Cancel"); self.cancel_button.clicked.connect(self.reject); actions.addWidget(self.cancel_button)
        self.reload_button = QPushButton("Reload Saved Choices"); self.reload_button.clicked.connect(self.reload); actions.addWidget(self.reload_button)
        actions.addStretch(1)
        self.save_button = QPushButton("Save and Return"); self.save_button.setProperty("primary",True); self.save_button.clicked.connect(self.save)
        actions.addWidget(self.save_button); layout.addLayout(actions)
        self.reload()

    def save_for_components(self):
        self.components_requested = True
        self.save()

    def refresh_location(self):
        item = PLAIN_LANGUAGE_MODELS.get(self.model.currentData())
        self.location_note.setText("Expected model folder: " + str(Path(self.directory.text()) / item[1]) if item else "Choose a caption model.")
        self.quantization.setEnabled(self.model.currentData() != FLORENCE2_MODEL_ID)

    def choose_directory(self):
        if not self.allowed() or self._picker_open:
            return
        self._picker_open = True
        try:
            selected = QFileDialog.getExistingDirectory(self,"Choose Existing Caption Models Parent Folder",self.directory.text())
        finally:
            self._picker_open = False
        if selected and self.allowed():
            self.directory.setText(selected)

    def reload(self):
        if not self.allowed():
            self.status.setText("The Manager session ended. Close this review; saved choices were not changed.")
            self.save_button.setEnabled(False); return
        try:
            self.snapshot = self.service.observe()
            values = self.snapshot.values
        except DescriptionSettingsError:
            self.snapshot = None; self.save_button.setEnabled(False)
            self.status.setText("Saved description settings could not be read. Keep the original file and any recovery records; manual setup text remains available."); return
        self.model.setCurrentIndex(self.model.findData(values["plain_language_model"]))
        self.directory.setText(values["ai_models_directory"])
        self.quantization.setCurrentIndex(self.quantization.findData(values["joycaption_quantization"]))
        for key, control in self.controls.items():control.setValue(values[key])
        self.sample.setChecked(values["joycaption_do_sample"]); self.prompt.setPlainText(values["joycaption_prompt"])
        self.save_button.setEnabled(True); self.status.setText("Saved choices loaded. Nothing has been checked or run.")
        self.refresh_location()

    def save(self):
        if self.snapshot is None or self._picker_open or not self.allowed():
            self.status.setText("The original Manager session is unavailable. Close this review without saving."); return
        values = {key: control.value() for key, control in self.controls.items()}
        values.update(plain_language_model=self.model.currentData(), ai_models_directory=self.directory.text(),
            joycaption_quantization=self.quantization.currentData(), joycaption_prompt=self.prompt.toPlainText(),
            joycaption_do_sample=self.sample.isChecked())
        if values["joycaption_quantization"] != self.snapshot.values["joycaption_quantization"]:
            values["joycaption_quantization_explicit"] = True
        try:
            self.saved_snapshot = self.service.save(values,expected=self.snapshot)
        except DescriptionSettingsError as error:
            messages={"settings_changed":"Saved settings changed elsewhere. Reload the current choices, then review your changes again.",
                "absolute_models_directory_required":"Choose the full models parent folder path.",
                "description_length_order":"Minimum new tokens must not exceed Maximum new tokens."}
            self.status.setText(messages.get(str(error),"Settings were not confirmed saved. Your choices remain here; keep any recovery records and retry or reload.")); return
        self.snapshot = self.saved_snapshot
        self.accept()

    def reject(self):
        if not self._picker_open:super().reject()

    def closeEvent(self,event):
        if self._picker_open:event.ignore()
        else:super().closeEvent(event)
