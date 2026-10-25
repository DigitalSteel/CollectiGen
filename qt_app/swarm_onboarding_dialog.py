"""Category-scoped Swarm onboarding with explicit reads and reviewed writes."""
from __future__ import annotations

from dataclasses import replace
from itertools import islice
import ntpath

from PySide6.QtCore import Qt, QSignalBlocker, QTimer, Signal
from PySide6.QtWidgets import (QCheckBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout)

from core.model_resource_registry import ModelResourceError
from core.swarm_onboarding import FavoriteTarget, ImportCategories, ReadConsent, SourceFavorite, OnboardingResult


class SwarmOnboardingDialog(QDialog):
    locate_favorite_requested = Signal(object)
    registration_requested = Signal(object)
    metadata_requested = Signal(object)
    presets_requested = Signal()
    appearance_changed = Signal(str)
    changed = Signal(object)

    def __init__(self, registry, jobs, parent=None, *, service=None, service_factory=None, resource_ids=()):
        super().__init__(parent)
        self.registry, self.jobs = registry, jobs
        self.service, self.service_factory = service, service_factory
        if service is None and service_factory is None: raise ModelResourceError("An onboarding service is required.")
        self.resource_ids = tuple(islice(iter(resource_ids), 65))
        if len(self.resource_ids) > 64 or any(not isinstance(value, str) for value in self.resource_ids):
            raise ModelResourceError("Choose at most 64 registered models.")
        self._source = self._preview = self._task = self._external_child = None
        self._snapshot = None
        self._targets = {}
        self._row_choices = {}
        self._account_fingerprint = ""
        self._closing = self._closed = False
        self._close_parent = None
        self.has_changed = False
        self.setWindowTitle("Import Existing SwarmUI Setup")
        self.resize(1180, 850); self.setWindowModality(Qt.WindowModal)
        body = QVBoxLayout(self)
        body.addWidget(self._label("Reuse selected model favorites and appearance in CollectiGen. Review each change before importing; your SwarmUI setup stays unchanged."))
        scope = QHBoxLayout()
        self.favorites = QCheckBox("Model favorites"); self.appearance = QCheckBox("Appearance brightness")
        scope.addWidget(self.favorites); scope.addWidget(self.appearance); scope.addStretch(1)
        self.register = self._button("Register Model Files…", lambda: self.registration_requested.emit(self._selected_source()))
        self.metadata = self._button("Review Model Metadata…", lambda: self.metadata_requested.emit(self.resource_ids))
        scope.addWidget(self.register); scope.addWidget(self.metadata); body.addLayout(scope)
        self.presets = self._button("Import Prompt Presets…", self.presets_requested.emit)
        body.addWidget(self.presets)
        body.addWidget(self._label("Prompt presets use a separate collection review with supported and unavailable fields shown. Other preferences and reliable SwarmUI execution history are unavailable. Moving files requires a separate move review."))
        self.consent = QCheckBox("Allow the combined user-data read needed to identify the source account")
        body.addWidget(self.consent)
        body.addWidget(self._label("SwarmUI returns favorites, presets (which may contain prompts), model-preset links and account fields together. Only selected favorites and an opaque account fingerprint are kept; other returned content is discarded. This read is also needed for appearance-only import."))
        self.settings_consent = QCheckBox("Allow the user-settings read; keep only the selected theme and its light/dark classification")
        body.addWidget(self.settings_consent)
        source_actions = QHBoxLayout()
        self.read_source = self._button("Read Selected Categories", self._read)
        source_actions.addWidget(self.read_source); source_actions.addStretch(1)
        self.locate = self._button("Choose Matching Model File…", self._locate)
        source_actions.addWidget(self.locate); body.addLayout(source_actions)
        self.rows = QTreeWidget(); self.rows.setColumnCount(5)
        self.rows.setHeaderLabels(("Import", "Matching SwarmUI model", "Model file", "Current favorite", "Replace local choice"))
        self.rows.setRootIsDecorated(False); self.rows.setAccessibleName("SwarmUI favorites and exact model files")
        self.rows.setMinimumHeight(145)
        self.rows.setColumnWidth(0, 65); self.rows.setColumnWidth(1, 280); self.rows.setColumnWidth(2, 280)
        self.rows.setColumnWidth(3, 140)
        body.addWidget(self.rows, 1)
        appearance_row = QHBoxLayout()
        self.appearance_summary = self._label("Appearance has not been read.")
        self.overwrite_appearance = QCheckBox("Replace current appearance")
        appearance_row.addWidget(self.appearance_summary, 1); appearance_row.addWidget(self.overwrite_appearance)
        body.addLayout(appearance_row)
        self.review = QPlainTextEdit(); self.review.setReadOnly(True)
        self.review.setAccessibleName("Onboarding before and after review")
        self.review.setPlaceholderText("Choose categories, allow their reads, then review selected local changes.")
        body.addWidget(self.review, 1)
        self.status = self._label("Opening this window reads only recorded local state. No SwarmUI data has been requested.")
        body.addWidget(self.status)
        footer = QHBoxLayout()
        self.receipts = self._button("Review Previous Imports", self._review_receipts)
        self.stop = self._button("Cancel Safely", self._stop)
        self.preview_button = self._button("Review Selected Changes", self._make_preview)
        self.apply_button = self._button("Import Reviewed Changes", self._apply)
        self.apply_button.setProperty("primary", True)
        self.close_button = self._button("Close", self.reject)
        for widget in (self.receipts, self.stop): footer.addWidget(widget)
        footer.addStretch(1)
        for widget in (self.preview_button, self.apply_button, self.close_button): footer.addWidget(widget)
        body.addLayout(footer)
        for widget in (self.favorites, self.appearance, self.consent, self.settings_consent):
            widget.toggled.connect(self._scope_changed)
        self.overwrite_appearance.toggled.connect(self._invalidate)
        self.rows.itemChanged.connect(self._row_choice_changed)
        self.rows.currentItemChanged.connect(self._update_actions)
        self._update_actions()
        QTimer.singleShot(0, lambda: self.refresh_after_child() if self._snapshot is None else None)

    @staticmethod
    def _label(text):
        label = QLabel(text); label.setWordWrap(True); label.setTextFormat(Qt.PlainText)
        return label

    @staticmethod
    def _button(text, slot):
        button = QPushButton(text); button.clicked.connect(slot); return button

    def _idle(self): return self._task is None and self._external_child is None and not self._closing

    def _selected_source(self):
        item = self.rows.currentItem()
        return item.data(0, Qt.UserRole) if item is not None else None

    def _update_actions(self, *_):
        idle = self._idle()
        for widget in (self.favorites, self.appearance, self.consent, self.settings_consent, self.rows,
                       self.overwrite_appearance, self.register, self.receipts, self.presets): widget.setEnabled(idle)
        self.settings_consent.setEnabled(idle and self.appearance.isChecked())
        self.overwrite_appearance.setEnabled(idle and self._source is not None and self.appearance.isChecked() and bool(self._source.theme))
        consent = self.consent.isChecked() and (not self.appearance.isChecked() or self.settings_consent.isChecked())
        self.read_source.setEnabled(idle and consent and (self.favorites.isChecked() or self.appearance.isChecked()))
        selected = self._selected_source()
        self.locate.setEnabled(idle and selected is not None and not selected.held)
        self.metadata.setEnabled(idle)
        self.preview_button.setEnabled(idle and self._source is not None)
        self.apply_button.setEnabled(idle and self._preview is not None and any(op.action == "import" for op in self._preview.operations))
        self.stop.setEnabled(self._task is not None)

    def _invalidate(self, *_):
        self._preview = None
        self.review.clear(); self._update_actions()

    def _row_choice_changed(self, item, _column):
        source = item.data(0, Qt.UserRole)
        if isinstance(source, SourceFavorite):
            self._row_choices[source] = (item.checkState(0) == Qt.Checked, item.checkState(4) == Qt.Checked)
        self._invalidate()

    def _scope_changed(self, *_):
        self._source = None
        with QSignalBlocker(self.rows): self.rows.clear()
        self.appearance_summary.setText("Appearance has not been read for this scope.")
        self._invalidate()

    def _policy_current(self):
        snapshot, _ = self.registry.snapshot()
        self._snapshot = snapshot
        if self.service_factory is None: return True
        current = self.service_factory(snapshot)
        def boundary(service):
            workspace = getattr(service, "workspace", None)
            return (tuple(getattr(workspace, "protected_roots", ())), tuple(getattr(workspace, "managed_roots", ())))
        old, new = boundary(self.service), boundary(current)
        was_set = self.service is not None
        self.service = current
        if was_set and old != new:
            self._invalidate(); self.status.setText("Model folder boundaries changed. Review the selected changes again.")
            return False
        return True

    def refresh_after_child(self):
        if not self._idle(): return None
        self._invalidate()
        try:
            self._policy_current()
            self._fill_rows()
        except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc)); return False
        return True

    def _read(self):
        if not self._idle(): return
        try:
            if not self._policy_current(): return
        except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc)); return
        categories = ImportCategories(self.favorites.isChecked(), self.appearance.isChecked())
        consent = ReadConsent(self.consent.isChecked(), self.settings_consent.isChecked())
        self._invalidate()
        self._run("Reading selected SwarmUI categories", lambda job: self.service.read_categories(categories, consent,
            should_cancel=lambda: job.cancel_requested), self._read_ready)

    def _read_ready(self, source):
        if self._account_fingerprint and self._account_fingerprint != source.account_fingerprint:
            self._targets.clear(); self._row_choices.clear()
        self._account_fingerprint = source.account_fingerprint
        self._source = source
        self._targets = {key: value for key, value in self._targets.items() if key in source.favorites}
        self._row_choices = {key: value for key, value in self._row_choices.items() if key in source.favorites}
        self._fill_rows()
        self.appearance_summary.setText((source.appearance_issue or f"SwarmUI {source.theme_id} → CollectiGen {source.theme.title()}; styling differs.") if source.categories.appearance else "Appearance is outside this import scope.")
        self.status.setText("Source read complete. Choose exact model files and review any replacements before importing.")

    def _fill_rows(self):
        selected = self._selected_source()
        snapshot = self._snapshot or {"resources": {}}
        with QSignalBlocker(self.rows):
            self.rows.clear()
            for source in self._source.favorites if self._source else ():
                target = self._targets.get(source)
                record = snapshot["resources"].get(target.resource_id, {}) if target else {}
                local = record.get("user", {}).get("favorite")
                item = QTreeWidgetItem(("", f"{source.subtype} · {source.model_name}",
                    ntpath.basename(target.selected_path) if target else source.held or "Choose a registered model file", "Yes" if local is True else "No" if local is False else "Not set", ""))
                item.setData(0, Qt.UserRole, source)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                use, overwrite = self._row_choices.get(source, (bool(target), bool(target and target.overwrite)))
                item.setCheckState(0, Qt.Checked if use else Qt.Unchecked)
                item.setCheckState(4, Qt.Checked if overwrite else Qt.Unchecked)
                item.setToolTip(2, target.selected_path if target else source.held)
                self.rows.addTopLevelItem(item)
                if source == selected: self.rows.setCurrentItem(item)
        self._update_actions()

    def _locate(self):
        if self._idle() and self._selected_source() is not None:
            self.locate_favorite_requested.emit(self._selected_source())

    def accept_favorite_target(self, source_favorite, resource_id, selected_path):
        if not self._idle() or self._source is None or source_favorite not in self._source.favorites:
            return False
        if not isinstance(source_favorite, SourceFavorite) or source_favorite.held: return False
        self._targets[source_favorite] = FavoriteTarget(source_favorite, resource_id, str(selected_path))
        self._snapshot = self.registry.snapshot()[0]
        self._invalidate(); self._fill_rows()
        return True

    def _make_preview(self):
        if not self._idle() or self._source is None: return
        try:
            if not self._policy_current(): return
            targets = []
            for index in range(self.rows.topLevelItemCount()):
                item = self.rows.topLevelItem(index)
                if item.checkState(0) != Qt.Checked: continue
                source = item.data(0, Qt.UserRole)
                target = self._targets.get(source)
                if target is None: raise ModelResourceError("Choose an exact model file for each selected favorite, or clear its Import checkbox.")
                targets.append(replace(target, overwrite=item.checkState(4) == Qt.Checked))
            source = self._source
            appearance, overwrite = self.appearance.isChecked(), self.overwrite_appearance.isChecked()
            self._invalidate()
            self._run("Reviewing selected local changes", lambda job: self.service.preview(source, favorites=tuple(targets),
                appearance=appearance, overwrite_appearance=overwrite, should_cancel=lambda: job.cancel_requested,
                progress=self._progress(job)), self._preview_ready)
        except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc))

    def _preview_ready(self, preview):
        self._preview = preview
        labels = {"keep_local": "Keep local choice", "unchanged": "Already matches", "already_imported": "Already imported", "import": "Import after confirmation"}
        lines = []
        for op in preview.operations:
            name = op.target.source.model_name if op.target else "Appearance"
            before = "Not set" if op.before is None else str(op.before)
            lines.append(f"{name}\nCurrent: {before} → Proposed: {op.after}\n{labels.get(op.action, op.action)}")
        self.review.setPlainText("\n\n".join(lines))
        self.status.setText("Review the before/after changes. Unchecked replacements preserve your current choices.")

    def _apply(self):
        if not self._idle() or self._preview is None: return
        try:
            if not self._policy_current(): return
            preview = self._preview
            self._run("Importing reviewed local changes", lambda job: self.service.apply(preview, confirmed=True,
                should_cancel=lambda: job.cancel_requested, progress=self._progress(job)), self._applied)
        except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc))

    def _applied(self, result):
        self._preview = None
        self.has_changed = self.has_changed or any(row.status == "completed" for row in result.outcomes)
        self.review.setPlainText(self._outcomes(result))
        self.status.setText(result.issue or "Reviewed changes processed. Source SwarmUI settings were not changed.")
        if result.appearance: self.appearance_changed.emit(result.appearance)
        self.changed.emit(result)

    @staticmethod
    def _outcomes(result):
        return "\n".join(f"{row.category.title()} · {row.destination}\n{row.status.replace('_', ' ').capitalize()}" for row in result.outcomes)

    def _review_receipts(self):
        if not self._idle(): return
        try:
            if not self._policy_current(): return
            self._invalidate()
            self._run("Reviewing previous import outcomes", lambda job: self.service.review_receipts(),
                lambda results: self.review.setPlainText("\n\n".join(self._outcomes(result) for result in results) or "No previous onboarding imports."))
        except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc))

    @staticmethod
    def _progress(job):
        return lambda done, total, label: job.update(done // (2**20), max(1, (total + 2**20 - 1) // (2**20)), label)

    def _run(self, title, function, callback):
        if not self._idle(): return
        from .workers import CoordinatedCallTask
        from .job_progress import bind_job_progress
        completed_write = []
        def run(job):
            result = function(job)
            if isinstance(result, OnboardingResult): completed_write.append(result)
            return result
        task = CoordinatedCallTask(self.jobs, title, run, resource="io")
        self._task = task; self.status.setText(title)
        binding = bind_job_progress(self, task, title=title, unit="MiB", cancel_button=self.stop)
        task.signals.complete.connect(lambda result: self._finish(task, callback, result))
        task.signals.failed.connect(lambda message: self._finish(task, self._failed, message))
        task.signals.cancelled.connect(lambda: self._finish(task, callback, completed_write[0]) if completed_write
            else self._finish(task, self._failed, "Stopped safely. Review previous imports before retrying remaining changes."))
        self.destroyed.connect(task.cancel); self._update_actions()
        try: task.start()
        except Exception:
            binding.finish(task); task.cancel(); self._finish(task, self._failed, "Could not start background work. Try again when capacity is available.")

    def _finish(self, task, callback, result):
        from shiboken6 import isValid
        if not isValid(self): task.cancel(); return
        if self._task is not task: return
        self.destroyed.disconnect(task.cancel); self._task = None
        try: callback(result)
        except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc))
        if self._closing: self._finish_close()
        else: self._update_actions()

    def _failed(self, message):
        self._invalidate(); self.status.setText(str(message))

    def _stop(self):
        if self._task is not None and self._task.job_id:
            self.jobs.cancel(self._task.job_id); self.status.setText("Stopping at the next safe boundary…")

    def has_active_background_work(self): return self._task is not None or self._external_child is not None

    def request_background_shutdown(self): self.reject()

    def track_external_child(self, dialog):
        if not self._idle(): raise ModelResourceError("Finish the active onboarding action first.")
        self._external_child = dialog
        dialog.finished.connect(lambda _result: self._external_finished(dialog)); self._update_actions()

    def _external_finished(self, dialog):
        if self._external_child is not dialog: return
        self._external_child = None
        if self._closing: self._finish_close()
        else: self._update_actions()

    def request_parent_close(self, continuation):
        pending = self.has_active_background_work()
        self._close_parent = continuation if pending else None
        self.reject(); return not pending

    def _finish_close(self):
        if self._closed or self.has_active_background_work(): return
        self._closed = True
        continuation, self._close_parent = self._close_parent, None
        super().reject()
        if continuation: continuation()

    def reject(self):
        self._closing = True
        if self._external_child is not None:
            child = self._external_child
            close = getattr(child, "request_parent_close", None)
            if callable(close):
                if close(lambda: self._external_finished(child)): self._external_finished(child)
            else: child.reject()
        self._stop(); self._finish_close()

    def closeEvent(self, event):
        if self.has_active_background_work(): self.reject(); event.ignore()
        else: super().closeEvent(event)
