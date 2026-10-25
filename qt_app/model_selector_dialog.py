"""Reusable role-aware selector for the shared model resource registry."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import dataclass
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QCompleter,
    QDialog,
    QDialogButtonBox,
    QFormLayout,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMessageBox,
    QPushButton,
    QPlainTextEdit,
    QScrollArea,
    QSizePolicy,
    QSplitter,
    QTabBar,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
    QWidget,
)

from core.model_resource_registry import RESOURCE_ROLES, ResourceRecoveryRequired
from core.model_selection import ModelLocationUnavailable, ModelSelectionService
from .components import bind_status_footer
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask


@dataclass(frozen=True)
class _UnavailableChoice:
    resource_id: str
    role: str
    family: str
    reason: str


class ModelSelectorDialog(FeedbackEnabledDialog):
    """Select one exact resource role without claiming adapter readiness."""

    feedback_workflow = "Model resource selection"
    PAGE_SIZE = 100

    def __init__(
        self,
        registry,
        jobs,
        family="",
        parent=None,
        *,
        role="checkpoint",
        protected_roots=(),
        selected_resources=(),
        required_roles=(),
        allow_manage=True,
        settings=None,
        current_choice_label="",
        main_model_resource_id="",
        qualified_compatibility=(),
        collect_for_import=False,
        task_focused=False,
        allowed_declarations=(),
        choice_scope_label="",
    ):
        super().__init__(parent)
        self.registry = registry
        self.jobs = jobs
        self.collect_for_import = bool(collect_for_import)
        self.task_focused = bool(task_focused) and not self.collect_for_import
        self.allowed_declarations = frozenset(tuple(item) for item in allowed_declarations)
        self.choice_scope_label = str(choice_scope_label or "").strip()
        self._import_locations = {}
        from core.managed_model_identity import InstalledComponentModelSource
        self.settings = settings
        self.component_source = InstalledComponentModelSource(settings, protected_roots=protected_roots) if settings is not None and not self.collect_for_import else None
        self.service = ModelSelectionService(registry, protected_roots=protected_roots, component_source=self.component_source)
        self._pack_preview = None
        self.selected_component_model_id = ""
        self.selected_resources = tuple(selected_resources)
        self.main_model_resource_id = str(main_model_resource_id or "")
        self.qualified_compatibility = tuple(qualified_compatibility)
        self.required_roles = tuple(required_roles)
        self.allow_manage = bool(allow_manage) and not self.collect_for_import
        self.selected_resource_id = ""
        self.selected_role = ""
        self.selected_family = ""
        self.selected_path = ""
        self._role_constraint = role
        self._rows = ()
        self._page = 0
        self._revision = ""
        self._task = None
        self._closing = False
        self._reload_pending = False
        self._reload_selected_resource_id = ""
        self._reload_selected_declaration = None
        self._reload_apply_path = ""
        self._copy_restore_preferences = None
        self._copy_preference_stale = False
        self._preference_drafts = {}
        self._active_preference_id = ""
        self._active_preference_baseline = None
        self._unavailable_choice = None
        self._restore_unavailable_after_reload = None
        self.setWindowTitle("Choose Model Resource")
        self.resize(1180, 800)
        layout = QVBoxLayout(self)
        heading = QLabel("Choose a model")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        self.current_choice = QLabel(
            f"Current choice: {current_choice_label}" if current_choice_label
            else "Choose a file for the import plan; nothing is applied here." if self.collect_for_import
            else "Your workflow keeps its current choice until you apply a model here."
        )
        self.current_choice.setTextFormat(Qt.PlainText)
        self.current_choice.setWordWrap(True)
        layout.addWidget(self.current_choice)
        self.retained_choices = QLabel(
            f"Your {len(self.selected_resources)} other selected model "
            f"{'choice stays' if len(self.selected_resources) == 1 else 'choices stay'} "
            "in the request while you choose this one."
            if self.task_focused and self.selected_resources else ""
        )
        self.retained_choices.setTextFormat(Qt.PlainText)
        self.retained_choices.setWordWrap(True)
        self.retained_choices.setVisible(bool(self.retained_choices.text()))
        layout.addWidget(self.retained_choices)
        intro = QLabel(
            "Browse your registered models and select one to see its details. Use Model checks its files "
            "and applies your choice. Run compatibility is checked separately."
        )
        intro.setWordWrap(True)
        if self.task_focused:
            intro.setText("Select a registered model to review your choice. Use Model checks its files and applies it. "
                "Run compatibility is checked separately; browsing and exact details are available below.")
        if self.allowed_declarations:
            intro.setText(self.choice_scope_label + " Select the matching registered file; Use Model "
                "checks its bytes. Other registered models can be inspected in Models but cannot run this recipe.")
        elif self.collect_for_import:
            intro.setText("Choose an existing registered model and its exact permanent file for the import plan. "
                "Verification waits for the combined approval. Add new files through the import's Choose Files action.")
        layout.addWidget(intro)
        self.more_options_button = QPushButton("More Model Options and Details")
        self.more_options_button.setCheckable(True)
        self.more_options_button.setToolTip(
            "Show folder browsing, view settings, exact model details and model management without changing your pending choice."
        )
        self.more_options_button.setVisible(self.task_focused)
        self.more_options_button.toggled.connect(self._task_options_changed)
        layout.addWidget(self.more_options_button, 0, Qt.AlignLeft)

        self.role_tabs = QTabBar()
        self.role_tabs.setExpanding(False)
        self.role_tabs.setAccessibleName("Model resource categories")
        role_labels = {"": "All resources", "checkpoint": "Models", "lora": "LoRAs", "vae": "VAEs",
                       "text_encoder": "Text encoders", "control_model": "ControlNets", "upscaler": "Upscalers"}
        tab_roles = (role,) if role else ("", "checkpoint", "lora", "vae", *(value for value in RESOURCE_ROLES if value not in ("checkpoint", "lora", "vae")))
        for value in tab_roles:
            index = self.role_tabs.addTab(role_labels.get(value, value.replace("_", " ").title()))
            self.role_tabs.setTabData(index, value)
        if self.allowed_declarations:
            self.role_tabs.hide()  # The pinned recipe already defines the relevant roles.
        layout.addWidget(self.role_tabs)

        filters = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search models, notes, sources or exact SHA-256")
        self.search.setAccessibleName("Search registered model resources")
        self.role_filter = QComboBox()
        if role:
            if role not in RESOURCE_ROLES:
                raise ValueError("Unsupported model resource role")
            self.role_filter.addItem(role.replace("_", " ").title(), role)
            self.role_filter.setEnabled(False)
        else:
            self.role_filter.addItem("All roles", "")
            for value in RESOURCE_ROLES:
                self.role_filter.addItem(value.replace("_", " ").title(), value)
        self.family_filter = QLineEdit(str(family or ""))
        self.family_filter.setPlaceholderText("All families")
        from core.model_organization import family_options
        family_completer=QCompleter(list(dict.fromkeys((*family_options("Images"),*family_options("Video")))),self)
        family_completer.setCaseSensitivity(Qt.CaseInsensitive)
        family_completer.setCompletionMode(QCompleter.UnfilteredPopupCompletion)
        self.family_filter.setCompleter(family_completer)
        self.family_filter.setAccessibleName("Filter by model base family")
        self.family_filter.setToolTip("Filter by base family, for example SDXL, Flux.1, Flux.2 or Anima. A family label is not proof of compatibility.")
        self.family_filter.setVisible(not self.allowed_declarations)
        self.favorite_only = QCheckBox("Favorites")
        self.recent_only = QCheckBox("Recent selections")
        for widget in (self.search, self.role_filter, self.family_filter, self.favorite_only, self.recent_only):
            filters.addWidget(widget)
        layout.addLayout(filters)
        self.compatibility_filter = QComboBox()
        self.compatibility_filter.addItem("Compatible with selected model", "compatible")
        self.compatibility_filter.addItem("Compatibility unknown", "unknown")
        self.compatibility_filter.addItem("Show all", "all")
        self.compatibility_filter.setAccessibleName("Companion compatibility view")
        self.compatibility_filter.setToolTip("Only exact role-specific qualified evidence marks a companion compatible. Family and provider claims alone leave compatibility unknown.")
        self.compatibility_filter.setVisible(bool(self.main_model_resource_id and role in {"lora", "vae"}))
        layout.addWidget(self.compatibility_filter)
        self.role_filter.hide()  # Category tabs share this existing filter state.
        self.role_tabs.currentChanged.connect(self._role_tab_changed)

        self.managed_models = QComboBox()
        self.preview_pack_button = QPushButton("Verify Installed Pack…")
        self.register_pack_button = QPushButton("Register Verified Pack")
        managed = QHBoxLayout()
        managed.addWidget(self.managed_models, 1)
        managed.addWidget(self.preview_pack_button)
        managed.addWidget(self.register_pack_button)
        layout.addLayout(managed)
        choices = self.component_source.choices(role) if self.component_source else ()
        self._managed_role = role
        for model_id, label, typed_role, _family in choices:
            self.managed_models.addItem(f"{label} ({typed_role})", model_id)
        for widget in (self.managed_models, self.preview_pack_button, self.register_pack_button):
            widget.setVisible(bool(choices) and role in ("tagger", "captioner"))
        self.preview_pack_button.clicked.connect(self._preview_managed_pack)
        self.register_pack_button.clicked.connect(self._register_managed_pack)
        self.managed_models.currentIndexChanged.connect(self._clear_pack_preview)

        self.count = QLabel("Reading registered resources…")
        layout.addWidget(self.count)
        self.empty_state = QWidget()
        empty_layout = QVBoxLayout(self.empty_state)
        empty_layout.setContentsMargins(0, 0, 0, 0)
        self.empty_message = QLabel(
            "No registered models match this view. Models already on disk need a folder preview "
            "and registration before they appear here. You can also clear the filters."
        )
        self.empty_message.setWordWrap(True)
        empty_layout.addWidget(self.empty_message)
        empty_actions = QHBoxLayout()
        self.find_models_button = QPushButton("Find Existing Models…")
        self.find_models_button.clicked.connect(lambda: self._manage_models(find_existing=True))
        self.find_models_button.setVisible(self.allow_manage)
        self.clear_filters_button = QPushButton("Clear Filters")
        self.clear_filters_button.clicked.connect(self._clear_filters)
        self.show_unknown_button = QPushButton("Show Compatibility Unknown")
        self.show_unknown_button.clicked.connect(lambda: self.compatibility_filter.setCurrentIndex(1))
        self.show_all_button = QPushButton("Show All")
        self.show_all_button.clicked.connect(lambda: self.compatibility_filter.setCurrentIndex(2))
        empty_actions.addWidget(self.find_models_button)
        empty_actions.addWidget(self.clear_filters_button)
        empty_actions.addWidget(self.show_unknown_button)
        empty_actions.addWidget(self.show_all_button)
        empty_actions.addStretch(1)
        empty_layout.addLayout(empty_actions)
        layout.addWidget(self.empty_state)
        self.empty_state.hide()
        self.models = QTreeWidget()
        self.models.setHeaderLabels(("Favorite", "Model", "Model Type", "Declared family", "Cached state", "Recent selection",
            "Author", "Version", "Source", "Size", "Storage"))
        self.models.setRootIsDecorated(False)
        self.models.setColumnWidth(0, 65)
        self.models.setColumnWidth(1, 280)
        self.models.setColumnWidth(2, 125)
        self.models.setColumnWidth(3, 160)
        self.models.itemSelectionChanged.connect(self._selection_changed)
        from .model_resource_browser import ModelResourceBrowser
        from core.resource_browser_view import BrowserViewPreferences, load_view_preferences
        self._view_preferences_saved = deepcopy(getattr(settings, "resource_browser_views", {}))
        self._view_preferences_writable = not self.collect_for_import and callable(getattr(settings, "update_resource_browser_view", None))
        view_notice = ""
        try: initial_view = load_view_preferences(self._view_preferences_saved)
        except ValueError as exc:
            initial_view = BrowserViewPreferences(); self._view_preferences_writable = False
            view_notice = f"Saved layout unavailable. Changes apply to this window only: {exc}"
        self.browser = ModelResourceBrowser(
            self.models, contextual=bool(self.main_model_resource_id and self._role_constraint in {"lora", "vae"}), initial_state=initial_view)
        self.browser.viewChanged.connect(self._browser_changed)
        self.browser_splitter = QSplitter(Qt.Horizontal)
        self.browser_splitter.addWidget(self.browser)
        self.details_panel = QWidget()
        details_layout = QVBoxLayout(self.details_panel)
        details_layout.setContentsMargins(8, 0, 0, 0)
        self.details_heading = QLabel("Model details")
        self.details_heading.setObjectName("sectionTitle")
        details_layout.addWidget(self.details_heading)
        self.resource_details = QPlainTextEdit()
        self.resource_details.setReadOnly(True)
        self.resource_details.setAccessibleName("Selected model details")
        details_layout.addWidget(self.resource_details, 1)
        from .model_artwork_dialog import artwork_service
        self._artwork_service = artwork_service(registry, settings, protected_roots)
        self.browser.configure_artwork(self._artwork_service, jobs, settings=settings)
        self.artwork_button = QPushButton("Preview Artwork…")
        self.artwork_button.clicked.connect(self._open_artwork)
        details_layout.addWidget(self.artwork_button)
        self.browser_splitter.addWidget(self.details_panel)
        self.browser_splitter.setSizes([850, 310])
        layout.addWidget(self.browser_splitter, 1)
        self.folder_scope = QWidget()
        folder_scope_layout = QHBoxLayout(self.folder_scope)
        folder_scope_layout.setContentsMargins(0, 0, 0, 0)
        self.folder_scope_label = QLabel()
        self.folder_scope_label.setWordWrap(True)
        self.folder_scope_label.setTextFormat(Qt.PlainText)
        self.clear_folder_scope_button = QPushButton("Show All Folders")
        self.clear_folder_scope_button.clicked.connect(self.browser.clear_folder_scope)
        folder_scope_layout.addWidget(self.folder_scope_label, 1)
        folder_scope_layout.addWidget(self.clear_folder_scope_button)
        layout.addWidget(self.folder_scope)
        self.folder_scope.hide()
        self.view_preferences_notice = QLabel(view_notice)
        self.view_preferences_notice.setWordWrap(True); self.view_preferences_notice.setTextFormat(Qt.PlainText)
        self.view_preferences_notice.setVisible(bool(view_notice)); layout.addWidget(self.view_preferences_notice)
        self.browser.preference_changed.connect(self._save_browser_view)

        paging = QHBoxLayout()
        self.previous = QPushButton("Previous")
        self.next = QPushButton("Next")
        self.previous.clicked.connect(lambda: self._change_page(-1))
        self.next.clicked.connect(lambda: self._change_page(1))
        paging.addWidget(self.previous)
        paging.addWidget(self.next)
        paging.addStretch(1)
        layout.addLayout(paging)

        self.detail = QLabel("Choose a resource to inspect its exact identity and registered locations.")
        self.detail.setWordWrap(True)
        self.detail.setTextFormat(Qt.PlainText)
        self.detail.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.show_identity = QCheckBox("Show Identity and Locations")
        self.show_identity.toggled.connect(self.detail.setVisible)
        details_layout.addWidget(self.show_identity)
        identity_scroll = QScrollArea()
        identity_scroll.setWidgetResizable(True)
        identity_scroll.setWidget(self.detail)
        identity_scroll.setMaximumHeight(160)
        identity_scroll.hide()
        self.show_identity.toggled.connect(identity_scroll.setVisible)
        details_layout.addWidget(identity_scroll)
        self.detail.hide()
        self.import_location = QComboBox()
        self.import_location.setAccessibleName("Exact registered permanent file for import")
        self.import_location.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.import_location.setMinimumContentsLength(26)
        self.import_location.setVisible(self.collect_for_import)
        self.import_location.currentIndexChanged.connect(self._update_actions)
        details_layout.addWidget(self.import_location)
        preferences = QFormLayout()
        pref_row = QVBoxLayout()
        self.display_name = QLineEdit()
        self.display_name.setMaxLength(160)
        self.favorite = QCheckBox("Favorite")
        self.save_preferences_button = QPushButton("Save Name and Favorite")
        self.save_preferences_button.clicked.connect(self._save_preferences)
        self.preference_notice = QLabel()
        self.preference_notice.setWordWrap(True)
        self.preference_notice.setTextFormat(Qt.PlainText)
        self.review_preference_button = QPushButton("Review Pending Edit")
        self.review_preference_button.setToolTip("Clear view filters and return to a model with unsaved name or Favorite changes.")
        self.review_preference_button.clicked.connect(self._review_pending_preference)
        self.discard_preferences_button = QPushButton("Discard Pending Edits…")
        self.discard_preferences_button.clicked.connect(self._discard_pending_preferences)
        self.display_name.textChanged.connect(self._preference_edited)
        self.favorite.toggled.connect(self._preference_edited)
        pref_row.addWidget(self.display_name)
        pref_row.addWidget(self.favorite)
        pref_row.addWidget(self.save_preferences_button)
        pref_row.addWidget(self.preference_notice)
        pref_actions = QHBoxLayout()
        pref_actions.addWidget(self.review_preference_button)
        pref_actions.addWidget(self.discard_preferences_button)
        pref_actions.addStretch(1)
        pref_row.addLayout(pref_actions)
        preferences.addRow("Display name", pref_row)
        details_layout.addLayout(preferences)
        self.selection_summary = QLabel()
        self.selection_summary.setWordWrap(True)
        self.selection_summary.setTextFormat(Qt.PlainText)
        layout.addWidget(self.selection_summary)
        self.pending_choice = QLabel("Pending choice: none")
        self.pending_choice.setTextFormat(Qt.PlainText)
        self.pending_choice.setWordWrap(True)
        layout.addWidget(self.pending_choice)
        self.recipe_empty_spacer = QWidget()
        self.recipe_empty_spacer.setSizePolicy(QSizePolicy.Preferred, QSizePolicy.Expanding)
        self.recipe_empty_spacer.hide()
        layout.addWidget(self.recipe_empty_spacer, 1)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        self.recovery_guidance = QLabel()
        self.recovery_guidance.setWordWrap(True)
        self.recovery_guidance.setTextFormat(Qt.PlainText)
        self.recovery_guidance.hide()
        layout.addWidget(self.recovery_guidance)
        self.repair_choice_button = QPushButton("Locate Exact Copy…")
        self.repair_choice_button.setToolTip(
            "Choose a file with the same exact bytes for this model. Nothing is applied until you use the model again."
        )
        self.repair_choice_button.clicked.connect(self._locate_exact_copy)
        self.repair_choice_button.hide()
        layout.addWidget(self.repair_choice_button, 0, Qt.AlignLeft)
        bind_status_footer(self, self.status, title="Model selector")

        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.manage_button = QPushButton("Manage or Index Models…")
        self.manage_button.clicked.connect(self._manage_models)
        buttons.addButton(self.manage_button, QDialogButtonBox.ActionRole)
        self.manage_button.setVisible(self.allow_manage)
        self.metadata_button = QPushButton("Find Model Metadata…")
        self.metadata_button.clicked.connect(self._find_model_metadata)
        buttons.addButton(self.metadata_button, QDialogButtonBox.ActionRole)
        self.metadata_button.setVisible(self.allow_manage)
        self.locate_copy_button = QPushButton("Locate Exact Copy…")
        self.locate_copy_button.clicked.connect(self._locate_exact_copy)
        self.locate_copy_button.setVisible(self.allow_manage)
        details_layout.addWidget(self.locate_copy_button)
        self.required_resources_button = QPushButton("Required Resources…")
        self.required_resources_button.clicked.connect(self._open_required_resources)
        self.required_resources_button.setVisible(self.allow_manage)
        details_layout.addWidget(self.required_resources_button)
        self.upscaler_models_button = QPushButton("Get Upscaler Models…")
        self.upscaler_models_button.clicked.connect(self._open_upscaler_models)
        self.upscaler_models_button.setToolTip(
            "Review optional ESRGAN upscaler packs. Opening the catalog does not download a model."
        )
        buttons.addButton(self.upscaler_models_button, QDialogButtonBox.ActionRole)
        self.upscaler_models_button.setVisible(self.allow_manage and role=="upscaler")
        self.reload_button = QPushButton("Reload")
        self.reload_button.clicked.connect(self._load)
        buttons.addButton(self.reload_button, QDialogButtonBox.ActionRole)
        self.use_button = QPushButton("Use Model")
        self.use_button.setToolTip("Verify this exact model's files, then apply the pending choice. Does not start a model job.")
        if self.collect_for_import:
            self.use_button.setText("Use File Choice in Import")
            self.use_button.setToolTip("Return the exact registered path and declaration. File verification remains part of the combined import.")
            self.display_name.setReadOnly(True)
            self.favorite.setEnabled(False)
            for control in (self.display_name, self.favorite, self.save_preferences_button,
                            self.preference_notice, self.review_preference_button,
                            self.discard_preferences_button): control.hide()
            label = preferences.labelForField(pref_row)
            if label is not None: label.hide()
        self.use_button.setProperty("primary", True)
        self.use_button.clicked.connect(self._use)
        buttons.addButton(self.use_button, QDialogButtonBox.AcceptRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.search.textChanged.connect(self._filter_changed)
        self.family_filter.textChanged.connect(self._filter_changed)
        self.role_filter.currentIndexChanged.connect(self._filter_changed)
        self.favorite_only.toggled.connect(self._filter_changed)
        self.recent_only.toggled.connect(self._filter_changed)
        self.compatibility_filter.currentIndexChanged.connect(self._filter_changed)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        if self.task_focused:
            self._task_options_changed(False)
        self._update_actions()
        self._load()

    @staticmethod
    def _progress(job):
        return lambda current, total, phase: job.update(
            min(2_147_483_647, int(current) // (1024 * 1024)),
            min(2_147_483_647, int(total) // (1024 * 1024)),
            phase,
        )

    def _run(self, label, callable_, complete):
        if self._task is not None:
            return
        def run(job):
            return callable_(job)
        task = CoordinatedCallTask(self.jobs, label, run, resource="io")
        self._task = task
        self.status.setText(label)
        task.signals.complete.connect(lambda result: self._finish(task, complete, result))
        task.signals.failed.connect(lambda message: self._finish(task, self._failed, message))
        task.signals.cancelled.connect(lambda: self._finish(task, self._failed, "Stopped at a safe boundary."))
        self._update_actions()
        task.start()

    def _finish(self, task, complete, result):
        if self._task is not task:
            return
        self._task = None
        if self._closing:
            super().reject()
            return
        complete(result)
        if self._reload_pending and self._task is None:
            self._load()
        self._update_actions()

    def _load(self):
        if self._task is not None:
            self._reload_pending = True
            return
        if self._closing:
            return
        self._unavailable_choice = None
        self.recovery_guidance.hide()
        self.repair_choice_button.hide()
        if self._copy_restore_preferences is None:
            self._copy_preference_stale = False
        self._reload_pending = False
        query = self.search.text()
        role = str(self.role_filter.currentData() or "")
        family = self.family_filter.text()
        favorites = self.favorite_only.isChecked()
        recent = self.recent_only.isChecked()
        def read(_job):
            rows, revision = self.service.project(
                query=query,
                role=role,
                family=family,
                favorite_only=favorites,
                recent_only=recent,
                main_model_resource_id=self.main_model_resource_id,
                compatibility_scope=(str(self.compatibility_filter.currentData())
                    if self.main_model_resource_id and self._role_constraint in {"lora", "vae"} else "all"),
                qualified_compatibility=self.qualified_compatibility,
            )
            if self.allowed_declarations:
                from core.model_organization import normalize_model_family
                rows = tuple(row for row in rows if (
                    row.resource_id, row.role, normalize_model_family(row.family)
                ) in self.allowed_declarations)
            if not self.collect_for_import: return rows, revision
            snapshot, current = self.registry.snapshot()
            if current != revision: raise ValueError("The registry changed while reading choices. Reload before selecting a file.")
            locations = {row.resource_id: self._permanent_locations(snapshot, row.resource_id) for row in rows}
            return rows, revision, locations
        self._run("Reading model choices", read, self._loaded)

    def _loaded(self, result):
        if self.collect_for_import: self._rows, self._revision, self._import_locations = result
        else: self._rows, self._revision = result
        try: self.browser.set_rows(self._rows)
        except ValueError as exc:
            self._rows = (); self.browser.set_rows(())
            self._render(); self.status.setText(str(exc)); return
        self._rows = self.browser.visible_rows()
        self._page = min(self._page, max(0, (len(self._rows) - 1) // self.PAGE_SIZE))
        self.status.setText("Choose one declaration and an exact permanent file. Verification waits for the combined import."
            if self.collect_for_import else "Choose one declaration. Exact bytes will be checked only after Use.")
        selected_id = self._reload_selected_resource_id
        self._reload_selected_resource_id = ""
        returned_from_models = getattr(self, "_reload_from_models", False)
        self._reload_from_models = False
        declaration = self._reload_selected_declaration
        self._reload_selected_declaration = None
        apply_path = self._reload_apply_path
        self._reload_apply_path = ""
        if apply_path:
            normalized = apply_path.casefold()
            selected_id = next((row.resource_id for row in self._rows
                                if any(str(path).casefold() == normalized for path in row.locations)), "")
        if returned_from_models:
            choices = [row for row in self._rows if row.resource_id == selected_id]
            if len(choices) != 1:
                self.status.setText(
                    "That registered model has more than one declaration. Choose its model type and family before Use Model."
                    if choices else
                    "That model is not available for this picker role. Choose another registered model or return to Models."
                )
                selected_id = ""
        def matches(row):
            return row.resource_id == selected_id and (
                bool(apply_path) or declaration is None or (row.role, row.family) == declaration)
        if selected_id:
            for index, row in enumerate(self._rows):
                if matches(row):
                    self._page = index // self.PAGE_SIZE
                    break
        self._render()
        if selected_id:
            for index in range(self.models.topLevelItemCount()):
                item = self.models.topLevelItem(index)
                if matches(item.data(0, Qt.UserRole)):
                    self.models.setCurrentItem(item)
                    break
        if selected_id in self._preference_drafts and self._current_row() is None:
            self.status.setText(
                "That model with pending name and Favorite edits is no longer available in this picker. "
                "Restore its registration and Reload, or discard the pending edits. Nothing was saved."
            )
        restore = self._copy_restore_preferences
        self._copy_restore_preferences = None
        if restore and self._current_row() is not None and self._current_row().resource_id == restore[0]:
            _, baseline, pending = restore
            row = self._current_row()
            if pending != baseline:
                self.display_name.setText(pending[0]); self.favorite.setChecked(pending[1])
                self._copy_preference_stale = (row.display_name, row.favorite) != baseline
                if self._copy_preference_stale:
                    self.status.setText("Saved name/favorite changed during recovery. Your pending values are retained; copy them and Reload before saving.")
        if apply_path:
            if selected_id and self._current_row() is not None:
                self._use()
            else:
                self.status.setText("The selected upscaler was not registered. Review the catalog result before using it.")
        restore_unavailable = self._restore_unavailable_after_reload
        self._restore_unavailable_after_reload = None
        row = self._current_row()
        if (restore_unavailable is not None and row is not None
                and (row.resource_id, row.role, row.family) == restore_unavailable):
            self._unavailable_choice = restore_unavailable
            self.status.setText(
                "No exact copy was added. Your pending model is unchanged. Locate Exact Copy, "
                "choose another model, or Reload after restoring the original file."
            )
            self.recovery_guidance.setText(
                "No copy was added. Your current model choice is unchanged; find an identical file, "
                "choose another model, or Reload after restoring the original."
            )
            self._update_actions()

    def _filter_changed(self, *_):
        role = str(self.role_filter.currentData() or "")
        if role != self._managed_role:
            self._managed_role = role
            self._pack_preview = None
            choices = self.component_source.choices(role) if self.component_source else ()
            with QSignalBlocker(self.managed_models):
                self.managed_models.clear()
                for model_id, label, typed_role, _family in choices:
                    self.managed_models.addItem(f"{label} ({typed_role})", model_id)
            for widget in (self.managed_models, self.preview_pack_button, self.register_pack_button):
                widget.setVisible(bool(choices) and role in ("tagger", "captioner"))
        with QSignalBlocker(self.role_tabs):
            for index in range(self.role_tabs.count()):
                if self.role_tabs.tabData(index) == role:
                    self.role_tabs.setCurrentIndex(index)
                    break
        self._page = 0
        self._load()

    def _role_tab_changed(self, index):
        value = self.role_tabs.tabData(index)
        self.role_filter.setCurrentIndex(self.role_filter.findData(value))

    def _browser_changed(self):
        from core.resource_browser_view import resource_key
        current = self._current_row()
        selected = resource_key(current) if current is not None else None
        self._rows = self.browser.visible_rows()
        self._page = next((index // self.PAGE_SIZE for index, row in enumerate(self._rows) if resource_key(row) == selected), 0)
        self._render()
        if selected is not None:
            for index in range(self.models.topLevelItemCount()):
                item = self.models.topLevelItem(index)
                if resource_key(item.data(0, Qt.UserRole)) == selected:
                    self.models.setCurrentItem(item)
                    break
        self._update_folder_scope()

    def _task_options_changed(self, expanded):
        self.more_options_button.setText(
            "Hide Model Options and Details" if expanded else "More Model Options and Details"
        )
        self.browser.set_picker_chrome_visible(expanded)
        self.details_panel.setVisible(expanded)
        self.manage_button.setVisible(self.allow_manage and expanded)
        self.metadata_button.setVisible(self.allow_manage and expanded)
        self._update_folder_scope()

    def _update_folder_scope(self):
        if not self.task_focused:
            return
        folder, label = self.browser.active_folder_scope()
        compact = bool(folder) and not self.more_options_button.isChecked()
        self.folder_scope_label.setText(f"Folder filter: {label}" if compact else "")
        self.folder_scope_label.setToolTip(folder if compact else "")
        self.folder_scope.setVisible(compact)

    def _save_browser_view(self, changes):
        if not self._view_preferences_writable: return
        try:
            saved = self.settings.update_resource_browser_view("models", changes, expected=deepcopy(self._view_preferences_saved))
        except (ValueError, OSError, RuntimeError) as exc:
            self._view_preferences_writable = False
            self.view_preferences_notice.setText(f"Layout changed for this window. Saved preferences were preserved: {exc}")
            self.view_preferences_notice.show(); return
        self._view_preferences_saved = deepcopy(saved)
        self.view_preferences_notice.hide()

    def _clear_filters(self):
        blockers = [QSignalBlocker(widget) for widget in (
            self.search, self.family_filter, self.favorite_only, self.recent_only,
            self.role_filter, self.compatibility_filter)]
        self.search.clear()
        self.family_filter.clear()
        self.favorite_only.setChecked(False)
        self.recent_only.setChecked(False)
        self.compatibility_filter.setCurrentIndex(self.compatibility_filter.findData("all"))
        if not self._role_constraint:
            self.role_filter.setCurrentIndex(0)
            with QSignalBlocker(self.role_tabs):
                self.role_tabs.setCurrentIndex(0)
        del blockers
        self.browser.reset_filters()
        self._filter_changed()

    def _change_page(self, delta):
        self._page = max(0, self._page + delta)
        self._render()

    def _render(self):
        start = self._page * self.PAGE_SIZE
        self.browser.begin_update()
        self.models.clear()
        for row in self._rows[start:start + self.PAGE_SIZE]:
            item = QTreeWidgetItem((
                "Favorite" if row.favorite else "",
                row.display_name,
                row.role.replace("_", " ").title(),
                row.family,
                "Stored; verify to use" if row.cached_status == "stored" else "No local location",
                row.last_used_at,
                row.author or "Unknown", getattr(row, "version_label", "") or "Unknown", row.source or "Unknown",
                f"{row.size_bytes / 1024**2:,.1f} MiB" if row.size_bytes else "Unknown", getattr(row, "storage_state", "Unknown"),
            ))
            item.setData(0, Qt.UserRole, row)
            item.setToolTip(1, row.resource_id)
            self.models.addTopLevelItem(item)
        shown_from = min(start + 1, len(self._rows))
        shown_to = min(start + self.PAGE_SIZE, len(self._rows))
        choice_count = len(self._rows)
        label = f"{choice_count:,} model {'choice' if choice_count == 1 else 'choices'}"
        if choice_count > self.PAGE_SIZE:
            label += f" · Showing {shown_from:,}–{shown_to:,}"
        self.count.setText(label)
        self.empty_state.setVisible(not self._rows)
        if self.allowed_declarations:
            self.count.hide()  # Browser count or the empty action already names the outcome.
            self.browser_splitter.setVisible(bool(self._rows))
            self.recipe_empty_spacer.setVisible(not self._rows)
            self.selection_summary.setVisible(bool(self._rows or self.selected_resources))
            self.clear_filters_button.setVisible(bool(
                self.search.text() or self.favorite_only.isChecked() or self.recent_only.isChecked()
            ))
        contextual = bool(self.main_model_resource_id and self._role_constraint in {"lora", "vae"})
        current_scope = self.compatibility_filter.currentData()
        self.show_unknown_button.setVisible(contextual and current_scope == "compatible")
        self.show_all_button.setVisible(contextual and current_scope != "all")
        if self.allowed_declarations:
            self.empty_message.setText(
                "No matching registered file is available for this Video recipe. "
                "Find Existing Models to register the exact file or correct its Wan2.2 "
                "declaration, then Reload. Other models cannot be used here."
            )
        elif contextual and current_scope == "compatible":
            self.empty_message.setText("No exact role-specific compatibility evidence matches this model. Browse Compatibility unknown or Show all, then review the exact adapter before running.")
        elif contextual and current_scope == "unknown":
            self.empty_message.setText("No unknown companions match these filters. Show all to inspect every registered choice.")
        else:
            self.empty_message.setText("No registered models match this view. Models already on disk need a folder preview and registration before they appear here. You can also clear the filters.")
        self.browser.show_page(self._rows[start:start + self.PAGE_SIZE])
        self.previous.setEnabled(self._task is None and self._page > 0)
        self.next.setEnabled(self._task is None and start + self.PAGE_SIZE < len(self._rows))
        if self.allowed_declarations:
            self.previous.setVisible(len(self._rows) > self.PAGE_SIZE)
            self.next.setVisible(len(self._rows) > self.PAGE_SIZE)
        self._selection_changed()

    def _current_row(self):
        item = self.models.currentItem()
        return item.data(0, Qt.UserRole) if item is not None else None

    def _preference_edited(self, *_):
        if self.collect_for_import or not self._active_preference_id or self._active_preference_baseline is None:
            return
        name, favorite = self.display_name.text(), self.favorite.isChecked()
        saved_name, saved_favorite, default_name = self._active_preference_baseline
        original_text = "" if saved_name == default_name else saved_name
        if (name, favorite) == (original_text, saved_favorite):
            self._preference_drafts.pop(self._active_preference_id, None)
        else:
            prior = self._preference_drafts.get(self._active_preference_id)
            baseline = prior[2:] if prior is not None else (saved_name, saved_favorite)
            self._preference_drafts[self._active_preference_id] = (name, favorite, *baseline)
        self._show_preference_state()
        self._update_actions()

    def _selected_preference_stale(self):
        row = self._current_row()
        draft = self._preference_drafts.get(row.resource_id) if row is not None else None
        return bool(draft and (row.display_name, row.favorite) != draft[2:])

    def _show_preference_state(self):
        count = len(self._preference_drafts)
        if not count:
            self.preference_notice.hide()
            self.review_preference_button.hide()
            self.discard_preferences_button.hide()
            return
        if self._selected_preference_stale():
            message = "Saved name or Favorite changed since this edit began. Copy your draft, then discard it and reapply against current values before saving."
        else:
            message = f"{count} unsaved model {'edit' if count == 1 else 'edits'}. Save or discard before using a model or closing."
        self.preference_notice.setText(message)
        self.preference_notice.show()
        self.review_preference_button.setText("Go to Another Pending Model")
        self.review_preference_button.setVisible(
            self._active_preference_id in self._preference_drafts and any(
                resource_id != self._active_preference_id for resource_id in self._preference_drafts
            )
        )
        self.discard_preferences_button.show()

    def _review_pending_preference(self):
        if self._task is not None or not self._preference_drafts:
            return
        resource_id = next((key for key in self._preference_drafts if key != self._active_preference_id),
                           next(iter(self._preference_drafts)))
        self._reload_selected_resource_id = resource_id
        self._reload_selected_declaration = None
        self._clear_filters()
        self.status.setText("Showing an unsaved model edit. Review and Save it, or discard pending edits.")

    def _discard_pending_preferences(self):
        if self._task is not None or not self._preference_drafts:
            return
        count = len(self._preference_drafts)
        if QMessageBox.question(
                self, "Discard Pending Model Edits?",
                f"Discard unsaved name and Favorite changes for {count} model(s)? Saved preferences and model files stay unchanged.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return
        self._preference_drafts.clear()
        self._copy_preference_stale = False
        self._selection_changed()
        self.status.setText("Pending name and Favorite edits discarded. Saved model preferences are unchanged.")

    def _confirm_preference_close(self):
        if not self._preference_drafts:
            return True
        count = len(self._preference_drafts)
        if QMessageBox.question(
                self, "Close Without Saving Model Edits?",
                f"Discard unsaved name and Favorite changes for {count} model(s) and close? Saved preferences and model files stay unchanged.",
                QMessageBox.Yes | QMessageBox.No, QMessageBox.No) != QMessageBox.Yes:
            return False
        self._preference_drafts.clear()
        return True

    def _selection_changed(self):
        row = self._current_row()
        self._active_preference_id = row.resource_id if row is not None else ""
        self._active_preference_baseline = (
            row.display_name, row.favorite, row.default_name
        ) if row is not None else None
        if (row is None or (row.resource_id, row.role, row.family) != self._unavailable_choice):
            self._unavailable_choice = None
        self.artwork_button.setEnabled(row is not None and self._task is None)
        if self.collect_for_import:
            prior = self.import_location.currentData()
            with QSignalBlocker(self.import_location):
                self.import_location.clear(); self.import_location.addItem("Choose an exact permanent file…", None)
                locations = self._import_locations.get(row.resource_id, ()) if row is not None else ()
                for path in locations:
                    self.import_location.addItem(path, path)
                    self.import_location.setItemData(self.import_location.count() - 1, path, Qt.ToolTipRole)
                chosen = self.import_location.findData(prior) if prior is not None else -1
                self.import_location.setCurrentIndex(chosen if chosen >= 0 else 1 if len(locations) == 1 else 0)
        if row is not None:
            pending = (f"Pending choice: {row.display_name} · {row.role.replace('_', ' ')} "
                       f"· {row.family or 'family unknown'}")
        elif self.allowed_declarations and not self._rows:
            pending = "Pending choice: none — register the exact required file or Cancel."
        else:
            pending = "Pending choice: none — select a row below the filters."
        self.pending_choice.setText(pending)
        if row is None:
            self.resource_details.setPlainText("Select a card to see its description, family, notes and registered locations.\n\nFind Existing Models previews a folder before registration.")
            self.detail.setText("No resource selected. Use Manage or Index Models to add an existing safe tensor folder.")
            with QSignalBlocker(self.display_name), QSignalBlocker(self.favorite):
                self.display_name.clear()
                self.favorite.setChecked(False)
            summary = self.service.summarize(self.selected_resources, required_roles=self.required_roles)
        else:
            info = [row.display_name, f"{row.role.replace('_', ' ').title()} · {row.family}"]
            if row.title: info.append("Embedded title: " + row.title)
            if row.architecture_claim: info.append("Embedded architecture claim: " + row.architecture_claim)
            if row.derivative_label: info.append("Recorded derivative/variant label: " + row.derivative_label)
            if row.author: info.append("Embedded author: " + row.author)
            if row.resolution: info.append("Embedded resolution: " + row.resolution)
            if row.size_bytes: info.append(f"File size: {row.size_bytes / (1024 ** 3):.2f} GiB")
            if row.description: info.append("Embedded description:\n" + row.description)
            if row.tags: info.append("Tags: " + ", ".join(row.tags))
            if row.notes: info.append("Your notes:\n" + row.notes)
            if row.provider_details: info.append("Attached provider metadata:\n\n" + "\n\n".join(row.provider_details))
            if row.provider_triggers: info.append("Provider suggested trigger words (review before use): " + ", ".join(row.provider_triggers))
            if self.main_model_resource_id:
                info.append("With selected model: " + row.context_compatibility.replace("_", " ").title()
                    + (" (" + row.context_evidence + ")" if row.context_evidence else "")
                    + ". Exact adapter preflight still decides run support.")
            if row.folder_hints:
                info.append("Suggested from folder location: " + ", ".join(f"{media} / {family}" for media,family in row.folder_hints)
                            + ". This is an organizational hint, not verified compatibility.")
            if row.source: info.append("Declared source: " + row.source)
            if row.license_note: info.append("Declared license: " + row.license_note)
            info.append("Registered locations:\n" + "\n".join(row.locations[:100]))
            info.append("Recorded file choice only. The combined import verifies exact files after approval."
                        if self.collect_for_import else
                        "Stored model. Exact files are checked when you choose Use Model."
                        if row.cached_status == "stored" else
                        "No local location. Register an existing model file through Manage or Index Models before using this resource.")
            info.append("Embedded details are model-author claims; compatibility remains unverified.")
            self.resource_details.setPlainText("\n\n".join(info))
            shown_locations = row.locations[:100]
            locations = "\n".join(f"• {value}" for value in shown_locations) or "• No registered local location"
            if len(row.locations) > len(shown_locations):
                locations += f"\n• {len(row.locations) - len(shown_locations):,} more registered location(s)"
            self.detail.setText(
                f"{row.resource_id}\nRole: {row.role.replace('_', ' ').title()}\nDeclared family: {row.family}\n"
                f"Exact verification: {row.verification_status.replace('_', ' ').capitalize()}.\n"
                f"Cached compatibility: {row.compatibility.replace('_', ' ').capitalize()}. "
                f"Recommendation: {row.recommendation_status.replace('_', ' ').capitalize()}. "
                f"Run readiness: {'Ready' if row.ready_to_run else 'Not assessed; check the exact adapter preflight'}.\n"
                f"Registered locations:\n{locations}"
            )
            draft = self._preference_drafts.get(row.resource_id)
            with QSignalBlocker(self.display_name), QSignalBlocker(self.favorite):
                self.display_name.setText(draft[0] if draft else
                                          "" if row.display_name == row.default_name else row.display_name)
                self.favorite.setChecked(draft[1] if draft else row.favorite)
            summary = self.service.summarize(
                self.selected_resources,
                required_roles=self.required_roles,
                candidate=row,
            )
        self.selection_summary.setText(summary.message)
        self._copy_preference_stale = self._selected_preference_stale()
        self._show_preference_state()
        self._update_actions()

    def _save_preferences(self):
        if self.collect_for_import: return
        if self._selected_preference_stale():
            self.status.setText("Saved preferences changed. Copy your pending values, discard them, then reapply against current values before saving.")
            return
        row = self._current_row()
        if row is None:
            return
        display_name = self.display_name.text()
        favorite = self.favorite.isChecked()
        revision = self._revision
        self._run(
            "Saving model display preferences",
            lambda _job: self.service.save_preferences(
                row.resource_id,
                display_name=display_name,
                favorite=favorite,
                expected_registry=revision,
            ),
            lambda _revision: self._preferences_saved(row),
        )

    def _preferences_saved(self, row):
        self._preference_drafts.pop(row.resource_id, None)
        self._reload_selected_resource_id = row.resource_id
        self._reload_selected_declaration = (row.role, row.family)
        self._load()

    def _use(self):
        if self.collect_for_import:
            self._collect_file_choice(); return
        if self._preference_drafts:
            if self.task_focused and not self.more_options_button.isChecked():
                self.more_options_button.setChecked(True)
            self.status.setText("Save each pending name and Favorite edit, or choose Discard Pending Edits before Use Model. Your model choice has not changed.")
            row = self._current_row()
            if row is None or row.resource_id not in self._preference_drafts:
                self._review_pending_preference()
            elif self._selected_preference_stale():
                self.discard_preferences_button.setFocus()
            else:
                self.save_preferences_button.setFocus()
            return
        row = self._current_row()
        if row is None:
            return
        revision = self._revision
        def verify(job):
            try:
                return self.service.verify_and_record(
                    row.resource_id, row.role, row.family,
                    expected_registry=revision,
                    progress=self._progress(job),
                    should_cancel=lambda: job.cancel_requested,
                )
            except ModelLocationUnavailable as exc:
                return _UnavailableChoice(row.resource_id, row.role, row.family, str(exc))
        self._run(
            "Verifying selected model bytes",
            verify,
            self._used,
        )

    def _used(self, result):
        if isinstance(result, _UnavailableChoice):
            row = self._current_row()
            if row is None or (row.resource_id, row.role, row.family) != (
                    result.resource_id, result.role, result.family):
                self.status.setText("The pending model changed during verification. Select a model and try again.")
                return
            self._unavailable_choice = (result.resource_id, result.role, result.family)
            self.status.setText(
                "This model's registered file cannot be verified. Nothing was applied. "
                "Locate an exact copy of this model, choose another model, or Reload after restoring its file. "
                "Use Model will check the bytes again before applying.\nExact check: " + result.reason
            )
            self.recovery_guidance.setText(
                "This model file could not be verified. Your current model choice is unchanged. "
                "Find an identical file or choose another model; then choose Use Model again."
            )
            self._update_actions()
            return
        self.selected_resource_id = result.resource_id
        self.selected_role = result.role
        self.selected_family = result.family
        self.selected_path = result.path
        self.selected_component_model_id = getattr(result, "component_model_id", "")
        self.accept()

    def _clear_pack_preview(self, *_):
        self._pack_preview = None
        self._update_actions()

    def _preview_managed_pack(self):
        if self.collect_for_import: return
        model_id = self.managed_models.currentData()
        if not model_id or self.component_source is None:
            return
        self._pack_preview = None
        source = self.component_source
        revision = self._revision
        self._run("Verifying installed component files; no model loading", lambda job: (
            source.inspect(model_id, progress=self._progress(job), should_cancel=lambda: job.cancel_requested), revision), self._managed_pack_previewed)

    def _managed_pack_previewed(self, result):
        self._pack_preview = result
        inspection, _revision = result
        self.status.setText(f"Verified {len(inspection.identity['files'])} files, {inspection.size:,} bytes. "
            f"{inspection.resource_id}\nRegister records this exact pack in Models. Component Manager still owns installation and runtime readiness.")
        self._update_actions()

    def _register_managed_pack(self):
        if self.collect_for_import: return
        if self._pack_preview is None:
            return
        inspection, revision = self._pack_preview
        source = self.component_source
        def register(job):
            if source.target(inspection.identity["model_id"]) != inspection.path:
                raise ValueError("The managed component location changed; preview it again.")
            return self.registry.register_managed_pack(inspection, expected_registry=revision,
                protected_roots=self.service.protected_roots, progress=self._progress(job), should_cancel=lambda: job.cancel_requested)
        self._run("Registering the verified component identity", register, self._managed_pack_registered)

    def _managed_pack_registered(self, resource_id):
        self._pack_preview = None
        self.search.setText(resource_id)
        self._load()

    def _locate_exact_copy(self):
        row = self._current_row()
        if self._task is not None or row is None or not self.allow_manage or not row.resource_id.startswith("sha256:"): return
        from .model_workspace_dialog import ExactModelCopyDialog
        self._copy_restore_preferences = (row.resource_id, (row.display_name, row.favorite),
            (self.display_name.text(), self.favorite.isChecked()))
        self._reload_selected_resource_id = row.resource_id
        self._reload_selected_declaration = row.role, row.family
        dialog = ExactModelCopyDialog(self, row.resource_id, self._revision, self.service.protected_roots)
        self._exact_copy_dialog = dialog
        dialog.finished.connect(lambda _: self._exact_copy_finished(dialog))
        dialog.show()

    def _exact_copy_finished(self, dialog):
        from shiboken6 import isValid
        if not isValid(self): return
        if dialog._parent_closing:
            if dialog._close_parent: dialog._close_parent()
            return
        if not self._closing and self.isVisible():
            if dialog.result() != QDialog.Accepted:
                self._restore_unavailable_after_reload = self._unavailable_choice
            self._load()

    def _open_artwork(self):
        row = self._current_row()
        if self._task is not None or row is None: return
        prior = getattr(self, "_artwork_dialog", None)
        if prior is not None and prior.isVisible():
            prior.raise_(); prior.activateWindow(); return
        from .model_artwork_dialog import ModelArtworkDialog
        dialog = ModelArtworkDialog(self._artwork_service, self.jobs, row.resource_id, self,
            settings=self.settings, read_only=True)
        self._artwork_dialog = dialog
        dialog.setModal(True); dialog.show()

    def _manage_models(self, *, find_existing=False):
        if self._task is not None or not self.allow_manage:
            return
        from .model_workspace_dialog import ModelWorkspaceDialog
        dialog = ModelWorkspaceDialog(
            self.registry,
            self.jobs,
            self,
            protected_roots=self.service.protected_roots,
            settings=self.settings,
            initial_folder_picker=find_existing,
            initial_role=str(self.role_filter.currentData() or ""),
            return_to_selector=True,
        )
        result = dialog.exec()
        if result == QDialog.Accepted and dialog.selected_for_selector_id:
            self._reload_selected_resource_id = dialog.selected_for_selector_id
            self._reload_from_models = True
            self._clear_filters()
        elif find_existing and dialog.registration_applied:
            self._clear_filters()
        else:
            self._load()

    def _find_model_metadata(self):
        row = self._current_row()
        if self._task is not None or row is None or not self.allow_manage:
            return
        from .model_workspace_dialog import ModelWorkspaceDialog
        dialog = ModelWorkspaceDialog(self.registry,self.jobs,self,
            protected_roots=self.service.protected_roots,settings=self.settings,
            initial_metadata_resource=row.resource_id)
        dialog.exec()
        self._load()

    def _open_upscaler_models(self):
        if self._task is not None or not (self.allow_manage and self._role_constraint=="upscaler"):
            return
        current = self._current_row()
        self._reload_selected_resource_id = current.resource_id if current is not None else ""
        self._reload_selected_declaration = (current.role, current.family) if current is not None else None
        from .resolution_models_dialog import UpscalerModelsDialog
        dialog = UpscalerModelsDialog(
            self.registry,
            self.jobs,
            self,
            protected_roots=self.service.protected_roots,
            allow_select=True,
        )
        if dialog.exec() == QDialog.Accepted and dialog.selected_path:
            with QSignalBlocker(self.browser):
                self.browser.reset_filters()
            blockers = tuple(QSignalBlocker(widget) for widget in (
                self.search, self.family_filter, self.favorite_only, self.recent_only,
            ))
            self.search.clear()
            self.family_filter.clear()
            self.favorite_only.setChecked(False)
            self.recent_only.setChecked(False)
            del blockers
            self._reload_apply_path = str(dialog.selected_path)
        self._load()

    def _failed(self, message):
        self._restore_unavailable_after_reload = None
        self._unavailable_choice = None
        self.recovery_guidance.hide()
        self.repair_choice_button.hide()
        if isinstance(message, ResourceRecoveryRequired):
            message = str(message)
        self.status.setText(str(message))
        self._update_actions()

    def _update_actions(self, *_):
        idle = self._task is None
        selected = self._current_row() is not None if hasattr(self, "models") else False
        if hasattr(self, "use_button"):
            self.use_button.setEnabled(idle and (selected or bool(self._preference_drafts)))
            if not self.collect_for_import:
                self.use_button.setText("Review Pending Edits" if self._preference_drafts else "Use Model")
                self.use_button.setToolTip(
                    "Save or discard the pending name and Favorite edits before using a model."
                    if self._preference_drafts else
                    "Verify this exact model's files, then apply the pending choice. Does not start a model job."
                )
            self.save_preferences_button.setEnabled(idle and selected and not self._selected_preference_stale())
            self.display_name.setEnabled(idle and selected and not self.collect_for_import)
            self.favorite.setEnabled(idle and selected and not self.collect_for_import)
            self.review_preference_button.setEnabled(idle)
            self.discard_preferences_button.setEnabled(idle)
            row = self._current_row()
            can_locate = idle and self.allow_manage and row is not None and row.resource_id.startswith("sha256:")
            contextual_recovery = (self.allow_manage and row is not None and
                (row.resource_id, row.role, row.family) == self._unavailable_choice)
            self.locate_copy_button.setVisible(self.allow_manage and not contextual_recovery)
            self.recovery_guidance.setVisible(contextual_recovery)
            self.repair_choice_button.setVisible(contextual_recovery)
            self.locate_copy_button.setEnabled(can_locate)
            self.repair_choice_button.setEnabled(can_locate)
            self.required_resources_button.setEnabled(idle and self.allow_manage and row is not None and row.resource_id.startswith("sha256:"))
            self.manage_button.setEnabled(idle and self.allow_manage)
            self.metadata_button.setEnabled(idle and selected and self.allow_manage)
            self.find_models_button.setEnabled(idle and self.allow_manage)
            self.clear_filters_button.setEnabled(idle)
            self.browser.set_busy(not idle)
            self.role_tabs.setEnabled(idle)
            self.upscaler_models_button.setEnabled(
                idle and self.allow_manage and self._role_constraint=="upscaler"
            )
            self.reload_button.setEnabled(idle)
            self.search.setEnabled(True)
            self.family_filter.setEnabled(True)
            self.favorite_only.setEnabled(True)
            self.recent_only.setEnabled(True)
            self.compatibility_filter.setEnabled(idle)
            self.role_filter.setEnabled(not self._role_constraint)
            self.models.setEnabled(idle)
            self.managed_models.setEnabled(idle)
            self.preview_pack_button.setEnabled(idle and self.component_source is not None)
            self.register_pack_button.setEnabled(idle and self._pack_preview is not None)
            if self.collect_for_import:
                self.use_button.setEnabled(idle and selected and self.import_location.currentData() is not None)
                self.import_location.setEnabled(idle)
                self.save_preferences_button.setEnabled(False)
                for widget in (self.managed_models, self.preview_pack_button, self.register_pack_button):
                    widget.setEnabled(False); widget.hide()

    def _permanent_locations(self, snapshot, identity):
        """Project recorded paths only; no file stat, content read or recency write."""
        from core.model_storage_policy import retained_fast_roots
        record = snapshot.get("resources", {}).get(identity, {})
        if not identity.startswith("sha256:") or record.get("identity", {}).get("kind") == "managed_model_pack": return ()
        excluded = (*self.service.protected_roots, *retained_fast_roots(snapshot))
        paths = []
        for location in record.get("locations", ()):
            path = location.get("path", "")
            if (not isinstance(path, str) or not Path(path).is_absolute() or location.get("cache_entry_id")
                    or any(Path(path).is_relative_to(Path(root)) for root in excluded)):
                continue
            if path not in paths: paths.append(path)
        return tuple(paths)

    def _collect_file_choice(self):
        row, path = self._current_row(), self.import_location.currentData()
        if self._task is not None or row is None or not path: return
        revision = self._revision
        def choose(_job):
            snapshot, current = self.registry.snapshot()
            record = snapshot.get("resources", {}).get(row.resource_id, {})
            if (current != revision or path not in self._permanent_locations(snapshot, row.resource_id)
                    or not any(value.get("role") == row.role and value.get("family") == row.family
                        for value in record.get("declarations", ()))):
                raise ValueError("The exact registered file or declaration changed. Reload and choose again.")
            return row.resource_id, row.role, row.family, path
        self._run("Checking recorded file choice", choose, self._file_choice_collected)

    def _file_choice_collected(self, result):
        self.selected_resource_id, self.selected_role, self.selected_family, self.selected_path = result
        self.accept()

    def _open_required_resources(self):
        row = self._current_row()
        if row is None or self._task is not None or not self.allow_manage: return
        from .model_companion_workflow import open_companion_review
        self._companion_dialog = open_companion_review(self, self.registry, self.jobs,
            row.resource_id, settings=self.settings, protected_roots=self.service.protected_roots)
        dialog = self._companion_dialog
        dialog.finished.connect(lambda _: self._load() if not self._closing and not dialog._closing else None)

    def _request_companion_close(self, continuation):
        dialog = getattr(self, "_companion_dialog", None)
        return dialog.request_parent_close(continuation) if dialog is not None and dialog.isVisible() else True

    def accept(self):
        if not self.collect_for_import and self._preference_drafts:
            self.status.setText("Save or discard pending name and Favorite edits before applying a model.")
            return
        artwork = getattr(self, "_artwork_dialog", None)
        if artwork is not None and artwork.isVisible() and not artwork.request_parent_close(self.accept): return
        if self._request_companion_close(self.accept):
            self.browser.close_artwork(); super().accept()

    def reject(self):
        artwork = getattr(self, "_artwork_dialog", None)
        if artwork is not None and artwork.isVisible() and not artwork.request_parent_close(self.reject): return
        self.browser.close_artwork()
        if not self._request_companion_close(self.reject): return
        from shiboken6 import isValid
        dialog = getattr(self, "_exact_copy_dialog", None)
        if dialog is not None and isValid(dialog) and dialog.isVisible():
            if not dialog.request_parent_close(self.reject): return
        if not self._confirm_preference_close(): return
        self._closing = True
        if self._task is not None:
            self.jobs.cancel(self._task.job_id)
            self.status.setText("Stopping after the current safe model boundary…")
            return
        super().reject()

    def closeEvent(self, event):
        artwork = getattr(self, "_artwork_dialog", None)
        if artwork is not None and artwork.isVisible():
            self.reject(); event.ignore(); return
        self.browser.close_artwork()
        dialog = getattr(self, "_companion_dialog", None)
        if dialog is not None and dialog.isVisible():
            self.reject(); event.ignore(); return
        from shiboken6 import isValid
        dialog = getattr(self, "_exact_copy_dialog", None)
        if dialog is not None and isValid(dialog) and dialog.isVisible():
            self.reject(); event.ignore(); return
        if self._task is not None:
            self.reject()
            event.ignore()
            return
        if not self._confirm_preference_close():
            event.ignore(); return
        super().closeEvent(event)
