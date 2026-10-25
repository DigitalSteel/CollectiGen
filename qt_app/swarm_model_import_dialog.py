"""Reviewed import of selected Swarm model metadata into one registered model."""
from __future__ import annotations

from itertools import islice
import json
import ntpath

from PySide6.QtCore import Qt, QSignalBlocker, QTimer, Signal
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QVBoxLayout

from core.model_resource_registry import ModelResourceError


MAX_RESOURCES = 64


def _plain(value, fallback="Unknown"):
    text = " ".join(str(value or "")[:4096].split())
    return text or fallback


def _name(record, path=""):
    user = record.get("user", {}) if isinstance(record, dict) else {}
    label = user.get("display_name", "") if isinstance(user, dict) else ""
    if not label:
        label = ntpath.basename(str(path).replace("/", "\\"))
    return _plain(label, "Registered model")


def _readable(value):
    return _plain(str(value).replace("_", " ").capitalize())


class SwarmModelImportDialog(QDialog):
    """Explicit, metadata-only import; opening is limited to a registry snapshot.

    The service owns Swarm inventory qualification, metadata parsing, stale
    checks, and registry writes.  This dialog only retains user choices and
    presents its attributed before/after review.
    """

    changed = Signal(object)
    imported = Signal(object)

    def __init__(self, registry, jobs, resource_ids=(), parent=None, *, service=None, service_factory=None):
        super().__init__(parent)
        identities = tuple(islice(iter(resource_ids), MAX_RESOURCES + 1))
        if len(identities) > MAX_RESOURCES or any(not isinstance(value, str) for value in identities):
            raise ModelResourceError("Choose at most 64 registered model resources.")
        if len(set(identities)) != len(identities):
            raise ModelResourceError("Each registered model can appear only once.")
        self.registry, self.jobs, self.resource_ids = registry, jobs, identities
        self.service, self.service_factory = service, service_factory
        if self.service is None and self.service_factory is None:
            raise ModelResourceError("A Swarm metadata import service is required.")
        self._snapshot = self._token = None
        self._task = self._preview = None
        self._choices = ()
        self._closing = False
        self._close_parent = None
        self.has_changed = False

        self.setWindowTitle("Import Swarm Model Metadata")
        self.setWindowModality(Qt.WindowModal)
        self.resize(1120, 780)
        body = QVBoxLayout(self)
        body.addWidget(self._label(
            "Import selected metadata from a matching Swarm model entry into one registered permanent file. "
            "This importer never reads a model file until you request metadata, and it does not establish compatibility."
        ))

        context = QHBoxLayout()
        context.addWidget(QLabel("Import scope"))
        self.category = QComboBox(); self.category.setAccessibleName("Swarm import scope")
        self.category.addItem("Metadata fields", "metadata")
        self.category.addItem("Preferences — not supported by this importer yet", "preferences")
        self.category.addItem("Presets — not supported by this importer yet", "presets")
        self.category.addItem("Favorites — not supported by this importer yet", "favorites")
        for index in range(1, self.category.count()):
            item = self.category.model().item(index)
            if item is not None: item.setFlags(item.flags() & ~Qt.ItemIsEnabled)
        context.addWidget(self.category)
        context.addWidget(QLabel("Registered model"))
        self.resource = QComboBox(); self.resource.setAccessibleName("Registered model resource")
        context.addWidget(self.resource, 1)
        body.addLayout(context)

        path_row = QHBoxLayout(); path_row.addWidget(QLabel("Permanent registered file"))
        self.path = QComboBox(); self.path.setAccessibleName("Registered permanent model file")
        path_row.addWidget(self.path, 1); body.addLayout(path_row)

        source_row = QHBoxLayout(); source_row.addWidget(QLabel("Swarm source correspondence"))
        self.source = QComboBox(); self.source.setAccessibleName("Derived Swarm source correspondence")
        source_row.addWidget(self.source, 1)
        self.find_sources = self._button("Find Matching Swarm Entries", self._find_sources)
        source_row.addWidget(self.find_sources); body.addLayout(source_row)

        self.summary = self._label("Choose a registered permanent file, then find its matching Swarm entry.")
        body.addWidget(self.summary)
        self.review = QPlainTextEdit(); self.review.setReadOnly(True)
        self.review.setAccessibleName("Attributed Swarm metadata import review")
        self.review.setPlaceholderText("Read selected metadata to review its source and exact before/after evidence.")
        body.addWidget(self.review, 1)

        review_actions = QHBoxLayout()
        self.read_metadata = self._button("Read Selected Metadata", self._read_metadata)
        self.raw_details = self._button("View Raw Metadata Details", self._show_raw_details)
        self.apply_import = self._button("Import Reviewed Metadata", self._apply)
        self.apply_import.setProperty("primary", True)
        review_actions.addWidget(self.read_metadata); review_actions.addWidget(self.raw_details)
        review_actions.addStretch(1); review_actions.addWidget(self.apply_import)
        body.addLayout(review_actions)
        body.addWidget(self._label("SwarmUI may report a preview field with metadata. This importer discards it and never fetches artwork or links."))

        self.status = self._label("Opening this review reads recorded registry choices only.")
        body.addWidget(self.status)
        footer = QHBoxLayout()
        self.reload = self._button("Refresh Registered Files", self._load)
        self.stop = self._button("Cancel Safely", self._stop)
        self.close_button = self._button("Back to Models", self.reject)
        footer.addWidget(self.reload); footer.addWidget(self.stop); footer.addStretch(1); footer.addWidget(self.close_button)
        body.addLayout(footer)
        self.resource.currentIndexChanged.connect(self._resource_changed)
        self.path.currentIndexChanged.connect(self._choices_changed)
        self.source.currentIndexChanged.connect(self._choices_changed)
        self.category.currentIndexChanged.connect(self._category_changed)
        self._update_actions()
        QTimer.singleShot(0, self._load)

    @staticmethod
    def _label(text):
        label = QLabel(text); label.setWordWrap(True); label.setTextFormat(Qt.PlainText)
        return label

    @staticmethod
    def _button(text, callback):
        button = QPushButton(text); button.clicked.connect(callback)
        return button

    def _update_actions(self):
        idle = self._task is None and not self._closing
        supported = self.category.currentData() == "metadata"
        chosen_path = self.path.currentData()
        choice = self.source.currentData()
        for widget in (self.category, self.resource, self.path, self.source, self.reload): widget.setEnabled(idle)
        self.find_sources.setEnabled(idle and supported and bool(chosen_path) and self._token is not None)
        self.read_metadata.setEnabled(idle and supported and bool(chosen_path) and choice is not None and self._token is not None)
        self.apply_import.setEnabled(idle and self._preview is not None and self._preview.action == "append")
        self.raw_details.setEnabled(idle and self._preview is not None)
        self.stop.setEnabled(self._task is not None); self.close_button.setEnabled(not self._closing)

    def _clear_review(self, message="Read selected metadata to create a new attributed review."):
        self._preview = None; self.review.clear(); self.summary.setText(message)

    def _category_changed(self, *_):
        if self.category.currentData() != "metadata":
            self._clear_review("This scope is not supported by this importer yet. Metadata fields remain available.")
        self._update_actions()

    def _choices_changed(self, *_):
        self._clear_review(); self._update_actions()

    def _resource_changed(self, *_):
        current = self.resource.currentData()
        previous_path = self.path.currentData()
        record = (self._snapshot or {}).get("resources", {}).get(current, {})
        locations = record.get("locations", ()) if isinstance(record, dict) else ()
        with QSignalBlocker(self.path), QSignalBlocker(self.source):
            self.path.clear(); self.path.addItem("Choose a registered permanent file…", None)
            for location in islice(iter(locations), 257):
                if not isinstance(location, dict) or location.get("cache_entry_id") or not location.get("path"): continue
                value = str(location["path"])
                self.path.addItem(value, value); self.path.setItemData(self.path.count() - 1, value, Qt.ToolTipRole)
            index = self.path.findData(previous_path)
            self.path.setCurrentIndex(index if index >= 0 else 0)
            self.source.clear(); self.source.addItem("Find a matching Swarm entry…", None)
        self._choices = (); self._clear_review(); self._update_actions()

    def _load(self):
        if self._task is not None: return
        selection, path, source = self.resource.currentData(), self.path.currentData(), self.source.currentData()
        self._clear_review("Reading registered choices only. No Swarm or model file was queried.")
        self._run("Reading registered model choices", lambda _job: self.registry.snapshot(),
                  lambda result: self._loaded(result, selection, path, source))

    def _loaded(self, result, selected, selected_path, selected_source=None):
        self._snapshot, self._token = result
        if self.service_factory is not None: self.service = self.service_factory(self._snapshot)
        if self.service is None: raise ModelResourceError("The Swarm metadata import service is unavailable.")
        with QSignalBlocker(self.resource):
            self.resource.clear(); self.resource.addItem("Choose a registered model…", None)
            for identity in self.resource_ids:
                record = self._snapshot.get("resources", {}).get(identity)
                if not isinstance(record, dict): continue
                locations = tuple(islice(iter(record.get("locations", ())), 257))
                permanent = next((row.get("path", "") for row in locations if isinstance(row, dict) and row.get("path") and not row.get("cache_entry_id")), "")
                if not permanent: continue
                label = _name(record, permanent)
                self.resource.addItem(label, identity); self.resource.setItemData(self.resource.count() - 1, f"{label}\n{identity}", Qt.ToolTipRole)
            index = self.resource.findData(selected)
            if selected is None and self.resource.count() == 2:
                index = 1
            self.resource.setCurrentIndex(index if index >= 0 else 0)
        self._resource_changed()
        index = self.path.findData(selected_path)
        if index >= 0: self.path.setCurrentIndex(index)
        if selected_source is not None and self.path.currentData() == selected_path:
            with QSignalBlocker(self.source):
                self.source.addItem(f"{self._choice_label(selected_source)} — recheck required", selected_source)
                self.source.setCurrentIndex(self.source.count() - 1)
            self._choices = (selected_source,)
        self.summary.setText("Recorded choices loaded. No Swarm source or model file was queried.")
        self.status.setText("Choose a registered permanent file, then explicitly find a matching Swarm entry.")

    def _choice_label(self, choice):
        return f"{_readable(choice.subtype)} · {_plain(choice.model_name, 'Unnamed Swarm model')}"

    def _find_sources(self):
        if self._task is not None or not self.path.currentData() or self._token is None or not self._policy_current(): return
        resource_id, selected_path, service, token = self.resource.currentData(), self.path.currentData(), self.service, self._token
        self._clear_review("Finding bounded derived Swarm correspondences…")
        self._run("Finding matching Swarm entries", lambda job: service.candidates(resource_id, selected_path,
            expected_registry=token, should_cancel=lambda: job.cancel_requested), self._sources_found)

    def _sources_found(self, choices):
        self._choices = tuple(islice(iter(choices), 65))
        with QSignalBlocker(self.source):
            self.source.clear(); self.source.addItem("Choose a matching Swarm entry…", None)
            for choice in self._choices:
                self.source.addItem(self._choice_label(choice), choice)
                self.source.setItemData(self.source.count() - 1, self._choice_label(choice), Qt.ToolTipRole)
        self.summary.setText("Choose the Swarm source correspondence before reading metadata. The match is derived from enrolled Swarm inventory.")
        self.status.setText("No metadata has been read yet." if self._choices else "No matching enrolled Swarm entry was found for this permanent file.")

    def _read_metadata(self):
        choice = self.source.currentData()
        if self._task is not None or choice is None or self._token is None or not self._policy_current(): return
        resource_id, selected_path, service, token = self.resource.currentData(), self.path.currentData(), self.service, self._token
        self._clear_review("Reading selected metadata for an attributed before/after review…")
        self._run("Reading selected Swarm metadata", lambda job: service.preview(resource_id, selected_path,
            subtype=choice.subtype, model_name=choice.model_name, expected_registry=token,
            should_cancel=lambda: job.cancel_requested, progress=self._progress(job)), self._preview_ready)

    @staticmethod
    def _progress(job):
        def report(done, total, label):
            units = max(1, (total + 2**20 - 1) // 2**20)
            job.update(units if done == total else done // 2**20, units, label)
        return report

    def _preview_ready(self, preview):
        self._preview = preview
        before = tuple(preview.before or ())
        proposed = preview.proposed if isinstance(preview.proposed, dict) else {}
        metadata = proposed.get("metadata", proposed)
        metadata = metadata if isinstance(metadata, dict) else {}
        fields = ("title", "author", "description", "architecture", "class", "compat_class", "license", "date",
                  "usage_hint", "trigger_phrase", "merged_from", "special_format", "tags", "standard_width", "standard_height")
        proposed_text = "\n".join(f"{_readable(key)}: {_plain(metadata[key])}" for key in fields if key in metadata)
        if not proposed_text: proposed_text = "No supported metadata fields were found."
        prior = []
        for claim in before:
            values = claim.get("metadata", {}) if isinstance(claim, dict) else {}
            if isinstance(values, dict):
                line = "; ".join(f"{_readable(key)}: {_plain(values[key])}" for key in fields if key in values)
                if line: prior.append(line)
        before_text = "\n".join(prior) or "No prior imported Swarm metadata claims."
        binding = preview.source_binding if isinstance(preview.source_binding, dict) else {}
        source = f"Server: {_plain(binding.get('server_id'))}\nEndpoint: {_plain(binding.get('endpoint'))}\nSource revision: {_plain(binding.get('source_revision'))}"
        unknown = sorted(set(metadata) - set(fields) - {"name", "local"})
        action = "Append new attributed evidence" if preview.action == "append" else "Already imported — no changes"
        self.review.setPlainText(
            f"Selected registered file: {preview.selected_path}\nSelected Swarm model: {_readable(preview.subtype)} · {_plain(preview.model_name)}\n"
            f"{source}\nRetrieved: {_plain(proposed.get('retrieved_at'))}\nSource digest: {_plain(preview.source_digest)}\n"
            f"Action: {action}\n\nBefore (retained attributed claims):\n{before_text}\n\n"
            f"Proposed attributed metadata:\n{proposed_text}\n\nUnknown metadata fields retained in raw evidence: {len(unknown)}"
        )
        if preview.action == "unchanged":
            self.summary.setText("This metadata is already recorded. No import is needed; reopening this review remains a no-op.")
            self.status.setText("No changes were made.")
        else:
            self.summary.setText("Review the attributed evidence. Import appends it without replacing user notes or declarations.")
            self.status.setText("Metadata read. Import remains an explicit final action.")

    def _apply(self):
        if self._task is not None or self._preview is None or self._preview.action != "append" or not self._policy_current(): return
        preview, service = self._preview, self.service
        self._run("Importing reviewed Swarm metadata", lambda job: service.apply(preview, confirmed=True,
            should_cancel=lambda: job.cancel_requested, progress=self._progress(job)), self._applied)

    def _applied(self, token):
        self.has_changed = True; self.changed.emit(token); self.imported.emit(token)
        self._preview = None
        self.status.setText("Metadata imported. Refreshing recorded files confirms the repeat review is a no-op.")
        self._load()

    def _show_raw_details(self):
        if self._preview is None or self._task is not None: return
        dialog = QDialog(self); dialog.setWindowTitle("Raw Selected Swarm Metadata"); dialog.resize(900, 660)
        body = QVBoxLayout(dialog)
        body.addWidget(self._label("Exact selected metadata is retained as attributed evidence. Links and artwork are not fetched here."))
        raw = QPlainTextEdit(); raw.setReadOnly(True)
        raw.setPlainText(json.dumps(self._preview.proposed, ensure_ascii=False, indent=2, sort_keys=True))
        body.addWidget(raw, 1)
        row = QHBoxLayout(); close = self._button("Close", dialog.accept); row.addStretch(1); row.addWidget(close); body.addLayout(row)
        dialog.setAttribute(Qt.WA_DeleteOnClose); dialog.show()

    def _run(self, title, function, callback):
        if self._task is not None: return
        from .workers import CoordinatedCallTask
        from .job_progress import bind_job_progress
        task = CoordinatedCallTask(self.jobs, title, function, resource="io")
        self._task = task; self.status.setText(title)
        binding = bind_job_progress(self, task, title=title, unit="MiB", cancel_button=self.stop)
        task.signals.complete.connect(lambda result: self._finish(task, callback, result))
        task.signals.failed.connect(lambda message: self._finish(task, self._failed, message))
        task.signals.cancelled.connect(lambda: self._finish(task, self._failed, "Stopped safely. Your selected resource and source correspondence remain available."))
        self.destroyed.connect(task.cancel); self._update_actions()
        try: task.start()
        except Exception as exc:
            binding.finish(task); task.cancel(); self._finish(task, self._failed, f"Could not start: {exc}. Retry when background capacity is available.")

    def _finish(self, task, callback, result):
        from shiboken6 import isValid
        if not isValid(self): task.cancel(); return
        if self._task is not task: return
        self.destroyed.disconnect(task.cancel); self._task = None
        if self._closing:
            continuation, self._close_parent = self._close_parent, None
            super().reject()
            if continuation: continuation()
            return
        try: callback(result)
        except (ValueError, RuntimeError, OSError, ModelResourceError) as exc: self._failed(str(exc))
        self._update_actions()

    def _failed(self, message):
        self._preview = None; self.status.setText(str(message))
        self.summary.setText("The current review is unavailable. Choices are retained; refresh or try the explicit action again.")
        self.review.clear()

    def _policy_current(self):
        if self.service_factory is None: return True
        try: current = self.service_factory(self._snapshot)
        except (ValueError, RuntimeError, OSError, ModelResourceError) as exc:
            self._failed(str(exc)); self._update_actions(); return False
        old_roots = tuple(getattr(getattr(self.service, "workspace", None), "protected_roots", ()))
        new_roots = tuple(getattr(getattr(current, "workspace", None), "protected_roots", ()))
        self.service = current
        if old_roots != new_roots:
            self._clear_review("Protected model-folder boundaries changed. Refresh and explicitly review this import again.")
            self.status.setText("Protected model-folder boundaries changed. No metadata was imported.")
            self._update_actions(); return False
        return True

    def _stop(self):
        if self._task is not None and self._task.job_id:
            self.jobs.cancel(self._task.job_id)
            self.status.setText("Stopping at the next safe boundary. Keep this window open until work settles.")

    def request_parent_close(self, continuation):
        pending = self._task is not None
        self._close_parent = continuation if pending else None
        self._closing = True
        self.reject(); return not pending

    def reject(self):
        if self._task is not None:
            self._closing = True; self._stop(); return
        super().reject()

    def closeEvent(self, event):
        if self._task is not None:
            self.reject(); event.ignore()
        else: super().closeEvent(event)
