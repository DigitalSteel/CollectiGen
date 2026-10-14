from __future__ import annotations

from collections.abc import Iterable

from PySide6.QtCore import QPointF,QRectF,QSize,Qt,Signal
from PySide6.QtGui import QIcon,QIconEngine,QPainter,QPainterPath,QPalette,QPen,QPixmap
from PySide6.QtWidgets import QApplication,QListWidget,QStyle,QStyledItemDelegate,QStyleOptionViewItem


NAVIGATION_ICON_NAMES=(
    "Dashboard","Ingest","Dataset","Review Center",
    "Tag Encyclopedia","Rules & Automation","Setup","Generate","Training",
)


def _line(painter: QPainter,*points: tuple[float,float]) -> None:
    painter.drawPolyline([QPointF(x,y) for x,y in points])


def _dashboard(painter: QPainter) -> None:
    for x,y,width,height in ((3,3,7,7),(14,3,7,7),(3,14,7,7),(14,14,7,7)):
        painter.drawRoundedRect(QRectF(x,y,width,height),1.5,1.5)


def _ingest(painter: QPainter) -> None:
    _line(painter,(12,3),(12,15),(8,11))
    _line(painter,(12,15),(16,11))
    path=QPainterPath(QPointF(4,14))
    path.lineTo(6,20); path.lineTo(18,20); path.lineTo(20,14)
    painter.drawPath(path)


def _dataset(painter: QPainter) -> None:
    painter.drawRoundedRect(QRectF(3,5,14,14),1.8,1.8)
    _line(painter,(6,16),(9.5,12.5),(12,15),(15,11))
    painter.drawEllipse(QPointF(8,9),1.2,1.2)
    _line(painter,(7,3),(21,3),(21,17))


def _review(painter: QPainter) -> None:
    painter.drawRoundedRect(QRectF(5,4,14,17),1.8,1.8)
    painter.drawRoundedRect(QRectF(9,2.5,6,4),1.3,1.3)
    _line(painter,(8,13),(11,16),(16.5,10))


def _tag(painter: QPainter) -> None:
    path=QPainterPath(QPointF(3,5))
    path.lineTo(12.5,5); path.lineTo(21,13.5); path.lineTo(13.5,21)
    path.lineTo(3,10.5); path.closeSubpath()
    painter.drawPath(path)
    painter.drawEllipse(QPointF(7.5,9),1.2,1.2)


def _automation(painter: QPainter) -> None:
    painter.drawRoundedRect(QRectF(2.5,9,5,5),1.3,1.3)
    painter.drawRoundedRect(QRectF(16.5,3,5,5),1.3,1.3)
    painter.drawRoundedRect(QRectF(16.5,16,5,5),1.3,1.3)
    _line(painter,(7.5,11.5),(12,11.5),(12,5.5),(16.5,5.5))
    _line(painter,(12,11.5),(12,18.5),(16.5,18.5))


def _setup(painter: QPainter) -> None:
    for y,x in ((6,8),(12,16),(18,11)):
        _line(painter,(3,y),(x-2,y))
        _line(painter,(x+2,y),(21,y))
        painter.drawEllipse(QPointF(x,y),2,2)


def _generate(painter: QPainter) -> None:
    # A creation wand, distinct from Dataset's stacked image frames.
    _line(painter,(3,19),(12,10),(15,13),(6,22),(3,19))
    _line(painter,(10,12),(13,15))
    _line(painter,(17,2),(18,5),(21,6),(18,7),(17,10),(16,7),(13,6),(16,5),(17,2))
    _line(painter,(5,3),(5,7)); _line(painter,(3,5),(7,5))
    _line(painter,(20,15),(20,19)); _line(painter,(18,17),(22,17))


def _training(painter: QPainter) -> None:
    _line(painter,(3,5),(3,21),(21,21))
    _line(painter,(6,17),(10,12),(14,14),(20,5))
    _line(painter,(15,5),(20,5),(20,10))


def _collapse(painter: QPainter) -> None:
    _line(painter,(19,3),(19,21))
    _line(painter,(14,7),(9,12),(14,17))


def _expand(painter: QPainter) -> None:
    _line(painter,(5,3),(5,21))
    _line(painter,(10,7),(15,12),(10,17))


_DRAWERS={
    "Dashboard":_dashboard,
    "Ingest":_ingest,
    "Dataset":_dataset,
    "Review Center":_review,
    "Tag Encyclopedia":_tag,
    "Rules & Automation":_automation,
    "Setup":_setup,
    "Generate":_generate,
    "Training":_training,
    "_collapse":_collapse,
    "_expand":_expand,
}


class _PaletteLineIconEngine(QIconEngine):
    def __init__(self,name: str,*,mirrored: bool=False):
        super().__init__()
        self._name=name
        self._mirrored=mirrored

    def clone(self):
        return _PaletteLineIconEngine(self._name,mirrored=self._mirrored)

    def pixmap(self,size,mode,state) -> QPixmap:
        pixmap=QPixmap(size)
        pixmap.fill(Qt.transparent)
        painter=QPainter(pixmap)
        self.paint(painter,pixmap.rect(),mode,state)
        painter.end()
        return pixmap

    def paint(self,painter: QPainter,rect,mode,state) -> None:
        del state
        palette=QApplication.palette()
        if mode==QIcon.Disabled:
            color=palette.color(QPalette.Disabled,QPalette.Text)
        elif mode==QIcon.Selected:
            color=palette.color(QPalette.Active,QPalette.HighlightedText)
        else:
            color=palette.color(QPalette.Active,QPalette.Text)
        side=min(rect.width(),rect.height())
        if side<=0:
            return
        painter.save()
        painter.setRenderHint(QPainter.Antialiasing,True)
        painter.translate(
            rect.x()+(rect.width()-side)/2,
            rect.y()+(rect.height()-side)/2,
        )
        painter.scale(side/24.0,side/24.0)
        if self._mirrored:
            painter.translate(24,0)
            painter.scale(-1,1)
        pen=QPen(color,1.8,Qt.SolidLine,Qt.RoundCap,Qt.RoundJoin)
        painter.setPen(pen); painter.setBrush(Qt.NoBrush)
        _DRAWERS[self._name](painter)
        painter.restore()


def navigation_icon(name: str) -> QIcon:
    """Return a palette-aware line icon for a top-level navigation page."""
    if name not in _DRAWERS:
        raise ValueError(f"Unknown navigation page: {name}")
    return QIcon(_PaletteLineIconEngine(name))


def navigation_icons(names: Iterable[str]=NAVIGATION_ICON_NAMES) -> tuple[QIcon,...]:
    return tuple(navigation_icon(name) for name in names)


def navigation_mode_icon(compact: bool,*,right_side: bool=False) -> QIcon:
    """Return the line icon for expanding or compacting the navigation rail."""
    return QIcon(_PaletteLineIconEngine(
        "_expand" if compact else "_collapse",mirrored=right_side,
    ))


class PageNavigationList(QListWidget):
    """Present reordered pages while retaining the shell's stable-index API.

    Existing workflow callers use setCurrentRow/currentRow as canonical page
    indices. Keep that contract here; item(), model() and currentRowChanged
    retain Qt's visual-row semantics. Shell dispatch must use pageChanged.
    Each item carries its canonical stack index in PAGE_INDEX_ROLE.
    """

    PAGE_INDEX_ROLE=Qt.UserRole+1
    pageChanged=Signal(int)

    def __init__(self,parent=None):
        super().__init__(parent)
        self.currentRowChanged.connect(self._emit_page_changed)

    def currentRow(self) -> int:
        item=self.currentItem()
        return int(item.data(self.PAGE_INDEX_ROLE)) if item is not None else -1

    def setCurrentRow(self,index: int,*args) -> None:
        if index==-1:
            super().setCurrentRow(-1,*args)
            return
        for row in range(self.count()):
            if self.item(row).data(self.PAGE_INDEX_ROLE)==index:
                super().setCurrentRow(row,*args)
                return

    def _emit_page_changed(self,_row: int) -> None:
        self.pageChanged.emit(self.currentRow())


class NavigationItemDelegate(QStyledItemDelegate):
    """Center icon-only navigation without changing expanded list rendering."""

    def __init__(self,parent=None):
        super().__init__(parent)
        self._compact=False

    def set_compact(self,compact: bool) -> None:
        compact=bool(compact)
        if compact==self._compact:
            return
        self._compact=compact
        parent=self.parent()
        if parent is not None:
            parent.viewport().update()
            parent.updateGeometry()

    def sizeHint(self,option,index) -> QSize:
        if self._compact:
            return QSize(44,52)
        return super().sizeHint(option,index)

    def paint(self,painter: QPainter,option,index) -> None:
        if not self._compact:
            super().paint(painter,option,index)
            return
        item_option=QStyleOptionViewItem(option)
        self.initStyleOption(item_option,index)
        icon=index.data(Qt.DecorationRole)
        if not isinstance(icon,QIcon):
            icon=item_option.icon
        item_option.icon=QIcon()
        item_option.text=""
        style=item_option.widget.style() if item_option.widget else QApplication.style()
        style.drawControl(
            QStyle.CE_ItemViewItem,item_option,painter,item_option.widget,
        )
        icon_mode=QIcon.Normal
        if not item_option.state&QStyle.State_Enabled:
            icon_mode=QIcon.Disabled
        elif item_option.state&QStyle.State_Selected:
            icon_mode=QIcon.Selected
        elif item_option.state&QStyle.State_MouseOver:
            icon_mode=QIcon.Active
        icon_size=item_option.decorationSize
        if not icon_size.isValid() or icon_size.isEmpty():
            icon_size=(item_option.widget.iconSize()
                       if item_option.widget is not None else QSize(24,24))
        icon_rect=QRectF(
            item_option.rect.center().x()-icon_size.width()/2,
            item_option.rect.center().y()-icon_size.height()/2,
            icon_size.width(),icon_size.height(),
        ).toRect()
        icon.paint(painter,icon_rect,Qt.AlignCenter,icon_mode,QIcon.Off)
