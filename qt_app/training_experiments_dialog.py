"""Bounded nonmodal comparison and iteration over retained training runs."""
from __future__ import annotations

import json
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt, QTimer, Signal
from PySide6.QtGui import QColor
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialogButtonBox, QHBoxLayout,
    QHeaderView, QLabel, QLineEdit, QMessageBox, QPlainTextEdit, QPushButton,
    QTableWidget, QTableWidgetItem, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from core.training_experiments import TrainingExperiments
from core.training_run_index import TrainingRunPage
from .components import EmptyStatePanel
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask


PAGE_SIZE = 100


class TrainingExperimentsDialog(FeedbackEnabledDialog):
    """The list is cheap; review/package validation happens in a worker."""

    openRunRequested = Signal(object, str)
    draftCreated = Signal(object)
    evaluationRequested = Signal(object)
    prepareRequested = Signal()
    editorCloseReady = Signal()
    feedback_workflow = "Training experiments"

    def __init__(self, index, jobs, draft_store_path, parent=None):
        super().__init__(parent)
        self.service = TrainingExperiments(index, draft_store_path)
        self.jobs = jobs
        self._task = None
        self._closing = False
        self._cancel_requested = False
        self._revision = ""
        self._records = ()
        self._offset = 0
        self._next_offset = None
        self._editing_identity = None
        self._clean_annotation = None
        self._pending_navigation = None
        self._after_saved_navigation = None
        self._allow_close = False
        self._active_filter = ("", False, "")
        self.setModal(False)
        self.setWindowTitle("Training Experiments")
        self.resize(1020, 800)
        layout = QVBoxLayout(self)
        heading = QLabel("Training Experiments")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        intro = QLabel("Browse saved training runs and compare two execution runs. A cloned draft keeps the "
            "frozen package plan for editing; sources must be checked again before preparation.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        bar = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self.refresh)
        bar.addWidget(self.refresh_button)
        self.summary = QLabel("Waiting to read saved runs.")
        bar.addWidget(self.summary)
        bar.addStretch(1)
        layout.addLayout(bar)
        filters = QHBoxLayout()
        self.search_edit = QLineEdit()
        self.search_edit.setPlaceholderText("Search saved name, notes or run ID")
        self.search_edit.setAccessibleName("Search saved training runs")
        self.search_edit.returnPressed.connect(self._apply_filters)
        filters.addWidget(self.search_edit, 2)
        self.favorite_filter = QCheckBox("Favorites only")
        filters.addWidget(self.favorite_filter)
        self.status_filter = QComboBox()
        self.status_filter.setAccessibleName("Run status filter")
        self.status_filter.addItem("All statuses", "")
        for phase in ("prepared", "ready", "starting", "running", "held", "stopping",
                "recovery_required", "stopped", "cancelled", "failed", "interrupted",
                "verification_required", "registration_required", "completed"):
            self.status_filter.addItem(phase.replace("_", " ").title(), phase)
        filters.addWidget(self.status_filter)
        self.apply_filter_button = QPushButton("Apply Filters")
        self.apply_filter_button.clicked.connect(self._apply_filters)
        filters.addWidget(self.apply_filter_button)
        layout.addLayout(filters)
        self.runs = QTreeWidget()
        self.runs.setHeaderLabels(["Run", "Phase", "Favorite", "Kind"])
        self.runs.setRootIsDecorated(False)
        self.runs.setUniformRowHeights(True)
        self.runs.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.runs.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.runs.header().setSectionResizeMode(0, QHeaderView.Stretch)
        for column in (1, 2, 3):
            self.runs.header().setSectionResizeMode(column, QHeaderView.ResizeToContents)
        self.runs.itemSelectionChanged.connect(self._selection_changed)
        layout.addWidget(self.runs, 2)
        self.empty_state = EmptyStatePanel("Prepare a local run to compare its retained review.",
            "No Training Experiments")
        self.prepare_button = self.empty_state.add_action("Go to Prepare Training", self._prepare_training)
        self.clear_filters_button = self.empty_state.add_action("Clear Filters", self._clear_filters)
        self.empty_state.hide()
        layout.addWidget(self.empty_state, 1)
        pages = QHBoxLayout()
        self.previous_button = QPushButton("Previous")
        self.previous_button.clicked.connect(lambda: self._navigate_page(max(0, self._offset - PAGE_SIZE)))
        pages.addWidget(self.previous_button)
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(lambda: self._navigate_page(self._next_offset))
        pages.addWidget(self.next_button)
        pages.addStretch(1)
        layout.addLayout(pages)
        self.name_edit = QLineEdit()
        self.name_edit.setPlaceholderText("Experiment name")
        self.name_edit.setMaxLength(100)
        layout.addWidget(self.name_edit)
        self.notes_edit = QPlainTextEdit()
        self.notes_edit.setPlaceholderText("Private notes about this run")
        self.notes_edit.setMaximumHeight(90)
        layout.addWidget(self.notes_edit)
        self.favorite = QCheckBox("Favorite")
        layout.addWidget(self.favorite)
        self.save_button = QPushButton("Save Name and Notes")
        self.save_button.clicked.connect(self._save_annotation)
        layout.addWidget(self.save_button)
        self.clone_name = QLineEdit()
        self.clone_name.setPlaceholderText("New draft name")
        self.clone_name.setMaxLength(100)
        self.clone_name.textChanged.connect(self._actions)
        layout.addWidget(self.clone_name)
        self.detail = QLabel("Select a run to inspect it, or two to compare exact inputs.")
        self.detail.setWordWrap(True)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(self.detail)
        self.outcome_summary = QLabel()
        self.outcome_summary.setWordWrap(True)
        self.outcome_summary.setTextFormat(Qt.PlainText)
        self.outcome_summary.setAccessibleName('Compared retained run outcomes')
        self.outcome_summary.hide()
        layout.addWidget(self.outcome_summary)
        self.comparison = QPlainTextEdit()
        self.comparison.setReadOnly(True)
        self.comparison.setPlaceholderText("Exact compared settings will appear here.")
        self.comparison.setMaximumHeight(150)
        self.comparison.hide()
        self.comparison_table = QTableWidget(0, 3)
        self.comparison_table.setHorizontalHeaderLabels(("Field", "First run", "Second run"))
        self.comparison_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.comparison_table.setSelectionMode(QAbstractItemView.NoSelection)
        self.comparison_table.setWordWrap(True)
        self.comparison_table.setMinimumHeight(260)
        self.comparison_table.setMaximumHeight(300)
        self.comparison_table.horizontalHeader().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.comparison_table.horizontalHeader().setSectionResizeMode(1, QHeaderView.Stretch)
        self.comparison_table.horizontalHeader().setSectionResizeMode(2, QHeaderView.Stretch)
        self.comparison_table.hide()
        layout.addWidget(self.comparison_table)
        self.advanced_button = QPushButton("Advanced: Exact Comparison JSON")
        self.advanced_button.setCheckable(True)
        self.advanced_button.toggled.connect(self.comparison.setVisible)
        self.advanced_button.hide()
        layout.addWidget(self.advanced_button)
        layout.addWidget(self.comparison)
        self.status = QLabel("Waiting to read saved runs.")
        self.status.setWordWrap(True)
        layout.addWidget(self.status)
        actions = QHBoxLayout()
        self.cancel_button = QPushButton("Cancel Check")
        self.cancel_button.clicked.connect(self._cancel)
        self.cancel_button.hide()
        actions.addWidget(self.cancel_button)
        actions.addStretch(1)
        self.open_button = QPushButton("Open Run")
        self.open_button.clicked.connect(self._open)
        actions.addWidget(self.open_button)
        self.compare_button = QPushButton("Compare Selected")
        self.compare_button.clicked.connect(self._compare)
        actions.addWidget(self.compare_button)
        self.evaluate_button = QPushButton("Review Evaluation")
        self.evaluate_button.clicked.connect(self._evaluate)
        actions.addWidget(self.evaluate_button)
        self.clone_button = QPushButton("Create Draft From Run")
        self.clone_button.setProperty("primary", True)
        self.clone_button.clicked.connect(self._clone)
        actions.addWidget(self.clone_button)
        layout.addLayout(actions)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self._actions()
        QTimer.singleShot(0, self.refresh)

    def refresh(self):
        self._navigate_page(0)

    def _apply_filters(self):
        chosen = (self.search_edit.text().strip(), self.favorite_filter.isChecked(),
            self.status_filter.currentData())
        def apply():
            self._active_filter = chosen
            self._request_page(0)
        self._guard_unsaved(apply)

    def _clear_filters(self):
        if self._task is not None or self._closing:
            return
        self.search_edit.clear()
        self.favorite_filter.setChecked(False)
        self.status_filter.setCurrentIndex(0)
        self._apply_filters()

    def _prepare_training(self):
        if self._task is not None or self._closing:
            return
        def leave():
            self._allow_close = True
            super(TrainingExperimentsDialog, self).reject()
            self.prepareRequested.emit()
        self._guard_unsaved(leave)

    def _navigate_page(self, offset):
        if offset is not None:
            self._guard_unsaved(lambda: self._request_page(offset))

    def _annotation_values(self):
        return (self.name_edit.text(), self.notes_edit.toPlainText(), self.favorite.isChecked())

    def _annotation_dirty(self):
        return self._editing_identity is not None and self._clean_annotation is not None and (
            self._annotation_values() != self._clean_annotation)

    def has_unsaved_changes(self):
        """Allow the shell to preflight a close using this dialog's annotation authority."""
        return self._annotation_dirty()

    def _guard_unsaved(self, proceed, *, on_saved=None):
        if self._task is not None or self._closing:
            return False
        if not self._annotation_dirty():
            proceed()
            return True
        choice = QMessageBox.question(self, "Unsaved experiment notes",
            "Save changes to this run before leaving it?",
            QMessageBox.Save | QMessageBox.Discard | QMessageBox.Cancel,
            QMessageBox.Cancel)
        if choice == QMessageBox.Cancel:
            return False
        if choice == QMessageBox.Discard:
            self._editing_identity, self._clean_annotation = None, None
            proceed()
            return True
        self._pending_navigation = proceed
        self._after_saved_navigation = on_saved
        self._save_annotation()
        return False

    def _request_page(self, offset):
        if self._task is not None or self._closing or type(offset) is not int or offset < 0:
            return
        self._invalidate("Reading saved training runs…")
        query, favorite_only, status = self._active_filter
        self._launch("page", lambda job: self.service.page(offset=offset, limit=PAGE_SIZE,
            query=query, favorite_only=favorite_only, status=status,
            should_cancel=lambda: job.cancel_requested))

    def _launch(self, action, operation):
        if self._task is not None or self._closing:
            return
        result = {}
        def work(job):
            if job.cancel_requested:
                raise InterruptedError("Experiment check cancelled.")
            value = operation(job)
            result["value"] = value
            return value
        task = CoordinatedCallTask(self.jobs, "Training experiments: " + action,
            work, resource="io")
        self._task = task
        self._cancel_requested = False
        self.cancel_button.show()
        self.status.setText("Checking selected training evidence…")
        task.signals.complete.connect(lambda value: self._finish(task, action, result.get("value", value)))
        task.signals.failed.connect(lambda error: self._finish(task, action, error=error))
        task.signals.cancelled.connect(lambda: self._finish(task, action,
            value=result.get("value"), error="Experiment check cancelled at a safe boundary."))
        self._actions()
        try:
            task.start()
        except Exception as exc:
            self._finish(task, action, error=str(exc))

    def _finish(self, task, action, value=None, error=""):
        if task is not self._task:
            return
        self._task = None
        self.cancel_button.hide()
        if self._closing:
            self._closing = False
            if action == "annotate" and value is None:
                self._pending_navigation = None
                self._after_saved_navigation = None
                self.status.setText(str(error or "The annotation was not saved.")[:2000])
                self._actions()
                return
            if action == "annotate":
                self._clean_annotation = self._annotation_values()
                self._pending_navigation = None
                self._after_saved_navigation = None
                self._allow_close = True
                super().reject()
                return
            self.reject()
            return
        if self._cancel_requested and not (action in {"annotate", "clone"} and value is not None):
            self._pending_navigation = None
            self._after_saved_navigation = None
            self.status.setText("Experiment check cancelled. Refresh to continue.")
        elif error and value is None:
            if action == "annotate":
                self._pending_navigation = None
                self._after_saved_navigation = None
            if action == "page":
                self._pending_navigation = None
                self._after_saved_navigation = None
            self.status.setText(str(error)[:2000])
        elif action == "page":
            self._loaded(value)
        elif action == "annotate":
            self.status.setText("Experiment name and notes saved. Reloading the list.")
            self._clean_annotation = self._annotation_values()
            self._request_page(self._offset)
            return
        elif action == "compare":
            labels = value.differences
            self.detail.setText("Exact retained reviews compared. Differences: "
                + (", ".join(labels) if labels else "none in the compared fields")
                + ". Recorded process outcomes and observed training measurements are shown below. Model output and image quality are not reverified here.")
            def outcome_line(label, outcome):
                measured = (f" · observed step {outcome.observed_step:,}"
                    if outcome.observed_step is not None else "")
                if outcome.observed_loss is not None:
                    measured += f" · last observed training loss {outcome.observed_loss:.6g}"
                trial = f" · trial {outcome.trial_status}" if outcome.trial_status != "none" else ""
                return (f"{label}: indexed {outcome.indexed_phase} · process {outcome.process_state}"
                    + (f" · exit {outcome.exit_code}" if outcome.exit_code is not None else "")
                    + measured + trial)
            self.outcome_summary.setText(outcome_line("FIRST", value.first_outcome) + "\n"
                + outcome_line("SECOND", value.second_outcome))
            self.outcome_summary.show()
            def fields(review, outcome):
                manifest = review.candidate_manifest
                configuration = review.configuration
                return {"run_id": review.record.run_id,
                    "outcome": {"indexed_phase": outcome.indexed_phase,
                        "process_state": outcome.process_state,
                        "exit_code": outcome.exit_code,
                        "observed_optimizer_step": outcome.observed_step,
                        "observed_training_loss": outcome.observed_loss,
                        "observed_gaps": outcome.observed_gaps,
                        "trial_status": outcome.trial_status,
                        "evidence": outcome.evidence},
                    "package_fingerprint": review.package_fingerprint,
                    "goal": manifest.get("goal"),
                    "selection_scope": manifest.get("selection_scope"),
                    "profile": manifest.get("profile"),
                    "model": configuration.get("target"),
                    "seed": configuration.get("optimization_request", {}).get("seed"),
                    "configuration": configuration}
            self.comparison.setPlainText("FIRST\n" + json.dumps(fields(value.first, value.first_outcome),
                indent=2, ensure_ascii=False) + "\n\nSECOND\n" + json.dumps(fields(value.second, value.second_outcome),
                indent=2, ensure_ascii=False))
            self._show_comparison_table(value)
            self.advanced_button.show()
            self.status.setText("Comparison complete.")
        elif action == "clone":
            self.draftCreated.emit(value)
            self.status.setText("New draft saved. Recheck sources and settings before preparation.")
        elif action == "evaluate":
            self.evaluationRequested.emit(value)
            self.status.setText("Selected run sent for evaluation review.")
        self._actions()

    def _show_comparison_table(self, comparison):
        first, second = comparison.first, comparison.second
        left, right = first.candidate_manifest, second.candidate_manifest
        a, b = first.configuration, second.configuration
        first_outcome, second_outcome = comparison.first_outcome, comparison.second_outcome
        def section(value, key):
            nested = value.get(key) if isinstance(value, dict) else None
            return nested if isinstance(nested, dict) else {}
        def scope_count(manifest, key):
            scope = section(manifest, "selection_scope")
            value = scope.get(key)
            if value is None and key == "candidate_count":
                frozen = scope.get("frozen_selection")
                value = len(frozen) if isinstance(frozen, list) else None
            return value
        def recipe(review):
            indexed = getattr(review, "indexed", None)
            value = getattr(indexed, "value", None)
            return section(section(value, "package"), "recipe")
        goal_a, goal_b = section(left, "goal"), section(right, "goal")
        profile_a, profile_b = section(left, "profile"), section(right, "profile")
        target_a, target_b = section(a, "target"), section(b, "target")
        weight_a, weight_b = section(a, "weighting"), section(b, "weighting")
        optimize_a, optimize_b = section(a, "optimization_request"), section(b, "optimization_request")
        preparation_a, preparation_b = section(a, "image_preparation"), section(b, "image_preparation")
        recipe_a, recipe_b = recipe(first), recipe(second)
        rows = [
            ("Run", first.record.annotation.name or first.record.run_id[:8],
                second.record.annotation.name or second.record.run_id[:8]),
            ("Goal", goal_a.get("text") or goal_a.get("name"),
                goal_b.get("text") or goal_b.get("name")),
            ("Goal type", goal_a.get("kind"), goal_b.get("kind")),
            ("Model family", target_a.get("model_family"), target_b.get("model_family")),
            ("Model version", target_a.get("model_version"), target_b.get("model_version")),
            ("Caption profile", profile_a.get("name") or profile_a.get("profile_id") or profile_a.get("id"),
                profile_b.get("name") or profile_b.get("profile_id") or profile_b.get("id")),
            ("Seed", optimize_a.get("seed"), optimize_b.get("seed")),
            ("Learning rate", optimize_a.get("learning_rate"), optimize_b.get("learning_rate")),
            ("Epochs", weight_a.get("epochs"), weight_b.get("epochs")),
            ("Batch size", optimize_a.get("batch_size"), optimize_b.get("batch_size")),
            ("LoRA rank", recipe_a.get("rank"), recipe_b.get("rank")),
            ("Precision", optimize_a.get("precision"), optimize_b.get("precision")),
            ("Collection", section(left, "selection_scope").get("source_label"),
                section(right, "selection_scope").get("source_label")),
            ("Frozen candidates", scope_count(left, "candidate_count"),
                scope_count(right, "candidate_count")),
            ("Processed candidates", scope_count(left, "processed_count"),
                scope_count(right, "processed_count")),
            ("Training images", section(left, "counts").get("training"),
                section(right, "counts").get("training")),
            ("Validation images", section(left, "counts").get("validation"),
                section(right, "counts").get("validation")),
            ("Package fingerprint", first.package_fingerprint, second.package_fingerprint),
            ("Base model ID", target_a.get("base_model_id"), target_b.get("base_model_id")),
            ("Resolution", preparation_a.get("target_resolution"), preparation_b.get("target_resolution")),
            ("Repeats", weight_a.get("repeats"), weight_b.get("repeats")),
            ("Gradient accumulation", optimize_a.get("gradient_accumulation"),
                optimize_b.get("gradient_accumulation")),
            ("Process", first_outcome.process_state, second_outcome.process_state),
            ("Observed step", first_outcome.observed_step, second_outcome.observed_step),
            ("Last training loss", first_outcome.observed_loss, second_outcome.observed_loss),
        ]
        for root_key in sorted((set(a) | set(b)) - {"schema_version", "adapter", "target",
                "expected_output", "image_preparation", "captions", "weighting",
                "optimization_request", "recommendation"}):
            rows.append((root_key.replace("_", " ").title(), a.get(root_key), b.get(root_key)))
        for key in sorted((set(optimize_a) | set(optimize_b)) - {"batch_size",
                "gradient_accumulation", "learning_rate", "precision", "seed",
                "translation_status"}):
            rows.append((key.replace("_", " ").title(), optimize_a.get(key), optimize_b.get(key)))
        rows = [(label, x, y) for label, x, y in rows if x is not None or y is not None]
        self.comparison_table.setRowCount(len(rows))
        different_color = QColor("#614922" if self.palette().window().color().lightness() < 128
            else "#fff0c2")
        for row_number, (label, first_value, second_value) in enumerate(rows):
            different = first_value != second_value
            def display(value):
                if value is None:
                    return "Not recorded"
                if isinstance(value, dict):
                    return "; ".join(f"{key.replace('_', ' ')}: {display(part)}"
                        for key, part in sorted(value.items()))
                if isinstance(value, (list, tuple)):
                    return f"{len(value)} items (open Advanced for the exact list)"
                if isinstance(value, bool):
                    return "Yes" if value else "No"
                return str(value)
            for column, value in enumerate((label + (" · changed" if different else ""),
                    display(first_value), display(second_value))):
                item = QTableWidgetItem(value)
                if column:
                    item.setToolTip(json.dumps((first_value, second_value)[column - 1],
                        ensure_ascii=False, sort_keys=True, default=str))
                if different:
                    item.setBackground(different_color)
                self.comparison_table.setItem(row_number, column, item)
            self.comparison_table.setRowHeight(row_number, 42)
        self.comparison_table.show()

    def _loaded(self, page):
        if not isinstance(page, TrainingRunPage) or len(page.records) > PAGE_SIZE:
            self._pending_navigation = None
            self._after_saved_navigation = None
            self._invalidate("The run index returned an invalid page.")
            return
        if page.total and not page.records and page.offset:
            self._request_page(0)
            return
        self._records, self._revision = page.records, page.revision
        self._offset, self._next_offset = page.offset, page.next_offset
        self._editing_identity, self._clean_annotation = None, None
        self.runs.clear()
        self.comparison.clear()
        self.comparison_table.hide()
        self.advanced_button.setChecked(False)
        self.advanced_button.hide()
        self.outcome_summary.hide()
        for record in page.records:
            name = record.annotation.name or Path(record.destination).name or record.run_id[:8]
            kind = {"execution": "SDXL run", "readiness": "Readiness check",
                "anima": "Anima run", "validation": "Held-out validation"}.get(
                    record.kind, record.kind.replace("_", " ").title())
            item = QTreeWidgetItem([name, record.phase.replace("_", " ").title(),
                "Yes" if record.annotation.favorite else "", kind])
            item.setData(0, Qt.UserRole, (record.run_id, record.sequence))
            self.runs.addTopLevelItem(item)
        filtered = any((self._active_filter[0], self._active_filter[1], self._active_filter[2]))
        count = "Matching reviews" if filtered else "Saved reviews"
        self.summary.setText(f"{count}: {page.total:,} · page {page.offset // PAGE_SIZE + 1:,}")
        self.empty_state.setTitle("No matching saved runs" if filtered else "No Training Experiments")
        self.empty_state.setText("Change the search or filters to see saved runs." if filtered
            else "Prepare a local run to compare its retained review.")
        self.prepare_button.setVisible(not filtered)
        self.clear_filters_button.setVisible(filtered)
        self.empty_state.setVisible(page.total == 0)
        self.runs.setVisible(page.total != 0)
        self.status.setText("Select a run to open or annotate; execution runs can be compared or cloned." if page.records else "")
        self._selection_changed()
        pending, self._pending_navigation = self._pending_navigation, None
        on_saved, self._after_saved_navigation = self._after_saved_navigation, None
        if pending is not None:
            pending()
            if on_saved is not None:
                on_saved()

    def _invalidate(self, message):
        self._records, self._revision = (), ""
        self.runs.clear()
        self.comparison.clear()
        self.comparison_table.hide()
        self.advanced_button.setChecked(False)
        self.advanced_button.hide()
        self.outcome_summary.hide()
        self.empty_state.hide()
        self.runs.show()
        self.status.setText(message)
        self.summary.setText("Saved training experiments unavailable until this check completes.")
        self._actions()

    def _selected(self):
        identities = [item.data(0, Qt.UserRole) for item in self.runs.selectedItems()]
        return tuple(record for record in self._records
            if (record.run_id, record.sequence) in identities)

    def _selection_changed(self):
        identities = tuple(item.data(0, Qt.UserRole) for item in self.runs.selectedItems())
        if (self._editing_identity is not None and identities != (self._editing_identity,)
                and self._annotation_dirty()):
            with QSignalBlocker(self.runs):
                for index in range(self.runs.topLevelItemCount()):
                    item = self.runs.topLevelItem(index)
                    item.setSelected(item.data(0, Qt.UserRole) == self._editing_identity)
            self._guard_unsaved(lambda: self._select_identities(identities))
            return
        self.outcome_summary.hide()
        self.comparison.clear()
        self.comparison_table.hide()
        self.advanced_button.setChecked(False)
        self.advanced_button.hide()
        rows = self._selected()
        if len(rows) == 1:
            annotation = rows[0].annotation
            self._editing_identity = (rows[0].run_id, rows[0].sequence)
            self._clean_annotation = (annotation.name, annotation.notes, annotation.favorite)
            self.name_edit.setText(annotation.name)
            self.notes_edit.setPlainText(annotation.notes)
            self.favorite.setChecked(annotation.favorite)
            self.detail.setText("Exact review and package bytes will be checked before comparing or cloning. Evaluation opens this run's results; verify its output or export a checkpoint there."
                if rows[0].kind == "execution" else
                "This retained run can be opened and annotated. Comparison, evaluation and cloning require an execution run.")
        elif len(rows) > 2:
            self._editing_identity, self._clean_annotation = None, None
            self.name_edit.clear(); self.notes_edit.clear(); self.favorite.setChecked(False)
            self.detail.setText("Select at most two runs for comparison.")
        elif len(rows) == 0:
            self._editing_identity, self._clean_annotation = None, None
            self.name_edit.clear(); self.notes_edit.clear(); self.favorite.setChecked(False)
            self.detail.setText("Select a run to inspect it, or two to compare exact inputs.")
        else:
            self._editing_identity, self._clean_annotation = None, None
            self.name_edit.clear(); self.notes_edit.clear(); self.favorite.setChecked(False)
        self._actions()

    def _select_identities(self, identities):
        with QSignalBlocker(self.runs):
            for index in range(self.runs.topLevelItemCount()):
                item = self.runs.topLevelItem(index)
                item.setSelected(item.data(0, Qt.UserRole) in identities)
        self._selection_changed()

    def _actions(self):
        idle = self._task is None and not self._closing
        rows = self._selected() if idle else ()
        single = len(rows) == 1 and rows[0].kind == "execution" and bool(self._revision)
        pair = len(rows) == 2 and all(row.kind == "execution" for row in rows) and bool(self._revision)
        self.refresh_button.setEnabled(idle)
        self.search_edit.setEnabled(idle)
        self.favorite_filter.setEnabled(idle)
        self.status_filter.setEnabled(idle)
        self.apply_filter_button.setEnabled(idle)
        self.previous_button.setEnabled(idle and self._offset > 0)
        self.next_button.setEnabled(idle and self._next_offset is not None)
        self.open_button.setEnabled(idle and len(rows) == 1 and bool(self._revision))
        self.compare_button.setEnabled(idle and pair)
        self.evaluate_button.setEnabled(idle and single)
        self.clone_button.setEnabled(idle and single and bool(self.clone_name.text().strip()))
        self.save_button.setEnabled(idle and len(rows) == 1 and bool(self._revision))
        show_annotation = idle and len(rows) == 1
        for field in (self.name_edit, self.notes_edit, self.favorite, self.save_button):
            field.setVisible(show_annotation)
        self.clone_name.setVisible(idle and single)
        self.name_edit.setEnabled(idle and len(rows) == 1)
        self.notes_edit.setEnabled(idle and len(rows) == 1)
        self.favorite.setEnabled(idle and len(rows) == 1)
        self.clone_name.setEnabled(idle and single)
        self.runs.setEnabled(idle)
        self.cancel_button.setEnabled(not idle and not self._cancel_requested)

    def _open(self):
        rows = self._selected()
        if self._task is None and len(rows) == 1 and self._revision:
            self.openRunRequested.emit(rows[0], self._revision)

    def _save_annotation(self):
        rows = self._selected()
        if self._task is None and len(rows) == 1 and self._revision:
            run_id, revision = rows[0].run_id, self._revision
            name, notes, favorite = self.name_edit.text(), self.notes_edit.toPlainText(), self.favorite.isChecked()
            if self._pending_navigation is None:
                identity = (rows[0].run_id, rows[0].sequence)
                self._pending_navigation = lambda: self._select_identities((identity,))
            self._launch("annotate", lambda _job: self.service.annotate(run_id, revision,
                name=name, notes=notes, favorite=favorite))

    def _compare(self):
        rows = self._selected()
        if self._task is None and len(rows) == 2 and all(row.kind == "execution" for row in rows):
            ids, revision = tuple(row.run_id for row in rows), self._revision
            self._launch("compare", lambda job: self.service.compare(ids, revision,
                should_cancel=lambda: job.cancel_requested))

    def _evaluate(self):
        rows = self._selected()
        if self._task is None and len(rows) == 1 and rows[0].kind == "execution":
            run_id, revision = rows[0].run_id, self._revision
            self._launch("evaluate", lambda job: self.service.review(run_id, revision,
                should_cancel=lambda: job.cancel_requested))

    def _clone(self):
        rows = self._selected()
        if self._task is None and len(rows) == 1 and rows[0].kind == "execution":
            run_id, revision, name = rows[0].run_id, self._revision, self.clone_name.text()
            if name.strip():
                self._launch("clone", lambda job: self.service.clone(run_id, revision, name,
                    should_cancel=lambda: job.cancel_requested))

    def _cancel(self):
        if self._task is not None:
            self._cancel_requested = True
            if self._task.job_id:
                self.jobs.cancel(self._task.job_id)
            self.status.setText("Cancelling after the current bounded check…")
            self.cancel_button.setEnabled(False)

    def reject(self):
        if self._task is not None:
            self._closing = True
            self._cancel()
            return
        if self._allow_close:
            super().reject()
            return
        def close():
            self._allow_close = True
            super(TrainingExperimentsDialog, self).reject()
        self._guard_unsaved(close)

    def request_shell_close(self):
        """Close for app Exit; notify the shell only after an async guarded Save."""
        if self._task is not None:
            self._closing = True
            self._cancel()
            return
        def close():
            self._allow_close = True
            super(TrainingExperimentsDialog, self).reject()
        self._guard_unsaved(close, on_saved=self.editorCloseReady.emit)

    def closeEvent(self, event):
        if self._task is not None:
            event.ignore()
            self.reject()
        else:
            if self._allow_close or not self._annotation_dirty():
                super().closeEvent(event)
            else:
                event.ignore()
                self._guard_unsaved(lambda: self._close_after_guard())

    def _close_after_guard(self):
        self._allow_close = True
        self.close()
