"""One callback-backed trainer preparation chooser within canonical Setup."""
from __future__ import annotations

from dataclasses import dataclass
from itertools import islice
import math
from pathlib import Path
import time

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (
    QComboBox, QDialogButtonBox, QFileDialog, QGroupBox, QHBoxLayout,
    QLabel, QListWidget, QPlainTextEdit, QProgressBar, QPushButton, QScrollArea, QTabWidget,
    QSizePolicy, QStackedWidget, QVBoxLayout, QWidget,
)

from core.trainer_environment import TrainerEnvironmentStatus, TrainerGeneration, TrainerHealth
from core.trainer_environment_pack import TrainerEnvironmentPack
from .components import bind_status_footer
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask


@dataclass(frozen=True)
class TrainingSetupOverview:
    versions: str = "No reviewed trainer profile is available."
    costs: str = "Download and storage costs are unavailable until a reviewed plan is supplied."
    sources: str = "Source and license evidence is unavailable."
    details: str = ""
    file_count: int = 0
    license_details: str = ""


@dataclass(frozen=True)
class TrainingSetupPreview:
    action: str
    title: str
    summary: str
    details: str
    confirmation_label: str
    payload: object = None
    can_apply: bool = False
    profile_sha256: str = ""


def _size(value):
    return f"{value / 1024**3:.2f} GiB" if value >= 1024**3 else f"{value / 1024**2:.1f} MiB"


def profile_overview(profile):
    """Memory-only projection called in a coordinated worker, never while painting."""
    if profile is None:
        return TrainingSetupOverview()
    value = profile.payload()
    python, trainer, backend = (value[name] for name in ('python', 'trainer', 'backend'))
    files = value['files']
    licenses = sorted({row['license'] for row in files})
    # License fields may be complete notices, not short SPDX identifiers.
    # Keep all notices in the scrollable evidence viewer, never a layout label.
    notices = '\n\n'.join(licenses)
    return TrainingSetupOverview(
        f"Python {python['version']} · {trainer['name']} {trainer['revision'][:12]} · {backend['name']} {backend['version']} ({backend['platform']})",
        f"Installed software: {_size(sum(row['size_bytes'] for row in files))}. Download size and temporary storage require a reviewed acquisition plan.",
        f"Sources and {len(licenses):,} distinct dependency license notices are available in Software and license details.",
        f"Profile: {value['profile_id']}\nProfile SHA-256: {profile.sha256}\nPython source: {python['source_url']}\nPython source SHA-256: {python['source_sha256']}\nTrainer revision: {trainer['revision']}\nTrainer source: {trainer['source_url']}\nTrainer source SHA-256: {trainer['source_sha256']}\nStandalone executable: {python['executable']}",
        len(files),
        f"Python license:\n{python['license']}\n\nTrainer license:\n{trainer['license']}\n\nDependency licenses and notices:\n{notices}")


class TrainerSetupServiceController:
    """Optional direct adapter; canonical Setup may supply its own queue callbacks.

    A managed acquisition path exists only when both reviewed callbacks are
    supplied. Every method is worker-only and accepts cooperative cancellation.
    """
    def __init__(self, service=None, *, managed_prepare=None, managed_apply=None):
        self.service = service
        self.managed_prepare, self.managed_apply = managed_prepare, managed_apply

    def supports(self, action):
        if action == 'managed':
            return self.service is not None and callable(self.managed_prepare) and callable(self.managed_apply)
        return self.service is not None and action in {'existing', 'import', 'export', 'repair', 'rollback', 'activate'}

    def prepare(self, action, selection, *, should_cancel, progress):
        if not self.supports(action):
            raise ValueError('This preparation option has no reviewed controller.')
        if should_cancel():
            raise InterruptedError('Trainer preparation cancelled.')
        if action == 'managed':
            return self.managed_prepare(action, selection, should_cancel=should_cancel, progress=progress)
        profile = self.service.profile
        details = profile_overview(profile).details
        labels = {'existing': ('Review Existing Installation', 'Verify Installation'),
            'import': ('Review Offline Pack', 'Import Verified Pack'),
            'export': ('Review Pack Export', 'Export Software Pack'),
            'repair': ('Review Environment Repair', 'Create Repair Generation'),
            'rollback': ('Review Rollback', 'Activate Earlier Generation'),
            'activate': ('Review Environment Activation', 'Activate This Generation')}
        title, label = labels[action]
        if action == 'existing':
            root = Path(selection)
            values = profile.payload()
            trainers = tuple(dict.fromkeys(str(root / Path(row['path']).parent) for row in values['files'] if row['role'] == 'trainer'))
            details = (f"Selected software root: {root}\nStandalone interpreter: {root / values['python']['executable']}\n"
                f"Trainer source locations: {'; '.join(trainers[:10])}\nDependencies: exact profile inventory under {root}\n\n" + details)
            summary = f'Check the software at {root}. This does not establish recipe, model, or device compatibility.'
        elif action == 'import':
            info = TrainerEnvironmentPack(self.service).inspect(selection, cancel=should_cancel)
            summary = f"Import {info.file_count:,} verified software files ({_size(info.byte_count)}) from {selection} into a new generation. Existing generations remain available."
            details = f"Selected pack: {selection}\n\n" + details
        elif action == 'repair':
            summary = f'Create a new verified generation from {selection}. Current generations remain available; activation is a separate choice.'
            details = f"Selected software source: {selection}\n\n" + details
        else:
            generation_id = selection[0] if action == 'export' else selection
            generation = self.service.verify_generation(generation_id, cancel=should_cancel)
            details = f"Generation: {generation.generation_id}\nSoftware location: {generation.software_root}\n\n" + details
            if action == 'export':
                summary = f"Export software only to {selection[1]}. The pack contains no models or datasets and will not overwrite an existing file."
            else:
                summary = f'Check and activate the trainer at {generation.software_root}. Other generations remain available. Training compatibility remains separate.'
        if action in {'import', 'repair'}:
            summary += f'\nManaged storage: {self.service.root}'
        return TrainingSetupPreview(action, title, summary, details, label, selection, True, profile.sha256)

    def apply(self, preview, *, should_cancel, progress):
        if not self.supports(preview.action) or preview.profile_sha256 != self.service.profile.sha256:
            raise ValueError('The reviewed trainer profile changed. Prepare a new preview.')
        if should_cancel():
            raise InterruptedError('Trainer preparation cancelled.')
        action, selection = preview.action, preview.payload
        if action == 'managed':
            return self.managed_apply(preview, should_cancel=should_cancel, progress=progress)
        if action == 'existing':
            return self.service.inspect_existing(selection, cancel=should_cancel)
        if action == 'import':
            return TrainerEnvironmentPack(self.service).import_pack(selection, cancel=should_cancel, activate=False)
        if action == 'export':
            return TrainerEnvironmentPack(self.service).export(selection[0], selection[1], cancel=should_cancel)
        if action == 'repair':
            return self.service.repair(selection, cancel=should_cancel, activate=False)
        return getattr(self.service, action)(selection, cancel=should_cancel)


class TrainingSetupDialog(FeedbackEnabledDialog):
    preparationSelected = Signal(object)
    operationFinished = Signal(str, object)
    feedback_workflow = 'Trainer setup'
    GENERATION_PAGE_SIZE = 100
    DETAIL_PAGE_SIZE = 64000

    def __init__(self, profile, jobs, *, goal_summary, machine_fit_summary, controller=None,
                 overview=None, held_reason='', generations=(), parent=None):
        super().__init__(parent)
        self.profile, self.jobs, self.controller = profile, jobs, controller
        self._task = self._preview = self._candidate = None
        self._queued = None
        self._queue_cancel_requested = False
        self._cancel_requested = False
        self._candidate_reference = None
        self._queue_timer = QTimer(self)
        self._queue_timer.setInterval(500)
        self._queue_timer.timeout.connect(self._poll_queued)
        self._closing = False
        self._phase = ''
        self._started = 0.0
        self._profile_sha256 = ''
        self._review_active = False
        self._preparation_review = None
        self.setWindowTitle('Set Up Training')
        self.resize(940, 760)
        layout = QVBoxLayout(self)
        self.heading = self._label('Set up training on this computer', 'sectionTitle')
        layout.addWidget(self.heading)
        layout.addWidget(self._label(str(goal_summary)[:2000]))
        self.machine_fit_summary = str(machine_fit_summary)[:2000]
        default_reason = ('A qualified trainer profile is not available. Continue with a manual training package.' if profile is None else
            'Managed installation needs a reviewed acquisition plan. Choose an existing installation or continue with manual export.' if not self._supported('managed') else
            'Review the recommended software plan before anything is installed.')
        self.held = self._label(held_reason or default_reason)
        layout.addWidget(self.held)
        self.tabs = QTabWidget()
        layout.addWidget(self.tabs, 1)
        guided = QWidget(); guided_layout = QVBoxLayout(guided)
        self.steps = self._label('1. Review plan   →   2. Install and verify   →   3. Select trainer')
        guided_layout.addWidget(self.steps)
        self.guided_pages = QStackedWidget()
        guided_layout.addWidget(self.guided_pages, 1)
        welcome = QWidget(); welcome_layout = QVBoxLayout(welcome)
        self.recommendation = self._label('Recommended setup', 'sectionTitle')
        welcome_layout.addWidget(self.recommendation)
        self.welcome_summary = self._label('Let CollectiGen prepare the trainer software for you. First, review what it will install, where it will go, and the download and storage requirements.')
        welcome_layout.addWidget(self.welcome_summary)
        self.managed_button = self._button('Set Up Trainer…', lambda: self.prepare_action('managed', None), welcome_layout)
        self.managed_button.setProperty('primary', True)
        self.managed_button.setMinimumHeight(44)
        self.managed_note = self._label('Installation checks the trainer and GPU. You will choose the verified trainer before checking your model and training recipe.')
        welcome_layout.addWidget(self.managed_note)
        self.advanced_button = self._button('Other Setup Options…', self._show_existing_options, welcome_layout)
        self.return_button = self._button('Return to current review', self._show_review, welcome_layout)
        self.return_button.hide()
        welcome_layout.addStretch(1)
        self.guided_pages.addWidget(welcome)
        choices = QWidget(); choices_layout = QVBoxLayout(choices)
        self.versions = self._label('Reading reviewed profile details…' if profile else 'No reviewed trainer profile is available.')
        self.costs = self._label('')
        self.preview_title = self._label('Choose a preparation option.', 'sectionTitle')
        self.preview_summary = self._label('Every installation, import, repair, and activation requires a concrete review here.')
        choices_layout.addWidget(self.preview_title)
        choices_layout.addWidget(self.preview_summary)
        self.plan_details_button = self._button('Show exact plan details', self._toggle_plan_details, choices_layout)
        self.preview_detail = QPlainTextEdit()
        self.preview_detail.setReadOnly(True)
        self.preview_detail.setPlaceholderText('Choose an option to review its exact locations, software, and next action here.')
        self.preview_detail.setAccessibleName('Exact preparation locations and consequences')
        self.preview_detail.setMinimumHeight(100)
        choices_layout.addWidget(self.preview_detail, 1)
        self.preview_detail.hide()
        choices_layout.addStretch(1)
        self.back_button = self._button('Back to setup options', self._show_options, choices_layout)
        self.open_recovery_button = self._button('Open Recovery', self._show_recovery, choices_layout)
        self.open_recovery_button.hide()
        scroll = QScrollArea(); scroll.setWidgetResizable(True); scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(choices)
        self.guided_pages.addWidget(scroll)
        self.tabs.addTab(guided, 'Guided setup')
        advanced = QWidget(); advanced_layout = QVBoxLayout(advanced)
        advanced_layout.addWidget(self._label('Choose one task. Reviewing software or a retained generation does not change the selected trainer.'))
        task_layout = QHBoxLayout(); advanced_layout.addLayout(task_layout, 1)
        self.advanced_tasks = QListWidget()
        self.advanced_tasks.setAccessibleName('Advanced trainer setup tasks')
        self.advanced_tasks.setFixedWidth(180)
        for title in ('Existing Software', 'Offline Transfer', 'Recovery', 'Software Details'):
            self.advanced_tasks.addItem(title)
        task_layout.addWidget(self.advanced_tasks)
        task_content = QWidget(); task_content_layout = QVBoxLayout(task_content)
        task_layout.addWidget(task_content, 1)
        self.advanced_task_intro = self._label('')
        task_content_layout.addWidget(self.advanced_task_intro)
        self.advanced_stack = QStackedWidget()
        existing_page = QWidget(); existing_layout = QVBoxLayout(existing_page)
        options = QHBoxLayout()
        self.existing_button = self._choice(options, 'Use existing software', 'Select your trainer folder, review its locations, then check compatibility.', 'Choose Installation…', self._choose_existing)
        self.manual_button = self._choice(options, 'Set up separately', 'Use the training package with a trainer you configure yourself.', 'Continue With Manual Export', self._manual)
        existing_layout.addLayout(options); existing_layout.addStretch(1)
        self.advanced_stack.addWidget(existing_page)
        offline_page = QWidget(); offline_layout = QVBoxLayout(offline_page)
        self.import_button = self._button('Import Offline Pack…', self._choose_import, offline_layout)
        offline_layout.addWidget(self._label('Import checks the chosen pack before a trainer environment can be selected.'))
        offline_layout.addStretch(1)
        self.advanced_stack.addWidget(offline_page)
        recovery_page = QWidget(); recovery_layout = QVBoxLayout(recovery_page)
        self.repair_button = self._button('Repair From Software…', self._choose_repair, recovery_layout)
        recovery_layout.addWidget(self._label('Neither action changes the active trainer until its reviewed plan is confirmed.'))
        recovery_layout.addStretch(1)
        self.advanced_stack.addWidget(recovery_page)
        details_page = QWidget(); details_layout = QVBoxLayout(details_page)
        details_layout.addWidget(self.versions)
        details_layout.addWidget(self.costs)
        self.inventory_count = self._label('')
        details_layout.addWidget(self.inventory_count)
        self.profile_detail = QPlainTextEdit()
        self.profile_detail.setReadOnly(True)
        self.profile_detail.setAccessibleName('Reviewed versions, source hashes, and licenses')
        details_layout.addWidget(self.profile_detail, 1)
        self._profile_detail_text = ''
        self._profile_detail_page = 0
        self.detail_paging = QWidget()
        paging = QHBoxLayout(self.detail_paging); paging.setContentsMargins(0, 0, 0, 0)
        self.previous_details = self._button('Previous details page', lambda: self._page_details(-1), paging)
        self.detail_page_label = self._label('')
        paging.addWidget(self.detail_page_label)
        self.next_details = self._button('Next details page', lambda: self._page_details(1), paging)
        details_layout.addWidget(self.detail_paging)
        self.detail_paging.hide()
        self.advanced_stack.addWidget(details_page)
        self.generation_panel = QWidget(); generation_layout = QVBoxLayout(self.generation_panel)
        generation_layout.setContentsMargins(0, 0, 0, 0)
        self.generation_picker = QComboBox()
        self.generation_picker.setAccessibleName('Retained trainer generation')
        self.generation_picker.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.generation_picker.setMinimumContentsLength(20)
        self.generation_picker.setSizePolicy(QSizePolicy.Expanding, QSizePolicy.Fixed)
        self._generations = generations
        self._generation_total = len(generations)
        self._generation_page = 0
        self._recent_generation = None
        self.generation_page_label = self._label('')
        generation_layout.addWidget(self.generation_page_label)
        generation_layout.addWidget(self.generation_picker)
        generation_paging = QHBoxLayout()
        self.previous_generations = self._button(
            'Previous Generations', lambda: self._page_generations(-1), generation_paging)
        self.next_generations = self._button(
            'Next Generations', lambda: self._page_generations(1), generation_paging)
        generation_paging.addStretch(1)
        generation_layout.addLayout(generation_paging)
        self.previous_generations.setVisible(self._generation_total > self.GENERATION_PAGE_SIZE)
        self.next_generations.setVisible(self._generation_total > self.GENERATION_PAGE_SIZE)
        actions = QHBoxLayout()
        self.export_button = self._button('Export Software Pack…', self._choose_export, actions)
        self.rollback_button = self._button('Review Rollback', lambda: self.prepare_action('rollback', self.generation_picker.currentData()), actions)
        generation_layout.addLayout(actions)
        task_content_layout.addWidget(self.generation_panel)
        task_content_layout.addWidget(self.advanced_stack, 1)
        self.advanced_tasks.currentRowChanged.connect(self._select_advanced_task)
        self.advanced_tasks.setCurrentRow(0)
        self.tabs.addTab(advanced, 'Advanced setup')
        self.details_button = self._button('Software and License Details…', self._show_software_details, guided_layout)
        self.status = self._label('Choose a preparation option.')
        self.progress = QProgressBar(); self.progress.setRange(0, 0); self.progress.hide()
        self.cancel_button = QPushButton('Cancel Operation'); self.cancel_button.clicked.connect(self._cancel)
        self.cancel_button.hide()
        layout.addWidget(self.status); layout.addWidget(self.progress); layout.addWidget(self.cancel_button)
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        self.return_preparation_button = QPushButton('Return to Training Preparation')
        self.return_preparation_button.clicked.connect(self._return_to_preparation)
        buttons.addButton(self.return_preparation_button, QDialogButtonBox.ActionRole)
        self.return_preparation_button.hide()
        self.confirm_button = QPushButton('Review a setup plan first')
        self.confirm_button.setProperty('primary', True)
        self.confirm_button.clicked.connect(self._confirm)
        buttons.addButton(self.confirm_button, QDialogButtonBox.ActionRole)
        self.use_button = QPushButton('Use Verified Environment')
        self.use_button.setProperty('primary', True)
        self.use_button.clicked.connect(self._use_candidate)
        buttons.addButton(self.use_button, QDialogButtonBox.ActionRole)
        self.use_button.hide()
        layout.addWidget(buttons)
        bind_status_footer(self, self.status, self.progress, self.cancel_button, title='Trainer preparation')
        self._timer = QTimer(self); self._timer.setInterval(1000); self._timer.timeout.connect(self._timing)
        for button in self.findChildren(QPushButton): button.setAutoDefault(False)
        self.generation_picker.currentIndexChanged.connect(self._update_actions)
        self.tabs.currentChanged.connect(self._update_actions)
        self._show_generation_page(0)
        if overview is not None:
            self._show_overview(overview)
        self._update_actions()
        if profile is not None:
            QTimer.singleShot(0, lambda: self._load_overview(overview))

    def set_return_to_preparation(self, review):
        """Expose an explicit return without adopting settings or starting work."""
        import weakref
        self._preparation_review = weakref.ref(review)
        self._update_actions()

    def _return_to_preparation(self, *, setup_saved=False, setup_cancelled=False):
        from shiboken6 import isValid
        review = self._preparation_review() if self._preparation_review is not None else None
        if review is None or not isValid(review) or not review.isVisible():
            self._preparation_review = None
            self._update_actions()
            return
        if setup_saved:
            review.trainer_setup_saved()
        elif setup_cancelled:
            review.trainer_setup_cancelled()
        review.raise_()
        review.activateWindow()

    @staticmethod
    def _label(text, object_name=''):
        label = QLabel(text); label.setWordWrap(True); label.setTextFormat(Qt.PlainText)
        if object_name: label.setObjectName(object_name)
        return label

    @staticmethod
    def _button(text, callback, layout):
        button = QPushButton(text); button.clicked.connect(callback); layout.addWidget(button)
        return button

    def _choice(self, layout, title, text, button_label, callback):
        group = QGroupBox(title); inner = QVBoxLayout(group)
        inner.addWidget(self._label(text)); inner.addStretch(1)
        button = self._button(button_label, callback, inner)
        layout.addWidget(group, 1)
        return button

    def _supported(self, action):
        try:
            return self.controller is not None and callable(getattr(self.controller, 'supports', None)) and self.controller.supports(action) is True
        except Exception:
            return False

    def _show_review(self):
        self.guided_pages.setCurrentIndex(1)
        self.tabs.setCurrentIndex(0)
        self._update_actions()

    def _show_options(self):
        # Navigation alone never discards a reviewed plan or verified result.
        self.guided_pages.setCurrentIndex(0)
        self._update_actions()

    def _page_generations(self, offset):
        if self._task is not None or self._queued is not None or self._closing:
            return
        self._show_generation_page(self._generation_page + offset)

    def _show_generation_page(self, page, *, selected_id=None):
        pages = max(1, math.ceil(self._generation_total / self.GENERATION_PAGE_SIZE))
        self._generation_page = max(0, min(pages - 1, page))
        first = self._generation_page * self.GENERATION_PAGE_SIZE
        last = min(self._generation_total, first + self.GENERATION_PAGE_SIZE)
        self.generation_picker.blockSignals(True)
        self.generation_picker.clear()
        for generation_id, label in islice(self._generations, first, last):
            self.generation_picker.addItem(str(label)[:180], str(generation_id))
        recent = self._recent_generation
        if recent is not None and self.generation_picker.findData(recent[0]) < 0:
            self.generation_picker.addItem('Just verified: ' + recent[1][:160], recent[0])
        if selected_id is not None:
            index = self.generation_picker.findData(selected_id)
            if index >= 0:
                self.generation_picker.setCurrentIndex(index)
        self.generation_picker.blockSignals(False)
        self.generation_page_label.setText(
            f'{self._generation_total:,} supplied retained generations · '
            + (f'Showing {first + 1:,}–{last:,}.' if last > first else 'None to select.')
            + (' Newly verified result also available.' if recent is not None else '')
        )
        self.previous_generations.setEnabled(self._generation_page > 0)
        self.next_generations.setEnabled(self._generation_page + 1 < pages)
        self._update_actions()

    def _select_advanced_task(self, index):
        if index < 0:
            return
        self.advanced_stack.setCurrentIndex(index)
        self.advanced_task_intro.setText((
            'Choose a compatible installation or continue with a manual training package.',
            'Import an offline software pack, or export one exact retained trainer generation.',
            'Repair from a verified source, or review rollback to one retained generation.',
            'Inspect the complete reviewed software, source, cost and license evidence.',
        )[index])
        self.generation_panel.setVisible(index in (1, 2))
        self.export_button.setVisible(index == 1)
        self.rollback_button.setVisible(index == 2)

    def _show_recovery(self):
        self.tabs.setCurrentIndex(1)
        self.advanced_tasks.setCurrentRow(2)
        self.advanced_tasks.setFocus()

    def _show_existing_options(self):
        self.tabs.setCurrentIndex(1)
        self.advanced_tasks.setCurrentRow(0)
        self.existing_button.setFocus()

    def _show_software_details(self):
        self.tabs.setCurrentIndex(1)
        self.advanced_tasks.setCurrentRow(3)
        self.profile_detail.setFocus()

    def _page_details(self, offset):
        pages = max(1, math.ceil(len(self._profile_detail_text) / self.DETAIL_PAGE_SIZE))
        self._profile_detail_page = max(0, min(pages - 1, self._profile_detail_page + offset))
        start = self._profile_detail_page * self.DETAIL_PAGE_SIZE
        self.profile_detail.setPlainText(self._profile_detail_text[start:start + self.DETAIL_PAGE_SIZE])
        self.detail_page_label.setText(f'Page {self._profile_detail_page + 1:,} of {pages:,} · Complete evidence')
        self.previous_details.setEnabled(self._profile_detail_page > 0)
        self.next_details.setEnabled(self._profile_detail_page + 1 < pages)
        self.detail_paging.setVisible(pages > 1)

    def _toggle_plan_details(self):
        visible = self.preview_detail.isHidden()
        self.preview_detail.setVisible(visible)
        self.plan_details_button.setText('Hide exact plan details' if visible else 'Show exact plan details')

    def _set_stage(self, stage, action=None):
        if action is not None:
            self._review_action = action
        labels = {1: 'Step 1 of 3 · Review the setup plan',
                  2: 'Step 2 of 3 · Install or check the software',
                  3: 'Step 3 of 3 · Review and select the verified trainer'}
        if getattr(self, '_review_action', None) == 'export':
            labels = {1: 'Step 1 of 3 · Review the software pack export',
                      2: 'Step 2 of 3 · Export and verify the software pack',
                      3: 'Step 3 of 3 · Software pack ready'}
        self.steps.setText(labels[stage])

    def _load_overview(self, supplied):
        if self._closing or self._task is not None: return
        def work(job):
            return self.profile.sha256, supplied if supplied is not None else profile_overview(self.profile)
        def completed(result):
            self._profile_sha256 = result[0]
            self._show_overview(result[1])
            self.status.setText('Choose Set Up Trainer to review the plan.' if self._supported('managed') else 'Choose Existing Trainer to review a local installation, or use a manual training package.')
        self._run('Reading reviewed profile', work, completed)

    def _show_overview(self, value):
        if (not isinstance(value, TrainingSetupOverview) or type(value.file_count) is not int or value.file_count < 0
                or any(type(getattr(value, name)) is not str for name in ('versions', 'costs', 'sources', 'details', 'license_details'))):
            self._failed('The reviewed profile overview is invalid. Request an updated Setup plan.')
            return
        self.versions.setText(value.versions[:4000]); self.costs.setText(value.costs[:4000])
        self._profile_detail_text = (self.machine_fit_summary + '\n\n' + value.details
                                     + '\n\n' + value.sources + '\n\n' + value.license_details)
        self._profile_detail_page = 0
        self._page_details(0)
        self.inventory_count.setText(f'{value.file_count:,} pinned software files · Summary only; no directory scan')

    def _run(self, name, work, completed):
        if self._closing or self._task is not None: return
        task = CoordinatedCallTask(self.jobs, name, work, resource='io')
        self._cancel_requested = False
        self._task = task; self._phase = name; self._started = time.monotonic()
        self.progress.setRange(0, 0); self.progress.show(); self.cancel_button.show()
        self._timer.start(); self._timing()
        task.signals.progress.connect(lambda done, total, phase: self._on_progress(task, done, total, phase))
        task.signals.complete.connect(lambda result: self._finish(task, completed, result))
        task.signals.failed.connect(lambda error: self._finish(task, self._failed, error))
        task.signals.cancelled.connect(lambda: self._finish(task, self._failed, 'Stopped at a safe boundary. Existing generations remain available; review Setup for retained work.'))
        self._update_actions()
        try: task.start()
        except Exception as exc: self._finish(task, self._failed, str(exc))

    def _finish(self, task, completed, result):
        if self._task is not task: return
        self._task = None; self._timer.stop(); self.progress.hide(); self.cancel_button.hide()
        if self._closing:
            super().reject()
            return
        completed(result)
        self._update_actions()

    def _on_progress(self, task, done, total, phase):
        if self._task is not task: return
        self._phase = str(phase)[:400]
        self.progress.setRange(0, max(0, total))
        if total: self.progress.setValue(min(done, total))
        self._timing()

    def _timing(self):
        if self._task is not None:
            self.status.setText(f'{self._phase} · Elapsed {int(time.monotonic() - self._started):,}s')

    @staticmethod
    def _callbacks(job):
        def progress(done, total, label):
            # Normalize large byte/file counters before Qt's signed-int signal.
            if total > 0:
                job.update(max(0, min(1000, int(done * 1000 // total))), 1000, str(label)[:400])
            else: job.update(0, 0, str(label)[:400])
        return {'should_cancel': lambda: job.cancel_requested, 'progress': progress}

    def prepare_action(self, action, selection):
        if self._task is not None or self._queued is not None or self._closing or not self._supported(action): return
        if action != 'existing' and (self.profile is None or not self._profile_sha256): return
        self._preview = self._candidate = None; self.use_button.hide()
        self.open_recovery_button.hide()
        self._candidate_reference = None
        self._review_active = True
        self._show_review()
        self._set_stage(3 if action in {'activate', 'rollback'} else 1, action)
        self.preview_title.setText('Checking the setup plan…')
        self.preview_summary.setText('Reading the selected software plan and its requirements. Installation and software health checks follow confirmation.')
        self.preview_detail.clear()
        self.preview_detail.hide()
        self.plan_details_button.setText('Show exact plan details')
        def work(job):
            preview = self.controller.prepare(action, selection, **self._callbacks(job))
            expected = self.profile.sha256 if self.profile is not None else ''
            if (not isinstance(preview, TrainingSetupPreview) or preview.action != action
                    or type(preview.can_apply) is not bool or preview.profile_sha256 != expected
                    or expected != self._profile_sha256
                    or any(type(getattr(preview, name)) is not str or not getattr(preview, name).strip()
                           or len(getattr(preview, name)) > 64000 for name in ('title', 'summary', 'details', 'confirmation_label'))):
                raise ValueError('The preparation preview is stale or invalid. Request a new Setup plan.')
            return preview
        self._run('Reviewing trainer preparation', work, self._prepared)

    def _prepared(self, preview):
        self._preview = preview
        self.preview_title.setText(preview.title[:500]); self.preview_summary.setText(preview.summary[:4000])
        self.preview_detail.setPlainText(preview.details)
        self.confirm_button.setText(preview.confirmation_label[:100])
        self.status.setText('Review the plan above, then choose ' + preview.confirmation_label + '.' if preview.can_apply else 'This option is held. Open Advanced setup for other options or recovery.')
        self.open_recovery_button.setVisible(not preview.can_apply)
        self.confirm_button.setFocus()

    def _confirm(self):
        preview = self._preview
        if self._task is not None or self._queued is not None or preview is None or not preview.can_apply: return
        self._preview = None
        self._set_stage(3 if preview.action in {'activate', 'rollback'} else 2)
        self.preview_title.setText('Selecting the trainer…' if preview.action in {'activate', 'rollback'} else
                                  'Exporting the software pack…' if preview.action == 'export' else 'Preparing the trainer software…')
        self.preview_summary.setText('Your confirmed operation is starting. Progress and cancellation are available below.')
        def work(job):
            expected = self.profile.sha256 if self.profile is not None else ''
            if preview.profile_sha256 != expected or expected != self._profile_sha256:
                raise ValueError('The trainer profile changed. Review a new preparation plan.')
            result = self.controller.apply(preview, **self._callbacks(job))
            if self.profile is not None and self.profile.sha256 != expected:
                raise ValueError('The trainer profile changed during preparation. Review the retained result in Setup.')
            return result
        self._run(preview.confirmation_label, work, lambda result: self._applied(preview.action, result))

    def _applied(self, action, result):
        from .training_setup_queue_controller import QueuedTrainerSetup
        self.open_recovery_button.hide()
        self._review_active = True
        self._show_review()
        if isinstance(result, QueuedTrainerSetup):
            self._queued = result
            self._queue_cancel_requested = False
            self.preview_title.setText('Trainer Setup Queued')
            self.preview_summary.setText('Your confirmed operation is in the shared Setup queue. You can cancel it or close this window while it continues.')
            self._candidate_reference = dict(result.reference)
            self.status.setText(result.message + ' This operation continues in the shared Setup queue if you close this window.')
            self.progress.setRange(0, 0); self.progress.show(); self.cancel_button.show()
            self._queue_timer.start()
            return
        self._candidate = None; self.use_button.hide()
        if action == 'export':
            self._set_stage(3, action)
            self.preview_title.setText('Software Pack Exported')
            self.preview_summary.setText('The software pack passed verification and is ready to import on another compatible machine.')
            self.operationFinished.emit(action, result)
            self.status.setText('Software pack export finished. The result is available in Setup.')
            return
        if ((action == 'existing' and not isinstance(result, TrainerEnvironmentStatus))
                or (action != 'existing' and not isinstance(result, TrainerGeneration))):
            self._failed('The controller returned an invalid preparation result. Review retained work in Setup before trying again.')
            return
        generation = result.generation if isinstance(result, TrainerEnvironmentStatus) else result
        valid = (isinstance(generation, TrainerGeneration) and generation.profile_sha256 == self._profile_sha256
                 and isinstance(generation.health, TrainerHealth) and generation.health.healthy is True
                 and isinstance(generation.software_root, Path) and generation.software_root.is_absolute()
                 and type(generation.health_checked_at) in (int, float) and math.isfinite(generation.health_checked_at)
                 and generation.health_checked_at > 0 and type(generation.health_inventory_sha256) is str
                 and len(generation.health_inventory_sha256) == 64
                 and all(char in '0123456789abcdef' for char in generation.health_inventory_sha256)
                 and generation.execution_ready is False)
        if isinstance(result, TrainerEnvironmentStatus):
            valid = valid and result.mode == 'existing' and result.state == 'healthy' and result.execution_ready is False
        else:
            valid = (valid and type(generation.generation_id) is str and len(generation.generation_id) == 32
                     and all(char in '0123456789abcdef' for char in generation.generation_id))
        if not valid:
            details = str(result.details)[:2000] if isinstance(result, TrainerEnvironmentStatus) else ''
            self._failed(details + '\nNo verified environment can be selected from this result. Review its profile, health, and retained work in Setup.')
            return
        self._set_stage(3)
        self.preview_title.setText('Trainer software is ready')
        self.preview_summary.setText('Next, review and activate this verified trainer. Then choose the tokenizer and output folders. Your model and recipe receive separate checks before training.'
            if action in {'managed', 'import', 'repair'} else
            'Continue with this verified trainer, then review the tokenizer and training output folders to save your Setup. Your model and recipe receive separate checks before training.')
        self.preview_detail.setPlainText('Software location: ' + str(generation.software_root)
            + '\nGeneration: ' + (generation.generation_id or 'Existing installation')
            + '\nProfile SHA-256: ' + generation.profile_sha256 + '\n\n' + generation.health.details)
        self.operationFinished.emit(action, result)
        if generation.generation_id:
            self._recent_generation = (
                generation.generation_id, 'Generation ' + generation.generation_id)
            self._show_generation_page(self._generation_page,
                                       selected_id=generation.generation_id)
        if valid and action in {'existing', 'managed', 'import', 'repair', 'activate', 'rollback'}:
            self._candidate = (action, result)
            self.use_button.setText('Review Trainer Activation' if action in {'managed', 'import', 'repair'} else 'Use This Trainer')
            self.use_button.show()
            self.status.setText('Software health was verified. Review activation or select this environment; training compatibility remains separate.')

    def _use_candidate(self):
        if self._task is not None or self._candidate is None: return
        action, result = self._candidate
        if action in {'managed', 'import', 'repair'}:
            self.prepare_action('activate', result.generation_id)
        else:
            selection = {'mode': 'existing' if action == 'existing' else 'managed', 'result': result}
            if self._candidate_reference is not None:
                selection['review_reference'] = dict(self._candidate_reference)
            self.preparationSelected.emit(selection)
            self.accept()

    def _manual(self):
        if self._task is not None or self._queued is not None: return
        self.preparationSelected.emit({'mode': 'manual'})
        self.accept()

    def _choose_existing(self):
        path = QFileDialog.getExistingDirectory(self, 'Choose Existing Trainer Installation')
        if path: self.prepare_action('existing', path)

    def _choose_import(self):
        path, _ = QFileDialog.getOpenFileName(self, 'Choose Offline Trainer Pack', '', 'Trainer software packs (*.zip)')
        if path: self.prepare_action('import', path)

    def _choose_repair(self):
        path = QFileDialog.getExistingDirectory(self, 'Choose Verified Trainer Software Source')
        if path: self.prepare_action('repair', path)

    def _choose_export(self):
        generation = self.generation_picker.currentData()
        if not generation: return
        path, _ = QFileDialog.getSaveFileName(self, 'Export Trainer Software Pack', '', 'Trainer software packs (*.zip)')
        if path: self.prepare_action('export', (generation, path))

    def _failed(self, error):
        self._preview = self._candidate = None; self.use_button.hide()
        self._review_active = True
        self._show_review()
        self.preview_title.setText('Preparation Needs Attention')
        self.preview_summary.setText('Existing generations are preserved. Review the details, retry preparation, choose recovery, or continue with manual export.')
        self.preview_detail.setPlainText(str(error)[:64000]); self.status.setText(str(error)[:2000])
        self.preview_detail.show()
        self.plan_details_button.setText('Hide exact plan details')
        self.open_recovery_button.show()

    def _update_actions(self, *_):
        from shiboken6 import isValid
        idle = self._task is None and self._queued is None and not self._closing
        review = self._preparation_review() if self._preparation_review is not None else None
        self.return_preparation_button.setVisible(
            review is not None and isValid(review) and review.isVisible())
        reviewing = self.tabs.currentIndex() == 0 and self.guided_pages.currentIndex() == 1
        if idle and self.tabs.currentIndex() == 1:
            if self._preview is not None:
                self.status.setText('Reviewed plan awaiting confirmation in Guided setup.')
            elif not self._review_active:
                self.status.setText('Choose an advanced task; changes require a separate plan review.')
        self.held.setVisible(not reviewing)
        self.return_button.setVisible(self._review_active)
        self.back_button.setEnabled(idle)
        guided_available = self.profile is not None and self._supported('managed')
        self.steps.setVisible(guided_available)
        self.recommendation.setText('Recommended setup' if guided_available else 'Use an existing trainer')
        if not guided_available:
            self.welcome_summary.setText('Choose a compatible local installation, or use a manual training package without selecting a trainer here.')
        self.advanced_button.setText('Other Setup Options…' if guided_available else 'Choose Existing Trainer…')
        self.managed_button.setVisible(guided_available)
        self.managed_note.setVisible(guided_available)
        self.manual_button.setEnabled(idle)
        self.managed_button.setEnabled(idle and self.profile is not None and bool(self._profile_sha256) and self._supported('managed'))
        self.managed_button.setToolTip('A reviewed acquisition plan and supported controller are required.' if not self.managed_button.isEnabled() else 'Review the exact installation plan before confirming.')
        self.existing_button.setEnabled(idle and self._supported('existing'))
        self.existing_button.setToolTip('Choose an explicit installation; the controller will show its software locations before verification.' if self._supported('existing') else 'An existing-installation controller is required; manual export remains available.')
        for action, button in (('import', self.import_button), ('export', self.export_button), ('repair', self.repair_button), ('rollback', self.rollback_button)):
            button.setEnabled(idle and self.profile is not None and bool(self._profile_sha256) and self._supported(action) and (action not in {'export', 'rollback'} or self.generation_picker.count() > 0))
            button.setToolTip('Requires a reviewed profile, supported Setup controller, and an applicable selected generation.')
        self.generation_picker.setEnabled(idle)
        self.previous_generations.setEnabled(idle and self._generation_page > 0)
        self.next_generations.setEnabled(
            idle and (self._generation_page + 1) * self.GENERATION_PAGE_SIZE
            < self._generation_total)
        self.confirm_button.setEnabled(idle and self._preview is not None and self._preview.can_apply)
        self.confirm_button.setVisible(reviewing and self._preview is not None)
        self.use_button.setEnabled(idle and self._candidate is not None)
        self.use_button.setVisible(reviewing and self._candidate is not None)
        self.cancel_button.setEnabled((self._task is not None or self._queued is not None) and not self._closing
                                      and not self._queue_cancel_requested and not self._cancel_requested)

    def _poll_queued(self):
        if self._closing or self._queued is None:
            self._queue_timer.stop()
            return
        pending = self._queued
        operation = self.controller.operation(pending.operation_id)
        if operation is None:
            self._queued = None; self._queue_timer.stop()
            self.progress.hide(); self.cancel_button.hide()
            self._failed('The retained Setup queue operation is unavailable. Review the shared queue before retrying.')
            self._update_actions()
            return
        self.status.setText(operation.detail)
        if operation.state in {'waiting', 'running'}:
            if self._queue_cancel_requested:
                self.preview_title.setText('Stopping Trainer Setup')
                self.preview_summary.setText('Cancellation requested. Waiting for the current copy or native process to stop and retain its recovery evidence.')
            else:
                self.preview_title.setText('Trainer Setup Queued' if operation.state == 'waiting' else 'Preparing Trainer')
                self.preview_summary.setText(operation.detail)
        if operation.state in {'waiting', 'running'}:
            return
        self._queued = None; self._queue_cancel_requested = False; self._queue_timer.stop()
        self.progress.hide(); self.cancel_button.hide()
        if operation.state == 'complete':
            self._run('Verifying the retained Setup result',
                lambda job: self.controller.completed(pending, **self._callbacks(job)),
                lambda result: self._applied(pending.action, result))
        else:
            self._failed(operation.error or operation.detail)
            self._update_actions()

    def _cancel(self):
        if self._queued is not None:
            _, message = self.controller.cancel(self._queued)
            self._queue_cancel_requested = True
            self.preview_title.setText('Stopping Trainer Setup')
            self.preview_summary.setText('Cancellation requested. Waiting for the current copy or native process to stop and retain its recovery evidence.')
            self.status.setText(message); self.cancel_button.setEnabled(False)
            return
        if self._task is not None:
            self._cancel_requested = True
            self.jobs.cancel(self._task.job_id)
            self._phase = 'Stopping at the current safe software boundary'
            self.cancel_button.setEnabled(False); self._timing()

    def reject(self):
        self._closing = True
        if self._task is not None:
            self._cancel(); self._update_actions()
            return
        super().reject()

    def accept(self):
        if self._task is not None: return
        self._closing = True
        super().accept()

    def closeEvent(self, event):
        if self._task is not None:
            event.ignore(); self.reject()
        else: super().closeEvent(event)
