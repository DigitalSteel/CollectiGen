"""Review and finish selected verified generation winners as separate versions."""
from __future__ import annotations

from pathlib import Path
from math import gcd, isqrt
from types import SimpleNamespace

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QComboBox, QDialog, QFormLayout, QHBoxLayout,
    QLabel, QPushButton, QSpinBox, QVBoxLayout)

from core.generation_finishing import (execute_generation_finish,
    discover_generation_finishes, inspect_generation_finish_source, preview_generation_finish,
    verified_generation_refinement, verify_generation_finish)
from core.training_source_io import read_stable_training_source
from .gallery import ProtectedImageView
from .workers import CoordinatedCallTask


class GenerationFinishingDialog(QDialog):
    """Retained chooser; its original generation workspace remains the owner."""

    background_work_finished = Signal()
    ingest_requested = Signal(object)
    refine_requested = Signal(object)

    def __init__(self, jobs, storage_root, selected_paths, verified_source, parent=None,
                 *, initial_goals=None, settings=None):
        super().__init__(parent)
        paths = tuple(Path(value).absolute() for value in selected_paths)
        if not paths or len(paths) > 32 or len(set(paths)) != len(paths) or not callable(verified_source):
            raise ValueError("Choose one to thirty-two distinct verified winners.")
        self.jobs = jobs
        self.storage_root = Path(storage_root).absolute()
        self.verified_source = verified_source
        self.paths = paths
        self._initial_goals = {Path(key).absolute(): value for key, value in
                               (initial_goals or {}).items() if isinstance(value, dict)}
        self._task = None
        self._closing = False
        self._finished_close = False
        self._source_details = {}
        self._size_by_source = {}
        self._editing_sizes = False
        self._preview = None
        self._finished = {}
        self._versions = {}
        self._selected_version_fingerprint = {}
        self.settings = settings if settings is not None else SimpleNamespace(gallery_blur_unknown=True)
        self.setWindowTitle("Finish Selected Image")
        self.resize(820, 800)
        layout = QVBoxLayout(self)
        heading = QLabel("Finish a selected generated image")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        note = QLabel("Faithful Resize makes a separate version at your chosen size while keeping the original image and its history. Enlarging pixels does not add detail. Generative Refinement opens a new Remix draft for review.")
        note.setWordWrap(True); layout.addWidget(note)
        self.winner = QComboBox()
        for path in paths:
            self.winner.addItem(path.parent.name + " · " + path.name, str(path))
        self.winner.setAccessibleName("Selected generated winner")
        self.winner.currentIndexChanged.connect(self._winner_changed)
        layout.addWidget(self.winner)
        self.source_info = QLabel("Inspect the selected winner to set an exact final size.")
        self.source_info.setWordWrap(True); layout.addWidget(self.source_info)
        form = QFormLayout()
        self.final_width = QSpinBox(); self.final_height = QSpinBox()
        for spin in (self.final_width, self.final_height):
            spin.setRange(64, 8192); spin.valueChanged.connect(self._size_changed)
        self.final_width.setAccessibleName("Final width in pixels")
        self.final_height.setAccessibleName("Final height in pixels")
        form.addRow("Final width", self.final_width)
        form.addRow("Final height", self.final_height)
        self.aspect_note = QLabel("Source aspect ratio is preserved for faithful resize.")
        self.aspect_note.setWordWrap(True)
        form.addRow("Size control", self.aspect_note)
        self.scale_preset = QComboBox()
        for label, factor in (("Custom", None), ("Native 1× (reference)", 1),
                              ("Half size", .5), ("2× size", 2),
                              ("3× size", 3), ("4× size", 4)):
            self.scale_preset.addItem(label, factor)
        self.scale_preset.currentIndexChanged.connect(self._apply_scale_preset)
        form.addRow("Quick size", self.scale_preset)
        layout.addLayout(form)
        self.image_preview = ProtectedImageView(settings=self.settings)
        self.image_preview.setMinimumHeight(225)
        layout.addWidget(self.image_preview, 1)
        preview_actions = QHBoxLayout()
        self.preview_source_button = QPushButton("Preview Original Winner")
        self.preview_saved_button = QPushButton("Preview Saved Version")
        self.preview_source_button.clicked.connect(self.preview_original_image)
        self.preview_saved_button.clicked.connect(self.preview_saved_image)
        preview_actions.addWidget(self.preview_source_button)
        preview_actions.addWidget(self.preview_saved_button)
        layout.addLayout(preview_actions)
        self.review = QLabel("The final size must preserve the exact source aspect ratio and be at most 40 megapixels.")
        self.review.setWordWrap(True); layout.addWidget(self.review)
        buttons = QHBoxLayout()
        self.inspect_button = QPushButton("Inspect Winner")
        self.preview_button = QPushButton("Review Faithful Resize")
        self.finish_button = QPushButton("Create Separate Version")
        self.cancel_button = QPushButton("Cancel Work")
        self.inspect_button.clicked.connect(self.inspect_winner)
        self.preview_button.clicked.connect(self.preview_finish)
        self.finish_button.clicked.connect(self.create_version)
        self.cancel_button.clicked.connect(self.cancel_work)
        for button in (self.inspect_button, self.preview_button, self.finish_button, self.cancel_button):
            buttons.addWidget(button)
        layout.addLayout(buttons)
        versions = QHBoxLayout()
        self.saved_version = QComboBox()
        self.saved_version.setAccessibleName("Saved finishing version for this winner")
        self.saved_version.currentIndexChanged.connect(self._saved_version_changed)
        self.find_versions_button = QPushButton("Find Saved Versions")
        self.find_versions_button.clicked.connect(self.load_saved_versions)
        versions.addWidget(self.saved_version, 1)
        versions.addWidget(self.find_versions_button)
        layout.addLayout(versions)
        next_buttons = QHBoxLayout()
        self.ingest_button = QPushButton("Review Finished Version in Ingest…")
        self.refine_button = QPushButton("Generative Refinement in New Remix Draft…")
        self.ingest_button.clicked.connect(self.send_to_ingest)
        self.refine_button.clicked.connect(self.refine_winner)
        next_buttons.addWidget(self.ingest_button); next_buttons.addWidget(self.refine_button)
        layout.addLayout(next_buttons)
        self.status = QLabel("Inspect a winner, then review its processing before creating a version.")
        self.status.setTextFormat(Qt.PlainText); self.status.setWordWrap(True)
        layout.addWidget(self.status)
        self.close_button = QPushButton("Close Finishing")
        self.close_button.clicked.connect(self.close)
        layout.addWidget(self.close_button)
        self._timer = QTimer(self); self._timer.setInterval(50)
        self._timer.timeout.connect(self._advance_close)
        self._update_actions()

    def _selected(self):
        return Path(str(self.winner.currentData())).absolute()

    def _winner_changed(self, *_):
        self._preview = None
        self.scale_preset.blockSignals(True)
        self.scale_preset.setCurrentIndex(0)
        self.scale_preset.blockSignals(False)
        self.image_preview.clear("Choose a verified preview")
        detail = self._source_details.get(self._selected())
        self.source_info.setText(
            f"Native {detail['width']} × {detail['height']} · exact verified PNG" if detail else
            "Inspect the selected winner to set an exact final size.")
        self.review.setText("Review the final size and its processing for this winner.")
        self._refresh_versions()
        self._update_actions()
        QTimer.singleShot(0, self.inspect_winner)

    def _size_changed(self, *_):
        if self._editing_sizes:
            return
        detail = self._source_details.get(self._selected())
        if detail is not None:
            native_w, native_h = detail["width"], detail["height"]
            divisor = gcd(native_w, native_h)
            unit_w, unit_h = native_w // divisor, native_h // divisor
            chosen = self.final_height.value() if self.sender() is self.final_height else self.final_width.value()
            unit = unit_h if self.sender() is self.final_height else unit_w
            maximum = min(8192 // unit_w, 8192 // unit_h, isqrt(40_000_000 // (unit_w * unit_h)))
            minimum = max((64 + unit_w - 1) // unit_w, (64 + unit_h - 1) // unit_h)
            if maximum >= minimum:
                multiple = min(maximum, max(minimum, round(chosen / unit)))
                self._editing_sizes = True
                try:
                    self.final_width.setValue(unit_w * multiple)
                    self.final_height.setValue(unit_h * multiple)
                finally:
                    self._editing_sizes = False
        self._size_by_source[self._selected()] = (self.final_width.value(), self.final_height.value())
        if self.sender() in (self.final_width, self.final_height):
            self.scale_preset.blockSignals(True)
            self.scale_preset.setCurrentIndex(0)
            self.scale_preset.blockSignals(False)
        self._preview = None
        native = detail and (detail["width"], detail["height"])
        self.review.setText(
            "Native size: Generative Refinement can open a new Remix draft. Faithful Resize needs a different final size."
            if native == (self.final_width.value(), self.final_height.value()) else
            (f"Native {detail['width']} × {detail['height']} → faithful local resize → "
             f"Final {self.final_width.value()} × {self.final_height.value()}. "
             "Review this size before creating the version.") if detail else
            "Size changed. Review the final resize again before creating the version.")
        self._update_actions()

    def _apply_scale_preset(self, *_):
        detail = self._source_details.get(self._selected())
        factor = self.scale_preset.currentData()
        if detail is None or factor is None:
            return
        width, height = detail["width"] * factor, detail["height"] * factor
        if width != int(width) or height != int(height) or not 64 <= width <= 8192 or not 64 <= height <= 8192 or width * height > 40_000_000:
            self.status.setText("That quick size exceeds the finishing bounds for this winner. Choose a smaller size.")
            return
        self._editing_sizes = True
        try:
            self.final_width.setValue(int(width)); self.final_height.setValue(int(height))
        finally:
            self._editing_sizes = False
        self._size_changed()

    def _saved_version_changed(self, *_):
        source = self._selected()
        fingerprint = self.saved_version.currentData()
        result = next((item for item in self._versions.get(source, ())
                       if item.get("fingerprint") == fingerprint), None)
        if result is not None:
            self._finished[source] = result
            self._selected_version_fingerprint[source] = fingerprint
        else:
            self._finished.pop(source, None)
        self._update_actions()

    def _refresh_versions(self):
        source = self._selected()
        chosen = self._selected_version_fingerprint.get(source)
        self.saved_version.blockSignals(True)
        self.saved_version.clear()
        for result in self._versions.get(source, ()):
            size = result.get("plan", {}).get("target_size", ("?", "?"))
            self.saved_version.addItem(f"Saved {size[0]} × {size[1]} · {result['fingerprint'][:8]}",
                                       result["fingerprint"])
        if chosen:
            index = self.saved_version.findData(chosen)
            if index >= 0:
                self.saved_version.setCurrentIndex(index)
        self.saved_version.blockSignals(False)
        self._saved_version_changed()

    def _run(self, label, worker, completed):
        if self._task is not None or self._closing:
            return
        task = CoordinatedCallTask(self.jobs, label, worker, resource="io")
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
            self.background_work_finished.emit()
        self._update_actions(); self._advance_close()

    def _failed(self, task, error):
        if self._task is task:
            self._task = None
            self.status.setText(str(error))
            if not self._closing:
                self.background_work_finished.emit()
            self._update_actions(); self._advance_close()

    def _update_actions(self):
        ready = self._task is None and not self._closing
        selected = self._selected()
        detail = self._source_details.get(selected)
        width, height = self.final_width.value(), self.final_height.value()
        valid_goal = bool(detail and 64 <= width <= 8192 and 64 <= height <= 8192
                          and width * height <= 40_000_000
                          and width * detail["height"] == height * detail["width"])
        distinct = valid_goal and (width, height) != (detail["width"], detail["height"])
        self.winner.setEnabled(ready)
        self.final_width.setEnabled(ready); self.final_height.setEnabled(ready)
        self.scale_preset.setEnabled(ready)
        self.inspect_button.setEnabled(ready)
        self.preview_button.setEnabled(ready and distinct)
        self.preview_source_button.setEnabled(ready and selected in self._source_details)
        self.preview_saved_button.setEnabled(ready and selected in self._finished)
        self.finish_button.setEnabled(ready and self._preview is not None)
        self.saved_version.setEnabled(ready and bool(self._versions.get(selected)))
        self.find_versions_button.setEnabled(ready)
        self.ingest_button.setEnabled(ready and selected in self._finished)
        self.refine_button.setEnabled(ready and valid_goal)
        self.cancel_button.setEnabled(self._task is not None)
        self.close_button.setEnabled(ready)

    def inspect_winner(self):
        if self._task is not None or self._closing:
            return
        self._preview = None
        source = self._selected()
        saved_size = self._size_by_source.get(source)
        goal = self._initial_goals.get(source, {})
        def work(_job):
            detail = inspect_generation_finish_source(source,
                storage_root=self.storage_root, verified_source=self.verified_source)
            width, height = (saved_size if saved_size is not None else
                             (goal.get("final_width"), goal.get("final_height")))
            if type(width) is int and type(height) is int:
                try:
                    preview_generation_finish(source, width, height,
                        storage_root=self.storage_root, verified_source=self.verified_source)
                except ValueError:
                    detail["initial_goal_invalid"] = True
                else:
                    detail["initial_goal_size"] = (width, height)
            native_w, native_h = detail["width"], detail["height"]
            divisor = gcd(native_w, native_h)
            unit_w, unit_h = native_w // divisor, native_h // divisor
            maximum = min(8192 // unit_w, 8192 // unit_h,
                          isqrt(40_000_000 // (unit_w * unit_h)))
            minimum = max((64 + unit_w - 1) // unit_w, (64 + unit_h - 1) // unit_h)
            if maximum >= minimum:
                multiple = min(maximum, max(minimum, divisor * 2))
                detail["fallback_size"] = (unit_w * multiple, unit_h * multiple)
            return detail
        def complete(detail):
            self._source_details[source] = detail
            if self._selected() != source:
                return
            self.source_info.setText(f"Native {detail['width']} × {detail['height']} · exact verified PNG")
            size = detail.get("initial_goal_size", detail.get("fallback_size"))
            if size is not None:
                width, height = size
                self._editing_sizes = True
                try:
                    self.final_width.setValue(width); self.final_height.setValue(height)
                finally:
                    self._editing_sizes = False
                self._size_by_source[source] = (self.final_width.value(), self.final_height.value())
            else:
                self._editing_sizes = True
                try:
                    self.final_width.setValue(64); self.final_height.setValue(64)
                finally:
                    self._editing_sizes = False
                self.status.setText("No bounded same-aspect final size is available for this winner.")
                return
            self.status.setText(
                "Saved final size does not match this winner; choose a valid size and review it."
                if detail.get("initial_goal_invalid") else
                "Source verified. Review or edit the final dimensions, then preview the resize.")
            if (self.final_width.value(), self.final_height.value()) == (detail["width"], detail["height"]):
                self.review.setText("Native size: Generative Refinement opens a new Remix draft for review. Faithful Resize needs a different final size; the original remains available.")
            else:
                self.review.setText(f"Native {detail['width']} × {detail['height']} → faithful local resize → Final {self.final_width.value()} × {self.final_height.value()}. Review the processing before creating a separate version.")
        self._run("Inspect verified winner", work, complete)

    def preview_finish(self):
        source = self._selected()
        width, height = self.final_width.value(), self.final_height.value()
        def complete(plan):
            if self._selected() != source or (self.final_width.value(), self.final_height.value()) != (width, height):
                self.status.setText("Selection or size changed; preview again.")
                return
            self._preview = plan
            self.review.setText(f"{plan['source_size'][0]} × {plan['source_size'][1]} → {width} × {height}. Faithful Resize uses local processing and saves a separate image. Estimated space: {plan['estimated_bytes'] / 1048576:.1f} MiB. Upscaling adds no true detail; the original stays intact.")
            self.status.setText("Resize reviewed. Create the separate version when ready.")
        self._run("Preview faithful resize", lambda _job: preview_generation_finish(source, width, height,
            storage_root=self.storage_root, verified_source=self.verified_source), complete)

    def load_saved_versions(self):
        """Bounded, cancellable recovery of this chooser's exact versions only."""
        def work(job):
            return discover_generation_finishes(self.storage_root, self.paths,
                self.verified_source, cancelled=lambda: job.cancel_requested)
        def complete(result):
            self._versions = result["versions"]
            self._refresh_versions()
            count = sum(len(values) for values in self._versions.values())
            suffix = " Search limit reached; refine the selection." if result["truncated"] else ""
            self.status.setText(f"Found {count} verified saved version(s); {result['unavailable']} unavailable entry(ies).{suffix}")
        self._run("Find saved finishing versions", work, complete)

    def _preview_image(self, path, *, result=None):
        def work(_job):
            if result is None:
                inspect_generation_finish_source(path, storage_root=self.storage_root,
                                                 verified_source=self.verified_source)
            else:
                verify_generation_finish(result, storage_root=self.storage_root,
                                         verified_source=self.verified_source)
            raw, _ = read_stable_training_source(path)
            if len(raw) > 128 * 1024 * 1024:
                raise ValueError("The verified preview exceeds the image byte limit.")
            if result is None:
                inspect_generation_finish_source(path, storage_root=self.storage_root,
                                                 verified_source=self.verified_source)
            else:
                verify_generation_finish(result, storage_root=self.storage_root,
                                         verified_source=self.verified_source)
            return raw
        def complete(raw):
            image = QImage.fromData(raw)
            if image.isNull():
                raise ValueError("The selected verified image could not be previewed.")
            self.image_preview.set_image(QPixmap.fromImage(image), path)
            self.status.setText("Verified image preview. Reveal is temporary if your content-safety setting protects unknown images.")
        self._run("Preview verified image", work, complete)

    def preview_original_image(self):
        self._preview_image(self._selected())

    def preview_saved_image(self):
        result = self._finished.get(self._selected())
        if result is not None:
            self._preview_image(Path(result["output"]), result=result)

    def prepare_for_privacy_capture(self):
        self.image_preview.prepare_for_privacy_capture()

    def create_version(self):
        plan = self._preview
        if plan is None:
            return
        source = self._selected()
        def complete(result):
            if result.get("state") == "verified":
                self._finished[source] = result
                versions = self._versions.setdefault(source, [])
                if all(item["fingerprint"] != result["fingerprint"] for item in versions):
                    versions.append(result)
                self._selected_version_fingerprint[source] = result["fingerprint"]
                self._refresh_versions()
                self.status.setText("Separate version verified. Review it in Ingest; the original remains unchanged. You can finish another selected winner.")
            else:
                self.status.setText("Finishing was cancelled before publication. Review and retry.")
        self._run("Create separate finishing version", lambda job: execute_generation_finish(plan,
            storage_root=self.storage_root, verified_source=self.verified_source,
            cancelled=lambda: job.cancel_requested), complete)

    def send_to_ingest(self):
        source = self._selected()
        result = self._finished.get(source)
        if result is None:
            return
        def complete(path):
            self.ingest_requested.emit([path])
            self.status.setText("Finished version sent to Ingest for explicit review.")
        self._run("Recheck finishing and generation receipts", lambda _job: verify_generation_finish(result,
            storage_root=self.storage_root, verified_source=self.verified_source), complete)

    def refine_winner(self):
        source = self._selected()
        width, height = self.final_width.value(), self.final_height.value()
        def complete(payload):
            payload["verified_source"] = self.verified_source
            self.refine_requested.emit(payload)
            self.status.setText("A new source-bound Remix draft was requested. The native winner remains intact.")
        self._run("Recheck winner for Remix", lambda _job: verified_generation_refinement(source, width, height,
            storage_root=self.storage_root, verified_source=self.verified_source), complete)

    def cancel_work(self):
        if self._task is not None and self._task.job_id:
            try:
                self.jobs.cancel(self._task.job_id)
                self.status.setText("Cancellation requested; waiting for the exact finishing operation to stop.")
            except Exception as exc:
                self.status.setText("Cancellation could not be confirmed: " + str(exc))

    def has_active_background_work(self):
        return self._task is not None

    def request_background_shutdown(self):
        if not self._closing:
            self._closing = True
            self.cancel_work()
            self.prepare_for_privacy_capture()
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
