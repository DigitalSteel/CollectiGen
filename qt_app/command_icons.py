"""Small palette-aware icons for command buttons and menus.

The icons deliberately use Qt drawing primitives instead of a font or external
asset pack, so their colour follows the active application palette.
"""

from __future__ import annotations

from PySide6.QtCore import QPointF, QRectF, Qt
from PySide6.QtGui import QIcon, QIconEngine, QPainter, QPainterPath, QPalette, QPen, QPixmap
from PySide6.QtWidgets import QApplication


COMMAND_ICON_NAMES = (
    "home", "import", "refresh", "edit", "review", "organize", "prepare",
    "generate", "view", "settings", "help", "search", "feedback", "history",
    "backup", "delete", "exit", "more", "library", "undo", "add", "save",
    "filter", "advanced",
)


def _line(painter: QPainter, *points: tuple[float, float]) -> None:
    painter.drawPolyline([QPointF(x, y) for x, y in points])


def _home(p: QPainter) -> None:
    _line(p, (3, 11), (12, 3), (21, 11)); p.drawRoundedRect(QRectF(6, 10, 12, 11), 1, 1); _line(p, (10, 21), (10, 15), (14, 15), (14, 21))


def _import(p: QPainter) -> None:
    _line(p, (12, 3), (12, 15), (8, 11)); _line(p, (12, 15), (16, 11)); _line(p, (4, 15), (5, 21), (19, 21), (20, 15))


def _refresh(p: QPainter) -> None:
    p.drawArc(QRectF(4, 4, 16, 16), 35 * 16, 270 * 16); _line(p, (19, 5), (20, 10), (15, 9))


def _edit(p: QPainter) -> None:
    _line(p, (5, 19), (7, 15), (17, 5), (20, 8), (10, 18), (5, 19)); _line(p, (14, 6), (18, 10))


def _review(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(5, 4, 14, 17), 1.8, 1.8); p.drawRoundedRect(QRectF(9, 2.5, 6, 4), 1.3, 1.3); _line(p, (8, 13), (11, 16), (16.5, 10))


def _organize(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(3, 7, 18, 13), 1.5, 1.5); _line(p, (3, 10), (21, 10)); _line(p, (6, 5), (12, 5), (14, 7))


def _prepare(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(4, 4, 16, 16), 2, 2); _line(p, (8, 12), (11, 15), (17, 8)); _line(p, (7, 6), (10, 6))


def _generate(p: QPainter) -> None:
    _line(p, (12, 3), (13.5, 9), (20, 12), (13.5, 15), (12, 21), (10.5, 15), (4, 12), (10.5, 9), (12, 3))


def _view(p: QPainter) -> None:
    path = QPainterPath(QPointF(2.5, 12)); path.cubicTo(6, 6, 9, 4.5, 12, 4.5); path.cubicTo(15, 4.5, 18, 6, 21.5, 12); path.cubicTo(18, 18, 15, 19.5, 12, 19.5); path.cubicTo(9, 19.5, 6, 18, 2.5, 12); p.drawPath(path); p.drawEllipse(QPointF(12, 12), 3, 3)


def _settings(p: QPainter) -> None:
    for x, y in ((12, 4), (19, 8), (19, 16), (12, 20), (5, 16), (5, 8)):
        p.drawEllipse(QPointF(x, y), 1.4, 1.4)
    p.drawEllipse(QPointF(12, 12), 3.2, 3.2); p.drawEllipse(QPointF(12, 12), 1, 1)


def _help(p: QPainter) -> None:
    p.drawEllipse(QPointF(12, 12), 9, 9); _line(p, (9.5, 9), (10.5, 7.5), (13.5, 7.5), (14.5, 9.5), (12, 12), (12, 14)); p.drawPoint(QPointF(12, 17))


def _search(p: QPainter) -> None:
    p.drawEllipse(QPointF(10, 10), 6, 6); _line(p, (14.5, 14.5), (20, 20))


def _feedback(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(3, 4, 18, 14), 2, 2); _line(p, (9, 18), (7, 21), (13, 18)); _line(p, (7, 9), (17, 9)); _line(p, (7, 13), (14, 13))


def _history(p: QPainter) -> None:
    p.drawArc(QRectF(4, 4, 16, 16), 35 * 16, 270 * 16); _line(p, (5, 7), (5, 3), (9, 4)); _line(p, (12, 7), (12, 12), (16, 14))


def _backup(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(4, 3, 16, 18), 1.5, 1.5); p.drawRoundedRect(QRectF(7, 4, 8, 5), 1, 1); p.drawRoundedRect(QRectF(7, 14, 10, 6), 1, 1); p.drawEllipse(QPointF(16.5, 6.5), .7, .7)


def _delete(p: QPainter) -> None:
    _line(p, (4, 7), (20, 7)); _line(p, (9, 4), (15, 4)); p.drawRoundedRect(QRectF(6, 7, 12, 14), 1, 1); _line(p, (10, 11), (10, 17)); _line(p, (14, 11), (14, 17))


def _exit(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(4, 3, 12, 18), 1.5, 1.5); _line(p, (12, 12), (21, 12), (17, 8)); _line(p, (21, 12), (17, 16))


def _more(p: QPainter) -> None:
    for x in (5, 12, 19): p.drawEllipse(QPointF(x, 12), 1.5, 1.5)


def _library(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(3, 5, 18, 16), 1, 1); _line(p, (3, 9), (21, 9)); _line(p, (7, 5), (7, 21)); _line(p, (12, 9), (12, 21)); _line(p, (17, 9), (17, 21))


def _undo(p: QPainter) -> None:
    _line(p, (9, 6), (4, 11), (9, 16)); p.drawArc(QRectF(5, 6, 15, 13), -35 * 16, 210 * 16)


def _add(p: QPainter) -> None:
    _line(p, (12, 4), (12, 20)); _line(p, (4, 12), (20, 12))


def _save(p: QPainter) -> None:
    p.drawRoundedRect(QRectF(4, 3, 16, 18), 1.5, 1.5); p.drawRoundedRect(QRectF(7, 4, 8, 5), 1, 1); p.drawRoundedRect(QRectF(7, 14, 10, 6), 1, 1)


def _filter(p: QPainter) -> None:
    _line(p, (3, 5), (21, 5), (14, 13), (14, 20), (10, 18), (10, 13), (3, 5))


def _advanced(p: QPainter) -> None:
    _line(p, (3, 6), (11, 6)); p.drawEllipse(QPointF(14, 6), 2, 2); _line(p, (16, 6), (21, 6)); _line(p, (3, 12), (7, 12)); p.drawEllipse(QPointF(10, 12), 2, 2); _line(p, (12, 12), (21, 12)); _line(p, (3, 18), (14, 18)); p.drawEllipse(QPointF(17, 18), 2, 2); _line(p, (19, 18), (21, 18))


_DRAWERS = {name: globals()[f"_{name}"] for name in COMMAND_ICON_NAMES}


class _CommandIconEngine(QIconEngine):
    def __init__(self, name: str):
        super().__init__()
        self._name = name

    def clone(self):
        return _CommandIconEngine(self._name)

    def pixmap(self, size, mode, state) -> QPixmap:
        pixmap = QPixmap(size)
        pixmap.fill(Qt.transparent)
        painter = QPainter(pixmap)
        self.paint(painter, pixmap.rect(), mode, state)
        painter.end()
        return pixmap

    def paint(self, painter: QPainter, rect, mode, state) -> None:
        del state
        palette = QApplication.palette()
        color = (
            palette.color(QPalette.Disabled, QPalette.Text)
            if mode == QIcon.Disabled
            else palette.color(QPalette.Active, QPalette.HighlightedText)
            if mode == QIcon.Selected
            else palette.color(QPalette.Active, QPalette.Text)
        )
        side = min(rect.width(), rect.height())
        if side <= 0:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing, True)
        painter.translate(rect.x() + (rect.width() - side) / 2, rect.y() + (rect.height() - side) / 2)
        painter.scale(side / 24.0, side / 24.0)
        painter.setPen(QPen(color, 1.8, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        painter.setBrush(Qt.NoBrush)
        _DRAWERS[self._name](painter)
        painter.restore()


def command_icon(name: str) -> QIcon:
    """Return a 24px-friendly palette-aware command icon.

    Unknown names intentionally use the neutral overflow/action glyph so callers
    can render newly introduced commands without an asset or a runtime failure.
    """
    key = str(name or "").strip().casefold()
    return QIcon(_CommandIconEngine(key if key in _DRAWERS else "more"))


_LABEL_ICONS = (
    ("home", "home"), ("import", "import"), ("filter", "filter"),
    ("duplicate", "search"), ("find", "search"), ("audit", "review"),
    ("folder", "organize"), ("organization", "organize"),
    ("concept", "library"), ("knowledge", "library"), ("libraries", "library"),
    ("summary", "view"), ("resolution", "settings"), ("policy", "settings"),
    ("recheck", "refresh"), ("about", "help"), ("what's new", "history"),
    ("refresh", "refresh"), ("reload", "refresh"),
    ("inspect", "search"), ("relationship", "library"),
    ("maintenance", "settings"), ("color", "view"), ("logs", "review"),
    ("scan", "refresh"), ("edit", "edit"), ("review", "review"),
    ("move", "organize"), ("organize", "organize"), ("prepare", "prepare"),
    ("training", "prepare"), ("plan", "generate"),
    ("generate", "generate"), ("view", "view"), ("setting", "settings"),
    ("help", "help"), ("search", "search"), ("feedback", "feedback"),
    ("history", "history"), ("backup", "backup"), ("metadata", "backup"),
    ("delete", "delete"), ("remove", "delete"), ("recycle", "delete"),
    ("exit", "exit"), ("more", "more"), ("library", "library"),
    ("undo", "undo"), ("add", "add"), ("new", "add"), ("save", "save"),
    ("advanced", "advanced"),
)


def icon_for_label(label: str) -> QIcon:
    """Choose a conservative icon from a visible command label."""
    normalized = str(label or "").casefold()
    for term, name in _LABEL_ICONS:
        if term in normalized:
            return command_icon(name)
    return command_icon("more")
