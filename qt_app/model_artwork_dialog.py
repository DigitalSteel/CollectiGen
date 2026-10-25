"""Explicit local artwork review and bounded, protected cached presentation."""
from __future__ import annotations

from copy import deepcopy
from collections import deque
from itertools import islice
from threading import Lock
from pathlib import Path
import re

from PySide6.QtCore import QObject, Signal, QTimer, Qt, QSignalBlocker
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel, QLineEdit, QPlainTextEdit, QPushButton, QVBoxLayout, QWidget

from .gallery import ProtectedImageView
from .workers import CoordinatedCallTask


class ArtworkSafetyPolicy:
    """Read-free policy: artwork has no dependable Image Knowledge rating."""

    def __init__(self, settings=None):
        self.settings = settings

    def preview_state(self, path, *, honor_session_reveal=False):
        blurred = bool(getattr(self.settings, "gallery_blur_unknown", True))
        return dict(rating="unknown", basis="Model artwork has no dependable content rating",
                    checking=False, requires_blur=blurred, blurred=blurred)


def artwork_service(registry, settings=None, protected_roots=()):
    """Defer all boundary/snapshot reads until the core admits an operation."""
    from core.model_artwork import ModelArtworkService
    from .model_companion_workflow import resource_boundaries
    from core.model_storage_policy import retained_fast_roots

    def boundaries():
        snapshot, _ = registry.snapshot()
        protected, managed = resource_boundaries(snapshot, settings, protected_roots)
        return (*protected, *retained_fast_roots(snapshot)), managed

    class DeferredArtworkService:
        def __init__(self):
            self._service = None
            self._lock = Lock()

        def __getattr__(self, name):
            with self._lock:
                if self._service is None:
                    protected, managed = boundaries()
                    self._service = ModelArtworkService(registry, protected_roots=protected,
                        managed_roots=managed, boundary_provider=boundaries)
            return getattr(self._service, name)

    return DeferredArtworkService()


def thumbnail_image(raw):
    """Second bounded decoder gate for validated, normalized core PNG bytes."""
    from PySide6.QtCore import QBuffer, QByteArray, QIODevice
    from PySide6.QtGui import QImageReader
    if not isinstance(raw, bytes) or len(raw) > 2 * 1024 * 1024:
        raise ValueError("Artwork thumbnail exceeds the display limit.")
    buffer = QBuffer()
    buffer.setData(QByteArray(raw)); buffer.open(QIODevice.ReadOnly)
    reader = QImageReader(buffer, b"PNG")
    size = reader.size()
    if not size.isValid() or not 0 < size.width() <= 512 or not 0 < size.height() <= 512:
        raise ValueError("Artwork thumbnail has unsupported dimensions.")
    image = reader.read()
    if image.isNull():
        raise ValueError("Artwork thumbnail could not be decoded.")
    return image


class ArtworkPageLoader(QObject):
    """One bounded batch in flight; generation tokens reject superseded pages."""

    changed = Signal()
    LIMIT = 100
    BATCH = 4

    def __init__(self, service, jobs, parent=None, *, settings=None):
        super().__init__(parent)
        self.service, self.jobs = service, jobs
        self.policy = ArtworkSafetyPolicy(settings)
        self.images, self.states = {}, {}
        self._queue = deque()
        self._task = None
        self._generation = 0
        self._closed = False

    @staticmethod
    def key(row):
        return str(row.resource_id)

    def set_rows(self, rows):
        self._generation += 1
        if self._task is not None:
            self.jobs.cancel(self._task.job_id)
        self.images.clear(); self.states.clear(); self._queue.clear()
        seen = set()
        for row in tuple(rows)[:self.LIMIT]:
            key = self.key(row)
            if key in seen: continue
            seen.add(key)
            status = getattr(row, "artwork_status", "absent")
            self.states[key] = status
            if status == "recorded" and getattr(row, "artwork", None):
                self.states[key] = "loading"
                self._queue.append((key, deepcopy(row.artwork)))
        self.changed.emit()
        self._start()

    def _start(self):
        if self._closed or self._task is not None or not self._queue: return
        batch = [self._queue.popleft() for _ in range(min(self.BATCH, len(self._queue)))]
        generation = self._generation

        def read(job):
            result = []
            for key, descriptor in batch:
                if job.cancel_requested: break
                try:
                    value = self.service.resolve(descriptor, should_cancel=lambda: job.cancel_requested)
                    result.append((key, thumbnail_image(value.png_bytes), "ready"))
                except Exception:
                    result.append((key, QImage(), "unavailable"))
            return result

        task = CoordinatedCallTask(self.jobs, "Reading cached model artwork", read, resource="io")
        self._task = task
        task.signals.complete.connect(lambda result: self._finished(task, generation, result))
        task.signals.failed.connect(lambda _: self._finished(task, generation, [(k, QImage(), "unavailable") for k, _ in batch]))
        task.signals.cancelled.connect(lambda: self._finished(task, generation, []))
        task.start()

    def _finished(self, task, generation, result):
        if self._task is not task: return
        self._task = None
        if self._closed: return
        if generation == self._generation:
            for key, image, state in result:
                self.states[key] = state
                if not image.isNull(): self.images[key] = QPixmap.fromImage(image)
            self.changed.emit()
        self._start()

    def display_pixmap(self, key):
        if self.policy.preview_state(key)["blurred"]: return QPixmap()
        return self.images.get(key, QPixmap())

    def label(self, key):
        state = self.states.get(key, "absent")
        if state == "ready":
            return "Protected artwork" if self.policy.preview_state(key)["blurred"] else "Local artwork · unrated"
        return {"absent": "No artwork", "unsupported": "Unsupported artwork record",
                "loading": "Loading cached artwork", "recorded": "Recorded artwork",
                "unavailable": "Artwork unavailable · reselect in Models"}.get(state, "Artwork unavailable")

    def close(self):
        self._closed = True; self._generation += 1; self._queue.clear()
        self.images.clear()
        if self._task is not None: self.jobs.cancel(self._task.job_id)


class ModelArtworkDialog(QDialog):
    """Review one explicitly selected artwork source before applying a change."""

    def __init__(self, service, jobs, resource_id, parent=None, *, settings=None, read_only=False,
                 registry=None, swarm_service_factory=None):
        super().__init__(parent)
        self.service, self.jobs, self.resource_id = service, jobs, resource_id
        self.read_only = bool(read_only)
        self.registry, self.swarm_service_factory = registry, swarm_service_factory
        self._snapshot = None; self._swarm_service = None
        self.has_changed = False
        self._task = None; self._closing = False; self._parent_close = None
        self._descriptor = None; self._revision = ""; self._review = None
        self._mode = ""; self._operation_id = ""
        self.setWindowTitle("Model Preview Artwork"); self.resize(820, 760)
        layout = QVBoxLayout(self)
        self.consequences = QLabel("Choose local artwork for this model. No image is sent online.")
        self.consequences.setTextFormat(Qt.PlainText)
        self.consequences.setWordWrap(True); layout.addWidget(self.consequences)
        self.source_controls = QWidget(); source_layout = QVBoxLayout(self.source_controls)
        source_layout.setContentsMargins(0, 0, 0, 0)
        source_row = QHBoxLayout(); source_row.addWidget(QLabel("New artwork source"))
        self.source_kind = QComboBox(); self.source_kind.setAccessibleName("New artwork source")
        self.source_kind.addItem("Local image", "local"); self.source_kind.addItem("SwarmUI", "swarm")
        source_row.addWidget(self.source_kind, 1); source_layout.addLayout(source_row)
        self.swarm_controls = QWidget(); swarm_layout = QVBoxLayout(self.swarm_controls)
        swarm_layout.setContentsMargins(0, 0, 0, 0)
        permanent_row = QHBoxLayout(); permanent_row.addWidget(QLabel("Permanent registered file"))
        self.swarm_path = QComboBox(); self.swarm_path.setAccessibleName("Permanent registered model file for artwork")
        permanent_row.addWidget(self.swarm_path, 1); swarm_layout.addLayout(permanent_row)
        match_row = QHBoxLayout(); match_row.addWidget(QLabel("Matching Swarm entry"))
        self.swarm_source = QComboBox(); self.swarm_source.setAccessibleName("Selected Swarm artwork correspondence")
        for combo in (self.swarm_path, self.swarm_source):
            combo.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
            combo.setMinimumContentsLength(24)
        match_row.addWidget(self.swarm_source, 1)
        self.find_swarm = QPushButton("Find Matching Entries"); self.find_swarm.clicked.connect(self._find_swarm)
        match_row.addWidget(self.find_swarm); swarm_layout.addLayout(match_row)
        self.swarm_hint = QLabel("Choose a permanent registered file in an enrolled Swarm model folder. "
            "Managed Fast copies are not matched; choose a local image if only a Fast copy is available.")
        self.swarm_hint.setWordWrap(True); swarm_layout.addWidget(self.swarm_hint)
        source_layout.addWidget(self.swarm_controls); layout.addWidget(self.source_controls)
        self.source_kind.currentIndexChanged.connect(self._source_changed)
        self.swarm_path.currentIndexChanged.connect(self._path_changed)
        self.swarm_source.currentIndexChanged.connect(self._swarm_choice_changed)
        self.swarm_controls.hide()
        self.source_controls.setVisible(not self.read_only and registry is not None and swarm_service_factory is not None)
        self.preview = ProtectedImageView(policy=ArtworkSafetyPolicy(settings), settings=settings)
        layout.addWidget(self.preview, 1)
        self.source = QPlainTextEdit(); self.source.setReadOnly(True); self.source.setMaximumHeight(112)
        self.source.setAccessibleName("Artwork source and provenance")
        layout.addWidget(self.source)
        self.status = QLabel("Reading recorded artwork…"); self.status.setWordWrap(True); layout.addWidget(self.status)
        self.status.setTextFormat(Qt.PlainText)
        actions = QHBoxLayout(); layout.addLayout(actions)
        self.choose = QPushButton("Choose Local Image…"); self.choose.clicked.connect(self._choose)
        self.read_swarm = QPushButton("Read Selected Artwork"); self.read_swarm.clicked.connect(self._read_swarm)
        self.remove = QPushButton("Review Removal"); self.remove.clicked.connect(self._review_remove)
        self.recovery = QPushButton("Review Recovery"); self.recovery.clicked.connect(self._recover_review)
        self.apply = QPushButton("Apply Artwork"); self.apply.setProperty("primary", True); self.apply.clicked.connect(self._apply)
        self.cancel = QPushButton("Cancel Work"); self.cancel.clicked.connect(self._cancel)
        self.back = QPushButton("Close"); self.back.clicked.connect(self.reject)
        for button in (self.choose, self.read_swarm, self.remove, self.recovery, self.apply, self.cancel, self.back):
            button.setAutoDefault(False); actions.addWidget(button)
        self.reload_button = QPushButton("Reload Saved Artwork"); self.reload_button.clicked.connect(self.reload)
        layout.addWidget(self.reload_button)
        recovery_row = QHBoxLayout(); layout.addLayout(recovery_row)
        self.recovery_id = QLineEdit(); self.recovery_id.setReadOnly(True)
        self.recovery_id.setPlaceholderText("No recovery receipt selected")
        self.recovery_id.setAccessibleName("Saved artwork recovery receipt location")
        self.open_recovery = QPushButton("Open Artwork Recovery…"); self.open_recovery.clicked.connect(self._open_recovery)
        self.new_review = QPushButton("Start New Review"); self.new_review.clicked.connect(self._new_review)
        recovery_row.addWidget(self.recovery_id, 1); recovery_row.addWidget(self.open_recovery); recovery_row.addWidget(self.new_review)
        if self.read_only:
            self.consequences.setText("Saved model artwork. Choose or replace artwork from Models → Preview Artwork.")
            for button in (self.choose, self.read_swarm, self.remove, self.recovery, self.apply): button.hide()
            for widget in (self.recovery_id, self.open_recovery, self.new_review): widget.hide()
        self._actions(); QTimer.singleShot(0, self.reload)

    def _actions(self):
        busy = self._task is not None
        idle = not busy and not self._operation_id and not self._closing and not self.read_only
        swarm = self.source_kind.currentData() == "swarm"
        self.source_kind.setEnabled(idle)
        self.swarm_controls.setVisible(swarm and not self.read_only)
        self.choose.setVisible(not swarm and not self.read_only)
        self.read_swarm.setVisible(swarm and not self.read_only)
        self.swarm_path.setEnabled(idle); self.swarm_source.setEnabled(idle)
        self.find_swarm.setEnabled(idle and bool(self.swarm_path.currentData()) and self._snapshot is not None)
        self.read_swarm.setEnabled(idle and self.swarm_source.currentData() is not None)
        self.choose.setEnabled(not busy and not self._operation_id)
        self.choose.setText("Reselect Local Image…" if self._descriptor is not None else "Choose Local Image…")
        self.remove.setEnabled(not busy and self._descriptor is not None and not self._operation_id)
        self.recovery.setVisible(bool(self._operation_id) and not self.read_only); self.recovery.setEnabled(not busy)
        self.apply.setEnabled(not busy and self._review is not None)
        self.cancel.setEnabled(busy)
        self.reload_button.setEnabled(not busy and not self._operation_id)
        self.open_recovery.setEnabled(not busy); self.recovery_id.setEnabled(not busy)
        self.new_review.setEnabled(not busy and bool(self._operation_id))

    def _run(self, label, function, done):
        if self._task is not None or self._closing: return
        self.status.setText(label)
        # Retain the terminal value: cancelling an observed job discards its result.
        terminal = {}
        def run(job):
            try: terminal["result"] = function(job)
            except Exception as exc: terminal["error"] = exc
            return terminal
        task = CoordinatedCallTask(self.jobs, label, run, resource="io")
        self._task = task
        task.signals.complete.connect(lambda result: self._finished(task, done, result))
        task.signals.cancelled.connect(lambda: self._finished(task, done, terminal))
        task.signals.failed.connect(lambda message: self._finished(task, done, {"error": RuntimeError(message)}))
        self._actions(); task.start()

    def _finished(self, task, done, terminal):
        if self._task is not task: return
        self._task = None
        if "error" in terminal:
            exc = terminal["error"]
            self._operation_id = str(getattr(exc, "operation_id", "") or self._operation_id)
            if self._operation_id:
                self.recovery_id.setText(str(self.service.root / self._operation_id / "receipt.json"))
            self._review = None; self._mode = ""
            if getattr(exc, "status", "") in ("unavailable", "unsupported"):
                self.preview.clear("No preview")
                self.status.setText("Swarm artwork " + exc.status + ": " + str(exc.reason)
                    + " Choose a local image or explicitly read another matching entry.")
            else:
                self.status.setText(str(exc) + (" Review Recovery before making another change." if self._operation_id else " Reload or reselect to review a fresh choice."))
        elif "result" in terminal:
            done(terminal["result"])
        else: self.status.setText("Stopped at a safe boundary.")
        self._actions()
        if self._closing:
            super().reject()
            if self._parent_close: self._parent_close()

    def reload(self):
        def read(job):
            descriptor, token = self.service.read(self.resource_id)
            snapshot = None
            if self.registry is not None and not self.read_only:
                snapshot, snapshot_token = self.registry.snapshot()
                if snapshot_token != token:
                    raise RuntimeError("Model records changed while loading artwork. Reload to use the current choices.")
            image, error = QImage(), ""
            if descriptor:
                try: image = thumbnail_image(self.service.resolve(descriptor, should_cancel=lambda: job.cancel_requested).png_bytes)
                except Exception as exc: error = str(exc)
            return descriptor, token, image, error, snapshot
        self._run("Reading recorded artwork", read, self._loaded)

    def _show_image(self, image, key):
        if image.isNull(): self.preview.clear("No preview")
        else: self.preview.set_image(QPixmap.fromImage(image), "model-artwork-" + str(key))

    def _loaded(self, result):
        self._descriptor, self._revision, image, error, self._snapshot = result
        self._swarm_service = None
        self._load_swarm_paths()
        self._review = None; self._mode = ""
        self.apply.setText("Apply Artwork")
        self.consequences.setText("Saved model artwork. Choose or replace artwork from Models → Preview Artwork."
            if self.read_only else "Choose artwork for this model. Apply saves a small local preview copy; the source stays unchanged."
            + (" Earlier recovery receipts remain intact." if self.recovery_id.text() else ""))
        self._show_image(image, (self._descriptor or {}).get("operation_id", self.resource_id))
        self._show_source(self._descriptor, "Recorded source")
        self.status.setText("Artwork unavailable. Reselect an image or remove its association. " + error if error else
                            "Saved cached artwork. The source is not opened when browsing." if self._descriptor else "No artwork assigned.")

    def _show_source(self, descriptor, label):
        source = (descriptor or {}).get("source", {})
        if not source:
            self.source.setPlainText(label + ": None"); return
        if source.get("kind") == "swarm_inline":
            text = (f"{label}: SwarmUI · {source.get('subtype', 'Unknown')} · {source.get('model_name', 'Unknown')}\n"
                f"Server: {source.get('server_id', 'Unknown')} · Endpoint: {source.get('endpoint', 'Unknown')}\n"
                f"{source.get('width', '?')} × {source.get('height', '?')} · {source.get('format', 'Unknown')} · {source.get('size', '?')} bytes\n"
                f"Source revision: {source.get('source_revision', 'Unknown')}\n"
                f"Folder fingerprint: {source.get('folder_fingerprint', 'Unknown')}\n"
                f"Image SHA-256: {source.get('sha256', 'Unknown')}\n"
                f"Inline source SHA-256: {source.get('data_uri_sha256', 'Unknown')}")
        else:
            text = (f"{label}: {source.get('path', 'Unknown')}\n"
                f"{source.get('width', '?')} × {source.get('height', '?')} · {source.get('format', 'Unknown')} · {source.get('size', '?')} bytes")
        self.source.setPlainText(text)

    def _load_swarm_paths(self):
        selected = self.swarm_path.currentData()
        record = (self._snapshot or {}).get("resources", {}).get(self.resource_id, {})
        with QSignalBlocker(self.swarm_path):
            self.swarm_path.clear(); self.swarm_path.addItem("Choose a permanent registered file…", None)
            for location in islice(iter(record.get("locations", ())), 257):
                if not isinstance(location, dict) or location.get("cache_entry_id") or not location.get("path"): continue
                path = str(location["path"])
                self.swarm_path.addItem(path, path); self.swarm_path.setItemData(self.swarm_path.count() - 1, path, Qt.ToolTipRole)
            index = self.swarm_path.findData(selected)
            self.swarm_path.setCurrentIndex(index if index >= 0 else 0)
        self._clear_swarm_choices()

    def _clear_swarm_choices(self):
        with QSignalBlocker(self.swarm_source):
            self.swarm_source.clear(); self.swarm_source.addItem("Find and choose a matching entry…", None)

    def _clear_source_review(self, message):
        self._review = None; self._mode = ""
        self.apply.setText("Apply Artwork")
        self.preview.clear("No preview")
        self.source.clear(); self.status.setText(message)
        self.consequences.setText("A source choice does not change saved artwork. Review the image before Apply.")

    def _source_changed(self, *_):
        self._clear_source_review("Choose a local image." if self.source_kind.currentData() == "local"
            else "Choose a permanent registered file, then explicitly find its matching Swarm entry.")
        self._actions()

    def _path_changed(self, *_):
        self._clear_swarm_choices()
        self._clear_source_review("Find matching entries for this permanent registered file."); self._actions()

    def _swarm_choice_changed(self, *_):
        self._clear_source_review("Read the selected artwork to review it. No artwork has been read for this choice.")
        self._actions()

    def _find_swarm(self):
        path = self.swarm_path.currentData()
        if self.read_only or self.source_kind.currentData() != "swarm" or self._task is not None or self._operation_id or not path or self._snapshot is None or self.swarm_service_factory is None: return
        token, snapshot = self._revision, self._snapshot
        self._clear_swarm_choices(); self._clear_source_review("Finding matching Swarm entries…")
        def find(job):
            if self._swarm_service is None:
                self._swarm_service = self.swarm_service_factory(self.service, snapshot)
            return self._swarm_service.candidates(self.resource_id, path, expected_registry=token,
                should_cancel=lambda: job.cancel_requested)
        self._run("Finding matching Swarm entries", find, self._swarm_found)

    def _swarm_found(self, choices):
        with QSignalBlocker(self.swarm_source):
            for choice in islice(iter(choices), 65):
                label = f"{choice.subtype} · {choice.model_name}"
                self.swarm_source.addItem(label, choice); self.swarm_source.setItemData(self.swarm_source.count() - 1, label, Qt.ToolTipRole)
        self.status.setText("Choose a matching entry, then Read Selected Artwork. No artwork has been read yet."
            if self.swarm_source.count() > 1 else "No matching entry for this permanent file. Choose another registered file or a local image; managed Fast copies are not matched.")

    def _read_swarm(self):
        choice, path = self.swarm_source.currentData(), self.swarm_path.currentData()
        if self.read_only or self.source_kind.currentData() != "swarm" or self._task is not None or self._operation_id or choice is None or not path or self._swarm_service is None: return
        token, service = self._revision, self._swarm_service
        self._clear_source_review("Reading selected Swarm artwork…")
        def read(job):
            review = service.preview(self.resource_id, path, subtype=choice.subtype, model_name=choice.model_name,
                expected_registry=token, should_cancel=lambda: job.cancel_requested)
            return review, thumbnail_image(review.artwork.thumbnail_bytes)
        self._run("Reading selected Swarm artwork", read, self._swarm_previewed)

    def _swarm_previewed(self, result):
        self._review, image = result; self._mode = "swarm"
        self._present_review(self._review.artwork, image)

    def _choose(self):
        if self.read_only or self._task is not None or self._operation_id: return
        path, _ = QFileDialog.getOpenFileName(self, "Choose Local Model Artwork", "", "Still Images (*.png *.jpg *.jpeg *.webp)")
        if path: self.preview_path(path)

    def preview_path(self, path):
        if self.read_only or self._task is not None or self._operation_id: return
        self._review = None; self._mode = ""
        token = self._revision
        def read(job):
            review = self.service.preview_local(self.resource_id, path, expected_registry=token,
                should_cancel=lambda: job.cancel_requested)
            return review, thumbnail_image(review.thumbnail_bytes)
        self._run("Reviewing selected local image", read, self._previewed)

    def _previewed(self, result):
        self._review, image = result; self._mode = "assign"
        self._present_review(self._review, image)

    def _present_review(self, review, image):
        self._show_image(image, review.review_id)
        self._show_source(review.descriptor, "Selected source")
        self.consequences.setText("Apply saves a small local preview copy and associates it with this model. "
            + ("This replaces the current artwork association. " if review.before is not None else "")
            + "The source image stays unchanged. Existing cached artwork is retained. This does not rate the image or change model readiness.")
        self.apply.setText("Apply Artwork"); self.status.setText("Review the selected source and protected preview, then Apply.")

    def _review_remove(self):
        if self.read_only or self._task is not None or self._descriptor is None or self._operation_id: return
        self._review = deepcopy(self._descriptor); self._mode = "remove"
        self.consequences.setText("Remove only this model's artwork association. The original image and existing cached preview remain intact.")
        self.apply.setText("Remove Association"); self.status.setText("Review removal, then confirm with Remove Association."); self._actions()

    def _recover_review(self):
        if self.read_only or self._task is not None or not self._operation_id: return
        self._run("Reviewing artwork recovery", lambda job: self.service.preview_recovery(
            self._operation_id, should_cancel=lambda: job.cancel_requested), self._recovery_ready)

    def _open_recovery(self):
        if self.read_only or self._task is not None: return
        path, _ = QFileDialog.getOpenFileName(self, "Open Saved Artwork Recovery", str(self.service.root), "Artwork Receipt (receipt.json)")
        if path: self.open_recovery_path(path)

    def open_recovery_path(self, path):
        if self.read_only or self._task is not None: return
        chosen = Path(path)
        operation = chosen.parent.name
        if (not re.fullmatch(r"[0-9a-f]{32}", operation)
                or not chosen.is_absolute() or chosen != self.service.root / operation / "receipt.json"):
            self.status.setText("Choose an exact receipt.json inside this registry's model-artwork folder."); return
        self.recovery_id.setText(str(chosen))
        self._review = None; self._mode = ""; self._operation_id = operation
        self._recover_review()

    def _new_review(self):
        if self.read_only or self._task is not None: return
        self._operation_id = ""; self._review = None; self._mode = ""
        self.consequences.setText("Starting a fresh review preserves the earlier recovery receipt and cached artwork.")
        self.reload()

    def _recovery_ready(self, review):
        if review.resource_id != self.resource_id:
            self._review = None; self._mode = ""
            self.status.setText("This recovery belongs to another model. Select that model in Models, or start a new review here.")
            return
        self._show_source(review.descriptor, "Retained source")
        self.preview.clear("Recovery")
        self.status.setText(review.message); self._review = review if review.status == "ready" else None
        self._mode = "recover"; self.apply.setText("Apply Recovery")
        self.consequences.setText("Recover only the exact retained artwork publication. Recovery does not reopen the source image.")
        if review.status == "completed":
            self.has_changed = True; self._operation_id = ""; self.reload()

    def _apply(self):
        if self.read_only or self._task is not None or self._review is None: return
        review, mode, token = self._review, self._mode, self._revision
        self._review = None
        def apply(job):
            if mode == "remove":
                return self.service.remove(self.resource_id, expected_registry=token, expected_descriptor=review, confirmed=True)
            if mode == "recover": return self.service.recover(review, confirmed=True, should_cancel=lambda: job.cancel_requested)
            if mode == "swarm": return self._swarm_service.apply(review, confirmed=True, should_cancel=lambda: job.cancel_requested)
            return self.service.apply(review, confirmed=True, should_cancel=lambda: job.cancel_requested)
        self._run("Applying reviewed artwork", apply, self._applied)

    def _applied(self, result):
        self.has_changed = True; self._operation_id = ""; self._mode = ""
        self._descriptor, self._revision = result.descriptor, result.registry_token
        self.status.setText("Artwork association saved.")
        if not self._closing: self.reload()

    def _cancel(self):
        if self._task is not None: self.jobs.cancel(self._task.job_id)

    def has_active_background_work(self): return self._task is not None

    def request_parent_close(self, callback):
        self._parent_close = callback; self.reject()
        return self._task is None

    def reject(self):
        self._closing = True
        if self._task is not None:
            self._cancel(); self.status.setText("Stopping at a safe artwork boundary…"); return
        super().reject()

    def closeEvent(self, event):
        if self._task is not None: self.reject(); event.ignore(); return
        super().closeEvent(event)
