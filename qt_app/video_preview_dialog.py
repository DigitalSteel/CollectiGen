"""One bounded, in-app local video player over verified temporal evidence."""
from __future__ import annotations

from fractions import Fraction
from pathlib import Path
from queue import Empty, Full, Queue
from threading import Event, Lock, Thread
from time import monotonic
from types import SimpleNamespace
import weakref

from PySide6.QtCore import Qt, QSignalBlocker, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton, QSlider, QVBoxLayout

from core.video_decoder import VideoPreviewFrame, iter_video_preview_frames
from core.video_generation import VideoSourceAsset
from .gallery import ProtectedImageView
from .modal_feedback import FeedbackEnabledDialog


class _VideoProtectedView(ProtectedImageView):
    """Keep an in-memory still unknown until the caller supplies its identity."""

    def _current_state(self):
        if self.path is None:
            blur = bool(getattr(self.settings, "gallery_blur_unknown", True))
            return {"rating": "unknown", "basis": "No dependable rating yet",
                "checking": False, "requires_blur": blur, "blurred": blur}
        return super()._current_state()


class VideoPreviewDialog(FeedbackEnabledDialog):
    """Display verified frames without decoding on Qt's UI thread.

    Only one frame can be pending on the UI side and the worker queue holds at
    most two more. Seek reopens the exact clip and decodes to an observed PTS;
    no frame index is treated as a timestamp.
    """

    background_work_finished = Signal()
    selected_frame_changed = Signal(int)
    _decoder_lock = Lock()
    _active_ref = None

    def __init__(self, path: str | Path, asset: VideoSourceAsset, *,
                 allowed_root: str | Path, source_image: QImage | None = None,
                 source_path: str | Path | None = None, settings=None,
                 content_policy=None, frame_source=iter_video_preview_frames,
                 runtime_binding=None, decoder_lifetime=None, parent=None):
        super().__init__(parent)
        if not isinstance(asset, VideoSourceAsset) or not asset.timing.frame_pts:
            raise ValueError("Video playback requires a fully decoded temporal asset.")
        self.path = Path(path)
        self.allowed_root = Path(allowed_root)
        self.asset = asset
        self.frame_source = frame_source
        self.runtime_binding = runtime_binding
        self.decoder_lifetime = decoder_lifetime
        self._queue: Queue = Queue(maxsize=2)
        self._stop = Event()
        self._thread: Thread | None = None
        self._pending_seek: int | None = None
        self._pending_frame: VideoPreviewFrame | None = None
        self._shown_pts: int | None = None
        self._playing = False
        self._ended = False
        self._closing = False
        self._play_clock = 0.0
        self._play_base_pts = 0
        self._shutdown_notified = False
        self._finished_close = False
        self._video_frame_ready = False
        protection_settings = settings if settings is not None else SimpleNamespace(gallery_blur_unknown=True)
        self.setWindowTitle("Video Result Preview")
        self.resize(960, 680)
        layout = QVBoxLayout(self)
        summary = QLabel(
            f"Unreviewed video • {asset.timing.frame_count:,} decoded frames • "
            f"{float(asset.timing.duration or Fraction(0)):.2f} s observed"
        )
        summary.setWordWrap(True)
        summary.setTextFormat(Qt.PlainText)
        layout.addWidget(summary)
        pictures = QHBoxLayout()
        self.video = _VideoProtectedView(self, policy=content_policy, settings=protection_settings)
        self.video.clear("Loading the verified clip…")
        self.video.setMinimumSize(480, 270)
        pictures.addWidget(self.video, 1)
        self.source = _VideoProtectedView(self, policy=content_policy, settings=protection_settings)
        if source_image is not None and not source_image.isNull():
            self.source.set_image(QPixmap.fromImage(source_image), source_path)
            self.source.setMaximumWidth(320)
            self.source.setToolTip("Original still-image source; generated video frames are separate evidence.")
            pictures.addWidget(self.source)
        else:
            # An unlaid-out child is still shown with its parent. Keep the
            # optional source toolbar out of a video-only preview.
            self.source.hide()
        layout.addLayout(pictures, 1)
        controls = QHBoxLayout()
        self.play_button = QPushButton("Play")
        self.play_button.clicked.connect(self.toggle_playback)
        controls.addWidget(self.play_button)
        self.timeline = QSlider(Qt.Horizontal)
        self.timeline.setAccessibleName("Observed video timeline")
        self.timeline.setRange(0, asset.timing.frame_count - 1)
        self.timeline.sliderReleased.connect(lambda: self.seek_to_index(self.timeline.value()))
        controls.addWidget(self.timeline, 1)
        self.position = QLabel("0.00 s")
        controls.addWidget(self.position)
        layout.addLayout(controls)
        self.status = QLabel("Loading the verified clip…")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._timer = QTimer(self)
        self._timer.setInterval(15)
        self._timer.timeout.connect(self._tick)
        self._timer.start()
        QTimer.singleShot(0, lambda: self.seek_to_index(0))

    def _stop_worker(self):
        self._stop.set()
        self._queue = Queue(maxsize=2)
        self._pending_frame = None

    def stop_preview(self):
        """Yield decoder ownership to another video preview."""
        self._playing = False
        self.play_button.setText("Play")
        self._pending_seek = None
        self._stop_worker()
        self.prepare_for_privacy_capture()
        self.status.setText("Playback paused while another video opens.")

    def _start_worker(self, index: int):
        try:
            use = self.decoder_lifetime.acquire() if self.decoder_lifetime is not None else None
        except ValueError as exc:
            self._playback_error(str(exc))
            return False
        previous = self._active_ref() if self._active_ref is not None else None
        if previous is not None and previous is not self:
            try:
                previous.stop_preview()
            except RuntimeError:
                pass
        type(self)._active_ref = weakref.ref(self)
        self._queue = Queue(maxsize=2)
        self._stop = Event()
        target_pts = self.asset.timing.frame_pts[index]
        queue, stopped = self._queue, self._stop

        def deliver(kind, value):
            while not stopped.is_set():
                try:
                    queue.put((kind, value), timeout=.05)
                    return
                except Full:
                    continue

        def decode_frames():
            while not stopped.is_set():
                if type(self)._decoder_lock.acquire(timeout=.05):
                    break
            else:
                return
            frames = None
            try:
                options = {}
                if self.runtime_binding is not None:
                    options["runtime_binding"] = self.runtime_binding
                if self.decoder_lifetime is not None:
                    options["decoder_lifetime"] = self.decoder_lifetime
                frames = self.frame_source(self.path, self.asset,
                    allowed_root=self.allowed_root, start_pts=target_pts,
                    should_cancel=stopped.is_set, **options)
                for frame in frames:
                    if stopped.is_set():
                        break
                    deliver("frame", frame)
                if not stopped.is_set():
                    deliver("end", None)
            except InterruptedError:
                pass
            except Exception as exc:
                if not stopped.is_set():
                    deliver("error", str(exc)[:2000])
            finally:
                try:
                    close = getattr(frames, "close", None)
                    if callable(close):
                        close()
                finally:
                    type(self)._decoder_lock.release()

        def decode():
            try:
                decode_frames()
            finally:
                if use is not None:
                    use.finish()

        self._thread = Thread(target=decode, name="CollectiGen video preview", daemon=True)
        if use is not None:
            use.owners(threads=(self._thread,))
        try:
            self._thread.start()
        except BaseException:
            if use is not None:
                use.finish()
            raise
        self._ended = False
        self._pending_frame = None
        self._shown_pts = None
        self.status.setText("Reading verified video frames…")

    def seek_to_index(self, index: int):
        if self._closing:
            return
        if not 0 <= index < self.asset.timing.frame_count:
            raise ValueError("Video seek is outside the observed timeline.")
        self._pending_seek = index
        self._stop_worker()
        self._pending_frame = None
        self._shown_pts = None
        self._ended = False
        self._video_frame_ready = False
        self.video.clear("Checking the exact video bytes…")
        self.source.prepare_for_privacy_capture()
        self.status.setText("Seeking to the selected observed frame…")
        return self._tick()

    def toggle_playback(self):
        if self._closing:
            return
        if self._ended:
            if self.seek_to_index(0) is False:
                return
        elif self._playing:
            self._playing = False
            self._stop_worker()
            self.prepare_for_privacy_capture()
            self.play_button.setText("Play")
            self.status.setText("Paused on a verified frame.")
            return
        elif self._shown_pts is not None:
            # Reopen and rehash on resume. A clip changed during a long pause
            # cannot display cached frames from its former decoder process.
            index = self.asset.timing.frame_pts.index(self._shown_pts) + 1
            if self.seek_to_index(index if index < self.asset.timing.frame_count else 0) is False:
                return
        elif self.seek_to_index(0) is False:
            # Retry from the visible Play control after a component hold/error.
            return
        self._playing = True
        self.play_button.setText("Pause" if self._playing else "Play")
        if self._playing:
            self._play_clock = monotonic()
            self._play_base_pts = self._shown_pts if self._shown_pts is not None else self.asset.timing.start_pts

    def _show_frame(self, frame: VideoPreviewFrame):
        if frame.pts not in self.asset.timing.frame_pts:
            raise ValueError("Preview frame is absent from verified timing.")
        if len(frame.rgb) != frame.width * frame.height * 3:
            raise ValueError("Preview frame has invalid RGB bytes.")
        image = QImage(frame.rgb, frame.width, frame.height,
            frame.width * 3, QImage.Format_RGB888).copy()
        if self._shown_pts is None and self._playing:
            self._play_clock = monotonic()
            self._play_base_pts = frame.pts
        pixmap = QPixmap.fromImage(image)
        if not self._video_frame_ready:
            self.video.set_image(pixmap, self.path)
            self._video_frame_ready = True
        else:
            self.video.viewer.set_image(pixmap)
            self.video.refresh_protection(preserve_temporary_reveal=True)
        self._shown_pts = frame.pts
        position = self.asset.timing.frame_pts.index(frame.pts)
        with QSignalBlocker(self.timeline):
            self.timeline.setValue(position)
        self.selected_frame_changed.emit(position)
        seconds = self.asset.timing.seconds_at(frame.pts)
        self.position.setText(f"{float(seconds):.2f} s • frame {position + 1}/{self.asset.timing.frame_count}")
        self.status.setText("Playing verified local video." if self._playing else "Paused on a verified frame.")
        if not self._playing:
            self._stop_worker()

    def _playback_error(self, message: str):
        self._stop_worker()
        self._playing = False
        self._ended = False
        self._shown_pts = None
        self._video_frame_ready = False
        self.play_button.setText("Play")
        self.video.clear("Video cannot be verified.")
        self.source.prepare_for_privacy_capture()
        self.status.setText("Video playback unavailable: " + message)

    def prepare_for_privacy_capture(self):
        self.video.prepare_for_privacy_capture()
        self.source.prepare_for_privacy_capture()

    def _finish_close(self):
        if self._finished_close or self.has_active_background_work():
            return
        self._finished_close = True
        self._timer.stop()
        if type(self)._active_ref is not None and type(self)._active_ref() is self:
            type(self)._active_ref = None
        if not self._shutdown_notified:
            self._shutdown_notified = True
            self.background_work_finished.emit()
        # QDialog.reject emits rejected and finished exactly once. QWidget.close
        # alone can hide this dialog without finishing its owning workflow.
        QDialog.reject(self)

    def _tick(self):
        active = self._thread is not None and self._thread.is_alive()
        if self._closing:
            if not active:
                self._finish_close()
            return
        if self._pending_seek is not None:
            if active:
                return
            index, self._pending_seek = self._pending_seek, None
            return self._start_worker(index)
        if self._pending_frame is None:
            try:
                kind, value = self._queue.get_nowait()
            except Empty:
                return
            if kind == "error":
                self._playback_error(value)
                return
            if kind == "end":
                self._ended = True
                self._playing = False
                self.play_button.setText("Play Again")
                self.status.setText("Verified video playback reached the final frame.")
                return
            self._pending_frame = value
        frame = self._pending_frame
        if frame is None:
            return
        if self._shown_pts is not None and not self._playing:
            return
        if self._shown_pts is not None:
            due = float(Fraction(frame.pts - self._play_base_pts)
                        * self.asset.timing.time_base.fraction)
            if monotonic() - self._play_clock < due:
                return
        self._pending_frame = None
        try:
            self._show_frame(frame)
        except (ValueError, RuntimeError) as exc:
            self._playback_error(str(exc))

    def has_active_background_work(self):
        return self._thread is not None and self._thread.is_alive()

    def request_background_shutdown(self):
        self._closing = True
        self._stop_worker()
        self.prepare_for_privacy_capture()
        self._timer.start()
        return self.has_active_background_work()

    def reject(self):
        self.close()

    def closeEvent(self, event):
        if self._finished_close:
            event.accept()
            return
        self._closing = True
        self._stop_worker()
        self.prepare_for_privacy_capture()
        if self.has_active_background_work():
            self.status.setText("Stopping the local video decoder safely…")
            self._timer.start()
            event.ignore()
            return
        event.ignore()
        QTimer.singleShot(0, self._finish_close)
