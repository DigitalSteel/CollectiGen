"""Alternatives, comparison and selected-result finishing over canonical jobs."""
from __future__ import annotations

from copy import deepcopy
from io import BytesIO
import json
from pathlib import Path

from PySide6.QtCore import Qt, Signal, QTimer
from PySide6.QtGui import QImage, QPixmap
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialog,
    QFormLayout, QGridLayout, QHBoxLayout, QLabel, QLineEdit, QPushButton, QSpinBox, QSplitter,
    QScrollArea, QTabWidget, QTextEdit, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from core.generation_experiments import (build_experiment, grid_shape, load_experiment,
    save_experiment, reconcile_experiment, retry_failed)
from core.generation_planning import GenerationPlan
from core.generation_execution import GenerationExecutionService
from .gallery import ProtectedImageView
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask


class GenerationExperimentDialog(FeedbackEnabledDialog):
    feedback_workflow = "Generation alternatives"
    _terminal_signal = Signal(object)

    def __init__(self, owner):
        super().__init__(owner)
        self.owner = owner
        self.experiment = None
        self.path = None
        self.digest = None
        self.prepared = {}
        self.review_identity = None
        self._task = None
        self._running = False
        self._job = None
        self._row_id = None
        self._terminal_payload = None
        self._pause = False
        self._cancel_requested = False
        self._finishing_dialogs = []
        self._thumb_task = None
        self._thumb_revision = 0
        self._thumb_cancelled = False
        self._lora_hashes = {}
        self._saved_work_dialog = None
        self._form_error = ""
        self._operation_error = False
        self._creating_backup = None
        self.service = GenerationExecutionService(owner.storage_root, owner.jobs)
        self._terminal_signal.connect(self._terminal)
        self.setWindowTitle("Generate Alternatives and Compare")
        self.resize(1200, 850)
        layout = QVBoxLayout(self)
        intro = QLabel("Draft → Choose → Finish. Review a bounded group, generate one at a time, then finish only your selected results.")
        intro.setWordWrap(True); layout.addWidget(intro)
        self.workspaces = QTabWidget()
        create_page = QWidget(); create_layout = QVBoxLayout(create_page)
        review_page = QWidget(); review_layout = QVBoxLayout(review_page)
        self.workspaces.addTab(create_page, "Create New Group")
        self.workspaces.addTab(review_page, "Review Saved Group")
        self.workspaces.currentChanged.connect(lambda _index: self.refresh_actions())
        layout.addWidget(self.workspaces, 1)
        create_hint = QLabel("A new group uses the current Generate request. It does not change an opened saved group.")
        create_hint.setWordWrap(True); create_layout.addWidget(create_hint)
        self.controls = QWidget(); form = QFormLayout(self.controls); self.options_form = form
        self.group_name = QLineEdit()
        self.group_name.setPlaceholderText("e.g. Portrait lighting comparison")
        self.group_name.setMaxLength(120)
        self.axis = QComboBox()
        for label, value in (("Independent alternatives", ""), ("Compare steps", "steps"),
            ("Compare guidance", "cfg_scale"), ("Compare Remix strength", "denoising_strength"),
            ("Compare seeds", "seed"), ("Compare LoRA weights", "lora_weight"), ("Compare positive prompts", "positive_prompt"),
            ("Compare negative prompts", "negative_prompt")):
            self.axis.addItem(label, value)
        self.column_axis = QComboBox()
        self.column_axis.addItem("One setting only", "")
        for index in range(1, self.axis.count()):
            self.column_axis.addItem(self.axis.itemText(index).replace("Compare ", ""), self.axis.itemData(index))
        self.count = QSpinBox(); self.count.setRange(1, 32); self.count.setValue(4)
        self.lora = QComboBox()
        self.values = QTextEdit(); self.values.setMaximumHeight(44)
        self.values.setPlaceholderText("One value or complete prompt per line. Other settings and the seed stay fixed.")
        self.column_values = QTextEdit(); self.column_values.setMaximumHeight(44)
        self.column_values.setPlaceholderText("One second-setting value or complete prompt per line.")
        self.grid_preview = QTextEdit(); self.grid_preview.setReadOnly(True); self.grid_preview.setMaximumHeight(92)
        self.draft_first = QCheckBox("Draft first: defer final resize and detail pass until I choose results")
        self.draft_first.setChecked(True)
        form.addRow("Group name", self.group_name)
        form.addRow("Explore", self.axis); form.addRow("Alternatives (one at a time)", self.count)
        form.addRow("LoRA to compare", self.lora)
        form.addRow("First-setting values (rows)", self.values)
        form.addRow("Second setting (columns)", self.column_axis)
        form.addRow("Second-setting values", self.column_values)
        form.addRow("Grid preview", self.grid_preview)
        form.addRow(self.draft_first)
        self.sequence_note = QLabel("Alternatives run as separate local jobs, sequentially. Pause after the current result or cancel current work; each result uses its own retained receipt.")
        self.sequence_note.setWordWrap(True)
        self.setup_scroll = QScrollArea()
        self.setup_scroll.setWidgetResizable(True)
        self.setup_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.setup_scroll.setFixedHeight(230)
        setup_body = QWidget(); setup_body_layout = QVBoxLayout(setup_body)
        setup_body_layout.setContentsMargins(0, 0, 0, 0)
        setup_body_layout.addWidget(self.controls)
        setup_body_layout.addStretch(1)
        self.setup_scroll.setWidget(setup_body)
        create_layout.addWidget(self.setup_scroll)
        self.grid_summary = QLabel()
        self.grid_summary.setTextFormat(Qt.PlainText)
        self.grid_summary.setWordWrap(True)
        create_layout.addWidget(self.grid_summary)
        create_layout.addWidget(self.sequence_note)
        create_actions = QHBoxLayout()
        self.build = QPushButton("Create Candidate Group"); self.build.clicked.connect(self.create_group)
        create_actions.addStretch(1); create_actions.addWidget(self.build)
        create_layout.addLayout(create_actions)
        create_layout.addStretch(1)
        self.group_summary = QLabel("Open a saved group to review its exact candidates.")
        self.group_summary.setWordWrap(True); review_layout.addWidget(self.group_summary)
        row = QHBoxLayout()
        self.load = QPushButton("Open Saved Group…"); self.load.clicked.connect(self.open_group)
        self.review = QPushButton("Check All Pending Candidates"); self.review.clicked.connect(self.prepare_group)
        self.payload_details = QPushButton("Exact Request Details…"); self.payload_details.clicked.connect(self.show_exact_details)
        for button in (self.load, self.review, self.payload_details): row.addWidget(button)
        row.addStretch(); review_layout.addLayout(row)
        self.readiness_handoff = QWidget()
        handoff = QHBoxLayout(self.readiness_handoff); handoff.setContentsMargins(0, 0, 0, 0)
        self.readiness_notice = QLabel()
        self.readiness_notice.setWordWrap(True)
        self.readiness_notice.setTextFormat(Qt.PlainText)
        self.readiness_button = QPushButton("Open Readiness")
        self.readiness_button.clicked.connect(lambda: self.owner.review_alternatives_readiness())
        handoff.addWidget(self.readiness_notice, 1); handoff.addWidget(self.readiness_button)
        self.readiness_handoff.hide()
        review_layout.addWidget(self.readiness_handoff)
        split = QSplitter(Qt.Vertical)
        self.tree = QTreeWidget(); self.tree.setHeaderLabels(["Favorite", "Candidate / changed setting", "Seed", "State"])
        self.tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.tree.setColumnWidth(0, 75); self.tree.setColumnWidth(1, 520)
        self.tree.itemChanged.connect(self.favorite_changed)
        self.tree.itemSelectionChanged.connect(self._selection_changed)
        split.addWidget(self.tree)
        self.details = QTextEdit(); self.details.setReadOnly(True); self.details.setMaximumHeight(180)
        split.addWidget(self.details)
        results = QWidget(); self.result_panel = results; result_layout = QVBoxLayout(results)
        self.selected_result_label = QLabel("Select a verified result to preview")
        self.selected_result_label.setWordWrap(True)
        result_layout.addWidget(self.selected_result_label)
        self.result_view = ProtectedImageView(settings=owner.settings)
        self.result_view.setMinimumSize(250, 220)
        self.result_view.clear("No preview yet")
        result_layout.addWidget(self.result_view, 1)
        self.result_status = QLabel("Thumbnails are shown only after exact receipt and image checks.")
        self.result_status.setWordWrap(True); result_layout.addWidget(self.result_status)
        outer = QSplitter(Qt.Horizontal); self.candidate_area = outer
        outer.addWidget(split); outer.addWidget(results)
        outer.setStretchFactor(0, 3); outer.setStretchFactor(1, 2)
        review_layout.addWidget(outer, 1)
        self.candidate_actions = QWidget(); order = QHBoxLayout(self.candidate_actions)
        order.setContentsMargins(0, 0, 0, 0)
        self.up = QPushButton("Move Pending Up"); self.up.clicked.connect(lambda: self.reorder(-1))
        self.down = QPushButton("Move Pending Down"); self.down.clicked.connect(lambda: self.reorder(1))
        self.retry = QPushButton("Prepare Selected Retries"); self.retry.clicked.connect(self.retry_selected)
        self.retry.setToolTip("Return selected failed or cancelled candidates to pending after checking their retained jobs. Review them again before generating.")
        self.recover = QPushButton("Reconcile Retained Jobs"); self.recover.clicked.connect(self.reconcile)
        self.use = QPushButton("Create Draft from Candidate…")
        self.use.setToolTip(
            "Copy this candidate's complete request, including prompt, model and seed, into "
            "a separate Generate draft. The saved comparison and original request stay unchanged."
        )
        self.use.setAccessibleDescription(self.use.toolTip())
        self.use.clicked.connect(self.use_selected)
        for button in (self.up, self.down, self.retry, self.recover, self.use): order.addWidget(button)
        review_layout.addWidget(self.candidate_actions)
        self.empty_stage_spacer = QWidget()
        review_layout.addWidget(self.empty_stage_spacer, 1)
        self.ack = QCheckBox("I reviewed these exact candidates and authorize this local group")
        self.safety_review = QCheckBox("I have reviewed the content safety of every candidate in this group")
        review_layout.addWidget(self.safety_review); self.safety_review.toggled.connect(self.invalidate_safety)
        review_layout.addWidget(self.ack); self.ack.toggled.connect(self.refresh_actions)
        self.compare = QPushButton("Compare Selected Results"); self.compare.clicked.connect(self.compare_selected)
        self.finish = QPushButton("Finish Selected…"); self.finish.clicked.connect(self.finish_selected)
        self.ingest = QPushButton("Send Selected to Ingest"); self.ingest.clicked.connect(self.ingest_selected)
        for button in (self.compare, self.finish, self.ingest): result_layout.addWidget(button)
        self.status = QLabel("Create a group from the current request. Nothing runs until you review and start it.")
        self.status.setTextFormat(Qt.PlainText); self.status.setWordWrap(True); layout.addWidget(self.status)
        footer = QHBoxLayout()
        self.pause = QPushButton("Pause After Current"); self.pause.clicked.connect(self.pause_after_current)
        self.cancel = QPushButton("Cancel Current Work"); self.cancel.clicked.connect(self.cancel_current)
        self.close_button = QPushButton("Close"); self.close_button.clicked.connect(self.reject)
        self.start = QPushButton("Generate Reviewed Group"); self.start.setProperty("primary", True)
        self.start.clicked.connect(self.start_group)
        for button in (self.pause, self.cancel): footer.addWidget(button)
        footer.addStretch(); footer.addWidget(self.close_button); footer.addWidget(self.start)
        layout.addLayout(footer)
        self.axis.currentIndexChanged.connect(self._axis_changed)
        self.column_axis.currentIndexChanged.connect(self._axis_changed)
        self.values.textChanged.connect(self._update_grid_preview)
        self.column_values.textChanged.connect(self._update_grid_preview)
        self.group_name.textChanged.connect(self._clear_form_error)
        self.count.valueChanged.connect(self._clear_form_error)
        self._axis_changed()
        self.refresh_lora_choices()
        self.refresh_actions()

    def _axis_changed(self):
        self.refresh_lora_choices()
        comparison = bool(self.axis.currentData())
        grid = comparison and bool(self.column_axis.currentData())
        self.setup_scroll.setFixedHeight(500 if grid else 320 if comparison else 230)
        self.count.setEnabled(not comparison); self.values.setEnabled(comparison)
        self.lora.setEnabled("lora_weight" in (self.axis.currentData(), self.column_axis.currentData()) and comparison)
        self.options_form.setRowVisible(self.count, not comparison)
        self.options_form.setRowVisible(self.values, comparison)
        self.options_form.setRowVisible(self.column_axis, comparison)
        self.options_form.setRowVisible(self.column_values, grid)
        self.options_form.setRowVisible(self.grid_preview, grid)
        self.options_form.setRowVisible(self.lora, comparison and self.lora.isEnabled())
        self.grid_summary.setVisible(comparison)
        self._update_grid_preview()

    @staticmethod
    def _values_for(axis, widget):
        return [line if axis.endswith("prompt") else int(line) if axis in {"seed", "steps"} else float(line)
                for line in widget.toPlainText().splitlines() if line.strip()]

    def _clear_form_error(self):
        if self._form_error and self.status.text() == self._form_error:
            self.status.setText("Review the choices, then create a saved group. Nothing runs until you check and authorize it.")
            self._form_error = ""
            self.refresh_actions()

    def _update_grid_preview(self):
        self._clear_form_error()
        axis = self.axis.currentData(); column_axis = self.column_axis.currentData()
        self._grid_valid = True
        if axis:
            try:
                rows, columns, count = grid_shape(axis, self._values_for(axis, self.values),
                    column_axis, self._values_for(column_axis, self.column_values))
                if column_axis:
                    self.grid_preview.setPlainText(f"{len(rows)} rows × {len(columns)} columns = {count} sequential candidates\n" +
                        "\n".join(f"R{r + 1} C{c + 1}: {self.axis.currentText().replace('Compare ', '')} {value} · "
                                  f"{self.column_axis.currentText()} {other}"
                                  for r, value in enumerate(rows) for c, other in enumerate(columns)))
                else:
                    self.grid_preview.setPlainText(f"{count} sequential candidates · distinct {self.axis.currentText().replace('Compare ', '').lower()}.")
            except (TypeError, ValueError) as exc:
                self._grid_valid = False
                self.grid_preview.setPlainText(str(exc))
        else:
            self.grid_preview.clear()
        self.grid_summary.setText(self.grid_preview.toPlainText().split("\n", 1)[0])
        if hasattr(self, "build"):
            self.refresh_actions()

    def busy(self):
        return (self._task is not None or self._running or self._job is not None
                or self._thumb_task is not None
                or bool(self._saved_work_dialog and self._saved_work_dialog.has_active_background_work())
                or any(child.has_active_background_work() for child in self._finishing_dialogs))

    def refresh_lora_choices(self):
        current_id = self.lora.currentData()
        current_hash = self._lora_hashes.get(current_id)
        resources = [(r.id, r.hashes.get("sha256", ""), r.name or r.id)
                     for r in self.owner.plan.request.resources if r.role == "lora"]
        if resources == [(self.lora.itemData(i), self._lora_hashes.get(self.lora.itemData(i)),
                          self.lora.itemText(i)) for i in range(self.lora.count())]:
            return
        self.lora.blockSignals(True); self.lora.clear()
        self._lora_hashes = {resource_id: sha for resource_id, sha, _label in resources}
        for resource_id, _sha, label in resources:
            self.lora.addItem(label, resource_id)
        if current_id in self._lora_hashes and self._lora_hashes[current_id] == current_hash:
            self.lora.setCurrentIndex(self.lora.findData(current_id))
        self.lora.blockSignals(False)

    def showEvent(self, event):
        self.refresh_lora_choices()
        super().showEvent(event)

    def _identity(self):
        return (self.owner.plan.digest, deepcopy(self.owner.binding), id(self.owner.adapter))

    def _run(self, name, call, done):
        if self._task is not None:
            return
        task = CoordinatedCallTask(self.owner.jobs, name, lambda _progress: call(), resource="io")
        self._task = task
        def complete(value):
            self._task = None
            try: done(value)
            except Exception as exc: self._failed(str(exc))
            self.refresh_actions(); self.owner._notify_background_work_finished()
        task.signals.complete.connect(complete)
        task.signals.failed.connect(self._failed)
        task.signals.cancelled.connect(lambda: self._failed("Operation cancelled; retained candidates remain available."))
        try: task.start()
        except Exception as exc: self._failed(str(exc))
        self.refresh_actions()

    def _failed(self, message):
        self._task = None; self._running = False; self._pause = True
        self.prepared = {}; self.review_identity = None; self.ack.setChecked(False)
        if self._creating_backup is not None:
            self.experiment, self.path, self.digest = self._creating_backup
            self._creating_backup = None
            self.refresh_tree()
        if self._row_id and self.experiment:
            row = next((r for r in self.experiment["candidates"] if r["id"] == self._row_id), None)
            if row and row["state"] == "submitting":
                row["state"] = "outcome_unknown"
                row["error"] = "Submission needs reconciliation: " + str(message)
        self._operation_error = True
        self.status.setText(str(message)); self.refresh_actions()
        self.owner._notify_background_work_finished()

    def invalidate_safety(self):
        self.prepared = {}; self.ack.setChecked(False); self.refresh_actions()

    def _persist(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        return save_experiment(self.path, self.experiment, expected_digest=self.digest)

    def _saved(self, digest):
        self.digest = digest; self._creating_backup = None
        self._operation_error = False; self.refresh_tree()
        self.status.setText(f"{len(self.experiment['candidates'])} candidates and exact seeds saved. Check pending candidates before generation. Drafts keep the current base canvas and sampling settings.")
        self.workspaces.setCurrentIndex(1)

    def create_group(self):
        if self.busy(): return
        try:
            self._operation_error = False
            axis = str(self.axis.currentData())
            column_axis = str(self.column_axis.currentData()) if axis else ""
            values = self._values_for(axis, self.values) if axis else []
            column_values = self._values_for(column_axis, self.column_values) if column_axis else []
            value = build_experiment(self.owner.plan, count=self.count.value(), axis=axis or None,
                values=values, column_axis=column_axis or None, column_values=column_values,
                draft_first=self.draft_first.isChecked(), lora_id=self.lora.currentData(),
                name=self.group_name.text())
            self.group_name.setText(value["name"])
            self._creating_backup = (self.experiment, self.path, self.digest)
            self.experiment = value; self.prepared = {}; self.review_identity = None
            self.details.clear(); self.ack.setChecked(False)
            self.safety_review.setChecked(False)
            self.path = self.owner.storage_root / "experiments" / (value["id"] + ".json")
            self.digest = None
            self.status.setText("Saving the new candidate group…")
            self._run("Save generation alternatives", self._persist, self._saved)
        except Exception as exc:
            self._form_error = str(exc)
            self.status.setText(self._form_error)
            self.refresh_actions()

    def open_group(self):
        if self.busy(): return
        from .generation_saved_work_dialog import SavedGenerationWorkDialog
        browser = SavedGenerationWorkDialog(self.owner.jobs, self.owner.storage_root, "groups", parent=self)
        self._saved_work_dialog = browser
        browser.background_work_finished.connect(self.owner._notify_background_work_finished)
        try:
            accepted = browser.exec() == QDialog.Accepted
        finally:
            self._saved_work_dialog = None
            self.refresh_actions()
        if not accepted or not browser.selected_path: return
        path = browser.selected_path
        def done(result):
            self.experiment, self.digest = result; self.path = Path(path)
            self._operation_error = False
            self.prepared = {}; self.review_identity = None; self.details.clear()
            self.ack.setChecked(False); self.refresh_tree()
            self.safety_review.setChecked(False)
            self.status.setText("Group reopened. Reconcile uncertain outcomes; check pending candidates before resuming.")
            self.workspaces.setCurrentIndex(1)
        self._run("Open generation alternatives", lambda: load_experiment(path), done)

    def open_exact_group(self, path, expected_digest, expected_group_id):
        """Open one caller-pinned saved group for review, without submitting it."""
        if (self.busy() or getattr(self.owner, '_closed', False)
                or self.owner._shutdown_requested):
            self.status.setText("Generation alternatives are busy. Reopen this exact group after the current work finishes.")
            return False
        selected = Path(path).absolute()
        if (selected.parent != (Path(self.owner.storage_root).absolute() / "experiments")
                or not isinstance(expected_digest, str) or len(expected_digest) != 64
                or not isinstance(expected_group_id, str) or len(expected_group_id) != 32):
            self.status.setText("The selected generation group pointer is invalid.")
            return False
        def work():
            value, digest = load_experiment(selected)
            if digest != expected_digest or value["id"] != expected_group_id:
                raise ValueError("The selected generation group changed. Reopen it from the saved work list.")
            return value, digest
        def done(result):
            if getattr(self.owner, '_closed', False) or self.owner._shutdown_requested:
                return
            self.experiment, self.digest = result
            self.path = selected
            self._operation_error = False
            self.prepared = {}; self.review_identity = None; self.details.clear()
            self.ack.setChecked(False); self.safety_review.setChecked(False)
            self.refresh_tree()
            self.status.setText("Exact saved group opened for review. Check candidates and safety before any separate submission.")
            self.workspaces.setCurrentIndex(1)
        self._run("Open exact generation alternatives", work, done)
        return True

    def refresh_tree(self):
        selected = {row["id"] for row in self.selected()}
        if self.experiment:
            group = self.experiment
            finishing = "Draft first; finish selected results later" if group["draft_first"] else "Finish each result"
            self.group_summary.setText(
                f"Saved group: {group['name']}  •  {len(group['candidates'])} candidates  •  {finishing}. "
                "Exact differences are listed below.")
        else:
            self.group_summary.setText("Open a saved group to review its exact candidates.")
        self.tree.blockSignals(True); self.tree.clear()
        for row in self.experiment["candidates"] if self.experiment else ():
            item = QTreeWidgetItem(["", row["label"], str(row["plan"]["request"].get("seed")), row["state"].replace("_", " ")])
            item.setData(0, Qt.UserRole, row["id"]); item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(0, Qt.Checked if row["favorite"] else Qt.Unchecked)
            item.setToolTip(1, row["error"] or row["label"])
            self.tree.addTopLevelItem(item); item.setSelected(row["id"] in selected)
        self.tree.blockSignals(False); self._selection_changed()

    def selected(self):
        ids = {item.data(0, Qt.UserRole) for item in self.tree.selectedItems()}
        return [row for row in self.experiment["candidates"] if row["id"] in ids] if self.experiment else []

    def _selection_changed(self):
        self._thumb_revision += 1
        self._thumb_cancelled = False
        rows = self.selected()
        verified = next((row for row in rows if row["state"] == "verified"), None)
        self.result_view.clear("No preview yet" if not verified else "Checking result…")
        self.selected_result_label.setText(
            f"{verified['label']}" + (f" · {len(rows)} selected" if len(rows) > 1 else "")
            if verified else "Select a verified result to preview")
        self.result_status.setText("Checking exact receipt and image…" if verified else
            "Thumbnails are shown only after exact receipt and image checks.")
        self.refresh_actions()
        if verified:
            QTimer.singleShot(0, self._start_thumbnail)

    def _start_thumbnail(self):
        if self._thumb_task is not None or self._thumb_cancelled or self.owner._shutdown_requested:
            return
        rows = self.selected()
        row = next((row for row in rows if row["state"] == "verified"), None)
        if row is None:
            return
        frozen = deepcopy(row); revision = self._thumb_revision
        def work(_progress):
            from PIL import Image
            from core.training_source_io import read_stable_training_source
            _row, receipt, paths = self._verified([frozen])[0]
            raw, identity = read_stable_training_source(Path(paths[0]))
            if identity["sha256"] != receipt["outputs"][0]["sha256"]:
                raise ValueError("The selected result changed; reconcile the retained job.")
            with Image.open(BytesIO(raw)) as image:
                if image.format != "PNG" or image.width * image.height > 32_000_000:
                    raise ValueError("The selected image exceeds the protected thumbnail bounds.")
                image.load()
                image.thumbnail((320, 240))
                output = BytesIO(); image.convert("RGBA").save(output, format="PNG")
            if output.tell() > 2 * 1024 * 1024:
                raise ValueError("The selected thumbnail exceeds its size bound.")
            return paths[0], output.getvalue()
        task = CoordinatedCallTask(self.owner.jobs, "Verify selected result thumbnail", work, resource="io")
        self._thumb_task = task
        def finish(value=None, error=""):
            if self._thumb_task is not task: return
            self._thumb_task = None
            if revision == self._thumb_revision and any(r["id"] == frozen["id"] for r in self.selected()):
                if error:
                    self.result_view.clear("Preview unavailable")
                    self.result_status.setText(str(error))
                else:
                    path, raw = value
                    pixmap = QPixmap.fromImage(QImage.fromData(raw))
                    if pixmap.isNull():
                        self.result_view.clear("Preview unavailable")
                        self.result_status.setText("The verified image could not be decoded for preview.")
                    else:
                        self.result_view.set_image(pixmap, path)
                        self.result_status.setText("Exact verified result · protected thumbnail")
            self.refresh_actions()
            self.owner._notify_background_work_finished()
            if revision != self._thumb_revision and not self.owner._shutdown_requested:
                QTimer.singleShot(0, self._start_thumbnail)
        task.signals.complete.connect(lambda value: finish(value))
        task.signals.failed.connect(lambda error: finish(error=error))
        task.signals.cancelled.connect(lambda: finish(error="Thumbnail check cancelled."))
        try: task.start()
        except Exception as exc: finish(error=str(exc))
        self.refresh_actions()

    def refresh_actions(self):
        self.refresh_lora_choices()
        idle = not self.busy()
        selection_idle = self._task is None and not self._running and self._job is None
        rows = self.experiment["candidates"] if self.experiment else []
        pending = [r for r in rows if r["state"] == "pending"]
        selected = self.selected()
        verified = bool(selected) and all(r["state"] == "verified" for r in selected)
        has_group = bool(rows)
        # The shared footer may describe the opened group. Do not present that
        # state as feedback for a separate, unsaved builder draft.
        self.status.setVisible(self.workspaces.currentIndex() == 1 or not has_group
            or bool(self._form_error) or self._operation_error or self.busy())
        show_reorder = len(selected) == 1 and selected[0]["state"] == "pending"
        show_retry = bool(selected) and all(r["state"] in {"failed", "cancelled"} for r in selected)
        show_recover = any(r["state"] != "pending" for r in rows)
        show_use = len(selected) == 1
        self.candidate_area.setVisible(has_group)
        self.details.setVisible(has_group and bool(self.details.toPlainText()))
        self.result_panel.setVisible(any(r["state"] == "verified" for r in rows))
        self.payload_details.setVisible(has_group)
        self.review.setVisible(has_group)
        self.up.setVisible(show_reorder); self.down.setVisible(show_reorder)
        self.retry.setVisible(show_retry); self.recover.setVisible(show_recover)
        self.use.setVisible(show_use)
        self.candidate_actions.setVisible(show_reorder or show_retry or show_recover or show_use)
        self.empty_stage_spacer.setVisible(not has_group)
        self.safety_review.setVisible(bool(pending))
        self.ack.setVisible(bool(pending) and bool(self.prepared))
        self.start.setVisible(bool(pending) and bool(self.prepared)
            and self.workspaces.currentIndex() == 1)
        self.pause.setVisible(self._running)
        self.cancel.setVisible(self._job is not None or self._task is not None or self._thumb_task is not None)
        self.controls.setEnabled(idle)
        lora_needed = bool(self.axis.currentData()) and "lora_weight" in (
            self.axis.currentData(), self.column_axis.currentData())
        self.build.setEnabled(idle and self._grid_valid and (not lora_needed or self.lora.count() > 0))
        self.load.setEnabled(idle)
        self.payload_details.setEnabled(idle and bool(rows))
        self.review.setEnabled(idle and bool(pending) and self.safety_review.isChecked() and self.owner.adapter is not None and bool(self.owner.binding))
        self.review.setToolTip("Check your generator and selected model on Readiness first.")
        needs_readiness = bool(pending) and (self.owner.adapter is None or not self.owner.binding)
        self.readiness_handoff.setVisible(bool(pending))
        self.readiness_button.setVisible(needs_readiness)
        self.readiness_button.setEnabled(idle)
        if self.owner.adapter is None:
            self.readiness_notice.setText("The generator has not been checked. Review it on Readiness, then return to this saved group. Every candidate still needs its own check.")
        elif not self.owner.binding:
            self.readiness_notice.setText("The selected model has not been checked. Review it on Readiness, then return to this saved group. Every candidate still needs its own check.")
        elif not self.safety_review.isChecked():
            self.readiness_notice.setText("Review the content safety of every candidate, then check the pending candidates separately.")
        else:
            self.readiness_notice.setText("Generator and model readiness do not approve this group. Check every pending candidate before generating.")
        self.start.setEnabled(idle and bool(pending) and self.ack.isChecked()
            and all(r["id"] in self.prepared for r in pending) and self.review_identity == self._identity())
        for button in (self.up, self.down): button.setEnabled(idle and len(selected) == 1 and selected[0]["state"] == "pending")
        self.retry.setEnabled(idle and bool(selected) and all(r["state"] in {"failed", "cancelled"} for r in selected))
        self.recover.setEnabled(idle and bool(rows)); self.compare.setEnabled(idle and verified and len(selected) <= 4)
        self.finish.setEnabled(idle and verified); self.ingest.setEnabled(idle and verified)
        self.use.setEnabled(idle and len(selected) == 1)
        self.pause.setEnabled(self._running); self.cancel.setEnabled(
            self._job is not None or self._task is not None or self._thumb_task is not None)
        self.ack.setEnabled(idle); self.safety_review.setEnabled(idle); self.tree.setEnabled(selection_idle)
        self.close_button.setText("Hide" if self.busy() else "Close")
        self.owner._update_actions()

    def favorite_changed(self, item, column):
        if not self.experiment or column != 0: return
        if self.busy():
            row = next((row for row in self.experiment["candidates"]
                        if row["id"] == item.data(0, Qt.UserRole)), None)
            if row is not None:
                self.tree.blockSignals(True)
                item.setCheckState(0, Qt.Checked if row["favorite"] else Qt.Unchecked)
                self.tree.blockSignals(False)
            return
        for row in self.experiment["candidates"]:
            if row["id"] == item.data(0, Qt.UserRole): row["favorite"] = item.checkState(0) == Qt.Checked
        self._run("Save candidate favorite", self._persist, self._saved)

    def reorder(self, direction):
        selected = self.selected()
        if self.busy() or len(selected) != 1 or selected[0]["state"] != "pending": return
        rows = self.experiment["candidates"]; index = rows.index(selected[0]); other = index + direction
        if 0 <= other < len(rows) and rows[other]["state"] == "pending":
            rows[index], rows[other] = rows[other], rows[index]
            self._run("Save candidate order", self._persist, self._saved)

    def prepare_group(self):
        if self.busy() or not self.experiment or not self.owner.adapter or not self.owner.binding or not self.safety_review.isChecked(): return
        self.prepared = {}; self.ack.setChecked(False)
        self._cancel_requested = False
        identity = self._identity(); adapter = self.owner.adapter; binding = deepcopy(self.owner.binding)
        pending = deepcopy([r for r in self.experiment["candidates"] if r["state"] == "pending"])
        def work():
            result = {}
            for row in pending:
                if self._cancel_requested:
                    raise InterruptedError("Candidate checks cancelled; no generation was submitted.")
                plan = GenerationPlan.from_dict(row["plan"])
                plan = plan.revise({"safety_policy": {**plan.safety_policy, "state": "allowed", "reviewed": True}},
                    source="explicit_candidate_content_review")
                row["plan"] = plan.to_dict()
                if plan.source_identity != self.owner.plan.source_identity:
                    raise ValueError("Reopen this group from its exact source image before checking it.")
                current = self.owner.source_current_provider() if callable(self.owner.source_current_provider) else self.owner.source_current
                if plan.source is not None and not callable(self.owner.source_current_provider):
                    raise ValueError("Reconnect the original source before checking this group.")
                adapter.source_current_provider = self.owner.source_current_provider
                adapter.source_image_provider = self.owner.source_image_provider
                adapter.reference_image_provider = self.owner._reference_image
                prepared = adapter.prepare(plan, binding, self.owner.registry, source_current=current)
                if prepared.get("valid") is False:
                    raise ValueError(f"{row['label']}: preparation blocked this candidate.")
                result[row["id"]] = prepared
                if len(json.dumps(result)) > 64 * 1024 * 1024:
                    raise ValueError("Prepared group is too large. Use fewer alternatives.")
            try:
                from core.generation_resource_guard import summarize_group_costs
                observe = getattr(adapter, "observe_resources", None)
                observation = observe() if callable(observe) else None
                costs = summarize_group_costs(list(result.values()), observation, self.service.history())
            except Exception:
                costs = {"message": "Queue time and retained-file size estimates are unavailable. Jobs run one at a time."}
            return result, costs
        def done(value):
            result, costs = value
            if self._cancel_requested:
                raise InterruptedError("Candidate checks cancelled; no generation was submitted.")
            if identity != self._identity(): raise ValueError("The source request or connection changed. Check this group again.")
            self.prepared = result; self.review_identity = identity
            by_id = {row["id"]: row for row in pending}
            for row in self.experiment["candidates"]:
                if row["id"] in by_id: row["plan"] = by_id[row["id"]]["plan"]
            summaries = []
            for row in pending:
                plan = GenerationPlan.from_dict(row["plan"]); request = plan.request
                finish = plan.target_context.get("deferred_finishing", {}).get("output_goal", {})
                summaries.append(f"{row['label']}\nPrompt: {request.positive_prompt}\nAvoid: {request.negative_prompt or '(none)'}\n"
                    f"Model: {request.model} · {request.width} × {request.height} · {request.steps} steps · Guidance {request.cfg_scale}\n"
                    f"Seed: {request.seed}\nAdditions: " + (", ".join(r.name or r.role for r in request.resources if r.role != 'checkpoint') or "none")
                    + (f"\nFinish later: {finish.get('final_width')} × {finish.get('final_height')}" if finish else ""))
            self.details.setPlainText(costs["message"] + "\n\n" + "\n\n".join(summaries))
            self.status.setText(f"{len(result)} candidates checked. Review exact prompts, seeds, resources and output stages below; then authorize the group.")
            self._run("Save reviewed candidate group", self._persist, self._saved)
        self._run("Check generation alternatives", work, done)

    def show_exact_details(self):
        if self.busy() or not self.experiment: return
        dialog = FeedbackEnabledDialog(self); dialog.setWindowTitle("Exact Candidate Requests"); dialog.resize(900, 700)
        layout = QVBoxLayout(dialog); text = QTextEdit(); text.setReadOnly(True)
        text.setPlainText(json.dumps(self.experiment, indent=2, ensure_ascii=False)); layout.addWidget(text)
        close = QPushButton("Close"); close.clicked.connect(dialog.accept); layout.addWidget(close); dialog.exec()

    def start_group(self):
        if not self.start.isEnabled() or self.busy(): return
        self._running = True; self._pause = False; self._cancel_requested = False; self._advance()

    def _advance(self):
        if self._task is not None or self._job is not None: return
        if self._pause or self.owner._shutdown_requested:
            self._running = False; self.refresh_actions(); self.owner._notify_background_work_finished(); return
        row = next((r for r in self.experiment["candidates"] if r["state"] == "pending"), None)
        if row is None:
            self._running = False; self.status.setText("Group finished. Select favorites to compare, finish, or send to Ingest.")
            self.refresh_actions(); return
        if row["id"] not in self.prepared or self.review_identity != self._identity():
            self._failed("The reviewed setup changed. Check the remaining candidates again."); return
        self._row_id = row["id"]; row["state"] = "submitting"
        self._terminal_payload = None
        prepared = deepcopy(self.prepared[row["id"]]); adapter = self.owner.adapter
        def submit():
            digest = self._persist()
            self.digest = digest
            job = self.service.submit(adapter, prepared, confirmed=True,
                listener=lambda job: self._terminal_signal.emit({"id": row["id"], "receipt": getattr(job, "result", None),
                    "error": str(getattr(job, "error", "")), "state": str(getattr(job, "state", ""))}))
            return digest, job
        def done(result):
            self.digest, self._job = result
            if self._terminal_payload is not None:
                payload = self._terminal_payload; self._terminal_payload = None; self._terminal(payload)
            else:
                if self.owner._shutdown_requested or self._cancel_requested:
                    self.cancel_current()
                self.refresh_tree()
        self._run("Submit reviewed candidate", submit, done)

    def _terminal(self, payload):
        if payload["id"] != self._row_id: return
        if self._task is not None:
            self._terminal_payload = payload; return
        row = next(r for r in self.experiment["candidates"] if r["id"] == payload["id"])
        receipt = payload.get("receipt")
        if isinstance(receipt, dict):
            row["job_id"] = receipt.get("job_id", "")
            state = receipt.get("state")
            row["state"] = "verified" if state == "verified" else state if state in {"failed", "cancelled"} and (receipt.get("server_stopped") or receipt.get("proven_unsubmitted")) else "outcome_unknown"
        else: row["state"] = "outcome_unknown"
        row["error"] = payload.get("error", "")
        self._job = None
        if row["state"] != "verified": self._pause = True
        def done(digest):
            self._saved(digest); self._advance()
        self._run("Retain candidate outcome", self._persist, done)

    def pause_after_current(self):
        self._pause = True; self.status.setText("The current candidate will finish; remaining candidates stay pending.")

    def cancel_current(self):
        self._pause = True
        self._cancel_requested = True
        thumbnail_only = self._thumb_task is not None and self._job is None and self._task is None
        if self._job is not None:
            self.owner.jobs.cancel(self._job.id)
        if self._thumb_task is not None:
            self._thumb_cancelled = True
            self._thumb_revision += 1
            if self._thumb_task.job_id:
                self.owner.jobs.cancel(self._thumb_task.job_id)
        self.status.setText("Thumbnail check cancellation requested." if thumbnail_only else
            "Cancellation requested. The outcome will be reconciled before retry is allowed.")

    def request_shutdown(self):
        self.cancel_current()
        if self._saved_work_dialog is not None:
            self._saved_work_dialog.request_background_shutdown()
        for child in self._finishing_dialogs:
            child.request_background_shutdown()
        if self._job is None and self._task is None: self._running = False

    def retry_selected(self):
        if self.busy(): return
        rows = deepcopy(self.selected())
        if not rows: return
        def check():
            receipts = []
            for row in rows:
                if not row.get("job_id"):
                    raise ValueError("Reconcile the exact retained job before retrying this candidate.")
                receipt = self.service.recover(row["job_id"], adapter=self.owner.adapter)
                if (receipt.get("state") not in {"failed", "cancelled"}
                        or not (receipt.get("proven_unsubmitted") or receipt.get("server_stopped"))
                        or receipt.get("prepared", {}).get("plan", {}).get("digest") != row["plan"]["digest"]):
                    raise ValueError("This candidate has no confirmed failed or cancelled outcome. Reconcile it; an uncertain job cannot be retried.")
                receipts.append(receipt)
            return retry_failed(self.experiment, [r["id"] for r in rows], receipts=receipts)
        def done(result):
            self.experiment = result
            self.prepared = {}; self.ack.setChecked(False)
            self._run("Retain candidate retry", self._persist, self._saved)
        self._run("Verify retry authority", check, done)

    def reconcile(self):
        if self.busy() or not self.experiment: return
        value = deepcopy(self.experiment)
        def work():
            receipts = self.service.history()
            ids = {r["plan"]["plan_id"] for r in value["candidates"]}
            relevant = [self.service.recover(r["job_id"], adapter=self.owner.adapter)
                        for r in receipts if r.get("prepared", {}).get("plan", {}).get("plan_id") in ids]
            return reconcile_experiment(value, relevant)
        def done(result):
            self.experiment = result; self.prepared = {}; self.ack.setChecked(False)
            self._run("Save reconciled group", self._persist, self._saved)
        self._run("Reconcile candidate outcomes", work, done)

    def _verified(self, rows):
        result = []
        for row in rows:
            receipt = self.service.recover(row["job_id"])
            if receipt.get("prepared", {}).get("plan", {}).get("digest") != row["plan"]["digest"]:
                raise ValueError("The saved candidate does not match its generation receipt. Reconcile this group.")
            paths = self.service.verified_output_paths(receipt)
            result.append((row, receipt, paths))
        return result

    def compare_selected(self):
        rows = deepcopy(self.selected())
        if self.busy() or not 1 <= len(rows) <= 4 or any(r["state"] != "verified" for r in rows): return
        if self.experiment.get("grid"):
            rows.sort(key=lambda row: (row["grid_position"]["row"], row["grid_position"]["column"]))
        def work():
            from core.training_source_io import read_stable_training_source
            values = []
            for row, receipt, paths in self._verified(rows):
                raw, identity = read_stable_training_source(Path(paths[0]))
                if identity["sha256"] != receipt["outputs"][0]["sha256"]: raise ValueError("A result changed while opening comparison.")
                values.append((row["label"], paths[0], raw))
            return values
        def show(values):
            dialog = FeedbackEnabledDialog(self); dialog.setWindowTitle("Compare Generation Results"); dialog.resize(1200, 800)
            layout = QVBoxLayout(dialog); grid = QGridLayout(); layout.addLayout(grid, 1)
            for index, (label, path, raw) in enumerate(values):
                cell = QWidget(); box = QVBoxLayout(cell); title = QLabel(label); title.setWordWrap(True); title.setTextFormat(Qt.PlainText)
                box.addWidget(title); view = ProtectedImageView(settings=self.owner.settings); view.set_image(QPixmap.fromImage(QImage.fromData(raw)), path)
                box.addWidget(view, 1); grid.addWidget(cell, index // 2, index % 2)
            close = QPushButton("Close"); close.clicked.connect(dialog.accept); layout.addWidget(close)
            dialog.exec()
        self._run("Verify selected comparison images", work, show)

    def use_selected(self):
        rows = self.selected()
        if self.busy() or len(rows) != 1: return
        plan = GenerationPlan.from_dict(rows[0]["plan"]).fork()
        def opened(_draft):
            self.status.setText(
                "A separate Generate draft opened from this candidate. The saved comparison "
                "and original request are unchanged; review the new draft before saving or submitting."
            )
            self.hide()
        self.status.setText("Opening a separate draft from the selected candidate. This group remains saved if it cannot open.")
        self.owner.result_action_requested.emit({
            "action": "same_seed", "paths": [], "plan": plan, "on_opened": opened,
        })

    def finish_selected(self):
        rows = deepcopy(self.selected())
        if self.busy() or not rows or any(r["state"] != "verified" for r in rows): return
        def done(values):
            from .generation_finishing_dialog import GenerationFinishingDialog
            plans = {}
            labels = {}
            for row, receipt, paths in values:
                for path in paths:
                    plans[Path(path).absolute()] = GenerationPlan.from_dict(row["plan"])
                    labels[Path(path).absolute()] = row["label"][:120]
            def verified_source():
                return [path for _row, _receipt, paths in self._verified(rows) for path in paths]
            child = GenerationFinishingDialog(self.owner.jobs, self.owner.storage_root, list(plans),
                verified_source, parent=self, initial_goals={path: plan.target_context.get(
                    "deferred_finishing", {}).get("output_goal", {}) for path, plan in plans.items()},
                settings=self.owner.settings)
            for index, path in enumerate(plans):
                child.winner.setItemText(index, labels[path])
            def refine(value):
                self.owner.result_action_requested.emit({"action": "remix", "paths": [value["path"]],
                    "plan": None, "finish_context": {"output_goal": value["output_goal"]},
                    "finish_source_plan": plans[Path(value["path"]).absolute()], "finish_strength": value["strength"],
                    "verified_source": value["verified_source"], "on_opened": child.close})
            child.refine_requested.connect(refine)
            child.ingest_requested.connect(self.owner.ingest_requested.emit)
            child.background_work_finished.connect(self.owner._notify_background_work_finished)
            child.background_work_finished.connect(self.refresh_actions)
            self._finishing_dialogs.append(child)
            child.show()
            child.inspect_winner()
            self.status.setText("Choose a faithful resized copy or a refinement draft. Switch between selected winners in the finishing window.")
        self._run("Verify selected finishing sources", lambda: self._verified(rows), done)

    def ingest_selected(self):
        rows = deepcopy(self.selected())
        if self.busy() or not rows or any(r["state"] != "verified" for r in rows): return
        self._run("Verify selected Ingest results", lambda: self._verified(rows),
            lambda values: self.owner.ingest_requested.emit([p for _row, _receipt, paths in values for p in paths]))

    def reject(self):
        # This retained child belongs to the draft; hiding never cancels or resubmits.
        self.hide()

    def closeEvent(self, event):
        self.hide(); event.ignore()
