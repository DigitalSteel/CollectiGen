from __future__ import annotations

from PySide6.QtGui import QBrush, QColor
from PySide6.QtWidgets import QHBoxLayout, QLabel, QWidget


STANDARD_STATUS_COLORS = {
    "red": "#e5484d",
    "orange": "#f28c28",
    "yellow": "#d9b300",
    "green": "#2fa84f",
    "blue": "#1686d9",
    "neutral": "#7b8490",
}
COLOR_BLIND_STATUS_COLORS = {
    "red": "#cc79a7",
    "orange": "#d55e00",
    "yellow": "#e69f00",
    "green": "#009e73",
    "blue": "#0072b2",
    "neutral": "#7b8490",
}
STATUS_COLORS = dict(STANDARD_STATUS_COLORS)
STATUS_LABELS = {
    "red": "Problem",
    "orange": "Needs review",
    "yellow": "Uncertain",
    "green": "High confidence",
    "neutral": "Not evaluated",
}


def refresh_style(widget) -> None:
    widget.style().unpolish(widget); widget.style().polish(widget); widget.update()


def set_color_blind_mode(enabled: bool) -> None:
    STATUS_COLORS.clear(); STATUS_COLORS.update(COLOR_BLIND_STATUS_COLORS if enabled else STANDARD_STATUS_COLORS)


def set_visual_status(widget, status: str, *, selected: bool = False) -> None:
    status = status if status in STATUS_LABELS else "neutral"
    widget.setProperty("visualStatus", status)
    widget.setProperty("selected", bool(selected))
    refresh_style(widget)


def color_tree_item(item, status: str, column: int = 0) -> None:
    color = STATUS_COLORS.get(status, STATUS_COLORS["neutral"])
    item.setForeground(column, QBrush(QColor(color)))
    item.setToolTip(column, STATUS_LABELS.get(status, STATUS_LABELS["neutral"]))


class ConfidenceLegend(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent); layout=QHBoxLayout(self); layout.setContentsMargins(0,0,0,0); layout.setSpacing(10)
        for status in ("red","orange","yellow","green","blue"):
            label="Active" if status=="blue" else STATUS_LABELS[status]
            swatch=QLabel(f"● {label}"); swatch.setObjectName("confidenceLegend"); swatch.setProperty("visualStatus",status)
            swatch.setToolTip("Blue identifies the single image currently open in the inspector." if status=="blue" else label)
            layout.addWidget(swatch)
        layout.addStretch(1)
