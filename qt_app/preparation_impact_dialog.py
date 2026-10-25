from __future__ import annotations

import copy
from dataclasses import dataclass
from pathlib import Path
from typing import Callable

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QComboBox, QDialogButtonBox, QHeaderView, QHBoxLayout, QLabel, QPushButton,
    QProgressBar, QSizePolicy, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from core.ai_interrogation import configured_plain_language_backend
from core.library_storage import LibraryStorageInspector
from core.preferred_caption_preparation import PreferredCaptionPreparationService
from core.preparation_policy_impact import PreparationPolicyImpactService
from .components import bind_status_footer,set_progress_bar
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask


@dataclass(frozen=True)
class PreparationImpactContext:
    images: tuple[Path, ...]
    jobs: object
    service: object
    is_current: Callable[[], bool]
    owner: object = None


def _caption_configuration(settings):
    keys=(
        "plain_language_model", "ai_models_directory", "joycaption_prompt",
        "joycaption_max_new_tokens", "joycaption_min_new_tokens",
        "joycaption_num_beams", "joycaption_length_penalty", "joycaption_do_sample",
        "joycaption_temperature", "joycaption_top_k", "joycaption_top_p",
        "joycaption_repetition_penalty", "joycaption_no_repeat_ngram_size",
        "joycaption_gpu_index", "joycaption_quantization",
    )
    return tuple(getattr(settings,key,None) for key in keys)


def preparation_impact_context(page) -> PreparationImpactContext:
    """Freeze a contextual read without loading models or creating a journal."""
    images = tuple(dict.fromkeys(Path(p) for p in page.preparation_paths()))
    settings = copy.copy(page.settings)
    configuration = _caption_configuration(settings)
    ignored = tuple(settings.ignored_plain_language_sentences)
    dataset = getattr(page, "dataset", None)
    generation = getattr(page, "_dataset_scope_generation", None)
    root = getattr(page, "root", None)

    def current():
        return (
            not getattr(page, "_shutdown_requested", False)
            and getattr(page, "dataset", None) is dataset
            and getattr(page, "_dataset_scope_generation", None) == generation
            and getattr(page, "root", None) == root
            and tuple(dict.fromkeys(Path(p) for p in page.preparation_paths())) == images
            and _caption_configuration(page.settings) == configuration
            and tuple(page.settings.ignored_plain_language_sentences) == ignored
        )

    def availability(path):
        provider = getattr(dataset, "storage_availability", None)
        value = provider(path) if callable(provider) else None
        if value is not None and not value.ready_for_write:
            return f"{value.label}: {value.detail}"
        # Check local file-family state before any content read. This does not
        # hydrate placeholders or treat a cached library status as file proof.
        value = LibraryStorageInspector().inspect_image(path)
        if not value.ready_for_write:
            return f"{value.label}: {value.detail}"
        return ""

    identity_cache=[]

    def identity(_record):
        # Resolve only inside an explicitly requested Description assessment.
        # Evidence-only/Tag-only work stays usable with missing captioner setup.
        if not identity_cache:
            identity_cache.append(configured_plain_language_backend(settings).evidence_identity())
        return identity_cache[0]

    preferred = PreferredCaptionPreparationService(None, identity, ignored_plain_language=ignored)
    service = PreparationPolicyImpactService(preferred, availability=availability)
    return PreparationImpactContext(images, page.state.jobs, service, current, page)


class PreparationImpactDialog(FeedbackEnabledDialog):
    """Read-only current-evidence assessment; execution remains in Prepare."""

    feedback_workflow = "Preparation policy impact"
    PAGE_SIZE = 100
    LABELS = {
        "eligible": "Eligible now", "review": "Needs review",
        "unchanged": "Already current", "unavailable": "Unavailable",
        "not_assessed": "Not assessed",
    }

    def __init__(self, context, options, *, is_current=None, parent=None):
        super().__init__(parent)
        self.context = context
        self.options = options
        self.is_current = is_current or context.is_current
        self.report = None
        self._task = None
        self._closed = False
        self.page = 0
        if context.owner is not None:
            context.owner._preparation_impact_dialog=self
        self.setWindowTitle("Preparation Impact")
        self.resize(900, 650)
        layout = QVBoxLayout(self)
        self.heading = QLabel(f"Check preparation for {len(context.images):,} images")
        self.heading.setWordWrap(True)
        self.heading.setObjectName("sectionTitle")
        layout.addWidget(self.heading)
        note = QLabel(
            "These counts describe current Description evidence. New AI results "
            "are unknown until generated. Starting preparation checks the images again."
        )
        note.setWordWrap(True)
        layout.addWidget(note)
        self.steps_toggle=QPushButton("Show Planned Steps")
        self.steps_toggle.setCheckable(True)
        self.steps_toggle.setSizePolicy(QSizePolicy.Maximum,QSizePolicy.Fixed)
        layout.addWidget(self.steps_toggle)
        self.steps = QLabel()
        self.steps.setWordWrap(True)
        self.steps.setTextFormat(Qt.PlainText)
        self.steps.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.steps.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Maximum)
        self.steps.hide()
        self.steps_toggle.toggled.connect(self._toggle_steps)
        layout.addWidget(self.steps)
        self.status = QLabel("Checking current evidence…")
        self.status.setWordWrap(True)
        self.status.setTextFormat(Qt.PlainText)
        self.status.setAccessibleName("Preparation impact status")
        layout.addWidget(self.status)
        self.outcome_filter=QComboBox()
        self.outcome_filter.setAccessibleName("Filter current Description outcomes")
        self.outcome_filter.addItem("All Images", "")
        for key,label in self.LABELS.items():
            self.outcome_filter.addItem(label,key)
        self.outcome_filter.currentIndexChanged.connect(self._filter_changed)
        layout.addWidget(self.outcome_filter)
        self._visible_items=()
        self.rows = QTreeWidget()
        self.rows.setHeaderLabels(("Image", "Current Description", "Reason"))
        self.rows.setRootIsDecorated(False)
        self.rows.setUniformRowHeights(True)
        self.rows.setAccessibleName("Exact preparation scope and current outcomes")
        header = self.rows.header()
        header.setSectionResizeMode(QHeaderView.Interactive)
        header.setStretchLastSection(True)
        self.rows.setColumnWidth(0, 210)
        self.rows.setColumnWidth(1, 145)
        layout.addWidget(self.rows, 1)
        paging = QHBoxLayout()
        self.previous = QPushButton("Previous")
        self.next = QPushButton("Next")
        self.page_label = QLabel()
        self.previous.clicked.connect(lambda: self._move(-1))
        self.next.clicked.connect(lambda: self._move(1))
        paging.addWidget(self.previous)
        paging.addWidget(self.next)
        paging.addWidget(self.page_label)
        paging.addStretch(1)
        layout.addLayout(paging)
        self.progress = QProgressBar()
        layout.addWidget(self.progress)
        self.buttons = QDialogButtonBox(QDialogButtonBox.Close)
        self.close_button = self.buttons.button(QDialogButtonBox.Close)
        self.close_button.setText("Cancel Check")
        self.buttons.rejected.connect(self.reject)
        bind_status_footer(
            self,self.status,self.progress,self.buttons,title="Preparation impact check",
        )
        layout.addWidget(self.buttons)
        # Enter in this read-only child never submits the parent operation.
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
            button.setDefault(False)
        self._render()
        QTimer.singleShot(0, self._start)

    def _start(self):
        if self._closed or self._task is not None:
            return
        if not self.is_current():
            self._failed("The scope or configuration changed. Close and reopen Prepare Images.")
            return
        set_progress_bar(self.progress, unknown_text="Checking current evidence…")

        def work(job):
            return self.context.service.preview(
                self.context.images, self.options, progress=job.update,
                should_cancel=lambda: not job.checkpoint(),
            )

        task = CoordinatedCallTask(
            self.context.jobs, "Checking preparation impact", work, resource="io",
        )
        self._task = task
        task.signals.progress.connect(self._progress)
        task.signals.complete.connect(self._ready)
        task.signals.failed.connect(self._failed)
        task.signals.cancelled.connect(lambda: self._failed("Check cancelled. Nothing changed."))
        task.start()

    def _progress(self, current, total, detail):
        if not self._closed:
            set_progress_bar(self.progress, current, total, detail=detail)

    def _toggle_steps(self, checked):
        self.steps.setVisible(checked)
        self.steps_toggle.setText("Hide Planned Steps" if checked else "Show Planned Steps")

    def _ready(self, report):
        if self._closed:
            return
        self._task = None
        self._notify_owner()
        if not self.is_current() or tuple(report.images) != self.context.images:
            self._failed("The scope or configuration changed. This result was discarded.")
            return
        if report.cancelled:
            self._failed("Check cancelled. Nothing changed.")
            return
        self.report = report
        self.steps.setText("\n".join(report.steps))
        self.status.setText("Current Descriptions: " + " • ".join(
            f"{label}: {report.count(key):,}" for key, label in self.LABELS.items()
            if report.count(key)
        ))
        self.progress.hide()
        self.close_button.setText("Back to Options")
        self._filter_changed()

    def _failed(self, message):
        if self._closed:
            return
        self._task = None
        self._notify_owner()
        self.report = None
        self._visible_items=()
        self.steps.clear()
        self.status.setText(str(message))
        self.progress.hide()
        self.close_button.setText("Back to Options")
        self._render()

    def _move(self, delta):
        self.page += delta
        self._render()

    def _filter_changed(self):
        status=self.outcome_filter.currentData()
        items=self.report.items if self.report is not None else ()
        self._visible_items=tuple(item for item in items if not status or item.status==status)
        self.page=0
        self._render()

    def _render(self):
        items = self._visible_items
        pages = max(1, (len(items) + self.PAGE_SIZE - 1) // self.PAGE_SIZE)
        self.page = max(0, min(self.page, pages - 1))
        start = self.page * self.PAGE_SIZE
        self.rows.clear()
        for item in items[start:start + self.PAGE_SIZE]:
            row = QTreeWidgetItem((item.image.name, self.LABELS[item.status], item.detail))
            row.setToolTip(0, str(item.image))
            row.setData(0, Qt.UserRole, str(item.image))
            row.setToolTip(2, item.detail)
            self.rows.addTopLevelItem(row)
        self.previous.setEnabled(self.page > 0)
        self.next.setEnabled(self.page + 1 < pages)
        self.page_label.setText(
            f"{start + 1:,}–{min(start + self.PAGE_SIZE, len(items)):,} of {len(items):,} matches • {len(self.context.images):,} in scope"
            if items else "No completed assessment"
        )

    def done(self, result):
        self._closed = True
        if self._task is not None:
            self._task.cancel()
            self._task=None
        self.report=None
        self._visible_items=()
        self.rows.clear()
        owner=self.context.owner
        if owner is not None and getattr(owner,"_preparation_impact_dialog",None) is self:
            owner._preparation_impact_dialog=None
        self._notify_owner()
        super().done(result)

    def _notify_owner(self):
        notify=getattr(self.context.owner,"_notify_background_work_finished",None)
        if callable(notify):
            QTimer.singleShot(0,notify)
