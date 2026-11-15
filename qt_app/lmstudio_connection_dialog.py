"""Explicit model-list review and separate preferences; no inference or model control."""
from time import monotonic
from threading import Event

from PySide6.QtCore import QThreadPool, QTimer, QSize, Qt
from PySide6.QtWidgets import (
    QApplication, QComboBox, QDialog, QDialogButtonBox, QFormLayout, QHBoxLayout, QLabel, QLineEdit,
    QMessageBox, QProgressBar, QPushButton, QVBoxLayout,
)

from core.lmstudio_connection_settings import SettingsError, SettingsRecoveryRequired
from core.lmstudio_discovery import MAX_ID, ModelInventory, DiscoveryError, _text
from core.lmstudio_transport import _endpoint, discover
from .components import bounded_window_size, compact_button
from .modal_feedback import FeedbackEnabledDialog
from .workers import AsyncCallTask, start_qt_delivery_task


class LmStudioConnectionDialog(FeedbackEnabledDialog):
    """Edit one explicit service; retain submitted writes until settled."""
    feedback_workflow = "LM Studio connection preferences"

    def __init__(self, service, parent=None, *, discovery=None):
        super().__init__(parent)
        self.setWindowTitle("LM Studio Connection")
        self._service = service
        self._discovery = discover if discovery is None else discovery
        self._discovery_cancel = Event()
        self._discovery_generation = 0
        self._submitted_generation = None
        self._observed_endpoint = None
        self._close_requested = None
        self._snapshot = None
        self._base = ("", "")
        self._held = True
        self._task = None
        self._outcome = None
        self._operation = ""
        self._settle = QTimer(self)
        self._settle.setInterval(20)
        self._settle.timeout.connect(self._deliver_settled)
        layout = QVBoxLayout(self)
        note = QLabel("Remember a local LM Studio endpoint and preferred model key. "
            "Analysis through LM Studio is not available yet. This does not change your current AI models.")
        note.setWordWrap(True)
        layout.addWidget(note)
        form = QFormLayout()
        self.endpoint = QLineEdit()
        self.endpoint.setAccessibleName("Local LM Studio endpoint")
        self.endpoint.setPlaceholderText("For example: http://127.0.0.1:1234")
        self.model_key = QLineEdit()
        self.model_key.setAccessibleName("Preferred model key (optional)")
        form.addRow("Local server address", self.endpoint)
        form.addRow("Preferred model key (optional)", self.model_key)
        layout.addLayout(form)
        hint = QLabel("Copy the server address from LM Studio. Only this computer is supported, "
            "with an explicit HTTP address and port, for example http://127.0.0.1:1234. "
            "Leave the preferred key blank unless you know its exact value. This only remembers your choice; "
            "Refresh Models only reads the local model list. No model is loaded or credential stored.")
        hint.setWordWrap(True)
        layout.addWidget(hint)
        refresh_row = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh Models")
        self.cancel_button = QPushButton("Cancel Refresh")
        refresh_row.addWidget(self.refresh_button)
        refresh_row.addWidget(self.cancel_button)
        refresh_row.addStretch(1)
        layout.addLayout(refresh_row)
        self.models = QComboBox()
        self.models.setAccessibleName("Models reported by the selected local server")
        self.models.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.models.setMinimumContentsLength(24)
        self.models.setMaxVisibleItems(8)
        layout.addWidget(self.models)
        self.model_status = QLabel("Refresh to inspect models. Saved preferences do not establish server readiness.")
        self.model_status.setWordWrap(True)
        layout.addWidget(self.model_status)
        layout.addStretch(1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Connection preference status")
        layout.addWidget(self.status)
        self.activity = QProgressBar()
        self.activity.setRange(0, 0)
        self.activity.setTextVisible(False)
        layout.addWidget(self.activity)
        self.buttons = QDialogButtonBox()
        self.reload_button = self.buttons.addButton("Reload", QDialogButtonBox.ActionRole)
        self.forget_button = self.buttons.addButton("Forget Connection", QDialogButtonBox.ActionRole)
        self.save_button = self.buttons.addButton("Save Connection", QDialogButtonBox.ActionRole)
        compact_button(self.save_button, primary=True)
        self.close_button = self.buttons.addButton(QDialogButtonBox.Close)
        layout.addWidget(self.buttons)
        for button in self.buttons.buttons():
            button.setAutoDefault(False)
        self.reload_button.clicked.connect(self._reload)
        self.forget_button.clicked.connect(self._forget)
        self.save_button.clicked.connect(self._save)
        self.close_button.clicked.connect(self.reject)
        self.refresh_button.clicked.connect(self._refresh_models)
        self.cancel_button.clicked.connect(self._cancel_refresh)
        self.models.currentIndexChanged.connect(self._select_model)
        self.endpoint.textChanged.connect(self._endpoint_changed)
        self.endpoint.textChanged.connect(self._draft_changed)
        self.model_key.textChanged.connect(self._draft_changed)
        self.resize(bounded_window_size(QApplication.primaryScreen().availableGeometry(), QSize(720, 530)))
        self._run("read", self._service.observe)

    def _values(self):
        return self.endpoint.text(), self.model_key.text()

    def _dirty(self):
        return self._values() != self._base

    def _valid(self):
        try:
            _endpoint(self.endpoint.text())
            if self.model_key.text():
                _text(self.model_key.text(), MAX_ID, "invalid_model_preference")
            return True
        except Exception:
            return False

    def _controls(self):
        busy = self._task is not None
        self.activity.setVisible(busy)
        self.endpoint.setEnabled(not busy and not self._held)
        self.model_key.setEnabled(not busy and not self._held)
        self.reload_button.setEnabled(not busy)
        refreshing = busy and self._operation == "discover"
        self.close_button.setEnabled(not busy or refreshing)
        self.cancel_button.setVisible(refreshing)
        self.cancel_button.setEnabled(refreshing and not self._discovery_cancel.is_set())
        self.refresh_button.setEnabled(not busy and not self._held and self._valid_endpoint())
        self.models.setEnabled(not busy and self._observed_endpoint == self.endpoint.text())
        self.save_button.setEnabled(not busy and not self._held and self._dirty() and self._valid())
        self.forget_button.setEnabled(not busy and not self._held and self._snapshot is not None
            and self._snapshot.selection is not None)

    def _valid_endpoint(self):
        try:
            _endpoint(self.endpoint.text())
            return True
        except Exception:
            return False

    def _endpoint_changed(self):
        self._invalidate_models()

    def _invalidate_models(self):
        self._discovery_generation += 1
        self._observed_endpoint = None
        self.models.clear()
        self.models.addItem("Choose a model after Refresh…", None)
        self.model_status.setText("Model list not checked for this address. Refresh to inspect it.")

    def _select_model(self, index):
        if (self._task is None and self._observed_endpoint == self.endpoint.text()
                and index > 0):
            key = self.models.itemData(index)
            if type(key) is str:
                self.model_key.setText(key)

    def _refresh_models(self):
        if not self.refresh_button.isEnabled():
            return
        endpoint = self.endpoint.text()
        self._invalidate_models()
        self._submitted_generation = self._discovery_generation
        self._discovery_cancel = Event()
        self._close_requested = None
        self._run("discover", lambda: self._discovery(endpoint,
            cancelled=self._discovery_cancel.is_set))

    def _cancel_refresh(self):
        self._discovery_cancel.set()
        self._controls()

    def _finish_discovery(self, kind, inventory):
        current = self._submitted_generation == self._discovery_generation
        if self._discovery_cancel.is_set() or not current:
            self.model_status.setText("Refresh cancelled or address changed. Refresh again when ready.")
        elif kind == "ok" and type(inventory) is ModelInventory:
            self._observed_endpoint = self.endpoint.text()
            for model in inventory.models:
                loaded = "Loaded" if model.loaded_instances else "Not loaded"
                vision = {True: "Vision advertised", False: "No vision advertised", None: "Vision unknown"}[model.vision]
                self.models.addItem(model.display_name + " — " + loaded, model.key)
                self.models.setItemData(self.models.count() - 1,
                    model.key + "\n" + vision + "; inference has not been qualified.", Qt.ToolTipRole)
            self.model_status.setText("Choose a declared model key, then Save Connection to remember it. "
                "This list does not establish inference readiness." if inventory.models else
                "The server reported no available models. You can still save a preferred key manually.")
            preferred = self.model_key.text()
            if preferred and not any(m.key == preferred for m in inventory.models):
                self.model_status.setText(self.model_status.text() + " The preferred key was not reported.")
        else:
            messages = {
                "authentication_required_or_rejected": "The server requires authentication. This editor has no credential entry yet; preferences can still be saved.",
                "unsupported_route": "The server does not provide the supported model-list API. Check its version before refreshing again.",
                "discovery_deadline": "The model-list request timed out. Refresh again when the server is available.",
                "discovery_io_timeout": "The model-list request timed out. Refresh again when the server is available.",
            }
            self.model_status.setText(messages.get(kind,
                "The model list could not be read. Check the local server and Refresh again; preferences can still be saved."))
        self._saved_status() if not self._dirty() else self.status.setText("Unsaved connection preferences.")
        self._controls()
        result = self._close_requested
        self._close_requested = None
        if result is not None:
            self.done(result)

    def _draft_changed(self):
        if self._task is None and not self._held:
            if self._dirty():
                self.status.setText("Unsaved connection preferences." if self._valid() else
                    "Use a valid local HTTP address and port. The exact model key is optional.")
            else:
                self._saved_status()
        self._controls()

    def _saved_status(self):
        self.status.setText(("Connection preferences saved. Model list observed; inference is not qualified."
            if self._observed_endpoint == self.endpoint.text() else
            "Connection preferences saved. Server and model have not been checked.")
            if self._snapshot and self._snapshot.selection else
            "No saved connection preference. Enter an explicit local endpoint to remember it.")

    def _run(self, operation, call):
        if self._task is not None:
            return
        if operation in ("read", "forget"):
            self._invalidate_models()
        self._operation = operation
        self._started = monotonic()
        self._outcome = None
        def safe_call():
            try:
                return "ok", call()
            except DiscoveryError as error:
                return str(error), None
            except (SettingsRecoveryRequired, SettingsError):
                return "held", None
            except Exception:
                return "error", None
        task = AsyncCallTask(safe_call)
        self._task = task
        task.signals.complete.connect(self._receive)
        task.signals.failed.connect(lambda _message: self._receive(("error", None)))
        self.status.setText({"read": "Reading saved connection preferences…",
            "save": "Saving connection preferences…", "forget": "Forgetting connection preferences…",
            "discover": "Reading the local model list…"}[operation] +
            (" Cancel or Close to stop this request." if operation == "discover" else " You can close after this finishes."))
        self._controls()
        self._settle.start()
        try:
            start_qt_delivery_task(QThreadPool.globalInstance(), task)
        except Exception:
            # Dispatch never accepted the runnable; no operation was submitted.
            self._settle.stop()
            self._task = None
            if operation == "discover":
                self._finish_discovery("error", None)
            else:
                self._held = True
                self.status.setText("The preference operation could not start. Reload and review before trying again.")
            self._controls()

    def _receive(self, outcome):
        self._outcome = outcome

    def _deliver_settled(self):
        task = self._task
        if task is None:
            return
        if self._outcome is None or not task.is_finished():
            phase = {"read": "Reading saved connection preferences", "save": "Saving connection preferences",
                "forget": "Forgetting connection preferences", "discover": "Reading the local model list"}[self._operation]
            self.status.setText(f"{phase}… Elapsed {monotonic() - self._started:.1f} s. " +
                ("Cancel or Close to stop this request." if self._operation == "discover" else "You can close after this finishes."))
            return
        kind, snapshot = self._outcome
        operation = self._operation
        self._settle.stop()
        self._task = None
        self._outcome = None
        if operation == "discover":
            self._finish_discovery(kind, snapshot)
            return
        if kind != "ok":
            self._held = True
            self.status.setText("Preferences changed or need recovery. Reload and review before saving."
                if kind == "held" else
                "Connection preferences could not be read or saved. Reload and review before trying again.")
        else:
            self._snapshot = snapshot
            self._held = False
            selection = snapshot.selection
            self._base = (selection.endpoint, selection.preferred_model_key or "") if selection else ("", "")
            self.endpoint.setText(self._base[0])
            self.model_key.setText(self._base[1])
            self._saved_status()
            if operation == "forget":
                self.status.setText("Connection preferences forgotten. The server and models were not changed.")
        self._controls()
        if kind == "ok":
            (self.close_button if operation == "save" else self.endpoint).setFocus(Qt.OtherFocusReason)
        else:
            self.reload_button.setFocus(Qt.OtherFocusReason)

    def _discard_draft(self):
        if not self._dirty():
            return True
        box = QMessageBox(QMessageBox.Question, "Unsaved Connection Preferences",
            "Discard the unsaved connection preferences? Your saved connection and pending AI configuration will not change.", parent=self)
        discard = box.addButton("Discard Changes", QMessageBox.DestructiveRole)
        keep = box.addButton("Keep Editing", QMessageBox.RejectRole)
        box.setDefaultButton(keep)
        box.exec()
        return box.clickedButton() is discard

    def _reload(self):
        if self._task is None and self._discard_draft():
            self._run("read", self._service.observe)

    def _save(self):
        if self.save_button.isEnabled():
            endpoint, key = self._values()
            expected = self._snapshot
            self._run("save", lambda: self._service.save_selection(endpoint,
                preferred_model_key=key or None, expected=expected))

    def _forget(self):
        if self.forget_button.isEnabled() and self._discard_draft():
            expected = self._snapshot
            self._run("forget", lambda: self._service.forget_selection(expected=expected))

    def done(self, result):
        if self._task is not None:
            if self._operation == "discover":
                self._close_requested = result
                self._cancel_refresh()
            return
        if not self._discard_draft():
            return
        self._invalidate_models()
        self._settle.stop()
        super().done(result)

    def reject(self):
        self.done(QDialog.DialogCode.Rejected)

    def closeEvent(self, event):
        if self._task is not None:
            if self._operation == "discover":
                self.reject()
            event.ignore()
            return
        self.reject()
        if self.isVisible():
            event.ignore()
        else:
            event.accept()
