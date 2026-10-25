"""Numeric, image-free editor for two bounded rectangular prompt regions."""
from __future__ import annotations

from .components import ContextHelpButton
from .help_content import section_help

from PySide6.QtCore import Qt, QSize, QRectF
from PySide6.QtGui import QColor, QPainter, QPen
from PySide6.QtWidgets import (QDialog, QDoubleSpinBox, QFormLayout, QGroupBox,
    QHBoxLayout, QLabel, QPushButton, QTextEdit, QVBoxLayout, QWidget)

from core.generation_regions import (empty_region_spec, render_region_prompt,
    validate_region_spec)


class PercentSpinBox(QDoubleSpinBox):
    """Keep six-decimal editing precision without showing padding zeroes."""

    def textFromValue(self, value):
        return f"{value:.6f}".rstrip("0").rstrip(".")


class RegionSketch(QWidget):
    """Geometry preview only; it never loads an image or captures pixels."""

    def __init__(self, width, height, parent=None):
        super().__init__(parent)
        self.canvas_width = width
        self.canvas_height = height
        self.regions = []
        self.setAccessibleName("Regional rectangle layout")
        self.setMinimumHeight(180)

    def sizeHint(self):
        return QSize(500, 190)

    def set_regions(self, regions):
        self.regions = list(regions)
        self.update()

    def canvas_rect(self):
        """The exact base aspect, centered within the available sketch area."""
        area = QRectF(self.rect().adjusted(12, 10, -12, -10))
        scale = min(area.width() / self.canvas_width,
                    area.height() / self.canvas_height)
        width = self.canvas_width * scale
        height = self.canvas_height * scale
        return QRectF(area.center().x() - width / 2, area.center().y() - height / 2,
                      width, height)

    def region_rect(self, region):
        canvas = self.canvas_rect()
        return QRectF(canvas.x() + region["x"] * canvas.width(),
                      canvas.y() + region["y"] * canvas.height(),
                      max(1, region["width"] * canvas.width()),
                      max(1, region["height"] * canvas.height()))

    def paintEvent(self, _event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing)
        canvas = self.canvas_rect()
        painter.fillRect(canvas, self.palette().base())
        painter.setPen(QPen(self.palette().mid().color(), 2))
        painter.drawRect(canvas)
        if not self.regions:
            painter.setPen(self.palette().text().color())
            painter.drawText(canvas.adjusted(8, 8, -8, -8),
                             Qt.AlignCenter | Qt.TextWordWrap,
                             "No regions — the global prompt covers the canvas")
        for index, region in enumerate(self.regions, 1):
            box = self.region_rect(region)
            color = QColor("#2387cf" if index == 1 else "#c77b19")
            fill = QColor(color); fill.setAlpha(75)
            painter.fillRect(box, fill)
            painter.setPen(QPen(color, 2)); painter.drawRect(box)
            painter.setPen(self.palette().text().color())
            painter.drawText(box.adjusted(5, 5, -5, -5), Qt.AlignLeft | Qt.AlignTop,
                             f"Region {index}")


class GenerationRegionsDialog(QDialog):
    """Apply returns a detached spec; the caller owns any plan change or save."""

    def __init__(self, parent, *, global_prompt, width, height, spec=None):
        super().__init__(parent)
        self.canvas_width = width; self.canvas_height = height
        self.global_prompt = global_prompt
        self.accepted_spec = None
        self._stale_canvas = False
        incoming = spec if spec is not None else empty_region_spec(width, height)
        if isinstance(incoming, dict) and incoming.get("base_canvas") != {"width": width, "height": height}:
            old = incoming.get("base_canvas")
            if not isinstance(old, dict):
                raise ValueError("The saved regional prompt canvas is invalid.")
            checked = validate_region_spec(incoming, width=old.get("width"), height=old.get("height"))
            self._stale_canvas = True
        else:
            checked = validate_region_spec(incoming, width=width, height=height)
        self.setWindowTitle("Rectangular Regional Prompts")
        self.resize(1000, 850)
        layout = QVBoxLayout(self)
        intro = QLabel(f"Canvas: {width} × {height}. Place up to two nonoverlapping rectangles. "
            "The global/region blend is fixed at 0.5. These are prompt areas, not separate subjects, "
            "semantic masks, or style controls.")
        intro.setWordWrap(True); layout.addWidget(intro)
        layout.addWidget(QLabel("Global prompt (kept exactly as written)"))
        self.global_view = QTextEdit(); self.global_view.setReadOnly(True)
        self.global_view.setMaximumHeight(65); self.global_view.setPlainText(global_prompt)
        layout.addWidget(self.global_view)
        self.sketch = RegionSketch(width, height, self); layout.addWidget(self.sketch)
        region_layout = QHBoxLayout(); layout.addLayout(region_layout)
        self.entries = []
        for index in range(2):
            group = QGroupBox(f"Region {index + 1}")
            form = QFormLayout(group)
            prompt = QTextEdit(); prompt.setMaximumHeight(60)
            prompt.setPlaceholderText("Describe what belongs in this rectangle")
            form.addRow("Prompt", prompt)
            fields = {}
            for key, caption in (("x", "Left"), ("y", "Top"),
                                 ("width", "Width"), ("height", "Height"),
                                 ("strength", "Strength")):
                spin = PercentSpinBox(); spin.setRange(0, 100); spin.setDecimals(6)
                spin.setSingleStep(1); spin.setSuffix(" %")
                form.addRow(caption, spin); fields[key] = spin
                spin.valueChanged.connect(self._refresh)
            remove = QPushButton(f"Remove Region {index + 1}")
            form.addRow(remove)
            entry = {"group": group, "prompt": prompt, "fields": fields, "remove": remove,
                     "active": False}
            remove.clicked.connect(lambda _checked=False, i=index: self._remove(i))
            prompt.textChanged.connect(self._refresh)
            region_layout.addWidget(group, 1); self.entries.append(entry)
        actions = QHBoxLayout()
        self.add_button = QPushButton("Add Region"); self.add_button.clicked.connect(self._add)
        self.clear_button = QPushButton("Remove All Regions"); self.clear_button.clicked.connect(self._clear)
        self.rebase_button = QPushButton("Use Rectangles on Current Canvas")
        self.rebase_button.clicked.connect(self._rebase)
        actions.addWidget(self.add_button); actions.addWidget(self.clear_button)
        actions.addWidget(self.rebase_button); actions.addStretch()
        layout.addLayout(actions)
        layout.addWidget(QLabel("Exact prompt preview"))
        self.preview = QTextEdit(); self.preview.setReadOnly(True); self.preview.setMaximumHeight(115)
        layout.addWidget(self.preview)
        self.status = QLabel(); self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText); layout.addWidget(self.status)
        footer = QHBoxLayout()
        self.help_button = ContextHelpButton("Regional Prompts", section_help("generation-regions"), self)
        self.help_button.setShortcut("F1")
        footer.addWidget(self.help_button); footer.addStretch()
        cancel = QPushButton("Cancel"); cancel.clicked.connect(self.reject)
        self.apply_button = QPushButton("Apply Regional Prompts")
        self.apply_button.setProperty("primary", True); self.apply_button.clicked.connect(self._apply)
        footer.addWidget(cancel); footer.addWidget(self.apply_button); layout.addLayout(footer)
        for index, region in enumerate(checked["regions"]):
            self._set_entry(index, region)
        self._refresh()

    def _set_entry(self, index, region):
        entry = self.entries[index]; entry["active"] = True
        entry["prompt"].setPlainText(region["prompt"])
        for key, spin in entry["fields"].items():
            spin.setValue(region[key] * 100)

    def _add(self):
        for index, entry in enumerate(self.entries):
            if not entry["active"]:
                self._set_entry(index, {"prompt": "", "x": .05 if index == 0 else .55,
                    "y": .05, "width": .4, "height": .9, "strength": 1})
                self._refresh(); entry["prompt"].setFocus(); return

    def _remove(self, index):
        if index == 0 and self.entries[1]["active"]:
            second = self.entries[1]
            region = {key: spin.value() / 100 for key, spin in second["fields"].items()}
            region["prompt"] = second["prompt"].toPlainText()
            self._set_entry(0, region)
            self.entries[1]["active"] = False
        else:
            self.entries[index]["active"] = False
        self._refresh()

    def _clear(self):
        for entry in self.entries:
            entry["active"] = False
        self._stale_canvas = False
        self._refresh()

    def _rebase(self):
        self._stale_canvas = False
        self._refresh()

    def _draft(self):
        regions = []
        for entry in self.entries:
            if entry["active"]:
                region = {key: spin.value() / 100 for key, spin in entry["fields"].items()}
                region["prompt"] = entry["prompt"].toPlainText()
                regions.append(region)
        return {"schema_version": 1,
                "base_canvas": {"width": self.canvas_width, "height": self.canvas_height},
                "regions": regions}

    def _refresh(self):
        if not hasattr(self, "apply_button"):
            return
        for entry in self.entries:
            entry["prompt"].setEnabled(entry["active"])
            for spin in entry["fields"].values():
                spin.setEnabled(entry["active"])
            entry["remove"].setEnabled(entry["active"])
        self.add_button.setEnabled(sum(entry["active"] for entry in self.entries) < 2)
        self.clear_button.setEnabled(any(entry["active"] for entry in self.entries))
        self.rebase_button.setVisible(self._stale_canvas)
        draft = self._draft()
        self.sketch.set_regions(draft["regions"])
        try:
            checked = validate_region_spec(draft, width=self.canvas_width, height=self.canvas_height)
            rendered = render_region_prompt(self.global_prompt, checked,
                                            width=self.canvas_width, height=self.canvas_height)
        except ValueError as exc:
            self.preview.clear(); self.status.setText(str(exc))
            self.apply_button.setEnabled(False)
            return
        self.preview.setPlainText(rendered)
        if self._stale_canvas:
            self.status.setText("Canvas size changed. Inspect the rectangle sketch and exact prompt, "
                "then use rectangles on the current canvas or remove all regions.")
            self.apply_button.setEnabled(False)
            return
        self.status.setText(f"Regions: {len(checked['regions'])}. Apply changes this draft; review it before generating.")
        self.apply_button.setEnabled(True)

    def _apply(self):
        checked = validate_region_spec(self._draft(), width=self.canvas_width, height=self.canvas_height)
        render_region_prompt(self.global_prompt, checked,
                             width=self.canvas_width, height=self.canvas_height)
        self.accepted_spec = checked
        self.accept()
