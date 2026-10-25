"""Modeless chat presentation. Host owns backend admission and worker lifetime."""
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import (QDialog, QVBoxLayout, QHBoxLayout, QLabel,
    QComboBox, QTextEdit, QPlainTextEdit, QPushButton)
from suite_core.manager.ui_cards import compact_action


class ModelChatPanel(QDialog):
    def __init__(self, controller, targets=(), parent=None):
        super().__init__(parent)
        self.controller = controller
        self._closing = False
        self._rendered = None
        self.setWindowTitle("Chat with a local model")
        self.resize(760, 680)
        layout = QVBoxLayout(self)
        layout.setSpacing(12)
        title = QLabel("Chat with a local model")
        title.setObjectName("pageHeading")
        heading_font = title.font()
        heading_font.setPointSize(16)
        heading_font.setBold(True)
        title.setFont(heading_font)
        layout.addWidget(title)
        note = QLabel("Choose a configured model. Messages stay in memory and are cleared when this panel closes.")
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
        self.send = compact_action(QPushButton("Send"))
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
            self.controller.send(self.prompt.toPlainText())
        except Exception as error:
            # Never display raw backend errors/credentials or response bodies.
            self.status.setText(str(error) if type(error) is ValueError else "Chat worker unavailable. Check the backend and retry.")
            return
        self.prompt.clear()
        self.render()

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
