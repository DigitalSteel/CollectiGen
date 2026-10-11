"""Qt projection of one existing settings transaction; no settings or network ownership."""
def create_setup_dialog(controller, parent):
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtWidgets import QDialog, QLabel, QLineEdit, QCheckBox, QPushButton, QHBoxLayout, QDialogButtonBox, QWidget, QToolButton
    from suite_core.manager.ui_cards import workspace_layout, workspace_heading, disclosure_panel

    class SetupDialog(QDialog):
        def __init__(self):
            super().__init__(parent)
            self.setWindowTitle("Setup local Ollama")
            self.resize(660, 390)
            layout = workspace_layout(self)
            layout.addWidget(workspace_heading("Local Ollama metadata"))
            purpose = QLabel("Save one local connection for Manager metadata. Refresh reads the Ollama version, installed models and loaded models. Save does not contact Ollama.")
            purpose.setWordWrap(True); purpose.setTextFormat(Qt.PlainText)
            layout.addWidget(purpose)
            label = QLabel("&Endpoint (HTTP loopback address and port)")
            self.endpoint = QLineEdit(); label.setBuddy(self.endpoint)
            self.endpoint.setAccessibleName("Local Ollama endpoint")
            self.endpoint.setPlaceholderText("For example: http://127.0.0.1:11434")
            self.endpoint.setMaxLength(2048)
            layout.addWidget(label); layout.addWidget(self.endpoint)
            self.permission = QCheckBox("Allow metadata access to this endpoint")
            self.permission.setAccessibleName("Allow metadata access to the saved local Ollama endpoint")
            layout.addWidget(self.permission)
            self.status = QLabel(); self.status.setWordWrap(True); self.status.setTextFormat(Qt.PlainText)
            self.status.setAccessibleName("Backend setup status")
            layout.addWidget(self.status)
            saved_label = QLabel("Connection status and read-only model information are shown in Applications > Ollama. Library privacy choices remain separate.")
            saved_label.setWordWrap(True); saved_label.setTextFormat(Qt.PlainText)
            layout.addWidget(saved_label)
            review_widget = QWidget(); review = QHBoxLayout(review_widget)
            self.reload = QPushButton("Reload saved values")
            self.use_saved = QPushButton("Use saved values")
            self.retry = QPushButton("Retry connection")
            for control in (self.reload, self.use_saved, self.retry): review.addWidget(control)
            self.saved_options = disclosure_panel("Saved connection options", review_widget)
            layout.addWidget(self.saved_options)
            actions = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
            self.save = actions.button(QDialogButtonBox.Save)
            self.cancel = actions.button(QDialogButtonBox.Cancel)
            self.save.setAutoDefault(False)
            self.save.setAccessibleName("Save connection")
            actions.rejected.connect(self.reject)
            layout.addWidget(actions)
            self.endpoint.textEdited.connect(self.edit)
            self.permission.toggled.connect(self.edit)
            self.reload.clicked.connect(lambda: self.perform(controller.reload))
            self.use_saved.clicked.connect(lambda: self.perform(controller.use_saved))
            self.retry.clicked.connect(lambda: self.perform(controller.activate))
            self.save.clicked.connect(self.save_and_close)
            controller.open(); self.render()
            self.timer = QTimer(self); self.timer.setInterval(250)
            self.timer.timeout.connect(self.render); self.timer.start()
            self.endpoint.setFocus()

        def edit(self, *_):
            controller.edit(self.endpoint.text(), self.permission.isChecked()); self.render()

        def perform(self, action):
            action(); self.render()

        def save_and_close(self):
            if controller.save():
                self.timer.stop()
                self.accept()
            else:
                self.render()

        def render(self):
            for control in (self.endpoint, self.permission): control.blockSignals(True)
            if self.endpoint.text() != controller.draft.endpoint: self.endpoint.setText(controller.draft.endpoint)
            self.permission.setChecked(controller.draft.allowed)
            for control in (self.endpoint, self.permission): control.blockSignals(False)
            for control in (self.endpoint, self.permission): control.setEnabled(not controller.closed)
            self.status.setText("Manager close is pending. Draft editing is paused; Cancel pending close in Manager to continue. Your draft is retained." if controller.closed else controller.message)
            self.save.setEnabled(controller.can_save)
            if controller.hold or controller.activation_pending:
                self.saved_options.findChild(QToolButton).setChecked(True)
            self.reload.setEnabled(not controller.closed)
            self.use_saved.setVisible(controller.dirty and controller.state == "reloaded")
            self.use_saved.setEnabled(controller.opened and not controller.hold and not controller.closed)
            self.retry.setVisible(controller.activation_pending)
            self.retry.setEnabled(not controller.closed)

        def reject(self):
            controller.cancel()
            self.timer.stop()
            super().reject()

        def closeEvent(self, event):
            self.reject(); event.accept()  # closes the editor, never the Manager worker

    return SetupDialog()
