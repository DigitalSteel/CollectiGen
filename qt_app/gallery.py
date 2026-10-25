from __future__ import annotations

from datetime import datetime,timezone
from pathlib import Path

from PySide6.QtCore import QEvent,QObject,QPoint,QRectF,QRunnable,QThreadPool,QSize,QTimer,Signal,Slot,Qt,QUrl
from PySide6.QtGui import QColor,QCursor,QDesktopServices,QImage,QPainter,QPainterPath,QPen,QPixmap,QPolygon
from PySide6.QtWidgets import QApplication,QCheckBox,QDialog,QDialogButtonBox,QFrame,QGraphicsBlurEffect,QGridLayout,QHBoxLayout,QLabel,QMenu,QPushButton,QScrollArea,QSizePolicy,QSlider,QVBoxLayout,QWidget
from core.image_workflow_state import ImageKnowledgeCoverage,concise_image_status_detail,image_knowledge_coverage
from .confidence import STATUS_COLORS,STATUS_LABELS, set_visual_status
from .components import EmptyStatePanel
from .workers import AsyncCallTask


_content_safety_policy=None
_library_identity_resolver=None
IMAGE_BADGE_HELP="""

## Image badges

The split-circle badge shows **T = Tags** and **D = Descriptions**. Each half
independently describes the image's saved coverage and findings:

- **Green:** saved coverage looks complete.
- **Yellow:** findings of that kind are pending review.
- **Red:** a problem affects that kind.
- **Black:** nothing is saved or pending for that kind.
- **Gray:** saved coverage is partial or its assessment is not loaded.

A thick outline marks an image problem; a dashed outline marks pending review.
Overall readiness remains on the card border. Coverage does not mean that a
person reviewed the image, that AI finished scanning it, or that training
quality is guaranteed.

In Ingest, an amber overlapping-squares badge marks an exact duplicate
candidate and a blue eye badge marks a visually similar candidate. These
appear only from a completed current duplicate check; drawing cards never
starts a scan. Recheck duplicates to refresh the evidence after an Ingest rescan.

Hover over a badge for that image's coverage, specific findings, and recorded
AI scan dates. **History not loaded** means the cached history is unavailable;
**no recorded completed run** means the loaded history has no completed run.
"""
GALLERY_DENSITIES=(
    (12,210,"12 / page — large"),(24,168,"24 / page — standard"),
    (36,150,"36 / page — compact"),(48,136,"48 / page — dense"),
    (72,124,"72 / page — maximum"),(96,112,"96 / page — ultra"),
)


class ZoomableImageView(QScrollArea):
    """Responsive fit view with bounded wheel zoom for a single image."""

    view_scale_changed=Signal(float)

    def __init__(self,parent=None):
        super().__init__(parent); self._source=QPixmap(); self._scale=1.0; self._fit_mode=True
        self._pan_origin=None; self._pan_scroll=None
        self.setWidgetResizable(False); self.setAlignment(Qt.AlignCenter)
        self.image_label=QLabel("Preview unavailable")
        self.image_label.setAlignment(Qt.AlignCenter)
        self.image_label.setStyleSheet("background:#202226; color:#b8bdc7;")
        self.image_label.setMinimumSize(1,1)
        self.setWidget(self.image_label)
        self.viewport().installEventFilter(self); self.image_label.installEventFilter(self)
        self.setToolTip("Use the mouse wheel to zoom. Drag a zoomed image to pan. Double-click to fit the image to the window.")

    @property
    def zoom_scale(self): return self._scale

    def set_image(self,pixmap: QPixmap):
        self._source=QPixmap(pixmap)
        if self._source.isNull():
            self.image_label.setText("Preview unavailable"); return
        self.image_label.setText(""); self._fit_mode=True; self.fit_to_view()

    def _fit_scale(self):
        if self._source.isNull(): return 1.0
        available=self.viewport().size()-QSize(4,4)
        return max(.02,min(
            available.width()/max(1,self._source.width()),
            available.height()/max(1,self._source.height()),
        ))

    def fit_to_view(self):
        if self._source.isNull(): return
        self._fit_mode=True; self._apply_scale(self._fit_scale())

    def zoom_by(self,factor: float,anchor=None):
        if self._source.isNull(): return
        old=self._scale; maximum=min(8.0,8192/max(1,self._source.width(),self._source.height()))
        new=max(.02,min(maximum,old*float(factor)))
        if abs(new-old)<.0001: return
        anchor=anchor or self.viewport().rect().center()
        x=(self.horizontalScrollBar().value()+anchor.x())/old
        y=(self.verticalScrollBar().value()+anchor.y())/old
        self._fit_mode=False; self._apply_scale(new)
        self.horizontalScrollBar().setValue(round(x*new-anchor.x()))
        self.verticalScrollBar().setValue(round(y*new-anchor.y()))

    def _apply_scale(self,scale: float):
        self._scale=scale
        size=QSize(max(1,round(self._source.width()*scale)),max(1,round(self._source.height()*scale)))
        self.image_label.setPixmap(self._source.scaled(size,Qt.KeepAspectRatio,Qt.SmoothTransformation))
        self.image_label.resize(size)
        self._update_pan_cursor()
        self.view_scale_changed.emit(float(self._scale))

    def set_zoom_scale(self,scale: float):
        """Apply an explicit scale for linked comparison views."""
        if self._source.isNull():return
        maximum=min(8.0,8192/max(1,self._source.width(),self._source.height()))
        self._fit_mode=False; self._apply_scale(max(.02,min(maximum,float(scale))))

    def _can_pan(self):
        return (self.image_label.width()>self.viewport().width()
                or self.image_label.height()>self.viewport().height())

    def _update_pan_cursor(self):
        cursor=Qt.OpenHandCursor if self._can_pan() else Qt.ArrowCursor
        self.viewport().setCursor(cursor); self.image_label.setCursor(cursor)

    def eventFilter(self,watched,event):
        event_type=event.type()
        if watched in (self.viewport(),self.image_label):
            if event_type==QEvent.MouseButtonDblClick and event.button()==Qt.LeftButton:
                self._pan_origin=None; self._pan_scroll=None; self.fit_to_view(); event.accept(); return True
            if event_type==QEvent.MouseButtonPress and event.button()==Qt.LeftButton and self._can_pan():
                self._pan_origin=event.globalPosition().toPoint()
                self._pan_scroll=(self.horizontalScrollBar().value(),self.verticalScrollBar().value())
                self.viewport().setCursor(Qt.ClosedHandCursor); self.image_label.setCursor(Qt.ClosedHandCursor)
                event.accept(); return True
            if event_type==QEvent.MouseMove and self._pan_origin is not None:
                delta=event.globalPosition().toPoint()-self._pan_origin
                self.horizontalScrollBar().setValue(self._pan_scroll[0]-delta.x())
                self.verticalScrollBar().setValue(self._pan_scroll[1]-delta.y())
                event.accept(); return True
            if event_type==QEvent.MouseButtonRelease and event.button()==Qt.LeftButton and self._pan_origin is not None:
                self._pan_origin=None; self._pan_scroll=None; self._update_pan_cursor(); event.accept(); return True
        return super().eventFilter(watched,event)

    def wheelEvent(self,event):
        delta=event.angleDelta().y()
        if delta:
            self.zoom_by(1.15 if delta>0 else 1/1.15,event.position().toPoint()); event.accept(); return
        super().wheelEvent(event)

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if self._fit_mode: self.fit_to_view()
        else: self._update_pan_cursor()


class ProtectedImageView(QWidget):
    """A zoomable image preview with local, screenshot-safe protection."""

    protection_changed=Signal()

    def __init__(self,parent=None,*,policy=None,settings=None):
        super().__init__(parent)
        self.policy=policy
        self.settings=settings
        self.path: Path | None=None
        self.record=None
        self._requires_blur=False
        self._temporarily_revealed=False
        self._checking=False
        self.rating="unknown"
        self.basis=""

        published=getattr(self.policy,"saved_result_published",None)
        if published is not None:
            published.connect(self._saved_result_published)

        layout=QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(5)
        self.viewer=ZoomableImageView()
        layout.addWidget(self.viewer,1)
        controls=QHBoxLayout()
        controls.setContentsMargins(0,0,0,0)
        self.protection_status=QLabel()
        self.protection_status.setObjectName("mutedLabel")
        self.protection_status.setWordWrap(True)
        self.reveal_button=QPushButton("Reveal Temporarily")
        self.reveal_button.setProperty("compact",True)
        self.reveal_button.clicked.connect(self.toggle_temporary_reveal)
        controls.addWidget(self.protection_status,1)
        controls.addWidget(self.reveal_button)
        layout.addLayout(controls)
        self.protection_status.hide()
        self.reveal_button.hide()

    def _saved_result_published(self,path) -> None:
        if self.path is None:
            return
        path_key=getattr(
            self.policy,"_path_key",lambda value:str(Path(value))
        )
        if path_key(path)==path_key(self.path):
            self.refresh_protection(preserve_temporary_reveal=False)

    def set_image(self,pixmap: QPixmap,path=None,*,record=None) -> None:
        self.path=Path(path) if path is not None else None
        record_path=getattr(record,"image_path",None)
        self.record=(
            record if (
                self.path is not None and record_path is not None
                and Path(record_path)==self.path
            ) else None
        )
        self.viewer.set_image(pixmap)
        if pixmap.isNull():
            self._requires_blur=False
            self._temporarily_revealed=False
            self._checking=False
            self.viewer.image_label.setGraphicsEffect(None)
            self.protection_status.hide()
            self.reveal_button.hide()
            return
        self.refresh_protection()

    def set_path(self,path) -> None:
        image_path=Path(path)
        availability=_storage_availability(image_path)
        if availability is not None and not bool(getattr(availability,"readable",True)):
            self.clear(str(getattr(availability,"label","Preview unavailable")))
            self.path=image_path
            return
        self.set_image(QPixmap(str(image_path)),image_path)

    def clear(self,message: str="Preview unavailable") -> None:
        self.set_image(QPixmap())
        self.viewer.image_label.setText(message)

    def _current_state(self) -> dict[str,object]:
        if self.path is None:
            return {
                "rating":"unknown","basis":"","checking":False,
                "requires_blur":False,"blurred":False,
            }
        if (
            self.record is not None
            and self.policy is not None
            and hasattr(self.policy,"preview_state_for_record")
        ):
            return self.policy.preview_state_for_record(
                self.record,honor_session_reveal=False,
            )
        if self.policy is not None and hasattr(self.policy,"preview_state"):
            return self.policy.preview_state(
                self.path, honor_session_reveal=False
            )
        blur_unknown=bool(
            getattr(self.settings,"gallery_blur_unknown",False)
        )
        return {
            "rating":"unknown",
            "basis":"No dependable rating yet",
            "checking":False,
            "requires_blur":blur_unknown,
            "blurred":blur_unknown,
        }

    @property
    def checking(self) -> bool:
        return self._checking

    @staticmethod
    def _rating_label(rating: str,checking: bool=False) -> str:
        if checking:
            return "Checking Content Rating"
        if rating=="unknown":
            return "No Dependable Rating Yet"
        return str(rating).title()

    def refresh_protection(
        self, *, preserve_temporary_reveal: bool=False
    ) -> None:
        """Re-read policy, optionally retaining this preview's local reveal."""
        was_revealed=(
            preserve_temporary_reveal and self._temporarily_revealed
        )
        state=self._current_state()
        self.rating=str(state.get("rating") or "unknown")
        self.basis=str(state.get("basis") or "")
        self._requires_blur=bool(state.get("requires_blur"))
        self._temporarily_revealed=bool(
            was_revealed and self._requires_blur
        )
        self._checking=bool(state.get("checking"))
        self._apply_protection(
            bool(state.get("blurred")) and not self._temporarily_revealed
        )

    def _apply_protection(self,blurred: bool) -> None:
        blurred=bool(blurred and self._requires_blur)
        if blurred:
            effect=QGraphicsBlurEffect(self.viewer.image_label)
            effect.setBlurRadius(28)
            self.viewer.image_label.setGraphicsEffect(effect)
        else:
            self.viewer.image_label.setGraphicsEffect(None)
        label=self._rating_label(self.rating,self._checking)
        if self._requires_blur:
            if blurred:
                self.protection_status.setText(f"Preview Blurred • {label}")
                self.reveal_button.setText("Reveal Temporarily")
                self.reveal_button.setAccessibleName(
                    f"Reveal protected image temporarily; {label}"
                )
            else:
                self.protection_status.setText(
                    f"Temporarily Revealed • {label}"
                )
                self.reveal_button.setText("Hide Preview")
                self.reveal_button.setAccessibleName(
                    f"Restore preview protection; {label}"
                )
            detail=(
                "This reveal applies only to this preview and resets when the "
                "image changes, the window reopens, or feedback captures it."
            )
            self.protection_status.setToolTip(
                f"{self.basis}\n{detail}".strip()
            )
            self.reveal_button.setToolTip(detail)
            self.protection_status.show()
            self.reveal_button.show()
        else:
            self.protection_status.setText(
                "" if self.rating=="general" else f"Preview Visible • {label}"
            )
            self.protection_status.setVisible(bool(self.protection_status.text()))
            self.reveal_button.hide()
        self.viewer.setAccessibleDescription(
            self.protection_status.text() or "Image preview visible"
        )
        self.protection_changed.emit()

    def toggle_temporary_reveal(self) -> None:
        if not self._requires_blur:
            return
        self._temporarily_revealed=not self._temporarily_revealed
        self._apply_protection(not self._temporarily_revealed)

    def reveal_temporarily(self) -> None:
        if self._requires_blur and not self._temporarily_revealed:
            self.toggle_temporary_reveal()

    def prepare_for_privacy_capture(self) -> None:
        """Restore protection before any feedback or screenshot capture."""
        self.refresh_protection()


class GalleryScaleControl(QWidget):
    """Compact thumbnail-size control shared by every primary gallery."""

    changed=Signal(int,int)

    def __init__(self,page_size: int=24,card_width: int=168,parent=None):
        super().__init__(parent); self._scales=tuple(reversed(GALLERY_DENSITIES))
        self._last_emitted=None
        layout=QHBoxLayout(self); layout.setContentsMargins(0,0,0,0); layout.setSpacing(5)
        label=QLabel("Size"); label.setObjectName("mutedLabel"); layout.addWidget(label)
        self.slider=QSlider(Qt.Horizontal); self.slider.setObjectName("galleryScale")
        self.slider.setRange(0,len(self._scales)-1); self.slider.setSingleStep(1); self.slider.setPageStep(1)
        self.slider.setFixedWidth(118); self.slider.setAccessibleName("Gallery thumbnail size")
        index=min(
            range(len(self._scales)),
            key=lambda item:abs(self._scales[item][0]-int(page_size))+abs(self._scales[item][1]-int(card_width))/4,
        )
        self.slider.setValue(index); self._update_tooltip(index)
        self._last_emitted=self.current_data()
        self.slider.valueChanged.connect(self._value_changed)
        self.slider.sliderReleased.connect(self._commit_change)
        layout.addWidget(self.slider)

    def current_data(self):
        page_size,card_width,_=self._scales[self.slider.value()]
        return int(page_size),int(card_width)

    def _update_tooltip(self,index: int):
        page_size,card_width,_=self._scales[index]
        self.slider.setToolTip(f"Thumbnail size • up to {page_size} images per page • {card_width}px cards")

    def _value_changed(self,index: int):
        self._update_tooltip(index)
        if self.slider.isSliderDown():
            # A gallery density change rebuilds the visible card page. Keep the
            # thumb and tooltip live while dragging, then perform that bounded
            # rebuild once after the user settles on a size.
            return
        self._commit_change()

    def _commit_change(self):
        if self.slider.isSliderDown():
            return
        value=self.current_data()
        if value==self._last_emitted:
            return
        self._last_emitted=value
        self.changed.emit(*value)


def set_content_safety_policy(policy):
    global _content_safety_policy
    _content_safety_policy=policy


def content_safety_policy():
    return _content_safety_policy


def set_library_identity_resolver(resolver):
    """Supply named-library identity to every shared image card."""
    global _library_identity_resolver
    _library_identity_resolver=resolver


def _storage_availability(path):
    if _library_identity_resolver is None:
        return None
    try:
        resolved=_library_identity_resolver(Path(path))
    except Exception:
        return None
    if isinstance(resolved,(tuple,list)) and len(resolved)>3:
        return resolved[3]
    return None


def filter_content_paths(paths):
    # Content-rating preferences blur previews; they do not remove images from
    # paging, selection, or reference galleries.
    return list(paths),0


class ThumbnailSignals(QObject):
    ready=Signal(str,object,int)


class ThumbnailTask(QRunnable):
    def __init__(self,path: Path,generation: int,size=(240,180)):
        super().__init__(); self.path=Path(path); self.generation=generation; self.size=size
        self.signals=ThumbnailSignals()

    @Slot()
    def run(self):
        availability=_storage_availability(self.path)
        if availability is not None and not bool(getattr(availability,"readable",True)):
            self.signals.ready.emit(str(self.path),QImage(),self.generation)
            return
        image=QImage(str(self.path))
        if not image.isNull(): image=image.scaled(*self.size,Qt.KeepAspectRatio,Qt.SmoothTransformation)
        self.signals.ready.emit(str(self.path),image,self.generation)


class SafetyFlagBadge(QWidget):
    """Font-independent high-contrast safety flag overlay."""

    def __init__(self,parent=None):
        super().__init__(parent)
        self.setObjectName("imageSafetyFlagBadge")
        self.setFixedSize(27,27)
        self.setAttribute(Qt.WA_TranslucentBackground)

    def paintEvent(self,event):
        painter=QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing,True)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor("#b42318"))
        painter.drawRoundedRect(0,0,self.width()-1,self.height()-1,5,5)
        painter.setPen(QPen(QColor("#ffffff"),2))
        painter.drawLine(8,5,8,22)
        painter.setBrush(QColor("#ffffff"))
        painter.drawPolygon(QPolygon((
            QPoint(9,6),QPoint(20,6),QPoint(17,11),
            QPoint(20,16),QPoint(9,16),
        )))
        painter.end()


class DuplicateCandidateBadge(QWidget):
    """Compact exact/visual candidate overlay backed by completed results."""

    def __init__(self,parent=None):
        super().__init__(parent)
        self.setObjectName("duplicateCandidateBadge")
        self.setFixedSize(31,31)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.kind=""

    def set_kind(self,kind: str) -> None:
        self.kind=str(kind or "")
        self.update()

    def paintEvent(self,event):
        if self.kind not in {"exact","similar"}:
            return
        painter=QPainter(self); painter.setRenderHint(QPainter.Antialiasing,True)
        painter.setPen(QPen(self.palette().text().color(),1.5))
        painter.setBrush(QColor("#f2a900" if self.kind=="exact" else "#4f8cff"))
        painter.drawEllipse(QRectF(1.5,1.5,27,27))
        color=QColor("#111111" if self.kind=="exact" else "#ffffff")
        painter.setPen(QPen(color,2))
        painter.setBrush(Qt.NoBrush)
        if self.kind=="exact":
            painter.drawRoundedRect(7,7,12,12,2,2)
            painter.drawRoundedRect(12,12,12,12,2,2)
        else:
            path=QPainterPath(QPoint(5,15))
            path.cubicTo(10,8,21,8,26,15)
            path.cubicTo(21,22,10,22,5,15)
            painter.drawPath(path)
            painter.setBrush(color); painter.drawEllipse(QPoint(15,15),3,3)
        painter.end()


class ImageKnowledgeBadge(QWidget):
    """Two independent halves for saved Tag/Description coverage."""

    def __init__(self,parent=None):
        super().__init__(parent)
        self.setObjectName("imageKnowledgeBadge")
        self.setFixedSize(40,40)
        self.setAttribute(Qt.WA_TranslucentBackground)
        self.coverage=ImageKnowledgeCoverage()
        self.problem=False
        self.setAccessibleName("Saved Tag and Description coverage")

    def background_status(self,index: int) -> str:
        if (self.coverage.tags_problem,self.coverage.descriptions_problem)[index]:
            return "red"
        pending=(self.coverage.tags_pending,self.coverage.descriptions_pending)[index]
        if pending:
            return "yellow"
        complete=(self.coverage.tags,self.coverage.descriptions)[index]
        stored=(self.coverage.tags_stored,self.coverage.descriptions_stored)[index]
        if pending is None and self.coverage.review_pending:
            return "partial"
        return "empty" if stored is False else "green" if complete else "partial"

    def paintEvent(self,event):
        painter=QPainter(self); painter.setRenderHint(QPainter.Antialiasing,True)
        palette=self.palette(); circle=QRectF(2,2,36,36)
        font=self.font(); font.setPixelSize(12); font.setBold(True); painter.setFont(font)
        for index,(letter,complete) in enumerate((
            ("T",self.coverage.tags),("D",self.coverage.descriptions),
        )):
            painter.save()
            half=QRectF(2+18*index,2,18,36)
            painter.setClipRect(half)
            painter.setPen(Qt.NoPen)
            status=self.background_status(index)
            color=STATUS_COLORS.get(status,"#000000" if status=="empty" else "#555555")
            painter.setBrush(QColor(color))
            painter.drawEllipse(circle)
            painter.setPen(QColor("#111111") if status in {"red","yellow","green"} else QColor("#ffffff"))
            painter.drawText(half,Qt.AlignCenter,letter)
            painter.restore()
        painter.setBrush(Qt.NoBrush); painter.setPen(QPen(palette.text().color(),1.5))
        if self.problem or self.coverage.problem:
            painter.setPen(QPen(palette.text().color(),3))
        elif self.coverage.review_pending:
            painter.setPen(QPen(palette.text().color(),1.5,Qt.DashLine))
        painter.drawEllipse(circle)
        painter.setPen(QPen(palette.text().color(),1.5)); painter.drawLine(20,3,20,37)


class ImageCard(QFrame):
    chosen=Signal(str); interaction=Signal(str,int); marked=Signal(str,bool); edit_requested=Signal(str); revealed=Signal(str); hidden_again=Signal(str)
    force_ai_requested=Signal(str,str,str)
    add_tag_requested=Signal(str,str)
    file_action_requested=Signal(str,str,str)
    duplicate_check_requested=Signal(str,str)

    def __init__(self,path: Path,parent=None,*,card_width: int=180):
        super().__init__(parent); self.path=Path(path); self.setObjectName("imageCard"); self.card_width=max(112,int(card_width))
        self.setFocusPolicy(Qt.StrongFocus)
        self.setMinimumWidth(self.card_width); self.setMaximumWidth(self.card_width+20); self.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Maximum)
        self._active_selected=False; self._marked_selected=False; self._content_blurred=False; self._content_protection_required=False; self.visual_status="neutral"; self.allow_data_edit=True; self.selection_enabled=False
        self.storage_availability=None; self.storage_readable=True; self.storage_writable=True
        self._compact_mode=False; self._content_rating="general"; self._content_rating_basis=""; self._content_blur_reason=""; self._status_detail=""; self._note_text=""; self._context_note_text=""
        self.allow_generation_actions=False
        self._ai_scan_detail=""; self._safety_review_detail=""; self._knowledge_coverage_detail=""
        self.gallery_selection_count=0
        self._file_actions=()
        self._read_only_file_actions=frozenset()
        self.allow_duplicate_check=False
        outer=QVBoxLayout(self); outer.setContentsMargins(3,3,3,3); outer.setSpacing(0)
        self.status_frame=QFrame(); self.status_frame.setObjectName("imageCardStatus"); outer.addWidget(self.status_frame)
        layout=QVBoxLayout(self.status_frame); layout.setContentsMargins(5,5,5,5); layout.setSpacing(3)
        set_visual_status(self,"neutral"); set_visual_status(self.status_frame,"neutral")
        self.preview=QLabel("Loading preview…"); self.preview.setObjectName("thumbnail")
        self.preview.setAlignment(Qt.AlignCenter); self.preview.setFixedHeight(max(96,int(self.card_width*.72)))
        self.preview.setStyleSheet("background:#202226; color:#b8bdc7; border-radius:4px;")
        self.preview.setCursor(QCursor(Qt.PointingHandCursor))
        self.preview.mousePressEvent=self._choose
        self.preview.mouseDoubleClickEvent=self.activate_default
        self.preview_layer=QWidget()
        self.preview_layer.setFixedHeight(self.preview.height())
        preview_layout=QGridLayout(self.preview_layer); preview_layout.setContentsMargins(0,0,0,0); preview_layout.setSpacing(0)
        preview_layout.addWidget(self.preview,0,0)
        self.knowledge_badge=ImageKnowledgeBadge(self.preview_layer)
        self.knowledge_badge.setToolTip("AI scan history")
        self.knowledge_badge.setCursor(QCursor(Qt.PointingHandCursor))
        self.knowledge_badge.mousePressEvent=self._choose
        self.knowledge_badge.mouseDoubleClickEvent=self.activate_default
        self.knowledge_badge.hide()
        preview_layout.addWidget(self.knowledge_badge,0,0,Qt.AlignRight|Qt.AlignBottom)
        self.safety_flag_badge=SafetyFlagBadge(self.preview_layer)
        self.safety_flag_badge.setAccessibleName("Safety review required")
        self.safety_flag_badge.setCursor(QCursor(Qt.PointingHandCursor))
        self.safety_flag_badge.mousePressEvent=self._choose
        self.safety_flag_badge.mouseDoubleClickEvent=self.activate_default
        self.safety_flag_badge.hide()
        preview_layout.addWidget(
            self.safety_flag_badge,0,0,Qt.AlignLeft|Qt.AlignTop,
        )
        self.duplicate_candidate_badge=DuplicateCandidateBadge(self.preview_layer)
        self.duplicate_candidate_badge.setCursor(QCursor(Qt.PointingHandCursor))
        self.duplicate_candidate_badge.mousePressEvent=self._choose
        self.duplicate_candidate_badge.mouseDoubleClickEvent=self.activate_default
        self.duplicate_candidate_badge.hide()
        preview_layout.addWidget(
            self.duplicate_candidate_badge,0,0,Qt.AlignRight|Qt.AlignTop,
        )
        # Kept as a non-layout compatibility/accessibility hook. Showing this row used to
        # resize cards whenever the active image changed; the stable outer blue border now
        # carries the visible active state instead.
        self.active_label=QLabel("Active image",self); self.active_label.setObjectName("activeLabel"); self.active_label.hide()
        name=QLabel(self.path.name); self.name_label=name; name.setAlignment(Qt.AlignCenter); name.setToolTip(str(self.path)); name.setCursor(QCursor(Qt.PointingHandCursor)); name.mousePressEvent=self._choose
        self.library_label=QLabel(); self.library_label.setObjectName("imageLibraryBadge")
        self.library_label.setAlignment(Qt.AlignCenter); self.library_label.hide()
        self._library_accessible=""
        source_profile=None
        if _library_identity_resolver is not None:
            try:
                source_profile=_library_identity_resolver(self.path)
            except Exception:
                source_profile=None
        self.status_label=QLabel(); self.status_label.setObjectName("mutedLabel"); self.status_label.hide()
        self.rating_label=QLabel(); self.rating_label.setObjectName("mutedLabel"); self.rating_label.hide()
        self.context_note=QLabel(); self.context_note.setWordWrap(True); self.context_note.setObjectName("mutedLabel"); self.context_note.hide()
        self.mark=QCheckBox(); self.mark.setObjectName("cardCheck"); self.mark.setToolTip("Included in the current selection"); self.mark.setAccessibleName("Include image in selection"); self.mark.toggled.connect(self._mark_changed); self.mark.hide()
        self.note=QLabel(); self.note.setWordWrap(True); self.note.setObjectName("mutedLabel"); self.note.hide()
        footer=QHBoxLayout(); footer.setContentsMargins(0,0,0,0); footer.setSpacing(3); footer.addWidget(self.status_label,1); footer.addWidget(self.mark,0,Qt.AlignRight)
        layout.addWidget(self.preview_layer,1); layout.addWidget(name); layout.addWidget(self.library_label); layout.addWidget(self.context_note); layout.addWidget(self.rating_label); layout.addWidget(self.note); layout.addLayout(footer)
        if source_profile is not None:
            if isinstance(source_profile,(tuple,list)):
                values=[*source_profile,"","",None]
                self.set_source_library(values[0],values[1],values[2])
                self.set_storage_availability(values[3])
            else:
                self.set_source_library(
                    source_profile.name,source_profile.display_role,source_profile.root,
                )

    def set_storage_availability(self,availability) -> None:
        self.storage_availability=availability
        if availability is None:
            return
        self.storage_readable=bool(getattr(availability,"readable",True))
        self.storage_writable=bool(getattr(availability,"ready_for_write",True))
        if not self.storage_readable:
            label=str(getattr(availability,"label","Unavailable"))
            self.preview.setText(label+"\nRefresh after restoring local access")
        if not self.storage_writable:
            self.allow_data_edit=False

    @staticmethod
    def _availability_key(availability):
        if availability is None:
            return None
        state=getattr(availability,"state","")
        return (
            str(getattr(state,"value",state)),
            bool(getattr(availability,"readable",True)),
            bool(getattr(availability,"ready_for_write",True)),
            str(getattr(availability,"label","") or ""),
            str(getattr(availability,"detail","") or ""),
            str(getattr(availability,"recovery","") or ""),
        )

    def library_identity_is_current(self) -> bool:
        """Detect availability/identity changes that require a fresh card."""
        if _library_identity_resolver is None:
            return True
        try:
            source_profile=_library_identity_resolver(self.path)
        except Exception:
            return True
        if source_profile is None:
            return not self.library_label.text() and self.storage_availability is None
        if isinstance(source_profile,(tuple,list)):
            name=source_profile[0] if len(source_profile)>0 else ""
            role=source_profile[1] if len(source_profile)>1 else ""
            root=source_profile[2] if len(source_profile)>2 else ""
            availability=source_profile[3] if len(source_profile)>3 else None
        else:
            name=getattr(source_profile,"name","")
            role=getattr(source_profile,"display_role","")
            root=getattr(source_profile,"root","")
            availability=None
        name=str(name or "").strip(); role=str(role or "").strip()
        detail=" • ".join(value for value in (name,role) if value)
        tooltip="\n".join(value for value in (detail,str(root or "")) if value)
        return (
            self.library_label.text()==name
            and self.library_label.toolTip()==tooltip
            and self._availability_key(self.storage_availability)
            ==self._availability_key(availability)
        )

    def set_file_actions(self,actions,*,read_only_actions=()) -> None:
        """Add page-owned file actions to the shared mouse/keyboard context menu.

        Each entry is ``(stable_id, one_image_label, selected_images_label)``.
        The card reports only the stable action and exact active/selected scope;
        page controllers retain all storage and confirmation authority.
        Read-only actions need readable source images but do not modify them.
        """
        self._file_actions=tuple(
            (str(action_id),str(one_label),str(selected_label))
            for action_id,one_label,selected_label in actions
        )
        self._read_only_file_actions=frozenset(str(value) for value in read_only_actions)

    def set_generation_actions(self,enabled: bool) -> None:
        """Expose image/video planning only where the owning gallery handles it."""
        self.allow_generation_actions=bool(enabled)

    def set_source_library(self,name: str,role: str="",root: str="") -> None:
        name=str(name or "").strip(); role=str(role or "").strip()
        self.library_label.setText(name)
        detail=" • ".join(value for value in (name,role) if value)
        self.library_label.setToolTip(
            "\n".join(value for value in (detail,str(root or "")) if value)
        )
        self.library_label.setAccessibleName(
            f"Source library: {detail or name}"
        )
        self.library_label.setVisible(bool(name))
        self._library_accessible=(f", source library {detail}" if detail else "")
        self.set_visual_status(self.visual_status,self._status_detail)

    def set_thumbnail(self,image: QImage):
        if image.isNull(): self.preview.setText("Preview unavailable")
        else: self.preview.setPixmap(QPixmap.fromImage(image))

    def set_content_protection_required(self,required: bool):
        self._content_protection_required=bool(required)

    def set_content_blurred(
        self,blurred: bool,rating: str="",*,protection_required=None
    ):
        if protection_required is not None:
            self._content_protection_required=bool(protection_required)
        elif blurred:
            self._content_protection_required=True
        if rating:
            self._content_blur_reason=str(rating)
        self._content_blurred=bool(blurred)
        if self._content_blurred:
            effect=QGraphicsBlurEffect(self.preview); effect.setBlurRadius(28); self.preview.setGraphicsEffect(effect)
            self.preview.setToolTip(
                "Blurred while CollectiGen checks this image's content rating."
                if rating in {"checking","checking saved rating"} else
                "Blurred because this image has no dependable rating yet. Double-click to review it or use View Image to reveal it intentionally."
                if rating=="unknown" else
                f"Blurred because this image is classified {rating}. Double-click to edit it or use View Image to reveal it intentionally."
            )
        else:
            self.preview.setGraphicsEffect(None); self.preview.setToolTip("")

    def set_content_rating(self,rating: str,basis: str=""):
        normalized=rating if rating in {"general","sensitive","questionable","explicit","unknown"} else "unknown"
        self._content_rating=normalized; self._content_rating_basis=str(basis or "")
        self.set_visual_status(self.visual_status,self._status_detail)

    def set_safety_review(self,status: str="",detail: str="") -> None:
        """Show a safety-review signal without presenting it as a legal fact."""
        normalized=str(status or "").strip().casefold()
        self._safety_review_detail=str(detail or "")
        visible=normalized in {"suspected","confirmed"}
        label=(
            "Confirmed safety restriction" if normalized=="confirmed"
            else "Suspected safety issue — review required"
        )
        self.safety_flag_badge.setToolTip(
            f"{label}\n{self._safety_review_detail}\nDouble-click to open image review."
        )
        self.safety_flag_badge.setAccessibleName(label)
        self.safety_flag_badge.setAccessibleDescription(
            self._safety_review_detail,
        )
        self.safety_flag_badge.setVisible(visible)
        self.set_visual_status(self.visual_status,self._status_detail)

    def set_duplicate_candidate(
        self,kind: str="",*,match_count: int=0,scope_label: str="",
        exact_relations: int=0,visual_relations: int=0,truncated: bool=False,
    ) -> None:
        normalized=str(kind or "").casefold()
        visible=normalized in {"exact","similar"} and int(match_count)>0
        self.duplicate_candidate_badge.set_kind(normalized if visible else "")
        if not visible:
            self.duplicate_candidate_badge.hide()
            return
        title=(
            "Possible exact duplicate" if normalized=="exact"
            else "Visually similar candidate"
        )
        detail=(
            f"{int(match_count):,} current candidate(s) from the latest completed "
            f"duplicate check. Exact relations: {int(exact_relations):,}; "
            f"visual relations: {int(visual_relations):,}."
        )
        if scope_label:
            detail+=f"\nScope: {scope_label}"
        if truncated:
            detail+="\nThe comparison safety bound was reached, so more candidates may exist."
        detail+=(
            "\nThis is review evidence only. Rendering this badge does not scan "
            "images or authorize removal. Right-click to check again."
        )
        self.duplicate_candidate_badge.setToolTip(f"{title}\n{detail}")
        self.duplicate_candidate_badge.setAccessibleName(title)
        self.duplicate_candidate_badge.setAccessibleDescription(detail)
        self.duplicate_candidate_badge.show()

    def reveal_content(self):
        if not self._content_blurred: return
        self.set_content_blurred(False); self.revealed.emit(str(self.path))

    def hide_content_again(self):
        if self._content_blurred or not self._content_protection_required:
            return
        self.set_content_blurred(
            True,self._content_blur_reason or self._content_rating
        )
        self.hidden_again.emit(str(self.path))

    def _choose(self,event=None):
        if event is not None and hasattr(event,"button") and event.button()!=Qt.LeftButton: return
        modifiers=event.modifiers() if event is not None and hasattr(event,"modifiers") else QApplication.keyboardModifiers()
        self.interaction.emit(str(self.path),int(getattr(modifiers,"value",modifiers)))

    def set_note(self,text: str):
        self._note_text=str(text or ""); self._refresh_summary()

    def set_context_note(self,text: str,*,full_text: str=""):
        self._context_note_text=str(text or "")
        self.context_note.setText(self._context_note_text)
        self.context_note.setToolTip(str(full_text or self._context_note_text))
        self.context_note.setVisible(bool(self._context_note_text))
        self.setAccessibleDescription(str(full_text or self._context_note_text))
        self.set_visual_status(self.visual_status,self._status_detail)

    @staticmethod
    def _format_ai_scan_time(value: str) -> str:
        raw=str(value or "")
        try:
            parsed=datetime.fromisoformat(raw.replace("Z","+00:00"))
            if parsed.tzinfo is None: parsed=parsed.replace(tzinfo=timezone.utc)
            return parsed.astimezone().strftime("%Y-%m-%d %H:%M %Z")
        except ValueError:
            return raw

    @staticmethod
    def _format_ai_scan_age(value: str,now: datetime | None=None) -> str:
        raw=str(value or "")
        try:
            parsed=datetime.fromisoformat(raw.replace("Z","+00:00"))
            if parsed.tzinfo is None:
                parsed=parsed.replace(tzinfo=timezone.utc)
        except ValueError:
            return "time unavailable"
        current=now or datetime.now(timezone.utc)
        if current.tzinfo is None:
            current=current.replace(tzinfo=timezone.utc)
        seconds=max(0,int((current-parsed).total_seconds()))
        if seconds<60:
            return "<1m ago"
        if seconds<3600:
            return f"{seconds//60}m ago"
        if seconds<86400:
            return f"{seconds//3600}h ago"
        if seconds<604800:
            return f"{seconds//86400}d ago"
        if seconds<2_592_000:
            return f"{seconds//604800}w ago"
        if seconds<31_536_000:
            return f"{seconds//2_592_000}mo ago"
        return f"{seconds//31_536_000}y ago"

    def set_ai_scan_history(self,sdxl_time: str | None=None,plain_language_time: str | None=None):
        rows=[
            (
                f"SDXL Tags: {self._format_ai_scan_time(sdxl_time)}"
                if sdxl_time else
                ("SDXL Tags: history not loaded" if sdxl_time is None else "SDXL Tags: no recorded completed run")
            ),
            (
                f"Plain Language Descriptions: {self._format_ai_scan_time(plain_language_time)}"
                if plain_language_time else
                ("Plain Language Descriptions: history not loaded" if plain_language_time is None else "Plain Language Descriptions: no recorded completed run")
            ),
        ]
        self._ai_scan_detail="\n".join(rows)
        self._refresh_knowledge_badge()
        self.knowledge_badge.show()
        self.set_visual_status(self.visual_status,self._status_detail)

    def set_knowledge_coverage(self,coverage: ImageKnowledgeCoverage | None=None):
        self.knowledge_badge.coverage=coverage or ImageKnowledgeCoverage()
        self._refresh_knowledge_badge()
        self.knowledge_badge.show()
        self._refresh_border()

    def _refresh_knowledge_badge(self):
        coverage=self.knowledge_badge.coverage
        self.knowledge_badge.problem=self.visual_status=="red"
        self._knowledge_coverage_detail=coverage.tags_detail+"\n"+coverage.descriptions_detail
        if self.knowledge_badge.problem or coverage.problem:
            self._knowledge_coverage_detail+="\nProblem: "+(self._status_detail or "Image data needs attention.")
        if coverage.review_pending:
            self._knowledge_coverage_detail+="\n"+(coverage.review_detail or "Image findings are pending review.")
        detail=self._knowledge_coverage_detail
        if self._ai_scan_detail:
            detail+="\n\nLast recorded AI scans\n"+self._ai_scan_detail
        self.knowledge_badge.setToolTip(detail)
        self.knowledge_badge.setAccessibleDescription(detail)
        self.knowledge_badge.update()

    def set_compact_mode(self,compact: bool=True,*,review_implied: bool=False,problem_implied: bool=False):
        self._review_implied=bool(review_implied)
        self._problem_implied=bool(problem_implied)
        self._compact_mode=bool(compact); self._refresh_summary()

    def _refresh_summary(self):
        label=STATUS_LABELS[self.visual_status]
        if self._compact_mode:
            # Routine confidence and sensitivity remain available through the
            # border, filters, tooltips, accessibility, and blur policy. Only
            # statuses that ask for action need another visible caption.
            show_status=((self.visual_status=="red" and not self._problem_implied) or (
                self.visual_status=="orange" and not self._review_implied
            ))
            parts=[label] if show_status else []
            self.status_label.setText(" • ".join(part for part in parts if part))
            self.status_label.setVisible(bool(self.status_label.text()))
            self.rating_label.hide(); self.note.hide()
        else:
            self.status_label.setText(label); self.status_label.setVisible(self.visual_status!="neutral")
            rating_label=("No dependable rating yet" if self._content_rating=="unknown" else self._content_rating.title())
            self.rating_label.setText(f"Sensitivity: {rating_label}")
            self.rating_label.setVisible(self._content_rating!="general")
            self.note.setText(self._note_text); self.note.setVisible(bool(self._note_text))

    def set_visual_status(self,status: str,detail: str=""):
        self.visual_status=status if status in STATUS_LABELS else "neutral"
        self._status_detail=concise_image_status_detail(detail); label=STATUS_LABELS[self.visual_status]; self._refresh_summary()
        self._refresh_knowledge_badge()
        status_tip=self._status_detail or label
        if self._content_rating!="general":
            rating_label=("No dependable rating yet" if self._content_rating=="unknown" else self._content_rating.title())
            status_tip+=f"\nSensitivity: {rating_label}"
            if self._content_rating_basis: status_tip+=f"\nRating basis: {self._content_rating_basis}"
        context_tip=f"\n{self.context_note.toolTip()}" if self._context_note_text else ""
        safety_tip=f"\nSafety review: {self._safety_review_detail}" if self._safety_review_detail else ""
        self.setToolTip(status_tip+context_tip+safety_tip)
        active=", active image" if self._active_selected else ""
        rating_accessible=(
            ", no dependable content rating yet" if self._content_rating=="unknown" else
            f", content rating {self._content_rating}" if self._content_rating!="general" else ""
        )
        context_accessible=f", {self.context_note.toolTip()}" if self._context_note_text else ""
        ai_accessible=f", AI scan history: {self._ai_scan_detail.replace(chr(10), '; ')}" if self._ai_scan_detail else ""
        status_accessible=f", {self._status_detail}" if self._status_detail else ""
        safety_accessible=f", safety review required: {self._safety_review_detail}" if self._safety_review_detail else ""
        self.setAccessibleName(f"{self.path.name}, {label}{status_accessible}{active}{self._library_accessible}{rating_accessible}{context_accessible}{ai_accessible}{safety_accessible}")
        self._refresh_border()

    def set_active_selected(self,selected: bool):
        self._active_selected=bool(selected); self._refresh_border()

    def set_marked_selected(self,selected: bool):
        self._marked_selected=bool(selected); self._refresh_border()

    def _mark_changed(self,checked: bool):
        self.set_marked_selected(checked); self.marked.emit(str(self.path),checked)

    def _refresh_border(self):
        self.setProperty("marked",self._marked_selected)
        set_visual_status(self,self.visual_status,selected=self._active_selected)
        set_visual_status(self.status_frame,self.visual_status)
        label=STATUS_LABELS[self.visual_status]
        state=", active image" if self._active_selected else ", selected" if self._marked_selected else ""
        rating=(
            ", no dependable content rating yet"
            if self._content_rating=="unknown"
            else f", sensitivity {self._content_rating}"
            if self._content_rating!="general"
            else ""
        )
        context=f", {self.context_note.toolTip()}" if self._context_note_text else ""
        ai=f", AI scan history: {self._ai_scan_detail.replace(chr(10), '; ')}" if self._ai_scan_detail else ""
        status_detail=f", {self._status_detail}" if self._status_detail else ""
        safety=f", safety review required: {self._safety_review_detail}" if self._safety_review_detail else ""
        coverage=f", {self._knowledge_coverage_detail.replace(chr(10), '; ')}" if self._knowledge_coverage_detail else ""
        self.setAccessibleName(f"{self.path.name}, {label}{status_detail}{self._library_accessible}{rating}{state}{context}{coverage}{ai}{safety}")

    def mouseDoubleClickEvent(self,event):
        self.activate_default(event)
        super().mouseDoubleClickEvent(event)

    def activate_default(self,event=None):
        if event is not None and hasattr(event,"button") and event.button()!=Qt.LeftButton: return
        if self.allow_data_edit and self.storage_writable: self.edit_requested.emit(str(self.path))
        else: self.show_large_preview()

    def show_large_preview(self):
        if not self.storage_readable:
            return
        dialog=QDialog(self); dialog.setWindowTitle(self.path.name); dialog.resize(1100,800)
        layout=QVBoxLayout(dialog)
        protected=ProtectedImageView(
            policy=_content_safety_policy,
            settings=getattr(_content_safety_policy,"settings",None),
        )
        protected.set_path(self.path)
        layout.addWidget(protected,1)
        buttons=QDialogButtonBox(QDialogButtonBox.Close); buttons.rejected.connect(dialog.reject); layout.addWidget(buttons)
        dialog.exec()

    def contextMenuEvent(self,event):
        self._show_context_menu(event.globalPos())

    def keyPressEvent(self,event):
        if event.key() in {Qt.Key_Return,Qt.Key_Enter}:
            self.activate_default(); event.accept(); return
        if event.key()==Qt.Key_Space:
            self._choose(); event.accept(); return
        if event.key()==Qt.Key_Menu or (event.key()==Qt.Key_F10 and event.modifiers()&Qt.ShiftModifier):
            self._show_context_menu(self.mapToGlobal(self.rect().center())); event.accept(); return
        super().keyPressEvent(event)

    def _build_context_menu(self):
        menu=QMenu(self); reveal=menu.addAction("Reveal Blurred Image") if self._content_blurred else None
        hide_again=(
            menu.addAction("Hide Image Again")
            if not self._content_blurred and self._content_protection_required
            else None
        )
        status=menu.addAction(f"Status: {STATUS_LABELS[self.visual_status]}"); status.setEnabled(False)
        edit=menu.addAction("Edit Image Data…") if self.allow_data_edit else None
        add_tag=None; add_tag_scope="this"
        if self.selection_enabled:
            if self._marked_selected and self.gallery_selection_count>1:
                add_tag=menu.addAction(f"Add Tag to {self.gallery_selection_count:,} Selected Images…")
                add_tag_scope="selected"
            else:
                add_tag=menu.addAction("Add Tag to This Image…")
        ai_menu=menu.addMenu("Run AI…") if getattr(self,"allow_ai_actions",True) else None
        ai_actions={}
        if ai_menu is not None:ai_actions[ai_menu.addAction("This image…")]=(None,"this")
        if ai_menu is not None and self.selection_enabled and self.gallery_selection_count>1:
            ai_menu.addSeparator()
            action=ai_menu.addAction(
                f"All {self.gallery_selection_count:,} selected images…"
            )
            ai_actions[action]=(None,"selected")
        duplicate_actions={}
        if self.allow_duplicate_check:
            duplicate_menu=menu.addMenu("Check for Duplicates…")
            duplicate_actions[duplicate_menu.addAction("This image…")]="this"
            if self.selection_enabled and self.gallery_selection_count>1:
                duplicate_actions[duplicate_menu.addAction(
                    f"All {self.gallery_selection_count:,} selected images…"
                )]="selected"
        file_location=menu.addAction("Open File Location")
        view_image=menu.addAction("View Image")
        remix=image_setup=animate=None
        if self.allow_generation_actions:
            remix=menu.addAction("Remix This Image…")
            remix.setToolTip("Start a new variation draft from this exact image and its available generation evidence.")
            animate=menu.addAction("Animate This Image…")
            animate.setToolTip("Start a new video draft from this exact image. The original image stays unchanged.")
            image_setup=menu.addAction("Create Generation Setup from Image…")
            image_setup.setToolTip("Review recorded settings and saved Tags or Descriptions before saving a reusable setup. No image pixels are copied.")
        external=menu.addAction("Open in External Viewer")
        copy_path=menu.addAction("Copy File Path")
        file_actions={}
        if self._file_actions:
            menu.addSeparator()
            selected_scope=self._marked_selected and self.gallery_selection_count>1
            scope="selected" if selected_scope else "this"
            move_menu=None
            move_destinations={
                "library":"Named Library…","problems":"Problems…",
                "recycle":"Recycle Bin…","folder":"Folder…","ingest":"Ingest…",
            }
            for action_id,one_label,selected_label in self._file_actions:
                if action_id in move_destinations:
                    if move_menu is None:
                        move_menu=menu.addMenu(
                            f"Move {self.gallery_selection_count:,} Selected Images to…"
                            if selected_scope else "Move This Image to…"
                        )
                        move_menu.setEnabled(self.storage_writable)
                    action=move_menu.addAction(move_destinations[action_id])
                else:
                    action=menu.addAction(selected_label if selected_scope else one_label)
                action.setEnabled(
                    self.storage_readable if action_id in self._read_only_file_actions
                    else self.storage_writable
                )
                file_actions[action]=(action_id,scope)
        if not self.storage_writable:
            if edit is not None: edit.setEnabled(False)
            if add_tag is not None: add_tag.setEnabled(False)
            if ai_menu is not None:ai_menu.setEnabled(False)
        if not self.storage_readable:
            view_image.setEnabled(False); external.setEnabled(False)
            if remix is not None: remix.setEnabled(False)
            if image_setup is not None: image_setup.setEnabled(False)
            if animate is not None: animate.setEnabled(False)
        return menu,{
            "reveal":reveal,
            "hide_again":hide_again,
            "edit":edit,
            "add_tag":add_tag,
            "add_tag_scope":add_tag_scope,
            "ai_actions":ai_actions,
            "duplicate_actions":duplicate_actions,
            "file_location":file_location,
            "view_image":view_image,
            "remix":remix,
            "image_setup":image_setup,
            "animate":animate,
            "external":external,
            "copy_path":copy_path,
            "file_actions":file_actions,
        }

    def _show_context_menu(self,global_position):
        menu,actions=self._build_context_menu()
        chosen=menu.exec(global_position)
        reveal=actions["reveal"]; hide_again=actions["hide_again"]
        edit=actions["edit"]; add_tag=actions["add_tag"]
        if reveal is not None and chosen is reveal: self.reveal_content()
        elif hide_again is not None and chosen is hide_again: self.hide_content_again()
        elif edit is not None and chosen is edit: self.edit_requested.emit(str(self.path))
        elif add_tag is not None and chosen is add_tag: self.add_tag_requested.emit(str(self.path),actions["add_tag_scope"])
        elif chosen in actions["ai_actions"]:
            kind,scope=actions["ai_actions"][chosen]; self.force_ai_requested.emit(str(self.path),kind or "",scope)
        elif chosen in actions.get("duplicate_actions",{}):
            self.duplicate_check_requested.emit(str(self.path),actions["duplicate_actions"][chosen])
        elif chosen is actions["file_location"]: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path.parent)))
        elif chosen is actions["view_image"]: self.show_large_preview()
        elif actions["remix"] is not None and chosen is actions["remix"]: self.file_action_requested.emit(str(self.path),"remix","this")
        elif actions["image_setup"] is not None and chosen is actions["image_setup"]: self.file_action_requested.emit(str(self.path),"image_setup","this")
        elif actions["animate"] is not None and chosen is actions["animate"]: self.file_action_requested.emit(str(self.path),"animate_this_image","this")
        elif chosen is actions["external"]: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.path)))
        elif chosen is actions["copy_path"]: QApplication.clipboard().setText(str(self.path))
        elif chosen in actions["file_actions"]:
            action_id,scope=actions["file_actions"][chosen]
            self.file_action_requested.emit(str(self.path),action_id,scope)


class ImageCardGrid(QWidget):
    """Shared asynchronous card grid for primary image collections."""
    chosen=Signal(str); marked=Signal(str,bool); selection_replaced=Signal(object); edit_requested=Signal(str)
    force_ai_requested=Signal(object,str)
    add_tag_requested=Signal(object)
    file_action_requested=Signal(object,str)
    duplicate_check_requested=Signal(object)

    def __init__(self,parent=None):
        super().__init__(parent); self.generation=0; self.cards={}; self.pool=QThreadPool.globalInstance(); self._selection_anchor=None; self._all_selected=set()
        self._columns=3; self._card_width=180; self._empty_widget=None
        self.grid=QGridLayout(self); self.grid.setContentsMargins(0,0,0,0); self.grid.setAlignment(Qt.AlignTop)

    def set_paths(
        self,paths,*,columns: int=3,card_width: int=180,
        show_marks: bool=False,allow_edit: bool=True,selected=(),active=None,
        configure=None,empty_text: str="",incremental: bool=False,
        batch_size: int=4,
    ):
        visible,_=filter_content_paths(paths)
        selected={str(value) for value in selected}
        path_keys=tuple(str(Path(value)) for value in visible)
        retained=(
            bool(path_keys)
            and path_keys==tuple(self.cards)
            and int(card_width)==self._card_width
            and bool(show_marks)==getattr(self,"_show_marks",False)
            and bool(allow_edit)==getattr(self,"_allow_edit",True)
            and self._empty_widget is None
            and all(card.library_identity_is_current() for card in self.cards.values())
        )
        if retained:
            # Status, content-rating, selection, and inspector completions often
            # ask the page to present the same visible paths again. Preserve the
            # existing widgets and decoded thumbnails; only their live state
            # needs to be republished.
            self.set_columns(columns)
            self._all_selected=set(selected)
            for key,card in self.cards.items():
                marked=key in selected
                card.selection_enabled=bool(show_marks)
                card.allow_data_edit=bool(allow_edit and card.storage_writable)
                blocked=card.mark.blockSignals(True)
                card.mark.setChecked(marked)
                card.mark.blockSignals(blocked)
                card.set_marked_selected(marked)
                card.set_active_selected(card.path==active)
                if configure: configure(card.path,card)
                if _content_safety_policy:
                    _content_safety_policy(card.path,card)
            self._update_selection_counts()
            return
        self.generation+=1; generation=self.generation; self.cards={}; self._all_selected=set(selected)
        self._card_width=int(card_width)
        self._show_marks=bool(show_marks); self._allow_edit=bool(allow_edit)
        self._columns=max(1,int(columns)); self._empty_widget=None
        while self.grid.count():
            item=self.grid.takeAt(0); widget=item.widget()
            if widget:
                # A nested modal event loop can defer DeleteLater delivery. Hide
                # and disable retired children immediately so an old card or
                # loading label cannot remain visible over the next generation.
                widget.hide(); widget.setEnabled(False); widget.deleteLater()
        if not visible and empty_text:
            empty=EmptyStatePanel(empty_text,minimum_height=240)
            self._empty_widget=empty; self.grid.addWidget(empty,0,0,1,self._columns)
        if not visible:
            self._update_selection_counts()
            return
        if incremental:
            loading=EmptyStatePanel(
                f"Loading {len(visible):,} visible image cards…",minimum_height=240,
            )
            self._empty_widget=loading
            self.grid.addWidget(loading,0,0,1,self._columns)
            QTimer.singleShot(
                16,lambda:self._append_paths(
                    visible,0,generation,card_width,show_marks,allow_edit,
                    selected,active,configure,max(1,int(batch_size)),
                ),
            )
        else:
            self._append_paths(
                visible,0,generation,card_width,show_marks,allow_edit,
                selected,active,configure,max(1,len(visible)),schedule=False,
            )
        self._update_selection_counts()

    def _append_paths(
        self,paths,start,generation,card_width,show_marks,allow_edit,
        selected,active,configure,batch_size,*,schedule=True,offset=0,
    ):
        if generation!=self.generation:
            return
        if schedule and not self.isVisible():
            if not self.window().isVisible():
                return
            QTimer.singleShot(
                50,lambda:self._append_paths(
                    paths,start,generation,card_width,show_marks,allow_edit,
                    selected,active,configure,batch_size,offset=offset,
                ),
            )
            return
        if self._empty_widget is not None:
            self.grid.removeWidget(self._empty_widget)
            self._empty_widget.hide(); self._empty_widget.deleteLater()
            self._empty_widget=None
        stop=min(len(paths),start+batch_size)
        for index in range(start,stop):
            value=paths[index]
            path=Path(value); card=ImageCard(path,card_width=card_width); self.cards[str(path)]=card
            card.selection_enabled=bool(show_marks); card.mark.hide(); card.allow_data_edit=bool(allow_edit and card.storage_writable); card.mark.setChecked(str(path) in selected)
            card.set_marked_selected(str(path) in selected); card.set_active_selected(path==active)
            if configure: configure(path,card)
            if _content_safety_policy: _content_safety_policy(path,card)
            card.interaction.connect(self._interact); card.marked.connect(self._marked); card.edit_requested.connect(self.edit_requested)
            card.force_ai_requested.connect(self._force_ai)
            card.add_tag_requested.connect(self._add_tag)
            card.file_action_requested.connect(self._file_action)
            card.duplicate_check_requested.connect(self._check_duplicates)
            grid_index=offset+index
            self.grid.addWidget(
                card,grid_index//self._columns,grid_index%self._columns,
            )
            if card.storage_readable:
                task=ThumbnailTask(path,generation,size=(max(160,card_width),max(120,int(card_width*.8)))); task.signals.ready.connect(self._ready); self.pool.start(task)
        self._update_selection_counts()
        if schedule and stop<len(paths):
            QTimer.singleShot(
                16,lambda:self._append_paths(
                    paths,stop,generation,card_width,show_marks,allow_edit,
                    selected,active,configure,batch_size,offset=offset,
                ),
            )

    def append_paths(
        self,paths,*,columns: int | None=None,card_width: int | None=None,
        show_marks: bool=False,allow_edit: bool=True,selected=(),active=None,
        configure=None,
    ):
        """Append cards without invalidating retained thumbnails or card state."""
        if columns is not None:
            self.set_columns(columns)
        visible,_=filter_content_paths(paths)
        existing=set(self.cards)
        additions=[Path(value) for value in visible if str(Path(value)) not in existing]
        if not additions:
            self._update_selection_counts()
            return
        selected={str(value) for value in selected}
        self._all_selected.update(selected)
        self._append_paths(
            additions,0,self.generation,
            self._card_width if card_width is None else int(card_width),
            show_marks,allow_edit,self._all_selected,active,configure,
            len(additions),schedule=False,offset=len(self.cards),
        )

    def set_columns(self,columns: int):
        """Reflow existing cards without discarding selection or loaded thumbnails."""
        columns=max(1,int(columns))
        if columns==self._columns: return
        self._columns=columns
        if self._empty_widget is not None:
            self.grid.removeWidget(self._empty_widget)
            self.grid.addWidget(self._empty_widget,0,0,1,self._columns)
        else:
            for index,card in enumerate(self.cards.values()):
                self.grid.removeWidget(card)
                self.grid.addWidget(card,index//self._columns,index%self._columns)
        self.grid.invalidate(); self.updateGeometry()

    def _marked(self,path: str,checked: bool):
        if checked: self._all_selected.add(path)
        else: self._all_selected.discard(path)
        self._update_selection_counts(); self.marked.emit(path,checked)

    def _update_selection_counts(self):
        count=len(self._all_selected)
        for card in self.cards.values(): card.gallery_selection_count=count

    def _force_ai(self,path: str,kind: str,scope: str):
        selected=[Path(value) for value in self._all_selected]
        paths=selected if scope=="selected" and selected else [Path(path)]
        self.force_ai_requested.emit(paths,kind)

    def _add_tag(self,path: str,scope: str):
        selected=[Path(value) for value in self._all_selected]
        paths=selected if scope=="selected" and selected else [Path(path)]
        self.add_tag_requested.emit(paths)

    def _check_duplicates(self,path: str,scope: str):
        selected=[Path(value) for value in sorted(self._all_selected)]
        paths=selected if scope=="selected" else [Path(path)]
        if paths:
            self.duplicate_check_requested.emit(paths)

    def _file_action(self,path: str,action_id: str,scope: str):
        selected=[Path(value) for value in self._all_selected]
        paths=selected if scope=="selected" and selected else [Path(path)]
        self.file_action_requested.emit(paths,action_id)

    def _interact(self,path: str,modifiers: int):
        card=self.cards.get(path)
        if card is None: return
        if not card.selection_enabled:
            self.chosen.emit(path); return
        keys=list(self.cards); control=bool(modifiers&Qt.ControlModifier.value); shift=bool(modifiers&Qt.ShiftModifier.value)
        if shift and self._selection_anchor in self.cards:
            first=keys.index(self._selection_anchor); last=keys.index(path)
            selected=set(keys[min(first,last):max(first,last)+1])
            if control: selected.update(key for key,value in self.cards.items() if value.mark.isChecked())
            self._replace_visible_selection(selected); self.selection_replaced.emit(list(selected))
        elif control:
            card.mark.setChecked(not card.mark.isChecked()); self._selection_anchor=path
        else:
            self._replace_visible_selection({path}); self.selection_replaced.emit([path]); self._selection_anchor=path
        self.chosen.emit(path)

    def _replace_visible_selection(self,selected):
        self._all_selected=set(selected)
        for path,card in self.cards.items():
            card.mark.blockSignals(True); card.mark.setChecked(path in selected); card.set_marked_selected(path in selected); card.mark.blockSignals(False)
        self._update_selection_counts()

    @Slot(str,object,int)
    def _ready(self,path,image,generation):
        if generation==self.generation and path in self.cards: self.cards[path].set_thumbnail(image)


class CompactGalleryStatusController(QObject):
    """Background, cached Image Knowledge status for compact contextual cards."""

    updated=Signal(object)

    def __init__(self,assessor,*,pool=None,parent=None):
        super().__init__(parent); self.assessor=assessor; self.pool=pool or QThreadPool.globalInstance()
        self._cache={}; self._errors={}; self._pending=set(); self._discard_pending=set(); self._queued={}; self._task=None

    @staticmethod
    def _keys(paths):
        return tuple(dict.fromkeys(str(Path(value)) for value in paths))

    def configure(self,path,card):
        card.set_compact_mode(True); key=str(Path(path))
        card.set_knowledge_coverage()
        availability=_storage_availability(path)
        if availability is not None and str(
            getattr(availability.state,"value",availability.state)
        )!="available":
            detail=" ".join(value for value in (
                str(getattr(availability,"detail","") or ""),
                str(getattr(availability,"recovery","") or ""),
            ) if value).strip()
            card.set_visual_status("orange",detail)
            card.set_note(str(getattr(availability,"label","Storage attention required")))
            card.set_ai_scan_history()
            return
        state=self._cache.get(key)
        if state is not None:
            card.set_knowledge_coverage(image_knowledge_coverage(state))
            card.set_visual_status(str(getattr(state,"color","neutral")),str(getattr(state,"detail","")))
            card.set_ai_scan_history(
                str(getattr(state,"last_sdxl_ai_scan","")),
                str(getattr(state,"last_plain_language_ai_scan","")),
            )
        elif key in self._errors:
            card.set_visual_status("red",f"Image data status could not be checked: {self._errors[key]}")
            card.set_ai_scan_history()
        else:
            card.set_visual_status("neutral","Checking Image Knowledge in the background")
            card.set_ai_scan_history()

    def request(self,paths,*,force: bool=False):
        keys=tuple(
            key for key in self._keys(paths)
            if (
                (availability:=_storage_availability(key)) is None
                or str(getattr(availability.state,"value",availability.state))=="available"
            )
        )
        if force:
            for key in keys:
                self._cache.pop(key,None); self._errors.pop(key,None); self._queued.setdefault(key,None)
                if key in self._pending:self._discard_pending.add(key)
        for key in keys:
            if key not in self._cache and (key not in self._pending or key in self._discard_pending):self._queued.setdefault(key,None)
        self._start_next()

    def invalidate(self,paths=()):
        keys=self._keys(paths)
        if not keys:
            self._cache.clear(); self._errors.clear(); self._queued.clear(); self._discard_pending.update(self._pending); return
        for key in keys:
            self._cache.pop(key,None); self._errors.pop(key,None); self._queued.pop(key,None)
            if key in self._pending:self._discard_pending.add(key)

    def _start_next(self):
        if self._task is not None or not self._queued:return
        batch=tuple(self._queued); self._queued.clear(); self._pending.update(batch)
        task=AsyncCallTask(lambda:{
            key:self.assessor(Path(key)) for key in batch
            if (
                (availability:=_storage_availability(key)) is None
                or str(getattr(availability.state,"value",availability.state))=="available"
            )
        }); self._task=task
        task.signals.complete.connect(lambda result,values=batch:self._complete(values,result))
        task.signals.failed.connect(lambda message,values=batch:self._failed(values,message))
        self.pool.start(task)

    def _complete(self,batch,result):
        self._pending.difference_update(batch); self._task=None
        current={key:value for key,value in result.items() if key not in self._queued and key not in self._discard_pending}; self._cache.update(current)
        self._discard_pending.difference_update(batch)
        if current:self.updated.emit(tuple(current))
        self._start_next()

    def _failed(self,batch,message):
        self._pending.difference_update(batch); self._task=None
        current=tuple(key for key in batch if key not in self._queued and key not in self._discard_pending)
        self._discard_pending.difference_update(batch)
        self._errors.update({key:str(message) for key in current})
        if current:self.updated.emit(current)
        self._start_next()


class EmbeddedGallery(QWidget):
    """Responsive bounded gallery for contextual image evidence inside a page."""
    chosen=Signal(str); edit_requested=Signal(str); force_ai_requested=Signal(object,str); open_dataset_requested=Signal(); visible_paths_changed=Signal(object)

    def __init__(self,parent=None,*,page_size: int=12,card_width: int=150,show_open_dataset: bool=True,compact_empty: bool=False):
        super().__init__(parent); self.page_size=max(1,int(page_size)); self.card_width=max(112,int(card_width)); self.cards={}
        self._compact_empty=bool(compact_empty); self._show_open_dataset=bool(show_open_dataset)
        self._paths=[]; self._shown=self.page_size; self._total=0; self._configure=None; self._active_path=""
        layout=QVBoxLayout(self); layout.setContentsMargins(0,0,0,0); layout.setSpacing(6)
        toolbar=QHBoxLayout(); self.context_label=QLabel("Example images"); self.context_label.setObjectName("sectionTitle")
        self.status=QLabel("No context selected"); self.status.setObjectName("mutedLabel"); self.status.setWordWrap(True)
        self.more=QPushButton("Show More"); self.more.setProperty("compact",True); self.more.clicked.connect(self.show_more)
        self.open_dataset=QPushButton("Open in Dataset…"); self.open_dataset.setProperty("compact",True); self.open_dataset.clicked.connect(self.open_dataset_requested)
        self.open_dataset.setVisible(bool(show_open_dataset))
        toolbar.addWidget(self.context_label)
        if not self._compact_empty:toolbar.addWidget(self.status,1)
        else:toolbar.addStretch(1)
        toolbar.addWidget(self.more); toolbar.addWidget(self.open_dataset); layout.addLayout(toolbar)
        if self._compact_empty:layout.addWidget(self.status)
        self.scroll=QScrollArea(); self.scroll.setWidgetResizable(True); self.card_grid=ImageCardGrid(); self.card_grid.chosen.connect(self._choose)
        self.card_grid.edit_requested.connect(self.edit_requested); self.card_grid.force_ai_requested.connect(self.force_ai_requested)
        self.scroll.setWidget(self.card_grid); layout.addWidget(self.scroll,1)
        if self._compact_empty:
            self.scroll.setMinimumHeight(230)
            self.scroll.viewport().installEventFilter(self)
        self.setAccessibleName("Context example image gallery")
        self._sync_empty_presentation()

    def _sync_empty_presentation(self):
        if not self._compact_empty:return
        has_examples=bool(self._paths)
        self.scroll.setVisible(has_examples)
        self.open_dataset.setVisible(self._show_open_dataset and has_examples)

    def eventFilter(self, watched, event):
        if watched is self.scroll.viewport() and event.type()==QEvent.Resize and self.card_grid.cards:
            self.card_grid.set_columns(self._columns())
        return super().eventFilter(watched,event)

    def set_loading(self,context: str="Example images"):
        self.context_label.setText(context); self.status.setText("Loading matching images…")
        self._paths=[]; self._total=0; self._active_path=""; self.more.hide(); self.open_dataset.setEnabled(False)
        self.card_grid.set_paths((),columns=self._columns(),card_width=self.card_width,empty_text="Loading matching images…")
        self.cards=self.card_grid.cards
        self._sync_empty_presentation()

    def set_unavailable(self,message: str="Open a library to see matching examples.",context: str="Example images"):
        self.context_label.setText(context); self.status.setText(message); self._paths=[]; self._total=0; self._active_path=""
        self.more.hide(); self.open_dataset.setEnabled(False)
        self.card_grid.set_paths((),columns=self._columns(),card_width=self.card_width,empty_text=message); self.cards=self.card_grid.cards
        self._sync_empty_presentation()

    def set_error(self,message: str,context: str="Example images"):
        self.set_unavailable(f"Examples could not be loaded: {message}",context)

    def set_paths(self,paths,*,total_count: int | None=None,context: str="Example images",configure=None):
        deduplicated=[]; seen=set()
        for value in paths:
            key=str(Path(value))
            if key in seen:continue
            seen.add(key); deduplicated.append(Path(value))
        self.context_label.setText(context); self._paths=deduplicated; self._total=max(len(deduplicated),int(total_count) if total_count is not None else len(deduplicated)); self._shown=min(self.page_size,len(deduplicated)); self._configure=configure
        if self._active_path not in {str(path) for path in deduplicated}:self._active_path=""
        self._refresh()

    def show_more(self):
        previous=self._shown
        self._shown=min(len(self._paths),self._shown+self.page_size)
        self._refresh(append_from=previous)

    def _columns(self):
        width=max(self.card_width,self.scroll.viewport().width() if hasattr(self,"scroll") else self.width())
        return max(1,width//(self.card_width+14))

    def visible_paths(self):
        return tuple(self._paths[:self._shown])

    def refresh_cards(self,*_):
        if self._configure:
            for path,card in self.cards.items():self._configure(Path(path),card)

    def _choose(self,path: str):
        self._active_path=str(Path(path))
        for value,card in self.cards.items():card.set_active_selected(value==self._active_path)
        self.chosen.emit(path)

    def _refresh(self,*,append_from: int | None=None):
        visible=self._paths[:self._shown]; shown=len(visible)
        if not self._paths:self.status.setText("No matching images in the current library filter.")
        elif shown<self._total:self.status.setText(f"Showing {shown:,} of {self._total:,} matching images.")
        else:self.status.setText(f"{self._total:,} matching image"+("" if self._total==1 else "s"))
        self.more.setVisible(shown<len(self._paths)); self.more.setEnabled(shown<len(self._paths)); self.open_dataset.setEnabled(bool(self._total))
        active=Path(self._active_path) if self._active_path in {str(path) for path in visible} else None
        if append_from is not None and self.card_grid.cards:
            self.card_grid.append_paths(
                visible[append_from:],columns=self._columns(),
                card_width=self.card_width,active=active,configure=self._configure,
            )
        else:
            self.card_grid.set_paths(visible,columns=self._columns(),card_width=self.card_width,active=active,configure=self._configure,empty_text="No matching images in the current library filter.")
        self.cards=self.card_grid.cards
        self._sync_empty_presentation()
        self.visible_paths_changed.emit(tuple(visible))

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if self.card_grid.cards:self.card_grid.set_columns(self._columns())


class ReferenceGallery(EmbeddedGallery):
    """Compatibility wrapper for existing bounded reference-gallery callers."""
    def __init__(self,parent=None,limit: int=12):
        self.limit=max(1,int(limit)); super().__init__(parent,page_size=self.limit,show_open_dataset=False)

    def set_paths(self,paths):
        values=list(paths)[:self.limit]
        super().set_paths(values,total_count=len(values),context="Reference images")
