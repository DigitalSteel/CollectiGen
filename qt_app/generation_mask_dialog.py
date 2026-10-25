"""Paint a full-resolution Remix mask without reading or changing source files."""
from __future__ import annotations

from io import BytesIO
from pathlib import Path

from PIL import Image
from PySide6.QtCore import QByteArray, QBuffer, QIODevice, QPointF, QRectF, Qt, QTimer, Signal
from PySide6.QtGui import QColor, QImage, QPainter, QPen, QPixmap
from PySide6.QtWidgets import (QCheckBox, QDialog, QDialogButtonBox, QGraphicsBlurEffect, QHBoxLayout, QLabel,
    QFileDialog, QPushButton, QScrollArea, QSpinBox, QVBoxLayout, QWidget)

from core.generation_masks import (MAX_IMAGE_PIXELS, MAX_IMAGE_SIDE, MaskPayload,
                                   MAX_MASK_DOCUMENT_BYTES, create_mask_payload,
                                   export_mask_document, import_mask_document)
from . import gallery


class _MaskSourceProtection(gallery.ProtectedImageView):
    """Use the shared preview control for a bytes-only, unrated source."""

    def _current_state(self):
        if self.path is None or self.policy is None:
            return {"rating": "unknown", "basis": "No dependable rating yet",
                "checking": False, "requires_blur": True, "blurred": True}
        return super()._current_state()


def _image_png(image: QImage) -> bytes:
    output = QByteArray()
    buffer = QBuffer(output)
    if not buffer.open(QIODevice.WriteOnly) or not image.save(buffer, "PNG"):
        raise ValueError("Could not encode the source or painted mask as PNG.")
    buffer.close()
    return bytes(output)


class _MaskCanvas(QWidget):
    stroke_started = Signal()
    stroke_finished = Signal()
    mask_changed = Signal()

    def __init__(self, source: QImage, mask: QImage, parent=None):
        super().__init__(parent)
        self.source = source.convertToFormat(QImage.Format_RGBA8888)
        self.mask = mask
        self.scale = 1.0
        self.brush_size = 32
        self.erase = False
        self.editable = True
        self.shape = "brush"
        self._last = None
        self._shape_origin = None
        self._shape_current = None
        self._overlay = QImage()
        self.crop_region = None
        self.setAccessibleName("Paint editable area on source image")
        self.setMouseTracking(True)
        self._refresh_overlay()
        self.set_scale(1.0)

    def set_scale(self, value):
        self.scale = max(.03, min(8.0, float(value), 16384 / max(self.source.width(), self.source.height())))
        self.setFixedSize(max(1, round(self.source.width() * self.scale)),
                          max(1, round(self.source.height() * self.scale)))
        self.update()

    def _refresh_overlay(self):
        overlay = QImage(self.mask.size(), QImage.Format_ARGB32_Premultiplied)
        overlay.fill(QColor(255, 55, 55, 110))
        gray = self.mask.convertToFormat(QImage.Format_Grayscale8)
        alpha = QImage(gray.size(), QImage.Format_Alpha8)
        alpha.bits()[:] = gray.constBits()[:]
        painter = QPainter(overlay)
        painter.setCompositionMode(QPainter.CompositionMode_DestinationIn)
        painter.drawImage(0, 0, alpha)
        painter.end()
        self._overlay = overlay
        self.mask_changed.emit()
        self.update()

    def replace_mask(self, value):
        self.mask = value.copy()
        self._refresh_overlay()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.SmoothPixmapTransform, True)
        area = QRectF(0, 0, self.width(), self.height())
        painter.drawImage(area, self.source)
        painter.drawImage(area, self._overlay)
        if self.crop_region is not None:
            left, top, right, bottom = self.crop_region
            painter.setPen(QPen(QColor(255, 225, 85), 2, Qt.DashLine))
            painter.drawRect(QRectF(left * self.scale, top * self.scale,
                                    (right - left) * self.scale,
                                    (bottom - top) * self.scale))
        if self._shape_origin is not None and self._shape_current is not None:
            area = QRectF(self._shape_origin, self._shape_current).normalized()
            painter.setPen(QPen(QColor(255, 235, 100), 2, Qt.DashLine))
            if self.shape == "ellipse":
                painter.drawEllipse(area)
            else:
                painter.drawRect(area)
        painter.setPen(QColor(225, 225, 225, 160))
        painter.drawRect(self.rect().adjusted(0, 0, -1, -1))

    def _image_point(self, position):
        x = max(0.0, min(self.source.width() - 1.0, position.x() / self.scale))
        y = max(0.0, min(self.source.height() - 1.0, position.y() / self.scale))
        return QPointF(x, y)

    def _stroke(self, point):
        if not self.editable:
            return
        painter = QPainter(self.mask)
        painter.setRenderHint(QPainter.Antialiasing, True)
        color = QColor(0, 0, 0) if self.erase else QColor(255, 255, 255)
        painter.setPen(QPen(color, self.brush_size, Qt.SolidLine, Qt.RoundCap, Qt.RoundJoin))
        if self._last is None:
            painter.drawPoint(point)
        else:
            painter.drawLine(self._last, point)
        painter.end()
        self._last = point
        self._refresh_overlay()

    def _draw_shape(self, end):
        if self._shape_origin is None:
            return
        start = self._shape_origin
        left, right = sorted((int(start.x()), int(end.x())))
        top, bottom = sorted((int(start.y()), int(end.y())))
        painter = QPainter(self.mask)
        painter.setPen(Qt.NoPen)
        painter.setBrush(QColor(0, 0, 0) if self.erase else QColor(255, 255, 255))
        area = QRectF(left, top, right - left + 1, bottom - top + 1)
        if self.shape == "ellipse":
            painter.setRenderHint(QPainter.Antialiasing, True)
            painter.drawEllipse(area)
        else:
            painter.drawRect(area)
        painter.end()
        self._shape_origin = self._shape_current = None
        self._refresh_overlay()

    def mousePressEvent(self, event):
        if not self.editable:
            event.accept(); return
        if event.button() == Qt.LeftButton:
            self.stroke_started.emit()
            self._last = None
            point = self._image_point(event.position())
            if self.shape == "brush":
                self._stroke(point)
            else:
                self._shape_origin = self._shape_current = point
                self.update()
            event.accept()
            return
        super().mousePressEvent(event)

    def mouseMoveEvent(self, event):
        if not self.editable:
            event.accept(); return
        if event.buttons() & Qt.LeftButton and (self._last is not None or self._shape_origin is not None):
            point = self._image_point(event.position())
            if self.shape == "brush":
                self._stroke(point)
            else:
                self._shape_current = point
                self.update()
            event.accept()
            return
        super().mouseMoveEvent(event)

    def mouseReleaseEvent(self, event):
        if not self.editable:
            self._last = None
            event.accept(); return
        if event.button() == Qt.LeftButton and (self._last is not None or self._shape_origin is not None):
            if self.shape == "brush":
                self._stroke(self._image_point(event.position()))
            else:
                self._draw_shape(self._image_point(event.position()))
            self._last = None
            self.stroke_finished.emit()
            event.accept()
            return
        super().mouseReleaseEvent(event)


class GenerationMaskDialog(QDialog):
    """Return a mask payload only after Apply; Cancel leaves the caller's draft."""

    def __init__(self, source: bytes | QImage, parent=None, *, initial_mask_png: bytes | None = None,
                 settings=None, source_path=None):
        super().__init__(parent)
        self.setWindowTitle("Paint Area for Remix")
        if isinstance(source, QImage):
            source_image = source.copy()
            source_bytes = _image_png(source_image)
        elif isinstance(source, bytes):
            source_bytes = source
            source_image = QImage.fromData(source)
        else:
            raise ValueError("Provide already-authorized source image bytes or a QImage.")
        if source_image.isNull() or not 0 < source_image.width() <= MAX_IMAGE_SIDE or not 0 < source_image.height() <= MAX_IMAGE_SIDE or source_image.width() * source_image.height() > MAX_IMAGE_PIXELS:
            raise ValueError("The source image is unavailable or exceeds the mask editor limits.")
        # QPainter does not support drawing into Grayscale8 on every Qt backend.
        # Keep an RGB32 editing surface and encode grayscale only at Apply.
        mask = QImage(source_image.size(), QImage.Format_RGB32)
        mask.fill(0)
        if initial_mask_png is not None:
            try:
                with Image.open(BytesIO(initial_mask_png)) as previous:
                    if previous.format != "PNG" or previous.mode != "L" or previous.size != (source_image.width(), source_image.height()):
                        raise ValueError("The previous mask must be a full-size grayscale PNG for this source.")
                    previous.load()
            except OSError as exc:
                raise ValueError("The previous mask cannot be decoded.") from exc
            loaded = QImage.fromData(initial_mask_png, "PNG")
            if loaded.isNull() or loaded.size() != source_image.size():
                raise ValueError("The previous mask must match this source image exactly.")
            mask = loaded.convertToFormat(QImage.Format_RGB32)
        self.source_bytes = source_bytes
        self._settings = settings
        self._payload: MaskPayload | None = None
        self._undo: list[QImage] = []
        self._redo: list[QImage] = []
        self._history_limit = max(1, min(12, (96 * 1024 * 1024) // (4 * mask.width() * mask.height())))
        self.canvas = _MaskCanvas(source_image, mask)
        self.canvas.stroke_started.connect(self._remember)
        self.canvas.stroke_finished.connect(self._update_history)
        self.canvas.mask_changed.connect(self._update_mask_preview)
        self._build_ui()
        self.source_protection.set_image(QPixmap.fromImage(source_image), source_path)
        self._sync_protection()
        self._crop_timer = QTimer(self)
        self._crop_timer.setSingleShot(True)
        self._crop_timer.setInterval(120)
        self._crop_timer.timeout.connect(self._update_crop_preview)
        self.canvas.mask_changed.connect(self._schedule_crop_preview)
        self.resize(1180, 760)
        self._fit_on_show = True

    def showEvent(self, event):
        super().showEvent(event)
        if self._fit_on_show:
            self._fit_on_show = False
            self.fit_view()

    def hideEvent(self, event):
        self.prepare_for_privacy_capture()
        super().hideEvent(event)

    def prepare_for_privacy_capture(self):
        self.source_protection.prepare_for_privacy_capture()

    def _sync_protection(self):
        preview = self.source_protection
        editable = not preview._requires_blur or preview._temporarily_revealed
        self.canvas.editable = editable
        self.canvas._last = None
        self.canvas._shape_origin = self.canvas._shape_current = None
        if editable:
            self.canvas.setGraphicsEffect(None)
        else:
            effect = QGraphicsBlurEffect(self.canvas)
            effect.setBlurRadius(28)
            self.canvas.setGraphicsEffect(effect)
        for control in (self.paint_button, self.erase_button, self.brush_size,
                        self.undo_button, self.redo_button, self.clear_button,
                        self.invert_button, self.import_button, self.export_button,
                        self.rectangle_button, self.ellipse_button, self.brush_button,
                        self.apply_button):
            control.setEnabled(editable)
        if not editable:
            self.status.setText("Source preview blurred. Reveal it temporarily to paint or apply a mask; hiding or capture restores protection.")
        else:
            self.status.setText("Paint an editable area, then Apply Mask. Cancel leaves the Remix draft unchanged.")
        if editable:
            self._update_history()

    def _build_ui(self):
        outer = QVBoxLayout(self)
        intro = QLabel("Paint the area the generator may change. Red overlay and white mask are editable; black stays protected. Feather softens only inside the painted area. No source pixels are changed here.")
        intro.setWordWrap(True)
        outer.addWidget(intro)
        self.source_protection = _MaskSourceProtection(
            policy=gallery._content_safety_policy, settings=self._settings)
        self.source_protection.viewer.hide()
        self.source_protection.protection_changed.connect(self._sync_protection)
        outer.addWidget(self.source_protection)
        row = QHBoxLayout()
        self.paint_button = QPushButton("Brush: Paint Editable Area")
        self.erase_button = QPushButton("Erase: Protect Area")
        self.paint_button.setCheckable(True)
        self.erase_button.setCheckable(True)
        self.paint_button.setChecked(True)
        self.paint_button.clicked.connect(lambda: self.set_erase(False))
        self.erase_button.clicked.connect(lambda: self.set_erase(True))
        row.addWidget(self.paint_button)
        row.addWidget(self.erase_button)
        row.addWidget(QLabel("Brush size (source pixels)"))
        self.brush_size = QSpinBox()
        self.brush_size.setRange(1, 512)
        self.brush_size.setValue(32)
        self.brush_size.valueChanged.connect(lambda value: setattr(self.canvas, "brush_size", value))
        row.addWidget(self.brush_size)
        row.addStretch(1)
        outer.addLayout(row)
        selection = QHBoxLayout()
        selection.addWidget(QLabel("Selection tool"))
        self.brush_button = QPushButton("Brush")
        self.rectangle_button = QPushButton("Rectangle")
        self.ellipse_button = QPushButton("Ellipse")
        for button, shape in ((self.brush_button, "brush"),
                              (self.rectangle_button, "rectangle"),
                              (self.ellipse_button, "ellipse")):
            button.setCheckable(True)
            button.clicked.connect(lambda _checked=False, chosen=shape: self.set_shape(chosen))
            selection.addWidget(button)
        self.brush_button.setChecked(True)
        selection.addStretch(1)
        outer.addLayout(selection)
        body = QHBoxLayout()
        self.scroll = QScrollArea()
        self.scroll.setWidgetResizable(False)
        self.scroll.setAlignment(Qt.AlignCenter)
        self.scroll.setWidget(self.canvas)
        body.addWidget(self.scroll, 1)
        panel = QVBoxLayout()
        panel.addWidget(QLabel("Mask preview · white editable, black protected"))
        self.mask_preview = QLabel()
        self.mask_preview.setAccessibleName("Full-resolution black and white mask preview")
        self.mask_preview.setAlignment(Qt.AlignCenter)
        self.mask_preview.setMinimumSize(180, 180)
        panel.addWidget(self.mask_preview)
        self._update_mask_preview()
        feather_label = QLabel("Inward feather (source pixels)")
        panel.addWidget(feather_label)
        self.feather = QSpinBox()
        self.feather.setRange(0, 128)
        self.feather.setAccessibleName("Inward feather pixels")
        panel.addWidget(self.feather)
        self.crop_context = QCheckBox("Crop generator context to painted area")
        self.crop_context.setAccessibleName("Crop context around painted area")
        self.crop_context.toggled.connect(self._crop_toggled)
        panel.addWidget(self.crop_context)
        panel.addWidget(QLabel("Extra context around paint (source pixels)"))
        self.crop_padding = QSpinBox()
        self.crop_padding.setRange(0, 1024)
        self.crop_padding.setValue(64)
        self.crop_padding.setEnabled(False)
        self.crop_padding.valueChanged.connect(self._schedule_crop_preview)
        panel.addWidget(self.crop_padding)
        self.crop_preview = QLabel("Full image will be sent to the generator.")
        self.crop_preview.setWordWrap(True)
        panel.addWidget(self.crop_preview)
        note = QLabel("The yellow outline shows the context sent to the generator. Only painted pixels may change in the final full-size result; unpainted pixels stay protected.")
        note.setWordWrap(True)
        panel.addWidget(note)
        panel.addStretch(1)
        body.addLayout(panel)
        outer.addLayout(body, 1)
        commands = QHBoxLayout()
        self.undo_button = QPushButton("Undo")
        self.redo_button = QPushButton("Redo")
        self.undo_button.clicked.connect(self.undo)
        self.redo_button.clicked.connect(self.redo)
        self.clear_button = QPushButton("Clear Mask")
        self.clear_button.clicked.connect(self.clear_mask)
        self.invert_button = QPushButton("Invert Mask")
        self.invert_button.clicked.connect(self.invert_mask)
        for button in (self.undo_button, self.redo_button, self.clear_button, self.invert_button):
            commands.addWidget(button)
        commands.addStretch(1)
        zoom_out = QPushButton("−")
        zoom_out.setAccessibleName("Zoom out of mask canvas")
        zoom_out.clicked.connect(lambda: self.zoom_by(1 / 1.2))
        zoom_in = QPushButton("+")
        zoom_in.setAccessibleName("Zoom into mask canvas")
        zoom_in.clicked.connect(lambda: self.zoom_by(1.2))
        fit = QPushButton("Fit")
        fit.clicked.connect(self.fit_view)
        commands.addWidget(zoom_out)
        commands.addWidget(zoom_in)
        commands.addWidget(fit)
        outer.addLayout(commands)
        document_commands = QHBoxLayout()
        self.import_button = QPushButton("Import Saved Mask…")
        self.export_button = QPushButton("Export Mask…")
        self.import_button.clicked.connect(self.choose_import_mask)
        self.export_button.clicked.connect(self.choose_export_mask)
        document_commands.addWidget(self.import_button)
        document_commands.addWidget(self.export_button)
        document_commands.addStretch(1)
        outer.addLayout(document_commands)
        self.status = QLabel("Paint an editable area, then Apply Mask. Cancel leaves the Remix draft unchanged.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        outer.addWidget(self.status)
        buttons = QDialogButtonBox()
        self.apply_button = buttons.addButton("Apply Mask", QDialogButtonBox.AcceptRole)
        buttons.addButton(QDialogButtonBox.Cancel)
        self.apply_button.clicked.connect(self.apply_mask)
        buttons.rejected.connect(self.reject)
        outer.addWidget(buttons)
        self._update_history()

    def _update_mask_preview(self):
        if not hasattr(self, "mask_preview"):
            return
        image = self.canvas.mask.scaled(180, 180, Qt.KeepAspectRatio, Qt.SmoothTransformation)
        self.mask_preview.setPixmap(QPixmap.fromImage(image))

    def _crop_toggled(self, enabled):
        self.crop_padding.setEnabled(enabled)
        self._schedule_crop_preview()

    def _schedule_crop_preview(self, *_):
        if hasattr(self, "_crop_timer"):
            self._crop_timer.start()

    def _update_crop_preview(self):
        if not self.crop_context.isChecked():
            self.canvas.crop_region = None
            self.crop_preview.setText("Full image will be sent to the generator.")
            self.canvas.update()
            return
        grayscale = self.canvas.mask.convertToFormat(QImage.Format_Grayscale8)
        with Image.open(BytesIO(_image_png(grayscale))) as painted:
            bounds = painted.getbbox()
        if bounds is None:
            self.canvas.crop_region = None
            self.crop_preview.setText("Paint an editable area to preview its crop context.")
        else:
            padding = self.crop_padding.value()
            left, top, right, bottom = bounds
            region = (max(0, left - padding), max(0, top - padding),
                      min(grayscale.width(), right + padding),
                      min(grayscale.height(), bottom + padding))
            self.canvas.crop_region = region
            self.crop_preview.setText(
                f"Generator context: {region[2] - region[0]} × {region[3] - region[1]} "
                f"pixels, from ({region[0]}, {region[1]}) in the full image."
            )
        self.canvas.update()

    def set_erase(self, erase: bool):
        self.canvas.erase = bool(erase)
        self.paint_button.setChecked(not erase)
        self.erase_button.setChecked(bool(erase))

    def set_shape(self, shape: str):
        if shape not in {"brush", "rectangle", "ellipse"}:
            raise ValueError("Choose a supported selection tool.")
        self.canvas.shape = shape
        self.canvas._shape_origin = self.canvas._shape_current = None
        for button, value in ((self.brush_button, "brush"),
                              (self.rectangle_button, "rectangle"),
                              (self.ellipse_button, "ellipse")):
            button.setChecked(shape == value)
        self.canvas.update()

    def import_mask_bytes(self, raw: bytes):
        if not self.canvas.editable:
            raise ValueError("Reveal the protected source preview before importing a mask.")
        payload = import_mask_document(raw, self.source_bytes)
        loaded = QImage.fromData(payload.mask_png, "PNG")
        if loaded.isNull() or loaded.size() != self.canvas.source.size():
            raise ValueError("The imported mask cannot be displayed at source size.")
        self._remember()
        self.canvas.replace_mask(loaded.convertToFormat(QImage.Format_RGB32))
        self.feather.setValue(payload.feather_px)
        self.crop_context.setChecked(payload.crop_region is not None)
        self.crop_padding.setValue(payload.crop_padding_px)
        self._update_crop_preview()
        self.status.setText("Exact-source mask imported into this draft. Review it, then Apply Mask.")

    def export_mask_bytes(self) -> bytes:
        if not self.canvas.editable:
            raise ValueError("Reveal the protected source preview before exporting a mask.")
        payload = create_mask_payload(self.source_bytes,
            _image_png(self.canvas.mask.convertToFormat(QImage.Format_Grayscale8)),
            feather_px=self.feather.value(), crop_context=self.crop_context.isChecked(),
            crop_padding_px=self.crop_padding.value() if self.crop_context.isChecked() else 0)
        return export_mask_document(payload, self.source_bytes)

    def choose_import_mask(self):
        path, _ = QFileDialog.getOpenFileName(self, "Import exact-source mask", "",
                                              "CollectiGen mask (*.cgmask.json)")
        if not path:
            return
        try:
            with Path(path).open("rb") as stream:
                raw = stream.read(MAX_MASK_DOCUMENT_BYTES + 1)
            self.import_mask_bytes(raw)
        except (OSError, ValueError) as exc:
            self.status.setText(f"Mask import failed: {exc} The current draft is unchanged.")

    def choose_export_mask(self):
        path, _ = QFileDialog.getSaveFileName(self, "Export exact-source mask", "",
                                              "CollectiGen mask (*.cgmask.json)")
        if not path:
            return
        created = False
        try:
            raw = self.export_mask_bytes()
            with Path(path).open("xb") as stream:
                created = True
                stream.write(raw)
            self.status.setText("Mask exported with exact source identity; the draft is unchanged.")
        except FileExistsError:
            self.status.setText("Mask export stopped: that file already exists. Choose a new name.")
        except (OSError, ValueError) as exc:
            if created:
                try:
                    Path(path).unlink()
                except OSError:
                    pass
            self.status.setText(f"Mask export failed: {exc} The draft is unchanged.")

    def _remember(self):
        self._undo.append(self.canvas.mask.copy())
        del self._undo[:-self._history_limit]
        self._redo.clear()
        self._update_history()

    def _update_history(self):
        if hasattr(self, "undo_button"):
            self.undo_button.setEnabled(bool(self._undo))
            self.redo_button.setEnabled(bool(self._redo))

    def undo(self):
        if not self.canvas.editable:
            return
        if self._undo:
            self._redo.append(self.canvas.mask.copy())
            self.canvas.replace_mask(self._undo.pop())
            self._update_history()

    def redo(self):
        if not self.canvas.editable:
            return
        if self._redo:
            self._undo.append(self.canvas.mask.copy())
            del self._undo[:-self._history_limit]
            self.canvas.replace_mask(self._redo.pop())
            self._update_history()

    def clear_mask(self):
        if not self.canvas.editable:
            return
        self._remember()
        mask = self.canvas.mask.copy()
        mask.fill(0)
        self.canvas.replace_mask(mask)

    def invert_mask(self):
        if not self.canvas.editable:
            return
        self._remember()
        mask = self.canvas.mask.copy()
        mask.invertPixels(QImage.InvertRgb)
        self.canvas.replace_mask(mask)

    def zoom_by(self, factor: float):
        self.canvas.set_scale(self.canvas.scale * factor)

    def fit_view(self):
        viewport = self.scroll.viewport().size()
        self.canvas.set_scale(max(.03, min((viewport.width() - 12) / self.canvas.source.width(),
                                          (viewport.height() - 12) / self.canvas.source.height())))

    def apply_mask(self):
        if not self.canvas.editable:
            self.status.setText("Reveal the protected source preview before applying a mask.")
            return
        try:
            payload = create_mask_payload(self.source_bytes, _image_png(self.canvas.mask.convertToFormat(QImage.Format_Grayscale8)),
                                          feather_px=self.feather.value(),
                                          crop_context=self.crop_context.isChecked(),
                                          crop_padding_px=self.crop_padding.value() if self.crop_context.isChecked() else 0)
        except ValueError as exc:
            self.status.setText(str(exc) + " The source and draft remain unchanged.")
            return
        self._payload = payload
        self.accept()

    def result_payload(self) -> MaskPayload | None:
        return self._payload if self.result() == QDialog.Accepted else None
