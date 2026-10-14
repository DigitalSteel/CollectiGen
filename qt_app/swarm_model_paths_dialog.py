"""Explicit model-folder review and recovery for an enrolled local generator."""
from __future__ import annotations

from pathlib import Path
from dataclasses import asdict

from PySide6.QtCore import Qt, QThreadPool, Signal
from PySide6.QtWidgets import (
    QDialog, QFileDialog, QGridLayout, QHBoxLayout, QLabel, QLineEdit,
    QPlainTextEdit, QPushButton, QScrollArea, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.swarm_model_paths import SwarmModelPaths
from .components import compact_button
from .workers import AsyncCallTask


ROLE_LABELS = {
    "sdmodelfolder": "Checkpoints",
    "sdlorafolder": "LoRAs",
    "sdvaefolder": "VAEs",
    "sdembeddingfolder": "Embeddings",
    "sdcontrolnetsfolder": "ControlNets",
    "sdclipfolder": "Text Encoders",
    "sdclipvisionfolder": "Vision Encoders",
}


class SwarmModelPathsDialog(QDialog):
    """One scoped transaction; the runtime manager remains the connection owner."""

    configuration_changed = Signal()

    def __init__(self, manager, parent=None, *, service=None, model_directory="", model_registry=None,
                 hot_service=None, protected_roots=(), boundary_provider=None, jobs=None, collect_for_import=False):
        super().__init__(parent)
        self.collect_for_import = bool(collect_for_import)
        self.selected_import_choice = None
        self.setWindowTitle("SwarmUI Model Folders")
        self.setWindowModality(Qt.WindowModal)
        self.resize(980, 740)
        self.service = service or SwarmModelPaths(manager)
        self.model_directory = str(model_directory)
        self.model_registry = model_registry
        self.hot_service = hot_service
        self._injected_hot_service = hot_service
        self.protected_roots = tuple(protected_roots)
        self.boundary_provider = boundary_provider
        self.jobs = jobs
        self._cancel_enabled = False
        self._hot_boundaries = None
        self._hot_qualification = None
        self._loading_hot = False
        self._tiered = False
        self._storage_error = ""
        self._proposal_registry_token = None
        self.pool = QThreadPool.globalInstance()
        self._task = None
        self._closing = False
        self._close_parent = None
        self.inventory = None
        self.preview = None
        self.restore_mode = False
        self.recovery_state = None
        self.recovery_error = False

        layout = QVBoxLayout(self)
        note = QLabel(
            "Use your existing models in SwarmUI. Review the exact folder changes before applying. "
            "Models stay where they are; changing folders does not select or verify a model."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.status = QLabel("Read the current folders to begin.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setAccessibleName("Model folder operation status")
        layout.addWidget(self.status)
        self.root_label = QLabel("Model roots: not yet read")
        self.root_label.setTextFormat(Qt.PlainText)
        self.root_label.setWordWrap(True)
        layout.addWidget(self.root_label)

        scroll = QScrollArea()
        scroll.setWidgetResizable(True)
        content = QWidget()
        form = QGridLayout(content)
        self.fields = {}
        self.browse_buttons = {}
        for row, (field, label) in enumerate(ROLE_LABELS.items()):
            edit = QLineEdit()
            edit.setAccessibleName(f"SwarmUI {label} Folders")
            edit.textChanged.connect(self._invalidate_preview)
            browse = compact_button(QPushButton("Add Folder…"))
            browse.setAccessibleName(f"Add {label} Folder")
            browse.clicked.connect(lambda _checked=False, key=field: self._add_folder(key))
            form.addWidget(QLabel(label), row, 0)
            form.addWidget(edit, row, 1)
            form.addWidget(browse, row, 2)
            self.fields[field] = edit
            self.browse_buttons[field] = browse
        form.setColumnStretch(1, 1)
        help_text = QLabel(
            "Keep existing entries and add folders with a semicolon (;). Relative entries use "
            "the model roots above; absolute folders can be shared with other tools. "
            "SwarmUI may create missing folders and refresh its model lists. Ordinary edits keep "
            "model roots unchanged. Fast Storage uses a separate review of the root and all model folders; "
            "outputs and unrelated settings remain outside this change."
        )
        help_text.setWordWrap(True)
        form.addWidget(help_text, len(ROLE_LABELS), 0, 1, 3)
        scroll.setWidget(content)
        layout.addWidget(scroll, 2)

        self.prepared_note = QLabel("Choose completed, pinned preparations to share with SwarmUI. "
            "Unknown companion requirements remain unknown. Only current Fast Storage folders are proposed; "
            "manual folder edits are unavailable while tiered storage is enabled.")
        self.prepared_note.setWordWrap(True)
        layout.addWidget(self.prepared_note)
        self.prepared = QTreeWidget()
        self.prepared.setHeaderLabels(("Prepared models", "Resources", "Companion requirements"))
        self.prepared.setRootIsDecorated(False)
        self.prepared.setMaximumHeight(150)
        self.prepared.setAccessibleName("Prepared Fast Storage batches to share with SwarmUI")
        self.prepared.itemChanged.connect(self._invalidate_preview)
        layout.addWidget(self.prepared)
        self.prepared_note.hide()
        self.prepared.hide()

        self.recovery_label = QLabel("No saved folder operation loaded.")
        self.recovery_label.setTextFormat(Qt.PlainText)
        self.recovery_label.setWordWrap(True)
        layout.addWidget(self.recovery_label)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("Exact Model Folder Changes")
        self.details.setPlaceholderText("Review Changes shows the exact before and after values here.")
        layout.addWidget(self.details, 1)

        recovery_actions = QHBoxLayout()
        self.reload_button = compact_button(QPushButton("Read Current Folders"))
        self.reconcile_button = compact_button(QPushButton("Check Recovery"))
        self.restore_button = compact_button(QPushButton("Review Restore…"))
        self.reload_button.clicked.connect(self.load_current)
        self.propose_button = compact_button(QPushButton("Suggest CollectiGen Folders"))
        self.propose_button.clicked.connect(self.propose_collectigen_folders)
        self.reconcile_button.clicked.connect(self.check_recovery)
        self.restore_button.clicked.connect(self.review_restore)
        for button in (self.reload_button, self.propose_button, self.reconcile_button, self.restore_button):
            recovery_actions.addWidget(button)
        recovery_actions.addStretch(1)
        layout.addLayout(recovery_actions)

        actions = QHBoxLayout()
        self.close_button = compact_button(QPushButton("Close"))
        self.stop_button = compact_button(QPushButton("Stop Checks"))
        self.stop_button.clicked.connect(self._stop_checks)
        self.review_button = compact_button(QPushButton("Review Changes"))
        self.apply_button = compact_button(QPushButton("Apply Changes"), primary=True)
        self.close_button.clicked.connect(self.reject)
        self.review_button.clicked.connect(self.review_changes)
        self.apply_button.clicked.connect(self.apply_changes)
        actions.addStretch(1)
        for button in (self.stop_button, self.close_button, self.review_button, self.apply_button):
            actions.addWidget(button)
        layout.addLayout(actions)
        self._update_actions()
        if self.collect_for_import:
            self.setWindowTitle("Choose SwarmUI Folder Changes for Import")
            note.setText("Review exact existing and proposed SwarmUI model folders for the combined import plan. Adding this choice does not change SwarmUI settings.")

    def _update_actions(self):
        idle = self._task is None and not self._closing
        can_edit = idle and self.inventory is not None and not self._storage_error
        recovery_blocks_write = self.recovery_error or (
            self.recovery_state is not None
            and self.recovery_state.status in {"uncertain", "conflict"}
        )
        for edit in self.fields.values():
            edit.setEnabled(can_edit and not self._tiered)
        for button in self.browse_buttons.values():
            button.setEnabled(can_edit and not self._tiered)
        self.prepared.setEnabled(can_edit)
        self.reload_button.setEnabled(idle)
        self.propose_button.setEnabled(can_edit and not recovery_blocks_write and self.model_registry is not None)
        self.close_button.setEnabled(idle)
        self.stop_button.setVisible(not idle and self.jobs is not None)
        self.stop_button.setEnabled(not idle and self._cancel_enabled)
        self.review_button.setEnabled(can_edit and not recovery_blocks_write
            and (not self._tiered or self._hot_qualification is not None))
        self.reconcile_button.setEnabled(idle and self.recovery_state is not None)
        self.restore_button.setEnabled(
            idle and self.recovery_state is not None
            and self.recovery_state.status == "applied"
        )
        self.apply_button.setEnabled(
            idle and not recovery_blocks_write and self.preview is not None
            and bool(self.preview.changes or getattr(self.preview, "root_changed", False))
        )
        if self.collect_for_import:
            self.restore_button.hide(); self.restore_button.setEnabled(False)
            self.reconcile_button.hide(); self.reconcile_button.setEnabled(False)
            self.apply_button.setText("Add Folder Changes to Import Plan")
            self.apply_button.setEnabled(self.apply_button.isEnabled() and not self._tiered and not self.restore_mode)

    def _run(self, message, operation, completed, *, with_context=False, cancellable=False):
        if self._task is not None or self._closing:
            return
        self.status.setText(message)
        self._cancel_enabled = cancellable
        if self.jobs is not None:
            from .workers import CoordinatedCallTask
            from .job_progress import bind_job_progress
            task = CoordinatedCallTask(self.jobs, message,
                (lambda job: operation(job)) if with_context else (lambda job: operation()), resource="io")
            self._task = task
            binding = bind_job_progress(self, task, title=message, unit="MiB", cancel_button=self.stop_button)
            task.signals.complete.connect(lambda result: self._deliver(task, completed, result))
            task.signals.failed.connect(lambda message: self._deliver(task, self._failed, message))
            task.signals.cancelled.connect(lambda: self._deliver(task, self._failed,
                "A stop was requested. Check the saved folder operation before retrying; a completed external change is retained there."))
            self.destroyed.connect(task.cancel)
            self._update_actions()
            try:
                task.start()
            except Exception as exc:
                binding.finish(task)
                task.cancel()
                self._deliver(task, self._failed, "Could not start folder checks: " + str(exc))
            return
        task = AsyncCallTask(operation)
        self._task = task
        self._update_actions()
        task.signals.complete.connect(lambda result: self._finished(result, completed))
        task.signals.failed.connect(self._failed)
        self.pool.start(task)

    def _deliver(self, task, completed, result):
        from shiboken6 import isValid
        if not isValid(self):
            task.cancel()
            return
        if self._task is not task:
            return
        self.destroyed.disconnect(task.cancel)
        self._finished(result, completed)

    def _stop_checks(self):
        if self._cancel_enabled and self.jobs is not None and self._task is not None:
            self.jobs.cancel(self._task.job_id)
            self.stop_button.setEnabled(False)

    @staticmethod
    def _callbacks(job):
        if job is None:
            return None, None
        def progress(done, total, message):
            units = max(1, (total + 2**20 - 1) // 2**20) if total else 0
            job.update(units if total and done == total else done // 2**20, units, message)
        return lambda: job.cancel_requested, progress

    def _finished(self, result, completed):
        self._task = None
        try:
            completed(result)
        finally:
            self._update_actions()
            self._finish_close()

    def _failed(self, message):
        self._task = None
        self.preview = None
        self._hot_qualification = None
        self.status.setText(str(message))
        self.apply_button.setText("Apply Changes")
        self._read_recovery()
        self._update_actions()
        self._finish_close()

    def _read_recovery(self):
        try:
            self.recovery_state = self.service.recovery()
            self.recovery_error = False
            if self.recovery_state is None:
                self.recovery_label.setText("No saved folder operation. Applying changes saves the previous folder values locally.")
            else:
                state = self.recovery_state.status.replace("_", " ")
                self.recovery_label.setText(
                    f"Saved folder operation: {state}. Check Recovery reads the server without repeating a write. "
                    "Restore changes only the folders from that operation and keeps later unrelated settings."
                )
        except Exception as exc:
            self.recovery_state = None
            self.recovery_error = True
            self.recovery_label.setText(f"Recovery needs attention: {exc}")

    def load_current(self):
        self.preview = None
        self._read_recovery()
        def read():
            inventory = self.service.read()
            snapshot = self.model_registry.snapshot()[0] if self.model_registry is not None else None
            return inventory, snapshot
        def received(result):
            inventory, snapshot = result
            self._received_inventory(inventory)
            self._set_storage_snapshot(snapshot)
        self._run("Reading current SwarmUI model folders…", read, received)

    def _set_storage_snapshot(self, snapshot):
        from core.model_storage_policy import load_policy, model_storage_roots
        self._storage_error = ""
        self._tiered = False
        self.prepared.clear()
        try:
            if snapshot is not None:
                model_storage_roots(snapshot)
                self._tiered = load_policy(snapshot.get("workspace", {})).mode == "tiered"
                operations = snapshot.get("model_cache", {}).get("operations", {})
                choices = [(key, row) for key, row in operations.items()
                    if row.get("kind") == "prepare" and row.get("phase") == "complete"]
                for key, row in choices[-256:]:
                    label = "Adapter-qualified set" if row.get("qualification") == "adapter_qualified" else "Unknown — manually prepared set"
                    entries = snapshot.get("model_cache", {}).get("entries", {})
                    names = [Path(entries.get(entry, {}).get("path", "")).name
                        for entry in row.get("entry_ids", ())]
                    names = [name for name in names if name]
                    title = ", ".join(names[:2]) or "Prepared models"
                    if len(names) > 2:
                        title += f" + {len(names) - 2} more"
                    item = QTreeWidgetItem([title, str(len(row.get("entry_ids", ()))), label])
                    item.setToolTip(0, f"Preparation: {key}\nCreated: {row.get('created_at', 'Unknown')}\n" + "\n".join(names))
                    item.setData(0, Qt.UserRole, key)
                    item.setCheckState(0, Qt.Unchecked)
                    self.prepared.addTopLevelItem(item)
                self.prepared.setColumnWidth(0, 360)
                self.prepared.setColumnWidth(1, 80)
                if self._tiered:
                    self.status.setText(f"Fast Storage enabled. Choose from {min(len(choices), 256)} shown preparations, "
                        "then suggest and qualify Fast folders. Prepare missing resources in Models first.")
        except (OSError, ValueError, RuntimeError) as exc:
            self._storage_error = str(exc)
            self.status.setText("Model storage needs review before changing engine paths: " + str(exc))
        self.prepared_note.setVisible(self._tiered)
        self.prepared.setVisible(self._tiered)
        self.propose_button.setText("Suggest Fast Storage Folders" if self._tiered else "Suggest CollectiGen Folders")

    def _received_inventory(self, inventory):
        self._proposal_registry_token = None
        self._hot_qualification = None
        self.inventory = inventory
        self.preview = None
        self.restore_mode = False
        self.apply_button.setText("Apply Changes")
        self.root_label.setText(f"Model roots (unchanged): {inventory.model_root}")
        for field, edit in self.fields.items():
            edit.setText(inventory.values[field])
        self.details.clear()
        self.status.setText("Current folders loaded. Edit a folder, then review the changes.")

    def _invalidate_preview(self, *_args):
        self.preview = None
        if not self._loading_hot:
            self._hot_qualification = None
        self.restore_mode = False
        self.apply_button.setText("Apply Changes")
        self.details.clear()
        self._update_actions()

    def _add_folder(self, field):
        if not self.browse_buttons[field].isEnabled():
            return
        selected = QFileDialog.getExistingDirectory(self, f"Add {ROLE_LABELS[field]} Folder", self.model_directory)
        if not selected:
            return
        if ";" in selected:
            self.status.setText("Choose a folder without a semicolon in its name; SwarmUI uses semicolons to separate folders.")
            return
        edit = self.fields[field]
        current = edit.text()
        if selected not in current.split(";"):
            edit.setText(current + (";" if current else "") + str(Path(selected)))
            self._invalidate_preview()

    def propose_collectigen_folders(self):
        if not self.propose_button.isEnabled():return
        from core.model_engine_locations import propose_swarm_folders
        current={field:edit.text() for field,edit in self.fields.items()}
        selected = tuple(self.prepared.topLevelItem(index).data(0, Qt.UserRole)
            for index in range(self.prepared.topLevelItemCount())
            if self.prepared.topLevelItem(index).checkState(0) == Qt.Checked)
        boundaries = tuple(self.boundary_provider()) if self.boundary_provider else self.protected_roots
        inventory = self.inventory
        def propose(job=None):
            from core.model_storage_policy import load_policy
            snapshot,token=self.model_registry.snapshot()
            if load_policy(snapshot.get("workspace", {})).mode == "tiered":
                from core.model_engine_locations import project_swarm_hot_folders
                from core.model_hot_paths import ModelHotPathService
                if self.jobs is None and self._injected_hot_service is None:
                    raise ValueError("Open this handoff from Setup so model verification has shared progress and cancellation.")
                projection = project_swarm_hot_folders(snapshot, inventory, selected, registry_token=token)
                if projection.held:
                    raise ValueError(" ".join(projection.held))
                service = self._injected_hot_service or ModelHotPathService(self.model_registry,
                    protected_roots=boundaries, managed_roots=(self.model_directory,) if self.model_directory else ())
                cancel, progress = self._callbacks(job)
                qualification = service.qualify(projection, should_cancel=cancel, progress=progress)
                return projection, token, qualification, service
            return propose_swarm_folders(snapshot.get("workspace",{}),current),token,None,None
        def received(result):
            proposal,token,qualification,service=result
            self._loading_hot = True
            try:
                for field,value in proposal.values.items():self.fields[field].setText(value)
            finally:
                self._loading_hot = False
            self._proposal_registry_token=token
            self._hot_qualification = qualification
            if qualification is not None:
                self.hot_service = service
                self._hot_boundaries = boundaries
                self._tiered = True
                self.root_label.setText(f"Model root before: {proposal.before_model_root}\nProposed Fast root: {proposal.after_model_root}")
                self.details.setPlainText(proposal.review_text)
                self.status.setText("Current Fast copies qualified for the selected preparations. Review Changes before applying; "
                    "this does not prove paths used by separately configured external backends.")
                return
            self.details.setPlainText("\n".join(
                [f"{ROLE_LABELS[field]}: {folder} ({reason})" for field,folder,reason in proposal.additions]
                + list(proposal.held)))
            self.status.setText(f"Suggested {len(proposal.additions)} folder entries; existing entries retained. "
                "Review Changes before applying. " + ("Some folders need attention; see details." if proposal.held else "No models were moved or verified."))
        self._run("Reviewing CollectiGen model folders…",propose,received,with_context=True,cancellable=True)

    def _check_proposed_layout(self):
        if self.model_registry is not None:
            from core.model_storage_policy import load_policy
            snapshot, token = self.model_registry.snapshot()
            tiered = load_policy(snapshot.get("workspace", {})).mode == "tiered"
            if tiered and self._hot_qualification is None:
                raise ValueError("Tiered storage requires freshly qualified Fast folders. Read Current Folders and review a prepared set.")
            if self._hot_qualification is not None:
                if not tiered:
                    raise ValueError("The storage mode changed. Read Current Folders and review again.")
                current = tuple(self.boundary_provider()) if self.boundary_provider else self.protected_roots
                if current != self._hot_boundaries:
                    raise ValueError("Protected storage folders changed. Qualify Fast folders again.")
        if self._proposal_registry_token is not None:
            if self.model_registry.snapshot()[1]!=self._proposal_registry_token:
                raise ValueError("CollectiGen model folders or inventory changed. Suggest the folders again and review a fresh plan.")

    def review_changes(self):
        if not self.review_button.isEnabled():
            return
        values = {field: edit.text() for field, edit in self.fields.items()}
        inventory = self.inventory
        self.restore_mode = False
        self.preview = None
        def review(job=None):
            self._check_proposed_layout()
            if self._hot_qualification is not None:
                cancel, progress = self._callbacks(job)
                with self.hot_service.guard(self._hot_qualification, should_cancel=cancel, progress=progress):
                    projection = self._hot_qualification.projection
                    return self.service.preview_managed_paths(projection.values,
                        model_root=projection.after_model_root, expected=inventory)
            return self.service.preview(values, expected=inventory)
        self._run("Checking the proposed folder changes…",review,self._received_preview,with_context=True,cancellable=True)

    def _received_preview(self, preview):
        self.preview = preview
        has_changes = bool(preview.changes or getattr(preview, "root_changed", False))
        lines = []
        if getattr(preview, "root_changed", False):
            lines.extend(("Model root", f"Before: {preview.model_root}", f"After:  {preview.after_model_root}", ""))
        for field in preview.changes:
            lines.extend((ROLE_LABELS[field], f"Before: {preview.before[field]}", f"After:  {preview.after[field]}", ""))
        if self._hot_qualification is not None and not self.restore_mode:
            lines.extend((self._hot_qualification.projection.review_text, ""))
        if self.restore_mode and self._tiered:
            lines.append("Restoring earlier folders can restore archive access. Fast Storage preferences and pins remain; qualify managed Fast folders again before a managed launch.")
        self.details.setPlainText("\n".join(lines) if lines else "No folder changes.")
        self.apply_button.setText("Add Folder Changes to Import Plan" if self.collect_for_import else "Restore Folders" if self.restore_mode else "Apply Changes")
        self.status.setText(
            "Review the exact changes below. Applying saves a local recovery record, then checks the server is idle, "
            "writes only these folders and verifies the result. SwarmUI may create missing folders. "
            "Keep SwarmUI settings unchanged in other windows until this finishes."
            if has_changes else "These folders already match. Nothing will be written."
        )
        if self.collect_for_import and has_changes:
            self.status.setText("Review these exact folder changes, then add them to the import plan. "
                "This window does not change SwarmUI settings; the combined approval and fresh checks come next.")

    def review_restore(self):
        if not self.restore_button.isEnabled():
            return
        self.preview = None
        self.restore_mode = True
        self._run("Reviewing the saved model-folder restoration…", self.service.preview_restore, self._received_preview)

    def check_recovery(self):
        if not self.reconcile_button.isEnabled():
            return
        self.preview = None
        self._run("Checking the previous folder operation…", self.service.reconcile, self._reconciled)

    def _reconciled(self, result):
        self._read_recovery()
        self.status.setText(
            "No saved folder operation." if result is None else
            f"Folder operation checked: {result.status.replace('_', ' ')}. "
            "Read Current Folders to refresh the editor. A conflict must be reviewed in SwarmUI before another write."
        )

    def apply_changes(self):
        if not self.apply_button.isEnabled():
            return
        preview = self.preview
        if self.collect_for_import:
            if self.restore_mode or self._tiered: return
            self.selected_import_choice = {"mode": "single", "inventory": asdict(self.inventory),
                "values": dict(preview.after), "model_root": preview.model_root, "prepared_steps": []}
            self.accept()
            return
        restoring = self.restore_mode
        operation = self.service.restore if restoring else self.service.apply
        qualification = self._hot_qualification
        self.preview = None
        def apply(job=None):
            if not restoring:
                self._check_proposed_layout()
                if qualification is not None:
                    cancel, progress = self._callbacks(job)
                    with self.hot_service.guard(qualification, should_cancel=cancel, progress=progress):
                        if job is not None:
                            job.update(0, 0, "Applying the reviewed root and folders; verifying SwarmUI readback")
                        return operation(preview)
            return operation(preview)
        self._run("Saving and verifying SwarmUI model folders…", apply, self._applied,with_context=True)

    def _applied(self, _receipt):
        self.inventory = None
        self.preview = None
        self._read_recovery()
        self.status.setText(
            "Folder changes verified. Read Current Folders to continue editing, or close and return to your "
            "generation plan. Check its connection, exact model and preparation again before generating."
        )
        self.configuration_changed.emit()

    def reject(self):
        self._closing = True
        self._stop_checks()
        if self._task is not None:
            self.status.setText("Closing after the current folder operation reaches its safe boundary…")
        self._update_actions()
        self._finish_close()

    def has_active_background_work(self):
        return self._task is not None

    def request_background_shutdown(self):
        self.reject()

    def request_parent_close(self, continuation):
        pending = self._task is not None
        self._close_parent = continuation if pending else None
        self.reject()
        return not pending

    def _finish_close(self):
        if not self._closing or self._task is not None: return
        continuation, self._close_parent = self._close_parent, None
        super().reject()
        if continuation: continuation()

    def closeEvent(self, event):
        if self._task is not None:
            event.ignore()
            self.reject()
        else:
            super().closeEvent(event)
