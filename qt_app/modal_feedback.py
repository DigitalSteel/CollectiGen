from __future__ import annotations

import weakref

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import (
    QApplication,
    QDialog,
    QTabWidget,
    QPushButton,
    QWidget,
)

from core.support_bundle import redact_diagnostic_text


def _is_descendant(widget: QWidget | None, parent: QWidget) -> bool:
    current = widget
    while current is not None:
        if current is parent:
            return True
        current = current.parentWidget()
    return False


def _feedback_host(dialog: QDialog):
    current = dialog.parentWidget()
    while current is not None:
        handler = getattr(current, "open_bug_report", None)
        if callable(handler):
            return handler
        current = current.parentWidget()
    app = QApplication.instance()
    if app is not None:
        for widget in app.topLevelWidgets():
            handler = getattr(widget, "open_bug_report", None)
            if callable(handler):
                return handler
    return None


class FeedbackEnabledDialog(QDialog):
    """QDialog base that adds the shared Feedback action to long workflows."""

    feedback_workflow = "Dialog"

    def __init__(self, parent=None, *args, **kwargs):
        super().__init__(parent, *args, **kwargs)
        self._feedback_button: QPushButton | None = None
        self._feedback_focus_ref = None
        self._feedback_button_timer = QTimer(self)
        self._feedback_button_timer.setSingleShot(True)
        self._feedback_button_timer.timeout.connect(self._ensure_feedback_button)
        app = QApplication.instance()
        if app is not None:
            app.focusChanged.connect(self._remember_feedback_focus)

    def showEvent(self, event) -> None:
        super().showEvent(event)
        self._feedback_button_timer.start(0)

    def _remember_feedback_focus(
        self, _old: QWidget | None, new: QWidget | None
    ) -> None:
        if (
            new is not None
            and new is not self._feedback_button
            and _is_descendant(new, self)
        ):
            self._feedback_focus_ref = weakref.ref(new)

    def _ensure_feedback_button(self) -> None:
        if self._feedback_button is not None or not self.isWindow():
            return
        button = QPushButton("Feedback…", self)
        button.setAutoDefault(False)
        button.setDefault(False)
        button.setProperty("compact", True)
        button.setAccessibleName(
            f"Send feedback about {self.feedback_workflow}"
        )
        button.setToolTip(
            "Create a local feedback report without closing this window or "
            "discarding its current work."
        )
        button.clicked.connect(self._request_feedback)
        from .components import window_status_footer
        footer=window_status_footer(self)
        footer.actions.insertWidget(1,button)
        self._feedback_button = button
        button.show()

    def feedback_tab_widget(self) -> QTabWidget | None:
        for name in ("tabs", "sections", "workflow_tabs"):
            candidate = getattr(self, name, None)
            if isinstance(candidate, QTabWidget) and candidate.isVisibleTo(self):
                return candidate
        return next(
            (
                tabs
                for tabs in self.findChildren(QTabWidget)
                if tabs.window() is self and tabs.isVisibleTo(self)
            ),
            None,
        )

    def feedback_context(self) -> tuple[str, str]:
        workflow = redact_diagnostic_text(str(self.feedback_workflow))[:120]
        tabs = self.feedback_tab_widget()
        active_tab = ""
        if tabs is not None and tabs.currentIndex() >= 0:
            active_tab = redact_diagnostic_text(
                tabs.tabText(tabs.currentIndex())
            )[:120]
        return workflow, active_tab

    def _request_feedback(self) -> None:
        handler = _feedback_host(self)
        if handler is None:
            return
        prepare_capture=getattr(self,"prepare_for_privacy_capture",None)
        if callable(prepare_capture):
            prepare_capture()
            QApplication.processEvents()
        workflow, active_tab = self.feedback_context()
        focus_ref = self._feedback_focus_ref
        handler(
            origin=self,
            modal_workflow=workflow,
            modal_tab=active_tab,
        )

        def restore_focus() -> None:
            widget = focus_ref() if focus_ref is not None else None
            if (
                widget is not None
                and widget.isVisibleTo(self)
                and widget.isEnabled()
            ):
                widget.setFocus(Qt.OtherFocusReason)
            elif self.isVisible():
                self.activateWindow()

        QTimer.singleShot(0, restore_focus)
