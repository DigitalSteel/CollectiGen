from __future__ import annotations

from PySide6.QtGui import QAccessible,QAccessibleAnnouncementEvent


def announce_accessibly(widget,message: str | None = None,*,assertive=False) -> None:
    """Publish screen-reader text without using the native warning/alert role."""
    if widget is None:
        return
    text=str(message or "").strip()
    if not text:
        accessible_name=getattr(widget,"accessibleName",None)
        if callable(accessible_name):
            text=str(accessible_name() or "").strip()
    if not text:
        visible_text=getattr(widget,"text",None)
        if callable(visible_text):
            text=str(visible_text() or "").strip()
    if not text:
        return
    event=QAccessibleAnnouncementEvent(widget,text)
    event.setPoliteness(
        QAccessible.AnnouncementPoliteness.Assertive
        if assertive else QAccessible.AnnouncementPoliteness.Polite
    )
    QAccessible.updateAccessibility(event)
