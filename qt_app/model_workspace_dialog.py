"""One private Models workspace using existing registry and coordinated jobs."""
from __future__ import annotations

import json
from collections import Counter
from copy import deepcopy
from dataclasses import replace, asdict
from pathlib import Path
from types import SimpleNamespace

from PySide6.QtCore import Qt, QUrl, QTimer, QSignalBlocker
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QFileDialog, QFormLayout, QGroupBox, QHBoxLayout, QLabel,
    QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QTabWidget, QVBoxLayout, QWidget,
    QDialog, QDialogButtonBox, QTreeWidget, QTreeWidgetItem, QSplitter, QSizePolicy,
)

from core.model_resource_registry import RESOURCE_ROLES, ModelResourceError
from core.model_workspace import ModelWorkspace, ModelLineageIndex, IndexDeclaration, preview_acquisition, acquire_model
from .command_bar import DialogCommandController, UiCommand, bind_source
from .model_resource_dialog import ModelResourceDialog
from .components import ContextHelpButton, DisclosureButton, EmptyStatePanel
from .help_content import section_help


class SelectedModelFilesDialog(QDialog):
    """Review the complete checked batch without materializing 20k Qt rows."""

    PAGE_SIZE = 100

    def __init__(self, root, selected, declarations, *, collect_for_import=False, update_import_choice=False,
                 checked_matching=0, checked_on_page=0, held_count=0, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review Files for Import Plan" if collect_for_import else "Review Model Files to Register")
        self.setWindowModality(Qt.WindowModal)
        self.resize(1050, 690)
        self._root = root
        self._selected = selected
        self._declarations = declarations
        self._page = 0
        body = QVBoxLayout(self)
        self.folder = QLabel(f"Selected folder: {root}")
        self.folder.setWordWrap(True); self.folder.setTextFormat(Qt.PlainText)
        body.addWidget(self.folder)
        effect = ("Updating this choice does not register or move files; the combined import plan needs separate review and approval."
                  if update_import_choice else
                  "Adding these choices does not register or move files; the combined import plan needs separate review and approval."
                  if collect_for_import else "Registration records exact file identities and declarations; source files stay where they are.")
        self.summary = QLabel(
            f"{len(selected):,} checked files · {len({item.resource_id for item in selected}):,} distinct model identities. "
            f"{checked_matching:,} checked match the current filters; {len(selected) - checked_matching:,} are outside them. "
            f"{checked_on_page:,} checked are on the current page. "
            f"{held_count:,} held {'file is' if held_count == 1 else 'files are'} excluded.\n{effect}")
        self.summary.setWordWrap(True); self.summary.setTextFormat(Qt.PlainText)
        body.addWidget(self.summary)
        kinds = Counter((row.role, row.family) for row in declarations)
        self.type_summary = QLabel("Selected declarations: " + "; ".join(
            f"{role} / {family}: {count:,}" for (role, family), count in sorted(kinds.items()))
            if len(kinds) <= 12 else f"{len(kinds):,} type/family combinations. Inspect the paged file list below.")
        self.type_summary.setWordWrap(True); body.addWidget(self.type_summary)
        self.files = QTreeWidget(); self.files.setRootIsDecorated(False)
        self.files.setHeaderLabels(("Checked file", "Model type", "Model family"))
        self.files.setColumnWidth(0, 690); self.files.setColumnWidth(1, 125)
        self.files.setColumnWidth(2, 160)
        self.files.setAccessibleName("All checked model files in this registration scope")
        body.addWidget(self.files, 1)
        self.details = QLabel("Select a file to see its full path and exact identity.")
        self.details.setWordWrap(True); self.details.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.files.currentItemChanged.connect(lambda row, _previous: self.details.setText(
            f"Full path: {row.toolTip(0)}\nExact identity: {row.data(0, Qt.UserRole)}" if row else
            "Select a file to see its full path and exact identity."))
        body.addWidget(self.details)
        paging = QHBoxLayout()
        self.previous = QPushButton("Previous Files")
        self.next = QPushButton("Next Files")
        self.count = QLabel()
        self.previous.clicked.connect(lambda: self._change_page(-1))
        self.next.clicked.connect(lambda: self._change_page(1))
        for widget in (self.previous, self.next, self.count): paging.addWidget(widget)
        paging.addStretch(1); body.addLayout(paging)
        actions = QHBoxLayout()
        self.cancel_button = QPushButton("Back to File Review")
        self.cancel_button.clicked.connect(self.reject)
        self.confirm_button = QPushButton(
            f"Update Choice with {len(selected):,} Reviewed Files" if update_import_choice else
            f"Add {len(selected):,} Files to Import Plan" if collect_for_import else f"Register {len(selected):,} Reviewed Files")
        self.confirm_button.setProperty("primary", True)
        self.confirm_button.setAutoDefault(False)
        self.confirm_button.clicked.connect(self.accept)
        actions.addStretch(1); actions.addWidget(self.cancel_button); actions.addWidget(self.confirm_button)
        body.addLayout(actions)
        self._render()

    def _change_page(self, delta):
        self._page = max(0, self._page + delta)
        self._render()

    def _render(self):
        start = self._page * self.PAGE_SIZE
        self.files.clear()
        for item, declaration in zip(self._selected[start:start + self.PAGE_SIZE],
                                     self._declarations[start:start + self.PAGE_SIZE]):
            name = str(item.path.relative_to(self._root))
            row = QTreeWidgetItem((name, declaration.role, declaration.family))
            row.setToolTip(0, str(item.path))
            row.setData(0, Qt.UserRole, item.resource_id)
            self.files.addTopLevelItem(row)
        total = len(self._selected)
        self.count.setText(f"Files {start + 1:,}–{min(start + self.PAGE_SIZE, total):,} of {total:,}")
        self.previous.setEnabled(self._page > 0)
        self.next.setEnabled(start + self.PAGE_SIZE < total)


def _management_browser_rows(snapshot, token):
    """Aggregate canonical read-only presentation by resource, never declaration."""
    from core.model_selection import ModelSelectionService
    from core.model_artwork import project_artwork
    projected, _ = ModelSelectionService(SimpleNamespace(snapshot=lambda: (snapshot, token))).project()
    grouped = {}
    for row in projected: grouped.setdefault(row.resource_id, []).append(row)
    result = {}
    for identity, record in snapshot["resources"].items():
        rows = grouped.get(identity, ())
        declarations = tuple(sorted({(str(d.get("role", "")), str(d.get("family", "")))
                                     for d in record["declarations"]}))
        roles = sorted({role for role, _ in declarations}); families = sorted({family for _, family in declarations})
        locations = tuple(str(value["path"]) for value in record["locations"])
        artwork, artwork_status = project_artwork(record, identity)
        user = record.get("user", {})
        values = asdict(rows[0]) if rows else dict(resource_id=identity,
            display_name=str(user.get("display_name", "") or (Path(locations[0]).name if locations else identity)), locations=locations,
            favorite=user.get("favorite") is True, last_used_at="", cached_status="stored" if locations else "no_location")
        values.update(browser_key=("models", identity), role=roles[0] if len(roles) == 1 else "Multiple types" if roles else "Unknown type",
            family=families[0] if len(families) == 1 else "Multiple families" if families else "Unknown family",
            declaration_summary="; ".join(f"{role} / {family}" for role, family in declarations) or "No recorded declaration",
            search_text="\n".join(row.search_text for row in rows), artwork=artwork, artwork_status=artwork_status)
        result[identity] = SimpleNamespace(**values)
    return result


class KnownModelFoldersDialog(QDialog):
    """Review recorded paths; only an explicit selection hands off to indexing."""

    def __init__(self, owner, *, preloaded_swarm=False):
        super().__init__(owner)
        from core.model_folder_discovery import review_known_model_folders
        self.owner = owner
        self._snapshot = owner._settings_snapshot
        self._token = owner._snapshot_token
        if preloaded_swarm and owner._swarm_folder_reader is None:
            raise ValueError("Read the enrolled SwarmUI source before choosing its folders.")
        self._inventory = deepcopy(owner._read_known_swarm_folders()) if preloaded_swarm else None
        self._task = None
        self._closing = False
        self._page = 0
        self.review = review_known_model_folders(
            {} if preloaded_swarm else self._snapshot, swarm_inventory=self._inventory,
            protected_roots=owner.workspace.protected_roots,
        )
        self.setWindowTitle("Review Known Model Folders")
        self.setWindowModality(Qt.WindowModal)
        self.resize(1000, 700)
        body = QVBoxLayout(self)
        intro = QLabel("Review saved Setup and previously indexed folders. These are recorded preferences, not verified contents. Nothing is scanned until you preview one selected folder; registration stays separate.")
        intro.setWordWrap(True); body.addWidget(intro)
        self.read_swarm = QPushButton("Read Connected SwarmUI Folders")
        self.read_swarm.setToolTip("Explicitly read model-folder settings from the enrolled local server. No files are listed and no server setting is changed.")
        self.read_swarm.clicked.connect(self._read_swarm)
        self.read_swarm.setVisible(not preloaded_swarm)
        body.addWidget(self.read_swarm, 0, Qt.AlignLeft)
        self.folders = QTreeWidget(); self.folders.setRootIsDecorated(False)
        self.folders.setHeaderLabels(["Recorded folder", "Sources / state"])
        self.folders.setColumnWidth(0, 590)
        self.folders.itemSelectionChanged.connect(self._selection_changed)
        body.addWidget(self.folders, 1)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True); self.details.setMaximumHeight(160)
        self.details.setAccessibleName("Recorded model folder provenance")
        body.addWidget(self.details)
        paging = QHBoxLayout()
        self.previous = QPushButton("Previous"); self.following = QPushButton("Next"); self.count = QLabel()
        self.previous.clicked.connect(lambda: self._change_page(-1))
        self.following.clicked.connect(lambda: self._change_page(1))
        for widget in (self.previous, self.following, self.count): paging.addWidget(widget)
        paging.addStretch(1); body.addLayout(paging)
        self.status = QLabel(
            "Recorded SwarmUI folders are ready to choose; no model files were scanned."
            if preloaded_swarm else "No folders or model files have been inspected."
        )
        self.status.setTextFormat(Qt.PlainText); self.status.setWordWrap(True); body.addWidget(self.status)
        recovery = QHBoxLayout()
        self.reload = QPushButton("Reload Models")
        self.reload.clicked.connect(lambda: self._return_to(owner._load))
        self.setup = QPushButton("Setup Folders…")
        self.setup.clicked.connect(lambda: self._return_to(owner._choose_destination))
        self.browse = QPushButton("Browse Another Folder…")
        self.browse.clicked.connect(self._browse)
        for widget in (self.reload, self.setup, self.browse): recovery.addWidget(widget)
        recovery.addStretch(1); body.addLayout(recovery)
        actions = QHBoxLayout()
        self.stop = QPushButton("Stop Read"); self.stop.clicked.connect(self._stop)
        self.preview = QPushButton("Preview Selected Folder")
        self.preview.setProperty("primary", True); self.preview.clicked.connect(self._preview_selected)
        close = QPushButton("Back to Models"); close.clicked.connect(self.reject)
        actions.addWidget(self.stop); actions.addStretch(1); actions.addWidget(self.preview); actions.addWidget(close)
        body.addLayout(actions)
        self._controls = (self.read_swarm, self.folders, self.reload, self.setup, self.browse)
        self._render()

    def _render(self):
        from core.model_folder_discovery import PAGE_SIZE
        total = len(self.review.candidates)
        self._page = min(self._page, max(0, (total - 1) // PAGE_SIZE))
        self.folders.clear()
        for row in self.review.page(self._page):
            item = QTreeWidgetItem([row.path, f"{len(row.origins)} source(s) · " + ("Needs review" if row.issue else "Not checked")])
            item.setData(0, Qt.UserRole, row); item.setToolTip(0, row.path)
            self.folders.addTopLevelItem(item)
        self.count.setText(f"{total} recorded folders · Page {self._page + 1}")
        self.details.setPlainText("Select one recorded folder. " + " ".join(self.review.warnings) if total else
            "No model folders are recorded. Use Setup Folders, Browse Another Folder, or explicitly read the connected SwarmUI settings.")
        self._update_actions()

    def _change_page(self, delta):
        self._page = max(0, self._page + delta); self._render()

    def _selection_changed(self):
        item = self.folders.currentItem()
        if item: self.details.setPlainText(item.data(0, Qt.UserRole).detail() + "\n" + "\n".join(self.review.warnings))
        self._update_actions()

    def _update_actions(self):
        if not hasattr(self, "_controls"): return
        from core.model_folder_discovery import PAGE_SIZE
        idle = self._task is None
        for widget in self._controls: widget.setEnabled(idle)
        self.stop.setVisible(not idle)
        self.previous.setEnabled(idle and self._page > 0)
        self.following.setEnabled(idle and (self._page + 1) * PAGE_SIZE < len(self.review.candidates))
        item = self.folders.currentItem()
        self.preview.setEnabled(idle and item is not None and item.data(0, Qt.UserRole).actionable)

    def _run(self, name, function, complete):
        if self._task is not None: return
        from .workers import CoordinatedCallTask
        task = CoordinatedCallTask(self.owner.jobs, name, function, resource="io")
        self._task = task; self.status.setText(name); self._update_actions()
        task.signals.complete.connect(lambda result: self._finish(task, complete, result))
        task.signals.failed.connect(lambda error: self._finish(task, None, error))
        task.signals.cancelled.connect(lambda: self._finish(task, None, "Read stopped. No preview or registration was started; retry when ready."))
        self.destroyed.connect(task.cancel)
        try:
            task.start()
        except Exception as exc:
            task.cancel()
            self.destroyed.disconnect(task.cancel)
            self._task = None
            self.status.setText(f"Could not start the read: {exc}. Retry when background work is available.")
            self._update_actions()

    def _finish(self, task, complete, result):
        from shiboken6 import isValid
        if not isValid(self) or not isValid(self.owner):
            task.cancel(); return
        if self._task is not task: return
        self.destroyed.disconnect(task.cancel)
        self._task = None
        if self._closing or not self.owner.isVisible():
            super().reject(); return
        if complete: complete(result)
        else: self.status.setText(str(result))
        self._update_actions()

    def _read_swarm(self):
        from core.model_folder_discovery import review_known_model_folders
        def read(job):
            if job.cancel_requested: raise InterruptedError()
            inventory = deepcopy(self.owner._read_known_swarm_folders())
            if job.cancel_requested: raise InterruptedError()
            review = review_known_model_folders(self._snapshot, swarm_inventory=inventory,
                protected_roots=self.owner.workspace.protected_roots)
            return inventory, review
        def loaded(result):
            self._inventory, self.review = result; self._page = 0; self._render()
            self.status.setText("Connected folder settings read. No folders scanned; unsupported expressions require an explicit folder choice.")
        self._run("Reading enrolled SwarmUI folder settings…", read, loaded)

    def _preview_selected(self):
        item = self.folders.currentItem()
        if item is None: return
        row = item.data(0, Qt.UserRole)
        if not row.actionable: return
        expected = deepcopy(self._inventory)
        def check(job):
            if job.cancel_requested: raise InterruptedError()
            if self.owner.registry.snapshot()[1] != self._token:
                raise ValueError("Saved model folders changed. Use Reload Models, then review the folders again.")
            if row.uses_swarm:
                current = self.owner._read_known_swarm_folders()
                if current != expected:
                    raise ValueError("Connected SwarmUI folders or enrollment changed. Read Connected SwarmUI Folders again before previewing.")
            if job.cancel_requested: raise InterruptedError()
            return row.path
        self._run("Checking the selected folder's recorded source…", check,
            lambda path: self._return_to(lambda: self.owner._preview_known_folder(path)))

    def _browse(self):
        path = QFileDialog.getExistingDirectory(self, "Choose One Model Folder to Preview")
        if path: self._return_to(lambda: self.owner._preview_known_folder(path))

    def _return_to(self, action):
        if self._task is not None: return
        super().accept(); action()

    def _stop(self):
        if self._task is not None: self.owner.jobs.cancel(self._task.job_id)

    def reject(self):
        if self._task is not None:
            self._closing = True
            task, self._task = self._task, None
            task.cancel()  # detach late results; the read has no write effects
            self.destroyed.disconnect(task.cancel)
        super().reject()

    def closeEvent(self, event):
        self.reject(); event.accept()


class ExactModelCopyDialog(QDialog):
    """One selected identity, one explicit copy, one guarded location update."""

    def __init__(self, owner, resource_id, revision, protected_roots=(), *, parent=None,
                 back_label="Back to Model Selection"):
        super().__init__(parent if parent is not None else owner)
        self.owner, self.jobs = owner, owner.jobs
        self.workspace = ModelWorkspace(owner.registry, protected_roots=protected_roots)
        self.resource_id, self.revision = resource_id, revision
        self._task = None; self._closing = False; self._close_parent = None; self._parent_closing = False
        self._preview = None; self.attached = False
        self.setWindowTitle("Locate Exact Model Copy"); self.resize(880, 560)
        self.setWindowModality(Qt.WindowModal)
        body = QVBoxLayout(self)
        intro = QLabel("Choose one .safetensors file to recover this registered model's location. Its full identity must match before you can add it. Existing locations and model knowledge stay intact; nothing is moved or loaded.")
        intro.setWordWrap(True); body.addWidget(intro)
        identity = QLabel(resource_id); identity.setTextFormat(Qt.PlainText)
        identity.setTextInteractionFlags(Qt.TextSelectableByMouse); body.addWidget(identity)
        self.choose = QPushButton("Choose Exact Copy…"); self.choose.clicked.connect(self._choose)
        body.addWidget(self.choose, 0, Qt.AlignLeft)
        self.details = QPlainTextEdit("No file selected. Choosing a file starts cancellable inspection, not registration.")
        self.details.setReadOnly(True); body.addWidget(self.details, 1)
        self.status = QLabel(); self.status.setWordWrap(True); self.status.setTextFormat(Qt.PlainText)
        body.addWidget(self.status)
        actions = QHBoxLayout()
        self.stop = QPushButton("Stop Work"); self.stop.clicked.connect(self._stop)
        self.recover = QPushButton("Recover Registry"); self.recover.clicked.connect(self._recover)
        self.recover.hide()
        self.apply = QPushButton("Add Verified Location"); self.apply.setProperty("primary", True)
        self.apply.clicked.connect(self._apply)
        self.back = QPushButton(back_label); self.back.clicked.connect(self.reject)
        actions.addWidget(self.stop); actions.addWidget(self.recover); actions.addStretch(1)
        actions.addWidget(self.apply); actions.addWidget(self.back); body.addLayout(actions)
        self._update_actions()

    def _choose(self):
        # Start beside the selected model's recorded copy when its folder still
        # exists. A moved file can then be chosen by name without navigating
        # from the application's working directory.
        initial_directory = ""
        try:
            snapshot, _revision = self.owner.registry.snapshot()
            locations = snapshot["resources"].get(self.resource_id, {}).get("locations", ())
            for location in locations:
                folder = Path(location["path"]).parent
                if folder.is_dir():
                    initial_directory = str(folder)
                    break
        except (KeyError, ModelResourceError, OSError, TypeError, ValueError):
            pass
        filename, _ = QFileDialog.getOpenFileName(self, "Choose an Exact Copy of the Registered Model", initial_directory, "Safe tensor model (*.safetensors)")
        if filename: self.preview_path(filename)

    def preview_path(self, path):
        if self._task is not None: return
        self._preview = None
        self.details.setPlainText(f"Selected file: {path}\nInspection has not yet established an identity match.")
        def inspect(job):
            return self.workspace.preview_exact_location(self.resource_id, path, expected_registry=self.revision,
                should_cancel=lambda: job.cancel_requested, progress=ModelResourceDialog._progress(job))
        def ready(preview):
            self._preview = preview
            self.details.setPlainText(f"Exact identity verified: {preview.resource_id}\nCopy: {preview.inspection.path}\n"
                f"Size: {preview.inspection.size:,} bytes\n\nAdd Verified Location changes only this existing record's locations. "
                "Other locations remain recorded. Declarations, notes and lineage are preserved. This does not establish adapter compatibility or start a model.")
            self.status.setText("Review the exact copy, then add its location. Bytes and registry revision will be checked again.")
        self._run("Verifying the selected copy…", inspect, ready)

    def _apply(self):
        if self._preview is None or self._task is not None: return
        preview = self._preview; self._preview = None
        def apply(job):
            return self.workspace.attach_exact_location(preview, should_cancel=lambda: job.cancel_requested,
                progress=ModelResourceDialog._progress(job))
        def saved(_):
            self.attached = True
            self.status.setText("Exact location recorded. Use the normal Verify or Use action to recheck the model before use.")
            if not self._closing: self.accept()
        self._run("Adding the verified location…", apply, saved)

    def _run(self, label, function, complete):
        from core.model_resource_registry import ResourceRecoveryRequired
        from .workers import CoordinatedCallTask
        def run(job):
            try:
                return True, function(job), False
            except InterruptedError:
                raise
            except Exception as exc:
                pending = isinstance(exc, ResourceRecoveryRequired)
                if not pending:
                    try: self.workspace.registry.snapshot()
                    except ResourceRecoveryRequired: pending = True
                    except Exception: pass
                return False, str(exc), pending
        task = CoordinatedCallTask(self.jobs, label, run, resource="io")
        self._task = task; self.status.setText(label); self._update_actions()
        task.signals.complete.connect(lambda result: self._finish(task, complete, result))
        task.signals.failed.connect(lambda message: self._finish(task, complete, (False, message, False)))
        task.signals.cancelled.connect(lambda: self._finish(task, complete, (False,
            "Stopped at a safe boundary. Close to refresh the registry before retrying; a completed publication remains recorded.", False)))
        task.signals.progress.connect(lambda current, total, phase: self._progress(task, current, total, phase))
        self.destroyed.connect(task.cancel)
        try: task.start()
        except Exception as exc:
            task.cancel(); self.destroyed.disconnect(task.cancel); self._task = None
            self.status.setText(f"Could not start work: {exc}. Retry when background work is available.")
            self._update_actions()

    def _finish(self, task, complete, result):
        from shiboken6 import isValid
        if not isValid(self) or not isValid(self.owner): task.cancel(); return
        if self._task is not task: return
        self.destroyed.disconnect(task.cancel); self._task = None
        success, value, recovery = result
        if success: complete(value)
        else:
            self._preview = None
            message = str(value)
            if not recovery:
                try:
                    stale_revision = self.workspace.registry.snapshot()[1] != self.revision
                except Exception:
                    stale_revision = False
                if stale_revision:
                    message += (f" Use {self.back.text()}, then reopen Locate Exact Copy to review the "
                                "current registry and choose this file again. No location was added here.")
            self.status.setText(message); self.recover.setVisible(recovery)
        if self._closing: super().reject()
        else: self._update_actions()

    def _progress(self, task, current, total, phase):
        from shiboken6 import isValid
        if isValid(self) and self._task is task:
            self.status.setText(f"{phase} · {current:,}/{total:,} MiB")

    def _recover(self):
        if self._task is not None: return
        self._preview = None
        def recover(job):
            if job.cancel_requested: raise InterruptedError()
            outcome = self.workspace.registry.recover()
            _, token = self.workspace.registry.snapshot()
            return outcome, token
        def recovered(result):
            outcome, self.revision = result; self.recover.hide()
            self.status.setText(f"Registry recovery: {outcome}. Choose the exact copy again to obtain a fresh preview.")
        self._run("Reconciling retained registry publication…", recover, recovered)

    def _update_actions(self):
        idle = self._task is None
        self.choose.setEnabled(idle and not self._closing)
        self.apply.setEnabled(idle and self._preview is not None and not self._closing)
        self.recover.setEnabled(idle and not self._closing); self.stop.setVisible(not idle)

    def _stop(self):
        if self._task is not None: self.jobs.cancel(self._task.job_id)

    def request_parent_close(self, continuation):
        pending = self._task is not None
        self._parent_closing = True
        self._close_parent = continuation if pending else None
        self.reject()
        return not pending

    def reject(self):
        if self._task is not None:
            self._closing = True; self._stop()
            self.status.setText("Stopping after the safe file or registry publication boundary…")
            self._update_actions(); return
        super().reject()

    def closeEvent(self, event):
        if self._task is not None: event.ignore(); self.reject()
        else: super().closeEvent(event)


class ModelWorkspaceDialog(ModelResourceDialog):
    feedback_workflow = "Models and provider evidence"

    def __init__(self, registry, jobs, parent=None, *, protected_roots=(), settings=None, select_plan=False, goal="", goal_kind="character", target_id="", current_configuration=None, image_count=0, initial_folder_picker=False, initial_role="", initial_metadata_resource="", swarm_folder_reader=None, initial_known_swarm_folders=False, initial_companion_handoff=None, collect_for_import=False, initial_import_review=None, return_to_selector=False):
        self.collect_for_import = bool(collect_for_import)
        if initial_import_review is not None and not self.collect_for_import:
            raise ValueError("Import-choice file review belongs to the combined import collector.")
        self._initial_import_review = deepcopy(initial_import_review)
        self.selected_import_choice = None
        from core.model_companion_readiness import CompanionAcquisitionHandoff
        if initial_companion_handoff is not None and not isinstance(initial_companion_handoff, CompanionAcquisitionHandoff):
            raise ValueError("Choose a reviewed companion acquisition request.")
        self._companion_handoff = initial_companion_handoff
        self.selected_companion_choice = None
        self.workspace = ModelWorkspace(registry, protected_roots=protected_roots)
        self._swarm_folder_reader = swarm_folder_reader
        self._initial_known_swarm_folders = bool(initial_known_swarm_folders)
        if self._initial_known_swarm_folders and (not collect_for_import or swarm_folder_reader is None):
            raise ValueError("Read the enrolled SwarmUI source before choosing its folders.")
        self._known_folder_context = None
        self._initial_metadata_resource = initial_metadata_resource
        self._selector_resource_id = initial_metadata_resource
        self._initial_folder_picker = bool(initial_folder_picker)
        self.registration_applied = False
        self.selected_for_selector_id = ""
        self._return_to_selector = bool(return_to_selector)
        self._records = {}
        self._lineage_index = None
        self._lineage_page = 0
        self._related_identity = ""
        self._lineage_history = []
        self._note_identity = ""
        self._note_drafts = {}
        self._note_baseline = ("", "")
        self._note_token = ""
        self._registry_signature = None
        self._index_preview = None
        self._folder_scan_root = None
        self._folder_scan_pending = False
        self._folder_scan_recovery = False
        self._folder_assignments = {}
        self._folder_checked = set()
        self._folder_page = 0
        self._review_resume = None
        self._scan_review_draft = None
        self._registration_recheck_draft = None
        self._single_file_register_pending = False
        self._single_file_review_path = None
        self._single_file_browser_open = False
        self._single_file_recheck_path = None
        self._single_file_recheck_identity = ""
        self._single_file_prior_identity = ""
        self._scan_review_path = None
        self._scan_review_digest = None
        self._review_requires_check = False
        self._folder_rendering = False
        self._detections = {}
        self._folder_ids = {}
        self._registered_group = ()
        self._registration_followup_dismissed = False
        self._fast_recovery_count = 0
        self._provider_result = None
        self.settings = settings
        self.select_plan = select_plan
        self.guidance_goal = goal
        self.guidance_goal_kind = goal_kind
        self.guidance_target_id = target_id
        self.guidance_configuration = current_configuration
        self.image_count = image_count
        self.selected_guidance = None
        self.selected_guidance_configuration = None
        self.selected_generation_configuration = None
        self.generation_handoff = None
        self._generation_handoff_ready = False
        self._planning_browser_open = False
        self._extras = []
        self._management_rows = {}
        self._browser_query = None
        self._browser_rendering = False
        self._registry_loaded = False
        self._start_on_first_load = not any((self.collect_for_import, select_plan, initial_folder_picker,
            initial_role, initial_metadata_resource, initial_known_swarm_folders, initial_companion_handoff,
            goal, target_id))
        super().__init__(registry, jobs, "", parent)
        if initial_metadata_resource:
            self.search.setText(initial_metadata_resource)
        self.setWindowTitle("Models")
        self.resize(1050, 850)
        layout = self.layout()
        # The dialog ribbon supplies its title.  Keep the explanatory context,
        # rather than a second workspace heading, in the local collection pane.
        layout.itemAt(0).widget().hide()
        self.model_context = layout.itemAt(1).widget()
        self.model_context.setText("Collect exact file or download choices for the combined import review. Adding choices does not register, download or move models."
            if self.collect_for_import else
            "Add existing model files or preview a folder, then register the models you want to use. Registration keeps files in place; Storage offers reviewed organization and recovery.")
        self.models.setHeaderLabels(["Model", "Model Type / Family", "Locations"])
        self.registration_followup = QWidget()
        followup_layout = QVBoxLayout(self.registration_followup)
        followup_layout.setContentsMargins(0, 0, 0, 0)
        self.registration_followup_label = QLabel()
        self.registration_followup_label.setWordWrap(True)
        followup_layout.addWidget(self.registration_followup_label)
        followup_actions = QHBoxLayout()
        self.registration_setup_button = QPushButton("Set Folder Locations for Organization…")
        self.registration_setup_button.clicked.connect(self._choose_destination)
        self.registration_organize_button = QPushButton("Review Organization for Registered Models…")
        self.registration_organize_button.clicked.connect(
            lambda: self._open_relocation(self._registered_group))
        followup_actions.addWidget(self.registration_setup_button)
        followup_actions.addWidget(self.registration_organize_button)
        followup_actions.addStretch(1)
        followup_layout.addLayout(followup_actions)
        layout.insertWidget(layout.indexOf(self.count) + 1, self.registration_followup)
        self.registration_followup.hide()
        self.return_to_selector_button = QPushButton("Return Selected Model to Picker")
        self.return_to_selector_button.setProperty("primary", True)
        self.return_to_selector_button.setToolTip("Highlight this exact registered model in the originating picker. You will still choose its declaration and Use Model there.")
        self.return_to_selector_button.clicked.connect(self._return_selected_to_selector)
        footer = next(box for box in self.findChildren(QDialogButtonBox) if box.button(QDialogButtonBox.Cancel) is not None)
        footer.addButton(self.return_to_selector_button, QDialogButtonBox.ActionRole)
        self.return_to_selector_button.setVisible(self._return_to_selector)
        self.models.setMinimumHeight(100)
        self.search.setPlaceholderText("Search models by name, type, family, tag, note, source or full hash")
        search_index = layout.indexOf(self.search)
        layout.removeWidget(self.search)
        search_row = QHBoxLayout()
        search_row.addWidget(self.search, 1)
        self.help_button = ContextHelpButton("Models", section_help("models"), self)
        self.help_button.setShortcut("F1")
        search_row.addWidget(self.help_button)
        self.selector_button = QPushButton("Find by Type / Favorites…")
        self.selector_button.clicked.connect(self._open_selector)
        search_row.addWidget(self.selector_button)
        self.artwork_button = QPushButton("Preview Artwork…")
        self.artwork_button.clicked.connect(self._open_artwork)
        search_row.addWidget(self.artwork_button)
        self.artwork_button.setVisible(not self.collect_for_import)
        self.upscaler_models_button = QPushButton("Get Upscaler Models…")
        self.upscaler_models_button.clicked.connect(self._open_upscaler_models)
        self.upscaler_models_button.setToolTip(
            "Review optional ESRGAN upscaler packs. Opening the catalog does not download a model."
        )
        search_row.addWidget(self.upscaler_models_button)
        layout.insertLayout(search_index, search_row)
        self.role = QComboBox()
        self.role.addItem("Choose Model Type…")
        self.role.addItems(RESOURCE_ROLES)
        self.role.setToolTip("Model type is what the file does: checkpoint, LoRA, VAE, text encoder, and so on. This is your declaration, not a compatibility check.")
        if initial_role in RESOURCE_ROLES:
            self.role.setCurrentText(initial_role)
        if initial_folder_picker:
            self.family.clear()
            self.family.setPlaceholderText("Declare the family shared by the previewed files")
        local_controls = layout.itemAt(2).layout()
        self._local_declaration_row = local_controls
        self.family_label = local_controls.itemAt(1).widget()
        self.role_label = QLabel("Model Type (what it does)")
        local_controls.addWidget(self.role_label); local_controls.addWidget(self.role)
        self.index_button = QPushButton("Scan Model Folder…")
        self.index_button.clicked.connect(self._choose_folder)
        self.reopen_review_button = QPushButton("Reopen File Review…")
        self.reopen_review_button.clicked.connect(self._choose_scan_review)
        self.known_folders_button = QPushButton("Review Known Folders…")
        self.known_folders_button.clicked.connect(self._open_known_folders)
        self.import_plan_button = QPushButton("Import Models from SwarmUI…")
        self.import_plan_button.clicked.connect(self._open_import_plan)
        self.locate_copy_button = QPushButton("Locate Exact Copy…")
        self.locate_copy_button.clicked.connect(self._locate_exact_copy)
        self.apply_index_button = QPushButton("Register Selected Files")
        self.apply_index_button.setProperty("primary", True)
        self.apply_index_button.clicked.connect(self._apply_index)
        self.cancel_work_button = QPushButton("Stop Work")
        self.cancel_work_button.setProperty("compact", True)
        self.cancel_work_button.clicked.connect(self._cancel_work)
        layout.insertWidget(layout.indexOf(self.status), self.cancel_work_button, 0, Qt.AlignLeft)
        self.continue_generation_button = QPushButton("Continue Reviewed Setup in Generate")
        self.continue_generation_button.setProperty("primary", True)
        self.continue_generation_button.clicked.connect(self._continue_reviewed_generation)
        layout.insertWidget(layout.indexOf(self.status), self.continue_generation_button, 0, Qt.AlignLeft)
        self.continue_generation_button.hide()
        self.guidance_button = QPushButton("Plan for a Model…")
        self.guidance_button.clicked.connect(self._open_guidance)
        self.generation_button = QPushButton("Recommend for Image Generation…")
        self.generation_button.clicked.connect(self._open_generation_plan)
        self.required_resources_button = QPushButton("Required Resources…")
        self.required_resources_button.clicked.connect(self._open_required_resources)
        self.find_metadata_button = QPushButton("Find Metadata on Civitai…")
        self.find_metadata_button.clicked.connect(self._prefill_model_metadata)
        self.import_swarm_metadata_button = QPushButton("Import SwarmUI Metadata…")
        self.import_swarm_metadata_button.clicked.connect(self._open_swarm_metadata_import)
        self.swarm_onboarding_button = QPushButton("Import Existing SwarmUI Setup…")
        self.swarm_onboarding_button.clicked.connect(self._open_swarm_onboarding)
        self.tabs = QTabWidget()
        layout.insertWidget(layout.count() - 3, self.tabs, 1)
        notes = QWidget(); notes_layout = QVBoxLayout(notes); form = QFormLayout()
        self.tags = QLineEdit(); self.tags.setMaxLength(16000)
        self.notes = QPlainTextEdit(); self.notes.setMaximumHeight(90)
        self.save_notes_button = QPushButton("Save Model Notes")
        self.save_notes_button.clicked.connect(self._save_notes)
        form.addRow("Tags (comma separated)", self.tags)
        form.addRow("Your notes", self.notes)
        notes_layout.addLayout(form)
        self.note_status = QLabel(); self.note_status.setWordWrap(True)
        self.note_status.setTextFormat(Qt.PlainText); notes_layout.addWidget(self.note_status)
        self.discard_notes_button = QPushButton("Discard Pending Notes")
        self.discard_notes_button.clicked.connect(self._discard_notes)
        self.review_notes_button = QPushButton("Review Pending Notes…")
        self.review_notes_button.clicked.connect(self._review_pending_notes)
        self.tags.textChanged.connect(self._notes_changed)
        self.notes.textChanged.connect(self._notes_changed)
        notes_layout.addStretch(1)
        save_notes_row = QHBoxLayout()
        save_notes_row.addWidget(self.discard_notes_button)
        save_notes_row.addWidget(self.review_notes_button)
        save_notes_row.addStretch(1); save_notes_row.addWidget(self.save_notes_button)
        notes_layout.addLayout(save_notes_row)
        self.tabs.addTab(notes, "Notes")
        self.lineage = QPlainTextEdit(); self.lineage.setReadOnly(True)
        lineage_tab = QWidget(); lineage_layout = QVBoxLayout(lineage_tab)
        lineage_layout.addWidget(self.lineage)
        self.lineage_controls = QWidget(); relationships_layout = QVBoxLayout(self.lineage_controls)
        relationships_layout.setContentsMargins(0,0,0,0)
        navigation = QHBoxLayout()
        self.lineage_back = QPushButton("Back to Previous Model")
        self.lineage_back.clicked.connect(self._lineage_return)
        self.lineage_kind = QComboBox(); self.lineage_kind.setAccessibleName("Model relationship view")
        for label, key in (("Ordered Parents", "parents"), ("Derived Children", "children"),
                           ("Operations and Evidence", "operations"), ("Typed References", "references"),
                           ("Model Files", "locations")):
            self.lineage_kind.addItem(label, key)
        self.lineage_kind.currentIndexChanged.connect(self._lineage_kind_changed)
        self.raw_details_button = QPushButton("Raw Details…")
        self.raw_details_button.clicked.connect(self._show_raw_details)
        navigation.addWidget(self.lineage_back); navigation.addWidget(self.lineage_kind,1)
        navigation.addWidget(self.raw_details_button); relationships_layout.addLayout(navigation)
        self.relationships = QTreeWidget(); self.relationships.setRootIsDecorated(False)
        self.relationships.setHeaderLabels(["Relationship / Type", "Identity / Path"])
        self.relationships.setColumnWidth(0,240)
        self.relationships.setMinimumHeight(100)
        self.relationships.setAccessibleName("Recorded model relationships")
        self.relationships.itemSelectionChanged.connect(self._relationship_selected)
        self.relationships.itemDoubleClicked.connect(lambda *_:self._open_relationship())
        relationships_layout.addWidget(self.relationships,1)
        self.relationship_detail = QPlainTextEdit("Choose a relationship to inspect its exact identity.")
        self.relationship_detail.setReadOnly(True)
        self.relationship_detail.setMinimumHeight(60)
        self.relationship_detail.setMaximumHeight(100)
        self.relationship_detail.setAccessibleName("Selected relationship details")
        relationships_layout.addWidget(self.relationship_detail)
        relationship_actions = QHBoxLayout()
        self.lineage_previous = QPushButton("Previous Relationships")
        self.lineage_next = QPushButton("Next Relationships")
        self.lineage_previous.clicked.connect(lambda:self._change_lineage_page(-1))
        self.lineage_next.clicked.connect(lambda:self._change_lineage_page(1))
        self.lineage_count = QLabel()
        self.open_relationship_button = QPushButton("Inspect Related Model")
        self.open_relationship_button.clicked.connect(self._open_relationship)
        for widget in (self.lineage_previous,self.lineage_next,self.lineage_count):
            relationship_actions.addWidget(widget)
        relationship_actions.addStretch(1)
        relationships_layout.addLayout(relationship_actions)
        relationships_layout.addWidget(self.open_relationship_button)
        lineage_layout.addWidget(self.lineage_controls,1)
        self.derivation_button = QPushButton("Record Model Origins…")
        self.derivation_button.clicked.connect(self._record_derivation)
        register_row = QHBoxLayout()
        register_row.addWidget(self.derivation_button)
        register_row.addWidget(self.locate_copy_button)
        self.return_from_folder_button = QPushButton("Back to Model Selection")
        self.return_from_folder_button.clicked.connect(self._return_from_known_folder)
        self.return_from_folder_button.hide()
        layout.insertWidget(layout.indexOf(self.status), self.return_from_folder_button, 0, Qt.AlignRight)
        self.resume_review_button = QPushButton("Return to File Review")
        self.resume_review_button.clicked.connect(self._resume_file_review)
        self.resume_review_button.hide()
        layout.insertWidget(layout.indexOf(self.status), self.resume_review_button, 0, Qt.AlignRight)
        register_row.addStretch(1)
        lineage_layout.addLayout(register_row)
        self.tabs.addTab(lineage_tab, "Model Files and Origins")
        self._build_provider_tab()
        self._build_storage_tab()
        self._build_file_review()
        self.imported_metadata = QPlainTextEdit()
        self.imported_metadata.setReadOnly(True)
        self.imported_metadata.setAccessibleName("Imported model metadata and source attribution")
        self._imported_metadata_tab = self.tabs.addTab(self.imported_metadata, "Imported Metadata")
        self.provider_model_scope = QLabel()
        self.provider_model_scope.setTextFormat(Qt.PlainText)
        self.provider_model_scope.setWordWrap(True)
        self.provider_model_scope.setObjectName("mutedLabel")
        layout.insertWidget(layout.indexOf(self.tabs), self.provider_model_scope)
        self.tabs.currentChanged.connect(self._workspace_tab_changed)
        self._extras = [self.index_button, self.apply_index_button, self.role, self.save_notes_button,
                        self.tags, self.notes, self.provider_lookup, self.provider_cached, self.provider_search,
                        self.provider, self.query_kind, self.query, self.network_consent, self.download_button,
                        self.save_storage_button, self.default_destination, self.destination_overrides, self.derivation_button, self.guidance_button, self.recover_download_button,
                        self.provider_listings, self.download_files, self.attach_evidence_button,
                        self.upscaler_models_button]
        self._extras.extend((self.lineage_controls,self.discard_notes_button,self.review_notes_button,
                            self.known_folders_button,self.return_from_folder_button,self.locate_copy_button,
                            self.folder_scope,self.folder_descendants,self.folder_files,self.assign_folder_button,
                            self.assign_files_button,self.select_page_button,self.clear_files_button,self.resume_review_button))
        self._extras.extend((self.detect_files_button, self.use_detected_button, self.evidence_filter))
        self._extras.extend((self.reopen_review_button, self.save_review_button, self.save_review_as_button, self.recheck_review_button))
        self._extras.append(self.usage_placement_button)
        self._extras.append(self.import_plan_button)
        self.recheck_inspected_button = QPushButton("Recheck This File")
        self.recheck_inspected_button.setToolTip("Inspect the same file again before reviewing its model type and family. This does not register it.")
        self.recheck_inspected_button.clicked.connect(self._recheck_inspected_file)
        self.recheck_inspected_button.hide()
        layout.insertWidget(layout.indexOf(self.status), self.recheck_inspected_button, 0, Qt.AlignRight)
        self.return_companion_button = QPushButton("Return to Required Resources")
        self.return_companion_button.clicked.connect(self.accept)
        self.return_companion_button.setVisible(initial_companion_handoff is not None)
        self.return_companion_button.setEnabled(False)
        layout.insertWidget(layout.indexOf(self.status), self.return_companion_button, 0, Qt.AlignRight)
        if initial_companion_handoff is not None:
            handoff = initial_companion_handoff
            self.role.setCurrentText(handoff.role)
            self.family.setText(handoff.family)
            if len(handoff.expected_resource_ids) == 1:
                self.query_kind.setCurrentIndex(self.query_kind.findData("sha256"))
                self.query.setText(handoff.expected_resource_ids[0].removeprefix("sha256:"))
            self.network_consent.setChecked(False)
            self.provider_model_scope.setText(f"Required resource: {handoff.slot} · {handoff.role or 'Type unknown'} / {handoff.family or 'Family unknown'}. Review the exact download, register it, then return. Compatibility is checked separately.")
        self.role.currentTextChanged.connect(self._update_actions)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self.dialog_commands = DialogCommandController(
            self, "Models", self._ribbon_commands(),
            groups=("Local Models", "Provider Evidence") if self.collect_for_import else ("Start", "Local Models", "Provider Evidence", "Planning", "Storage"),
            on_group_changed=self._ribbon_group_changed, initial_group="Local Models",
            visible_limits={"Local Models": 3, "Provider Evidence": 3, "Planning": 3, "Storage": 4},
        )
        self._setup_management_browser()
        self._update_actions()
        if self.collect_for_import:
            self.setWindowTitle("Choose Models for Import Plan")
            if initial_companion_handoff is not None:
                self.provider_model_scope.setText("Required resource: " + initial_companion_handoff.slot +
                    ". Add its exact download choice to the import plan. Registration and compatibility verification happen after the combined approval.")
        if initial_companion_handoff is not None:
            self.dialog_commands.select_group("Provider Evidence")

    def _setup_management_browser(self):
        from .model_resource_browser import ModelResourceBrowser
        from .model_artwork_dialog import artwork_service
        from core.resource_browser_view import BrowserViewPreferences, load_view_preferences
        self._management_preferences = deepcopy(getattr(self.settings, "resource_browser_views", {}))
        self._management_preferences_writable = not self.collect_for_import and callable(getattr(self.settings, "update_resource_browser_view", None))
        notice = ""
        try: initial = load_view_preferences(self._management_preferences, "models_management")
        except ValueError as exc:
            initial = BrowserViewPreferences(); self._management_preferences_writable = False
            notice = f"Saved layout unavailable. Changes apply to this window only: {exc}"
        layout = self.layout(); index = layout.indexOf(self.models); layout.removeWidget(self.models)
        self.browser = ModelResourceBrowser(self.models, initial_state=initial,
            table_value_to_row=lambda identity: self._management_rows.get(identity), name_column=0, compact_columns=(0,))
        self.browser.setMinimumHeight(355)
        self.browser.configure_artwork(artwork_service(self.registry, self.settings, self.workspace.protected_roots), self.jobs, settings=self.settings)
        self.browser.viewChanged.connect(self._management_view_changed)
        self.browser.preference_changed.connect(self._save_management_view)
        self.management_browser_panel = QWidget()
        browser_layout = QVBoxLayout(self.management_browser_panel); browser_layout.setContentsMargins(0, 0, 0, 0)
        self.management_view_button = QPushButton("Customize Browse View…")
        self.management_view_button.setCheckable(True)
        self.management_view_button.setAccessibleName("Show registered model view and folder options")
        self.management_view_button.toggled.connect(self._sync_management_view_chrome)
        browser_layout.addWidget(self.management_view_button, 0, Qt.AlignLeft)
        self.management_folder_scope = QWidget()
        scope_layout = QHBoxLayout(self.management_folder_scope)
        scope_layout.setContentsMargins(0, 0, 0, 0)
        self.management_folder_label = QLabel()
        self.management_folder_label.setWordWrap(True)
        self.management_folder_label.setTextFormat(Qt.PlainText)
        self.management_clear_folder_button = QPushButton("Show All Folders")
        self.management_clear_folder_button.clicked.connect(self.browser.clear_folder_scope)
        scope_layout.addWidget(self.management_folder_label, 1)
        scope_layout.addWidget(self.management_clear_folder_button)
        browser_layout.addWidget(self.management_folder_scope)
        self._management_view_chrome_visible = self.collect_for_import
        self.browser.set_picker_chrome_visible(self._management_view_chrome_visible)
        self.empty_models_panel = EmptyStatePanel(
            "Inspect a model file you already have. You can review its type and family before "
            "registering it; the file stays where it is. You can also review a folder or "
            "import selected files from an enrolled SwarmUI setup.",
            "No models added yet", minimum_height=355,
        )
        # This is a contextual shortcut to the existing ribbon command, not a
        # second registration path. It supplies the first step in an empty view.
        self.empty_inspect_button = QPushButton("Inspect a Model File…")
        self.empty_inspect_button.setProperty("primary", True)
        self.empty_inspect_button.setAutoDefault(False)
        self.empty_inspect_button.clicked.connect(self.inspect_button.click)
        self.empty_models_panel.layout().insertWidget(
            self.empty_models_panel.layout().count() - 1,
            self.empty_inspect_button, 0, Qt.AlignCenter,
        )
        self.empty_folder_button = QPushButton("Review a Model Folder…")
        self.empty_folder_button.setAutoDefault(False)
        self.empty_folder_button.clicked.connect(self.index_button.click)
        self.empty_models_panel.layout().insertWidget(
            self.empty_models_panel.layout().count() - 1,
            self.empty_folder_button, 0, Qt.AlignCenter,
        )
        self.empty_swarm_button = QPushButton("Import Models from SwarmUI…")
        self.empty_swarm_button.setAutoDefault(False)
        self.empty_swarm_button.clicked.connect(self.import_plan_button.click)
        self.empty_models_panel.layout().insertWidget(
            self.empty_models_panel.layout().count() - 1,
            self.empty_swarm_button, 0, Qt.AlignCenter,
        )
        self.empty_models_panel.hide()
        self._update_management_folder_scope()
        browser_layout.addWidget(self.empty_models_panel, 1)
        browser_layout.addWidget(self.browser, 1)
        for position in range(layout.count()):
            pages = layout.itemAt(position).layout()
            if pages is not None and pages.indexOf(self.previous) >= 0:
                layout.removeItem(pages); browser_layout.addLayout(pages); break
        layout.removeWidget(self.detail); layout.removeWidget(self.tabs)
        # Hidden provider/scan pages must not impose their wide minimum on the
        # selected model's details pane. They regain the full body when opened.
        self.tabs.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Expanding)
        self.detail.setSizePolicy(QSizePolicy.Ignored, QSizePolicy.Preferred)
        details = QWidget(); details_layout = QVBoxLayout(details); details_layout.setContentsMargins(0, 0, 0, 0)
        details.setMinimumWidth(360)
        self.unselected_model_detail = EmptyStatePanel(
            "Choose a registered model to see its notes, files, and origins here.",
            "Select a model to see details", minimum_height=355,
        )
        details_layout.addWidget(self.unselected_model_detail, 1)
        details_layout.addWidget(self.detail); details_layout.addWidget(self.tabs, 1)
        self.management_details_panel = details
        self.management_splitter = QSplitter(Qt.Horizontal)
        self.management_splitter.addWidget(self.management_browser_panel); self.management_splitter.addWidget(details)
        self.management_splitter.setStretchFactor(0, 1); self.management_splitter.setStretchFactor(1, 0)
        self.management_splitter.setSizes((920, 520))
        layout.insertWidget(index, self.management_splitter, 1)
        self.single_file_panel = QWidget()
        self.single_file_panel.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Expanding)
        single_file_layout = QVBoxLayout(self.single_file_panel)
        single_file_layout.setContentsMargins(12, 18, 12, 12)
        single_file_heading = QLabel("Review this model file")
        single_file_heading.setObjectName("sectionTitle")
        single_file_layout.addWidget(single_file_heading)
        self.single_file_summary = QLabel()
        self.single_file_summary.setWordWrap(True)
        self.single_file_summary.setTextFormat(Qt.PlainText)
        self.single_file_summary.setAccessibleName("Selected model file and next step")
        single_file_layout.addWidget(self.single_file_summary)
        self.single_file_identity = QPlainTextEdit()
        self.single_file_identity.setReadOnly(True)
        self.single_file_identity.setMaximumHeight(100)
        self.single_file_identity.setAccessibleName("Exact inspected model file identity")
        self.single_file_identity_toggle = DisclosureButton(
            "Show Exact File Identity", "Hide Exact File Identity", self.single_file_identity,
        )
        single_file_layout.addWidget(self.single_file_identity_toggle, 0, Qt.AlignLeft)
        single_file_layout.addWidget(self.single_file_identity)
        self.single_file_browse_button = QPushButton("Browse Registered Models")
        self.single_file_browse_button.clicked.connect(lambda: self._set_single_file_browser(True))
        single_file_layout.addWidget(self.single_file_browse_button, 0, Qt.AlignLeft)
        self.single_file_declaration_row = QHBoxLayout()
        self.single_file_declaration_row.addStretch(1)
        single_file_layout.addLayout(self.single_file_declaration_row)
        single_file_layout.addStretch(1)
        self._single_file_declaration_moved = False
        layout.insertWidget(layout.indexOf(self.model_context) + 1, self.single_file_panel, 1)
        self.single_file_panel.hide()
        self.single_file_return_button = QPushButton("Return to This File Review")
        self.single_file_return_button.clicked.connect(lambda: self._set_single_file_browser(False))
        layout.insertWidget(layout.indexOf(self.management_splitter),
                            self.single_file_return_button, 0, Qt.AlignLeft)
        self.single_file_return_button.hide()
        self._setup_start_panel(layout, index)
        self._setup_planning_panel(layout)
        self.management_view_notice = QLabel(notice); self.management_view_notice.setWordWrap(True)
        self.management_view_notice.setVisible(bool(notice)); layout.insertWidget(index, self.management_view_notice)
        self.models.setHeaderLabels(["Model", "Model Type / Family", "Locations", "Favorite", "Author", "Version", "Source", "Size", "Availability"])
        self._workspace_tab_changed(self.tabs.currentIndex())

    def _setup_start_panel(self, layout, index):
        self.start_panel = QWidget()
        self.start_panel.setMinimumWidth(850)
        self.start_panel.setMaximumWidth(1120)
        start = QVBoxLayout(self.start_panel)
        start.setContentsMargins(18, 24, 18, 12)
        heading = QLabel("What would you like to do with models?")
        title_font = heading.font(); title_font.setPointSize(16); title_font.setBold(True)
        heading.setFont(title_font)
        start.addWidget(heading)
        self.start_summary = QLabel("Loading registered models…")
        self.start_summary.setWordWrap(True); start.addWidget(self.start_summary)
        goals = QHBoxLayout()
        self.start_add_button = QPushButton("Add Models I Already Have")
        self.start_add_button.setProperty("primary", True)
        self.start_add_button.clicked.connect(self._show_start_add)
        self.start_choose_button = QPushButton("Choose Models for a Task")
        self.start_choose_button.clicked.connect(lambda: self.dialog_commands.select_group("Planning"))
        self.start_storage_button = QPushButton("Manage Model Storage")
        self.start_storage_button.clicked.connect(lambda: self.dialog_commands.select_group("Storage"))
        for button, explanation in (
                (self.start_add_button, "Review existing files, then register only what you choose."),
                (self.start_choose_button, "Open generation or training guidance using saved models."),
                (self.start_storage_button, "Review locations, moves, Fast copies and recovery.")):
            button.setAutoDefault(False)
            column = QVBoxLayout(); column.addWidget(button)
            note = QLabel(explanation); note.setWordWrap(True); column.addWidget(note)
            goals.addLayout(column, 1)
        start.addLayout(goals)
        self.start_add_panel = QWidget()
        add = QVBoxLayout(self.start_add_panel); add.setContentsMargins(0, 8, 0, 0)
        add.addWidget(QLabel("Choose where your existing models are. Reviewing never moves or registers them automatically."))
        add_choices = QHBoxLayout()
        self.start_file_button = QPushButton("One Model File…")
        self.start_folder_button = QPushButton("A Folder of Models…")
        self.start_swarm_button = QPushButton("An Existing SwarmUI Installation…")
        for button, source in ((self.start_file_button, self.inspect_button),
                               (self.start_folder_button, self.index_button),
                               (self.start_swarm_button, self.import_plan_button)):
            button.setAutoDefault(False)
            button.clicked.connect(lambda _checked=False, target=source: self._start_source(target))
            add_choices.addWidget(button)
        add.addLayout(add_choices)
        start.addWidget(self.start_add_panel)
        self.start_add_panel.hide()
        lower = QHBoxLayout()
        self.start_browse_button = QPushButton("Browse Registered Models")
        self.start_browse_button.clicked.connect(lambda: self.dialog_commands.select_group("Local Models"))
        self.start_reload_button = QPushButton("Retry Loading Models")
        self.start_reload_button.clicked.connect(self.reload_button.click)
        for button in (self.start_browse_button, self.start_reload_button):
            button.setAutoDefault(False); lower.addWidget(button)
        lower.addStretch(1); start.addLayout(lower)
        start.addStretch(1)
        self.start_panel.hide()
        layout.insertWidget(index, self.start_panel, 1)
        layout.setAlignment(self.start_panel, Qt.AlignHCenter)

    def _setup_planning_panel(self, layout):
        self.planning_panel = QWidget()
        self.planning_panel.setMinimumWidth(850)
        self.planning_panel.setMaximumWidth(1120)
        planning = QVBoxLayout(self.planning_panel)
        planning.setContentsMargins(18, 20, 18, 12)
        self.planning_heading = QLabel("Choose a model plan")
        self.planning_heading.setObjectName("sectionTitle")
        planning.addWidget(self.planning_heading)
        self.planning_summary = QLabel()
        self.planning_summary.setTextFormat(Qt.PlainText)
        self.planning_summary.setWordWrap(True)
        planning.addWidget(self.planning_summary)
        actions = QHBoxLayout()
        self.planning_browser_button = QPushButton("Browse or Change Registered Model…")
        self.planning_browser_button.setCheckable(True)
        self.planning_browser_button.setToolTip(
            "Optional: browse registered models and edit notes without changing the reviewed plan automatically."
        )
        self.planning_browser_button.toggled.connect(self._planning_browser_changed)
        self.planning_add_button = QPushButton("Add Models I Already Have")
        self.planning_add_button.clicked.connect(self._planning_add_models)
        self.planning_retry_button = QPushButton("Retry Loading Models")
        self.planning_retry_button.clicked.connect(self.reload_button.click)
        for button in (self.planning_browser_button, self.planning_add_button, self.planning_retry_button):
            button.setAutoDefault(False)
            actions.addWidget(button)
        actions.addStretch(1)
        planning.addLayout(actions)
        layout.removeWidget(self.continue_generation_button)
        planning.addWidget(self.continue_generation_button, 0, Qt.AlignLeft)
        planning.addStretch(1)
        self.planning_panel.hide()
        layout.insertWidget(layout.indexOf(self.management_splitter), self.planning_panel,
                            1, Qt.AlignHCenter)

    def _planning_browser_changed(self, expanded):
        self._planning_browser_open = bool(expanded)
        self.planning_browser_button.setText(
            "Hide Model Browser and Notes" if expanded else "Browse or Change Registered Model…"
        )
        self._update_start_panel()
        if expanded:
            self.search.setFocus(Qt.ShortcutFocusReason)

    def _planning_add_models(self):
        if self.dialog_commands.select_group("Start"):
            self._show_start_add()

    def _update_planning_summary(self):
        if not hasattr(self, "planning_summary"):
            return
        selected = self.models.currentItem()
        identity = selected.data(0, Qt.UserRole) if selected is not None else ""
        row = self._management_rows.get(identity)
        name = getattr(row, "display_name", "") or (selected.text(0) if selected is not None else "")
        if not self._registry_loaded:
            message = "Registered models are unavailable or still loading. Retry when the current work finishes."
        elif self.generation_handoff is not None:
            message = ("Image setup reviewed. Continue in Generate opens a separate new draft; "
                       "model readiness and generation are checked later.")
        else:
            message = ("Choose Plan for a Model or Recommend for Image Generation above. "
                       "You can start without preselecting a registered model.")
        self.planning_heading.setText(
            "Reviewed image setup" if self.generation_handoff is not None else "Choose a model plan"
        )
        if name and self.generation_handoff is not None:
            message += f" Browser selection: {name}; changing it does not alter this reviewed setup."
        elif name:
            message += f" Current browser selection: {name}; the recommendation shows what it will use."
        elif self._registry_loaded and not self._rows:
            message += " No models are registered yet; you can add existing files below."
        if self._note_drafts and self.generation_handoff is not None:
            message += " Pending model notes must be saved or discarded before continuing."
        self.planning_summary.setText(message)
        self.planning_add_button.setVisible(self._registry_loaded and not self._rows)
        self.planning_retry_button.setVisible(not self._registry_loaded)
        self.planning_retry_button.setEnabled(self._task is None and self.reload_button.isEnabled())

    def _show_start_add(self):
        self.start_add_panel.show()
        self.start_file_button.setFocus(Qt.ShortcutFocusReason)

    def _start_source(self, source):
        if self._closing or self._task is not None or not self._registry_loaded or not source.isEnabled(): return
        inspection, preview = self._inspection, self._index_preview
        if not self.dialog_commands.select_group("Local Models") or not source.isEnabled(): return
        if source is self.import_plan_button:
            self._start_import_return = True
        source.click()
        if source in (self.inspect_button, self.index_button) and self._task is None and self._inspection is inspection and self._index_preview is preview:
            self.dialog_commands.select_group("Start")
            self.start_add_panel.show()

    def _set_single_file_browser(self, open_browser):
        if self._single_file_review_path is None:
            return
        self._single_file_browser_open = bool(open_browser)
        self._update_start_panel()

    def _place_single_file_declarations(self, focused):
        if focused == self._single_file_declaration_moved:
            return
        controls = (self.family_label, self.family, self.role_label, self.role)
        source = self._local_declaration_row if focused else self.single_file_declaration_row
        target = self.single_file_declaration_row if focused else self._local_declaration_row
        for index, widget in enumerate(controls):
            source.removeWidget(widget)
            target.insertWidget(index if focused else index + 1, widget)
        self._single_file_declaration_moved = focused

    def _update_single_file_summary(self):
        path = self._single_file_review_path
        if path is None:
            return
        if self._inspection is not None:
            inspection = self._inspection
            state = ("Registering this file…" if self._single_file_register_pending else
                     "File inspection complete. Choose its Model Family and Model Type below, "
                     "then Register Model. Registration keeps the file in place; "
                     "compatibility is checked separately.")
            self.single_file_identity.setPlainText(
                f"File: {inspection.path}\nExact identity: {inspection.resource_id}\n"
                f"{inspection.size:,} bytes · {inspection.container['tensor_count']:,} tensors · "
                "Container verified"
            )
        elif self._single_file_recheck_path is not None:
            state = ("Registration was not confirmed. Use Recheck This File below, then review "
                     "the file and declarations before retrying. No changed file was registered.")
            self.single_file_identity.setPlainText(self.detail.text())
        elif self._task is not None:
            state = "Inspecting this file… Registration has not started. Stop Work remains available."
            self.single_file_identity.setPlainText(f"Selected file: {path}")
        else:
            state = ("This file is not ready to register. Choose Inspect File to inspect a file "
                     "again, or browse your registered models.")
            self.single_file_identity.setPlainText(self.detail.text())
        self.single_file_summary.setText(f"{path.name}\n{state}")

    def _update_start_panel(self):
        if not hasattr(self, "start_panel"): return
        home = getattr(self, "_active_ribbon_group", "") == "Start" and not self.collect_for_import
        planning = getattr(self, "_active_ribbon_group", "") == "Planning" and not self.collect_for_import
        local = self.tabs.currentIndex() in (0, 1, self._imported_metadata_tab)
        focused = (not self.collect_for_import
                   and getattr(self, "_active_ribbon_group", "") == "Local Models"
                   and local and self._single_file_review_path is not None)
        browse = focused and self._single_file_browser_open
        self._place_single_file_declarations(focused and not browse)
        self.start_panel.setVisible(home)
        self.planning_panel.setVisible(planning)
        self.layout().setStretch(self.layout().indexOf(self.planning_panel),
                                 1 if planning and not self._planning_browser_open else 0)
        self._update_planning_summary()
        self.single_file_panel.setVisible(focused and not browse)
        self.single_file_return_button.setVisible(browse)
        show_management = not home and (not focused or browse) and (not planning or self._planning_browser_open)
        self.management_splitter.setVisible(show_management)
        self._sync_management_view_chrome()
        show_browser_controls = show_management and local
        for widget in (self.search, self.count, self.help_button, self.selector_button):
            widget.setVisible(show_browser_controls)
        self.use_button.setVisible(show_management and local and not planning and
                                   (not self._return_to_selector or self._inspection is not None) and
                                   (not focused or self._inspection is not None))
        if focused:
            self._update_single_file_summary()
        if home:
            count = len(getattr(self, "_rows", ()))
            self.start_summary.setText(
                f"{count:,} registered {'model' if count == 1 else 'models'}. Your selection and pending edits are kept when you browse or return here."
                if self._registry_loaded else "Model list unavailable or still loading. Retry after the current work finishes.")
            ready = self._registry_loaded and self._task is None
            for button, source in ((self.start_file_button, self.inspect_button),
                                   (self.start_folder_button, self.index_button),
                                   (self.start_swarm_button, self.import_plan_button)):
                button.setEnabled(ready and source.isEnabled())
            for button in (self.start_add_button, self.start_choose_button, self.start_storage_button,
                           self.start_browse_button):
                button.setEnabled(ready)
            self.start_reload_button.setVisible(not self._registry_loaded)
            self.start_reload_button.setEnabled(self._task is None and self.reload_button.isEnabled())

    def _save_management_view(self, changes):
        if not self._management_preferences_writable: return
        try:
            saved = self.settings.update_resource_browser_view("models_management", changes, expected=deepcopy(self._management_preferences))
        except (ValueError, OSError, RuntimeError) as exc:
            self._management_preferences_writable = False
            self.management_view_notice.setText(f"Layout changed for this window. Saved preferences were preserved: {exc}")
            self.management_view_notice.show(); return
        self._management_preferences = deepcopy(saved)

    def _sync_management_view_chrome(self, *_args):
        if not hasattr(self, "management_view_button"):
            return
        planning = getattr(self, "_active_ribbon_group", "") == "Planning"
        self._management_view_chrome_visible = (
            self.collect_for_import or planning or self.management_view_button.isChecked())
        self.browser.set_picker_chrome_visible(self._management_view_chrome_visible)
        self.management_view_button.setText(
            "Hide Browse Options" if self.management_view_button.isChecked() else "Customize Browse View…")
        self.management_view_button.setVisible(
            not self.collect_for_import and not planning and self.empty_models_panel.isHidden())
        self._update_management_folder_scope()

    def _update_management_folder_scope(self):
        folder, label = self.browser.active_folder_scope()
        hidden = not self._management_view_chrome_visible
        self.management_folder_label.setText(
            f"Showing folder: {label}. Models outside this folder are hidden."
            if folder else "")
        self.management_folder_label.setToolTip(folder)
        self.management_folder_scope.setVisible(bool(folder and hidden) and self.empty_models_panel.isHidden())

    def _management_view_changed(self):
        self._update_management_folder_scope()
        self._stash_notes()
        current = self.models.currentItem(); identity = current.data(0, Qt.UserRole) if current else ""
        visible = self.browser.visible_rows()
        self._page = next((index // self.PAGE_SIZE for index, row in enumerate(visible) if row.resource_id == identity), 0)
        self._render()

    def _build_file_review(self):
        panel = QWidget(); body = QVBoxLayout(panel)
        self._folder_review_layout = body
        next_action = ("Update Selected Import Choice" if self._initial_import_review is not None else
            "Add Files to Import Plan" if self.collect_for_import else "Register Selected Files")
        explanation = QLabel(f"Model type says what a file does (checkpoint, LoRA, VAE); model family says which architecture or version it belongs to (Anima, SDXL, Flux, Krea, Wan). Assign both, check the files you want, then {next_action}. Mixed folders can have different assignments. Detection offers suggestions, not verified compatibility; source files stay in place.")
        self.folder_review_explanation = explanation
        explanation.setWordWrap(True); body.addWidget(explanation)
        scope_row = QHBoxLayout()
        self.folder_scope = QComboBox(); self.folder_scope.setAccessibleName("Scanned model subfolder")
        self.folder_descendants = QCheckBox("Include descendant folders"); self.folder_descendants.setChecked(True)
        scope_row.addWidget(QLabel("Folder")); scope_row.addWidget(self.folder_scope, 1); scope_row.addWidget(self.folder_descendants)
        self.folder_advanced_button = QPushButton("Detect Types or Customize Folders…")
        self.folder_advanced_button.setCheckable(True)
        self.folder_advanced_button.setAccessibleName("Show model detection and folder assignment controls")
        body.addWidget(self.folder_advanced_button, 0, Qt.AlignLeft)
        self.folder_advanced_panel = QWidget()
        advanced_layout = QVBoxLayout(self.folder_advanced_panel)
        advanced_layout.setContentsMargins(0, 0, 0, 0)
        advanced_layout.addLayout(scope_row)
        assignment_row = QHBoxLayout()
        self.assign_folder_button = QPushButton("Assign to Matching Folder Files")
        self.assign_folder_button.clicked.connect(lambda: self._assign_file_declarations(folder=True))
        self.assign_files_button = QPushButton("Assign to Checked Files")
        self.assign_files_button.clicked.connect(lambda: self._assign_file_declarations(folder=False))
        self.select_page_button = QPushButton("Check Matching Ready Files")
        self.select_page_button.clicked.connect(self._check_folder_files)
        self.clear_files_button = QPushButton("Clear Checked Files")
        self.clear_files_button.clicked.connect(self._clear_folder_files)
        for widget in (self.assign_files_button, self.select_page_button, self.clear_files_button):
            assignment_row.addWidget(widget)
        assignment_row.addStretch(1)
        body.addLayout(assignment_row)
        advanced_layout.addWidget(self.assign_folder_button, 0, Qt.AlignLeft)
        evidence_row = QHBoxLayout()
        self.detect_files_button = QPushButton("Detect Type, Family and Duplicates")
        self.detect_files_button.clicked.connect(self._detect_files)
        self.use_detected_button = QPushButton("Use Suggestions for Checked Files")
        self.use_detected_button.clicked.connect(self._use_detected_assignments)
        self.evidence_filter = QComboBox(); self.evidence_filter.setAccessibleName("Model evidence filter")
        for label, key in (("All reviewed files", "all"), ("Exact duplicate copies", "duplicates"), ("Related provider versions", "versions"), ("Unknown or conflicting identity", "unknown")):
            self.evidence_filter.addItem(label, key)
        self.evidence_filter.currentIndexChanged.connect(self._file_scope_changed)
        self.evidence_filter.setToolTip("Matching-file actions use both folder and evidence filters. Checked-file actions include checked files across filters and pages.")
        evidence_row.addWidget(self.detect_files_button); evidence_row.addWidget(self.use_detected_button)
        evidence_row.addStretch(1); evidence_row.addWidget(self.evidence_filter)
        advanced_layout.addLayout(evidence_row)
        body.addWidget(self.folder_advanced_panel)
        self.folder_advanced_panel.hide()
        self.folder_advanced_button.toggled.connect(self._toggle_folder_advanced)
        self.folder_summary = QLabel("Scan a folder to review files. Scanning does not register models.")
        self.folder_summary.setTextFormat(Qt.PlainText); self.folder_summary.setWordWrap(True)
        summary_row = QHBoxLayout()
        summary_row.addWidget(self.folder_summary, 1)
        self.review_held_button = QPushButton("Review Held Files")
        self.review_held_button.setToolTip("Show the next held file and its reason, including files outside the current folder, filter or page. A checked held file blocking the selected action is shown first. This does not register or rescan files.")
        self.review_held_button.clicked.connect(self._review_held_files)
        self.review_held_button.hide()
        summary_row.addWidget(self.review_held_button, 0, Qt.AlignTop)
        body.addLayout(summary_row)
        recovery_row = QHBoxLayout()
        self.retry_folder_button = QPushButton("Retry Folder Review")
        self.retry_folder_button.clicked.connect(self._retry_folder_review)
        self.choose_another_folder_button = QPushButton("Choose Another Folder…")
        self.choose_another_folder_button.clicked.connect(self._choose_folder)
        recovery_row.addWidget(self.retry_folder_button)
        recovery_row.addWidget(self.choose_another_folder_button)
        recovery_row.addStretch(1)
        body.addLayout(recovery_row)
        self.retry_folder_button.hide()
        self.choose_another_folder_button.hide()
        self.folder_files = QTreeWidget(); self.folder_files.setRootIsDecorated(False)
        self.folder_files.setUniformRowHeights(True)
        self.folder_files.setHeaderLabels(["File / subfolder", "Review state", "Model Type", "Model Family", "Detection / copies"])
        self.folder_files.setColumnWidth(0, 440); self.folder_files.setColumnWidth(1, 150)
        self.folder_files.setAccessibleName("Scanned files and registration assignments")
        self.folder_files.itemChanged.connect(self._file_checked)
        self.folder_files.itemSelectionChanged.connect(self._show_file_evidence)
        body.addWidget(self.folder_files, 1)
        self.file_evidence = QPlainTextEdit(); self.file_evidence.setReadOnly(True)
        self.file_evidence.setMaximumHeight(115); self.file_evidence.hide()
        self.file_evidence.setAccessibleName("Selected model detection and version evidence")
        body.addWidget(self.file_evidence)
        actions = QHBoxLayout()
        self.file_previous = QPushButton("Previous Files"); self.file_next = QPushButton("Next Files")
        self.file_previous.clicked.connect(lambda: self._file_page(-1))
        self.file_next.clicked.connect(lambda: self._file_page(1))
        actions.addWidget(self.file_previous); actions.addWidget(self.file_next); actions.addStretch(1)
        self.save_review_button = QPushButton("Save File Review…")
        self.save_review_button.setToolTip("Save your file choices and assignments to continue later. This does not register or move models.")
        self.save_review_button.clicked.connect(lambda: self._save_scan_review())
        self.save_review_as_button = QPushButton("Save As…")
        self.save_review_as_button.setToolTip("Save the current file review to a new file, leaving the previous review unchanged.")
        self.save_review_as_button.clicked.connect(lambda: self._save_scan_review(save_as=True))
        self.recheck_review_button = QPushButton("Recheck Saved Files")
        self.recheck_review_button.setToolTip("Explicitly verify the saved files before registration; no folder rescan or automatic registration.")
        self.recheck_review_button.clicked.connect(self._recheck_scan_review)
        actions.addWidget(self.save_review_button); actions.addWidget(self.save_review_as_button); actions.addWidget(self.recheck_review_button)
        actions.addWidget(self.apply_index_button); body.addLayout(actions)
        body.addStretch(0)
        self._folder_review_spacer_index = body.count() - 1
        self._folder_review_widgets = (self.folder_review_explanation, self.folder_advanced_button,
            self.assign_files_button, self.select_page_button, self.clear_files_button,
            self.folder_files, self.file_previous, self.file_next,
            self.save_review_button, self.apply_index_button)
        self.folder_scope.currentIndexChanged.connect(self._file_scope_changed)
        self.folder_descendants.toggled.connect(self._file_scope_changed)
        self.tabs.addTab(panel, "Add Model Files")
        self.tabs.setTabVisible(4, False)

    def _toggle_folder_advanced(self, open_):
        self.folder_advanced_panel.setVisible(open_)
        self.folder_advanced_button.setText(
            "Hide Folder and Detection Controls" if open_ else "Detect Types or Customize Folders…")
        if hasattr(self, "folder_summary"):
            self._file_selection_summary()

    def _file_scope_changed(self, *_):
        self._folder_page = 0
        self._render_file_review()

    def _file_entries(self):
        preview = self._index_preview
        if preview is None: return []
        folder = self.folder_scope.currentData() or Path(".")
        def included(name):
            parent = Path(name).parent
            return parent == folder or (self.folder_descendants.isChecked() and (folder == Path(".") or folder in parent.parents))
        rows = [(str(item.path.relative_to(preview.root)), item.path,
                 "Needs recheck" if self._review_requires_check else "Ready", "") for item in preview.inspections]
        missing = {}
        if self._scan_review_draft is not None and not self._review_requires_check:
            ready_paths = {item.path for item in preview.inspections}
            missing = {str(item.path.relative_to(preview.root)): item.path
                       for item in self._scan_review_draft.candidates if item.path not in ready_paths}
        rows.extend((name, missing.get(name), "Held", reason) for name, reason in preview.held)
        mode = self.evidence_filter.currentData()
        def matches(row):
            detection = self._detections.get(self._folder_ids.get(row[1]))
            return mode == "all" or bool(detection and (mode == "duplicates" and detection.duplicate or mode == "versions" and detection.related_versions or mode == "unknown" and detection.status != "suggested"))
        return sorted((row for row in rows if included(row[0]) and matches(row)), key=lambda row: row[0].casefold())

    def _render_file_review(self):
        if not hasattr(self, "folder_files"): return
        rows = self._file_entries()
        self._folder_page = min(self._folder_page, max(0, (len(rows) - 1) // self.PAGE_SIZE))
        start = self._folder_page * self.PAGE_SIZE
        self._folder_rendering = True
        self.folder_files.clear()
        for name, path, state, reason in rows[start:start + self.PAGE_SIZE]:
            declaration = self._folder_assignments.get(path)
            detection = self._detections.get(self._folder_ids.get(path))
            evidence = reason if state == "Held" else "Not detected"
            if detection:
                detection_text = f"{detection.status.capitalize()}: {detection.role or 'type unknown'} / {detection.family or 'family unknown'}"
                evidence = f"Held: {reason} · {detection_text}" if state == "Held" else detection_text
                if detection.duplicate: evidence += " · Exact duplicate copies"
                if detection.related_versions: evidence += " · Related versions"
            item = QTreeWidgetItem([name, state, declaration.role if declaration else "Not assigned" if path else "—",
                declaration.family if declaration else "Not assigned" if path else "—", evidence])
            item.setToolTip(4, evidence)
            item.setData(0, Qt.UserRole + 1, reason)
            if path:
                item.setData(0, Qt.UserRole, path)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Checked if path in self._folder_checked else Qt.Unchecked)
            else:
                item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
                item.setToolTip(1, reason)
            item.setToolTip(0, name)
            self.folder_files.addTopLevelItem(item)
        self._folder_rendering = False
        self.file_evidence.hide()
        self.file_previous.setEnabled(self._task is None and self._folder_page > 0)
        self.file_next.setEnabled(self._task is None and start + self.PAGE_SIZE < len(rows))
        self._file_selection_summary()

    def _file_selection_summary(self):
        preview = self._index_preview
        if preview is not None:
            filtered = self._file_entries()
            checked_matching = sum(path in self._folder_checked for _, path, _, _ in filtered)
            checked_elsewhere = len(self._folder_checked) - checked_matching
            missing = sum(path not in self._folder_assignments for path in self._folder_checked)
            state = "reviewed files need recheck" if self._review_requires_check else "ready"
            held_checked = len(self._folder_checked - {item.path for item in preview.inspections})
            recheck_label = "Recheck Saved Files" if self._scan_review_path is not None else "Recheck Reviewed Files"
            held_noun = "file" if held_checked == 1 else "files"
            next_step = f" {recheck_label} before registration." if self._review_requires_check else f" Uncheck {held_checked:,} held {held_noun} before registration." if held_checked else ""
            active_scope = self.folder_scope.currentText() if self.folder_scope.currentIndex() > 0 else "All scanned folders"
            if not self.folder_descendants.isChecked(): active_scope += " (this folder only)"
            active_filter = self.evidence_filter.currentText() if self.evidence_filter.currentIndex() > 0 else "All reviewed files"
            scope_note = f" Scope: {active_scope}; filter: {active_filter}. Open folder and detection controls to change these." if self.folder_scope.currentIndex() > 0 or self.evidence_filter.currentIndex() > 0 or not self.folder_descendants.isChecked() else ""
            self.folder_summary.setText(f"{len(preview.inspections):,} {state}, {len(preview.held):,} held · {len(self._folder_checked):,} checked across folders · {missing:,} checked files need assignments.\n"
                f"{len(filtered):,} match current filters ({checked_matching:,} checked match, {checked_elsewhere:,} checked outside current filters) · Page {self._folder_page + 1} · Scanning does not register files.{next_step}{scope_note} Folder: {preview.root}")
        self._update_actions()

    def _file_checked(self, item, column):
        if self._folder_rendering or column != 0: return
        path = item.data(0, Qt.UserRole)
        if path is None: return
        if item.checkState(0) == Qt.Checked: self._folder_checked.add(path)
        else: self._folder_checked.discard(path)
        self._file_selection_summary()

    def _file_page(self, delta):
        self._folder_page = max(0, self._folder_page + delta); self._render_file_review()

    def _check_folder_files(self):
        self._folder_checked.update(path for _, path, state, _ in self._file_entries() if path is not None and state != "Held")
        self._render_file_review()

    def _clear_folder_files(self):
        self._folder_checked.clear(); self._render_file_review()

    def _review_held_files(self):
        if self._task is not None or self._index_preview is None or not self._index_preview.held: return
        current = self.folder_files.currentItem()
        current_held = current.text(0) if current is not None and current.text(1) == "Held" else None
        with QSignalBlocker(self.folder_scope), QSignalBlocker(self.evidence_filter), QSignalBlocker(self.folder_descendants):
            self.folder_scope.setCurrentIndex(0)
            self.evidence_filter.setCurrentIndex(0)
            self.folder_descendants.setChecked(True)
        held_rows = [(index, name, path, reason) for index, (name, path, state, reason)
                     in enumerate(self._file_entries()) if state == "Held"]
        if not held_rows: return
        current_index = next((position for position, row in enumerate(held_rows) if row[1] == current_held), None)
        index, name, path, reason = (held_rows[(current_index + 1) % len(held_rows)] if current_index is not None
                                    else next((row for row in held_rows if row[2] in self._folder_checked), held_rows[0]))
        self._folder_page = index // self.PAGE_SIZE
        self._render_file_review()
        item = self.folder_files.topLevelItem(index % self.PAGE_SIZE)
        self.folder_files.setCurrentItem(item)
        self.folder_files.scrollToItem(item)
        self.folder_files.setFocus()
        next_step = "Uncheck it to register the other ready files, or choose Scan Model Folder to reconsider it." if path in self._folder_checked else "It is excluded from registration; choose Scan Model Folder to reconsider it."
        self.status.setText(f"Held file: {name}. {reason} {next_step}")

    def _assign_file_declarations(self, *, folder):
        if self._task is not None or self._index_preview is None: return
        role, family = self.role.currentText(), self.family.text().strip()
        if role not in RESOURCE_ROLES or not family: return
        paths = [path for _, path, _, _ in self._file_entries() if path is not None] if folder else list(self._folder_checked)
        for path in paths: self._folder_assignments[path] = IndexDeclaration(path, role, family)
        self._render_file_review()
        self.status.setText(f"Assigned {role} / {family} to {len(paths):,} reviewed files. Check the files to register; nested folders and checked files can be reassigned explicitly.")

    def _resume_file_review(self):
        if self._task is None and self._review_resume is not None:
            self._indexed(self._review_resume)
            state = "need recheck" if self._review_requires_check else "ready"
            self.status.setText(f"Remaining file review: {len(self._review_resume.inspections):,} {state}, {len(self._review_resume.held):,} held. Select a held row for its reason; Save File Review retains unfinished work."
                + (" Recheck Saved Files retries the saved candidates." if self._scan_review_draft is not None else ""))

    def _scan_review_service(self):
        from core.model_scan_review import ModelScanReviewService
        return ModelScanReviewService(self.workspace)

    def _current_scan_review(self):
        declarations = tuple(self._folder_assignments.values())
        selected = tuple(sorted(self._folder_checked, key=str))
        if self._scan_review_draft is not None:
            return replace(self._scan_review_draft, declarations=declarations, selected_paths=selected)
        preview = self._index_preview or self._review_resume
        return self._scan_review_service().capture(preview, declarations=declarations, selected_paths=selected)

    def _save_scan_review(self, path=None, *, save_as=False):
        if self._task is not None or not (self._index_preview or self._review_resume or self._scan_review_draft): return
        target = Path(path) if path else None if save_as else self._scan_review_path
        if target is None:
            chosen, _ = QFileDialog.getSaveFileName(self, "Save File Review", "model-file-review.json", "Model file review (*.json)")
            if not chosen: return
            target = Path(chosen)
        try:
            draft = self._current_scan_review()
        except Exception as exc:
            self._failed(str(exc)); return
        expected = self._scan_review_digest if target == self._scan_review_path else None
        def saved(digest):
            self._scan_review_path, self._scan_review_digest = target, digest
            self.status.setText(f"File review saved to {target}. Reopen File Review restores these choices; files are rechecked before registration.")
        self._run("Saving model file review", lambda job: self._scan_review_service().save(target, draft,
            expected_digest=expected, should_cancel=lambda: job.cancel_requested), saved)

    def _choose_scan_review(self):
        if self._task is not None: return
        chosen, _ = QFileDialog.getOpenFileName(self, "Reopen File Review", "", "Model file review (*.json)")
        if not chosen: return
        if self._index_preview or self._review_resume or self._scan_review_draft:
            if QMessageBox.question(self, "Replace File Review", "Replace the current file review with the saved review? Unsaved choices will be lost. Model files and any saved review stay unchanged.",
                    QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes: return
        self._open_scan_review(Path(chosen))

    def _open_scan_review(self, path):
        if self._task is not None: return
        def loaded(result):
            draft, digest = result
            self._scan_review_path, self._scan_review_digest = Path(path), digest
            self._scan_review_draft = draft
            self._review_requires_check = True
            self._folder_assignments = {item.path: item for item in draft.declarations}
            self._folder_checked = set(draft.selected_paths)
            self._detections.clear(); self.evidence_filter.setCurrentIndex(0)
            # Display-only metadata is deliberately not an IndexPreview. Neither
            # Detect nor Register may consume it before explicit revalidation.
            display = SimpleNamespace(root=draft.root, inspections=draft.candidates,
                                      held=draft.held, registry_token=draft.registry_token)
            self._review_resume = display
            self._indexed(display)
            self.status.setText("Saved choices restored without scanning or opening model files. Recheck Saved Files, review any held items, then register your selected files.")
        self._run("Reopening model file review", lambda job: self._scan_review_service().read(path), loaded)

    def _recheck_scan_review(self):
        if self._task is not None or self._scan_review_draft is None: return
        draft = self._current_scan_review()
        def checked(result):
            self._scan_review_draft = result.retained_draft
            self._review_requires_check = False
            self._folder_assignments = {item.path: item for item in result.retained_draft.declarations}
            self._folder_checked = set(result.retained_draft.selected_paths)
            self._review_resume = result.preview
            self._detections.clear()
            self._indexed(result.preview)
            if self._initial_import_review is not None:
                message = "Selected files rechecked. Review assignments and uncheck any held files before updating this import choice."
            else:
                message = f"Files rechecked; {len(result.completed_paths):,} already registered copies reconciled. Review assignments and uncheck any held files before registering. Save File Review retains the remaining work."
            self.status.setText(message + (" The registry changed since the saved review; this review uses its current state."
                if result.registry_changed else ""))
        self._run("Rechecking saved model files", lambda job: self._scan_review_service().revalidate(draft,
            should_cancel=lambda: job.cancel_requested,
            progress=lambda done, total, label: job.update(done, total or 0, label),
            reconcile_registered=self._initial_import_review is None), checked, unit="files")

    def _open_import_choice_review(self):
        draft = self._initial_import_review
        if draft is None or self._task is not None: return
        from core.model_scan_review import _decode, _encode, _json
        draft = _decode(draft if isinstance(draft, dict) else _json(_encode(draft)))
        self._scan_review_draft = draft
        self._review_requires_check = True
        self._folder_assignments = {item.path: item for item in draft.declarations}
        self._folder_checked = set(draft.selected_paths)
        self._review_resume = SimpleNamespace(root=draft.root, inspections=draft.candidates,
            held=draft.held, registry_token=draft.registry_token)
        self._indexed(self._review_resume)
        self.status.setText("Selected import choice restored without reading model files. Recheck Reviewed Files, review any held reason, then update this choice. Cancel keeps the original choice.")

    def _detect_files(self):
        if self._task is not None or self._index_preview is None or self._review_requires_check: return
        preview = self._index_preview
        def ready(result):
            self._detections = result
            self._render_file_review()
            self.status.setText("Detection is a suggestion, not compatibility proof. Review sources and conflicts, then explicitly use suggestions for checked files. Duplicate copies are never removed.")
        self._run("Reviewing model evidence and exact duplicates", lambda job: self.workspace.detect_preview(preview, should_cancel=lambda: job.cancel_requested), ready, unit="files")

    def _use_detected_assignments(self):
        if self._task is not None: return
        applied, unresolved = 0, 0
        for path in self._folder_checked:
            suggestion = self._detections.get(self._folder_ids.get(path))
            if suggestion and suggestion.status == "suggested":
                self._folder_assignments[path] = IndexDeclaration(path, suggestion.role, suggestion.family)
                applied += 1
            else: unresolved += 1
        self._render_file_review()
        self.status.setText(f"Applied reviewed suggestions to {applied:,} checked files; {unresolved:,} unknown/conflicting files keep their current assignments. Review or correct each declaration before registering.")

    def _show_file_evidence(self):
        if self._folder_rendering or not hasattr(self, "file_evidence"): return
        item = self.folder_files.currentItem()
        if item is None: self.file_evidence.hide(); return
        path = item.data(0, Qt.UserRole)
        suggestion = self._detections.get(self._folder_ids.get(path))
        lines = [item.text(0)]
        reason = item.data(0, Qt.UserRole + 1)
        if item.text(1) == "Held" and reason:
            lines.append("Held: " + reason)
        if suggestion:
            lines.extend((suggestion.resource_id, "Detection: " + suggestion.status, *suggestion.evidence))
            lines.extend((f"Exact identity: {len(suggestion.scanned_copies)} scanned copies, {len(suggestion.recorded_copies)} recorded locations (availability not rechecked).", *suggestion.scanned_copies[:50], *suggestion.recorded_copies[:50]))
            if len(suggestion.scanned_copies) > 50 or len(suggestion.recorded_copies) > 50: lines.append("Path detail limited to the first 50 of each group; use the duplicate filter to review scoped files.")
            lines.extend(suggestion.related_versions or ("No exact provider version relationship recorded; old/new chronology is unknown.",))
            if not suggestion.version_scope_complete: lines.append("Related-version evidence is partial; additional claims were outside this bounded review.")
        elif not (item.text(1) == "Held" and reason):
            lines.append(reason or "Use Detect Type, Family and Duplicates to inspect the saved evidence. No filename inference is used.")
        self.file_evidence.setPlainText("\n".join(lines)); self.file_evidence.show()

    def _ribbon_commands(self):
        """Supporting navigation only; local panes retain every transaction gate."""
        commands = (
            bind_source(self, "models.inspect_file", self.inspect_button, "Local Models",
                        move=True, keywords=("inspect", "file", "register")),
            bind_source(self, "models.known_folders", self.known_folders_button, "Local Models",
                        move=True, keywords=("saved", "folder", "discover", "swarm", "index")),
            bind_source(self, "models.import_plan", self.import_plan_button, "Local Models",
                        move=True, keywords=("swarm", "import", "organize", "fast", "resume")),
            bind_source(self, "models.find_role", self.selector_button, "Local Models",
                        move=True, keywords=("role", "family", "favorites")),
            bind_source(self, "models.artwork", self.artwork_button, "Local Models",
                        move=True, keywords=("artwork", "preview", "image", "thumbnail")),
            bind_source(self, "models.get_upscalers", self.upscaler_models_button, "Local Models",
                        move=True, keywords=("upscaler", "esrgan", "download", "catalog")),
            bind_source(self, "models.preview_folder", self.index_button, "Local Models",
                        move=True, keywords=("folder", "index", "register")),
            bind_source(self, "models.reopen_file_review", self.reopen_review_button, "Local Models",
                        move=True, keywords=("saved", "review", "resume", "registration")),
            bind_source(self, "models.reload", self.reload_button, "Local Models",
                        move=True, keywords=("refresh", "registry")),
            UiCommand("models.provider_query", "Enter Public Query", "Provider Evidence",
                      callback=self._focus_provider_query, keywords=("provider", "evidence", "search"),
                      description="Show the entered public provider query. It does not send it."),
            bind_source(self, "models.find_metadata", self.find_metadata_button, "Local Models",
                        move=True, keywords=("civitai", "hash", "metadata", "identify")),
            bind_source(self, "models.import_swarm_metadata", self.import_swarm_metadata_button, "Provider Evidence",
                        move=True, keywords=("swarm", "import", "metadata", "existing setup")),
            bind_source(self, "models.swarm_onboarding", self.swarm_onboarding_button, "Provider Evidence",
                        move=True, keywords=("swarm", "import", "favorites", "appearance", "existing setup")),
            UiCommand("models.plan_guidance", self.guidance_button.text(), "Planning",
                      callback=self._open_guidance, enabled=lambda: self._task is None and not self._closing,
                      keywords=("training", "guidance", "model")),
            UiCommand("models.plan_generation", self.generation_button.text(), "Planning",
                      callback=self._open_generation_plan, enabled=lambda: self._task is None and not self._closing,
                      keywords=("generation", "image", "plan")),
            UiCommand("models.required_resources", self.required_resources_button.text(), "Planning",
                      callback=self._open_required_resources, enabled=lambda: self._task is None and not self._closing,
                      keywords=("companion", "vae", "encoder", "requirements")),
            bind_source(self, "models.setup_folders", self.setup_folders_button, "Storage",
                        move=True, keywords=("location", "destination", "folders")),
            bind_source(self, "models.manage_fast", self.manage_fast_button, "Storage",
                        move=True, keywords=("cache", "pins", "cleanup", "recovery")),
            bind_source(self, "models.organize", self.organize_button, "Storage",
                        move=True, keywords=("move", "model", "organize", "source")),
            bind_source(self, "models.organize_results", self.organize_batch_button, "Storage",
                        move=True, keywords=("move", "registered", "batch")),
            bind_source(self, "models.move_recovery", self.relocation_recovery_button, "Storage",
                        move=True, keywords=("move", "recover", "interrupted")),
            bind_source(self, "models.prepare_fast", self.prepare_fast_button, "Storage",
                        move=True, keywords=("prepare", "fast", "ssd", "copy")),
            bind_source(self, "models.prepare_results", self.prepare_batch_button, "Storage",
                        move=True, keywords=("prepare", "registered", "batch")),
            bind_source(self, "models.usage_placement", self.usage_placement_button, "Storage",
                        move=True, keywords=("usage", "favorite", "placement", "fast", "history")),
        )
        if self.collect_for_import:
            allowed = {"models.inspect_file", "models.known_folders", "models.preview_folder",
                       "models.reopen_file_review", "models.reload", "models.provider_query", "models.find_metadata"}
            return tuple(command for command in commands if command.key in allowed)
        return commands

    def _focus_provider_query(self):
        self.tabs.setCurrentIndex(2)
        self.query.setFocus(Qt.ShortcutFocusReason)

    def _prefill_model_metadata(self):
        """Stage only the selected full-file hash; network consent stays local."""
        if self._task is not None:
            return
        item = self.models.currentItem()
        identity = str(item.data(0, Qt.UserRole) or "") if item else ""
        import re
        if not re.fullmatch(r"sha256:[0-9a-f]{64}", identity):
            self.status.setText("Choose one registered model with a full-file SHA-256 identity.")
            return
        self.provider.setCurrentIndex(self.provider.findData("civitai"))
        self.query_kind.setCurrentIndex(self.query_kind.findData("sha256"))
        self.query.setText(identity.removeprefix("sha256:"))
        self.network_consent.setChecked(False)
        self._provider_result = None
        self.provider_listings.clear()
        self.download_files.clear()
        self.provider_details.setPlainText("Read cached metadata offline, or allow this exact hash lookup to contact Civitai. Your model file is not uploaded. Returned claims remain separate from compatibility and your notes.")
        self.dialog_commands.select_group("Provider Evidence")
        self.tabs.setCurrentIndex(2)
        self._update_provider_model_scope()
        self.status.setText("Selected model hash prepared. Read Cached Evidence or allow Look Up Entered Query.")
        self.provider_cached.setFocus()

    def _ribbon_group_changed(self, group):
        if group == "Start" and self._index_preview is not None and self.tabs.currentIndex() == 4:
            self._start_return_file_review = True
        if group == "Planning" and getattr(self, "_active_ribbon_group", "") != "Planning":
            self._planning_browser_open = False
            if hasattr(self, "planning_browser_button"):
                self.planning_browser_button.setChecked(False)
        self._active_ribbon_group = group
        if self.collect_for_import:
            context = (
                "Collect exact file or download choices for the combined import review. "
                "Adding choices does not register, download or move models."
            )
        else:
            context = {
                "Start": (
                    "Choose a model task below. Existing files are reviewed before registration; "
                    "browsing, planning and storage use the same saved model collection."
                ),
                "Local Models": (
                    "Browse and verify registered models. To add models you already have, "
                    "inspect one file or review a known folder; registration keeps files in place."
                ),
                "Provider Evidence": (
                    "Review cached evidence first. Public lookups require explicit consent, do not upload "
                    "model files, and do not by themselves prove compatibility."
                ),
                "Planning": (
                    "Start from the training or generation goal, review the recommended model plan and "
                    "readiness, then customize exact resources only when needed."
                ),
                "Storage": (
                    self._storage_context_text()
                ),
            }.get(group, "")
        self.model_context.setText(context)
        self.model_context.setVisible(group not in {"Start", "Planning"})
        self.continue_generation_button.setVisible(group == "Planning" and self.generation_handoff is not None)
        local_detail = group in {"Local Models", "Planning"}
        # The ribbon owns the four workspace sections. The body retains only
        # selection-specific detail tabs while a local selection is in view.
        for index in range(self.tabs.count()):
            self.tabs.setTabVisible(index, local_detail and (index in (0, 1, self._imported_metadata_tab) or (index == 4 and self._index_preview is not None)))
        if group == "Local Models" and getattr(self, "_start_return_file_review", False):
            self._start_return_file_review = False
            if self._index_preview is not None:
                self.tabs.setCurrentIndex(4)
        if group == "Provider Evidence":
            self.tabs.setCurrentIndex(2)
        elif group == "Storage":
            self.tabs.setCurrentIndex(3)
        elif group in {"Local Models", "Planning"} and self.tabs.currentIndex() not in (
                0, 1, self._imported_metadata_tab, *([4] if self._index_preview is not None else [])):
            self.tabs.setCurrentIndex(0)
        self.tabs.tabBar().setVisible(local_detail)
        self._workspace_tab_changed(self.tabs.currentIndex())

    def _storage_context_text(self):
        if self._fast_recovery_count is None:
            return ("Fast-copy records could not be read. Preserve existing files and open Manage Fast Copies "
                    "and Recovery to inspect the error; do not start cleanup.")
        if self._fast_recovery_count:
            count = self._fast_recovery_count
            return (f"{count:,} interrupted Fast-copy operation{'s' if count != 1 else ''} need review. "
                    "Choose Manage Fast Copies and Recovery above; exact files and any resumption are reviewed separately.")
        return ("Choose a storage goal above: set locations, organize reviewed copies, prepare Fast "
                "copies, or recover interrupted work. File changes are previewed before confirmation.")

    def _workspace_tab_changed(self, index):
        local = index in (0, 1, getattr(self, "_imported_metadata_tab", -1))
        unselected = local and self.models.currentItem() is None
        if hasattr(self, "unselected_model_detail"):
            self.unselected_model_detail.setVisible(unselected)
            self.tabs.setVisible(not unselected)
        for widget in (self.search, self.count, getattr(self, "management_browser_panel", self.models), self.previous, self.next, self.use_button, self.detail):
            widget.setVisible(local and (widget is not self.detail or not unselected))
        self.selector_button.setVisible(local)
        self.help_button.setVisible(local)
        declaration = index == 4 or self._inspection is not None
        for widget in (self.family_label, self.family, self.role_label, self.role):
            widget.setVisible(declaration)
        self.cancel_work_button.setVisible(self._task is not None)
        self._update_empty_models_panel()
        self._update_provider_model_scope()
        self._update_registration_followup()
        self._update_start_panel()

    def _update_registration_followup(self):
        if not hasattr(self, "registration_followup"):
            return
        count = len(self._registered_group)
        local = (getattr(self, "_active_ribbon_group", "") == "Local Models"
                 and self.tabs.currentIndex() in (0, 1, self._imported_metadata_tab))
        visible = bool(count and local and not self.collect_for_import
                       and not self._registration_followup_dismissed
                       and self._single_file_review_path is None)
        self.registration_followup.setVisible(visible)
        if not visible:
            return
        if count > 256 or any(identity not in getattr(self, "_records", {})
                              for identity in self._registered_group):
            self.registration_followup_label.setText(
                "The last registration needs a smaller or refreshed selection before organization. "
                "Browse the exact saved models, then use Storage to review one model at a time.")
            self.registration_setup_button.hide()
            self.registration_organize_button.hide()
            return
        snapshot = getattr(self, "_settings_snapshot", {})
        configured = all(
            any(self.workspace.destination(snapshot, role=declaration["role"],
                                           family=declaration["family"], media_type=media)
                for media in ("Images", "Video"))
            for identity in self._registered_group
            for declaration in self._records[identity].get("declarations", ()))
        self.registration_followup_label.setText(
            "Files remain where they are. "
            + ("Set Folder Locations before reviewing optional organization."
               if not configured else
               "Organization is optional. Review exact files and saved destinations before moving anything."))
        self.registration_setup_button.setVisible(not configured)
        self.registration_setup_button.setEnabled(self._task is None)
        self.registration_organize_button.setVisible(configured and count <= 256)
        self.registration_organize_button.setEnabled(self._task is None)
        self.registration_organize_button.setText(
            f"Review Organization for {count:,} Registered Model{'s' if count != 1 else ''}…")

    def _update_empty_models_panel(self):
        if not hasattr(self, "empty_models_panel"):
            return
        local = self.tabs.currentIndex() in (0, 1, self._imported_metadata_tab)
        empty = (
            not self.collect_for_import
            and self._registry_loaded
            and self._task is None
            and getattr(self, "_active_ribbon_group", "") == "Local Models"
            and not getattr(self, "_rows", ())
            and not self.search.text().strip()
            and self.tabs.currentIndex() == 0
            and self._inspection is None
            and self._index_preview is None
            and self._review_resume is None
        )
        self.empty_models_panel.setVisible(empty)
        self.browser.setVisible(not empty and local)
        self.management_view_button.setVisible(not empty and local and not self.collect_for_import
                                               and getattr(self, "_active_ribbon_group", "") != "Planning")
        self.management_folder_scope.setVisible(not empty and local and
                                                bool(self.browser.active_folder_scope()[0]) and
                                                not self._management_view_chrome_visible)
        self.management_details_panel.setVisible(not empty)
        self.previous.setVisible(not empty)
        self.next.setVisible(not empty)
        self.search.setVisible(local and not empty)
        self.count.setVisible(local and not empty)
        self.help_button.setVisible(local and not empty)
        self.selector_button.setVisible(local and not empty)
        self.use_button.setVisible(local and not empty)
        self.empty_inspect_button.setEnabled(self._task is None and self.inspect_button.isEnabled())
        self.empty_folder_button.setEnabled(self._task is None and self.index_button.isEnabled())
        self.empty_swarm_button.setEnabled(self._task is None and self.import_plan_button.isEnabled())
        self._update_start_panel()

    def _update_provider_model_scope(self):
        if not hasattr(self, "provider_model_scope"): return
        if self._companion_handoff is not None:
            handoff = self._companion_handoff
            self.provider_model_scope.setText(f"Required resource: {handoff.slot} · {handoff.role or 'Type unknown'} / {handoff.family or 'Family unknown'}. Review the exact download, register it, then return. Compatibility is checked separately.")
            return
        item = self.models.currentItem()
        self.provider_model_scope.setText(
            f"Evidence attachment target: {item.text(0)}" if item else "No local model selected for evidence attachment."
        )
        self.provider_model_scope.setToolTip(str(item.data(0, Qt.UserRole) or "") if item else "")
        self.provider_model_scope.setVisible(self.tabs.currentIndex() == 2)

    def _open_selector(self):
        from .model_selector_dialog import ModelSelectorDialog
        dialog = ModelSelectorDialog(self.registry, self.jobs, parent=self, role="",
            protected_roots=self.workspace.protected_roots, allow_manage=False, settings=self.settings)
        if dialog.exec() != QDialog.Accepted:
            return
        self.role.setCurrentText(dialog.selected_role)
        self.family.setText(dialog.selected_family)
        self.search.setText(dialog.selected_resource_id)
        self._selector_resource_id = dialog.selected_resource_id
        self._load()

    def _open_upscaler_models(self):
        if self._task is not None:
            return
        from .resolution_models_dialog import UpscalerModelsDialog
        dialog = UpscalerModelsDialog(
            self.registry,
            self.jobs,
            self,
            protected_roots=self.workspace.protected_roots,allow_select=False,
        )
        dialog.exec()
        self._load()

    def _build_provider_tab(self):
        tab = QWidget(); layout = QVBoxLayout(tab)
        row = QHBoxLayout()
        self.provider = QComboBox()
        self.provider.addItem("Civitai", "civitai"); self.provider.addItem("Hugging Face", "hugging_face")
        self.query_kind = QComboBox()
        for label, kind in (("Model ID", "model_id"), ("Version ID", "version_id"), ("URL", "url"), ("Full SHA-256", "sha256"), ("Repository ID", "repo_id")):
            self.query_kind.addItem(label, kind)
        self.query = QLineEdit(); self.query.setPlaceholderText("Enter a public model ID, link, hash, repository or search terms")
        self.query.setMaxLength(1000)
        for widget in (self.provider, self.query_kind, self.query): row.addWidget(widget)
        layout.addLayout(row)
        self.network_consent = QCheckBox("Send only this entered query to the selected provider (including this hash if chosen)")
        layout.addWidget(self.network_consent)
        hint = QLabel("Enter public model terms only. Never paste Local Only information. Cached evidence remains available offline; browsing cannot install or run a model.")
        hint.setWordWrap(True); layout.addWidget(hint)
        actions_group = QGroupBox("Provider evidence actions")
        actions_group.setObjectName("providerEvidenceActions")
        buttons = QHBoxLayout(actions_group)
        self.provider_cached = QPushButton("Read Cached Evidence")
        self.provider_lookup = QPushButton("Look Up Entered Query")
        self.provider_search = QPushButton("Search Provider")
        self.provider_open = QPushButton("Open Provider Page")
        self.download_button = QPushButton("Preview Selected Download…")
        self.provider_cached.clicked.connect(lambda: self._lookup(False))
        self.provider_lookup.clicked.connect(lambda: self._lookup(True))
        self.provider_search.clicked.connect(lambda: self._lookup(True, search=True))
        self.provider_open.clicked.connect(self._open_provider)
        self.download_button.clicked.connect(self._preview_download)
        for button in (self.provider_cached, self.provider_lookup, self.provider_search, self.provider_open, self.download_button): buttons.addWidget(button)
        layout.addWidget(actions_group)
        self.provider_listings = QComboBox()
        self.provider_listings.setAccessibleName("Provider model and version")
        self.provider_listings.currentIndexChanged.connect(self._provider_selection_changed)
        layout.addWidget(self.provider_listings)
        self.provider_details = QPlainTextEdit(); self.provider_details.setReadOnly(True)
        self.provider_details.setPlaceholderText("Attributed descriptions, versions, terms and safety evidence appear here.")
        layout.addWidget(self.provider_details, 1)
        self.download_files = QComboBox(); layout.addWidget(self.download_files)
        self.attach_evidence_button = QPushButton("Attach Matching Evidence to Selected Model")
        self.attach_evidence_button.clicked.connect(self._attach_provider_evidence)
        layout.addWidget(self.attach_evidence_button)
        self.tabs.addTab(tab, "Provider Evidence")

    def _build_storage_tab(self):
        tab = QWidget(); body = QVBoxLayout(tab); form = QFormLayout()
        body.addLayout(form); body.addStretch(1)
        intro = QLabel("Folder Locations sets destinations for acquisitions and reviewed organization. Register existing files first, then preview selected moves. File moves and engine folder settings are separate reviewed actions.")
        intro.setWordWrap(True); form.addRow(intro)
        self.default_destination = QLineEdit()
        self.default_destination.setReadOnly(True)
        self.setup_folders_button = QPushButton("Setup Folders…")
        self.setup_folders_button.clicked.connect(self._choose_destination)
        row = QHBoxLayout(); row.addWidget(self.default_destination); row.addWidget(self.setup_folders_button)
        form.addRow("Default model location", row)
        self.destination_overrides = QPlainTextEdit()
        self.destination_overrides.setReadOnly(True)
        self.destination_overrides.setMaximumHeight(90)
        form.addRow("Purpose / family folders", self.destination_overrides)
        self.save_storage_button = QPushButton("Setup Folders…")
        self.save_storage_button.clicked.connect(self._save_storage)
        self.save_storage_button.hide()
        self.storage_summary = QLabel(); self.storage_summary.setWordWrap(True); self.storage_summary.setTextFormat(Qt.PlainText)
        form.addRow(self.storage_summary)
        self.organize_button = QPushButton("Organize Selected Model…")
        self.organize_button.clicked.connect(lambda: self._open_relocation())
        self.organize_batch_button = QPushButton("Organize Registered Results…")
        self.organize_batch_button.clicked.connect(lambda: self._open_relocation(self._registered_group))
        self.relocation_recovery_button = QPushButton("Review Model Move Recovery…")
        self.relocation_recovery_button.clicked.connect(lambda: self._open_relocation((), recovery=True))
        for button in (self.organize_button, self.organize_batch_button, self.relocation_recovery_button): form.addRow(button)
        cache_note = QLabel("Archive + Fast Storage keeps originals and prepares separate copies for use. "
            "After preparation, review SwarmUI Model Folders in Setup before using them in SwarmUI. "
            "Manual preparation does not establish companion completeness or run compatibility.")
        cache_note.setWordWrap(True); form.addRow(cache_note)
        self.prepare_fast_button = QPushButton("Prepare Selected Model for Use…")
        self.prepare_fast_button.clicked.connect(lambda: self._open_cache())
        self.prepare_batch_button = QPushButton("Prepare Registered Results…")
        self.prepare_batch_button.clicked.connect(lambda: self._open_cache(self._registered_group))
        self.manage_fast_button = QPushButton("Manage Fast Copies and Recovery…")
        self.manage_fast_button.clicked.connect(lambda: self._open_cache((), recovery=True))
        for button in (self.prepare_fast_button, self.prepare_batch_button, self.manage_fast_button): form.addRow(button)
        self.usage_placement_button = QPushButton("Model Usage and Placement…")
        self.usage_placement_button.clicked.connect(self._open_usage_placement)
        form.addRow(self.usage_placement_button)
        self.recover_download_button = QPushButton("Inspect Retained Acquisition…")
        self.recover_download_button.clicked.connect(self._recover_download)
        form.addRow(self.recover_download_button)
        self.tabs.addTab(tab, "Storage && Recovery")

    def _load(self):
        if self._closing or self._task is not None: return
        self._single_file_review_path = None
        self._single_file_browser_open = False
        self._registry_loaded = False
        self._update_empty_models_panel()
        self._stash_notes()
        current = self.models.currentItem()
        if current and not self._selector_resource_id:
            self._selector_resource_id = current.data(0, Qt.UserRole)
        self._note_identity = ""
        self._inspection = None; self._index_preview = None
        def read(job):
            from core.model_selection import swarm_evidence_details
            before = self._registry_file_signature()
            snapshot, token = self.registry.snapshot()
            rows = []
            for identity, record in snapshot["resources"].items():
                if job.cancel_requested: raise InterruptedError("Reading models was cancelled.")
                name = Path(record["locations"][0]["path"]).name if record["locations"] else identity
                family = ", ".join(sorted({f"{d['role']} / {d['family']}" for d in record["declarations"]}))
                search = json.dumps([name, identity, record["declarations"], record.get("user", {}), record.get("provider_evidence", []), swarm_evidence_details(record)], ensure_ascii=False).casefold()
                rows.append((name, family, len(record["locations"]), identity, search))
            rows.sort(key=lambda r: (r[0].casefold(), r[3]))
            browser_rows = _management_browser_rows(snapshot, token)
            index = ModelLineageIndex(snapshot, token, should_cancel=lambda:job.cancel_requested)
            signature = self._registry_file_signature()
            if signature != before:
                raise RuntimeError("The model registry changed while loading. Reload it to inspect one consistent snapshot.")
            return snapshot, rows, token, index, signature, browser_rows
        self._run("Reading local model collection", read, self._workspace_loaded)

    def _workspace_loaded(self, result):
        snapshot, rows, token, index, signature, self._management_rows = result
        self._registry_loaded = True
        self._browser_query = None
        self._lineage_index = index
        self._registry_signature = signature
        self._rows_by_id = {row[3]:row for row in rows}
        self._records = snapshot["resources"]
        self._settings_snapshot = snapshot
        missing_selection = bool(self._selector_resource_id and self._selector_resource_id not in self._records)
        if self._related_identity and self._related_identity not in self._records:
            # A removed related resource must not strand the user outside the
            # original search. Keep its draft until explicit discard/close.
            if self._lineage_history:
                _, query, page, identity, _, _ = self._lineage_history[0]
                blocker = QSignalBlocker(self.search); self.search.setText(query); del blocker
                self._page = page
                self._selector_resource_id = identity
            self._related_identity = ""
            self._lineage_history.clear()
        # Publish one consistent page without redundant intermediate list resets.
        self._rows, self._snapshot_token = rows, token
        self.recover_button.hide()
        self.status.setText("Model list loaded. A saved model is verified again before use.")
        self._render()
        selected_id = getattr(self, "_selector_resource_id", "")
        self._selector_resource_id = ""
        for index in range(self.models.topLevelItemCount()):
            item = self.models.topLevelItem(index)
            if item.data(0, Qt.UserRole) == selected_id:
                self.models.setCurrentItem(item)
                break
        values = snapshot.get("workspace", {})
        self.default_destination.setText(values.get("default_destination", ""))
        from .folder_locations import model_folder_summary
        self.destination_overrides.setPlainText(model_folder_summary(values.get("destination_overrides", {}), values.get("family_destinations", {})))
        self.storage_summary.setText("Indexed folders: " + ("; ".join(values.get("indexed_folders", [])) or "None. Preview a selected folder to start."))
        try:
            from core.model_use_leases import cache_state
            operations = cache_state(snapshot)["operations"]
            interrupted = sum(operation.get("phase") != "complete" for operation in operations.values())
        except (ValueError, RuntimeError, TypeError):
            interrupted = None
        self._fast_recovery_count = interrupted
        if self._active_ribbon_group == "Storage":
            self.model_context.setText(self._storage_context_text())
        self.start_storage_button.setText(
            f"Review {interrupted:,} Interrupted Fast Operation{'s' if interrupted != 1 else ''}…"
            if interrupted is not None and interrupted else
            "Inspect Fast Storage Records…" if interrupted is None else "Manage Model Storage")
        if self._known_folder_context and self._known_folder_context.get("registered"):
            self.status.setText("Reviewed model files registered in the shared inventory; existing copies were reconciled. Back to Model Selection restores your earlier context.")
        if missing_selection:
            self.status.setText("The previously selected resource is no longer registered. The original search was restored; pending notes are retained until this window closes.")
        if getattr(self, "_registration_message", ""):
            self.status.setText(self._registration_message)
            self.count.setText(self._registration_message)
            self._registration_message = ""
        if getattr(self, "_relocation_message", ""):
            self.status.setText(self._relocation_message)
            self._relocation_message = ""
        if getattr(self, "_cache_message", ""):
            self.status.setText(self._cache_message)
            self._cache_message = ""
        self.resume_review_button.setVisible(self._review_resume is not None)
        if self._start_on_first_load:
            self._start_on_first_load = False
            if rows and hasattr(self, "dialog_commands"):
                self.dialog_commands.select_group("Start")
        if getattr(self, "_start_import_return", False):
            self._start_import_return = False
            child = getattr(self, "_import_plan_dialog", None)
            if child is not None and not child.has_changed and not self._closing:
                self.dialog_commands.select_group("Start")
                self.start_add_panel.show()
        if self._initial_folder_picker:
            self._initial_folder_picker = False
            QTimer.singleShot(0, self._choose_folder)
        if self._initial_known_swarm_folders:
            self._initial_known_swarm_folders = False
            QTimer.singleShot(0, lambda: self._open_known_folders(preloaded_swarm=True))
        if self._initial_import_review is not None and self._scan_review_draft is None:
            QTimer.singleShot(0, self._open_import_choice_review)
        if self._initial_metadata_resource:
            self._initial_metadata_resource = ""
            QTimer.singleShot(0, self._prefill_model_metadata)

    def _render(self):
        self._stash_notes()
        if hasattr(self, "browser"):
            self._render_management_browser(); return
        if self._related_identity and hasattr(self,"_rows_by_id"):
            self.models.clear()
            row = self._rows_by_id.get(self._related_identity)
            if row:
                name,family,count,identity,_ = row
                item = QTreeWidgetItem([name,family,str(count)])
                item.setData(0,Qt.UserRole,identity); item.setToolTip(0,identity)
                self.models.addTopLevelItem(item)
            self.count.setText("Inspecting a related model · Original search and page retained")
            self._update_actions()
        else:
            super()._render()
        self.count.setText(self.count.text().replace("registered base model(s)", "registered models"))

    def _render_management_browser(self):
        query = self.search.text().strip().casefold()
        message = ""
        if query != self._browser_query:
            candidates = [self._management_rows[row[3]] for row in self._rows
                          if row[3] in self._management_rows and (not query or query in row[4] or query in self._management_rows[row[3]].search_text)]
            try: self.browser.set_rows(candidates)
            except ValueError as exc:
                self.browser.set_rows(()); message = str(exc) + " Narrow the search to continue."
            self._browser_query = query
        self._update_management_folder_scope()
        rows = self.browser.visible_rows()
        folder_notice = self.browser.folder_limit_notice()
        self.browser.summary.setText(folder_notice)
        self.browser.summary.setVisible(bool(folder_notice))
        if self._selector_resource_id and not self._related_identity:
            self._page = next((position // self.PAGE_SIZE for position, row in enumerate(rows)
                               if row.resource_id == self._selector_resource_id), self._page)
        if self._related_identity:
            related = self._management_rows.get(self._related_identity)
            shown = (related,) if related else ()
        else:
            self._page = min(self._page, max(0, (len(rows) - 1) // self.PAGE_SIZE))
            shown = rows[self._page * self.PAGE_SIZE:(self._page + 1) * self.PAGE_SIZE]
        current = self.models.currentItem(); old_identity = current.data(0, Qt.UserRole) if current else ""
        preview, inspection = self._index_preview, self._inspection
        assignment_controls = self.role.currentText(), self.family.text()
        self._browser_rendering = True; self.browser.begin_update()
        with QSignalBlocker(self.models):
            self.models.clear()
            for row in shown:
                item = QTreeWidgetItem([row.display_name, row.declaration_summary, str(len(row.locations)),
                    "Favorite" if row.favorite else "", getattr(row, "author", "") or "Unknown",
                    getattr(row, "version_label", "") or "Unknown", getattr(row, "source", "") or "Unknown",
                    f"{row.size_bytes / 1024**2:,.1f} MiB" if getattr(row, "size_bytes", 0) else "Unknown",
                    "Stored · verify to use" if row.locations else "No local location"])
                item.setData(0, Qt.UserRole, row.resource_id)
                item.setToolTip(0, row.resource_id); item.setToolTip(1, row.declaration_summary)
                self.models.addTopLevelItem(item)
            self.browser.show_page(shown)
        self._browser_rendering = False
        self._selection_changed()
        self._index_preview = preview
        with QSignalBlocker(self.role), QSignalBlocker(self.family):
            self.role.setCurrentText(assignment_controls[0]); self.family.setText(assignment_controls[1])
        current = self.models.currentItem()
        if current and current.data(0, Qt.UserRole) == old_identity: self._inspection = inspection
        self._filtered_count = len(rows)
        start = self._page * self.PAGE_SIZE
        self.count.setText(message or ("Inspecting a related model · Original search and page retained" if self._related_identity else
            f"{len(rows):,} matching models · {len(self._rows):,} registered · Showing {min(start + 1, len(rows)):,}–{min(start + self.PAGE_SIZE, len(rows)):,}"))
        self.previous.setEnabled(self._task is None and self._page > 0 and not self._related_identity)
        self.next.setEnabled(self._task is None and start + self.PAGE_SIZE < len(rows) and not self._related_identity)
        self._update_empty_models_panel()
        self._update_actions()

    def _selection_changed(self):
        if self._browser_rendering: return
        if self.models.currentItem() is not None and hasattr(self, "recheck_inspected_button"):
            self._single_file_review_path = None
            self._single_file_browser_open = False
            self._single_file_recheck_path = None
            self._single_file_recheck_identity = ""
            self.recheck_inspected_button.hide()
        self._stash_notes()
        self._index_preview = None
        super()._selection_changed()
        self._workspace_tab_changed(self.tabs.currentIndex())
        self._update_provider_model_scope()
        if not hasattr(self, "tags"): return
        item = self.models.currentItem()
        identity = item.data(0, Qt.UserRole) if item else ""
        record = self._records.get(identity, {})
        if hasattr(self, "imported_metadata"):
            from core.model_selection import swarm_evidence_details
            evidence = swarm_evidence_details(record)
            self.imported_metadata.setPlainText("\n\n".join(evidence) if evidence else
                "No SwarmUI metadata imported for this model. Select a registered model, then use Import SwarmUI Metadata in Provider Evidence. Your notes and declared model type/family remain separate.")
        declarations = {(item.get("role"), item.get("family")) for item in record.get("declarations", ())}
        if len(declarations) == 1:
            role, family = next(iter(declarations))
            self.role.setCurrentText(role); self.family.setText(family)
        user = record.get("user", {})
        baseline = (", ".join(user.get("tags", [])), user.get("notes", ""))
        draft = self._note_drafts.get(identity)
        self._note_identity = ""
        self.tags.setText(draft[0] if draft else baseline[0])
        self.notes.setPlainText(draft[1] if draft else baseline[1])
        self._note_baseline = baseline
        self._note_token = draft[2] if draft else self._snapshot_token
        self._note_identity = identity
        self._notes_changed()
        self._lineage_page = 0
        self._render_lineage()

    def _filter_changed(self):
        self._related_identity = ""
        self._lineage_history.clear()
        super()._filter_changed()

    def _registry_file_signature(self):
        try:
            stat = self.registry.path.stat()
            return (stat.st_dev,stat.st_ino,stat.st_size,stat.st_mtime_ns,stat.st_ctime_ns)
        except FileNotFoundError:
            return None

    def _lineage_current(self):
        try:
            current = self._registry_file_signature() == self._registry_signature
        except OSError:
            current = False
        if not current:
            self._reference_status("The registry changed or is unavailable. Cached details are retained; Reload before navigating. Pending notes are preserved.")
        return current

    def _reference_status(self, message):
        self.status.setText(message)
        # The shared footer derives a compact label. Changing the retained
        # source QLabel alone need not emit a layout event while it is hidden.
        for binding in getattr(self, "_status_footer_bindings", ()):
            binding._attach()

    def _stash_notes(self):
        identity = self._note_identity
        if not identity or not hasattr(self,"notes"): return
        values = (self.tags.text(),self.notes.toPlainText())
        if values != self._note_baseline:
            self._note_drafts[identity] = (*values,self._note_token)
        else:
            self._note_drafts.pop(identity,None)

    def _notes_changed(self):
        if not self._note_identity: return
        self._stash_notes()
        stale = self._note_identity in self._note_drafts and self._note_token != self._snapshot_token
        self.note_status.setText(
            "Pending notes came from an older registry snapshot. Copy them for comparison or discard to load saved notes; saving is blocked."
            if stale else f"Unsaved notes retained for {len(self._note_drafts)} model(s) while this window stays open."
            if self._note_drafts else "Notes match the loaded registry snapshot."
        )
        self._update_actions()

    def _discard_notes(self):
        identity = self._note_identity
        if identity not in self._note_drafts: return
        if QMessageBox.question(self,"Discard Pending Notes?",
            "Discard pending tags and notes for this model and load its saved values? Other models' pending notes remain.",
            QMessageBox.Yes | QMessageBox.No,QMessageBox.No) != QMessageBox.Yes: return
        self._note_identity = ""
        self._note_drafts.pop(identity,None)
        self._selection_changed()

    def _review_pending_notes(self):
        self._stash_notes()
        if self._task is not None or not self._note_drafts: return
        pending = {identity: {"tags": tags, "notes": notes, "original_snapshot": token,
            "registered_in_loaded_snapshot": identity in self._records}
            for identity, (tags, notes, token) in self._note_drafts.items()}
        self._run("Preparing pending model notes", lambda job: json.dumps(pending, indent=2, ensure_ascii=False),
            lambda raw: self._raw_details_ready(raw, title="Pending Model Notes",
                description="Copy pending notes before closing, including notes for resources no longer registered. This view does not save or discard them."))

    def _lineage_kind_changed(self):
        self._lineage_page = 0; self._render_lineage()

    def _change_lineage_page(self, delta):
        self._lineage_page = max(0,self._lineage_page+delta); self._render_lineage()

    def _render_lineage(self):
        if not hasattr(self,"lineage_controls"): return
        item = self.models.currentItem()
        identity = item.data(0,Qt.UserRole) if item else ""
        active = bool(identity and self._lineage_index)
        self.lineage_controls.setVisible(active)
        self.lineage.setMaximumHeight(110 if active else 16777215)
        if not active:
            self.lineage.setPlainText("Select a registered model to inspect its recorded relationships.")
            return
        self.lineage.setPlainText(self._lineage_index.summary(identity))
        kind = self.lineage_kind.currentData()
        rows,total = self._lineage_index.page(identity,kind,self._lineage_page)
        self.relationships.clear()
        for row in rows:
            entry = QTreeWidgetItem([row.label,row.identity]); entry.setData(0,Qt.UserRole,row)
            entry.setToolTip(0,row.label); entry.setToolTip(1,row.identity)
            self.relationships.addTopLevelItem(entry)
        self.lineage_count.setText(f"{total:,} recorded · Page {self._lineage_page+1}")
        self.lineage_previous.setEnabled(self._lineage_page>0)
        self.lineage_next.setEnabled((self._lineage_page+1)*ModelLineageIndex.PAGE_SIZE<total)
        self.lineage_back.setEnabled(bool(self._lineage_history))
        self.relationship_detail.setPlainText("Choose a row to inspect it." if total else "No entries recorded for this view. Absence does not prove there are no relationships.")
        self.open_relationship_button.setEnabled(False)
        if rows: self.relationships.setCurrentItem(self.relationships.topLevelItem(0))

    def _relationship_selected(self):
        item = self.relationships.currentItem()
        row = item.data(0,Qt.UserRole) if item else None
        reference = getattr(row, "training_reference", None)
        self.open_relationship_button.setText("Choose Referenced Package…" if reference else "Inspect Related Model")
        self.open_relationship_button.setEnabled(bool(row and (row.target or reference) and self._task is None))
        if row: self.relationship_detail.setPlainText(row.identity+"\n"+row.detail)

    def _open_relationship(self):
        item = self.relationships.currentItem()
        row = item.data(0,Qt.UserRole) if item else None
        if not row or self._task is not None or not self._lineage_current(): return
        if row.training_reference:
            self._open_training_reference(row.training_reference)
            return
        if not row.target: return
        if len(self._lineage_history)>=64:
            self.status.setText("Navigation limit reached. Use Back to Previous Model before following more relationships."); return
        current = self.models.currentItem()
        self._lineage_history.append((self._related_identity,self.search.text(),self._page,
            current.data(0,Qt.UserRole) if current else "",self.lineage_kind.currentIndex(),self._lineage_page))
        self._related_identity = row.target
        self._render()
        self.models.setCurrentItem(self.models.topLevelItem(0))
        self.tabs.setCurrentIndex(1)

    def _open_training_reference(self, reference):
        from core.model_training_reference import resolve_training_package_reference
        self._stash_notes()
        folder = QFileDialog.getExistingDirectory(self, "Choose the Referenced Public Training Package", "")
        if not folder:
            self._reference_status("Package inspection cancelled. Models selection and pending notes are retained.")
            return
        if not self._lineage_current(): return
        protected = self.workspace.protected_roots
        self._reference_inspecting = True
        self._run("Verifying the exact referenced training package…",
            lambda job: resolve_training_package_reference(self.registry, reference, folder,
                protected_roots=protected, should_cancel=lambda: job.cancel_requested),
            self._training_reference_ready)

    def _training_reference_ready(self, resolution):
        from core.model_training_reference import reference_anchor_current
        from core.training_package import MANIFEST_NAME, TrainingPackageResult
        from .training_package_dialog import TrainingPackageResultDialog
        self._reference_inspecting = False
        if not self._lineage_current(): return
        if resolution.status != "resolved":
            self._reference_status("Package unavailable: " + resolution.reason)
            return
        current = self.models.currentItem()
        if (not current or current.data(0, Qt.UserRole) != resolution.reference.model_id
                or self._snapshot_token != resolution.reference.registry_revision
                or not reference_anchor_current(resolution)):
            self._reference_status("The reference or package changed. Choose the referenced package again; Models state is retained.")
            return
        verified = resolution.verification
        result = TrainingPackageResult("current", verified.destination, verified.artifact_count,
            verified.destination / MANIFEST_NAME, verification=verified)
        dialog = TrainingPackageResultDialog(result, self, reference_inspection=True)
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        dialog.setWindowModality(Qt.WindowModal)
        self._reference_dialog = dialog
        dialog.finished.connect(lambda _result: self._reference_status(
            "Returned from the frozen package snapshot. Models selection and pending notes are retained."))
        dialog.show()
        self._reference_status("Exact referenced package verified. Reviewing a frozen snapshot; original libraries were not opened.")

    def _failed(self, message):
        if self._folder_scan_pending:
            self._folder_scan_pending = False
            self._folder_scan_recovery = True
            stopped = str(message).startswith("Stopped at a safe boundary")
            self.folder_summary.setText(
                "Folder review stopped. No files were registered. Retry this folder or choose another."
                if stopped else
                f"Folder review failed: {str(message)[:300]}\n"
                "No files were registered. Retry this folder or choose another.")
            self.status.setText(
                "Folder review stopped. Retry Folder Review or Choose Another Folder."
                if stopped else
                "Folder review failed. Retry Folder Review or Choose Another Folder.")
            self._update_actions()
            return
        if getattr(self, "_reference_inspecting", False):
            self._reference_inspecting = False
            if str(message).startswith("Stopped at a safe boundary"):
                message = "Package inspection cancelled. Models selection and pending notes are retained; choose the package again to retry."
            self._reference_status(str(message))
            return
        draft = self._registration_recheck_draft
        self._registration_recheck_draft = None
        if draft is not None:
            self._scan_review_draft = draft
            self._review_requires_check = True
            self._render_file_review()
            super()._failed(message)
            recheck_label = "Recheck Saved Files" if self._scan_review_path is not None else "Recheck Reviewed Files"
            self.status.setText(
                f"Registration did not finish: {message} Your checked files and assignments are retained. "
                f"Choose {recheck_label}, then review or uncheck held files before retrying."
            )
            return
        if self._single_file_register_pending and self._inspection is not None:
            self._single_file_recheck_path = self._inspection.path
            self._single_file_recheck_identity = self._inspection.resource_id
            self._single_file_register_pending = False
            super()._failed(message)
            self.recheck_inspected_button.show()
            self.detail.setText(
                f"{self._single_file_recheck_path.name}\nRegistration stopped: {message}\n"
                "Registration was not confirmed. Recheck this file, then review its model type and family before registering."
            )
            self.status.setText("Registration stopped. Recheck This File before retrying.")
            self._update_actions()
            return
        self._single_file_register_pending = False
        super()._failed(message)

    def _lineage_return(self):
        if not self._lineage_history or self._task is not None or not self._lineage_current(): return
        related,query,page,identity,kind,lineage_page = self._lineage_history.pop()
        self._related_identity = related
        blocker = QSignalBlocker(self.search); self.search.setText(query); del blocker
        self._page = page; self._render()
        for index in range(self.models.topLevelItemCount()):
            item = self.models.topLevelItem(index)
            if item.data(0,Qt.UserRole)==identity: self.models.setCurrentItem(item); break
        blocker = QSignalBlocker(self.lineage_kind); self.lineage_kind.setCurrentIndex(kind); del blocker
        self._lineage_page = lineage_page; self._render_lineage()

    def _show_raw_details(self):
        item = self.models.currentItem()
        if not item or self._task is not None: return
        identity,index = item.data(0,Qt.UserRole),self._lineage_index
        self._run("Preparing complete recorded details",lambda job:index.raw_details(identity),self._raw_details_ready)

    def _raw_details_ready(self, raw, *, title="Raw Model Details", description="Exact recorded fields from the loaded snapshot. Unknown fields are retained; paths and references are not opened."):
        dialog = QDialog(self); dialog.setWindowTitle(title); dialog.resize(960,720)
        body = QVBoxLayout(dialog)
        note = QLabel(description)
        note.setWordWrap(True); body.addWidget(note)
        text = QPlainTextEdit(); text.setReadOnly(True); body.addWidget(text,1)
        footer = QHBoxLayout(); previous = QPushButton("Previous Text"); following = QPushButton("Next Text"); count = QLabel()
        close = QPushButton("Close"); close.clicked.connect(dialog.accept)
        for widget in (previous,following,count): footer.addWidget(widget)
        footer.addStretch(1); footer.addWidget(close); body.addLayout(footer)
        page = [0]; size = 32768
        def render(delta=0):
            page[0] = max(0,min(page[0]+delta,max(0,(len(raw)-1)//size)))
            start = page[0]*size; text.setPlainText(raw[start:start+size])
            count.setText(f"Characters {start+1:,}–{min(start+size,len(raw)):,} of {len(raw):,}")
            previous.setEnabled(start>0); following.setEnabled(start+size<len(raw))
        previous.clicked.connect(lambda:render(-1)); following.clicked.connect(lambda:render(1)); render()
        dialog.setAttribute(Qt.WA_DeleteOnClose)
        self._raw_dialog = dialog; dialog.show()

    def _locate_exact_copy(self):
        item = self.models.currentItem()
        if self._task is not None or item is None: return
        identity = item.data(0, Qt.UserRole)
        if not identity.startswith("sha256:"): return
        self._stash_notes()
        dialog = ExactModelCopyDialog(self, identity, self._snapshot_token, self.workspace.protected_roots)
        self._exact_copy_dialog = dialog
        dialog.finished.connect(lambda _: self._exact_copy_finished(dialog))
        dialog.show()

    def _exact_copy_finished(self, dialog):
        from shiboken6 import isValid
        if not isValid(self): return
        if dialog._parent_closing:
            if dialog._close_parent: dialog._close_parent()
            return
        if self._closing or not self.isVisible(): return
        self._selector_resource_id = dialog.resource_id
        self._load()

    def _open_artwork(self):
        if self.collect_for_import or self._task is not None: return
        item = self.models.currentItem()
        if item is None: return
        prior = getattr(self, "_artwork_dialog", None)
        if prior is not None and prior.isVisible():
            prior.raise_(); prior.activateWindow(); return
        from .model_artwork_dialog import ModelArtworkDialog, artwork_service
        self._stash_notes()
        service = artwork_service(self.registry, self.settings, self.workspace.protected_roots)
        dialog = ModelArtworkDialog(service, self.jobs, item.data(0, Qt.UserRole), self, settings=self.settings,
            registry=self.registry, swarm_service_factory=self._swarm_artwork_service)
        self._artwork_dialog = dialog
        dialog.finished.connect(lambda _: self._load() if dialog.has_changed and not self._closing else None)
        dialog.setModal(True); dialog.show()

    def _swarm_artwork_service(self, artwork, snapshot):
        # Called only by the explicit matching action, never by Models browsing.
        from core.swarm_model_artwork import SwarmModelArtwork
        return SwarmModelArtwork(artwork, self._swarm_metadata_service(snapshot))

    def _request_artwork_close(self, continuation):
        dialog = getattr(self, "_artwork_dialog", None)
        return dialog.request_parent_close(continuation) if dialog is not None and dialog.isVisible() else True

    def _request_copy_close(self, continuation):
        from shiboken6 import isValid
        dialog = getattr(self, "_exact_copy_dialog", None)
        return dialog.request_parent_close(continuation) if dialog is not None and isValid(dialog) and dialog.isVisible() else True

    def _relocation_service(self, snapshot):
        from core.model_relocation import ModelRelocationService
        from core.model_selection import protected_model_roots
        from core.model_storage_policy import retained_fast_roots
        protected = tuple(dict.fromkeys((*self.workspace.protected_roots, *protected_model_roots(self.settings))))
        managed = [location["path"] for record in snapshot.get("resources", {}).values()
            if record.get("identity", {}).get("kind") == "managed_model_pack"
            for location in record.get("locations", ())]
        managed.extend(retained_fast_roots(snapshot))
        directory = str(getattr(self.settings, "ai_models_directory", "") or "")
        if directory and Path(directory).is_absolute():
            from core.ai_interrogation import WD_TAGGER_MODELS, PLAIN_LANGUAGE_MODELS
            managed.extend(str(Path(directory) / row[1]) for row in (*WD_TAGGER_MODELS.values(), *PLAIN_LANGUAGE_MODELS.values()))
        return ModelRelocationService(self.registry, protected_roots=protected, managed_roots=tuple(dict.fromkeys(managed)))

    def _open_relocation(self, resource_ids=None, *, recovery=False):
        if self._task is not None: return
        if resource_ids is None:
            item = self.models.currentItem()
            resource_ids = (item.data(0, Qt.UserRole),) if item else ()
        self._stash_notes()
        from .model_relocation_dialog import ModelRelocationDialog
        try:
            dialog = ModelRelocationDialog(self.registry, self.jobs, resource_ids, self,
                service_factory=self._relocation_service, setup_folders=self._choose_destination)
        except (ValueError, RuntimeError) as exc:
            self.status.setText(str(exc)); return
        self._relocation_dialog = dialog
        if recovery: dialog.tabs.setCurrentIndex(2)
        dialog.finished.connect(lambda _: self._relocation_finished(dialog))
        dialog.show()

    def _relocation_finished(self, dialog):
        from shiboken6 import isValid
        if not isValid(self): return
        if getattr(dialog, "_parent_closing", False):
            if dialog._close_parent: dialog._close_parent()
            return
        if not self._closing and self.isVisible():
            outcomes = tuple(getattr(dialog, "outcomes", ()))
            if outcomes:
                complete = sum(outcome.status == "complete" for outcome in outcomes)
                if complete == len(outcomes):
                    self._relocation_message = (
                        f"Organized {complete:,} selected model file{'s' if complete != 1 else ''}. "
                        "The saved locations are shown in Models; other copies and engine settings remain unchanged.")
                else:
                    self._relocation_message = (
                        f"Organized {complete:,} of {len(outcomes):,} selected model files. "
                        "Review the remaining results and exact Recovery before another move.")
                if (self._registered_group
                        and set(getattr(dialog, "resource_ids", ())) == set(self._registered_group)):
                    self._registration_followup_dismissed = (
                        complete == len(outcomes) == len(self._registered_group))
            self._load()

    def _request_relocation_close(self, continuation):
        from shiboken6 import isValid
        dialog = getattr(self, "_relocation_dialog", None)
        return dialog.request_parent_close(continuation) if dialog is not None and isValid(dialog) and dialog.isVisible() else True

    def _cache_service(self, snapshot):
        from core.model_cache import ModelCacheService
        from .model_companion_workflow import resource_boundaries
        protected, managed = resource_boundaries(snapshot, self.settings, self.workspace.protected_roots)
        return ModelCacheService(self.registry, protected_roots=protected, managed_roots=managed)

    def _open_required_resources(self):
        if self._task is not None: return
        item = self.models.currentItem()
        if item is None: return
        previous = getattr(self, "_companion_dialog", None)
        if previous is not None and previous.isVisible():
            previous.raise_(); previous.activateWindow(); return
        from .model_companion_workflow import open_companion_review
        self._stash_notes()
        self._companion_dialog = open_companion_review(self, self.registry, self.jobs,
            item.data(0, Qt.UserRole), settings=self.settings,
            protected_roots=self.workspace.protected_roots, setup_folders=self._choose_destination)
        dialog = self._companion_dialog
        dialog.finished.connect(lambda _: self._load() if not self._closing and not dialog._closing else None)

    def _request_companion_close(self, continuation):
        dialog = getattr(self, "_companion_dialog", None)
        return dialog.request_parent_close(continuation) if dialog is not None and dialog.isVisible() else True

    def request_parent_close(self, continuation):
        # ModelResourceDialog rejects only after its coordinated worker finishes.
        if not self.isVisible(): return True
        self.reject()
        return not self.isVisible()

    def _companion_guard(self, identity, *, role="", family="", path=None):
        if self._companion_handoff is None: return self.workspace.protected_roots
        from core.model_companion_readiness import validate_companion_acquisition
        from core.model_workspace import _path
        validate_companion_acquisition(self._companion_handoff, identity, role=role, family=family)
        guards = self._acquisition_roots()
        if path is not None: _path(path, guards)
        return guards

    def _acquisition_roots(self):
        if self._companion_handoff is None: return self.workspace.protected_roots
        from core.model_storage_policy import retained_fast_roots
        from .model_companion_workflow import resource_boundaries
        snapshot, _ = self.registry.snapshot()
        protected, managed = resource_boundaries(snapshot, self.settings, self.workspace.protected_roots)
        return (*protected, *managed, *retained_fast_roots(snapshot))

    def _open_cache(self, resource_ids=None, *, recovery=False, initial_operation_id=None, parent=None):
        if self._task is not None: return
        from shiboken6 import isValid
        previous = getattr(self, "_cache_dialog", None)
        if previous is not None and isValid(previous) and previous.isVisible():
            if initial_operation_id is not None:
                raise ModelResourceError("Close the other Fast window before reviewing this import's exact operation.")
            previous.raise_(); previous.activateWindow(); return
        if resource_ids is None:
            item = self.models.currentItem()
            resource_ids = (item.data(0, Qt.UserRole),) if item else ()
        self._stash_notes()
        from .model_cache_dialog import ModelCacheDialog
        try:
            dialog = ModelCacheDialog(self.registry, self.jobs, resource_ids, parent or self,
                service_factory=self._cache_service, setup_folders=self._choose_destination,
                initial_recovery=recovery, initial_operation_id=initial_operation_id)
        except (ValueError, RuntimeError) as exc:
            self.status.setText(str(exc)); return
        self._cache_dialog = dialog
        dialog.finished.connect(lambda _: self._cache_finished(dialog))
        dialog.show()
        return dialog

    def _open_import_fast_recovery(self, operation_id, import_dialog):
        if self._closing or not self.isVisible():
            raise ModelResourceError("Models closed before Fast Recovery could open. Reopen the saved import to review its exact owner.")
        return self._open_cache((), recovery=True, initial_operation_id=operation_id, parent=import_dialog)

    def _cache_finished(self, dialog):
        from shiboken6 import isValid
        if not isValid(self): return
        if dialog._closing: return  # The child invokes its deferred parent continuation.
        if not self._closing and self.isVisible():
            outcomes = tuple(getattr(dialog, "outcomes", ()))
            if outcomes:
                from core.model_use_leases import cache_state
                outcome = outcomes[-1]
                try:
                    snapshot, _ = self.registry.snapshot()
                    state = cache_state(snapshot)
                    operation = state["operations"].get(outcome.operation_id, {})
                    if operation.get("kind") == "cleanup":
                        retired = tuple(operation.get("retired", ()))
                        reclaimed = sum(state["entries"][key]["size"] for key in retired)
                        self._cache_message = (
                            f"Fast cleanup removed {len(retired):,} reviewed cop{'y' if len(retired) == 1 else 'ies'} "
                            f"and reclaimed {reclaimed / 2**20:,.2f} MiB. Archive originals remain. "
                            + ("Open Manage Fast Copies and Recovery for the held remainder."
                               if outcome.status != "complete" else "The saved Models collection has been reloaded."))
                except (KeyError, TypeError, ValueError, RuntimeError):
                    pass  # The owning cache dialog retains the exact recorded result.
            self._load()

    def _request_cache_close(self, continuation):
        from shiboken6 import isValid
        dialog = getattr(self, "_cache_dialog", None)
        return dialog.request_parent_close(continuation) if dialog is not None and isValid(dialog) and dialog.isVisible() else True

    def _open_usage_placement(self):
        if self._task is not None: return
        from shiboken6 import isValid
        previous = getattr(self, "_usage_dialog", None)
        if previous is not None and isValid(previous) and previous.isVisible():
            previous.raise_(); previous.activateWindow(); return
        from .model_usage_dialog import ModelUsageDialog, _free_space
        from .model_companion_workflow import resource_boundaries
        from core.model_usage import ModelUsageService
        self._stash_notes()
        protected, _ = resource_boundaries(self._settings_snapshot, self.settings, self.workspace.protected_roots)
        def space(snapshot):
            protected, managed = resource_boundaries(snapshot, self.settings, self.workspace.protected_roots)
            return _free_space(snapshot, protected_roots=(*protected, *managed))
        def retention(changes, token):
            from core.model_storage_policy import save_policy
            snapshot, _ = self.registry.snapshot()
            protected, managed = resource_boundaries(snapshot, self.settings, self.workspace.protected_roots)
            return save_policy(self.registry, changes, expected_registry=token,
                protected_roots=protected, managed_roots=managed)
        dialog = ModelUsageDialog(self.registry, self.jobs, self,
            service=ModelUsageService(self.registry, protected_roots=protected), space_reader=space,
            policy_writer=retention)
        self._usage_dialog = dialog
        handoff = {}
        def prepare(identity):
            handoff["resource"] = identity; dialog.reject()
        def folders():
            handoff["folders"] = True; dialog.reject()
        def finished(_):
            if not isValid(self) or not self.isVisible() or dialog.parent_closing: return
            if "resource" in handoff: self._open_cache((handoff["resource"],))
            elif handoff.get("folders"): self._choose_destination()
            elif dialog.changed: self._load()
        dialog.prepare_requested.connect(prepare)
        dialog.folders_requested.connect(folders)
        dialog.finished.connect(finished)
        dialog.show()

    def _request_usage_close(self, continuation):
        from shiboken6 import isValid
        dialog = getattr(self, "_usage_dialog", None)
        return dialog.request_parent_close(continuation) if dialog is not None and isValid(dialog) and dialog.isVisible() else True

    def _open_known_folders(self, *, preloaded_swarm=False):
        if self._closing or self._task is not None or not hasattr(self, "_settings_snapshot"): return
        try:
            dialog = KnownModelFoldersDialog(self, preloaded_swarm=preloaded_swarm)
        except ValueError as exc:
            self.status.setText(str(exc)); return
        self._known_folders_dialog = dialog
        dialog.show()

    def _read_known_swarm_folders(self):
        if self._swarm_folder_reader is not None:
            return self._swarm_folder_reader()
        # Construction and connection are both deferred until an explicit read.
        from core.swarm_model_paths import SwarmModelPaths
        from core.swarm_runtime import SwarmRuntimeManager
        return SwarmModelPaths(SwarmRuntimeManager()).read()

    def _open_import_plan(self):
        if self._task is not None or self.collect_for_import or self._companion_handoff is not None: return
        previous = getattr(self, "_import_plan_dialog", None)
        if previous is not None and previous.isVisible():
            previous.raise_(); previous.activateWindow(); return
        from .model_import_plan_dialog import ModelImportPlanDialog
        self._stash_notes()
        self._closing_import_plan = False
        dialog = ModelImportPlanDialog(self.registry, self.jobs, self, settings=self.settings,
            protected_roots=self.workspace.protected_roots,
            open_fast_recovery=self._open_import_fast_recovery)
        self._import_plan_dialog = dialog
        dialog.finished.connect(lambda _: self._load() if not self._closing and not self._closing_import_plan else None)
        dialog.setup_requested.connect(lambda: self._open_import_setup(dialog))
        dialog.show()

    def _open_import_setup(self, dialog):
        """Suspend the unsaved import at the canonical SwarmUI Setup controls."""
        if (dialog is not getattr(self, "_import_plan_dialog", None)
                or not dialog._idle() or dialog._approval_id is not None):
            return
        host = self.parentWidget().window() if self.parentWidget() is not None else None
        if host is None or not callable(getattr(host, "open_swarm_setup", None)):
            dialog.status.setText("Setup is unavailable from this Models window. Keep this import open and return from the main Setup page.")
            return
        if (getattr(host, "_generation_setup_return", None) is not None
                or getattr(host, "_model_import_setup_return", None) is not None):
            dialog.status.setText("Another workflow is waiting in Setup. Return to it before opening this connection review.")
            return
        origin = host.navigation.currentRow()
        try:
            host.open_swarm_setup()
            panel = host.setup_page.swarm_runtime
        except (AttributeError, RuntimeError, ValueError, OSError) as exc:
            host.navigation.setCurrentRow(origin)
            dialog.status.setText(f"Import retained, but SwarmUI Setup could not open: {exc}")
            return

        def detach(*_args):
            if getattr(host,"_model_import_setup_return",None) is not resume:
                return
            host._model_import_setup_return = None
            panel.set_request_return_available(False)
            panel.return_requested.disconnect(resume)
            dialog.finished.disconnect(detach)
            self.finished.disconnect(detach)

        def resume():
            try:
                dialog.resume_from_setup()
            except (RuntimeError, ValueError, OSError) as exc:
                dialog.status.setText(f"Import retained, but its return needs attention: {exc}")
                return
            detach()
            host.navigation.setCurrentRow(origin)
            self._setup_suspended = False
            self.setWindowModality(Qt.WindowModal)
            self.show(); self.raise_(); self.activateWindow()
            dialog.show(); dialog.raise_(); dialog.activateWindow()
            dialog.read_source.setFocus()

        host._model_import_setup_return = resume
        panel.return_requested.connect(resume)
        dialog.finished.connect(detach)
        self.finished.connect(detach)
        panel.set_request_return_available(True, context="model_import")
        self._setup_suspended = True
        dialog.hide(); self.hide()

    def _request_import_plan_close(self, continuation):
        dialog = getattr(self, "_import_plan_dialog", None)
        if dialog is None or not dialog.isVisible(): return True
        self._closing_import_plan = True
        return dialog.request_parent_close(continuation)

    def _swarm_metadata_service(self, snapshot):
        from core.swarm_model_import import SwarmModelImport
        from core.swarm_model_paths import SwarmModelPaths
        from core.swarm_runtime import SwarmRuntimeManager
        from core.model_storage_policy import retained_fast_roots
        from .model_companion_workflow import resource_boundaries
        protected, managed = resource_boundaries(snapshot, self.settings, self.workspace.protected_roots)
        workspace = ModelWorkspace(self.registry,
            protected_roots=(*protected, *managed, *retained_fast_roots(snapshot)))
        return SwarmModelImport(workspace, SwarmModelPaths(SwarmRuntimeManager()))

    def _open_swarm_metadata_import(self):
        if self._task is not None or self._companion_handoff is not None: return
        item = self.models.currentItem()
        if item is None: return
        previous = getattr(self, "_swarm_import_dialog", None)
        if previous is not None and previous.isVisible():
            previous.raise_(); previous.activateWindow(); return
        from .swarm_model_import_dialog import SwarmModelImportDialog
        self._stash_notes()
        dialog = SwarmModelImportDialog(self.registry, self.jobs, (item.data(0, Qt.UserRole),), self,
            service_factory=self._swarm_metadata_service)
        self._swarm_import_dialog = dialog
        dialog.finished.connect(lambda _: self._load() if not self._closing and not dialog._closing else None)
        dialog.show()

    def _request_swarm_import_close(self, continuation):
        dialog = getattr(self, "_swarm_import_dialog", None)
        return dialog.request_parent_close(continuation) if dialog is not None and dialog.isVisible() else True

    def _open_swarm_onboarding(self):
        if not self.swarm_onboarding_button.isEnabled(): return
        previous = getattr(self, "_swarm_onboarding_dialog", None)
        if previous is not None and previous.isVisible():
            previous.raise_(); previous.activateWindow(); return
        from .swarm_onboarding_workflow import open_swarm_onboarding
        self._stash_notes()
        item = self.models.currentItem()
        identities = (item.data(0, Qt.UserRole),) if item is not None else ()
        dialog = open_swarm_onboarding(self, self.registry, self.jobs, settings=self.settings,
            protected_roots=self.workspace.protected_roots, resource_ids=identities)
        self._swarm_onboarding_dialog = dialog
        dialog.finished.connect(lambda _: self._load() if not self._closing else None)

    def _request_swarm_onboarding_close(self, continuation):
        dialog = getattr(self, "_swarm_onboarding_dialog", None)
        return dialog.request_parent_close(continuation) if dialog is not None and dialog.isVisible() else True

    def _preview_known_folder(self, path):
        if self._task is not None: return
        if self._known_folder_context is None:
            item = self.models.currentItem()
            self._known_folder_context = dict(query=self.search.text(), page=self._page,
                identity=item.data(0, Qt.UserRole) if item else "", related=self._related_identity,
                history=list(self._lineage_history), tab=self.tabs.currentIndex(),
                kind=self.lineage_kind.currentIndex(), lineage_page=self._lineage_page,
                role=self.role.currentText(), family=self.family.text())
        self._stash_notes()
        self.return_from_folder_button.show()
        self.preview_folder(path)

    def _return_from_known_folder(self):
        context = self._known_folder_context
        if not context or self._task is not None: return
        self._known_folder_context = None
        self.return_from_folder_button.hide()
        self._scan_review_draft = None
        self._scan_review_path = self._scan_review_digest = None
        self._review_requires_check = False
        self._index_preview = None
        self._review_resume = None
        self.tabs.setTabVisible(4, False)
        self._related_identity = context["related"]
        self._lineage_history = context["history"]
        blocker = QSignalBlocker(self.search); self.search.setText(context["query"]); del blocker
        self._page = context["page"]; self._render()
        for index in range(self.models.topLevelItemCount()):
            item = self.models.topLevelItem(index)
            if item.data(0, Qt.UserRole) == context["identity"]:
                self.models.setCurrentItem(item); break
        self.role.setCurrentText(context["role"]); self.family.setText(context["family"])
        self.lineage_kind.setCurrentIndex(context["kind"])
        self._lineage_page = context["lineage_page"]; self._render_lineage()
        self.tabs.setCurrentIndex(context["tab"])
        self.status.setText("Returned to your model selection. Pending notes are retained; unregistered folder previews were discarded.")
        self._update_actions()

    def _choose_folder(self):
        if self._closing or self._task is not None:
            return
        try:
            initial = self._configured_model_folder()
        except (OSError, ValueError, RuntimeError) as exc:
            self.status.setText(f"Configured folder unavailable: {exc}. Choose another model folder.")
            initial = ""
        root = QFileDialog.getExistingDirectory(self, "Choose a Model Folder to Preview", initial)
        if root: self.preview_folder(root)

    def _retry_folder_review(self):
        if self._closing or self._task is not None or not self._folder_scan_recovery:
            return
        if self._folder_scan_root is not None:
            self.preview_folder(self._folder_scan_root)

    def _configured_model_folder(self):
        """Use configured locations as chooser hints, never as scan permission."""
        from core.model_workspace import _path
        root = self.workspace.destination(getattr(self, "_settings_snapshot", {}),
            role=self.role.currentText(), family=self.family.text().strip())
        root = root or str(getattr(self.settings, "ai_models_directory", "") or "")
        return str(_path(root, self.workspace.protected_roots)) if root else ""

    def _choose_file(self):
        if self._closing or self._task is not None:
            return
        try:
            initial = self._configured_model_folder()
        except (OSError, ValueError, RuntimeError) as exc:
            self.status.setText(f"Configured folder unavailable: {exc}. Choose another model file.")
            initial = ""
        filename, _ = QFileDialog.getOpenFileName(self, "Inspect an Existing Model", initial,
            "Safe tensor model (*.safetensors)")
        if filename:
            self.inspect_path(filename)

    def inspect_path(self, filename, *, prior_identity=""):
        self.selected_companion_choice = None
        self._single_file_review_path = Path(filename)
        self._single_file_browser_open = False
        self._single_file_recheck_path = None
        self._single_file_recheck_identity = ""
        self._single_file_prior_identity = prior_identity
        self.recheck_inspected_button.hide()
        self.models.clearSelection()
        self.models.setCurrentItem(None)
        self._inspection = None
        self._index_preview = None
        def inspect(job):
            from core.model_workspace import _path
            from core.model_resource_registry import inspect_model_resource
            path = _path(filename, self._acquisition_roots())
            _, token = self.registry.snapshot()
            result = inspect_model_resource(path, progress=self._progress(job),
                should_cancel=lambda: job.cancel_requested)
            return result, token
        self._run("Inspecting selected model", inspect, self._inspected, unit="MiB")

    def _inspected(self, result):
        super()._inspected(result)
        self._single_file_review_path = self._inspection.path
        self._single_file_browser_open = False
        self.tabs.setCurrentIndex(0)
        self._workspace_tab_changed(0)
        prior_identity = self._single_file_prior_identity
        self._single_file_prior_identity = ""
        if prior_identity and prior_identity != self._inspection.resource_id:
            self.detail.setText(self.detail.text() + "\nIdentity changed since the previous inspection.")
            self.status.setText("File identity changed. Review the new identity and type/family before Register Model.")
        else:
            self.status.setText("File identity verified. Choose its declared model type/family and Register Model when ready.")
        self._update_start_panel()

    def _recheck_inspected_file(self):
        if self._task is not None or self._single_file_recheck_path is None:
            return
        self.inspect_path(str(self._single_file_recheck_path),
            prior_identity=self._single_file_recheck_identity)

    def _open_guidance(self, *, workflow_kind="training"):
        from core.model_workflow_guidance import ModelWorkflowGuidanceService, machine_capability_from_setup
        item = self.models.currentItem()
        identity = item.data(0, Qt.UserRole) if item else ""
        role, family = self.role.currentText(), self.family.text()
        def prepare(job):
            snapshot, _ = self.registry.snapshot()
            verified = ()
            if identity:
                # Model-only locations must pass privacy scope before hashing.
                from core.model_workspace import _path
                for location in snapshot["resources"][identity]["locations"]:
                    _path(location["path"], self.workspace.protected_roots)
                result = self.registry.resolve(identity, should_cancel=lambda: job.cancel_requested, progress=self._progress(job))
                if result.status == "identity_verified": verified = (identity,)
            machine = machine_capability_from_setup(self.settings, self.jobs,
                model_directory=self.workspace.destination(snapshot, role=role, family=family))
            local = self.registry.path.with_name("model_workflow_knowledge.json")
            service = ModelWorkflowGuidanceService(update_path=local)
            return service, machine, snapshot, verified
        self._run("Preparing model-aware guidance", prepare,
                  lambda result: self._guidance_ready(result, identity, workflow_kind=workflow_kind), unit="MiB")

    def _open_generation_plan(self):
        self._open_guidance(workflow_kind="generation")

    def _guidance_ready(self, result, identity, *, workflow_kind="training"):
        from core.training_configuration import TrainingConfiguration
        from qt_app.model_workflow_guidance_dialog import ModelWorkflowGuidanceDialog
        service, machine, snapshot, verified = result
        configuration = self.guidance_configuration
        if identity:
            configuration = replace(configuration or TrainingConfiguration(), base_model_id=identity)
        dialog = ModelWorkflowGuidanceDialog(service, machine, registry_snapshot=snapshot,
            verified_resource_ids=verified, current_configuration=configuration,
            registry=self.registry, jobs=self.jobs, protected_roots=self.workspace.protected_roots,
            workflow_kind=workflow_kind, allow_workflow_switch=not self.select_plan and workflow_kind != "generation",
            image_count=self.image_count, parent=self)
        if self.select_plan:
            for index in reversed(range(dialog.goal_kind.count())):
                if dialog.goal_kind.itemData(index) not in ("character", "style", "product", "concept"):
                    dialog.goal_kind.removeItem(index)
        kind_index = dialog.goal_kind.findData(self.guidance_goal_kind)
        if kind_index >= 0: dialog.goal_kind.setCurrentIndex(kind_index)
        target_index = dialog.target.findData(self.guidance_target_id)
        if target_index >= 0: dialog.target.setCurrentIndex(target_index)
        dialog.goal.setText(self.guidance_goal)
        if dialog.exec() != QDialog.Accepted: return
        self.selected_guidance = dialog.plan
        self.selected_guidance_configuration = dialog.selected_configuration()
        self.selected_generation_configuration = dialog.selected_generation_configuration()
        if self.select_plan:
            self.accept()
        else:
            self.guidance_goal = dialog.goal.text()
            self.guidance_goal_kind = dialog.plan.request.goal_kind
            self.guidance_target_id = dialog.plan.request.target_id
            self.generation_handoff = None
            self.continue_generation_button.hide()
            self._update_planning_summary()
            if dialog.plan.request.workflow_kind == "generation":
                if self.selected_generation_configuration is None:
                    self.status.setText("No generation configuration was accepted. Review the recommendation again before continuing.")
                    return
                current_snapshot, registry_revision = self.registry.snapshot()
                if dialog.registry_snapshot != current_snapshot:
                    self.status.setText("Registered models changed during review. Open Plan for a Model and review a fresh recommendation.")
                    return
                catalog_token = service.catalog_write_token()
                if catalog_token != service._loaded_token:
                    self.status.setText("Model guidance changed during review. Open Plan for a Model and review a fresh recommendation.")
                    return
                self.generation_handoff = (dialog.plan, self.selected_generation_configuration,
                                           registry_revision, service, catalog_token)
                self.continue_generation_button.show()
                self._update_planning_summary()
                self.status.setText("Generation setup reviewed but not applied. Continue in Generate to open a new draft; model readiness and generation remain separate.")
            else:
                self.guidance_configuration = self.selected_guidance_configuration
                self.status.setText("Guidance preview reviewed. Open Dataset → Plan Training Dataset → Models & Guidance to apply this model-specific profile to a frozen image selection.")

    def _continue_reviewed_generation(self):
        if self.generation_handoff is None or self._generation_handoff_ready:
            return
        if self._task is not None or self._closing:
            self.status.setText("Model work is still in progress. Finish or Stop it, then continue in Generate.")
            return
        from shiboken6 import isValid
        children = ("_artwork_dialog", "_import_plan_dialog", "_usage_dialog",
                    "_swarm_onboarding_dialog", "_swarm_import_dialog", "_companion_dialog",
                    "_cache_dialog", "_relocation_dialog", "_exact_copy_dialog",
                    "_known_folders_dialog")
        if any((child := getattr(self, name, None)) is not None and isValid(child)
               and child.isVisible() for name in children):
            self.status.setText("Finish the open Models review, then continue in Generate. Your recommendation remains here.")
            return
        self._stash_notes()
        if self._note_drafts:
            self.status.setText("Save or discard pending model notes, then continue in Generate. The reviewed setup remains here.")
            self.dialog_commands.select_group("Local Models")
            self.notes.setFocus(Qt.OtherFocusReason)
            return
        guidance, configuration, registry_revision, service, catalog_token = self.generation_handoff
        try:
            if (self.registry.snapshot()[1] != registry_revision or
                    service.catalog_write_token() != catalog_token):
                self.status.setText("Models or guidance changed. Open Plan for a Model and review a fresh recommendation before continuing.")
                return
            from .generation_workflow import _host, _setup_window
            host = _host(self)
            setup_window = _setup_window(host)
            if (getattr(host, "_generation_source_task", None) is not None or
                    callable(getattr(setup_window, "_image_setup_return", None)) or
                    callable(getattr(setup_window, "_generation_setup_return", None))):
                self.status.setText("Finish the current Generate or Setup return first, then continue with this reviewed setup.")
                return
        except (OSError, RuntimeError, ValueError) as exc:
            self.status.setText(f"Could not recheck the reviewed setup: {exc}. Review it again before continuing.")
            return
        self._generation_handoff_ready = True
        self.accept()
        if self.isVisible():
            self._generation_handoff_ready = False

    def preview_folder(self, root):
        if self._task is not None: return
        self._folder_scan_root = Path(root)
        self._folder_scan_pending = True
        self._folder_scan_recovery = False
        self._scan_review_draft = None
        self._registration_recheck_draft = None
        self._scan_review_path = self._scan_review_digest = None
        self._review_requires_check = False
        self._index_preview = None
        self._review_resume = None
        self._folder_assignments.clear(); self._folder_checked.clear()
        self._detections.clear(); self._folder_ids.clear()
        self.evidence_filter.setCurrentIndex(0)
        self._folder_page = 0
        self.family.clear(); self.role.setCurrentIndex(0)
        self.folder_files.clear()
        self.folder_summary.setText("Scanning the selected folder. No files are registered by scanning.")
        self.tabs.setTabVisible(4, True); self.tabs.setCurrentIndex(4)
        self._run("Previewing selected model folder", lambda job: self.workspace.preview_folder(root,
            should_cancel=lambda: job.cancel_requested, collect_detection=True,
            workflow_progress=lambda done, total, label: job.update(done, total or 0, label)), self._indexed, unit="files")

    def _indexed(self, preview):
        self._folder_scan_pending = False
        self._folder_scan_recovery = False
        self._index_preview = preview
        self._inspection = None
        self._folder_ids = {item.path: item.resource_id for item in preview.inspections}
        self._folder_page = 0
        folders = {Path(".")}
        for name in [*(str(item.path.relative_to(preview.root)) for item in preview.inspections), *(name for name, _ in preview.held)]:
            folder = Path(name).parent
            folders.add(folder); folders.update(folder.parents)
        blocker = QSignalBlocker(self.folder_scope)
        self.folder_scope.clear()
        for folder in sorted(folders, key=lambda path: (len(path.parts), str(path).casefold())):
            self.folder_scope.addItem("All scanned folders" if folder == Path(".") else str(folder), folder)
        del blocker
        self.tabs.setTabVisible(4, True); self.tabs.setCurrentIndex(4)
        self._render_file_review()
        self.status.setText("Scan complete. Assign Model Family and Model Type by folder or checked files, then register your checked files.")

    def _apply_index(self):
        preview = self._index_preview
        if self._closing or self._task is not None or preview is None or self._review_requires_check: return
        if not self._folder_checked.issubset({item.path for item in preview.inspections}): return
        selected = tuple(item for item in preview.inspections if item.path in self._folder_checked)
        if not selected or any(item.path not in self._folder_assignments for item in selected): return
        declarations = tuple(self._folder_assignments[item.path] for item in selected)
        checked = frozenset(self._folder_checked)
        matching = {path for _, path, _, _ in self._file_entries()}
        on_page = {self.folder_files.topLevelItem(index).data(0, Qt.UserRole)
                   for index in range(self.folder_files.topLevelItemCount())}
        review = SelectedModelFilesDialog(preview.root, selected, declarations,
            collect_for_import=self.collect_for_import,
            update_import_choice=self._initial_import_review is not None,
            checked_matching=len(checked & matching), checked_on_page=len(checked & on_page),
            held_count=len(preview.held), parent=self)
        self._selection_review_dialog = review
        accepted = review.exec() == QDialog.Accepted
        self._selection_review_dialog = None
        if not accepted: return
        if (self._closing or self._task is not None or self._index_preview is not preview
                or self._review_requires_check or self._folder_checked != checked
                or any(self._folder_assignments.get(item.path) != declaration
                       for item, declaration in zip(selected, declarations))):
            self.status.setText("File review changed. Review the checked files again before continuing.")
            return
        if self.collect_for_import:
            if self._initial_import_review is not None:
                try:
                    draft = self._current_scan_review()
                except (ValueError, RuntimeError, OSError) as exc:
                    self._failed(str(exc)); return
                self._collected_import_choice({"kind": "registration", "review": draft})
            else:
                from core.model_scan_review import ModelScanReviewService
                self._run("Adding reviewed files to import plan", lambda job: ModelScanReviewService(self.workspace).capture(
                    preview, declarations=declarations, selected_paths=tuple(item.path for item in selected)),
                    lambda draft: self._collected_import_choice({"kind": "registration", "review": draft}))
            return
        try:
            self._registration_recheck_draft = self._current_scan_review()
        except Exception as exc:
            self.status.setText(f"File review could not be retained for recovery: {exc}")
            return
        def saved(receipt):
            self._registration_recheck_draft = None
            self._registered_group = tuple(dict.fromkeys(item.resource_id for item in selected))
            self._registration_followup_dismissed = False
            chosen = {item.path for item in selected}
            remaining = tuple(item for item in preview.inspections if item.path not in chosen)
            self._review_resume = replace(preview, inspections=remaining, registry_token=receipt.registry_token) if remaining or preview.held else None
            self._folder_checked.difference_update(chosen)
            for path in chosen: self._folder_assignments.pop(path, None)
            if self._scan_review_draft is not None:
                self._scan_review_draft = replace(self._scan_review_draft,
                    registry_token=receipt.registry_token,
                    candidates=tuple(item for item in self._scan_review_draft.candidates if item.path not in chosen),
                    declarations=tuple(self._folder_assignments.values()),
                    selected_paths=tuple(sorted(self._folder_checked, key=str)))
            self._selector_resource_id = selected[0].resource_id
            self._related_identity = ""
            blocker = QSignalBlocker(self.search); self.search.clear(); del blocker
            self._registration_message = f"Registered {receipt.count:,} selected files. The registered result is selected below."
            if remaining: self._registration_message += f" {len(remaining):,} files remain in Return to File Review."
            self.tabs.setCurrentIndex(0)
            self._registered(receipt)
        self._run("Registering selected model files", lambda job: self.workspace.apply_index_selection(preview,
            declarations, should_cancel=lambda: job.cancel_requested), saved, unit="files")

    def _registered(self, _result):
        self.registration_applied = True
        if self._known_folder_context is not None:
            self._known_folder_context["registered"] = True
        self._load()

    def _return_selected_to_selector(self):
        if self._task is not None or not self._return_to_selector:
            return
        item = self.models.currentItem()
        identity = item.data(0, Qt.UserRole) if item is not None else ""
        if identity not in self._records:
            return
        self.selected_for_selector_id = identity
        self.accept()

    def _companion_registered(self, identity, inspection, role, family):
        self._single_file_register_pending = False
        if self._companion_handoff is not None:
            from core.model_companion_readiness import CompanionChoice
            self.selected_companion_choice = CompanionChoice(self._companion_handoff.slot,
                identity, str(inspection.path), role, family)
        self._selector_resource_id = identity
        self._registration_message = "Registered model file. The registered result is selected below."
        self._registered(identity)

    def _use(self):
        inspection = self._inspection
        item = self.models.currentItem()
        identity = item.data(0, Qt.UserRole) if item else ""
        role, family, token = self.role.currentText(), self.family.text(), self._snapshot_token
        if self.collect_for_import:
            if inspection is None or role not in RESOURCE_ROLES or not family.strip(): return
            from core.model_workspace import IndexPreview
            from core.model_scan_review import ModelScanReviewService
            preview = IndexPreview(inspection.path.parent, (inspection,), (), token)
            self._run("Adding inspected file to import plan", lambda job: ModelScanReviewService(self.workspace).capture(
                preview, declarations=(IndexDeclaration(inspection.path, role, family),), selected_paths=(inspection.path,)),
                lambda draft: self._collected_import_choice({"kind": "registration", "review": draft}))
            return
        def run(job):
            from core.model_workspace import _path
            if inspection is not None:
                self._companion_guard(inspection.resource_id, role=role, family=family, path=inspection.path)
                _path(inspection.path, self.workspace.protected_roots)
                return self.registry.register(inspection, role=role, family=family, expected_registry=token,
                    progress=self._progress(job), should_cancel=lambda: job.cancel_requested)
            for location in self._records.get(identity, {}).get("locations", []):
                _path(location["path"], self.workspace.protected_roots)
            result = self.registry.resolve(identity, progress=self._progress(job), should_cancel=lambda: job.cancel_requested)
            if result.status != "identity_verified": raise RuntimeError(result.reason)
            return result.resource_id
        self._single_file_register_pending = inspection is not None
        self._run("Verifying selected model identity", run, lambda identity: self._companion_registered(identity, inspection, role, family) if inspection else self.status.setText(f"Verified {identity}. Adapter compatibility remains a separate check."), unit="MiB")

    def _record_derivation(self):
        item = self.models.currentItem()
        if item is None: return
        output_id, token = item.data(0, Qt.UserRole), self._snapshot_token
        dialog = QDialog(self); dialog.setWindowTitle("Record Model Origins"); dialog.resize(750, 560)
        layout = QFormLayout(dialog)
        notice = QLabel("Record what created this exact output. Parent and output files will be verified. This is your attribution; it does not prove execution, compatibility or quality.")
        notice.setWordWrap(True); layout.addRow(notice)
        layout.addRow("Output", QLabel(output_id))
        parents = QPlainTextEdit(); parents.setPlaceholderText("One exact sha256: parent identity per line, in operation order")
        parents.setMaximumHeight(90); layout.addRow("Ordered parents", parents)
        operation = QComboBox(); operation.addItems(["fine_tune", "merge", "conversion", "quantization"])
        layout.addRow("Operation", operation)
        tool = QLineEdit(); version = QLineEdit()
        layout.addRow("Tool", tool); layout.addRow("Tool/runtime version", version)
        settings = QPlainTextEdit("{}"); settings.setMaximumHeight(90); layout.addRow("Exact settings (JSON)", settings)
        refs = QPlainTextEdit("[]"); refs.setMaximumHeight(70)
        refs.setPlaceholderText('[{"kind": "training_manifest", "identity": "sha256:…"}]')
        layout.addRow("Source/evaluation references", refs)
        buttons = QDialogButtonBox(QDialogButtonBox.Save | QDialogButtonBox.Cancel)
        buttons.accepted.connect(dialog.accept); buttons.rejected.connect(dialog.reject); layout.addRow(buttons)
        if dialog.exec() != QDialog.Accepted: return
        try:
            values, references = json.loads(settings.toPlainText()), json.loads(refs.toPlainText())
        except ValueError as exc: self.status.setText(str(exc)); return
        parent_ids = [line.strip() for line in parents.toPlainText().splitlines() if line.strip()]
        op, tool_name, tool_version = operation.currentText(), tool.text(), version.text()
        self._run("Verifying and recording attributed lineage", lambda job: self.workspace.record_derivation(output_id,
            parent_ids=parent_ids, operation=op, tool=tool_name, tool_version=tool_version,
            settings=values, references=references, expected_registry=token,
            should_cancel=lambda: job.cancel_requested), lambda _: self._load())

    def _save_notes(self):
        item = self.models.currentItem()
        if item is None: return
        identity, token = item.data(0, Qt.UserRole), self._snapshot_token
        self._stash_notes()
        if identity in self._note_drafts and self._note_token != token:
            self.status.setText("Pending notes belong to an older snapshot. Compare or discard them before saving; no record was changed.")
            return
        tags, notes = self.tags.text().split(","), self.notes.toPlainText()
        def saved(_):
            self._note_identity = ""
            self._note_drafts.pop(identity,None)
            self._selector_resource_id = identity
            self._load()
        self._run("Saving model notes", lambda job: self.workspace.save_notes(identity, tags=tags, notes=notes,
            expected_registry=token), saved)

    def _choose_destination(self):
        if self._task is not None:
            return
        from .folder_locations import open_folder_locations
        open_folder_locations(self, focus="default_destination", settings=self.settings,
                              registry=self.workspace.registry,
                              extra_protected_roots=self.workspace.protected_roots)
        try:
            from core.model_selection import protected_model_roots
            self.workspace.protected_roots = tuple(dict.fromkeys((
                *self.workspace.protected_roots, *protected_model_roots(self.settings))))
            snapshot, token = self.registry.snapshot()
            self._settings_snapshot = snapshot
            self._snapshot_token = token
            values = snapshot.get("workspace", {})
            self.default_destination.setText(values.get("default_destination", ""))
            from .folder_locations import model_folder_summary
            self.destination_overrides.setPlainText(model_folder_summary(values.get("destination_overrides", {}), values.get("family_destinations", {})))
            self._update_registration_followup()
        except (OSError, ValueError, RuntimeError) as exc:
            self.status.setText(str(exc))

    def _save_storage(self):
        self._choose_destination()

    def _lookup(self, online, search=False):
        from core.model_provider_evidence import ProviderEvidenceService, ProviderQuery
        query = ProviderQuery(self.provider.currentData(), "search" if search else self.query_kind.currentData(), self.query.text())
        consent = self.network_consent.isChecked()
        service = ProviderEvidenceService(self.registry.path.with_name("model_provider_cache.json"))
        def run(job):
            method = service.search if search else service.lookup
            return method(query, allow_network=online, confirmed=consent, cancelled=lambda: job.cancel_requested)
        self._run("Reading requested provider evidence", run, self._provider_loaded)

    def _provider_loaded(self, result):
        self._provider_result = result
        self.provider_listings.blockSignals(True)
        self.provider_listings.clear()
        for item in result.get("items", [])[:100]:
            name = item.get("model_name") or item.get("repo_id") or item.get("provider_model_id") or "Provider model"
            version = item.get("version_name") or item.get("revision") or item.get("version_id") or "Version unavailable"
            self.provider_listings.addItem(f"{name} · {version}", item)
        self.provider_listings.blockSignals(False)
        self._provider_selection_changed()
        self.status.setText("Provider evidence is attributed advice. Review version, license and content rating before choosing a file.")

    def _provider_selection_changed(self, *_):
        result = self._provider_result or {}
        item = self.provider_listings.currentData() or {}
        source = result.get("source", {})
        details = [f"Evidence: {result.get('state', result.get('status', 'unavailable'))} · {result.get('currentness', 'unknown')}",
            f"Retrieved: {source.get('retrieved_at', 'Unavailable')}",
            f"Creator: {item.get('creator') or 'Not supplied'}",
            f"Model Type: {item.get('role') or 'Unverified'} · Base family: {item.get('base_model') or 'Unverified'}",
            f"Availability: {item.get('availability') or 'Unknown'}",
            "License: " + str(item.get('license') or 'Not supplied'),
            "Terms: " + json.dumps(item.get('terms') or {}, ensure_ascii=False),
            "Content rating: " + json.dumps(item.get('content_rating') or {}, ensure_ascii=False),
            "Trigger guidance: " + ", ".join(item.get('trigger_words') or []),
            "\n" + str(item.get('description') or result.get('detail') or result.get('description') or 'No description supplied.'),
            "\n" + "\n".join(result.get('limitations', []))]
        if result.get("refresh"): details.append("Refresh: " + json.dumps(result["refresh"], ensure_ascii=False))
        if len(result.get("items", [])) > 100: details.append("First 100 versions shown; narrow the query to see additional versions.")
        self.provider_details.setPlainText("\n".join(details)[:64_000])
        self.download_files.clear()
        context = {key: item.get(key) for key in ("version_id", "revision", "license", "terms", "page_url")}
        context["provider"] = result.get("provider")
        for file in item.get("files", [])[:256]:
            if file.get("download_url") and str(file.get("name") or "").casefold().endswith(".safetensors"):
                self.download_files.addItem(f"{file.get('name')} · {file.get('size_bytes') or 'Unknown'} bytes · {file.get('hash_scope', 'unknown')} hash", dict(context, **file))

    def _open_provider(self):
        result = self._provider_result or {}
        file = self.download_files.currentData() or {}
        item = self.provider_listings.currentData() or {}
        url = file.get("page_url") or item.get("page_url") or result.get("page_url")
        if not url:
            url = "https://civitai.com" if self.provider.currentData() == "civitai" else "https://huggingface.co/models"
        from core.model_workspace import validate_download_url
        try: validate_download_url(url)
        except RuntimeError as exc: self.status.setText(str(exc)); return
        QDesktopServices.openUrl(QUrl(url))

    def _attach_provider_evidence(self):
        item = self.models.currentItem()
        if item is None or not self._provider_result:
            self.status.setText("Select a registered local model, then look up an exact matching provider version."); return
        identity, token = item.data(0, Qt.UserRole), self._snapshot_token
        evidence = self._provider_result
        self._run("Attaching reviewed provider evidence", lambda job: self.workspace.attach_provider_evidence(identity,
            evidence, expected_registry=token), lambda _: self._load())

    def _preview_download(self):
        file = self.download_files.currentData()
        if not file: self.status.setText("Choose an exact version with a downloadable safe tensor file first."); return
        default = self.workspace.destination(getattr(self, "_settings_snapshot", {}), role=self.role.currentText(), family=self.family.text())
        filename = Path(str(file.get("name") or file.get("filename") or "model.safetensors")).name
        destination, _ = QFileDialog.getSaveFileName(self, "Choose New Model Destination", str(Path(default) / filename) if default else filename, "Safe tensors (*.safetensors)")
        if not destination: return
        hashes = file.get("hashes", {})
        sha = file.get("sha256", "") or (hashes.get("sha256", "") if isinstance(hashes, dict) else "")
        try:
            guards = self._companion_guard("sha256:" + str(sha).lower(), path=destination)
            terms = file.get("terms") or file.get("license")
            # HF gating flags alone are not license evidence.
            if not file.get("license") and (not isinstance(terms, dict) or not any(k.startswith("allow") for k in terms)):
                raise ValueError("License/terms evidence is unavailable. Review the provider page; acquisition stays held until attributed terms are available.")
            plan = preview_acquisition(provider=str(file.get("provider") or self.provider.currentData()),
                version=str(file.get("version_id") or file.get("revision") or ""), url=file["download_url"],
                sha256=sha, size=file.get("size_bytes", file.get("size", 0)), destination=destination,
                terms=json.dumps({"license": file.get("license"), "terms": file.get("terms")}, ensure_ascii=False),
                protected_roots=guards)
        except (ValueError, RuntimeError, OSError) as exc: self.status.setText(str(exc)); return
        if self.collect_for_import:
            role, family = self.role.currentText(), self.family.text().strip()
            if role not in RESOURCE_ROLES or not family:
                self.status.setText("Choose the Model Type and Family for this download before adding it to the import plan.")
                return
            self._collected_import_choice({"kind": "acquisition", "plan": plan,
                "role": role, "family": family, "companion_handoff": self._companion_handoff})
            return
        text = f"Download this exact file?\n\nVersion: {plan.version}\nSHA-256: {plan.sha256}\nSize: {plan.size:,} bytes\nDestination: {plan.destination}\nTerms: {plan.terms}\n\nExisting files will not be replaced. Staged attempts are retained. Registration and launch are separate actions."
        if QMessageBox.question(self, "Review Model Acquisition", text) != QMessageBox.Yes: return
        self.selected_companion_choice = None
        self._run("Acquiring reviewed model", lambda job: acquire_model(plan, confirmed=True,
            should_cancel=lambda: job.cancel_requested, progress=self._progress(job),
            protected_roots=self._companion_guard("sha256:" + plan.sha256, path=plan.destination)), self._acquired, unit="MiB")

    def _acquired(self, inspection):
        self.selected_companion_choice = None
        if self._companion_handoff is not None:
            self.role.setCurrentText(self._companion_handoff.role)
            self.family.setText(self._companion_handoff.family)
        self._inspection = inspection
        self.dialog_commands.select_group("Local Models")
        self.tabs.setCurrentIndex(0)
        self._workspace_tab_changed(0)
        self.detail.setText(f"Verified download: {inspection.path.name}\n{inspection.resource_id}")
        self.status.setText("Download verified. Choose its model type/family and Register Model when ready.")
        self._update_actions()

    def _collected_import_choice(self, choice):
        if not self.collect_for_import: return
        self.selected_import_choice = choice
        self.accept()

    def _recover_download(self):
        from core.model_workspace import preview_acquisition_recovery, recover_acquisition
        attempt = QFileDialog.getExistingDirectory(self, "Choose a Retained .collectigen-acquisition Folder")
        if not attempt: return
        self.selected_companion_choice = None
        def reviewed(preview):
            try: self._companion_guard("sha256:" + preview.sha256, path=preview.destination)
            except (ValueError, RuntimeError, OSError) as exc:
                self.status.setText(str(exc)); return
            message = f"Verified retained file: {preview.staged.name}\nSHA-256: {preview.sha256}\nDestination: {preview.destination}\n\n" + ("The exact output is already present. Record successful recovery?" if preview.destination_present else "Publish these verified bytes at the missing destination? Existing files will not be replaced.")
            if QMessageBox.question(self, "Review Acquisition Recovery", message) != QMessageBox.Yes: return
            self._run("Recovering reviewed acquisition", lambda job: recover_acquisition(preview, confirmed=True,
                protected_roots=self._companion_guard("sha256:" + preview.sha256, path=preview.destination), should_cancel=lambda: job.cancel_requested), self._acquired)
        self._run("Inspecting retained acquisition", lambda job: preview_acquisition_recovery(attempt,
            protected_roots=self._acquisition_roots(), should_cancel=lambda: job.cancel_requested), reviewed)

    def _cancel_work(self):
        if self._task is not None: self.jobs.cancel(self._task.job_id)

    def _update_actions(self, *_):
        super()._update_actions()
        idle = self._task is None
        if hasattr(self, "recheck_inspected_button"):
            self.recheck_inspected_button.setEnabled(idle and self._single_file_recheck_path is not None)
        if hasattr(self, "empty_inspect_button"):
            self.empty_inspect_button.setEnabled(idle and self.inspect_button.isEnabled())
            self.empty_folder_button.setEnabled(idle and self.index_button.isEnabled())
            self.empty_swarm_button.setEnabled(idle and self.import_plan_button.isEnabled())
        if hasattr(self, "browser"): self.browser.set_busy(not idle)
        if hasattr(self, "artwork_button"):
            self.artwork_button.setEnabled(idle and self.models.currentItem() is not None and not self.collect_for_import)
        for widget in self._extras: widget.setEnabled(idle)
        if not hasattr(self, "folder_files"): return
        no_review = self._folder_scan_pending or self._folder_scan_recovery
        for widget in self._folder_review_widgets:
            widget.setVisible(not no_review)
        self._folder_review_layout.setStretchFactor(self.folder_files, 0 if no_review else 1)
        self._folder_review_layout.setStretch(self._folder_review_spacer_index, 1 if no_review else 0)
        if no_review: self.file_evidence.hide()
        self.folder_advanced_panel.setVisible(not no_review and self.folder_advanced_button.isChecked())
        self.retry_folder_button.setVisible(self._folder_scan_recovery)
        self.retry_folder_button.setEnabled(idle and self._folder_scan_recovery and self._folder_scan_root is not None)
        self.choose_another_folder_button.setVisible(self._folder_scan_recovery)
        self.choose_another_folder_button.setEnabled(idle and self._folder_scan_recovery)
        ready_paths = {item.path for item in self._index_preview.inspections} if self._index_preview is not None else set()
        selected_ready = bool(self._folder_checked) and not self._review_requires_check and self._folder_checked.issubset(ready_paths) and all(path in self._folder_assignments for path in self._folder_checked)
        self.apply_index_button.setEnabled(idle and self._index_preview is not None and selected_ready)
        self.apply_index_button.setText(f"Register All {len(self._folder_checked):,} Checked Files" if self._folder_checked else "Register Selected Files")
        self.apply_index_button.setToolTip("Applies to every checked file across folders, filters and pages; review the complete checked count above before registering.")
        declaration_ready = idle and self._index_preview is not None and self.role.currentText() in RESOURCE_ROLES and bool(self.family.text().strip())
        self.assign_folder_button.setEnabled(declaration_ready)
        self.assign_files_button.setEnabled(declaration_ready and bool(self._folder_checked))
        self.detect_files_button.setEnabled(idle and self._index_preview is not None and not self._review_requires_check)
        self.save_review_button.setEnabled(idle and bool(self._index_preview or self._review_resume or self._scan_review_draft))
        self.save_review_as_button.setVisible(not no_review and self._scan_review_path is not None)
        self.save_review_as_button.setEnabled(self.save_review_button.isEnabled())
        self.recheck_review_button.setText(
            "Recheck Saved Files" if self._scan_review_path is not None else "Recheck Reviewed Files"
        )
        self.recheck_review_button.setVisible(not no_review and self._scan_review_draft is not None)
        self.recheck_review_button.setEnabled(idle and self._scan_review_draft is not None)
        self.review_held_button.setText(f"Review Held Files ({len(self._index_preview.held):,})" if self._index_preview is not None else "Review Held Files")
        self.review_held_button.setVisible(self._index_preview is not None and bool(self._index_preview.held))
        self.review_held_button.setEnabled(idle and self._index_preview is not None and bool(self._index_preview.held))
        self.use_detected_button.setEnabled(idle and bool(self._folder_checked) and bool(self._detections))
        self.evidence_filter.setEnabled(idle and bool(self._detections))
        self.select_page_button.setEnabled(idle and self._index_preview is not None)
        self.clear_files_button.setEnabled(idle and bool(self._folder_checked))
        self.file_previous.setEnabled(idle and self._folder_page > 0)
        self.file_next.setEnabled(idle and (self._folder_page + 1) * self.PAGE_SIZE < len(self._file_entries()))
        self.resume_review_button.setVisible(self._review_resume is not None and self.tabs.currentIndex() != 4)
        self.cancel_work_button.setEnabled(not idle)
        self.cancel_work_button.setVisible(not idle)
        if hasattr(self, "dialog_commands"):
            self.dialog_commands.bar.tabs.setEnabled(idle)
        self._update_start_panel()
        self.save_notes_button.setEnabled(idle and self.models.currentItem() is not None)
        if self._note_identity in self._note_drafts and self._note_token != self._snapshot_token:
            self.save_notes_button.setEnabled(False)
        self.discard_notes_button.setEnabled(idle and self._note_identity in self._note_drafts)
        self.review_notes_button.setEnabled(idle and bool(self._note_drafts))
        current = self.models.currentItem()
        self.return_to_selector_button.setEnabled(idle and current is not None and
            current.data(0, Qt.UserRole) in self._records)
        self.organize_button.setEnabled(idle and current is not None and str(current.data(0, Qt.UserRole)).startswith("sha256:"))
        self.organize_batch_button.setEnabled(idle and 0 < len(self._registered_group) <= 256)
        self.organize_batch_button.setToolTip("Review up to 256 distinct models from the last registration. Choose each exact source copy; nothing moves automatically.")
        self._update_registration_followup()
        self.relocation_recovery_button.setEnabled(idle)
        self.prepare_fast_button.setEnabled(idle and current is not None and str(current.data(0, Qt.UserRole)).startswith("sha256:"))
        self.prepare_batch_button.setEnabled(idle and 0 < len(self._registered_group) <= 64)
        self.prepare_batch_button.setToolTip("Review up to 64 registered models with explicit archive, family and type choices. Larger sets need smaller preparations.")
        self.manage_fast_button.setEnabled(idle)
        self.required_resources_button.setEnabled(idle and self._companion_handoff is None and current is not None and str(current.data(0, Qt.UserRole)).startswith("sha256:"))
        self.import_swarm_metadata_button.setEnabled(idle and self._companion_handoff is None and current is not None and str(current.data(0, Qt.UserRole)).startswith("sha256:"))
        self.swarm_onboarding_button.setEnabled(idle and self.settings is not None and self._companion_handoff is None and not getattr(self, "_onboarding_parent", False))
        self.swarm_onboarding_button.setToolTip("Review supported favorites and appearance from your enrolled setup." if self.settings is not None else "Open Setup to import existing SwarmUI preferences.")
        self.import_plan_button.setEnabled(idle and self._companion_handoff is None and not self.collect_for_import)
        if hasattr(self, "return_companion_button"):
            self.return_companion_button.setEnabled(idle and self.selected_companion_choice is not None)
        self.locate_copy_button.setEnabled(idle and current is not None and str(current.data(0, Qt.UserRole)).startswith("sha256:"))
        if self._related_identity:
            self.previous.setEnabled(False); self.next.setEnabled(False)
        self.find_metadata_button.setEnabled(idle and self.models.currentItem() is not None)
        self.derivation_button.setEnabled(idle and self.models.currentItem() is not None)
        self.use_button.setText("Register Model" if self._inspection else "Verify Selected Model")
        self.use_button.setVisible((self.collect_for_import or self._active_ribbon_group == "Local Models")
                                   and (not self._return_to_selector or self._inspection is not None)
                                   and not (self._single_file_review_path is not None
                                            and self._inspection is None))
        self.use_button.setEnabled(idle and (bool(self._inspection) and self.role.currentText() in RESOURCE_ROLES and bool(self.family.text().strip()) or current is not None))

        if self.collect_for_import:
            for name in ("save_notes_button", "tags", "notes", "derivation_button", "locate_copy_button",
                    "attach_evidence_button", "recover_download_button", "save_storage_button", "setup_folders_button",
                    "selector_button", "upscaler_models_button", "guidance_button", "generation_button", "required_resources_button",
                    "import_swarm_metadata_button", "swarm_onboarding_button", "import_plan_button", "organize_button", "organize_batch_button",
                    "relocation_recovery_button", "prepare_fast_button", "prepare_batch_button", "manage_fast_button", "usage_placement_button"):
                widget = getattr(self, name, None)
                if widget is not None: widget.setEnabled(False); widget.hide()
            if self._initial_import_review is not None:
                action = "Update Selected Import Choice"
            elif self._folder_checked:
                action = f"Add All {len(self._folder_checked):,} Checked Files to Import Plan"
            else:
                action = "Add Files to Import Plan"
            self.apply_index_button.setText(action)
            self.use_button.setText("Add Inspected File to Import Plan")
            self.use_button.setEnabled(self.use_button.isEnabled() and self._inspection is not None)
            self.download_button.setText("Add Download to Import Plan…")

    def _confirm_note_close(self):
        self._stash_notes()
        if self._note_drafts and not getattr(self,"_allow_note_close",False):
            if QMessageBox.question(self,"Discard Pending Model Notes?",
                "This window has unsaved notes. Close and discard those pending edits? Saved registry records are unchanged.",
                QMessageBox.Yes | QMessageBox.No,QMessageBox.No) != QMessageBox.Yes: return False
            self._allow_note_close = True
        return True

    def accept(self):
        if not self._request_artwork_close(self.accept): return
        if self._confirm_note_close():
            if not self._request_import_plan_close(self.accept): return
            if not self._request_usage_close(self.accept): return
            if not self._request_swarm_onboarding_close(self.accept): return
            if not self._request_swarm_import_close(self.accept): return
            if not self._request_companion_close(self.accept): return
            if not self._request_cache_close(self.accept): return
            if not self._request_relocation_close(self.accept): return
            if not self._request_copy_close(self.accept): return
            self.browser.close_artwork(); self._close_known_folder_review(); super().accept()

    def reject(self):
        if self._confirm_note_close():
            if not self._request_artwork_close(self.reject): return
            if not self._request_import_plan_close(self.reject): return
            if not self._request_usage_close(self.reject): return
            if not self._request_swarm_onboarding_close(self.reject): return
            if not self._request_swarm_import_close(self.reject): return
            if not self._request_companion_close(self.reject): return
            if not self._request_cache_close(self.reject): return
            if not self._request_relocation_close(self.reject): return
            if not self._request_copy_close(self.reject): return
            self.browser.close_artwork(); self._close_known_folder_review(); super().reject()

    def _close_known_folder_review(self):
        dialog = getattr(self, "_known_folders_dialog", None)
        if dialog is not None:
            from shiboken6 import isValid
            if isValid(dialog): dialog.reject()

    def closeEvent(self,event):
        artwork = getattr(self, "_artwork_dialog", None)
        if artwork is not None and artwork.isVisible():
            event.ignore(); self.reject(); return
        import_plan = getattr(self, "_import_plan_dialog", None)
        if import_plan is not None and import_plan.isVisible():
            event.ignore(); self.reject(); return
        usage = getattr(self, "_usage_dialog", None)
        if usage is not None and usage.isVisible():
            event.ignore(); self.reject(); return
        onboarding = getattr(self, "_swarm_onboarding_dialog", None)
        if onboarding is not None and onboarding.isVisible():
            event.ignore(); self.reject(); return
        swarm_import = getattr(self, "_swarm_import_dialog", None)
        if swarm_import is not None and swarm_import.isVisible():
            event.ignore(); self.reject(); return
        companion = getattr(self, "_companion_dialog", None)
        if companion is not None and companion.isVisible():
            event.ignore(); self.reject(); return
        cache = getattr(self, "_cache_dialog", None)
        from shiboken6 import isValid
        if cache is not None and isValid(cache) and cache.isVisible():
            event.ignore(); self.reject(); return
        relocation = getattr(self, "_relocation_dialog", None)
        from shiboken6 import isValid
        if relocation is not None and isValid(relocation) and relocation.isVisible():
            event.ignore(); self.reject(); return
        dialog = getattr(self, "_exact_copy_dialog", None)
        from shiboken6 import isValid
        if dialog is not None and isValid(dialog) and dialog.isVisible():
            event.ignore(); self.reject(); return
        self._stash_notes()
        if self._note_drafts and not getattr(self,"_allow_note_close",False):
            event.ignore(); self.reject(); return
        self._close_known_folder_review()
        self.browser.close_artwork()
        super().closeEvent(event)
