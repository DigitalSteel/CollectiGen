from __future__ import annotations

import hashlib

from PySide6.QtCore import QEvent,QObject
from PySide6.QtWidgets import QApplication,QMessageBox,QWidget


class NotificationTrace(QObject):
    """Privacy-safe runtime evidence for unexpected native alert feedback."""

    def __init__(self,logger,parent=None):
        super().__init__(parent)
        self.logger=logger
        self.records=[]

    @staticmethod
    def _severity(box: QMessageBox) -> str:
        return {
            QMessageBox.NoIcon:"plain",
            QMessageBox.Information:"information",
            QMessageBox.Question:"question",
            QMessageBox.Warning:"warning",
            QMessageBox.Critical:"critical",
        }.get(box.icon(),"unknown")

    @staticmethod
    def _title_id(title: str) -> str:
        return hashlib.sha256(str(title or "").encode("utf-8")).hexdigest()[:12]

    def eventFilter(self,watched,event):
        event_type=event.type()
        app=QApplication.instance()
        phase=str(app.property("collectigenNotificationPhase") or "ordinary") if app else "ordinary"
        if event_type==QEvent.Type.Show and isinstance(watched,QMessageBox):
            record={
                "event":"message_box_show",
                "severity":self._severity(watched),
                "title_id":self._title_id(watched.windowTitle()),
                "phase":phase,
            }
            self.records.append(record)
            self.logger.info(
                "notification_trace event=%s severity=%s title_id=%s phase=%s",
                record["event"],record["severity"],record["title_id"],phase,
            )
        elif event_type==QEvent.Type.WindowBlocked and isinstance(watched,QWidget) and watched.isWindow():
            modal=app.activeModalWidget() if app else None
            record={
                "event":"window_blocked",
                "window":type(watched).__name__,
                "modal":type(modal).__name__ if modal is not None else "",
                "phase":phase,
            }
            self.records.append(record)
            self.logger.info(
                "notification_trace event=%s window=%s modal=%s phase=%s",
                record["event"],record["window"],record["modal"],phase,
            )
        return False


def install_notification_trace(app: QApplication,logger) -> NotificationTrace:
    trace=NotificationTrace(logger,app)
    app.installEventFilter(trace)
    app.setProperty("collectigenNotificationPhase","application_start")
    return trace
