from __future__ import annotations

from html import escape
import re

from PySide6.QtCore import QEvent,QObject
from PySide6.QtGui import QFontMetrics,QGuiApplication
from PySide6.QtWidgets import QToolTip,QWidget


def wrapped_tooltip(text: str, width: int=420) -> str:
    """Wrap plain help without interpreting filenames or user text as HTML."""
    lines=[]
    for line in text.splitlines():
        # Allow even unbroken paths/identifiers to wrap, without losing characters.
        line=re.sub(r"\S{40,}",lambda match: "\u200b".join(
            match.group()[offset:offset+32]
            for offset in range(0,len(match.group()),32)
        ),line)
        lines.append(escape(line))
    return f'<qt><table width="{width}" cellspacing="0" cellpadding="0"><tr><td>'+"<br>".join(lines)+"</td></tr></table></qt>"


class _TooltipPolicy(QObject):
    def __init__(self,parent=None):
        super().__init__(parent); self.enabled=True

    def eventFilter(self,watched,event):
        if event.type()==QEvent.ToolTip:
            if not self.enabled:
                return True
            if isinstance(watched,QWidget):
                text=watched.toolTip()
                # Item-view delegates and existing rich help retain native handling.
                if text and not re.search(r"</?(?:qt|html|p|div|table|b|i|span|br)\b",text,re.I):
                    screen=QGuiApplication.screenAt(event.globalPos()) or watched.screen()
                    width=min(420,max(120,screen.availableGeometry().width()-32)) if screen else 420
                    metrics=QFontMetrics(QToolTip.font())
                    if max((metrics.horizontalAdvance(line) for line in text.splitlines()),default=0)>width:
                        QToolTip.showText(event.globalPos(),wrapped_tooltip(text,width),watched,watched.rect(),watched.toolTipDuration())
                        return True
        # Propagate unhandled events directly. Re-entering QObject's no-op
        # filter needlessly asks PySide to convert layout-event wrappers.
        return False


_policy=None


def apply_tooltip_policy(app,enabled: bool) -> bool:
    global _policy
    if _policy is None:
        _policy=_TooltipPolicy(app); app.installEventFilter(_policy)
    _policy.enabled=bool(enabled)
    if not _policy.enabled:
        QToolTip.hideText()
    return _policy.enabled
