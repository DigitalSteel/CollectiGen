"""Explicit physical organization of a bounded selection of registered models."""
from __future__ import annotations

from pathlib import Path
from itertools import islice

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton,
    QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.model_relocation import (
    COMPANIONS_NOTICE, ENGINE_NOTICE, MAX_OPERATIONS, MAX_SELECTION,
    ModelRelocationService, RelocationSelection,
)
from core.model_resource_registry import ModelResourceError

RECOVERY_LABELS = {
    "prepared": "Source retained; incomplete copy",
    "verified_staged": "Verified copy ready",
    "admitted": "Destination registered",
    "retiring_source": "Finishing source removal",
    "source_retired": "Source removed; finishing registry update",
    "complete": "Complete", "abandoned": "Abandoned; source retained",
}


class ModelRelocationDialog(QDialog):
    """Task-local preview/apply/recovery; browsing and folder policy stay shared.

    The caller supplies exact registered identities (at most MAX_SELECTION).
    No constructor filesystem work, discovery, automatic source selection among
    copies, inferred declarations, or engine configuration writes occur here.
    """

    def __init__(self, registry, jobs, resource_ids=(), parent=None, *, service=None,
                 protected_roots=(), managed_roots=(), recorded_engine_roots=(),
                 setup_folders=None, service_factory=None):
        super().__init__(parent)
        identities = tuple(islice(iter(resource_ids), MAX_SELECTION + 1))
        if len(identities) > MAX_SELECTION or any(not isinstance(identity, str) for identity in identities) or len(set(identities)) != len(identities):
            raise ModelResourceError(f"Choose at most {MAX_SELECTION} distinct registered models.")
        self.registry, self.jobs = registry, jobs
        self.service = service or ModelRelocationService(registry, protected_roots=protected_roots,
            managed_roots=managed_roots, recorded_engine_roots=recorded_engine_roots)
        self.resource_ids = identities
        self.setup_folders = setup_folders
        self.service_factory = service_factory
        self._policy_snapshot = None
        self._task = None
        self._closing = False
        self._preview = self._recovery = None
        self._token = None
        self._sources = {}
        self.outcomes = ()
        self.changed = False
        self.setWindowTitle("Organize Model Files")
        self.setWindowModality(Qt.WindowModal)
        self.resize(1380, 880)
        body = QVBoxLayout(self)
        intro = QLabel("Choose the exact registered copies to organize. Preview uses your saved Folder Locations; only checked ready files move after you apply the review.")
        intro.setWordWrap(True); body.addWidget(intro)
        self.notices = QLabel(COMPANIONS_NOTICE + "\n" + ENGINE_NOTICE)
        self.notices.setWordWrap(True); self.notices.setTextFormat(Qt.PlainText)
        body.addWidget(self.notices)
        self.tabs = QTabWidget(); body.addWidget(self.tabs, 1)

        choices = QWidget(); choice_body = QVBoxLayout(choices)
        choice_help = QLabel("Check each model and choose its source, recorded Model Type / Family, and Images or Video. Multiple locations remain separate copies.")
        choice_help.setWordWrap(True); choice_body.addWidget(choice_help)
        self.files = QTreeWidget(); self.files.setRootIsDecorated(False)
        self.files.setHeaderLabels(["Model", "Source copy", "Recorded Model Type / Family", "Media"])
        self.files.setColumnWidth(0, 250); self.files.setColumnWidth(1, 530); self.files.setColumnWidth(2, 260)
        self.files.itemChanged.connect(self._choices_changed)
        choice_body.addWidget(self.files, 1)
        choice_actions = QHBoxLayout()
        self.check_files = QPushButton("Check Listed Models")
        self.check_files.clicked.connect(self._check_files)
        self.preview_button = QPushButton("Preview Checked Files")
        self.preview_button.clicked.connect(self._preview_files)
        choice_actions.addWidget(self.check_files); choice_actions.addStretch(1)
        choice_actions.addWidget(self.preview_button); choice_body.addLayout(choice_actions)
        self.tabs.addTab(choices, "Choose Files")

        review = QWidget(); review_body = QVBoxLayout(review)
        self.review_summary = QLabel("No move preview. Choose files first.")
        self.review_summary.setWordWrap(True); review_body.addWidget(self.review_summary)
        self.rows = QTreeWidget(); self.rows.setRootIsDecorated(False)
        self.rows.setHeaderLabels(["Source", "Destination", "State / result"])
        self.rows.setColumnWidth(0, 430); self.rows.setColumnWidth(1, 430)
        self.rows.itemSelectionChanged.connect(self._row_details)
        self.rows.itemChanged.connect(lambda *_: self._update_actions())
        review_body.addWidget(self.rows, 1)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True); self.details.setMaximumHeight(180)
        self.details.setAccessibleName("Selected source destination and move consequences")
        review_body.addWidget(self.details)
        review_actions = QHBoxLayout()
        self.check_ready = QPushButton("Check Ready Files"); self.check_ready.clicked.connect(self._check_ready)
        self.apply_button = QPushButton("Move Checked Files"); self.apply_button.setProperty("primary", True)
        self.apply_button.clicked.connect(self._apply)
        self.result_recovery = QPushButton("Review Recovery for Selected Result")
        self.result_recovery.clicked.connect(self._review_result_recovery)
        review_actions.addWidget(self.check_ready); review_actions.addWidget(self.result_recovery); review_actions.addStretch(1)
        review_actions.addWidget(self.apply_button); review_body.addLayout(review_actions)
        self.tabs.addTab(review, "Move Review")

        recovery = QWidget(); recovery_body = QVBoxLayout(recovery)
        recovery_help = QLabel("Interrupted operations retain their original source and destination. Review an exact operation before recovery; a changed folder layout does not retarget it.")
        recovery_help.setWordWrap(True); recovery_body.addWidget(recovery_help)
        self.operations = QComboBox(); self.operations.setAccessibleName("Recorded model move operation")
        self.operations.currentIndexChanged.connect(self._clear_recovery)
        recovery_body.addWidget(self.operations)
        self.recovery_details = QPlainTextEdit(); self.recovery_details.setReadOnly(True)
        self.recovery_details.setAccessibleName("Exact recorded relocation recovery preview")
        recovery_body.addWidget(self.recovery_details, 1)
        recovery_actions = QHBoxLayout()
        self.inspect_recovery = QPushButton("Review Selected Recovery")
        self.inspect_recovery.clicked.connect(self._preview_recovery)
        self.recover_button = QPushButton("Apply Reviewed Recovery")
        self.recover_button.clicked.connect(self._recover)
        recovery_actions.addWidget(self.inspect_recovery); recovery_actions.addStretch(1)
        recovery_actions.addWidget(self.recover_button); recovery_body.addLayout(recovery_actions)
        self.tabs.addTab(recovery, "Recovery")

        self.status = QLabel("Loading the exact registered selection…")
        self.status.setWordWrap(True); self.status.setTextFormat(Qt.PlainText); body.addWidget(self.status)
        actions = QHBoxLayout()
        self.reload = QPushButton("Reload Recorded State"); self.reload.clicked.connect(self._load)
        self.setup = QPushButton("Edit Folder Locations…"); self.setup.clicked.connect(self._setup)
        self.stop = QPushButton("Cancel Safely"); self.stop.clicked.connect(self._stop)
        self.close_button = QPushButton("Back to Models"); self.close_button.clicked.connect(self.reject)
        for widget in (self.reload, self.setup, self.stop): actions.addWidget(widget)
        actions.addStretch(1); actions.addWidget(self.close_button); body.addLayout(actions)
        self._update_actions(); QTimer.singleShot(0, self._load)

    def _update_actions(self):
        if not hasattr(self, "close_button"): return
        idle = self._task is None
        for control in (self.files, self.rows, self.operations, self.reload, self.check_files):
            control.setEnabled(idle)
        self.setup.setEnabled(idle and self.setup_folders is not None)
        checked = [identity for identity, (item, *_rest) in self._sources.items() if item.checkState(0) == Qt.Checked]
        self.preview_button.setEnabled(idle and bool(checked) and self._token is not None)
        selected = self._checked_rows()
        self.apply_button.setText(f"Move {len(selected):,} Checked Files" if selected else "Move Checked Files")
        self.apply_button.setVisible(self._preview is not None)
        self.apply_button.setEnabled(idle and self._preview is not None and bool(selected))
        self.check_ready.setVisible(self._preview is not None)
        self.check_ready.setEnabled(idle and self._preview is not None)
        all_complete = bool(self.outcomes) and all(outcome.status == "complete" for outcome in self.outcomes)
        self.close_button.setText("Done — Back to Models" if all_complete else "Back to Models")
        result_item = self.rows.currentItem()
        result = result_item.data(0, Qt.UserRole) if result_item else None
        needs_recovery = bool(result and getattr(result, "operation_id", "") and result.status not in {"complete", "abandoned"})
        self.result_recovery.setVisible(needs_recovery)
        self.result_recovery.setEnabled(idle and needs_recovery)
        self.inspect_recovery.setEnabled(idle and bool(self.operations.currentData()))
        self.recover_button.setEnabled(idle and self._recovery is not None and self._recovery.status not in {"complete", "abandoned"})
        self.stop.setVisible(not idle)

    def _run(self, title, function, complete):
        if self._task is not None or self._closing: return
        from .workers import CoordinatedCallTask
        from .job_progress import bind_job_progress
        task = CoordinatedCallTask(self.jobs, title, function, resource="io")
        self._task = task; self.status.setText(title)
        binding = bind_job_progress(self, task, title=title, unit="MiB", cancel_button=self.stop)
        task.signals.complete.connect(lambda result: self._finish(task, complete, result))
        task.signals.failed.connect(lambda message: self._finish(task, self._failed, message))
        task.signals.cancelled.connect(lambda: self._finish(task, self._failed,
            "Stopped at a safe boundary. Some files may have completed. Reload Recorded State and review Recovery before another move."))
        self.destroyed.connect(task.cancel)
        self._update_actions()
        try: task.start()
        except Exception as exc:
            binding.finish(task); task.cancel()
            self._finish(task, self._failed, f"Could not start: {exc}. Retry when background capacity is available.")

    def _finish(self, task, complete, result):
        from shiboken6 import isValid
        if not isValid(self): task.cancel(); return
        if self._task is not task: return
        self.destroyed.disconnect(task.cancel); self._task = None
        if self._closing: super().reject(); return
        try: complete(result)
        except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc))
        self._update_actions()

    @staticmethod
    def _progress(job):
        def report(done, total, label):
            units = max(1, (total + 2**20 - 1) // 2**20)
            job.update(units if done == total else done // 2**20, units, label)
        return report

    def _failed(self, message):
        self._preview = self._recovery = None
        self.status.setText(str(message))

    def _load(self):
        if self._task is not None: return
        self._preview = self._recovery = None
        self._run("Reading recorded model locations", lambda _job: self.registry.snapshot(), self._loaded)

    def _loaded(self, result):
        snapshot, self._token = result
        self._policy_snapshot = snapshot
        if self.service_factory is not None: self.service = self.service_factory(snapshot)
        self._preview = self._recovery = None
        previous = {identity: (item.checkState(0), source.currentData(), declaration.currentData(), media.currentData())
            for identity, (item, source, declaration, media) in self._sources.items()}
        self._sources.clear(); self.files.clear()
        for identity in self.resource_ids:
            record = snapshot["resources"].get(identity)
            if record is None: continue
            locations, declarations = record.get("locations", ()), record.get("declarations", ())
            if len(locations) > 256 or len(declarations) > 100:
                raise ModelResourceError("A selected model exceeds the bounded location/declaration review. Narrow its recorded copies first.")
            filename = Path(locations[0]["path"]).name if locations else identity
            title = str(record.get("user", {}).get("display_name") or filename)
            item = QTreeWidgetItem([title, "", "", ""])
            item.setToolTip(0, identity); item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Unchecked); self.files.addTopLevelItem(item)
            source, declaration, media = QComboBox(), QComboBox(), QComboBox()
            source.addItem("Choose exact source copy…", None)
            for location in locations:
                source.addItem(str(location["path"]), str(location["path"]))
                source.setItemData(source.count() - 1, str(location["path"]), Qt.ToolTipRole)
            declaration.addItem("Choose recorded Model Type / Family…", None)
            for entry in declarations:
                declaration.addItem(f"{entry['role']} / {entry['family']}", (entry["role"], entry["family"]))
            media.addItem("Choose media…", None)
            for name in ("Images", "Video"): media.addItem(name, name)
            if identity in previous:
                checked, *values = previous[identity]
                item.setCheckState(0, checked)
                for widget, value in zip((source, declaration, media), values):
                    widget.setCurrentIndex(next((index for index in range(widget.count()) if widget.itemData(index) == value), 0))
            for column, widget in enumerate((source, declaration, media), 1):
                self.files.setItemWidget(item, column, widget)
                widget.currentIndexChanged.connect(self._choices_changed)
            self._sources[identity] = item, source, declaration, media
        operations = snapshot.get("model_relocations", {})
        if not isinstance(operations, dict) or len(operations) > MAX_OPERATIONS:
            raise ModelResourceError("Relocation history is invalid or exceeds its supported bound; preserve it for review.")
        self.operations.clear(); self.operations.addItem("Choose recorded operation…", "")
        for operation, marker in reversed(tuple(operations.items())):
            if not isinstance(marker, dict): continue
            self.operations.addItem(f"{RECOVERY_LABELS.get(marker.get('phase'), 'Unknown state')} · {operation}", operation)
        self.status.setText(f"{len(self._sources):,} selected registered models; {len(operations):,} recorded operations. Nothing has been moved by this read.")

    def _choices_changed(self, *_):
        if self._preview is not None:
            self._preview = None
            self.review_summary.setText("File choices changed. Preview Checked Files again before moving anything.")
        self._update_actions()

    def _check_files(self):
        if self._task is None:
            for item, *_rest in self._sources.values(): item.setCheckState(0, Qt.Checked)

    def _preview_files(self):
        if self._task is not None: return
        if not self._policy_current(): return
        selections = []
        for identity, (item, source, declaration, media) in self._sources.items():
            if item.checkState(0) != Qt.Checked: continue
            if not source.currentData() or not declaration.currentData() or not media.currentData():
                self._failed("Choose an exact source copy, recorded Model Type / Family and Images or Video for every checked model.")
                self._update_actions(); return
            role, family = declaration.currentData()
            selections.append(RelocationSelection(identity, Path(source.currentData()), role, family, media.currentData()))
        if not selections: return
        token = self._token
        self._preview = self._recovery = None
        self._run("Previewing model file destinations", lambda job: self.service.preview(tuple(selections),
            expected_registry=token, should_cancel=lambda: job.cancel_requested, progress=self._progress(job)), self._previewed)

    def _previewed(self, preview):
        self._preview = preview; self.outcomes = ()
        self.rows.clear(); self.details.clear()
        for row in preview.rows:
            item = QTreeWidgetItem([str(row.selection.source), str(row.destination or "Unavailable"), f"{row.status}: {row.reason}"])
            item.setData(0, Qt.UserRole, row)
            if row.status == "ready":
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable); item.setCheckState(0, Qt.Unchecked)
            for column in range(3): item.setToolTip(column, item.text(column))
            self.rows.addTopLevelItem(item)
        ready = sum(row.status == "ready" for row in preview.rows)
        self.review_summary.setText(f"{len(preview.rows):,} reviewed · {ready:,} ready. Check the exact moves to apply. Originals at those source locations retire only after verified destination publication.")
        self.tabs.setCurrentIndex(1)
        self.status.setText("Preview only. No model files moved. Review destinations, holds and sidecar/engine notices.")
        if self.rows.topLevelItemCount(): self.rows.setCurrentItem(self.rows.topLevelItem(0))

    def _row_details(self):
        item = self.rows.currentItem()
        if item is None: return
        row = item.data(0, Qt.UserRole)
        if row is None: return
        if hasattr(row, "selection"):
            selection = row.selection
            self.details.setPlainText(f"Source: {selection.source}\nDestination: {row.destination or 'Unavailable'}\n"
                f"Model Type / Family: {selection.role} / {selection.family} · {selection.media_type}\n"
                f"Destination rule: {row.destination_reason}\n{row.status}: {row.reason}\n{row.companions_notice}\n{row.engine_notice}")
        else:
            self.details.setPlainText(f"Source: {row.source}\nDestination: {row.destination}\n{row.status}: {row.message}\nRecovery operation: {row.operation_id or 'Not started'}")
        self._update_actions()

    def _review_result_recovery(self):
        item = self.rows.currentItem()
        result = item.data(0, Qt.UserRole) if item else None
        if self._task is not None or not result or not getattr(result, "operation_id", ""): return
        index = self.operations.findData(result.operation_id)
        if index < 0:
            self.status.setText("Reload Recorded State to find this recovery operation."); return
        self.operations.setCurrentIndex(index); self.tabs.setCurrentIndex(2)
        self._preview_recovery()

    def _checked_rows(self):
        return tuple(self.rows.topLevelItem(index).data(0, Qt.UserRole).row_id
            for index in range(self.rows.topLevelItemCount())
            if self.rows.topLevelItem(index).checkState(0) == Qt.Checked)

    def _check_ready(self):
        if self._task is not None or self._preview is None: return
        for index in range(self.rows.topLevelItemCount()):
            item = self.rows.topLevelItem(index)
            if item.data(0, Qt.UserRole).status == "ready": item.setCheckState(0, Qt.Checked)

    def _apply(self):
        if self._task is not None or self._preview is None: return
        if not self._policy_current(): return
        selected, preview = self._checked_rows(), self._preview
        if not selected: return
        self._preview = None
        self.changed = True  # A cancelled result may still have published some files.
        self._run("Moving reviewed model files", lambda job: (self.service.apply(preview, selected, confirmed=True,
            should_cancel=lambda: job.cancel_requested, progress=self._progress(job)), self.registry.snapshot()), self._applied)

    def _applied(self, result):
        outcomes, snapshot = result
        self._loaded(snapshot); self.outcomes = tuple(outcomes)
        self.rows.clear(); self.details.clear()
        for outcome in outcomes:
            item = QTreeWidgetItem([str(outcome.source), str(outcome.destination or "Unavailable"), f"{outcome.status}: {outcome.message}"])
            item.setData(0, Qt.UserRole, outcome)
            for column in range(3): item.setToolTip(column, item.text(column))
            self.rows.addTopLevelItem(item)
        complete = sum(outcome.status == "complete" for outcome in outcomes)
        if complete == len(outcomes) and outcomes:
            self.review_summary.setText(
                f"All {complete:,} selected model {'file' if complete == 1 else 'files'} moved. "
                "Review the saved results, then choose Done — Back to Models."
            )
        else:
            self.review_summary.setText(
                f"{complete:,} complete of {len(outcomes):,} selected files. Review each result; held or interrupted work may need Recovery."
            )
        self.status.setText("Recorded state reloaded. Other source copies and engine folder settings remain unchanged.")
        self.tabs.setCurrentIndex(1)
        if self.rows.topLevelItemCount(): self.rows.setCurrentItem(self.rows.topLevelItem(0))

    def _clear_recovery(self, *_):
        self._recovery = None
        self.recovery_details.clear(); self._update_actions()

    def _preview_recovery(self):
        operation = self.operations.currentData()
        if self._task is not None or not operation: return
        if not self._policy_current(): return
        self._preview = self._recovery = None
        self._run("Reviewing exact move recovery", lambda job: self.service.preview_recovery(operation,
            should_cancel=lambda: job.cancel_requested, progress=self._progress(job)), self._recovery_previewed)

    def _recovery_previewed(self, preview):
        self._recovery = preview
        self.recovery_details.setPlainText(f"Operation: {preview.operation_id}\nState: {RECOVERY_LABELS.get(preview.status, 'Unknown state')}\n"
            f"Original source: {preview.source}\nOriginal destination: {preview.destination}\n{preview.message}\n"
            + ("Folder layout changed: this action keeps the original destination above.\n" if preview.layout_changed else "")
            + COMPANIONS_NOTICE + "\n" + ENGINE_NOTICE)
        self.status.setText("Recovery preview only. Apply this reviewed recovery to reconcile the exact recorded operation.")

    def _recover(self):
        if self._task is not None or self._recovery is None: return
        if not self._policy_current(): return
        preview, self._recovery = self._recovery, None
        self.changed = True
        self._run("Recovering reviewed model move", lambda job: ((self.service.recover(preview, confirmed=True,
            should_cancel=lambda: job.cancel_requested, progress=self._progress(job)),), self.registry.snapshot()), self._applied)

    def _setup(self):
        if self._task is not None or self.setup_folders is None: return
        self._preview = self._recovery = None
        try: self.setup_folders()
        except (ValueError, RuntimeError, OSError) as exc:
            self._failed(str(exc)); self._update_actions(); return
        self._load()

    def _policy_current(self):
        if self.service_factory is None: return True
        try: current = self.service_factory(self._policy_snapshot)
        except (ValueError, RuntimeError, OSError) as exc:
            self._failed(str(exc)); self._update_actions(); return False
        keys = ("protected_roots", "managed_roots", "recorded_engine_roots")
        if any(getattr(current, key) != getattr(self.service, key) for key in keys):
            self.service = current
            self._failed("Protected or managed folder boundaries changed. Review the files or recovery again before applying anything.")
            self._update_actions(); return False
        return True

    def request_parent_close(self, continuation):
        self._parent_closing = True
        pending = self._task is not None
        self._close_parent = continuation if pending else None
        self.reject()
        return not pending

    def _stop(self):
        if self._task is not None and self._task.job_id:
            self.jobs.cancel(self._task.job_id)
            self.status.setText("Stopping at the next safe boundary. Keep this window open until work settles.")

    def reject(self):
        if self._task is not None:
            self._closing = True; self._stop()
            return
        super().reject()

    def closeEvent(self, event):
        if self._task is not None:
            self.reject(); event.ignore()
        else: super().closeEvent(event)
