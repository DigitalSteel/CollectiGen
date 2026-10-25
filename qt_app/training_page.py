"""Persistent Training navigation over canonical collections, drafts and reviews."""
from __future__ import annotations

from pathlib import Path

from .help_content import workspace_help

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QAbstractItemView, QComboBox, QDialog, QFileDialog, QGroupBox, QHBoxLayout, QHeaderView,
    QLabel, QLineEdit, QPushButton, QStackedWidget, QTabWidget,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from .components import EmptyStatePanel, PageHeader
from .workers import CoordinatedCallTask


class TrainingRecoveryFinderDialog(QDialog):
    """Choose a validated receipt by package name before canonical recovery review."""

    folder_requested = Signal()
    exact_receipt_requested = Signal()
    receipt_requested = Signal(str)

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setWindowTitle("Find Interrupted Training Package")
        self.resize(720, 240)
        self.setModal(True)
        layout = QVBoxLayout(self)
        explanation = QLabel(
            "Choose the parent folder that contains, or would contain, your package folder. "
            "CollectiGen will list interrupted packages there for review; it will not recover one automatically."
        )
        explanation.setWordWrap(True)
        layout.addWidget(explanation)
        self.folder_button = QPushButton("Choose Package Location…")
        self.folder_button.clicked.connect(self.folder_requested)
        layout.addWidget(self.folder_button, 0, Qt.AlignLeft)
        self.status = QLabel("No location selected yet.")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        self.items = QTreeWidget()
        self.items.setAccessibleName("Interrupted training packages in selected location")
        self.items.setHeaderLabels(("Package", "Saved recovery state"))
        self.items.setRootIsDecorated(False)
        self.items.setSelectionMode(QAbstractItemView.SingleSelection)
        self.items.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.items.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.items.itemSelectionChanged.connect(self._selection_changed)
        self.items.hide()
        layout.addWidget(self.items, 1)
        self.exact_button = QPushButton("Show Exact Receipt Path")
        self.exact_button.setCheckable(True)
        self.exact_button.toggled.connect(self._selection_changed)
        self.exact_path = QLabel()
        self.exact_path.setTextFormat(Qt.PlainText)
        self.exact_path.setWordWrap(True)
        self.exact_path.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        self.exact_path.hide()
        layout.addWidget(self.exact_button, 0, Qt.AlignLeft)
        layout.addWidget(self.exact_path)
        actions = QHBoxLayout()
        self.browse_receipt_button = QPushButton("Browse Exact Receipt…")
        self.browse_receipt_button.setToolTip("Advanced: choose a specific saved .recovery.json file.")
        self.browse_receipt_button.clicked.connect(self.exact_receipt_requested)
        actions.addWidget(self.browse_receipt_button)
        actions.addStretch(1)
        self.close_button = QPushButton("Close")
        self.close_button.clicked.connect(self.reject)
        actions.addWidget(self.close_button)
        self.review_button = QPushButton("Review Selected Package")
        self.review_button.setProperty("primary", True)
        self.review_button.clicked.connect(self._review_selected)
        actions.addWidget(self.review_button)
        layout.addLayout(actions)
        self._selection_changed()

    def show_choices(self, folder, choices):
        self.folder_button.setEnabled(True)
        self.browse_receipt_button.setEnabled(True)
        self.items.clear()
        states = {
            "prepared": "Publication started",
            "staging": "Copy interrupted",
            "staged_complete": "Files staged; review needed",
            "promoted": "Published; receipt unfinished",
            "attention_required": "Needs attention",
        }
        for choice in choices:
            item = QTreeWidgetItem((choice.destination_name, states.get(choice.status, choice.status)))
            item.setData(0, Qt.UserRole, choice)
            self.items.addTopLevelItem(item)
        self.items.setVisible(bool(choices))
        self.resize(720, 440 if choices else 240)
        self.status.setText(
            f"{len(choices):,} interrupted {'package' if len(choices)==1 else 'packages'} "
            f"found in {Path(folder).name}. "
            "Choose one to review before any recovery."
            if choices else
            "No interrupted packages found in this folder. Choose another location or browse an exact receipt."
        )
        if choices:
            self.items.setCurrentItem(self.items.topLevelItem(0))
        self._selection_changed()

    def show_searching(self):
        self.items.clear()
        self.items.hide()
        self.resize(720, 240)
        self.folder_button.setEnabled(False)
        self.browse_receipt_button.setEnabled(False)
        self.status.setText("Checking this location for saved interrupted packages…")
        self._selection_changed()

    def show_search_error(self, message):
        self.folder_button.setEnabled(True)
        self.browse_receipt_button.setEnabled(True)
        self.status.setText(message)

    def _selection_changed(self):
        item = self.items.currentItem()
        choice = item.data(0, Qt.UserRole) if item is not None else None
        self.review_button.setVisible(choice is not None)
        self.review_button.setEnabled(choice is not None)
        self.exact_button.setVisible(choice is not None)
        self.exact_button.setEnabled(choice is not None)
        self.exact_path.setText(str(choice.receipt) if choice is not None else "")
        self.exact_path.setVisible(choice is not None and self.exact_button.isChecked())

    def _review_selected(self):
        item = self.items.currentItem()
        choice = item.data(0, Qt.UserRole) if item is not None else None
        if choice is not None:
            self.receipt_requested.emit(str(choice.receipt))
            self.accept()


class TrainingPage(QWidget):
    collection_requested = Signal(object)
    draft_requested = Signal(object)
    dataset_requested = Signal()
    images_requested = Signal()
    setup_requested = Signal()
    models_requested = Signal()
    package_requested = Signal(str)
    package_recovery_requested = Signal(str)
    retry_package_requested = Signal()
    cancel_training_requested = Signal()
    background_work_finished = Signal()

    def __init__(self, jobs, catalog, controller, parent=None):
        super().__init__(parent)
        self.jobs, self.catalog, self.controller = jobs, catalog, controller
        self._task = None
        self._cancelled = False
        self._shutdown = False
        self._loaded = False
        self._collections_return_pending = False
        self._return_selection_key = None
        self._return_offset = 0
        self._offset = 0
        self._next_offset = None
        self._review = None
        self._review_container = None
        self._planning = False
        self._dataset_training_active = False
        self._training_retry_action = None
        self._package_retry_intent = None
        self._package_recovery_manifest = None
        self._training_cancellable = True
        self._rows = ()
        self._recovery_finder = None
        self.setAccessibleName("Training")
        layout = QVBoxLayout(self)
        self.header = PageHeader("Training", "Prepare your data → Run training → Evaluate the result", help_text=workspace_help("training"))
        layout.addWidget(self.header)
        actions = QHBoxLayout()
        self.overview_button = QPushButton("Training Overview")
        self.overview_button.clicked.connect(self.show_overview)
        actions.addWidget(self.overview_button)
        self.review_button = QPushButton("Return to Training Review")
        self.review_button.clicked.connect(self.show_review)
        self.review_button.hide()
        actions.addWidget(self.review_button)
        actions.addStretch(1)
        for name, label, signal in (
            ("models_button", "Models…", self.models_requested),
            ("setup_button", "Trainer Setup…", self.setup_requested),
        ):
            button = QPushButton(label)
            button.clicked.connect(signal)
            setattr(self, name, button)
            actions.addWidget(button)
        layout.addLayout(actions)
        self.stack = QStackedWidget()
        layout.addWidget(self.stack, 1)
        overview = QWidget()
        self.stages = QTabWidget()
        self.stages.setAccessibleName('Training workflow stages')
        self.stages.addTab(overview, '1. Prepare')
        body = QVBoxLayout(overview)
        body.setContentsMargins(0, 0, 0, 0)
        intro = QLabel("Choose a collection or resume a saved draft. Review the goal, model, captions and settings before creating a package or starting training.")
        intro.setWordWrap(True)
        body.addWidget(intro)
        filters = QHBoxLayout()
        self.kind = QComboBox()
        self.kind.setAccessibleName("Training work to browse")
        self.kind.addItem("Dataset Collections", "collections")
        self.kind.addItem("Saved Training Drafts", "drafts")
        self.kind.currentIndexChanged.connect(self._kind_changed)
        filters.addWidget(self.kind)
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search names or goals, then press Enter")
        self.search.setAccessibleName("Search training work")
        self.search.returnPressed.connect(self.refresh)
        filters.addWidget(self.search, 1)
        self.refresh_button = QPushButton("Refresh")
        self.refresh_button.clicked.connect(self._refresh_selected)
        filters.addWidget(self.refresh_button)
        self.all_drafts_button = QPushButton("Show All Saved Drafts")
        self.all_drafts_button.clicked.connect(self._show_all_drafts)
        self.all_drafts_button.hide()
        filters.addWidget(self.all_drafts_button)
        body.addLayout(filters)
        self.items = QTreeWidget()
        self.items.setHeaderLabels(("Name", "Scope and saved context"))
        self.items.setRootIsDecorated(False)
        self.items.setUniformRowHeights(True)
        self.items.setSelectionMode(QAbstractItemView.SingleSelection)
        self.items.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.items.header().setSectionResizeMode(1, QHeaderView.Stretch)
        self.items.itemSelectionChanged.connect(self._selection_changed)
        self.items.itemDoubleClicked.connect(lambda *_: self.open_selected())
        body.addWidget(self.items, 1)
        self.empty = EmptyStatePanel("Create a collection in Dataset, add images, then return here to prepare training.", "No collections yet")
        self.empty_dataset_button = self.empty.add_action(
            "Manage Collections in Dataset…", lambda: self.dataset_requested.emit(),
        )
        self.empty.hide()
        body.addWidget(self.empty, 1)
        self.pagination = QWidget()
        pages = QHBoxLayout(self.pagination)
        pages.setContentsMargins(0, 0, 0, 0)
        self.previous_button = QPushButton("Previous")
        self.previous_button.clicked.connect(lambda: self._load(max(0, self._offset - 100)))
        pages.addWidget(self.previous_button)
        self.next_button = QPushButton("Next")
        self.next_button.clicked.connect(lambda: self._load(self._next_offset or 0))
        pages.addWidget(self.next_button)
        self.page_count = QLabel()
        pages.addWidget(self.page_count)
        pages.addStretch(1)
        self.pagination.hide()
        body.addWidget(self.pagination)
        self.scope = QLabel("Choose an item to review its exact scope.")
        self.scope.setTextFormat(Qt.PlainText)
        self.scope.setWordWrap(True)
        self.scope.hide()
        body.addWidget(self.scope)
        launch = QHBoxLayout()
        self.dataset_button = QPushButton("Manage Collections in Dataset…")
        self.dataset_button.clicked.connect(self.dataset_requested)
        launch.addWidget(self.dataset_button)
        self.package_button = QPushButton("Open Training Package…")
        self.package_button.clicked.connect(self.open_package)
        launch.addWidget(self.package_button)
        self.package_recovery_button = QPushButton("Recover Interrupted Package…")
        self.package_recovery_button.clicked.connect(self.open_package_recovery)
        launch.addWidget(self.package_recovery_button)
        launch.addStretch(1)
        self.open_button = QPushButton("Prepare Training…")
        self.open_button.setProperty("primary", True)
        self.open_button.clicked.connect(self.open_selected)
        self.open_button.hide()
        launch.addWidget(self.open_button)
        body.addLayout(launch)
        run_page=QWidget(); run_body=QVBoxLayout(run_page)
        self.stages.addTab(run_page,'2. Run')
        find_run=QGroupBox('Find or resume a run')
        find_run_body=QVBoxLayout(find_run)
        run_intro=QLabel('Resume an open review, or find a previous SDXL / Anima run in Run History. Readiness checks confirm whether an exact setup can start; training runs create model results.')
        run_intro.setWordWrap(True); find_run_body.addWidget(run_intro)
        runs = QHBoxLayout()
        self.open_reviews = QComboBox()
        self.open_reviews.setAccessibleName("Open training run and setup reviews")
        self.open_reviews.setPlaceholderText("No open run or setup reviews")
        runs.addWidget(self.open_reviews, 1)
        self.open_review_button = QPushButton("Open Review")
        self.open_review_button.clicked.connect(self._open_controller_review)
        runs.addWidget(self.open_review_button)
        self.history_button = QPushButton("Run History and Results…")
        self.history_button.clicked.connect(self.controller.history)
        runs.addWidget(self.history_button)
        find_run_body.addLayout(runs)
        run_body.addWidget(find_run)
        compare_runs=QGroupBox('Compare saved runs')
        experiments = QVBoxLayout(compare_runs)
        experiment_action=QHBoxLayout()
        self.experiments_button = QPushButton("Experiments and Compare Runs…")
        self.experiments_button.clicked.connect(lambda: self.controller.experiments())
        experiment_action.addWidget(self.experiments_button)
        experiment_action.addStretch(1)
        experiments.addLayout(experiment_action)
        compare_help=QLabel('Name and favorite runs, compare their settings, or clone a reviewed SDXL run into a new draft. Measured trials are short training checks; their speed and loss do not establish image quality.')
        compare_help.setWordWrap(True); experiments.addWidget(compare_help)
        run_body.addWidget(compare_runs)
        import_review=QGroupBox('Import an older review')
        import_body=QVBoxLayout(import_review)
        import_help=QLabel('Have an older run that is not in history? Import its retained review once; subsequent reopening uses Run History.')
        import_help.setWordWrap(True);import_body.addWidget(import_help)
        retained = QHBoxLayout()
        self.anima_history_button = QPushButton('Import Anima Review…')
        self.anima_history_button.clicked.connect(lambda: self.controller.open_anima_run())
        retained.addWidget(self.anima_history_button)
        self.validation_history_button = QPushButton('Import Validation Review…')
        self.validation_history_button.clicked.connect(lambda: self.controller.open_validation())
        retained.addWidget(self.validation_history_button)
        retained.addStretch(1)
        import_body.addLayout(retained)
        run_body.addWidget(import_review)
        run_body.addStretch(1)
        evaluate_page=QWidget(); evaluate_body=QVBoxLayout(evaluate_page)
        self.stages.addTab(evaluate_page,'3. Evaluate')
        visual_comparison=QGroupBox('Visual comparison')
        visual_body=QVBoxLayout(visual_comparison)
        evaluate_intro=QLabel('Compare generated images with fixed prompts, seeds and model settings. Start from a registered result in Run History, or set up a comparison template in Generate.')
        evaluate_intro.setWordWrap(True);visual_body.addWidget(evaluate_intro)
        self.evaluation_button = QPushButton('Compare Generated Samples…')
        self.evaluation_button.setProperty('primary',True)
        self.evaluation_button.clicked.connect(lambda: self.controller.evaluation())
        evaluate_action=QHBoxLayout()
        evaluate_action.addStretch(1)
        evaluate_action.addWidget(self.evaluation_button)
        visual_body.addLayout(evaluate_action)
        evaluation_help=QLabel('Use the base model as a baseline, review missing samples in the Generation queue, then compare verified images side by side. Favorites and observations record your judgment.')
        evaluation_help.setWordWrap(True);visual_body.addWidget(evaluation_help)
        evaluate_body.addWidget(visual_comparison)
        validation=QGroupBox('Held-out validation')
        validation_body=QVBoxLayout(validation)
        validation_help=QLabel('This separate numerical check uses reserved data; it does not rate visual quality. Open a completed SDXL result in Run History on the Run tab to start it or reopen a saved review.')
        validation_help.setWordWrap(True);validation_body.addWidget(validation_help)
        evaluate_body.addWidget(validation)
        evaluate_body.addStretch(1)
        self.capability = QLabel("Local training is checked separately for the exact SDXL and Anima Base LoRA recipes. Model, software and qualification blockers appear before launch. A completed run does not establish image quality.")
        self.capability.setWordWrap(True)
        body.addWidget(self.capability)
        self.stack.addWidget(self.stages)
        footer = QHBoxLayout()
        self.status = QLabel("Open Training to load saved collections and drafts.")
        self.status.setTextFormat(Qt.PlainText)
        self.status.setWordWrap(True)
        footer.addWidget(self.status, 1)
        self.stages.currentChanged.connect(self._sync_stage_status)
        self.retry_package_button = QPushButton("Retry Package Preview…")
        self.retry_package_button.setToolTip(
            "Recheck the same saved draft, source images and destination before another package review. Nothing is built automatically."
        )
        self.retry_package_button.clicked.connect(self.retry_package_requested)
        self.retry_package_button.hide()
        footer.addWidget(self.retry_package_button)
        self.cancel_button = QPushButton("Cancel Check")
        self.cancel_button.clicked.connect(self.cancel_check)
        self.cancel_button.hide()
        footer.addWidget(self.cancel_button)
        self.cancel_training_button = QPushButton("Cancel Preparation Safely")
        self.cancel_training_button.clicked.connect(self.cancel_training_requested)
        self.cancel_training_button.hide()
        footer.addWidget(self.cancel_training_button)
        layout.addLayout(footer)
        controller.workspaceChanged.connect(self.refresh_reviews)
        self._review_timer = QTimer(self)
        self._review_timer.setInterval(500)
        self._review_timer.timeout.connect(self.refresh_reviews)
        self._shutdown_timer = QTimer(self)
        self._shutdown_timer.setInterval(100)
        self._shutdown_timer.timeout.connect(self._check_shutdown)
        self._update_actions()

    def showEvent(self, event):
        super().showEvent(event)
        self._review_timer.start()
        self.refresh_reviews()
        if self._collections_return_pending:
            QTimer.singleShot(0, self._refresh_after_collections)
        elif not self._loaded and self._task is None and not self._shutdown:
            QTimer.singleShot(0, self.refresh)

    def hideEvent(self, event):
        self._review_timer.stop()
        super().hideEvent(event)

    def show_overview(self):
        self.stack.setCurrentIndex(0)

    def _sync_stage_status(self, _index=None):
        # Catalog guidance belongs to Prepare; keep active work visible across tabs.
        preparing = self.stack.currentWidget() is not self.stages or self.stages.currentIndex() == 0
        busy = self._task is not None or self._planning or self._dataset_training_active
        self.status.setVisible(bool(self.status.text()) and (preparing or busy))

    def show_review(self):
        if self._review is not None:
            self.stack.setCurrentWidget(self._review_container)
            self._review.show()

    def retain_review(self, dialog, finished):
        """Embed the canonical candidate review; page navigation never closes it."""
        if self._review is not None:
            return False
        self._review = dialog
        dialog._training_workspace_owner = self
        dialog.setParent(self.stack)
        dialog.setWindowFlags(Qt.Widget)
        dialog.setMinimumSize(0, 0)
        # The canonical review owns task-local scrolling. Keep its goal,
        # navigation and completion actions in the bounded workspace viewport.
        container = QWidget()
        layout = QVBoxLayout(container)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.addWidget(dialog)
        self._review_container = container
        self.stack.addWidget(container)
        self.review_button.show()

        def complete(result):
            if self._review is not dialog:
                return
            self._review = None
            dialog._training_workspace_owner = None
            self.stack.removeWidget(container)
            self._review_container = None
            self.review_button.hide()
            self.show_overview()
            try:
                finished(result)
            finally:
                container.deleteLater()
                self._loaded = False
                self._update_actions()
                if self.isVisible() and not self._planning and not self._shutdown:
                    QTimer.singleShot(0, self._refresh_after_collections if self._collections_return_pending else self.refresh)

        dialog.finished.connect(complete)
        self.show_review()
        self._update_actions()
        return True

    def set_planning(self, busy, message=""):
        self._planning = bool(busy)
        if message:
            self.status.setText(message)
        self._update_actions()
        self._sync_stage_status()
        if not busy and self._collections_return_pending:
            QTimer.singleShot(0, self._refresh_after_collections)

    def refresh_collections_on_return(self):
        """Re-read the canonical Dataset store after its collection editor was opened."""
        item = self.items.currentItem()
        row = item.data(0, Qt.UserRole) if item else None
        self._return_selection_key = row.key if row is not None else None
        self._return_offset = self._offset
        self._collections_return_pending = True

    def _refresh_after_collections(self):
        if (not self._collections_return_pending or not self.isVisible() or self._task is not None
                or self._shutdown or self._planning or self._review is not None):
            return
        self._collections_return_pending = False
        key, offset = self._return_selection_key, self._return_offset
        self._return_selection_key = None
        self._load(offset, preserve_key=key)

    def refresh(self):
        self._load(0)

    def _kind_changed(self):
        if self.kind.currentData() != "drafts":
            self._package_recovery_manifest = None
            self.all_drafts_button.hide()
        self.refresh()

    def show_package_recovery_drafts(self, candidate_manifest):
        """Show likely source drafts without granting recovery authority."""
        self._package_recovery_manifest = candidate_manifest
        self.all_drafts_button.setVisible(candidate_manifest is not None)
        self.search.clear()
        index = self.kind.findData("drafts")
        if self.kind.currentIndex() != index:
            self.kind.setCurrentIndex(index)
        else:
            self.refresh()

    def _show_all_drafts(self):
        self._package_recovery_manifest = None
        self.all_drafts_button.hide()
        self.refresh()

    def _refresh_selected(self):
        """Recheck the visible page without losing the exact selected work item."""
        item = self.items.currentItem()
        row = item.data(0, Qt.UserRole) if item else None
        self._load(self._offset, preserve_key=row.key if row is not None else None)

    def _load(self, offset, *, preserve_key=None):
        if self._task is not None or self._shutdown or self._planning:
            return
        kind, query = self.kind.currentData(), self.search.text().strip()
        matching = self._package_recovery_manifest if kind == "drafts" else None
        self.items.clear()
        self._rows = ()
        self.empty.hide()
        self.scope.setText("Reading saved training work…")
        self._start("Reading saved training work…", lambda: self.catalog.page(kind=kind, offset=offset, limit=100, query=query, related_package_manifest=matching),
                    lambda page: self._loaded_page(page, preserve_key=preserve_key))

    def _start(
        self, label, operation, complete, *,
        cancel_message="Training list check cancelled. Choose Refresh to try again.",
    ):
        if self._task is not None or self._shutdown:
            return False
        self._cancelled = False
        task = CoordinatedCallTask(self.jobs, label, lambda job: operation(), resource="io")
        self._task = task
        self.status.setText(label)
        self._sync_stage_status()
        task.signals.complete.connect(lambda value: self._finish(task, complete, value,
                                                                 cancel_message=cancel_message))
        task.signals.failed.connect(lambda error: self._finish(task, complete, error=error,
                                                               cancel_message=cancel_message))
        task.signals.cancelled.connect(lambda: self._finish(task, complete, error=cancel_message,
                                                            cancel_message=cancel_message))
        self._update_actions()
        try:
            task.start()
        except Exception as exc:
            self._finish(task, complete, error=str(exc), cancel_message=cancel_message)
            return False
        return True

    def _finish(
        self, task, complete, value=None, error="",
        cancel_message="Training list check cancelled. Choose Refresh to try again.",
    ):
        if self._task is not task:
            return
        self._task = None
        if self._cancelled or self._shutdown:
            self.status.setText(cancel_message)
        elif error:
            if str(error) == "This collection changed or disappeared. Reload the list.":
                self.status.setText(
                    "This collection changed or disappeared. Choose Refresh, review its current image count, "
                    "then Prepare Training again. No plan was opened."
                )
            else:
                self.status.setText(str(error)[:2000])
        else:
            complete(value)
        self._update_actions()
        self._sync_stage_status()
        self.background_work_finished.emit()
        if self._collections_return_pending:
            QTimer.singleShot(0, self._refresh_after_collections)

    def _loaded_page(self, page, *, preserve_key=None):
        if page.offset and not page.rows and not page.issues:
            self._load(max(0, ((page.total - 1) // 100) * 100), preserve_key=preserve_key)
            return
        self._loaded = True
        self._offset, self._next_offset, self._rows = page.offset, page.next_offset, page.rows
        for row in page.rows:
            item = QTreeWidgetItem((row.name, row.detail))
            item.setData(0, Qt.UserRole, row)
            self.items.addTopLevelItem(item)
        self.page_count.setText(f"{len(page.rows):,} of {page.total:,}")
        self.pagination.setVisible(bool(page.offset or page.next_offset is not None))
        self.empty.setVisible(not page.rows and not page.issues)
        self.items.setVisible(bool(page.rows) or bool(page.issues))
        drafts = self.kind.currentData() == "drafts"
        empty_collections = not page.rows and not page.issues and not drafts and not self.search.text().strip()
        self.empty_dataset_button.setVisible(empty_collections)
        self.dataset_button.setVisible(not empty_collections)
        self.empty.title_label.setText("No related saved draft found" if self._package_recovery_manifest is not None else "No matching training work" if self.search.text().strip() else "No saved drafts" if drafts else "No collections yet")
        self.empty.setText("This staged package stays held. Show All Saved Drafts to review other work; recovery still requires rebuilding its exact plan." if self._package_recovery_manifest is not None else "Clear or change the search to see other saved work." if self.search.text().strip() else "Prepare training from a Dataset collection and use Save Draft to keep the reviewed plan." if drafts else "Create a collection in Dataset, add images, then return here to prepare training.")
        if page.issues:
            self.status.setText("\n".join(page.issues))
        elif page.rows:
            self.status.setText("Possible source drafts; recheck required. Choose one to rebuild. Recovery is offered only if its current plan matches the staged package exactly." if self._package_recovery_manifest is not None else "Choose a saved item. Opening it rechecks its identity before continuing.")
        else:
            self.status.clear()
        self._sync_stage_status()
        if preserve_key is not None:
            for index in range(self.items.topLevelItemCount()):
                item = self.items.topLevelItem(index)
                if item.data(0, Qt.UserRole).key == preserve_key:
                    self.items.setCurrentItem(item)
                    break
            else:
                if page.issues:
                    self._selection_changed()
                    return
                self.status.setText("The previously selected item is no longer on this page. Choose a current saved item.")
        self._selection_changed()

    def _selection_changed(self):
        item = self.items.currentItem()
        row = item.data(0, Qt.UserRole) if item else None
        if (self._package_retry_intent is not None and
                (self.kind.currentData() != "drafts" or row is None or
                 (row.key, row.fingerprint) != self._package_retry_intent[:2])):
            self._package_retry_intent = None
        if row is not None and self.kind.currentData() == "collections" and row.member_count == 0:
            self.scope.setText(
                f"{row.name}\nNo images are in this collection yet. In Dataset, select images "
                "and choose Add to Collection, then return here to prepare training."
            )
        else:
            self.scope.setText(f"{row.name}\n{row.detail}" if row else "Choose an item to review its exact scope.")
        self.scope.setVisible(bool(self._rows) or self._review is not None)
        self._update_actions()

    def remember_package_preview(self, candidate_plan, destination):
        """Keep only the selected exact saved draft's read-only retry intent."""
        item = self.items.currentItem()
        row = item.data(0, Qt.UserRole) if item else None
        if (self._shutdown or self.kind.currentData() != "drafts" or row is None
                or self._review is not None):
            self.clear_package_retry()
            return
        self._package_retry_intent = (row.key, row.fingerprint, candidate_plan, destination)
        self._update_actions()

    def clear_package_retry(self):
        self._package_retry_intent = None
        self._update_actions()

    def open_selected(self):
        if self._review is not None:
            self.show_review()
            return
        if self._task is not None or self._shutdown or self._planning:
            return
        item = self.items.currentItem()
        row = item.data(0, Qt.UserRole) if item else None
        if row is None:
            return
        drafts = self.kind.currentData() == "drafts"
        if not drafts and row.member_count == 0:
            self.images_requested.emit()
            return
        resolve = self.catalog.resolve_draft if drafts else self.catalog.resolve_collection
        signal = self.draft_requested if drafts else self.collection_requested
        retry = "Resume Draft" if drafts else "Prepare Training"
        self._start(
            "Rechecking the selected saved item…",
            lambda: resolve(row.key, row.fingerprint), signal.emit,
            cancel_message=f"Selected-item check cancelled. Choose {retry} again to retry; no plan was opened.",
        )

    def open_package(self):
        if self._task is not None or self._shutdown or self._planning or self._review is not None:
            return
        destination = QFileDialog.getExistingDirectory(self, "Open an Existing Training Package")
        if destination:
            self.package_requested.emit(destination)

    def open_package_recovery(self):
        if self._task is not None or self._shutdown or self._planning or self._review is not None:
            return
        if self._recovery_finder is not None and self._recovery_finder.isVisible():
            self._recovery_finder.raise_()
            return
        finder = TrainingRecoveryFinderDialog(self)
        self._recovery_finder = finder
        finder.folder_requested.connect(lambda: self._choose_recovery_location(finder))
        finder.exact_receipt_requested.connect(lambda: self._browse_exact_recovery_receipt(finder))
        finder.receipt_requested.connect(self.package_recovery_requested)
        finder.finished.connect(lambda _result: self._close_recovery_finder(finder))
        finder.open()

    def _choose_recovery_location(self, finder):
        if finder is not self._recovery_finder or self._task is not None:
            return
        folder = QFileDialog.getExistingDirectory(
            finder, "Where Would the Training Package Be Saved?",
        )
        if not folder:
            return
        from core.training_package import TrainingPackageService
        finder.show_searching()
        started = self._start(
            "Finding interrupted training packages…",
            lambda: TrainingPackageService.recovery_choices(
                folder, should_cancel=lambda: self._cancelled,
            ),
            lambda choices: finder.show_choices(folder, choices)
            if finder is self._recovery_finder and finder.isVisible() else None,
            cancel_message="Package search cancelled. Choose a location to try again; nothing was recovered.",
        )
        if not started:
            if finder is self._recovery_finder and finder.isVisible():
                finder.show_search_error(
                    f"Could not start the package search: {self.status.text()}. "
                    "Choose a location to retry after active work finishes, or browse an exact receipt."
                )
            return
        task = self._task
        if task is not None:
            task.signals.failed.connect(lambda error: finder.show_search_error(
                f"Could not check that location: {error}. Choose another location or browse an exact receipt."
            ) if finder is self._recovery_finder else None)
            task.signals.cancelled.connect(lambda: finder.show_search_error(
                "Package search cancelled. Choose a location to try again; nothing was recovered."
            ) if finder is self._recovery_finder else None)

    def _browse_exact_recovery_receipt(self, finder):
        if finder is not self._recovery_finder or self._task is not None:
            return
        receipt, _ = QFileDialog.getOpenFileName(
            finder, "Review an Exact Interrupted Training Package", "",
            "Training recovery receipt (*.recovery.json)",
        )
        if receipt:
            self.package_recovery_requested.emit(receipt)
            finder.accept()

    def _close_recovery_finder(self, finder):
        if finder is not self._recovery_finder:
            return
        if self._task is not None:
            self.cancel_check()
            self.status.setText("Cancelling package search; no recovery started.")
        self._recovery_finder = None
        finder.deleteLater()

    def refresh_reviews(self):
        selected = self.open_reviews.currentData()
        reviews = self.controller.open_reviews()
        self.open_reviews.blockSignals(True)
        self.open_reviews.clear()
        for dialog in reviews:
            status = getattr(dialog, "status", None)
            detail = status.text() if isinstance(status, QLabel) else ""
            self.open_reviews.addItem(dialog.windowTitle() + (" — " + detail[:160] if detail else ""), dialog)
        index = self.open_reviews.findData(selected) if selected is not None else -1
        if index >= 0:
            self.open_reviews.setCurrentIndex(index)
        elif reviews:
            self.open_reviews.setCurrentIndex(0)
        self.open_reviews.blockSignals(False)
        self.open_review_button.setEnabled(bool(reviews))

    def _open_controller_review(self):
        self.controller.show_review(self.open_reviews.currentData())

    def _update_actions(self):
        busy = self._task is not None or self._shutdown or self._planning
        self.empty_dataset_button.setEnabled(not busy)
        for widget in (self.kind, self.search, self.refresh_button, self.items, self.all_drafts_button):
            widget.setEnabled(not busy)
        self.previous_button.setEnabled(not busy and self._offset > 0)
        self.next_button.setEnabled(not busy and self._next_offset is not None)
        item = self.items.currentItem()
        row = item.data(0, Qt.UserRole) if item else None
        empty_collection = self.kind.currentData() == "collections" and row is not None and row.member_count == 0
        self.open_button.setText("Return to Training Review" if self._review is not None else "Resume Draft…" if self.kind.currentData() == "drafts" else "Add Images in Dataset…" if empty_collection else "Prepare Training…")
        self.open_button.setVisible(self._review is not None or bool(self._rows))
        self.open_button.setEnabled(not busy and not self._planning and (self._review is not None or self.items.currentItem() is not None))
        self.package_button.setEnabled(not busy and not self._planning and self._review is None)
        self.package_recovery_button.setEnabled(self.package_button.isEnabled())
        retry_available = self._package_retry_intent is not None and self._review is None
        for button, primary in ((self.retry_package_button, retry_available),
                                (self.open_button, not retry_available)):
            if bool(button.property("primary")) != primary:
                button.setProperty("primary", primary)
                button.style().unpolish(button)
                button.style().polish(button)
        self.retry_package_button.setVisible(retry_available and not busy)
        self.retry_package_button.setEnabled(not busy)
        self.cancel_button.setVisible(self._task is not None)
        self.cancel_button.setEnabled(not self._cancelled)
        self.cancel_training_button.setVisible(self._planning and self._review is None)
        self.cancel_training_button.setEnabled(self._training_cancellable)
        self.cancel_training_button.setToolTip("" if self._training_cancellable else
                                             "This recovery step must finish safely before it can close.")

    def cancel_check(self):
        if self._task is not None:
            self._cancelled = True
            self.jobs.cancel(self._task.job_id)
            self.status.setText("Cancelling the saved-work check…")
            self._update_actions()

    def has_active_background_work(self):
        return self._task is not None or bool(self._review is not None and (getattr(self._review, "_review_busy", False) or getattr(self._review, "_draft_save_task", None) is not None))

    def request_background_shutdown(self):
        self._shutdown = True
        self.clear_package_retry()
        self._review_timer.stop()
        self.cancel_check()
        if self._review is not None:
            if getattr(self._review,"_review_busy",False):
                self._review.cancel_review_requested.emit()
            self._review.reject()
        if self.has_active_background_work():
            self._shutdown_timer.start()
        return self.has_active_background_work()

    def _check_shutdown(self):
        if self.has_active_background_work():
            return
        self._shutdown_timer.stop()
        self.background_work_finished.emit()

    def resume_after_cancelled_shutdown(self):
        self._shutdown = False
        self._shutdown_timer.stop()
        if self.isVisible():
            self._review_timer.start()
        self._update_actions()
