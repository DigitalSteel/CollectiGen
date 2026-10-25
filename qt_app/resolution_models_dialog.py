"""One optional upscaler acquisition window shared by Setup, Models and Resize."""
from __future__ import annotations

from pathlib import Path
from threading import Event

from PySide6.QtWidgets import (QComboBox, QDialog, QFileDialog, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QTextEdit, QVBoxLayout)
from PySide6.QtCore import Qt, QUrl
from PySide6.QtGui import QDesktopServices

from core.model_workspace import ModelWorkspace
from core.resolution_models import (UPSCALERS, install_upscaler,
                                   preview_upscaler, verify_upscaler)
from .workers import CoordinatedCallTask


class UpscalerModelsDialog(QDialog):
    def __init__(self, registry, jobs, parent=None, *, protected_roots=(), allow_select=True):
        super().__init__(parent)
        self.registry, self.jobs = registry, jobs
        self.protected_roots = tuple(protected_roots)
        self.allow_select = allow_select
        self._registered_location = ""
        self.selected_path = ""
        self._task = None
        self._plan = None
        self._verified_path = ""
        self._stop = Event()
        self.setWindowTitle("Get Upscaler Models")
        self.resize(820, 630)
        layout = QVBoxLayout(self)
        title = QLabel("Add a model for AI Upscale")
        title.setObjectName("sectionTitle")
        layout.addWidget(title)
        intro = QLabel("ESRGAN models are optional for local AI Upscale. Simple Resize needs no model. "
                       "For generative upscale, choose one image in Ingest or Dataset, then use Resize to open "
                       "a separate Generate draft. It may redraw details; Generate reviews its model and runtime.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.models = QComboBox()
        for model in UPSCALERS:
            self.models.addItem(model.name, model)
        self.models.addItem("Other local ESRGAN model · Use an existing file", None)
        layout.addWidget(self.models)
        guide = QLabel("Start with x4plus for general images, Anime6B for drawn images, or compare Remacri for texture. "
                       "These are task suggestions, not a quality ranking.")
        guide.setWordWrap(True)
        layout.addWidget(guide)
        self.description = QLabel()
        self.description.setWordWrap(True)
        layout.addWidget(self.description)
        self.source = QLabel()
        self.source.setWordWrap(True)
        layout.addWidget(self.source)
        self.license_button = QPushButton("Show License and Exact Identity")
        self.license_button.setCheckable(True)
        provenance = QHBoxLayout()
        provenance.addWidget(self.license_button)
        self.source_button = QPushButton("View Model Source…")
        self.source_button.clicked.connect(self._open_source)
        provenance.addWidget(self.source_button)
        provenance.addStretch(1)
        layout.addLayout(provenance)
        self.license = QTextEdit()
        self.license.setReadOnly(True)
        self.license.setMaximumHeight(145)
        self.license.hide()
        self.license_button.toggled.connect(self.license.setVisible)
        layout.addWidget(self.license)
        self.installed = QLabel()
        self.installed.setWordWrap(True)
        layout.addWidget(self.installed)
        layout.addWidget(QLabel("Model file destination"))
        self.destination = QLineEdit()
        self.destination.setPlaceholderText("Choose an existing model folder or an already downloaded file")
        self.destination.setAccessibleName("Upscaler model file destination")
        layout.addWidget(self.destination)
        locations = QHBoxLayout()
        self.folder_button = QPushButton("Choose Download Folder…")
        self.folder_button.clicked.connect(self._choose_folder)
        self.file_button = QPushButton("Use Existing File…")
        self.file_button.clicked.connect(self._choose_file)
        locations.addWidget(self.folder_button)
        locations.addWidget(self.file_button)
        locations.addStretch(1)
        layout.addLayout(locations)
        self.review_button = QPushButton("Review Download / Verify Existing File")
        self.review_button.clicked.connect(self._review)
        layout.addWidget(self.review_button, 0, Qt.AlignLeft)
        self.review = QLabel("Choose a destination to review the download. Nothing is downloaded when this window opens.")
        self.review.setWordWrap(True)
        self.review.setTextFormat(Qt.PlainText)
        layout.addWidget(self.review)
        layout.addStretch(1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        self.cancel_button = QPushButton("Cancel Download / Check")
        self.cancel_button.clicked.connect(self._cancel)
        self.install_button = QPushButton("Download & Add to Models")
        self.install_button.setProperty("primary", True)
        self.install_button.clicked.connect(self._install)
        self.use_button = QPushButton("Use Model")
        self.use_button.setVisible(allow_select)
        self.use_button.clicked.connect(self._use)
        for button in (self.close_button, self.cancel_button, self.install_button, self.use_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self.models.currentIndexChanged.connect(self._selection_changed)
        self.destination.textChanged.connect(self._invalidate)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self._selection_changed()

    def _selection_changed(self):
        model = self.models.currentData()
        self._registered_location = ""
        self.source_button.setVisible(model is not None)
        if model is None:
            self.description.setText("Reuse a local ESRGAN 4× Safetensors file without downloading or copying it. "
                "CollectiGen checks the architecture and runtime. A filename does not prove source, quality or license. "
                "Forge/A1111 .pth models need a compatible Safetensors version; .pth loading is not supported.")
            self.source.setText("Custom model: verify its source and terms yourself.")
            self.license.setPlainText("Custom model identity is verified when you add it. No catalog recommendation or license is inferred from its filename.")
            self.installed.setText("Choose a local file to check and register in Models.")
            self.destination.clear()
            self._invalidate()
            return
        self.description.setText(f"{model.description}\nDownload: {model.size / 1_000_000:.1f} MB. Model weights are not bundled with the app.")
        self.source.setText(f'{model.attribution} · {model.license_name}')
        self.license.setPlainText(f"Revision: {model.revision}\nSHA-256: {model.sha256}\nSize: {model.size:,} bytes\n\n{model.terms}")
        try:
            snapshot, _ = self.registry.snapshot()
            record = snapshot.get("resources", {}).get("sha256:" + model.sha256, {})
            # Registry metadata only. Never probe locations on open, especially protected ones.
            paths = [str(row.get("path", "")) for row in record.get("locations", ())]
            protected = tuple(Path(root) for root in self.protected_roots)
            paths = [value for value in paths if value and not any(
                Path(value) == root or root in Path(value).parents or Path(value) in root.parents for root in protected)]
            self._registered_location = paths[0] if paths else ""
            folder = ModelWorkspace(self.registry).destination(snapshot, role="upscaler", family="ESRGAN")
            self.destination.setText(paths[0] if paths else str(Path(folder) / model.filename) if folder else "")
            self.installed.setText("Registered in Models. Verify the file and runtime before use." if paths
                                   else "Not registered in Models. Download it or add an existing copy.")
        except (OSError, RuntimeError, ValueError) as exc:
            self._invalidate()
            self.status.setText(str(exc))
            return
        self._invalidate()

    def _invalidate(self, *_):
        self._plan = None
        self._verified_path = ""
        self.selected_path = ""
        self.installed.setText("Registered in Models; file and runtime not checked yet." if self._registered_location and
            self.destination.text() == self._registered_location else "Model file not checked. Review the destination to download or verify it.")
        self.review.setText("Review this destination before downloading or adding the model.")
        self.status.clear()
        self._update_actions()

    def _update_actions(self):
        busy = self._task is not None
        for widget in (self.models, self.destination, self.folder_button, self.file_button, self.review_button):
            widget.setEnabled(not busy)
        self.folder_button.setEnabled(not busy and self.models.currentData() is not None)
        self.install_button.setEnabled(not busy and self._plan is not None)
        self.use_button.setEnabled(not busy and bool(self._verified_path))
        self.cancel_button.setVisible(busy)
        self.cancel_button.setEnabled(not self._stop.is_set())

    def _choose_folder(self):
        folder = QFileDialog.getExistingDirectory(self, "Choose Upscaler Download Folder")
        if folder:
            self.destination.setText(str(Path(folder) / self.models.currentData().filename))
            self._review()

    def _choose_file(self):
        path, _ = QFileDialog.getOpenFileName(self, "Choose Downloaded Upscaler", "", "Safetensors (*.safetensors)")
        if path:
            self.destination.setText(path)
            self._review()

    def _open_source(self):
        model = self.models.currentData()
        if model is not None:
            QDesktopServices.openUrl(QUrl(f"{model.source}/tree/{model.revision}"))

    def _review(self):
        if self._task is not None:
            return
        self._invalidate()
        try:
            self._plan = preview_upscaler(self.registry, self.models.currentData(), self.destination.text(),
                                          protected_roots=self.protected_roots)
            online = self._plan.acquisition is not None
            self.install_button.setText("Download & Add to Models" if online else "Verify & Add to Models")
            self.review.setText(
                (f"Download {self._plan.model.size / 1_000_000:.1f} MB from Hugging Face, verify its exact bytes, and register it as an ESRGAN upscaler."
                 if online else "Verify this existing file and register it as an ESRGAN upscaler. No download needed.")
                + f"\nDestination: {self._plan.destination}\nOriginal images stay unchanged. This does not run a resize.")
            if self._plan.model:
                self.review.setText(self.review.text() + f"\nLicense: {self._plan.model.license_name}. Adding this model retains its source and terms.")
        except (OSError, RuntimeError, ValueError) as exc:
            self.status.setText(str(exc))
        self._update_actions()

    def _run(self, title, work, done):
        if self._task is not None:
            return
        self._stop.clear()
        task = CoordinatedCallTask(self.jobs, title, work)
        self._task = task
        task.signals.complete.connect(lambda result: self._finish(task, done, result))
        task.signals.failed.connect(lambda message: self._finish(task, self._failure, message))
        task.signals.cancelled.connect(lambda: self._finish(task, self._failure, "Cancelled. Review the destination before retrying."))
        task.signals.progress.connect(lambda current, total, message: self.status.setText(f"{message}: {current:,} / {total:,}"))
        self.status.setText(title + "…")
        self._update_actions()
        task.start()

    def _finish(self, task, done, result):
        if self._task is not task:
            return
        self._task = None
        done(result)
        self._update_actions()

    def _failure(self, message):
        self._plan = None
        self._verified_path = ""
        self.status.setText(f"{message}\nAny downloaded file or staged attempt is retained. Review the destination to retry; Models offers staged-download recovery.")

    def _install(self):
        if self._task is not None or self._plan is None:
            return
        plan = self._plan
        self._run("Download and verify upscaler" if plan.acquisition else "Verify existing upscaler",
            lambda job: install_upscaler(self.registry, plan, confirmed=True, protected_roots=self.protected_roots,
                should_cancel=lambda: self._stop.is_set() or job.cancel_requested, progress=job.update), self._installed)

    def _installed(self, spec):
        self._plan = None
        self._verified_path = spec["path"]
        self._registered_location = spec["path"]
        self.installed.setText("Installed and registered in Models. Available offline.")
        self.review.setText(f"Verified model: {spec['path']}\nThe model is registered for the shared Choose Model picker.")
        runtime = spec["runtime"]
        next_action = "Choose Use Model to apply it." if self.allow_select else "Close this window, then choose the model in AI Upscale."
        self.status.setText(f"Ready for AI Upscale · {spec['device'].upper()}. {next_action}"
            if runtime["ready"] else "Model installed; AI runtime needs attention. Open Setup → Components and Downloads → Core application → Repair, then restart with the supported launcher.\n" + runtime["error"])

    def _use(self):
        if self._task is not None or not self._verified_path:
            return
        path, model = self._verified_path, self.models.currentData()
        self._run("Verify model before selection", lambda job: verify_upscaler(model, path,
            protected_roots=self.protected_roots, should_cancel=lambda: self._stop.is_set() or job.cancel_requested,
            progress=job.update)[1], self._accept_model)

    def _accept_model(self, spec):
        self.selected_path = spec["path"]
        self.accept()

    def _cancel(self):
        self._stop.set()
        self.status.setText("Stopping at a safe boundary. Verified files and download receipts are retained.")
        self._update_actions()

    def reject(self):
        self._stop.set()
        if self._task is not None:
            self._task.cancel()
            self._task = None
        super().reject()

    def closeEvent(self, event):
        self.reject()
        event.accept()
