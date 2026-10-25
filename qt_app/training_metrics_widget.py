"""Read-only charts for controller-observed Training progress."""
from __future__ import annotations

import math

from PySide6.QtCore import Qt, QRectF
from PySide6.QtGui import QColor, QPainter, QPainterPath, QPen
from PySide6.QtWidgets import QLabel, QVBoxLayout, QWidget

from core.training_metrics import TrainingMetricsSnapshot


MAX_DISPLAY_POINTS = 2_000


def downsample_extrema(points: tuple[tuple[int, float], ...],
                       limit: int = MAX_DISPLAY_POINTS) -> tuple[tuple[int, float], ...]:
    """Keep time order and bucket extremes, including first and last samples."""
    if type(limit) is not int or limit < 4:
        raise ValueError("training_chart_limit_invalid")
    if len(points) <= limit:
        return points
    interior = points[1:-1]
    buckets = max(1, (limit - 2) // 2)
    selected: list[tuple[int, float]] = [points[0]]
    for index in range(buckets):
        part = interior[index * len(interior) // buckets:
                        (index + 1) * len(interior) // buckets]
        if not part:
            continue
        low = min(range(len(part)), key=lambda item: part[item][1])
        high = max(range(len(part)), key=lambda item: part[item][1])
        for item in sorted({low, high}):
            selected.append(part[item])
    selected.append(points[-1])
    return tuple(selected)


def _series(snapshot: TrainingMetricsSnapshot) -> tuple[tuple[tuple[int, float], ...],
                                                      tuple[tuple[int, float], ...]]:
    loss: list[tuple[int, float]] = []
    pace: list[tuple[int, float]] = []
    previous = None
    for row in snapshot.records:
        if row.optimizer_step > 0 and row.loss is not None:
            loss.append((row.optimizer_step, float(row.loss)))
        if (previous is not None and row.optimizer_step > previous.optimizer_step
                and row.elapsed_seconds is not None and previous.elapsed_seconds is not None
                and row.elapsed_seconds >= previous.elapsed_seconds):
            pace.append((row.optimizer_step, (row.elapsed_seconds - previous.elapsed_seconds)
                         / (row.optimizer_step - previous.optimizer_step)))
        previous = row
    return downsample_extrema(tuple(loss)), downsample_extrema(tuple(pace))


class _MetricsCanvas(QWidget):
    def __init__(self, parent=None):
        super().__init__(parent)
        self.loss: tuple[tuple[int, float], ...] = ()
        self.pace: tuple[tuple[int, float], ...] = ()
        self.maximum = 1
        self.setMinimumHeight(270)
        self.setAccessibleName("Observed Training metrics charts")

    def set_snapshot(self, snapshot: TrainingMetricsSnapshot | None):
        self.loss, self.pace = _series(snapshot) if snapshot is not None else ((), ())
        self.maximum = snapshot.max_train_steps if snapshot is not None else 1
        self.update()

    def paintEvent(self, event):
        painter = QPainter(self)
        painter.setRenderHint(QPainter.Antialiasing, True)
        left, right = 73.0, 16.0
        usable = max(1.0, self.width() - left - right)
        half = max(100.0, self.height() / 2.0)
        self._draw_plot(painter, QRectF(left, 20.0, usable, max(55.0, half - 45.0)),
                        self.loss, "Training loss", "No observed loss yet",
                        self.palette().highlight().color())
        self._draw_plot(painter, QRectF(left, half + 12.0, usable,
                                        max(55.0, self.height() - half - 43.0)),
                        self.pace, "Elapsed seconds / optimizer step", "No elapsed timing yet",
                        QColor("#b7772c") if self.palette().window().color().lightness() > 128
                        else QColor("#e1af64"))

    def _draw_plot(self, painter: QPainter, box: QRectF,
                   points: tuple[tuple[int, float], ...], title: str,
                   empty: str, color: QColor):
        ink = self.palette().text().color()
        subdued = self.palette().mid().color()
        painter.setPen(ink)
        painter.drawText(QRectF(8, box.top() - 17, self.width() - 16, 17),
                         Qt.AlignLeft | Qt.AlignVCenter, title)
        painter.setPen(QPen(subdued, 1))
        painter.drawLine(box.bottomLeft(), box.bottomRight())
        painter.drawLine(box.bottomLeft(), box.topLeft())
        painter.setPen(ink)
        painter.drawText(QRectF(box.left(), box.bottom() + 3, box.width(), 16),
                         Qt.AlignRight | Qt.AlignVCenter, f"step {self.maximum:,}")
        if not points:
            painter.setPen(subdued)
            painter.drawText(box, Qt.AlignCenter, empty)
            return
        values = [value for _, value in points]
        minimum, maximum = min(values), max(values)
        if math.isclose(minimum, maximum):
            padding = max(abs(maximum) * .05, .001)
            minimum -= padding
            maximum += padding
        painter.setPen(subdued)
        painter.drawText(QRectF(1, box.top() - 3, box.left() - 7, 16),
                         Qt.AlignRight | Qt.AlignVCenter, f"{maximum:.3g}")
        painter.drawText(QRectF(1, box.bottom() - 13, box.left() - 7, 16),
                         Qt.AlignRight | Qt.AlignVCenter, f"{minimum:.3g}")
        path = QPainterPath()
        previous_step = None
        positions = []
        for index, (step, value) in enumerate(points):
            x = box.left() + box.width() * step / self.maximum
            y = box.bottom() - box.height() * (value - minimum) / (maximum - minimum)
            if previous_step is None or step != previous_step + 1:
                path.moveTo(x, y)
            else:
                path.lineTo(x, y)
            positions.append((x, y))
            previous_step = step
        painter.setPen(QPen(color, 2))
        painter.drawPath(path)
        painter.setBrush(color)
        for x, y in positions:
            painter.drawEllipse(QRectF(x - 2.5, y - 2.5, 5, 5))


class TrainingMetricsWidget(QWidget):
    """Present sampled progress without claiming validation or result quality."""

    def __init__(self, parent=None):
        super().__init__(parent)
        layout = QVBoxLayout(self)
        layout.setContentsMargins(12, 10, 12, 10)
        layout.setSpacing(5)
        self.heading = QLabel("Training measurements", self)
        self.heading.setObjectName("training_metrics_heading")
        self.heading.setStyleSheet("font-weight: 600;")
        layout.addWidget(self.heading)
        self.explanation = QLabel(
            "Observed training samples only. Gaps are not estimated; loss is not held-out validation.", self)
        self.explanation.setWordWrap(True)
        self.explanation.setObjectName("training_metrics_explanation")
        layout.addWidget(self.explanation)
        self.canvas = _MetricsCanvas(self)
        layout.addWidget(self.canvas)
        self.status = QLabel("No training measurements have been observed yet.", self)
        self.status.setObjectName("training_metrics_status")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.setAccessibleName("Training measurements")

    def set_history(self, snapshot: TrainingMetricsSnapshot | None) -> None:
        if snapshot is None:
            self.canvas.set_snapshot(None)
            self.status.setText("No training measurements have been observed yet.")
            return
        if (type(snapshot) is not TrainingMetricsSnapshot or type(snapshot.max_train_steps) is not int
                or snapshot.max_train_steps < 1 or len(snapshot.records) > 10_000):
            self.canvas.set_snapshot(None)
            self.status.setText("Training measurements are unavailable; review the retained run evidence.")
            return
        self.canvas.set_snapshot(snapshot)
        if not snapshot.records:
            self.status.setText("No training measurements have been observed yet.")
            return
        last = snapshot.records[-1]
        gap = " Some optimizer steps were not observed." if snapshot.observed_gaps else ""
        ending = (" Final progress was observed; result verification is separate."
                  if snapshot.final_progress_observed else " History is partial while the run is active or recovering.")
        self.status.setText(
            f"{len(snapshot.records):,} observed records · latest step {last.optimizer_step:,} "
            f"of {snapshot.max_train_steps:,}.{gap}{ending}")
