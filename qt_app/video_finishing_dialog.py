"""Review a verified clip's exact frames and publish explicit derivatives."""
from __future__ import annotations

from pathlib import Path
from uuid import uuid4

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout,
    QLabel, QPushButton, QSlider, QSpinBox, QVBoxLayout)

from core.video_finishing import (
    ExtractedFrame, TrimmedVideo, discover_finished_video, extract_verified_frame,
    prepare_frame_ingest, trim_verified_video, verified_trimmed_output,
    verify_extracted_frame, verify_prepared_frame_ingest,
)
from core.video_generation import VideoGenerationRequest, VideoSourceAsset
from .workers import CoordinatedCallTask


class VideoFinishingDialog(QDialog):
    """Use one exact generated result; its original and job receipt stay intact."""

    background_work_finished = Signal()
    ingest_requested = Signal(object)
    frame_ingest_requested = Signal(object)
    remix_requested = Signal(object)
    animate_requested = Signal(object)

    def __init__(self, jobs, path: str | Path, asset: VideoSourceAsset,
                 request: VideoGenerationRequest, *, allowed_root: str | Path,
                 storage_root: str | Path, verified_source, settings=None, parent=None):
        super().__init__(parent)
        if (not isinstance(asset, VideoSourceAsset) or not isinstance(request, VideoGenerationRequest)
                or not asset.timing.frame_pts or not callable(verified_source)):
            raise ValueError("Video finishing needs an exact verified clip and source checker.")
        self.jobs, self.path, self.asset, self.request = jobs, Path(path), asset, request
        self.allowed_root, self.storage_root = Path(allowed_root), Path(storage_root)
        self.verified_source, self.settings = verified_source, settings
        self._task = None
        self._preview = None
        self._closing = False
        self._finished_close = False
        self._frame_result: ExtractedFrame | None = None
        self._trim_result: TrimmedVideo | None = None
        self._saved_frames: list[ExtractedFrame] = []
        self._saved_trims: list[TrimmedVideo] = []
        self._selecting_saved = False
        self.setWindowTitle("Finish Verified Video")
        self.resize(790, 610)
        layout = QVBoxLayout(self)
        heading = QLabel("Finish a verified video")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        note = QLabel("The original clip and generation receipt stay intact. Choose observed frames for a still image or a shorter silent clip. Wan 5B does not support clip continuation or end-frame conditioning here.")
        note.setWordWrap(True); layout.addWidget(note)
        self.preview_button = QPushButton("Preview and Choose Frame…")
        self.preview_button.clicked.connect(self.preview_clip)
        layout.addWidget(self.preview_button)
        form = QFormLayout()
        self.frame_index = QSpinBox(); self.frame_index.setRange(1, asset.timing.frame_count)
        self.frame_index.setAccessibleName("Observed frame to extract")
        form.addRow("Extract frame", self.frame_index)
        self.first_frame = QSpinBox(); self.first_frame.setRange(1, asset.timing.frame_count)
        self.first_frame.setAccessibleName("First included trim frame")
        self.last_frame = QSpinBox(); self.last_frame.setRange(1, asset.timing.frame_count)
        self.last_frame.setValue(asset.timing.frame_count)
        self.last_frame.setAccessibleName("Last included trim frame")
        form.addRow("Trim from frame", self.first_frame)
        form.addRow("Through frame (inclusive)", self.last_frame)
        self.frame_slider = QSlider(Qt.Horizontal); self.frame_slider.setRange(1, asset.timing.frame_count)
        self.frame_slider.setAccessibleName("Observed frame position")
        self.first_handle = QSlider(Qt.Horizontal); self.first_handle.setRange(1, asset.timing.frame_count)
        self.first_handle.setAccessibleName("Trim start frame handle")
        self.last_handle = QSlider(Qt.Horizontal); self.last_handle.setRange(1, asset.timing.frame_count)
        self.last_handle.setValue(asset.timing.frame_count)
        self.last_handle.setAccessibleName("Trim end frame handle")
        form.addRow("Frame position", self.frame_slider)
        form.addRow("Trim start handle", self.first_handle)
        form.addRow("Trim end handle", self.last_handle)
        layout.addLayout(form)
        self.frame_time = QLabel()
        layout.addWidget(self.frame_time)
        self.interval = QLabel()
        self.interval.setWordWrap(True); layout.addWidget(self.interval)
        self.first_frame.valueChanged.connect(self._update_interval)
        self.last_frame.valueChanged.connect(self._update_interval)
        self._update_interval()
        actions = QHBoxLayout()
        self.extract_button = QPushButton("Extract PNG Frame")
        self.trim_button = QPushButton("Create Trimmed MP4")
        self.cancel_button = QPushButton("Cancel Current Work")
        self.extract_button.clicked.connect(self.extract_frame)
        self.trim_button.clicked.connect(self.trim_clip)
        self.cancel_button.clicked.connect(self.cancel_work)
        for button in (self.extract_button, self.trim_button, self.cancel_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        next_actions = QHBoxLayout()
        self.frame_ingest_button = QPushButton("Keep Extracted Frame in Ingest…")
        self.remix_button = QPushButton("Remix Extracted Frame…")
        self.animate_button = QPushButton("Animate Extracted Frame…")
        self.ingest_button = QPushButton("Review Trimmed Clip in Ingest…")
        self.frame_ingest_button.clicked.connect(self.send_frame_to_ingest)
        self.remix_button.clicked.connect(lambda: self._emit_frame(self.remix_requested))
        self.animate_button.clicked.connect(lambda: self._emit_frame(self.animate_requested))
        self.ingest_button.clicked.connect(self.send_trim_to_ingest)
        for button in (self.frame_ingest_button, self.remix_button, self.animate_button, self.ingest_button):
            next_actions.addWidget(button)
        layout.addLayout(next_actions)
        saved = QHBoxLayout()
        self.saved_frame = QComboBox(); self.saved_frame.setAccessibleName("Saved extracted frame")
        self.saved_trim = QComboBox(); self.saved_trim.setAccessibleName("Saved trimmed clip")
        self.find_saved_button = QPushButton("Find Saved Finishes")
        self.saved_frame.currentIndexChanged.connect(self._saved_frame_changed)
        self.saved_trim.currentIndexChanged.connect(self._saved_trim_changed)
        self.find_saved_button.clicked.connect(self.load_saved_finishes)
        saved.addWidget(self.saved_frame, 1); saved.addWidget(self.saved_trim, 1)
        saved.addWidget(self.find_saved_button)
        layout.addLayout(saved)
        layout.addStretch(1)
        self.status = QLabel("Choose an observed frame or trim interval, then create a separate result.")
        self.status.setTextFormat(Qt.PlainText); self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.close_button = QPushButton("Close Finishing")
        self.close_button.clicked.connect(self.close)
        layout.addWidget(self.close_button)
        self._timer = QTimer(self); self._timer.setInterval(50)
        self._timer.timeout.connect(self._advance_close)
        self.frame_index.valueChanged.connect(self._invalidate_frame_selection)
        self.frame_index.valueChanged.connect(self.frame_slider.setValue)
        self.frame_slider.valueChanged.connect(self.frame_index.setValue)
        self.frame_index.valueChanged.connect(self._update_frame_time)
        self.first_frame.valueChanged.connect(self._invalidate_trim_selection)
        self.last_frame.valueChanged.connect(self._invalidate_trim_selection)
        self.first_frame.valueChanged.connect(self.first_handle.setValue)
        self.first_handle.valueChanged.connect(self.first_frame.setValue)
        self.last_frame.valueChanged.connect(self.last_handle.setValue)
        self.last_handle.valueChanged.connect(self.last_frame.setValue)
        self._update_frame_time()
        self._update_actions()

    def _invalidate_frame_selection(self, *_):
        self._frame_result = None
        if hasattr(self, "saved_frame") and not self._selecting_saved:
            self.saved_frame.blockSignals(True)
            self.saved_frame.setCurrentIndex(0)
            self.saved_frame.blockSignals(False)
        self.status.setText("Frame selection changed. Extract or choose this observed frame before using it.")
        self._update_actions()

    def _invalidate_trim_selection(self, *_):
        self._trim_result = None
        if hasattr(self, "saved_trim") and not self._selecting_saved:
            self.saved_trim.blockSignals(True)
            self.saved_trim.setCurrentIndex(0)
            self.saved_trim.blockSignals(False)
        self.status.setText("Trim interval changed. Create and verify a new MP4 before Ingest review.")
        self._update_actions()

    def _update_frame_time(self, *_):
        pts = self.asset.timing.frame_pts[self.frame_index.value() - 1]
        seconds = self.asset.timing.seconds_at(pts)
        self.frame_time.setText(f"Selected observed frame {self.frame_index.value()} · {float(seconds):.3f} s")

    def _refresh_saved(self):
        self.saved_frame.blockSignals(True); self.saved_trim.blockSignals(True)
        self.saved_frame.clear(); self.saved_trim.clear()
        self.saved_frame.addItem("Choose saved frame…", None)
        self.saved_trim.addItem("Choose saved trim…", None)
        for frame in self._saved_frames:
            self.saved_frame.addItem(f"Frame {frame.reference.ordinal + 1} · {frame.output_path.name[-16:]}",
                                     str(frame.receipt_path))
        for trim in self._saved_trims:
            start = trim.lineage.source_start_pts
            self.saved_trim.addItem(f"Trim from PTS {start} · {trim.output_path.name[-16:]}",
                                    str(trim.receipt_path))
        if self._frame_result is not None:
            index = self.saved_frame.findData(str(self._frame_result.receipt_path))
            if index >= 0:
                self.saved_frame.setCurrentIndex(index)
        if self._trim_result is not None:
            index = self.saved_trim.findData(str(self._trim_result.receipt_path))
            if index >= 0:
                self.saved_trim.setCurrentIndex(index)
        self.saved_frame.blockSignals(False); self.saved_trim.blockSignals(False)
        self._saved_frame_changed()
        self._saved_trim_changed()
        self._update_actions()

    def _saved_frame_changed(self, *_):
        receipt = self.saved_frame.currentData()
        result = next((frame for frame in self._saved_frames
                       if str(frame.receipt_path) == receipt), None)
        if result is not None:
            self._selecting_saved = True
            try:
                self.frame_index.setValue(result.reference.ordinal + 1)
            finally:
                self._selecting_saved = False
            self._frame_result = result
        else:
            self._frame_result = None
        self._update_actions()

    def _saved_trim_changed(self, *_):
        receipt = self.saved_trim.currentData()
        result = next((trim for trim in self._saved_trims
                       if str(trim.receipt_path) == receipt), None)
        if result is not None:
            points = self.asset.timing.frame_pts
            first = points.index(result.lineage.source_start_pts) + 1
            stop = points.index(result.lineage.source_end_pts) if result.lineage.source_end_pts in points else len(points)
            self._selecting_saved = True
            try:
                self.first_frame.setValue(first); self.last_frame.setValue(stop)
            finally:
                self._selecting_saved = False
            self._trim_result = result
        else:
            self._trim_result = None
        self._update_actions()

    def load_saved_finishes(self):
        def work(job):
            return discover_finished_video(self.storage_root / "finishing", self.asset,
                self.request, self._source_descriptor(), should_cancel=lambda: job.cancel_requested)
        def complete(result):
            self._saved_frames = list(result["frames"])
            self._saved_trims = list(result["trims"])
            self._frame_result = self._saved_frames[0] if self._saved_frames else None
            self._trim_result = self._saved_trims[0] if self._saved_trims else None
            self._refresh_saved()
            self.status.setText(f"Found {len(self._saved_frames)} verified frame(s) and {len(self._saved_trims)} verified trim(s); {result['unavailable']} unavailable."
                + (" Search limit reached." if result["truncated"] else ""))
        self._run("Find saved video finishes", work, complete, resource="io")

    def _source_descriptor(self):
        outputs = self.verified_source()
        matches = [value for value in outputs if Path(value.get("path", "")) == self.path
                   and value.get("sha256") == self.asset.sha256]
        if len(matches) != 1:
            raise ValueError("The generated source is no longer one exact verified output.")
        return matches[0]

    def _finishing_root(self):
        from core.training_source_io import _assert_unredirected
        _assert_unredirected(self.storage_root)
        if not self.storage_root.is_dir():
            raise ValueError("The retained video workspace is unavailable.")
        root = self.storage_root / "finishing"
        if root.exists():
            _assert_unredirected(root)
        else:
            root.mkdir(mode=0o700)
        return root

    def _run(self, label, worker, completed, *, resource="cpu"):
        if self._task is not None or self._closing:
            return
        task = CoordinatedCallTask(self.jobs, label, worker, resource=resource)
        self._task = task
        task.signals.complete.connect(lambda result: self._complete(task, completed, result))
        task.signals.failed.connect(lambda error: self._failed(task, error))
        task.signals.cancelled.connect(lambda: self._failed(task, "Cancelled"))
        self.status.setText(label + "…")
        self._update_actions()
        try:
            task.start()
        except Exception as exc:
            self._failed(task, str(exc))

    def _complete(self, task, completed, result):
        if self._task is not task:
            return
        self._task = None
        if not self._closing:
            try:
                completed(result)
            except Exception as exc:
                self.status.setText(str(exc))
        self._update_actions(); self._advance_close()

    def _failed(self, task, error):
        if self._task is task:
            self._task = None
            self.status.setText(str(error))
            self._update_actions(); self._advance_close()

    def cancel_work(self):
        if self._task is not None and self._task.job_id:
            try:
                self.jobs.cancel(self._task.job_id)
                self.status.setText("Cancellation requested. Waiting for the exact encoder or decoder to stop safely…")
            except Exception as exc:
                self.status.setText("Cancellation could not be confirmed: " + str(exc))

    def _update_interval(self, *_):
        first, last = self.first_frame.value() - 1, self.last_frame.value() - 1
        if first <= last:
            timing = self.asset.timing
            start = timing.seconds_at(timing.frame_pts[first])
            end_pts = timing.frame_pts[last + 1] if last + 1 < timing.frame_count else timing.end_pts
            end = timing.seconds_at(end_pts) if end_pts is not None else None
            self.interval.setText(f"Observed frames {first + 1}–{last + 1} · "
                                  f"{float(start):.3f}–{float(end):.3f} s" if end is not None
                                  else "Selected interval lacks a verified end timestamp.")
        else:
            self.interval.setText("The first frame must precede the last frame.")
        if hasattr(self, "trim_button"):
            self._update_actions()

    def _update_actions(self):
        ready = self._task is None and not self._closing
        for control in (self.frame_index, self.first_frame, self.last_frame,
                        self.frame_slider, self.first_handle, self.last_handle):
            control.setEnabled(ready)
        self.preview_button.setEnabled(ready)
        self.extract_button.setEnabled(ready)
        self.trim_button.setEnabled(ready and self.first_frame.value() <= self.last_frame.value()
            and self.last_frame.value() - self.first_frame.value() + 1 < self.asset.timing.frame_count)
        self.cancel_button.setEnabled(self._task is not None)
        self.remix_button.setEnabled(ready and self._frame_result is not None)
        self.animate_button.setEnabled(ready and self._frame_result is not None)
        self.frame_ingest_button.setEnabled(ready and self._frame_result is not None)
        self.ingest_button.setEnabled(ready and self._trim_result is not None)
        self.saved_frame.setEnabled(ready and bool(self._saved_frames))
        self.saved_trim.setEnabled(ready and bool(self._saved_trims))
        self.find_saved_button.setEnabled(ready)
        self.close_button.setEnabled(ready)

    def preview_clip(self):
        from .video_preview_dialog import VideoPreviewDialog
        player = VideoPreviewDialog(self.path, self.asset, allowed_root=self.allowed_root,
                                    settings=self.settings, parent=self)
        self._preview = player
        player.selected_frame_changed.connect(lambda index: self.frame_index.setValue(index + 1))
        player.background_work_finished.connect(self._advance_close)
        player.exec()
        if self._preview is player:
            self._preview = None
        self._advance_close()

    def extract_frame(self):
        index = self.frame_index.value() - 1
        def work(job):
            self._source_descriptor()
            root = self._finishing_root()
            destination = root / f"frame-{self.asset.sha256[:12]}-{index + 1}-{uuid4().hex[:12]}.png"
            return extract_verified_frame(self.path, self.asset, self.request, index,
                destination, allowed_root=root, should_cancel=lambda: job.cancel_requested)
        def complete(result):
            self._frame_result = result
            self._saved_frames.append(result)
            self._refresh_saved()
            self.status.setText("Exact frame extracted as a separate PNG. Choose Remix or Animate to start a new draft; the original clip is unchanged.")
        self._run("Extract selected observed frame", work, complete)

    def trim_clip(self):
        first, stop = self.first_frame.value() - 1, self.last_frame.value()
        def work(job):
            self._source_descriptor()
            root = self._finishing_root()
            destination = root / f"trim-{self.asset.sha256[:12]}-{first + 1}-{stop}-{uuid4().hex[:12]}.mp4"
            return trim_verified_video(self.path, self.asset, self.request, first, stop,
                destination, allowed_root=root, should_cancel=lambda: job.cancel_requested)
        def complete(result):
            self._trim_result = result
            self._saved_trims.append(result)
            self._refresh_saved()
            self.status.setText("Trimmed MP4 fully verified and retained beside the original. Review it in Ingest before library admission.")
        self._run("Create exact trimmed video", work, complete)

    def _emit_frame(self, signal):
        result = self._frame_result
        if result is None:
            return
        source_path, asset, request = self.path, self.asset, self.request
        verified_source = self.verified_source
        def provenance_provider():
            matches = [value for value in verified_source()
                       if Path(value.get("path", "")) == source_path
                       and value.get("sha256") == asset.sha256]
            if len(matches) != 1:
                raise ValueError("The generated source is no longer one exact verified output.")
            return verify_extracted_frame(result, matches[0], request)
        def work(_job):
            provenance_provider()
            return {"path": result.output_path,
                    "frame_reference_provider": provenance_provider}
        self._run("Recheck extracted frame lineage", work, signal.emit)

    def send_frame_to_ingest(self):
        result = self._frame_result
        if result is None:
            return
        def descriptor():
            return self._source_descriptor()
        def verify_handoff(prepared):
            return verify_prepared_frame_ingest(prepared, result, descriptor(), self.request)
        def work(_job):
            return prepare_frame_ingest(result, descriptor(), self.request)
        def complete(prepared):
            self.frame_ingest_requested.emit({
                "path": prepared.output_path,
                "expected_image_sha256": prepared.image_sha256,
                "sidecar_sha256": prepared.sidecar_sha256,
                "frame_reference_provider": lambda: verify_handoff(prepared).reference,
                "verify_handoff": lambda: verify_handoff(prepared),
            })
            self.status.setText("Extracted frame prepared with unreviewed lineage. Review it in Ingest before keeping it.")
        self._run("Prepare extracted frame for Ingest", work, complete, resource="io")

    def send_trim_to_ingest(self):
        result = self._trim_result
        if result is None:
            return
        def verified_outputs(check_path):
            if Path(check_path).absolute() != result.receipt_path.absolute():
                raise ValueError("The selected finishing receipt differs from exact reviewed work.")
            return [verified_trimmed_output(result, self._source_descriptor(), self.request)]
        def work(_job):
            return verified_outputs(result.receipt_path)
        def complete(_outputs):
            self.ingest_requested.emit({"receipt_path": str(result.receipt_path),
                "source_path": str(result.output_path), "verified_outputs": verified_outputs})
            self.status.setText("Trimmed video opened in Ingest for explicit review and copy.")
        self._run("Recheck trimmed video for Ingest", work, complete)

    def has_active_background_work(self):
        player = self._preview
        return bool(self._task is not None or
            (player is not None and not getattr(player, "_finished_close", False)))

    def request_background_shutdown(self):
        if not self._closing:
            self._closing = True
            if self._task is not None:
                self.cancel_work()
            if self._preview is not None:
                self._preview.request_background_shutdown()
        self._timer.start(); self._update_actions(); self._advance_close()
        return self.has_active_background_work()

    def _advance_close(self):
        if self._closing and not self.has_active_background_work() and not self._finished_close:
            self._finished_close = True
            self._timer.stop()
            self.background_work_finished.emit()
            QDialog.reject(self)

    def reject(self):
        self.close()

    def closeEvent(self, event):
        if self._finished_close:
            event.accept(); return
        event.ignore()
        self.request_background_shutdown()
