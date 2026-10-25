"""Review selected Swarm presets before canonical collection publication."""
from __future__ import annotations

from itertools import islice

from PySide6.QtCore import Qt, QSignalBlocker, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)


class SwarmPresetImportDialog(QDialog):
    imported = Signal(object)
    background_work_finished = Signal()

    def __init__(self, jobs, *, service, parent=None):
        super().__init__(parent)
        self.jobs, self.service = jobs, service
        self._task = self._projection = self._review = None
        self._actions = {}
        self._closing = self._closed = False
        self._close_parent = None
        self.has_changed = False
        self.setWindowTitle("Import Prompt Presets from SwarmUI"); self.resize(1150, 800)
        self.setWindowModality(Qt.WindowModal)
        layout = QVBoxLayout(self)
        layout.addWidget(self._label("Choose source presets, review supported fields and collection conflicts, then import the selected items. Import does not apply a preset to a draft or start generation."))
        self.consent = QCheckBox("Allow the disclosed combined SwarmUI user-data read, including preset prompts")
        layout.addWidget(self.consent)
        layout.addWidget(self._label("The source API returns presets, favorites, model-preset links and account fields together. Only selected preset evidence and an opaque source account identity are retained. Unselected data is discarded; artwork and links are never fetched."))
        controls = QHBoxLayout()
        self.read_button = self._button("Read Available Presets", self.read_source)
        controls.addWidget(self.read_button); controls.addStretch(1); layout.addLayout(controls)
        self.rows = QTreeWidget(); self.rows.setColumnCount(4)
        self.rows.setHeaderLabels(("Import", "Preset", "Supported / held fields", "If already in collection"))
        self.rows.setRootIsDecorated(False); self.rows.setAccessibleName("SwarmUI preset import selection")
        self.rows.setColumnWidth(0, 65); self.rows.setColumnWidth(1, 250); self.rows.setColumnWidth(2, 450)
        layout.addWidget(self.rows, 1)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True)
        self.details.setAccessibleName("Selected preset and exact import review")
        self.details.setPlaceholderText("Read source presets only after allowing the disclosed access.")
        layout.addWidget(self.details, 1)
        self.status = self._label("No source data has been read."); layout.addWidget(self.status)
        footer = QHBoxLayout()
        self.stop = self._button("Cancel Safely", self._stop)
        self.preview_button = self._button("Review Selected Imports", self.preview_selected)
        self.apply_button = self._button("Import Reviewed Presets", self.apply_review)
        self.apply_button.setProperty("primary", True)
        self.close_button = self._button("Back to Presets", self.reject)
        footer.addWidget(self.stop); footer.addStretch(1)
        for button in (self.preview_button, self.apply_button, self.close_button): footer.addWidget(button)
        layout.addLayout(footer)
        self.consent.toggled.connect(self._scope_changed)
        self.rows.itemChanged.connect(self._invalidate)
        self.rows.currentItemChanged.connect(self._show_selected)
        self._update_actions()

    @staticmethod
    def _label(text):
        label = QLabel(text); label.setWordWrap(True); label.setTextFormat(Qt.PlainText); return label

    @staticmethod
    def _button(text, callback):
        button = QPushButton(text); button.clicked.connect(callback); return button

    def _update_actions(self):
        idle = self._task is None and not self._closing
        self.consent.setEnabled(idle); self.rows.setEnabled(idle)
        self.read_button.setEnabled(idle and self.consent.isChecked())
        self.preview_button.setEnabled(idle and self._projection is not None)
        self.apply_button.setEnabled(idle and self._review is not None and bool(self._review.get("operations")))
        self.stop.setEnabled(self._task is not None)

    def _invalidate(self, *_):
        self._review = None; self.details.clear(); self._update_actions()

    def _scope_changed(self, *_):
        self._projection = None
        with QSignalBlocker(self.rows): self.rows.clear()
        self._actions.clear(); self._invalidate()

    def read_source(self):
        if self._task is not None or not self.consent.isChecked(): return
        self._invalidate()
        self._run("Read SwarmUI preset candidates", lambda job: self.service.read(consent=True, cancelled=lambda: job.cancel_requested), self._read_ready)

    def _read_ready(self, projection):
        candidates = tuple(islice(iter(self.service.candidates(projection)), 257))
        if len(candidates) > 256: raise ValueError("The source exceeds the bounded preset review.")
        self._projection = projection; self._actions.clear()
        with QSignalBlocker(self.rows):
            self.rows.clear()
            for candidate in candidates:
                held = candidate.get("held", [])
                preset = candidate.get("preset")
                item = QTreeWidgetItem(("", candidate["title"], "Held: " + "; ".join(held) if held else "Supported prompt/scalar subset", ""))
                item.setData(0, Qt.UserRole, candidate)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Unchecked)
                if preset is None: item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
                self.rows.addTopLevelItem(item)
                action = QComboBox()
                for label, value in (("Keep local if present", "keep_local"), ("Review an update", "update"), ("Save a separate copy", "copy")):
                    action.addItem(label, value)
                action.setEnabled(preset is not None)
                action.currentIndexChanged.connect(self._invalidate)
                self.rows.setItemWidget(item, 3, action); self._actions[candidate["source_key"]] = action
        self.status.setText(f"{len(candidates)} source candidates. Choose items; existing local corrections are kept by default." if candidates else "No source presets are available. Create a new preset in the collection instead.")

    @staticmethod
    def _document(value):
        if value is None: return "No existing item."
        if hasattr(value, "to_dict"): value = value.to_dict()
        return (f"Name: {value.get('name', '')}\nPositive: {value.get('positive_prompt', '')}\nNegative: {value.get('negative_prompt', '')}\n"
            + "\n".join(f"{key}: {item}" for key, item in value.get("settings", {}).items()))

    def _show_selected(self, *_):
        if self._review is not None: return
        item = self.rows.currentItem()
        if item is None: return
        candidate = item.data(0, Qt.UserRole)
        self.details.setPlainText(self._document(candidate.get("preset")) + "\n\nHeld source fields: " + (", ".join(candidate.get("held", [])) or "None"))

    def preview_selected(self):
        if self._task is not None or self._projection is None: return
        selected, actions = [], {}
        for index in range(self.rows.topLevelItemCount()):
            item = self.rows.topLevelItem(index)
            if item.checkState(0) != Qt.Checked: continue
            row = item.data(0, Qt.UserRole)
            selected.append(row["source_key"]); actions[row["source_key"]] = self._actions[row["source_key"]].currentData()
        if not selected: self.status.setText("Select one or more source presets before reviewing."); return
        projection = self._projection; self._invalidate()
        self._run("Review selected preset imports", lambda job: self.service.preview(projection, selected, actions=actions), self._preview_ready)

    def _preview_ready(self, review):
        self._review = review
        lines = []
        for operation in review["operations"]:
            lines.append(f"{operation['title']} · {operation['action'].replace('_', ' ')}\nCURRENT COLLECTION ITEM\n"
                + self._document(operation.get("before")) + "\nAFTER IMPORT\n" + self._document(operation["after"])
                + "\nUnsupported source fields retained as evidence: " + (", ".join(operation.get("held_fields", [])) or "None"))
        lines.extend(f"Held: {row['title']} · " + "; ".join(row["reasons"]) for row in review.get("held", []))
        self.details.setPlainText("\n\n".join(lines))
        self.status.setText("Review exact collection changes. Unsupported fields remain inert and require a separate application review.")

    def apply_review(self):
        if self._task is not None or self._review is None: return
        review = self._review
        self._run("Import reviewed presets", lambda job: self.service.apply(review, confirmed=True, cancelled=lambda: job.cancel_requested), self._applied)

    def _applied(self, result):
        self._review = None
        self.has_changed = self.has_changed or any(row.get("state") == "saved" for row in result.get("outcomes", []))
        self.details.setPlainText("\n".join(f"{row['action'].replace('_', ' ').capitalize()}: {row['state']} · {row['item_id']}" for row in result.get("outcomes", [])))
        self.status.setText(result.get("message") or "Collection import reviewed. Return to the collection to open a saved preset; no draft was changed.")
        self.imported.emit(result)

    def _run(self, title, work, completed):
        if self._task is not None: return
        from .workers import CoordinatedCallTask
        from .job_progress import bind_job_progress
        returned = []
        def run(job):
            result = work(job); returned.append(result); return result
        task = CoordinatedCallTask(self.jobs, title, run, resource="io")
        self._task = task; self.status.setText(title + "…")
        binding = bind_job_progress(self, task, title=title, unit="items", cancel_button=self.stop)
        def finish(value=None, error=""):
            if self._task is not task: return
            self._task = None
            if error: self._review = None; self.status.setText(error)
            else:
                try: completed(value)
                except (ValueError, RuntimeError, OSError) as exc: self._review = None; self.status.setText(str(exc))
            if self._closing: self._finish_close()
            else: self._update_actions()
            self.background_work_finished.emit()
        task.signals.complete.connect(lambda value: finish(value))
        task.signals.failed.connect(lambda error: finish(error=error))
        task.signals.cancelled.connect(lambda: finish(returned[0]) if returned and isinstance(returned[0], dict) and "outcomes" in returned[0]
            else finish(error="Stopped safely. Open the collection to review any completed or interrupted writes."))
        self._update_actions()
        try: task.start()
        except Exception as exc: binding.finish(task); finish(error=str(exc))

    def _stop(self):
        if self._task is not None and self._task.job_id:
            self.jobs.cancel(self._task.job_id); self.status.setText("Stopping at the next safe preset boundary…")

    def has_active_background_work(self): return self._task is not None

    def request_background_shutdown(self): self.reject(); return self.has_active_background_work()

    def request_parent_close(self, continuation):
        pending = self._task is not None
        self._close_parent = continuation if pending else None
        self.reject(); return not pending

    def _finish_close(self):
        if self._closed or self._task is not None: return
        self._closed = True
        continuation, self._close_parent = self._close_parent, None
        QDialog.reject(self)
        if continuation: continuation()

    def reject(self):
        self._closing = True
        if self._task is not None: self._stop()
        else: self._finish_close()

    def closeEvent(self, event):
        if self._task is not None: self.reject(); event.ignore()
        else: super().closeEvent(event)
