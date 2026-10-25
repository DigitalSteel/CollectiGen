from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtGui import QIcon,QPixmap


BRAND_DIR=Path(__file__).resolve().parents[1]/"assets"/"brand"
APPLICATION_ICON_PATH=BRAND_DIR/"collectigen.ico"
ABOUT_MARK_PATH=BRAND_DIR/"raster"/"collectigen-mark-256.png"


def application_icon() -> QIcon:
    return QIcon(str(APPLICATION_ICON_PATH)) if APPLICATION_ICON_PATH.is_file() else QIcon()


def about_mark(size: int=112) -> QPixmap:
    pixmap=QPixmap(str(ABOUT_MARK_PATH))
    if pixmap.isNull(): return pixmap
    return pixmap.scaled(size,size,Qt.KeepAspectRatio,Qt.SmoothTransformation)
