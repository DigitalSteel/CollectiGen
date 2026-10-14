from __future__ import annotations

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialogButtonBox, QDoubleSpinBox, QFormLayout, QHBoxLayout,
    QLabel, QLineEdit, QPushButton, QSpinBox, QSplitter, QTabWidget, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from core.training_adapter_preview import (
    ADAPTER_VERSION, SOURCE_REVISION, SdScriptsSdxlLoraPreviewService, SdxlLoraRecipe,
    verified_adapter_image_path,
)
from .gallery import EmbeddedGallery
from .modal_feedback import FeedbackEnabledDialog
from .training_caption_review_view import PagedCaptionText
from .workers import CoordinatedCallTask


LOCAL_PREPARATION_REQUIREMENTS = {
    "base_compatibility_unverified": (
        "Base model compatibility is unverified",
        "Prepare Local Training will recheck the exact selected base architecture, tensor layout, and loader against the installed qualification before a run can be reviewed."),
    "trainer_runtime_unverified": (
        "Trainer installation needs a readiness check",
        "Prepare Local Training will inventory the selected source, environment, and Python base, then require a stopped native readiness check before a run can be reviewed."),
    "tokenizers_unverified": (
        "Caption token handling needs verification",
        "Prepare Local Training will select and verify both original SDXL tokenizers and bind their measured identities before a run can be reviewed."),
    "geometry_unverified": (
        "Package image effects need runtime verification",
        "Prepare Local Training will require the qualified package geometry and runtime dataset checks. Image-specific effect holds still require their listed review."),
    "dataset_schema_unverified": (
        "Dataset configuration needs verification",
        "Prepare Local Training will check the exact package configuration through the installed qualified runtime and bind the result before a run can be reviewed."),
    "offline_components_unverified": (
        "Local model components need selection checks",
        "Prepare Local Training will verify the selected base and both tokenizer directories against the installed profile before a run can be reviewed."),
    "output_process_unqualified": (
        "Managed training and recovery need preparation",
        "Prepare Local Training will reserve an output workspace and prepare an exact launch, checkpoint, and recovery review. It does not start training."),
}


def _preparation_requirement(requirement):
    """Presentation-only next step; retain the preview's unverified facts."""
    projected = LOCAL_PREPARATION_REQUIREMENTS.get(requirement.code)
    if projected is None:
        return requirement
    return type(requirement)(requirement.code, projected[0], requirement.reason, projected[1])


class TrainingAdapterPreviewDialog(FeedbackEnabledDialog):
    """Review one verified package and a constrained recipe without launching."""
    feedback_workflow = "Trainer settings preview"
    PAGE_SIZE = 100

    def __init__(self, package, registry, jobs, parent=None, *, service=None, availability=None):
        super().__init__(parent)
        self.package, self.registry, self.jobs = package, registry, jobs
        self.service = service or SdScriptsSdxlLoraPreviewService()
        self.availability = availability
        self.preview = None
        self.action = ""
        self.selected_recipe = None
        self._local_training_available = False
        self._requirements = ()
        self._task = None
        self._closing = False
        self._page = 0
        self._filtered = ()
        self._image_search = ()
        self._image_task = None
        self._image_selection = None
        self.setWindowTitle("Review Trainer Settings")
        self.resize(1080, 790)
        layout = QVBoxLayout(self)
        self.goal = QLabel("Review this training goal")
        self.goal.setObjectName("sectionTitle")
        self.goal.setWordWrap(True); self.goal.setTextFormat(Qt.PlainText)
        layout.addWidget(self.goal)
        self.identity = QLabel()
        self.identity.setWordWrap(True); self.identity.setTextFormat(Qt.PlainText)
        self.identity.setTextInteractionFlags(Qt.TextSelectableByMouse | Qt.TextSelectableByKeyboard)
        layout.addWidget(self.identity)
        self.summary = QLabel("Checking the package and local model…")
        self.summary.setWordWrap(True); self.summary.setTextFormat(Qt.PlainText)
        layout.addWidget(self.summary)
        intro = QLabel("Read-only preview: package files and Image Knowledge stay unchanged; no training begins here.")
        intro.setWordWrap(True)
        layout.addWidget(intro)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        overview = QWidget()
        overview_layout = QVBoxLayout(overview)
        self.overview_next = QLabel()
        self.overview_next.setWordWrap(True); self.overview_next.setTextFormat(Qt.PlainText)
        overview_layout.addWidget(self.overview_next)
        self.attention = QLabel()
        self.attention.setWordWrap(True); self.attention.setTextFormat(Qt.PlainText)
        overview_layout.addWidget(self.attention)
        self.availability_note = QLabel()
        self.availability_note.setWordWrap(True); self.availability_note.setTextFormat(Qt.PlainText)
        overview_layout.addWidget(self.availability_note)
        self.review_images_button = QPushButton("Review Images & Captions…")
        self.review_images_button.clicked.connect(self._review_images)
        overview_layout.addWidget(self.review_images_button, 0, Qt.AlignLeft)
        self.all_checks_button = QPushButton("Show All Readiness Checks")
        self.all_checks_button.setCheckable(True)
        self.all_checks_button.toggled.connect(self._toggle_readiness_details)
        overview_layout.addWidget(self.all_checks_button, 0, Qt.AlignLeft)
        self.readiness_details = QWidget()
        details_layout = QVBoxLayout(self.readiness_details)
        details_layout.setContentsMargins(0, 0, 0, 0)
        self.requirements = QTreeWidget()
        self.requirements.setHeaderLabels(["Needs Verification", "Reason", "Next Action"])
        self.requirements.setRootIsDecorated(False)
        self.requirements.setWordWrap(True)
        self.requirements.setColumnWidth(0, 230)
        self.requirements.setColumnWidth(1, 355)
        self.requirements.setAccessibleName("Trainer readiness and next actions")
        self.requirements.itemSelectionChanged.connect(self._select_requirement)
        details_layout.addWidget(self.requirements, 1)
        self.requirement_detail = PagedCaptionText("Selected readiness requirement")
        details_layout.addWidget(self.requirement_detail, 1)
        overview_layout.addWidget(self.readiness_details, 1)
        self.readiness_details.hide()
        self.overview_fill = QWidget()
        overview_layout.addWidget(self.overview_fill, 1)
        self.tabs.addTab(overview, "Overview")

        recipe_tab = QWidget()
        recipe_layout = QVBoxLayout(recipe_tab)
        note = QLabel("Starting proposal — unmeasured. These choices are separate from the immutable package's settings. Changing them clears this preview until Update Preview rechecks the package and base model.")
        note.setWordWrap(True)
        recipe_layout.addWidget(note)
        form = QFormLayout()
        self.rank = QSpinBox(); self.rank.setRange(4, 128); self.rank.setValue(32)
        self.alpha = QDoubleSpinBox(); self.alpha.setRange(1, 128); self.alpha.setValue(16)
        self.output_precision = QComboBox()
        for label, value in (("FP16 (smaller saved weights)", "fp16"), ("BF16", "bf16"), ("Float32 (larger saved weights)", "float")):
            self.output_precision.addItem(label, value)
        self.gradient_checkpointing = QCheckBox("Recompute intermediate activations; memory/time tradeoff unmeasured")
        self.gradient_checkpointing.setChecked(True)
        form.addRow("LoRA rank", self.rank)
        form.addRow("LoRA alpha (1 through rank)", self.alpha)
        form.addRow("Saved weight precision", self.output_precision)
        form.addRow("Gradient checkpointing", self.gradient_checkpointing)
        recipe_layout.addLayout(form)
        self.recipe_note = QLabel("The first proposal uses ordinary UNet-only LoRA, AdamW, a constant learning rate schedule, full-precision VAE, no latent cache and zero data-loader subprocesses. Its configuration text is a preliminary final-weight proposal without checkpoint or resume settings. When available, Prepare Local Training creates a separate checkpoint and recovery proposal for explicit review; it does not start training. These choices do not establish hardware fit or output quality.")
        self.recipe_note.setWordWrap(True)
        recipe_layout.addWidget(self.recipe_note)
        recipe_layout.addStretch(1)
        self.tabs.addTab(recipe_tab, "Recipe")

        data_tab = QWidget(); data_layout = QVBoxLayout(data_tab)
        image_scope = QHBoxLayout()
        self.search = QLineEdit(); self.search.setPlaceholderText("Search image IDs, roles, paths or effect details")
        self.search.setAccessibleName("Search trainer preview images")
        self.search.textChanged.connect(self._filter_images)
        image_scope.addWidget(self.search, 1)
        self.effect_filter = QComboBox()
        self.effect_filter.setAccessibleName("Filter image effect assessments")
        for label, value in (("All images", "all"), ("Needs image review", "held"),
                ("Reference match only", "reference_match"), ("Outside training", "not_training")):
            self.effect_filter.addItem(label, value)
        self.effect_filter.currentIndexChanged.connect(self._filter_images)
        image_scope.addWidget(self.effect_filter)
        data_layout.addLayout(image_scope)
        self.count = QLabel(); data_layout.addWidget(self.count)
        image_split = QSplitter(Qt.Horizontal)
        image_details = QWidget(); image_details_layout = QVBoxLayout(image_details)
        image_details_layout.setContentsMargins(0, 0, 0, 0)
        self.images = QTreeWidget()
        self.images.setHeaderLabels(["Package Image", "Role", "Dimensions", "Exposures", "Image Effects"])
        self.images.setRootIsDecorated(False)
        self.images.setColumnWidth(0, 245); self.images.setColumnWidth(1, 82)
        self.images.setColumnWidth(2, 102); self.images.setColumnWidth(3, 83)
        self.images.setColumnWidth(4, 190)
        self.images.itemSelectionChanged.connect(self._select_image)
        image_details_layout.addWidget(self.images, 1)
        pagination = QHBoxLayout()
        self.previous = QPushButton("Previous"); self.next = QPushButton("Next")
        self.previous.clicked.connect(lambda: self._change_page(-1))
        self.next.clicked.connect(lambda: self._change_page(1))
        pagination.addWidget(self.previous); pagination.addWidget(self.next); pagination.addStretch(1)
        image_details_layout.addLayout(pagination)
        self.caption = PagedCaptionText("Exact training caption, image effects and identity")
        image_details_layout.addWidget(self.caption, 1)
        image_split.addWidget(image_details)
        self.image_preview = EmbeddedGallery(page_size=1, card_width=180, show_open_dataset=False)
        self.image_preview.setAccessibleName("Selected verified packaged training image")
        # The one-image gallery already shows its loading, empty and error text
        # in the card area; the adjacent count would truncate this task title.
        self.image_preview.status.hide()
        self.image_preview.set_unavailable("Choose a packaged image to review it beside its exact caption.",
                                           "Packaged training image")
        self.image_preview.setMinimumWidth(220)
        self.image_preview.setMaximumWidth(285)
        image_split.addWidget(self.image_preview)
        image_split.setStretchFactor(0, 3)
        image_split.setStretchFactor(1, 1)
        data_layout.addWidget(image_split, 1)
        correction = QLabel("Need a correction? Return to training-data review to change Image Knowledge or caption choices, then rebuild this frozen package. Nothing changes here.")
        correction.setWordWrap(True); correction.setTextFormat(Qt.PlainText)
        data_layout.addWidget(correction)
        self.tabs.addTab(data_tab, "Images && Captions")

        settings_tab = QWidget(); settings_layout = QVBoxLayout(settings_tab)
        self.setting_search = QLineEdit(); self.setting_search.setPlaceholderText("Search requested or proposed settings")
        self.setting_search.setAccessibleName("Search trainer setting dispositions")
        self.setting_search.textChanged.connect(self._filter_settings)
        settings_layout.addWidget(self.setting_search)
        self.settings = QTreeWidget()
        self.settings.setHeaderLabels(["Setting", "Treatment", "Requested", "Proposed", "Explanation"])
        self.settings.setColumnWidth(0, 270); self.settings.setColumnWidth(1, 140)
        self.settings.setColumnWidth(2, 160); self.settings.setColumnWidth(3, 220)
        self.settings.setRootIsDecorated(False)
        self.settings.setAccessibleName("Every neutral setting and its trainer treatment")
        settings_layout.addWidget(self.settings, 1)
        self.setting_detail = PagedCaptionText("Selected setting details")
        settings_layout.addWidget(self.setting_detail, 1)
        self.settings.itemSelectionChanged.connect(self._select_setting)
        self.tabs.addTab(settings_tab, "Settings && Differences")

        proposal_tab = QWidget(); proposal_layout = QVBoxLayout(proposal_tab)
        proposal_note = QLabel("Private, incomplete review text. It includes local paths and has no reserved output, tokenizer mapping or launch command. No trainer configuration file has been written.")
        proposal_note.setWordWrap(True); proposal_layout.addWidget(proposal_note)
        self.proposal_text = PagedCaptionText("Private trainer configuration proposal")
        proposal_layout.addWidget(self.proposal_text, 1)
        self.tabs.addTab(proposal_tab, "Configuration Detail")
        self.status = QLabel(); self.status.setWordWrap(True); self.status.setTextFormat(Qt.PlainText)
        layout.addWidget(self.status)
        from .components import bind_status_footer
        bind_status_footer(self,self.status,title="Trainer preview")
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.update_button = QPushButton("Update Preview")
        self.update_button.clicked.connect(self._refresh)
        buttons.addButton(self.update_button, QDialogButtonBox.ActionRole)
        self.local_training_button = QPushButton("Prepare Local Training")
        self.local_training_button.setProperty("primary", True)
        self.local_training_button.clicked.connect(self._prepare_local_training)
        buttons.addButton(self.local_training_button, QDialogButtonBox.ActionRole)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self.rank.valueChanged.connect(self._changed)
        self.alpha.valueChanged.connect(self._changed)
        self.output_precision.currentIndexChanged.connect(self._changed)
        self.gradient_checkpointing.toggled.connect(self._changed)
        self._set_busy(False)
        QTimer.singleShot(0, self._refresh)

    def _clear(self):
        self._clear_image_preview()
        self.preview = None
        self.action = ""
        self.selected_recipe = None
        self._local_training_available = False
        self._requirements = ()
        self._filtered = ()
        self._image_search = ()
        for tree in (self.images, self.requirements, self.settings):
            tree.clear()
        for text in (self.caption, self.setting_detail, self.proposal_text, self.requirement_detail):
            text.set_text("")
        self.identity.clear(); self.count.clear()
        self.goal.setText("Review this training goal")
        self.goal.setToolTip("")
        self.overview_next.clear(); self.attention.clear(); self.availability_note.clear()
        self.review_images_button.setEnabled(False)
        self.all_checks_button.setChecked(False)
        self.all_checks_button.setEnabled(False)
        self.previous.setEnabled(False); self.next.setEnabled(False)
        self.local_training_button.setEnabled(False)
        self.local_training_button.setToolTip("A current preview and matching installed training qualification are required.")

    def _toggle_readiness_details(self, shown):
        self.readiness_details.setVisible(bool(shown and self.preview is not None))
        self.overview_fill.setVisible(not shown)
        self.all_checks_button.setText(
            "Hide All Readiness Checks" if shown else "Show All Readiness Checks")

    def _review_images(self):
        if self.preview is None:
            return
        held = any(row.code.startswith("image_policy_") for row in self._requirements)
        self.search.clear()
        self.effect_filter.setCurrentIndex(
            self.effect_filter.findData("held" if held else "all"))
        self.tabs.setCurrentIndex(2)
        if self.images.topLevelItemCount():
            self.images.setCurrentItem(self.images.topLevelItem(0))

    def _changed(self, *_):
        self._clear()
        self.summary.setText("Recipe changed — update the preview")
        self.status.setText("The previous proposal is no longer current. Package and base-model bytes will be checked again.")

    def _set_busy(self, busy):
        for widget in (self.rank, self.alpha, self.output_precision, self.gradient_checkpointing, self.update_button):
            widget.setEnabled(not busy)
        self.local_training_button.setEnabled(not busy and self._local_training_available and self.preview is not None)

    def _recipe(self):
        recipe = SdxlLoraRecipe(self.rank.value(), self.alpha.value(), self.output_precision.currentData(), self.gradient_checkpointing.isChecked())
        recipe.validate()
        return recipe

    def _refresh(self):
        if self._task is not None or self._closing:
            return
        self._clear()
        try:
            recipe = self._recipe()
        except ValueError as exc:
            self._failed(str(exc)); return
        self.summary.setText("Checking the package and exact local base model…")
        def work(job):
            def progress(done, total, label):
                # Model hashing reports bytes; the shared Qt signal uses int32.
                job.update(done // 2**20, (total + 2**20 - 1) // 2**20, label)
            preview = self.service.preview(self.package, self.registry, recipe, progress=progress, should_cancel=lambda: job.cancel_requested)
            search = tuple(f"{item.stable_id} {item.role} {item.image_path} {item.effects.search_text if item.effects else ''}".casefold() for item in preview.images)
            if job.cancel_requested:
                raise InterruptedError("Preview cancelled. The package remains unchanged.")
            try:
                if self.availability is None:
                    from core.training_workflow import _assets
                    available = _assets(self.package, recipe)
                else:
                    available = self.availability(self.package, recipe)
                if available is None or available is False:
                    raise ValueError("No matching installed training qualification is available.")
                availability_error = ""
            except InterruptedError:
                raise
            except Exception as exc:
                availability_error = str(exc)[:2000] or "The installed training qualification is unavailable."
            if job.cancel_requested:
                raise InterruptedError("Preview cancelled. The package remains unchanged.")
            return preview, search, recipe, availability_error
        task = CoordinatedCallTask(self.jobs, "Review trainer settings", work, resource="io")
        self._task = task
        task.signals.progress.connect(lambda _done, _total, message: self.status.setText(message))
        task.signals.complete.connect(lambda value: self._finish(task, self._completed, value))
        task.signals.failed.connect(lambda message: self._finish(task, self._failed, message))
        task.signals.cancelled.connect(lambda: self._finish(task, self._failed, "Preview cancelled. The package remains unchanged."))
        self._set_busy(True)
        task.start()

    def _finish(self, task, callback, value):
        if self._task is not task:
            return
        self._task = None
        self._set_busy(False)
        if self._closing:
            super().reject(); return
        callback(value)

    def _completed(self, result):
        preview, self._image_search, recipe, availability_error = result
        try:
            if self._recipe() != recipe or preview.recipe != recipe:
                raise ValueError("Recipe changed while the preview was running. Update the preview again.")
        except ValueError as exc:
            self._failed(str(exc)); return
        self.preview = preview
        self.goal.setText("Training goal: " + preview.goal[:160] + ("…" if len(preview.goal) > 160 else ""))
        self.goal.setToolTip(preview.goal)
        counts = {role: sum(item.role == role for item in preview.images) for role in ("training", "validation", "holdout")}
        base = ("registered base bytes match; compatibility still needs a local check"
                if preview.base_status == "identity_verified" else "exact local base unavailable")
        self._local_training_available = not availability_error
        self.local_training_button.setEnabled(self._local_training_available)
        self.local_training_button.setToolTip(
            "Continue to local software and device checks. This does not start training." if self._local_training_available else
            availability_error)
        self.summary.setText("Training data package: verified. Base model: " + base + ".\n" +
            ("Local preparation: available for checks."
             if self._local_training_available else "Local training: unavailable."))
        self._requirements = tuple(_preparation_requirement(value) for value in preview.requirements) if self._local_training_available else preview.requirements
        for index, requirement in enumerate(self._requirements):
            item = QTreeWidgetItem([requirement.summary, requirement.reason, requirement.next_action])
            item.setData(0, Qt.UserRole, index)
            for column, value in enumerate((requirement.summary, requirement.reason, requirement.next_action)):
                item.setToolTip(column, value)
            self.requirements.addTopLevelItem(item)
        self.identity.setText(f"{counts['training']:,} training · {counts['validation']:,} validation · {counts['holdout']:,} holdout · {len(preview.excluded_ids):,} excluded")
        self.overview_next.setText(
            "Next: review the packaged images and exact captions, then choose Prepare Local Training for a separate readiness and run review."
            if self._local_training_available else
            "Review the packaged images and exact captions. The verified manual package remains usable even though local preparation is unavailable here.")
        user_review = [value.summary for value in self._requirements
                       if value.code not in LOCAL_PREPARATION_REQUIREMENTS]
        self.attention.setText("Needs review or measurement:\n" + "\n".join("• " + value for value in user_review)
                               if user_review else "")
        self.availability_note.setText("Why local preparation is unavailable: " + availability_error
                                       if availability_error else "")
        image_holds = sum(value.code.startswith("image_policy_") for value in self._requirements)
        self.review_images_button.setText(
            "Review Images Needing Attention…" if image_holds else "Review Images & Captions…")
        self.review_images_button.setEnabled(True)
        self.all_checks_button.setEnabled(True)
        self.proposal_text.set_text(f"# Adapter proposal v{ADAPTER_VERSION}\n# sd-scripts source {SOURCE_REVISION}\n# Package: {preview.package_fingerprint}\n# Preview: {preview.fingerprint}\n# Image effect reference: {preview.image_effects_profile_sha256}\n\n"
            + preview.configuration_toml + "\n# Proposed dataset JSON\n" + preview.dataset_json)
        self.requirements.setCurrentItem(self.requirements.topLevelItem(0))
        self._filter_images(); self._filter_settings()
        self.status.setText(
            "Review the training images and captions, then explicitly prepare the local run. No files were changed and no training was started."
            if self._local_training_available else
            f"{availability_error} The manual package remains available; no files were changed and no training was started.")

    def _failed(self, message):
        self._clear()
        self.summary.setText("Trainer preview needs attention")
        self.status.setText(str(message))

    def _prepare_local_training(self):
        if self._task is not None or not self._local_training_available or self.preview is None:
            return
        try:
            if self._recipe() != self.preview.recipe:
                raise ValueError
        except ValueError:
            self._changed(); return
        self.action = "local_training"
        self.selected_recipe = self.preview.recipe
        super().accept()

    def _select_requirement(self):
        item = self.requirements.currentItem()
        if item is None or self.preview is None:
            self.requirement_detail.set_text(""); return
        value = self._requirements[item.data(0, Qt.UserRole)]
        self.requirement_detail.set_text(f"Goal: {self.preview.goal}\n\n{value.summary}\n\n{value.reason}\n\nNext action\n{value.next_action}")

    def _filter_images(self, *_):
        query = self.search.text().strip().casefold()
        scope = self.effect_filter.currentData()
        self._filtered = tuple(i for i, text in enumerate(self._image_search)
            if (not query or query in text) and (scope == "all" or
                (self.preview.images[i].effects is not None and self.preview.images[i].effects.status == scope))) if self.preview else ()
        self._page = 0
        self._render_images()

    def _change_page(self, delta):
        self._page = max(0, min(self._page + delta, max(0, (len(self._filtered) - 1) // self.PAGE_SIZE)))
        self._render_images()

    def _render_images(self):
        self._clear_image_preview()
        self.images.clear(); self.caption.set_text("")
        start = self._page * self.PAGE_SIZE
        for index in self._filtered[start:start + self.PAGE_SIZE]:
            value = self.preview.images[index]
            item = QTreeWidgetItem([value.image_path, value.role, f"{value.width} × {value.height}", str(value.effective_exposures),
                value.effects.summary if value.effects else "Unverified"])
            item.setData(0, Qt.UserRole, index)
            self.images.addTopLevelItem(item)
        self.count.setText(f"{len(self._filtered):,} matching images · Showing {min(start + 1, len(self._filtered)):,}–{min(start + self.PAGE_SIZE, len(self._filtered)):,}")
        self.previous.setEnabled(self._page > 0)
        self.next.setEnabled(start + self.PAGE_SIZE < len(self._filtered))

    def _select_image(self):
        self._clear_image_preview()
        item = self.images.currentItem()
        if item is None or self.preview is None:
            self.caption.set_text(""); return
        index = item.data(0, Qt.UserRole)
        value = self.preview.images[index]
        current_preview = self.preview
        self.caption.set_text("Checking this packaged image and exact caption…")
        self.image_preview.set_loading("Packaged training image")
        self._image_selection = (self.preview, index)
        def work(job):
            return verified_adapter_image_path(self.package, current_preview, value,
                should_cancel=lambda: job.cancel_requested)
        task = CoordinatedCallTask(self.jobs, "Check packaged training image", work, resource="io")
        self._image_task = task
        task.signals.complete.connect(lambda path: self._finish_image(task, path=path))
        task.signals.failed.connect(lambda message: self._finish_image(task, error=message))
        task.signals.cancelled.connect(lambda: self._finish_image(task, error="Image preview cancelled."))
        task.start()

    def _clear_image_preview(self):
        if self._image_task is not None:
            self.jobs.cancel(self._image_task.job_id)
            self._image_task = None
        self._image_selection = None
        self.image_preview.set_unavailable("Choose a packaged image to review it beside its exact caption.",
                                           "Packaged training image")

    def _finish_image(self, task, *, path=None, error=None):
        if self._image_task is not task:
            return
        self._image_task = None
        selection = self._image_selection
        if self._closing or selection is None or selection[0] is not self.preview:
            return
        item = self.images.currentItem()
        if item is None or item.data(0, Qt.UserRole) != selection[1]:
            return
        if error:
            self.caption.set_text("This packaged image and caption could not be rechecked. Verify or rebuild the training package before reviewing them.")
            self.image_preview.set_unavailable(
                f"Image unavailable: {error} Recheck or rebuild the training package.",
                "Packaged training image")
            return
        value = self.preview.images[selection[1]]
        assessment = value.effects.detail() if value.effects else "Image effects remain unverified."
        self.caption.set_text(f"Exact package caption (no additional prefix):\n{value.caption}\n\nImage effects and readiness:\n{assessment}\n\nTechnical identity:\n{value.stable_id}\nImage SHA-256: {value.image_sha256}\nCaption SHA-256: {value.caption_sha256}\nRole: {value.role}")
        def configure(_path, card):
            card.set_compact_mode(True)
            card.allow_data_edit = False
            card.set_note("Read-only packaged image")
        self.image_preview.set_paths((path,), context="Packaged training image", configure=configure)

    def _filter_settings(self, *_):
        self.settings.clear(); self.setting_detail.set_text("")
        query = self.setting_search.text().strip().casefold()
        for index, value in enumerate(self.preview.settings if self.preview else ()):
            row = [value.field, value.disposition, value.requested, value.proposed, value.reason]
            if query and query not in " ".join(row).casefold():
                continue
            item = QTreeWidgetItem([text[:200] for text in row])
            item.setData(0, Qt.UserRole, index)
            self.settings.addTopLevelItem(item)

    def _select_setting(self):
        item = self.settings.currentItem()
        if item is None or self.preview is None:
            self.setting_detail.set_text(""); return
        value = self.preview.settings[item.data(0, Qt.UserRole)]
        self.setting_detail.set_text(f"{value.field}\nTreatment: {value.disposition}\nRequested: {value.requested}\nProposed: {value.proposed}\n\n{value.reason}")

    def reject(self):
        self._closing = True
        self._clear_image_preview()
        if self._task is not None:
            self.jobs.cancel(self._task.job_id)
            self.status.setText("Stopping the read-only preview at a safe boundary…")
            return
        super().reject()

    def closeEvent(self, event):
        if self._task is not None:
            event.ignore(); self.reject()
        else:
            self._closing = True
            self._clear_image_preview()
            super().closeEvent(event)
