"""Model Chat and local external client connection presentation."""
from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QApplication, QComboBox, QFileDialog, QTextEdit, QPlainTextEdit, QPushButton,
    QDialogButtonBox, QFormLayout, QLineEdit)
from suite_core.manager.ui_cards import compact_action


def show_external_client_connection(parent, console, settings_path, code_root):
    from .assistant_discovery import connection_recipe

    dialog = QDialog(parent)
    dialog.setWindowTitle("Connect an external assistant")
    dialog.resize(740, 470)
    layout = QVBoxLayout(dialog)
    note = QLabel("Connect Codex, Hermes, or a terminal to this CollectiGen installation. "
                  "Discovery can explain workflows and check which applications are installed.")
    note.setWordWrap(True)
    layout.addWidget(note)
    if not console.is_file():
        note.setText("Open this from a packaged Manager with CollectiGenAssist.exe beside it to get connection details.")
        buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        dialog.exec()
        return

    form = QFormLayout()
    client = QComboBox()
    client.addItem("PowerShell", "powershell")
    client.addItem("Codex (MCP)", "codex_toml")
    client.addItem("Hermes (MCP)", "hermes_yaml")
    form.addRow("&Client", client)
    layout.addLayout(form)
    python_row = QHBoxLayout()
    choose_python = compact_action(QPushButton("Choose Python with MCP SDK…"))
    python_path = QLabel()
    python_path.setTextFormat(Qt.TextFormat.PlainText)
    python_row.addWidget(choose_python)
    python_row.addWidget(python_path, 1)
    layout.addLayout(python_row)
    details = QPlainTextEdit()
    details.setAccessibleName("Connection details")
    details.setReadOnly(True)
    layout.addWidget(details, 1)
    status = QLabel()
    status.setWordWrap(True)
    layout.addWidget(status)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Close)
    copy = buttons.addButton("Copy connection details", QDialogButtonBox.ButtonRole.ActionRole)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    scope = ["--manager-settings", str(settings_path), "--code-root", str(code_root)]
    selected_python = None

    def refresh():
        target = client.currentData()
        show_mcp = target != "powershell"
        choose_python.setVisible(show_mcp)
        python_path.setVisible(show_mcp)
        recipe = connection_recipe(console, scope, selected_python if show_mcp else None)
        if show_mcp:
            mcp = recipe["mcp"]
            text = mcp.get(target, "")
            status.setText({
                "recipe_available": "Paste into your trusted local client configuration.",
                "python_unavailable": "That Python executable is unavailable. Choose it again or choose another.",
                "adapter_unavailable": "The bundled MCP adapter is unavailable. Check the Manager package.",
            }.get(mcp["state"], "Choose an existing Python executable with the official MCP SDK installed."))
        else:
            text = recipe["cli_check"]["powershell"]
            status.setText("Paste into PowerShell to check discovery. The command makes no changes.")
        details.setPlainText(text)
        copy.setEnabled(bool(text))

    def select_python():
        from pathlib import Path

        nonlocal selected_python
        selected, _ = QFileDialog.getOpenFileName(dialog, "Choose an existing Python executable", "", "Python (python.exe)")
        if selected:
            selected_python = Path(selected)
            python_path.setText(selected_python.name)
            python_path.setToolTip(str(selected_python))
            refresh()

    def copy_details():
        QApplication.clipboard().setText(details.toPlainText())
        status.setText("Connection details copied.")

    client.currentIndexChanged.connect(refresh)
    choose_python.clicked.connect(select_python)
    copy.clicked.connect(copy_details)
    refresh()
    dialog.exec()


class ModelChatPanel(QDialog):
    def __init__(self, controller, targets=(), parent=None, *, review_endpoint=None):
        super().__init__(parent)
        self.controller = controller
        self._closing = False
        self._rendered = None
        self.review_endpoint = review_endpoint
        self.public_context = None
        title_text = "CollectiGen help with vLLM" if review_endpoint else "Chat with a local model"
        self.setWindowTitle(title_text)
        self.resize(760, 680)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        title = QLabel(title_text)
        title.setObjectName("pageHeading")
        heading_font = title.font()
        heading_font.setPointSize(16)
        heading_font.setBold(True)
        title.setFont(heading_font)
        layout.addWidget(title)
        note = QLabel(
            "Ask about CollectiGen. Review the exact messages before sending to the selected server."
            if review_endpoint else
            "Choose a configured model. Messages stay in memory and are cleared when this panel closes."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        row = QHBoxLayout()
        label = QLabel("Model:")
        self.models = QComboBox()
        label.setBuddy(self.models)
        row.addWidget(label)
        row.addWidget(self.models, 1)
        self.new_chat = compact_action(QPushButton("New Chat"))
        row.addWidget(self.new_chat)
        if review_endpoint:
            disconnect = compact_action(QPushButton("Disconnect"))
            disconnect.clicked.connect(self.close)
            row.addWidget(disconnect)
        layout.addLayout(row)
        self.transcript = QPlainTextEdit()
        self.transcript.setReadOnly(True)
        self.transcript.setAccessibleName("Chat messages")
        layout.addWidget(self.transcript, 1)
        self.prompt = QTextEdit()
        self.prompt.setAcceptRichText(False)
        self.prompt.setPlaceholderText("Write a message, then choose Send")
        self.prompt.setAccessibleName("Your message")
        self.prompt.setMaximumHeight(130)
        layout.addWidget(self.prompt)
        actions = QHBoxLayout()
        self.status = QLabel()
        self.status.setWordWrap(True)
        actions.addWidget(self.status, 1)
        self.stop = compact_action(QPushButton("Stop"))
        self.send = compact_action(QPushButton("Review request…" if review_endpoint else "Send"))
        if review_endpoint:
            self.local_help = compact_action(QPushButton("View local help…"))
            self.local_help.setEnabled(False)
            self.local_help.clicked.connect(self._show_local_help)
            actions.addWidget(self.local_help)
        self.send.setAutoDefault(False)
        self.send.setShortcut("Ctrl+Return")
        self.send.setToolTip("Send (Ctrl+Enter)")
        self.stop.setAutoDefault(False)
        self.new_chat.setAutoDefault(False)
        actions.addWidget(self.stop)
        actions.addWidget(self.send)
        layout.addLayout(actions)
        self.models.currentIndexChanged.connect(self._select)
        self.prompt.textChanged.connect(self.render)
        self.new_chat.clicked.connect(self._new)
        self.send.clicked.connect(self._send)
        self.stop.clicked.connect(self._stop)
        self.timer = QTimer(self)
        self.timer.setInterval(50)
        self.timer.timeout.connect(self.poll)
        self.set_targets(targets)
        self.timer.start()
        self.render()

    def set_targets(self, targets):
        previous = self.controller.target
        self.models.blockSignals(True)
        self.models.clear()
        self.models.addItem("Choose a configured model", None)
        selected = 0
        for target in targets:
            self.models.addItem(f"{target.model} — {target.backend_id}" + (" (unavailable)" if not target.ready else ""), target)
            if target == previous:
                selected = self.models.count()-1
        self.models.setCurrentIndex(selected)
        self.models.blockSignals(False)
        self._select()

    def _select(self):
        self.controller.select_target(self.models.currentData())
        self.render()

    def _new(self):
        self.controller.new_chat()
        self.prompt.clear()
        self.render()

    def _send(self):
        try:
            question = self.prompt.toPlainText()
            preview = None
            if self.review_endpoint:
                from .assistant_context import help_for_question
                context = help_for_question(self.public_context, question,
                    budget=self.controller.max_context_bytes - len(question.encode("utf-8")))
                preview = self.controller.preview_context(question, context)
                context, question_message = preview.messages
                text = f"CollectiGen context\n{context.text}\n\nYour question\n{question_message.text}"
                subject = f"Review request to {self.review_endpoint}\nModel: {preview.target.model}"
                if not self._show_text(subject, text, send=True):
                    return
            self.controller.send(question, preview=preview)
        except Exception as error:
            # Never display raw backend errors/credentials or response bodies.
            self.status.setText(str(error) if type(error) is ValueError else "Chat worker unavailable. Check the backend and retry.")
            return
        self.prompt.clear()
        self.render()

    def set_public_context(self, context, target):
        self.public_context = context
        self.local_help.setEnabled(True)
        if target is not None:
            self.set_targets((target,))
            self.models.setCurrentIndex(1)
        else:
            self.controller.status = "Connection check could not complete. Local help is available."
            self.render()
        self.prompt.setFocus()

    def _show_local_help(self):
        from .assistant_context import help_for_question
        self._show_text("Local CollectiGen help", help_for_question(
            self.public_context, self.prompt.toPlainText()))

    def _show_text(self, title, text, *, send=False):
        dialog = QDialog(self)
        dialog.setWindowTitle(title.splitlines()[0])
        dialog.resize(640, 480)
        layout = QVBoxLayout(dialog)
        label = QLabel(title)
        label.setTextFormat(Qt.TextFormat.PlainText)
        label.setWordWrap(True)
        layout.addWidget(label)
        preview = QPlainTextEdit(text)
        preview.setAccessibleName("Request messages" if send else "Local workflow help")
        preview.setReadOnly(True)
        layout.addWidget(preview)
        buttons = QDialogButtonBox(
            QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel
            if send else QDialogButtonBox.StandardButton.Close
        )
        if send:
            buttons.button(QDialogButtonBox.StandardButton.Ok).setText("Send to vLLM")
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setDefault(True)
        buttons.accepted.connect(dialog.accept)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)
        if send:
            buttons.button(QDialogButtonBox.StandardButton.Cancel).setFocus()
        return dialog.exec() == QDialog.DialogCode.Accepted

    def _stop(self):
        self.controller.stop()
        self.render()

    def poll(self):
        self.controller.poll()
        self.render()
        if self._closing and not self.controller.close_held:
            self.timer.stop()
            self.close()

    def render(self):
        messages = tuple(self.controller.messages)
        if messages != self._rendered:
            bar = self.transcript.verticalScrollBar()
            follow = bar.value() >= bar.maximum()-4
            position = bar.value()
            text = "\n\n".join(("You" if m.role == "user" else "Assistant") +
                (f" [{m.state}]" if m.state != "complete" else "") + "\n" + m.text for m in messages)
            self.transcript.setPlainText(text)
            bar.setValue(bar.maximum() if follow else position)
            self._rendered = messages
        self.status.setText(self.controller.status)
        self.send.setEnabled(self.controller.can_send and bool(self.prompt.toPlainText().strip()))
        self.stop.setEnabled(self.controller.busy and not self._closing)
        self.models.setEnabled(not self.controller.close_held and not self._closing)
        self.prompt.setEnabled(not self._closing)
        self.new_chat.setEnabled(not self._closing)

    def closeEvent(self, event):
        self._closing = True
        self.controller.begin_close()
        self.prompt.clear()
        self.transcript.clear()
        self._rendered = None
        if self.controller.close_held:
            event.ignore()
            self.render()
        else:
            self.timer.stop()
            event.accept()

    def reject(self):
        # Escape must use the same retained cancellation/drain path as the X.
        self.close()

    def accept(self):
        self.close()


def review_vllm_connection(parent):
    dialog = QDialog(parent)
    dialog.setWindowTitle("Connect to vLLM for CollectiGen help")
    layout = QVBoxLayout(dialog)
    note = QLabel("Choose an existing server and model. Opening help checks the model list. Review each question before sending it.")
    note.setWordWrap(True)
    layout.addWidget(note)
    form = QFormLayout()
    server, model, token = QLineEdit(), QLineEdit(), QLineEdit()
    server.setPlaceholderText("http://127.0.0.1:8000/v1")
    token.setEchoMode(QLineEdit.EchoMode.Password)
    for label, control in (("&Server endpoint", server), ("&Model name", model),
                           ("&API token (optional, this session)", token)):
        control.setMaxLength(4096)
        form.addRow(label, control)
    layout.addLayout(form)
    buttons = QDialogButtonBox(QDialogButtonBox.StandardButton.Ok | QDialogButtonBox.StandardButton.Cancel)
    open_help = buttons.button(QDialogButtonBox.StandardButton.Ok)
    open_help.setText("Open contextual help")
    open_help.setEnabled(False)
    def refresh():
        open_help.setEnabled(bool(server.text().strip() and model.text().strip()))
    server.textChanged.connect(refresh)
    model.textChanged.connect(refresh)
    buttons.accepted.connect(dialog.accept)
    buttons.rejected.connect(dialog.reject)
    layout.addWidget(buttons)
    accepted = dialog.exec() == QDialog.DialogCode.Accepted
    result = (server.text().strip(), model.text().strip(), token.text() or None) if accepted else None
    token.clear()
    return result
