"""One saved-history surface with explicit typed domain panels and shared lifetime."""
from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import QComboBox, QHBoxLayout, QLabel, QStackedWidget, QVBoxLayout

from .modal_feedback import FeedbackEnabledDialog


class HistoryWorkspaceDialog(FeedbackEnabledDialog):
    feedback_workflow = "History & Versions"
    restorationFinished = Signal(object)
    busyChanged = Signal(bool)

    def __init__(self, jobs, parent=None, *, image_browser=None, recovery_handler=None,
                 domains=(), initial_domain=None):
        super().__init__(parent)
        self.jobs = jobs
        self._closing = False
        self._closed = False
        self._active = ""
        self._panels = {}
        self._factories = {}
        self.setWindowTitle("History & Versions")
        self.resize(1540, 930)
        self.setModal(False)
        layout = QVBoxLayout(self)
        choice = QHBoxLayout()
        choice.addWidget(QLabel("Saved history for"))
        self.domain = QComboBox()
        self.domain.setAccessibleName("History domain")
        choice.addWidget(self.domain, 1)
        layout.addLayout(choice)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        if image_browser is not None:
            def image_panel():
                from .history_versions_dialog import HistoryVersionsDialog
                panel = HistoryVersionsDialog(image_browser, jobs, self,
                    recovery_handler=recovery_handler, close_callback=self.reject)
                panel.setWindowFlags(Qt.Widget)
                for binding in getattr(panel, "_status_footer_bindings", ()):
                    binding.follow_visibility = True
                panel.finished.connect(lambda _result: self._finish_close())
                return panel
            self._factories["image_knowledge"] = image_panel
            self.domain.addItem("Image Knowledge", "image_knowledge")
        for key, label, factory in domains:
            if not key or key in self._factories:
                raise ValueError("History domains must have distinct explicit identities.")
            self._factories[key] = lambda create=factory: create(jobs, self)
            self.domain.addItem(label, key)
        if not self._factories:
            raise ValueError("Choose at least one exact saved-history scope.")
        if initial_domain is not None:
            if initial_domain not in self._factories:
                raise ValueError("The requested history domain is outside this scope.")
            self.domain.setCurrentIndex(self.domain.findData(initial_domain))
        self.domain.currentIndexChanged.connect(self._select)
        from .components import guard_scrollable_value_controls
        guard_scrollable_value_controls(self)
        self._select()

    @property
    def _task(self):
        return next((panel._task for panel in self._panels.values() if panel._task is not None), None)

    @property
    def active_panel(self):
        return self._panels[self._active]

    def _select(self, *_args):
        selected = self.domain.currentData()
        if self._task is not None or self._closing:
            self.domain.blockSignals(True)
            self.domain.setCurrentIndex(self.domain.findData(self._active))
            self.domain.blockSignals(False)
            return
        if selected == self._active:
            return
        if self._active:
            previous = self.active_panel
            if hasattr(previous, "invalidate_comparison"):
                previous.invalidate_comparison()
            else:
                previous.invalidate()
        if selected not in self._panels:
            panel = self._factories[selected]()
            panel.busyChanged.connect(self._busy_changed)
            panel.restorationFinished.connect(self.restorationFinished)
            if hasattr(panel, "closeRequested"):
                panel.closeRequested.connect(self.reject)
            self._panels[selected] = panel
            self.stack.addWidget(panel)
        self._active = selected
        self.stack.setCurrentWidget(self.active_panel)
        if hasattr(self.active_panel, "activate"):
            self.active_panel.activate()
        self._busy_changed()

    def _busy_changed(self, *_args):
        self.domain.setEnabled(self._task is None and not self._closing)
        self.busyChanged.emit(self._task is not None)
        if self._closing and self._task is None:
            QTimer.singleShot(0, self._finish_close)

    def _finish_close(self):
        if self._closing and not self._closed and self._task is None:
            self._closed = True
            super().reject()

    def reject(self):
        self._closing = True
        self.domain.setEnabled(False)
        for panel in self._panels.values():
            if hasattr(panel, "shutdown"):
                panel.shutdown()
            else:
                panel._closing = True
                panel.reject()
        self._finish_close()

    def closeEvent(self, event):
        event.ignore()
        self.reject()
