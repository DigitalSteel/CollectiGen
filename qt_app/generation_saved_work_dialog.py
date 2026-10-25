"""Named, bounded browser for local generation groups and reusable setups."""
from __future__ import annotations

from datetime import datetime
import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QHBoxLayout,
    QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QListWidget)

from core.generation_saved_work import (export_selected_saved_work,
    export_reviewed_setup, review_setup_export, setup_export_payload,
    import_selected_saved_work, list_recent_saved_work, read_recipe_import_evidence)
from .components import DisclosureButton
from .workers import CoordinatedCallTask


class SetupExportReviewDialog(QDialog):
    """One selected export, with opt-in prompt sharing and exact-content review."""

    def __init__(self, review, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Review Setup Export")
        self.resize(780, 560)
        self.review = review
        layout = QVBoxLayout(self)
        heading = QLabel(review["recipe"]["name"])
        heading.setTextFormat(Qt.PlainText)
        layout.addWidget(heading)
        self.format = QComboBox()
        self.format.setAccessibleName("Export format")
        self.format.addItem("CollectiGen setup", "collectigen")
        self.format.addItem("SwarmUI preset — text and basic settings", "swarmui")
        self.format.setToolTip("SwarmUI JSON dialect qualified against public source revision e2c35f354dd9411b64037e18abf319927260609d. No live account or runtime check is performed.")
        layout.addWidget(self.format)
        self.include_prompts = QCheckBox("Include prompt text in the exported file")
        self.include_prompts.setToolTip("Only enable this if you intend to share this setup's positive and negative text.")
        layout.addWidget(self.include_prompts)
        self.scope = QLabel()
        self.scope.setTextFormat(Qt.PlainText)
        self.scope.setWordWrap(True)
        layout.addWidget(self.scope)
        self.fields = QTreeWidget()
        self.fields.setHeaderLabels(["Included choice", "Value"])
        self.fields.setAccessibleName("Exact choices included in export")
        self.fields.setColumnWidth(0, 175)
        layout.addWidget(self.fields, 1)
        self.details = QPlainTextEdit()
        self.details.setReadOnly(True)
        self.details.setAccessibleName("Exact exported JSON")
        self.details.setMaximumHeight(170)
        self.disclosure = DisclosureButton("Inspect Exported JSON", "Hide Exported JSON", self.details, self)
        layout.addWidget(self.disclosure, 0, Qt.AlignLeft)
        layout.addWidget(self.details)
        self.status = QLabel()
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        rail = QHBoxLayout()
        rail.addStretch(1)
        self.cancel_button = QPushButton("Cancel")
        self.export_button = QPushButton("Choose New Export File…")
        self.export_button.setProperty("primary", True)
        for button in (self.cancel_button, self.export_button):
            button.setAutoDefault(False)
            rail.addWidget(button)
        layout.addLayout(rail)
        self.cancel_button.clicked.connect(self.reject)
        self.export_button.clicked.connect(self.accept)
        self.format.currentIndexChanged.connect(self.refresh_preview)
        self.include_prompts.toggled.connect(self.refresh_preview)
        self.refresh_preview()

    def refresh_preview(self):
        format = self.format.currentData()
        self.fields.clear()
        self.details.clear()
        self.scope.setText(
            "Preserves reusable setup choices. Local import details, model files and artwork are not included."
            if format == "collectigen" else
            "Exports literal text and basic settings only. This preset does not select models in SwarmUI. "
            "Check the receiving SwarmUI version and settings before generation."
        )
        try:
            payload = setup_export_payload(self.review["recipe"], format=format,
                include_prompts=self.include_prompts.isChecked())
        except ValueError as exc:
            self.status.setText(str(exc))
            self.export_button.setEnabled(False)
            return
        self.details.setPlainText(json.dumps(payload, ensure_ascii=False, indent=2))
        values = (next(iter(payload.values()))["param_map"] if format == "swarmui" else payload)
        for key, value in values.items():
            if key in {"schema_version", "name", "resource_policy"}:
                continue
            rendered = (value if isinstance(value, str) else
                        f"{len(value)} selected entries — inspect exported JSON for full values"
                        if isinstance(value, list) else
                        "; ".join(f"{field}: {setting}" for field, setting in value.items())
                        if key == "settings" else "Included — inspect exported JSON")
            label = {"prompt": "Positive prompt", "negativeprompt": "Negative prompt",
                     "negative_prompt": "Negative prompt", "cfgscale": "Guidance (CFG)",
                     "prompt_parts": "Prompt parts"}.get(key, key.replace("_", " ").capitalize())
            if key in {"negativeprompt", "negative_prompt"} and rendered == "":
                rendered = "Empty — clears the negative prompt"
            row = QTreeWidgetItem([label, rendered])
            row.setToolTip(1, rendered)
            self.fields.addTopLevelItem(row)
        self.status.setText("Review the included choices. Export creates a new file; it does not change this setup or run generation.")
        self.export_button.setEnabled(True)


class SavedGenerationWorkDialog(QDialog):
    """Browsing never imports, executes, or grants a saved plan execution authority."""

    background_work_finished = Signal()

    def __init__(self, jobs, storage_root, kind, parent=None, *, settings=None,
                 settings_path=None, registry=None, protected_model_roots=(),
                 selection_only=False):
        super().__init__(parent)
        if kind not in {"groups", "recipes", "drafts", "prompt_presets"}:
            raise ValueError("Choose saved groups, recipes, drafts or prompt presets.")
        if selection_only and kind not in {"recipes", "drafts"}:
            raise ValueError("Selection-only browsing applies to image drafts or Generation Setups.")
        self.jobs = jobs
        self.settings, self.settings_path = settings, settings_path
        self.registry = registry
        self.protected_model_roots = tuple(protected_model_roots)
        self.storage_root = Path(storage_root).absolute()
        self.kind = kind
        self.selection_only = bool(selection_only)
        self.selected_path = None
        self.selected_digest = None
        self.requested_action = ""
        self._task = None
        self._cancel_requested = False
        self._closing = False
        self._items = {}
        self._offset = 0
        self._previous_offsets = []
        self._next_offset = None
        self._active_query = ""
        self._recovery_dialog = None
        self.preset_browser = None
        self.setWindowTitle({"groups": "Saved Alternative Groups", "recipes": "Saved Generation Setups", "drafts": "Resume Image Draft", "prompt_presets": "Prompt Preset Collection"}[kind])
        self.resize(1260 if kind == "prompt_presets" else 840, 780 if kind == "prompt_presets" else 580)
        layout = QVBoxLayout(self)
        heading = QLabel(("Choose an image draft to resume" if kind == "drafts" else
                          "Choose a saved setup for a new image") if self.selection_only else
            {"groups": "Groups compare alternatives", "recipes": "Setups reuse settings", "drafts": "Drafts resume unfinished requests", "prompt_presets": "Reusable prompt presets"}[kind])
        heading.setObjectName("sectionTitle"); layout.addWidget(heading)
        explanation = QLabel(
            ("Choose one saved request before opening its workspace. No generation starts here."
             if kind == "drafts" else
             "Choose one setup before opening a new image draft. You will review what it changes; no generation starts here.")
            if self.selection_only else
            "These are local saved items. Open one to review it; no generation starts here. "
            "Import and Export work on one explicitly chosen file at a time."
        )
        explanation.setWordWrap(True); layout.addWidget(explanation)
        self.swarm_setup_button = QPushButton("Create from SwarmUI Preset…")
        self.swarm_setup_button.setVisible(kind == "recipes" and not self.selection_only)
        self.swarm_setup_button.clicked.connect(self.import_swarm_setup)
        self.import_details_button = QPushButton("Review Import Details…")
        self.import_details_button.setVisible(False)
        self.import_details_button.clicked.connect(self.review_import_details)
        if kind == "recipes":
            swarm_row = QHBoxLayout()
            swarm_row.addWidget(self.swarm_setup_button)
            swarm_row.addWidget(self.import_details_button)
            swarm_row.addStretch(1)
            layout.addLayout(swarm_row)
        if kind != "prompt_presets":
            search_row = QHBoxLayout()
            self.search = QLineEdit(); self.search.setPlaceholderText("Search names and details across saved folder pages")
            self.search.setAccessibleName("Search saved work"); self.search.setMaxLength(512)
            self.search_button = QPushButton("Search")
            self.previous_button = QPushButton("Previous")
            self.next_button = QPushButton("Next / Search More")
            for widget in (self.search, self.search_button, self.previous_button, self.next_button): search_row.addWidget(widget)
            layout.addLayout(search_row)
            self.search.returnPressed.connect(self.restart)
            self.search.textChanged.connect(self._update_actions)
            self.search_button.clicked.connect(self.restart)
            self.previous_button.clicked.connect(self.previous_page)
            self.next_button.clicked.connect(self.next_page)
        self.tree = QTreeWidget()
        self.tree.setHeaderLabels(["Name", "Details", "Saved"])
        self.tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.tree.setColumnWidth(0, 280); self.tree.setColumnWidth(1, 370)
        self.tree.itemSelectionChanged.connect(self._update_actions)
        if kind == "prompt_presets":
            from .prompt_preset_browser import PromptPresetBrowser
            self.preset_browser = PromptPresetBrowser(scope="collection", settings=settings, settings_path=settings_path, table=self.tree)
            self.tree = self.preset_browser.table
            self.search, self.search_button = self.preset_browser.search, self.preset_browser.search_button
            self.previous_button, self.next_button = self.preset_browser.previous_button, self.preset_browser.next_button
            self.preset_browser.searchRequested.connect(lambda _query: self.restart())
            self.preset_browser.pageRequested.connect(lambda direction: self.next_page() if direction > 0 else self.previous_page())
            self.preset_browser.openRequested.connect(self.open_selected)
            layout.addWidget(self.preset_browser, 1)
        else:
            self.tree.itemDoubleClicked.connect(lambda *_: self.open_selected())
            layout.addWidget(self.tree, 1)
        self.status = QLabel("Loading saved work…")
        self.status.setWordWrap(True); self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        controls = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.create_button = QPushButton("Create New Preset")
        self.create_button.setVisible(kind == "prompt_presets")
        self.create_button.clicked.connect(self.create_new)
        self.create_setup_button = QPushButton("Create Setup…")
        if self.selection_only and kind == "drafts":
            self.create_setup_button.setText("Start New Image…")
        self.create_setup_button.setVisible(self.selection_only)
        self.create_setup_button.clicked.connect(self.create_setup)
        self.recovery_button = QPushButton("Review Recovery…")
        self.recovery_button.setVisible(kind == "prompt_presets")
        self.recovery_button.clicked.connect(self.review_recovery)
        self.import_button = QPushButton("Import File…")
        if kind == "recipes":
            self.import_button.setText("Import CollectiGen File…")
        self.export_button = QPushButton("Export Selected…")
        if kind == "recipes":
            self.export_button.setToolTip(
                "Review the format and included choices before exporting. Prompt text is shared only if you choose it."
            )
        self.cancel_button = QPushButton("Cancel Loading")
        self.open_button = QPushButton("Open Selected")
        if self.selection_only:
            self.open_button.setText("Resume Selected Draft" if kind == "drafts" else
                                     "Review in New Image…")
        self.open_button.setProperty("primary", True)
        self.close_button = QPushButton("Close")
        for button in (self.create_setup_button, self.create_button, self.refresh_button, self.import_button, self.export_button, self.recovery_button,
                       self.cancel_button, self.open_button, self.close_button):
            controls.addWidget(button)
        if self.selection_only:
            self.import_button.hide()
            self.export_button.hide()
        layout.addLayout(controls)
        self.refresh_button.clicked.connect(self.restart)
        self.import_button.clicked.connect(self.import_file)
        self.export_button.clicked.connect(self.export_selected)
        self.cancel_button.clicked.connect(self.cancel_loading)
        self.open_button.clicked.connect(self.open_selected)
        self.close_button.clicked.connect(self.reject)
        if self.preset_browser is not None:
            self.preset_browser.selectionChanged.connect(self._update_actions)
        self._update_actions()
        self.refresh()

    def _selected_item(self):
        selected = self.tree.selectedItems()
        if len(selected) != 1: return None
        value = selected[0].data(0, Qt.UserRole)
        return self._items.get(value.resource_id if self.preset_browser is not None else value)

    def _update_actions(self):
        idle = self._task is None
        selected = self._selected_item()
        available = bool(selected and selected["available"])
        self.import_details_button.setVisible(bool(
            self.kind == "recipes" and available
            and selected.get("has_import_evidence")
        ))
        self.import_details_button.setEnabled(idle)
        self.tree.setEnabled(idle)
        if self.preset_browser is not None: self.preset_browser.set_busy(not idle)
        self.refresh_button.setEnabled(idle)
        self.swarm_setup_button.setEnabled(idle)
        self.create_button.setEnabled(idle)
        self.create_setup_button.setEnabled(idle)
        self.recovery_button.setEnabled(idle)
        self.import_button.setEnabled(idle)
        self.export_button.setEnabled(idle and bool(selected and selected.get("exportable", available)))
        self.open_button.setEnabled(idle and available)
        self.cancel_button.setEnabled(not idle)
        self.close_button.setEnabled(idle)
        self.search.setEnabled(idle); self.search_button.setEnabled(idle)
        query_applied = self.search.text().strip() == self._active_query
        self.previous_button.setEnabled(idle and query_applied and bool(self._previous_offsets))
        self.next_button.setEnabled(idle and query_applied and self._next_offset is not None)

    def restart(self):
        self._load_page(0, [], self.search.text().strip())

    def next_page(self):
        if (self._task is None and self._next_offset is not None
                and self.search.text().strip() == self._active_query):
            self._load_page(self._next_offset, [*self._previous_offsets, self._offset], self._active_query)

    def previous_page(self):
        if (self._task is None and self._previous_offsets
                and self.search.text().strip() == self._active_query):
            self._load_page(self._previous_offsets[-1], self._previous_offsets[:-1], self._active_query)

    def _run(self, label, work, completed):
        if self._task is not None:
            return
        self._cancel_requested = False
        task = CoordinatedCallTask(self.jobs, label,
            lambda _progress: work(lambda: self._cancel_requested), resource="io")
        self._task = task
        def success(value):
            if self._task is not task: return
            self._task = None
            if self._closing:
                QDialog.reject(self)
                self.background_work_finished.emit()
                return
            if self._cancel_requested:
                self.status.setText("Operation cancelled. Saved items remain available; refresh to see any completed write.")
            else:
                try: completed(value)
                except Exception as exc: self.status.setText(str(exc))
            self._update_actions()
        def failed(error):
            if self._task is not task: return
            self._task = None
            if self._closing:
                QDialog.reject(self)
                self.background_work_finished.emit()
                return
            self.status.setText(str(error))
            if self._recovery_dialog is not None: self._recovery_dialog.status_label.setText(str(error))
            self._update_actions()
        task.signals.complete.connect(success)
        task.signals.failed.connect(failed)
        task.signals.cancelled.connect(lambda: failed("Operation cancelled. No further files will be read."))
        self.status.setText(label + "…"); self._update_actions()
        try: task.start()
        except Exception as exc: failed(str(exc))

    def refresh(self):
        self._load_page(self._offset, self._previous_offsets, self._active_query)

    def _load_page(self, offset, previous_offsets, query):
        selected = self._selected_item()
        keep = selected["path"] if selected else None
        def show(value):
            self._offset = offset
            self._previous_offsets = list(previous_offsets)
            self._active_query = query
            self._next_offset = value.get("next_offset")
            self._items = {item["path"]: item for item in value["items"]}
            if self.preset_browser is not None:
                from .prompt_preset_browser import collection_preset_descriptor
                self.preset_browser.set_rows(tuple(collection_preset_descriptor(item) for item in value["items"]),
                    page_number=len(previous_offsets) + 1, has_previous=bool(previous_offsets),
                    has_next=self._next_offset is not None, query=query)
            else:
                self.tree.clear()
                for item in value["items"]:
                    saved = datetime.fromtimestamp(item["modified_ns"] / 1_000_000_000).strftime("%Y-%m-%d %H:%M")
                    row = QTreeWidgetItem([item["name"], item["summary"], saved])
                    row.setData(0, Qt.UserRole, item["path"])
                    if not item["available"]:
                        row.setToolTip(0, "This file is unavailable. Its contents were not applied.")
                    self.tree.addTopLevelItem(row)
                    if item["path"] == keep: row.setSelected(True)
            message = f"Folder page {len(self._previous_offsets) + 1}: {len(value['items'])} saved items, newest on this page first"
            if self.preset_browser is not None: message = "Select a preset and Open Selected to review its exact snapshot"
            if value["truncated"]: message += "; Next searches the next folder page"
            if value["skipped"]: message += f"; {value['skipped']} unsupported entries skipped"
            if self.kind == "prompt_presets" and not value["items"] and not value["truncated"] and not query:
                message = "No prompt presets on this page. Create New Preset or import an explicit saved file"
            if self.selection_only and not value["items"] and not value["truncated"] and not query:
                message = ("No saved image drafts yet. Choose Start New Image, or Close without opening a workspace"
                           if self.kind == "drafts" else
                           "No saved Generation Setups yet. Choose Create Setup to make one, or Close without starting a draft")
            self.status.setText(message + ".")
        self._run("Load recent saved work", lambda cancelled: list_recent_saved_work(
            self.storage_root, self.kind, offset=offset, query=query, cancelled=cancelled), show)

    def open_selected(self):
        item = self._selected_item()
        if self._task is not None or not item or not item["available"]:
            return
        self.selected_path = item["path"]
        self.selected_digest = item.get("content_digest")
        self.requested_action = "open"
        self.accept()

    def create_setup(self):
        if self._task is not None or not self.selection_only:
            return
        self.selected_path = None
        self.selected_digest = None
        self.requested_action = "create"
        self.accept()

    def create_new(self):
        if self._task is not None or self.kind != "prompt_presets": return
        self.requested_action = "create"
        self.selected_path = None
        self.accept()

    def review_import_details(self):
        item = self._selected_item()
        if (self._task is not None or self.kind != "recipes"
                or not item or not item.get("has_import_evidence") or not item["available"]):
            return

        def read(cancelled):
            if callable(cancelled) and cancelled():
                raise InterruptedError("Import-detail review was cancelled.")
            try:
                return read_recipe_import_evidence(self.storage_root, item["path"]), ""
            except (OSError, ValueError, RuntimeError, TypeError, KeyError, UnicodeError) as exc:
                return None, str(exc)

        def show(result):
            receipt, error = result
            if receipt is None:
                self.status.setText(
                    "Local import details are unavailable. The saved setup remains usable; "
                    "retain the original source file for fresh source review. " + error
                )
                return
            evidence = receipt["import"]
            child = QDialog(self)
            child.setWindowTitle("Saved Setup Source and Omissions")
            child.resize(840, 300)
            layout = QVBoxLayout(child)
            layout.setAlignment(Qt.AlignTop)
            selected = evidence.get("selected_presets")
            title = QLabel(
                f"Imported from {len(selected)} SwarmUI presets: "
                + " → ".join(item["key"] for item in selected)
                if selected else
                f"Imported from SwarmUI preset: {evidence['selected_key']}"
            )
            title.setObjectName("sectionTitle")
            title.setWordWrap(True)
            title.setTextFormat(Qt.PlainText)
            layout.addWidget(title)
            explanation = QLabel(
                "These are the selected original fields and import decisions, bound to this exact saved setup. "
                "The editable setup may differ from the original. Source names and dynamic syntax are evidence, "
                "not registered resources or executable settings. Native setup export excludes these local details."
            )
            explanation.setWordWrap(True)
            layout.addWidget(explanation)
            mapped = evidence["mapped"]
            resolved = evidence.get("resolved_resources", [])
            summary = QLabel(
                f"Imported: {', '.join(mapped['settings']) or 'no scalar settings'}; "
                f"{len(resolved)} exact source-model choices recorded at save; "
                f"{len(evidence['held_fields'])} original source fields retained for review. "
                "No source file or account is opened here."
            )
            summary.setWordWrap(True)
            layout.addWidget(summary)
            if resolved:
                chosen = QLabel(
                    "Chosen at save: " + ", ".join(
                        f"{row['field']}" +
                        (f" from {row['preset_key']}" if row.get("preset_key") else "") +
                        f" → exact {row['role']}" for row in resolved
                    ) + ". Current model availability is checked separately before use."
                )
                chosen.setWordWrap(True)
                chosen.setTextFormat(Qt.PlainText)
                layout.addWidget(chosen)
            details = QPlainTextEdit()
            details.setReadOnly(True)
            details.setAccessibleName("Selected original SwarmUI fields and import decisions")
            detail_payload = {
                "source_dialect": evidence["source_dialect"],
                "source_sha256": evidence["source_sha256"],
                "imported_mapping": mapped,
                "held_or_omitted": evidence["held_fields"],
            }
            if resolved:
                detail_payload["explicit_exact_resource_choices_at_save"] = resolved
            if selected:
                detail_payload["selected_presets_in_order"] = selected
                detail_payload["conflict_choices"] = evidence["choices"]
            else:
                detail_payload["selected_key"] = evidence["selected_key"]
                detail_payload["original_selected_preset"] = evidence["selected_row"]
            details.setPlainText(json.dumps(detail_payload, ensure_ascii=False, indent=2))
            toggle = DisclosureButton("Inspect Exact Source and Omissions", "Hide Exact Source and Omissions",
                                      details, child)
            toggle.toggled.connect(lambda expanded: child.resize(840, 560 if expanded else 300))
            layout.addWidget(toggle, 0, Qt.AlignLeft)
            layout.addWidget(details, 1)
            close = QPushButton("Close")
            close.clicked.connect(child.accept)
            layout.addWidget(close, 0, Qt.AlignRight)
            child.exec()

        self._run("Read saved setup import details", read, show)

    def review_recovery(self):
        if self._task is not None or self.kind != "prompt_presets" or self._recovery_dialog is not None: return
        from core.generation_saved_work import PromptPresetCollection
        collection = PromptPresetCollection(self.storage_root)
        item = self._selected_item()
        item_id = Path(item["path"]).stem if item else None
        def show(rows):
            if not rows:
                self.status.setText("No interrupted preset saves were found in this bounded review."); return
            child = QDialog(self); child.setWindowTitle("Review Interrupted Preset Saves"); child.resize(720, 400)
            self._recovery_dialog = child
            layout = QVBoxLayout(child)
            note = QLabel("Choose one retained save to reconcile its exact bytes. Completed writes are recognized; later edits are preserved. An unwritten save needs a fresh review before retrying.")
            note.setWordWrap(True); layout.addWidget(note)
            choices = QListWidget(); layout.addWidget(choices, 1)
            for row in rows:
                choices.addItem(row.get("summary") or "Interrupted preset save")
                choices.item(choices.count() - 1).setData(Qt.UserRole, row["operation_id"])
                choices.item(choices.count() - 1).setToolTip(row["item_id"])
            choices.setCurrentRow(0)
            child.status_label = QLabel("Reconciliation never overwrites later local changes.")
            child.status_label.setWordWrap(True); child.status_label.setTextFormat(Qt.PlainText)
            layout.addWidget(child.status_label)
            buttons = QHBoxLayout(); reconcile = QPushButton("Reconcile Selected Save"); close = QPushButton("Close")
            buttons.addWidget(reconcile); buttons.addWidget(close); layout.addLayout(buttons)
            child.reconcile_button = reconcile
            def apply():
                selected = choices.currentItem()
                if self._task is not None or selected is None: return
                operation = selected.data(Qt.UserRole)
                def complete(result):
                    self.status.setText("Recovery checked: " + result["state"].replace("_", " ") + ". Review again before any new save.")
                    child.accept(); self.refresh()
                self._run("Reconcile selected preset save", lambda cancelled: collection.recover(operation, confirmed=True), complete)
            reconcile.clicked.connect(apply); close.clicked.connect(child.reject)
            child.finished.connect(lambda *_: setattr(self, "_recovery_dialog", None))
            child.setWindowModality(Qt.WindowModal); child.show()
        self._run("Read retained preset recovery", lambda cancelled: collection.pending_operations(item_id), show)

    def import_file(self):
        if self._task is not None: return
        path, _ = QFileDialog.getOpenFileName(self, "Import One Saved Item", "", "Saved JSON (*.json)")
        if not path: return
        self._run("Import selected saved item",
            lambda cancelled: import_selected_saved_work(self.storage_root, self.kind, path, cancelled=cancelled),
            self._imported)

    def import_swarm_setup(self):
        if self.kind != "recipes" or self._task is not None:
            return
        from .swarm_setup_file_dialog import SwarmSetupFileDialog
        dialog = SwarmSetupFileDialog(
            self.jobs, self.storage_root, registry=self.registry,
            settings=self.settings, protected_model_roots=self.protected_model_roots,
            parent=self,
        )
        if dialog.exec() != QDialog.Accepted or dialog.saved_path is None:
            return
        choice = QMessageBox(self)
        choice.setWindowTitle("Generation Setup saved")
        choice.setText(f'"{dialog.name.text()}" is saved for reuse.')
        choice.setInformativeText(
            "Review it against this image draft now, or keep it in Saved Generation Setups. "
            "Nothing has been generated. The original SwarmUI file was not changed."
        )
        review = choice.addButton("Review in This Draft", QMessageBox.AcceptRole)
        later = choice.addButton("Keep for Later", QMessageBox.RejectRole)
        choice.setDefaultButton(later)
        choice.exec()
        if choice.clickedButton() is review:
            self.selected_path = dialog.saved_path
            self.requested_action = "open"
            self.accept()
        else:
            self.refresh()

    def _imported(self, _path):
        self.search.clear()
        self.restart()

    def export_selected(self):
        item = self._selected_item()
        if self._task is not None or not item or not item.get("exportable", item["available"]): return
        if self.kind == "recipes":
            self._run("Read selected setup for export",
                lambda _cancelled: review_setup_export(self.storage_root, item["path"]),
                lambda review: self._review_setup_export(item, review))
            return
        path, _ = QFileDialog.getSaveFileName(self, "Export One Saved Item", Path(item["path"]).name,
            "Saved JSON (*.json)")
        if not path: return
        self._run("Export selected saved item",
            lambda cancelled: export_selected_saved_work(self.storage_root, self.kind,
                item["path"], path, cancelled=cancelled),
            lambda _path: self.status.setText(
                "Selected setup exported and verified; local source and omission details were not included."
                if self.kind == "recipes" else "Selected item exported and verified."
            ))

    def _review_setup_export(self, item, review):
        dialog = SetupExportReviewDialog(review, self)
        if dialog.exec() != QDialog.Accepted:
            self.status.setText("Export cancelled. The saved setup is unchanged.")
            return
        format = dialog.format.currentData()
        include_prompts = dialog.include_prompts.isChecked()
        path, _ = QFileDialog.getSaveFileName(self, "Export Reviewed Setup to a New File",
            Path(item["path"]).stem + ("-swarmui.json" if format == "swarmui" else ".json"),
            "JSON (*.json)")
        if not path:
            self.status.setText("Export cancelled. No export file was created.")
            return
        self._run("Export reviewed setup",
            lambda cancelled: export_reviewed_setup(self.storage_root, item["path"], path, review,
                format=format, include_prompts=include_prompts, cancelled=cancelled),
            lambda _path: self.status.setText(
                "SwarmUI preset exported and verified. Import the file in SwarmUI separately; choose models there."
                if format == "swarmui" else "CollectiGen setup exported and verified. Local import details were not included."))

    def cancel_loading(self):
        if self._task is None: return
        self._cancel_requested = True
        if self._task.job_id:
            try: self.jobs.cancel(self._task.job_id)
            except Exception: pass
        self.status.setText("Cancellation requested; waiting for the current file read to stop.")

    def has_active_background_work(self):
        return self._task is not None

    def request_background_shutdown(self):
        self._closing = True
        if self._task is not None:
            self.cancel_loading()
        else:
            QDialog.reject(self)
        return self.has_active_background_work()

    def reject(self):
        if self._recovery_dialog is not None: self._recovery_dialog.reject()
        if self._task is not None:
            self.request_background_shutdown(); return
        QDialog.reject(self)

    def closeEvent(self, event):
        if self._task is not None:
            event.ignore(); self.reject()
        else:
            event.accept()
