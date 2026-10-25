"""Reviewed fast-copy preparation, manual pins, cleanup and exact recovery."""
from __future__ import annotations

from itertools import islice
from datetime import datetime
import ntpath
import re
from pathlib import Path

from PySide6.QtCore import Qt, QTimer, Signal, QSignalBlocker
from PySide6.QtWidgets import (
    QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit,
    QPushButton, QSpinBox, QTabWidget, QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget,
)

from core.model_cache import CacheResource, MAX_MEMBERS, RECOVERY_SUMMARY_PAGE_SIZE, ModelCacheService, ModelRequirementSet
from core.model_resource_registry import ModelResourceError
from core.model_use_leases import ModelUseLeaseService, cache_state, pinned_entries


def _bytes(value):
    if type(value) is not int or value < 0: return "Unknown size"
    if value >= 2**30: return f"{value / 2**30:,.2f} GiB"
    if value >= 2**20: return f"{value / 2**20:,.2f} MiB"
    if value >= 2**10: return f"{value / 2**10:,.1f} KiB"
    return f"{value:,} bytes"


def _short_label(value, fallback):
    text = " ".join(str(value or "")[:4096].split())
    return (text[:117] + "…") if len(text) > 120 else text or fallback


def _record_label(record):
    user = record.get("user", {})
    if isinstance(user, dict) and user.get("display_name"):
        return _short_label(user["display_name"], "Unnamed model")
    locations = record.get("locations", ())
    names = []
    for location in islice(iter(locations), 256):
        if isinstance(location, dict) and location.get("path"):
            name = ntpath.basename(str(location["path"])[:4096].replace("/", "\\"))
            if name and name not in names: names.append(name)
        if len(names) == 3: break
    return _short_label(", ".join(names), "Model filename unknown")


def _created_label(value):
    if not isinstance(value, str) or len(value) > 64: return "Creation time unknown"
    try:
        timestamp = datetime.fromisoformat(value)
        return timestamp.isoformat(" ", timespec="seconds") + (" (zone unknown)" if timestamp.tzinfo is None else "")
    except ValueError: return "Creation time unknown"


def _entry_label(state, entry_ids):
    names = []
    if not isinstance(entry_ids, (list, tuple)): return "Model filenames unknown"
    for key in islice(iter(entry_ids), MAX_MEMBERS):
        entry = state["entries"].get(key, {}) if isinstance(key, str) else {}
        path = entry.get("archive_path") or entry.get("path")
        name = ntpath.basename(str(path)[:4096].replace("/", "\\")) if path else ""
        if name and name not in names: names.append(name)
        if len(names) == 3: break
    suffix = f" · {len(entry_ids)} files" if entry_ids else ""
    return _short_label(", ".join(names), "Model filenames not yet recorded") + suffix


class ModelCacheDialog(QDialog):
    """No discovery, inferred companions, eviction or backend configuration.

    A supplied service_factory(snapshot) returns a cache service using current
    protected/managed boundaries. Manual choices never claim adapter admission.
    Prepared IDs are receipts for caller-owned review, not launch permission.
    """

    prepared = Signal(str)

    def __init__(self, registry, jobs, resource_ids=(), parent=None, *, protected_roots=(),
                 managed_roots=(), service=None, lease_service=None, setup_folders=None,
                 service_factory=None, initial_requirements=None, initial_recovery=False,
                 initial_operation_id: str | None = None):
        super().__init__(parent)
        identities = tuple(islice(iter(resource_ids), MAX_MEMBERS + 1))
        if len(identities) > MAX_MEMBERS or any(not isinstance(key, str) for key in identities) or len(set(identities)) != len(identities):
            raise ModelResourceError(f"Choose at most {MAX_MEMBERS} distinct registered models.")
        self.registry, self.jobs, self.resource_ids = registry, jobs, identities
        self.service = service or ModelCacheService(registry, protected_roots=protected_roots, managed_roots=managed_roots)
        self.lease_service = lease_service or ModelUseLeaseService(registry,
            protected_roots=self.service.protected_roots, managed_roots=self.service.managed_roots)
        self.service_factory, self.setup_folders = service_factory, setup_folders
        self._initial_requirements = initial_requirements
        self._initial_recovery = bool(initial_recovery)
        self._initial_operation_id = initial_operation_id
        self._exact_recovery_hold = ""
        self._task = None
        self._closing = False
        self._close_parent = None
        self._token = None
        self._snapshot = None
        self._operation_offset = 0
        self._operation_keys = ()
        self._operation_label_generation = 0
        self._pending_operation_labels = None
        self._sources = {}
        self._preview = self._pin_preview = self._cleanup = self._recovery = None
        self._cleanup_recommendation = None
        self._pending_cleanup_recommendation = None
        self.outcomes = ()
        self.prepared_operation_ids = ()
        self.changed = False
        self.setWindowTitle("Prepare and Manage Fast Model Copies")
        self.setWindowModality(Qt.WindowModal)
        self.resize(1380, 880)
        body = QVBoxLayout(self)
        self.intro = QLabel("Prepare exact registered archive copies on Fast storage. Originals stay in place. Completed copies remain pinned until you explicitly release them after use.")
        self.intro.setWordWrap(True); body.addWidget(self.intro)
        self.tabs = QTabWidget(); body.addWidget(self.tabs, 1)
        self.tabs.currentChanged.connect(self._tab_changed)

        choose = self._tab("Choose Archives")
        choose.addWidget(self._label("Check each model, then choose its exact archive copy, recorded Model Type / Family and media. This manual set does not prove complete companions or run compatibility."))
        self.files = self._tree(("Model", "Archive copy", "Recorded Model Type / Family", "Media"), (260, 540, 300))
        self.files.itemChanged.connect(self._choices_changed); choose.addWidget(self.files, 1)
        self.check_files = self._button("Check Listed Models", self._check_files)
        self.preview_button = self._button("Preview Preparation", self._preview_files)
        self._actions(choose, self.check_files, self.preview_button)

        review = self._tab("Preparation Review")
        self.review_summary = self._label("No current preparation preview."); review.addWidget(self.review_summary)
        self.capacity_details = self._details("Exact least-used Fast copies and space consequences")
        self.capacity_details.setMaximumHeight(240); self.capacity_details.hide()
        review.addWidget(self.capacity_details)
        self.rows = self._tree(("Archive / Resource", "Fast destination", "State / consequence"), (420, 460))
        review.addWidget(self.rows, 1)
        self.preparation_exact_toggle = QCheckBox("Show Exact Preparation Details")
        self.preparation_exact_toggle.toggled.connect(lambda shown: self.preparation_details.setVisible(shown))
        self.preparation_exact_toggle.hide(); review.addWidget(self.preparation_exact_toggle)
        self.preparation_details = self._details("Exact preparation receipts and model paths")
        self.preparation_details.setMaximumHeight(180)
        self.preparation_details.hide(); review.addWidget(self.preparation_details)
        self.apply_button = self._button("Prepare Reviewed Copies", self._apply_prepare)
        self.apply_button.setProperty("primary", True)
        self.result_recovery = self._button("Review Last Operation Recovery", self._result_recovery)
        self._actions(review, self.result_recovery, self.apply_button)

        pins = self._tab("Pins")
        pins.addWidget(self._label("Pins protect prepared files from cleanup. Release only an active manual pin after every external user has finished. Execution leases remain owned by their jobs."))
        self.pins = QComboBox(); self.pins.setAccessibleName("Active manual cache pin")
        self.pins.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.pins.setMinimumContentsLength(28)
        self.pins.currentIndexChanged.connect(self._clear_pin); pins.addWidget(self.pins)
        self.pin_details = self._details("Reviewed manual pin release"); pins.addWidget(self.pin_details, 1)
        self.external_finished = QCheckBox("External use of every file in this pin has finished")
        self.external_finished.toggled.connect(self._update_actions); pins.addWidget(self.external_finished)
        self.inspect_pin = self._button("Review Pin Release", self._preview_pin)
        self.release_pin = self._button("Release Reviewed Manual Pin", self._release_pin)
        self._actions(pins, self.inspect_pin, self.release_pin)

        cleanup = self._tab("Cleanup")
        cleanup.addWidget(self._label("Free Fast storage without losing your models. A recommendation uses known successful-use history and verifies each selected copy's independent Archive original. Nothing is deleted by reviewing."))
        cleanup_goal = QHBoxLayout()
        cleanup_goal.addWidget(self._label("Free at least"))
        self.reclaim_target = QSpinBox()
        self.reclaim_target.setRange(1, 1024 * 1024)
        self.reclaim_target.setValue(1024)
        self.reclaim_target.setSuffix(" MiB")
        self.reclaim_target.setAccessibleName("Fast storage to reclaim in MiB")
        self.reclaim_target.valueChanged.connect(self._clear_cleanup)
        cleanup_goal.addWidget(self.reclaim_target)
        self.recommend_cleanup = self._button("Review Recommended Cleanup", self._preview_recommended_cleanup)
        cleanup_goal.addStretch(1); cleanup_goal.addWidget(self.recommend_cleanup)
        cleanup.addLayout(cleanup_goal)
        self.cleanup_summary = self._label("Choose how much space you need, then review a safe recommendation. Known-use history is required; manual review remains available.")
        cleanup.addWidget(self.cleanup_summary)
        self.recommended_entries = self._tree(("Model / Fast copy", "Archive protection", "Known successful use", "Size"), (270, 240, 140))
        self.recommended_entries.setMinimumHeight(240)
        self.recommended_entries.hide(); cleanup.addWidget(self.recommended_entries, 1)
        self.customize_cleanup = QCheckBox("Customize Cleanup and Individual Copies")
        self.customize_cleanup.toggled.connect(self._toggle_cleanup_customization)
        cleanup.addWidget(self.customize_cleanup)
        self.cleanup_advanced = QWidget(); advanced = QVBoxLayout(self.cleanup_advanced)
        advanced.addWidget(self._label("Choose up to 64 exact receipt-owned Fast copies. Pinned, active, Keep Warm, uncertain, or unverified copies cannot be removed; a fresh manual review will explain a refusal."))
        self.entries = self._tree(("Fast copy", "Archive original", "State / pin", "Size"), (470, 470, 190))
        self.entries.itemChanged.connect(self._clear_cleanup); advanced.addWidget(self.entries, 1)
        self.inspect_cleanup = self._button("Review Checked Cleanup", self._preview_cleanup)
        self.restore_cleanup = self._button("Restore Recommended Review", self._preview_recommended_cleanup)
        self._actions(advanced, self.inspect_cleanup, self.restore_cleanup)
        self.cleanup_advanced.hide(); cleanup.addWidget(self.cleanup_advanced, 1)
        self.cleanup_exact_toggle = QCheckBox("Show Exact Files and Consequences")
        self.cleanup_exact_toggle.toggled.connect(self._toggle_cleanup_exact)
        self.cleanup_exact_toggle.hide()
        cleanup.addWidget(self.cleanup_exact_toggle)
        self.cleanup_details = self._details("Exact cache cleanup review"); self.cleanup_details.setMaximumHeight(180)
        self.cleanup_details.hide(); cleanup.addWidget(self.cleanup_details)
        cleanup.addStretch(1)
        self.apply_cleanup = self._button("Delete Reviewed Fast Copies", self._apply_cleanup)
        self.apply_cleanup.setProperty("primary", True)
        self.cleanup_result_recovery = self._button("Review This Cleanup Recovery", self._result_recovery)
        self.cleanup_result_recovery.hide()
        self._actions(cleanup, self.cleanup_result_recovery, self.apply_cleanup)

        recovery = self._tab("Recovery")
        recovery.addWidget(self._label("Resume one recorded operation at its original paths. A changed folder layout never retargets recovery; current protected boundaries still apply."))
        self.recovery_summary = self._label("Loading recorded operations…")
        recovery.addWidget(self.recovery_summary)
        self.operations = QComboBox(); self.operations.setAccessibleName("Recorded cache operation")
        self.operations.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.operations.setMinimumContentsLength(28)
        self.operations.currentIndexChanged.connect(self._clear_recovery); recovery.addWidget(self.operations)
        self.operation_page = self._label("")
        recovery.addWidget(self.operation_page)
        self.previous_operations = self._button("Previous Operations", lambda: self._change_operation_page(-1))
        self.next_operations = self._button("Next Operations", lambda: self._change_operation_page(1))
        self._actions(recovery,self.previous_operations,self.next_operations)
        self.recovery_exact_toggle = QCheckBox("Show Exact Receipt and Paths")
        self.recovery_exact_toggle.toggled.connect(self._toggle_recovery_exact)
        recovery.addWidget(self.recovery_exact_toggle)
        self.recovery_details = self._details("Exact recorded cache recovery paths")
        recovery.addWidget(self.recovery_details, 1)
        self.recovery_details.hide()
        self.inspect_recovery = self._button("Review Selected Recovery", self._preview_recovery)
        self.recover_button = self._button("Resume Reviewed Operation", self._recover)
        self._actions(recovery, self.inspect_recovery, self.recover_button)
        recovery.addStretch(1)

        self.status = self._label("Loading recorded model and cache state…"); body.addWidget(self.status)
        self.reload = self._button("Reload Recorded State", self._load)
        self.setup = self._button("Edit Folder Locations…", self._setup)
        self.stop = self._button("Cancel Safely", self._stop)
        self.close_button = self._button(
            "Back to Saved Import" if initial_operation_id is not None else "Back to Models", self.reject)
        self._actions(body, self.reload, self.setup, self.stop, self.close_button)
        if initial_operation_id is not None:
            for index in range(self.tabs.count()):
                self.tabs.setTabVisible(index, index == 4)
            self.setup.hide()
            self.tabs.setCurrentIndex(4)
            self.resize(1080, 620)
        self._update_actions(); QTimer.singleShot(0, self._load)

    def _tab(self, title):
        page = QWidget(); layout = QVBoxLayout(page); self.tabs.addTab(page, title)
        return layout

    def _tab_changed(self, index):
        self.intro.setText(
            "Free Fast storage by reviewing receipt-owned copies. Only a separate confirmation can remove them; verified Archive originals remain."
            if index == 3 else
            "Review one recorded operation and its exact recovery. No new copies are selected while resuming retained work."
            if index == 4 else
            "Prepare exact registered archive copies on Fast storage. Originals stay in place. Completed copies remain pinned until you explicitly release them after use.")
        if index==4 and self._task is None and self._pending_operation_labels:
            self._load_operation_labels(*self._pending_operation_labels)

    @staticmethod
    def _label(text):
        label = QLabel(text); label.setWordWrap(True); label.setTextFormat(Qt.PlainText)
        return label

    @staticmethod
    def _details(name):
        widget = QPlainTextEdit(); widget.setReadOnly(True); widget.setAccessibleName(name)
        return widget

    @staticmethod
    def _tree(headers, widths):
        tree = QTreeWidget(); tree.setRootIsDecorated(False); tree.setHeaderLabels(headers)
        for index, width in enumerate(widths): tree.setColumnWidth(index, width)
        return tree

    @staticmethod
    def _button(text, callback):
        button = QPushButton(text); button.clicked.connect(callback); return button

    @staticmethod
    def _actions(layout, *buttons):
        row = QHBoxLayout()
        for index, button in enumerate(buttons):
            if index == len(buttons) - 1: row.addStretch(1)
            row.addWidget(button)
        layout.addLayout(row)

    @staticmethod
    def _item(tree, texts, data=None, checked=False):
        item = QTreeWidgetItem([str(text) for text in texts]); item.setData(0, Qt.UserRole, data)
        for index in range(len(texts)): item.setToolTip(index, item.text(index))
        if checked:
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable); item.setCheckState(0, Qt.Unchecked)
        tree.addTopLevelItem(item)
        return item

    def _update_actions(self, *_):
        if not hasattr(self, "close_button"): return
        idle = self._task is None
        self.previous_operations.setEnabled(idle and self._operation_offset > 0 and self._initial_operation_id is None)
        self.next_operations.setEnabled(idle and self._operation_offset + RECOVERY_SUMMARY_PAGE_SIZE < len(self._operation_keys) and self._initial_operation_id is None)
        for widget in (self.files, self.entries, self.pins, self.operations, self.reload, self.check_files,
                       self.reclaim_target, self.customize_cleanup): widget.setEnabled(idle)
        if self._initial_operation_id is not None:
            self.operations.setEnabled(False)
        self.setup.setEnabled(idle and self.setup_folders is not None)
        self.preview_button.setEnabled(idle and self._token is not None and any(item.checkState(0) == Qt.Checked for item, *_ in self._sources.values()))
        self.apply_button.setEnabled(idle and self._preview is not None and bool(self._preview.rows)
            and self._preview.can_prepare)
        self.apply_button.setVisible(self._preview is not None)
        self.result_recovery.setVisible(bool(self.outcomes and self.outcomes[-1].status != "complete"))
        self.inspect_pin.setEnabled(idle and bool(self.pins.currentData()))
        self.external_finished.setEnabled(idle and self._pin_preview is not None)
        self.release_pin.setEnabled(idle and self._pin_preview is not None and self.external_finished.isChecked())
        self.inspect_cleanup.setEnabled(idle and self._token is not None and 1 <= len(self._checked_entries()) <= MAX_MEMBERS)
        self.recommend_cleanup.setEnabled(idle and self._token is not None)
        self.restore_cleanup.setEnabled(idle and self._token is not None)
        self.apply_cleanup.setEnabled(idle and self._cleanup is not None)
        self.cleanup_result_recovery.setEnabled(idle and bool(self.outcomes))
        exact_ready = (self._initial_operation_id is None or
                       (not self._exact_recovery_hold and self.operations.currentData() == self._initial_operation_id))
        self.inspect_recovery.setEnabled(idle and exact_ready and bool(self.operations.currentData()))
        self.recover_button.setEnabled(idle and exact_ready and self._recovery is not None)
        self.result_recovery.setEnabled(idle and bool(self.outcomes))
        self.stop.setEnabled(not idle); self.close_button.setEnabled(not self._closing)
        if self._initial_operation_id is not None:
            self.stop.setVisible(not idle)

    def _invalidate(self, message="No current review. Preview again before applying."):
        self._preview = self._pin_preview = self._cleanup = self._recovery = None
        self._cleanup_recommendation = None
        self.external_finished.setChecked(False)
        self.review_summary.setText(message); self.rows.clear()
        self.rows.setHeaderLabels(("Archive / Resource", "Fast destination", "State / consequence"))
        self.preparation_exact_toggle.setChecked(False); self.preparation_exact_toggle.hide()
        self.preparation_details.clear(); self.preparation_details.hide()
        self.capacity_details.clear(); self.capacity_details.hide()
        self.apply_button.setText("Prepare Reviewed Copies")
        for view in (self.pin_details, self.cleanup_details, self.recovery_details): view.setPlainText(message)
        self.recommended_entries.clear(); self.recommended_entries.hide()
        self.cleanup_exact_toggle.setChecked(False)
        self.cleanup_exact_toggle.hide()
        self.cleanup_summary.setText("Choose how much space you need, then review a safe recommendation. Known-use history is required; manual review remains available.")
        self.cleanup_result_recovery.hide()

    def _failed(self, message):
        self._pending_cleanup_recommendation = None
        self._invalidate(); self.status.setText(str(message))
        if self.tabs.currentIndex() == 3:
            self.cleanup_summary.setText(
                f"Cleanup review could not continue: {message} No new deletion was approved. "
                "Reload recorded state, review a smaller target, or inspect individual copies.")
        if self.tabs.currentIndex() == 4:
            self.recovery_summary.setText(f"Recovery review could not continue: {message} Reload recorded state or keep this operation for later review.")

    def _run(self, title, function, complete):
        if self._task is not None: return
        from .workers import CoordinatedCallTask
        from .job_progress import bind_job_progress
        task = CoordinatedCallTask(self.jobs, title, function, resource="io")
        self._task = task; self.status.setText(title)
        binding = bind_job_progress(self, task, title=title, unit="MiB", cancel_button=self.stop)
        task.signals.complete.connect(lambda result: self._finish(task, complete, result))
        task.signals.failed.connect(lambda message: self._finish(task, self._failed, message))
        task.signals.cancelled.connect(lambda: self._finish(task, self._failed,
            "Stopped at a safe boundary. Completed copies may remain pinned. Reload Recorded State and review Recovery."))
        self.destroyed.connect(task.cancel); self._update_actions()
        try: task.start()
        except Exception as exc:
            binding.finish(task); task.cancel()
            self._finish(task, self._failed, f"Could not start: {exc}. Retry when background capacity is available.")

    def _finish(self, task, callback, result):
        from shiboken6 import isValid
        if not isValid(self): task.cancel(); return
        if self._task is not task: return
        self.destroyed.disconnect(task.cancel); self._task = None
        if self._closing:
            continuation, self._close_parent = self._close_parent, None
            super().reject()
            if continuation: continuation()
            return
        try: callback(result)
        except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc))
        self._update_actions()

    @staticmethod
    def _progress(job):
        def report(done, total, label):
            units = max(1, (total + 2**20 - 1) // 2**20)
            job.update(units if done == total else done // 2**20, units, label)
        return report

    def _replace_service(self, service):
        self.service = service
        self.lease_service = ModelUseLeaseService(self.registry, protected_roots=service.protected_roots,
            managed_roots=service.managed_roots, use_verifier=getattr(self.lease_service, "use_verifier", None),
            terminal_verifier=getattr(self.lease_service, "terminal_verifier", None))

    def _policy_current(self):
        if self.service_factory is None: return True
        try: current = self.service_factory(self._snapshot)
        except (ValueError, RuntimeError, OSError) as exc:
            self._failed(str(exc)); self._update_actions(); return False
        if any(getattr(current, key) != getattr(self.service, key) for key in ("protected_roots", "managed_roots")):
            self._replace_service(current)
            self._failed("Protected or managed folder boundaries changed. Reload and review again.")
            self._update_actions(); return False
        return True

    def _load(self):
        if self._task is not None: return
        self._invalidate(); self._token = None
        self._run("Reading recorded cache state", lambda _job: self.registry.snapshot(), self._loaded)

    def _loaded(self, result):
        snapshot, token = result
        state = cache_state(snapshot); pinned = pinned_entries(state)
        previous = {key: (item.checkState(0), *(widget.currentData() for widget in widgets))
            for key, (item, *widgets) in self._sources.items()}
        self._snapshot, self._token = snapshot, None
        if self.service_factory is not None: self._replace_service(self.service_factory(snapshot))
        self._invalidate(); self._sources.clear()
        with QSignalBlocker(self.files):
            self.files.clear()
            for identity in self.resource_ids:
                record = snapshot["resources"].get(identity)
                if record is None: continue
                locations, declarations = record.get("locations", ()), record.get("declarations", ())
                if len(locations) > 256 or len(declarations) > 100:
                    raise ModelResourceError("Narrow this model's recorded copies/declarations before preparing.")
                title = _record_label(record)
                item = self._item(self.files, (title, "", "", ""), identity, checked=True)
                item.setToolTip(0, f"{title}\nResource: {identity}")
                source, declaration, media = QComboBox(), QComboBox(), QComboBox()
                source.addItem("Choose exact archive copy…", None)
                for location in locations:
                    if not location.get("cache_entry_id"):
                        source.addItem(str(location["path"]), str(location["path"]))
                        source.setItemData(source.count() - 1, str(location["path"]), Qt.ToolTipRole)
                declaration.addItem("Choose recorded Model Type / Family…", None)
                for entry in declarations: declaration.addItem(f"{entry['role']} / {entry['family']}", (entry["role"], entry["family"]))
                media.addItem("Choose media…", None)
                for label in ("Images", "Video"): media.addItem(label, label)
                if identity in previous:
                    checked, *values = previous[identity]; item.setCheckState(0, checked)
                    for widget, value in zip((source, declaration, media), values):
                        widget.setCurrentIndex(next((i for i in range(widget.count()) if widget.itemData(i) == value), 0))
                for column, widget in enumerate((source, declaration, media), 1):
                    self.files.setItemWidget(item, column, widget); widget.currentIndexChanged.connect(self._choices_changed)
                self._sources[identity] = item, source, declaration, media
        with QSignalBlocker(self.pins), QSignalBlocker(self.operations), QSignalBlocker(self.entries):
            self.pins.clear(); self.pins.addItem("Choose active manual pin…", "")
            for key, pin in reversed(tuple(state["leases"].items())):
                if pin.get("kind") == "manual" and pin.get("state") == "active":
                    label = f"{_entry_label(state, pin['entry_ids'])} · Active manual pin · {_created_label(pin.get('created_at'))}"
                    self.pins.addItem(label, key)
                    self.pins.setItemData(self.pins.count() - 1, f"{label}\nPin: {key}", Qt.ToolTipRole)
            self.entries.clear()
            for key, entry in reversed(tuple(state["entries"].items())):
                if entry.get("state") == "retired": continue
                self._item(self.entries, (entry.get("path", "Unknown"), entry.get("archive_path", "Unknown"),
                    f"{entry.get('state', 'Unknown')} · {'Pinned / in use' if key in pinned else 'Review eligibility'}",
                    _bytes(entry.get("size", 0))), key, checked=True)
        pending = tuple(key for key, operation in state["operations"].items()
                        if operation.get("phase") != "complete")
        if pending:
            self.recovery_summary.setText(
                f"{len(pending):,} recorded operation{'s' if len(pending) != 1 else ''} need recovery review. "
                "Choose one operation, review its recorded progress and exact files, then decide whether to resume it.")
        else:
            self.recovery_summary.setText("No interrupted Fast-copy operation is recorded. "
                                          "Use Cleanup or Pins for ordinary storage maintenance.")
        self.status.setText(f"{len(self._sources)} selected models · {self.entries.topLevelItemCount()} recorded Fast copies · {self.pins.count() - 1} active manual pins. No files changed by this read.")
        self._token = token
        self._populate_operation_page(start_labels=self._initial_requirements is None)
        if self._initial_operation_id is not None:
            self._initial_recovery = False
            self.tabs.setCurrentIndex(4)
            self._select_exact_recovery(state)
        elif self._initial_recovery:
            self._initial_recovery = False
            self.tabs.setCurrentIndex(4 if pending else 3)
            if len(pending) == 1:
                self.operations.setCurrentIndex(self.operations.findData(pending[0]))
        if self._initial_requirements is not None and self._initial_operation_id is None:
            requirements, self._initial_requirements = self._initial_requirements, None
            self.preview_requirements(requirements)

    def _operation_label(self,key,operation,files):
        kind = "Prepare Fast copies" if operation.get("kind")=="prepare" else "Clean up Fast copies" if operation.get("kind")=="cleanup" else "Unknown operation"
        phase = "Completed" if operation.get("phase")=="complete" else "Needs recovery review"
        reference=key[-8:]
        for width in range(8,33,4):
            reference=key[-width:]
            if all(other==key or other[-width:]!=reference for other in self._operation_keys):break
        # Keep the distinguishing reference before secondary history metadata
        # so a narrow picker does not hide it behind a long filename list.
        return f"{files} · Ref {reference} · {phase} · {kind} · {_created_label(operation.get('created_at'))}"

    def _populate_operation_page(self,*,retain=True,start_labels=True):
        state=cache_state(self._snapshot)
        selected=self.operations.currentData() if retain else None
        items=tuple(reversed(tuple(state["operations"].items())))
        self._operation_keys=tuple(key for key,op in items if op.get("phase")!="complete")+tuple(key for key,op in items if op.get("phase")=="complete")
        anchor=self._initial_operation_id or selected
        if anchor in self._operation_keys:
            self._operation_offset=(self._operation_keys.index(anchor)//RECOVERY_SUMMARY_PAGE_SIZE)*RECOVERY_SUMMARY_PAGE_SIZE
        self._operation_offset=min(self._operation_offset,max(0,((len(self._operation_keys)-1)//RECOVERY_SUMMARY_PAGE_SIZE)*RECOVERY_SUMMARY_PAGE_SIZE))
        keys=self._operation_keys[self._operation_offset:self._operation_offset+RECOVERY_SUMMARY_PAGE_SIZE]
        with QSignalBlocker(self.operations):
            self.operations.clear();self.operations.addItem("Choose recorded operation…","")
            for key in keys:
                files=("Model details not read in this exact recovery" if self._initial_operation_id is not None and key!=self._initial_operation_id else "Reading recorded model names…")
                label=self._operation_label(key,state["operations"][key],files)
                self.operations.addItem(label,key)
                self.operations.setItemData(self.operations.count()-1,f"{label}\nOperation: {key}",Qt.ToolTipRole)
            if selected in keys:self.operations.setCurrentIndex(self.operations.findData(selected))
        self.operation_page.setText(f"Operations {self._operation_offset+1:,}–{self._operation_offset+len(keys):,} of {len(self._operation_keys):,}; interrupted work first." if keys else "No recorded operations.")
        multiple=len(self._operation_keys)>RECOVERY_SUMMARY_PAGE_SIZE
        self.previous_operations.setVisible(multiple);self.next_operations.setVisible(multiple)
        self._operation_label_generation+=1
        generation=self._operation_label_generation;token=self._token
        read_keys=tuple(key for key in keys if self._initial_operation_id is None or key==self._initial_operation_id)
        self._pending_operation_labels=(read_keys,token,generation) if read_keys else None
        if start_labels and self._pending_operation_labels:
            self._load_operation_labels(*self._pending_operation_labels)
        self._update_actions()

    def _change_operation_page(self,direction):
        if self._task is not None or self._initial_operation_id is not None:return
        self._operation_offset+=direction*RECOVERY_SUMMARY_PAGE_SIZE
        self._clear_recovery()
        self._populate_operation_page(retain=False)

    def _load_operation_labels(self,keys,token,generation):
        if self._closing or self._task is not None or generation!=self._operation_label_generation or token!=self._token:return
        self._pending_operation_labels=None
        previous_status=self.status.text()
        def complete(summaries):
            if generation!=self._operation_label_generation or token!=self._token:return
            state=cache_state(self._snapshot)
            with QSignalBlocker(self.operations):
                for summary in summaries:
                    index=self.operations.findData(summary.operation_id)
                    if index<0:continue
                    names=[ntpath.basename(path.replace("/","\\")) for path in summary.archive_paths]
                    model_names=_short_label(", ".join(names[:3]),"Model filenames unknown")
                    if len(model_names)>70:model_names=model_names[:69]+"…"
                    files=(model_names+f" · {len(names):,} {'file' if len(names)==1 else 'files'}") if names else summary.message
                    label=self._operation_label(summary.operation_id,state["operations"][summary.operation_id],files)
                    self.operations.setItemText(index,label)
                    self.operations.setItemData(index,f"{label}\nRecorded models: {', '.join(names)}\nOperation: {summary.operation_id}",Qt.ToolTipRole)
            if self.status.text()=="Reading recorded operation names":
                self.status.setText(previous_status)
        self._run("Reading recorded operation names",lambda job:self.service.recovery_summaries(keys,expected_registry=token,should_cancel=lambda:job.cancel_requested),complete)

    def _hold_exact_recovery(self, reason):
        with QSignalBlocker(self.operations):
            self.operations.setCurrentIndex(0)
        self._exact_recovery_hold = (
            f"Recovery held: {reason} No other operation was selected. "
            "Return to the saved import to check its recorded state, or reload this exact operation.")
        self._recovery = None
        self.recovery_summary.setText(self._exact_recovery_hold)
        self.status.setText(self._exact_recovery_hold)

    def _select_exact_recovery(self, state):
        """Navigation only; the native preview remains the recovery authority."""
        key = self._initial_operation_id
        self._exact_recovery_hold = ""
        reason = ""
        if not isinstance(key, str) or re.fullmatch(r"[0-9a-f]{32}", key) is None:
            reason = "the requested operation ID is malformed."
        else:
            operation = state["operations"].get(key)
            if operation is None:
                reason = "the requested operation is not recorded here."
            elif operation.get("phase") == "complete":
                reason = "the requested operation is already recorded complete; no recovery is needed."
            elif operation.get("kind") != "prepare" or operation.get("phase") != "prepared":
                reason = "the requested operation has unsupported recorded state."
        with QSignalBlocker(self.operations):
            self.operations.setCurrentIndex(0 if reason else self.operations.findData(key))
        if reason:
            self._hold_exact_recovery(reason)
        else:
            self._clear_recovery()
            self.recovery_summary.setText(
                "The exact interrupted operation from the saved import is selected. "
                "Review its recorded progress and files before deciding whether to resume. No files changed.")

    def _choices_changed(self, *_):
        if not hasattr(self, "close_button"): return
        self._preview = None; self.review_summary.setText("Archive choices changed. Preview Preparation again.")
        self.capacity_details.clear(); self.capacity_details.hide()
        self.apply_button.setText("Prepare Reviewed Copies")
        self.rows.clear(); self._update_actions()

    def _check_files(self):
        if self._task is not None: return
        for item, *_ in self._sources.values(): item.setCheckState(0, Qt.Checked)

    def _requirements(self):
        selected = []
        for identity, (item, source, declaration, media) in self._sources.items():
            if item.checkState(0) != Qt.Checked: continue
            if not source.currentData() or not declaration.currentData() or not media.currentData():
                raise ModelResourceError("Choose an exact archive copy, recorded Model Type / Family and media for every checked model.")
            role, family = declaration.currentData()
            selected.append(CacheResource(identity, Path(source.currentData()), role, family, media.currentData()))
        return ModelRequirementSet(tuple(selected))

    def _preview_files(self):
        if self._task is not None or not self._policy_current(): return
        try: requirements = self._requirements()
        except ModelResourceError as exc: self._failed(str(exc)); self._update_actions(); return
        self.preview_requirements(requirements)

    def preview_requirements(self, requirements):
        """Consumer-supplied sets still require the service's trusted resolver."""
        if self._task is not None or self._token is None or not self._policy_current(): return
        self._invalidate(); token = self._token
        self._run("Reviewing Fast copy preparation", lambda job: self.service.preview_prepare(requirements,
            expected_registry=token, should_cancel=lambda: job.cancel_requested, progress=self._progress(job)), self._previewed)

    def _previewed(self, preview):
        self._preview = preview; self.rows.clear()
        for row in preview.rows:
            size = _bytes(row.inspection.size) if row.inspection else "Size unavailable"
            self._item(self.rows, (row.resource.archive_path, row.destination or "Unavailable", f"{row.status}: {row.reason}\n{size}"))
        fixed_cap = getattr(preview, "cache_limit_mode", "fixed") == "fixed"
        cap_label = _bytes(preview.cache_limit_bytes) if fixed_cap else "No fixed cache cap"
        remaining = (f"Remaining configured capacity: {_bytes(max(0, preview.cache_limit_bytes - preview.charged_bytes))}. "
                     if fixed_cap else "No fixed cap; actual SSD free space still limits copies. ")
        self.review_summary.setText(f"Required new space: {_bytes(preview.new_bytes)} · Charged: {_bytes(preview.charged_bytes)} · Capacity: {cap_label}\n"
            f"{remaining}Free disk space and your reserve are checked for new copies.\n{preview.message}")
        from .model_capacity_review import capacity_review_text
        capacity_text = capacity_review_text(getattr(preview, "capacity", None))
        self.capacity_details.setPlainText(capacity_text); self.capacity_details.setVisible(bool(capacity_text))
        removes_copies = bool(getattr(getattr(preview, "capacity", None), "victims", ()))
        self.apply_button.setText("Make Room and Prepare Reviewed Set" if removes_copies and preview.can_prepare else "Prepare Reviewed Copies")
        self.tabs.setCurrentIndex(1); self.status.setText("Preview only. Review every destination and hold before preparing the entire set.")

    def _apply_prepare(self):
        if self._task is not None or self._preview is None or not self._policy_current(): return
        preview = self._preview
        if not preview.rows or not preview.can_prepare: return
        self._invalidate(); self.changed = True
        self._run("Preparing reviewed Fast copies", lambda job: (self.service.apply_prepare(preview, confirmed=True,
            should_cancel=lambda: job.cancel_requested, progress=self._progress(job)), self.registry.snapshot(), "prepare"), self._applied)

    def _applied(self, result):
        outcome, snapshot, kind = result
        recommendation = self._pending_cleanup_recommendation
        self._pending_cleanup_recommendation = None
        state = cache_state(snapshot[0])
        operation = state["operations"].get(outcome.operation_id, {})
        self._loaded(snapshot); self.outcomes += (outcome,)
        if operation.get("kind") == "cleanup":
            retired = set(operation.get("retired", ()))
            reviewed = tuple(operation.get("entry_ids", ()))
            reclaimed = sum(state["entries"][key]["size"] for key in retired)
            remaining = len(reviewed) - len(retired)
            protected = (f" {recommendation.protected_count:,} protected copies remain outside this review."
                         if recommendation is not None else " Other Fast copies remain unchanged.")
            self.cleanup_summary.setText(
                f"{len(retired):,} of {len(reviewed):,} reviewed Fast copies removed; "
                f"{_bytes(reclaimed)} reclaimed. {remaining:,} reviewed copies remain. "
                "Archive originals remain registered and in place." + protected)
            self.recommended_entries.clear()
            self.recommended_entries.setHeaderLabels(("Model / Fast copy", "Archive protection", "Result", "Size"))
            for key in reviewed:
                entry = state["entries"][key]
                self._item(self.recommended_entries, (
                    Path(entry["archive_path"]).name, "Original verified and retained",
                    "Removed" if key in retired else "Retained", _bytes(entry["size"])), key)
                row = self.recommended_entries.topLevelItem(self.recommended_entries.topLevelItemCount() - 1)
                row.setToolTip(0, entry["path"]); row.setToolTip(1, entry["archive_path"])
            self.recommended_entries.setVisible(bool(reviewed) and not self.customize_cleanup.isChecked())
            self.cleanup_exact_toggle.show()
            self.cleanup_details.setPlainText(
                f"Recorded cleanup operation: {outcome.operation_id}\n{outcome.message}\n"
                + self._entry_paths(reviewed))
            self.cleanup_result_recovery.setVisible(outcome.status != "complete")
            self.tabs.setCurrentIndex(3)
            self.status.setText(
                "Cleanup complete and recorded. Only reviewed Fast copies were removed; Archive originals remain."
                if outcome.status == "complete" else
                "Cleanup remains held. Review this exact recorded operation before another cleanup.")
            return
        self.rows.setHeaderLabels(("Model", "Model Type / Family", "Result"))
        exact = [f"Operation: {outcome.operation_id}", f"Manual pin: {outcome.manual_pin_id or 'None'}",
                 f"State: {outcome.status}", f"Qualification: {outcome.qualification}", outcome.message]
        for binding in outcome.bindings:
            record = snapshot[0]["resources"].get(binding.resource_id)
            entry = state["entries"].get(binding.entry_id, {})
            # New Fast locations have hash filenames: identify the selected
            # Archive, not every registered copy, without mutating the record.
            label_record = {**(record or {}), "locations": [{"path": entry.get("archive_path") or binding.path}]}
            name = _record_label(label_record)
            filename = _short_label(ntpath.basename(str(entry.get("archive_path") or binding.path).replace("/", "\\")), "Model filename unknown")
            label = name if name == filename else f"{name}\n{filename}"
            self._item(self.rows, (label, f"{binding.role} / {binding.family}",
                                  f"Recorded Fast copy · {binding.media_type}"), binding.resource_id)
            row = self.rows.topLevelItem(self.rows.topLevelItemCount() - 1)
            row.setToolTip(0, f"{name}\nResource: {binding.resource_id}")
            row.setToolTip(2, str(binding.path))
            exact.extend(("", f"Model: {name}", f"Resource: {binding.resource_id}",
                          f"Cache entry: {binding.entry_id}", f"Archive: {entry.get('archive_path') or 'Not recorded'}",
                          f"Fast: {binding.path}", f"Type / Family / Media: {binding.role} / {binding.family} / {binding.media_type}"))
        completeness = "Exact adapter set verified" if outcome.qualification == "adapter_qualified" else "Complete companions not verified (manual set)"
        headline = "Fast copies prepared" if outcome.status == "complete" else "Preparation needs attention"
        pin_notice = "Manual pin recorded; release it only after model use finishes." if outcome.manual_pin_id else "No manual pin was recorded."
        self.review_summary.setText(f"{headline} · {len(outcome.bindings):,} recorded copies\n"
                                    f"Archive originals remain in place. {pin_notice}\n{completeness}")
        self.preparation_details.setPlainText("\n".join(exact))
        self.preparation_exact_toggle.show()
        self.tabs.setCurrentIndex(1)
        self.status.setText(
            "Back to Models to continue. Model execution still needs its own readiness check."
            if outcome.status == "complete" else
            "Operation remains held. Review its recorded result, then open Recovery for the exact remaining work.")
        if kind == "prepare" and outcome.status == "complete" and outcome.operation_id not in self.prepared_operation_ids:
            self.prepared_operation_ids += (outcome.operation_id,); self.prepared.emit(outcome.operation_id)

    def _clear_pin(self, *_):
        self._pin_preview = None; self.external_finished.setChecked(False); self.pin_details.clear(); self._update_actions()

    def _preview_pin(self):
        if self._task is not None or not self.pins.currentData() or not self._policy_current(): return
        pin = self.pins.currentData(); self._invalidate()
        def preview_pin(_job):
            preview = self.lease_service.preview_release(pin)
            snapshot = self.registry.snapshot()
            if snapshot[1] != preview.registry_token: raise ModelResourceError("Pin state changed during review. Reload and review again.")
            return preview, snapshot
        self._run("Reviewing manual pin release", preview_pin, self._pin_reviewed)

    def _entry_paths(self, ids):
        entries = cache_state(self._snapshot)["entries"]
        return "\n".join(f"Fast: {entries.get(key, {}).get('path', 'Not yet recorded')}\nArchive: {entries.get(key, {}).get('archive_path', 'Not yet recorded')}\nEntry: {key}" for key in ids)

    def _pin_reviewed(self, result):
        preview, (self._snapshot, self._token) = result
        self._pin_preview = preview
        self.pin_details.setPlainText(f"Pin: {preview.pin_id}\n{preview.message}\n{self._entry_paths(preview.entry_ids)}")

    def _release_pin(self):
        if self._task is not None or self._pin_preview is None or not self.external_finished.isChecked() or not self._policy_current(): return
        preview = self._pin_preview; self._invalidate(); self.changed = True
        def release(_job):
            self.lease_service.release(preview, confirmed=True); return self.registry.snapshot()
        def released(result):
            self._loaded(result); self.status.setText("Manual pin released. Files remain in place; cleanup requires its own review.")
        self._run("Releasing reviewed manual pin", release, released)

    def _checked_entries(self):
        return tuple(self.entries.topLevelItem(i).data(0, Qt.UserRole) for i in range(self.entries.topLevelItemCount())
            if self.entries.topLevelItem(i).checkState(0) == Qt.Checked)

    def _clear_cleanup(self, *_):
        self._cleanup = self._cleanup_recommendation = None
        self.cleanup_details.clear(); self.recommended_entries.clear(); self.recommended_entries.hide()
        self.cleanup_exact_toggle.setChecked(False)
        self.cleanup_exact_toggle.hide()
        self.cleanup_summary.setText("Cleanup choices changed. Review the recommendation or checked copies again before deletion.")
        self._update_actions()

    def _toggle_cleanup_customization(self, checked):
        self.cleanup_advanced.setVisible(checked)
        self.recommended_entries.setVisible(not checked and self.recommended_entries.topLevelItemCount() > 0)

    def _toggle_cleanup_exact(self, checked):
        self.cleanup_details.setVisible(checked)

    def _preview_recommended_cleanup(self):
        if self._task is not None or self._token is None or not self._policy_current(): return
        token, target = self._token, self.reclaim_target.value() * 2**20
        self._clear_cleanup()
        self._run("Reviewing least-used eligible Fast copies",
            lambda job: self.service.recommend_cleanup(target, expected_registry=token,
                should_cancel=lambda: job.cancel_requested), self._recommended_cleanup)

    def _recommended_cleanup(self, result):
        recommendation, preview = result
        self._cleanup_recommendation, self._cleanup = recommendation, preview
        self.recommended_entries.clear()
        self.recommended_entries.setHeaderLabels(("Model / Fast copy", "Archive protection", "Known successful use", "Size"))
        entries = cache_state(self._snapshot)["entries"]
        for victim in recommendation.victims:
            entry = entries[victim.entry_id]
            self._item(self.recommended_entries, (
                Path(entry["archive_path"]).name, "Original independently verified",
                f"{victim.successful_use_count:,} · last {victim.last_success_at[:10]}",
                _bytes(victim.bytes)), victim.entry_id)
            row = self.recommended_entries.topLevelItem(self.recommended_entries.topLevelItemCount() - 1)
            row.setToolTip(0, str(victim.path)); row.setToolTip(1, str(entry["archive_path"]))
        self.recommended_entries.setVisible(bool(recommendation.victims) and not self.customize_cleanup.isChecked())
        self.cleanup_summary.setText(
            f"Need: {_bytes(recommendation.target_bytes)}. "
            f"Safe to reclaim: {_bytes(recommendation.reclaimable_bytes)} from "
            f"{len(recommendation.victims):,} reviewed Fast cop{'y' if len(recommendation.victims) == 1 else 'ies'}.\n"
            f"{recommendation.protected_count:,} protected copies ({_bytes(recommendation.protected_bytes)}) remain; "
            "Archive originals stay in place."
            + ("\n" + recommendation.message if not recommendation.ready else ""))
        detail = self._entry_paths(recommendation.entry_ids) if recommendation.victims else "No recommended victims."
        detail += "\n" + recommendation.message
        if recommendation.exclusions:
            detail += "\nExcluded from recommendation:\n" + "\n".join(
                f"{row.path}: {row.reason}" for row in recommendation.exclusions)
        self.cleanup_details.setPlainText(detail)
        self.cleanup_exact_toggle.setVisible(bool(preview or recommendation.exclusions))
        self.status.setText(
            "Recommendation reviewed; no files changed. Confirm only the exact listed Fast copies, or customize the cleanup."
            if preview is not None else
            "No safe least-used cleanup is ready. No files changed; adjust the target or customize individual review.")
        self._update_actions()

    def _preview_cleanup(self):
        if self._task is not None or not self._policy_current(): return
        ids = self._checked_entries()
        if not 1 <= len(ids) <= MAX_MEMBERS: return
        token = self._token; self._invalidate()
        self._run("Reviewing exact Fast copy cleanup", lambda job: self.service.preview_cleanup(ids,
            expected_registry=token, should_cancel=lambda: job.cancel_requested), self._cleanup_reviewed)

    def _cleanup_reviewed(self, preview):
        self._cleanup = preview; self._cleanup_recommendation = None
        self.cleanup_details.setPlainText(f"Reclaimable: {_bytes(preview.reclaimable_bytes)}\n{preview.message}\n{self._entry_paths(preview.entry_ids)}")
        self.cleanup_exact_toggle.show()
        self.cleanup_summary.setText(
            f"Manual review: {len(preview.entry_ids):,} exact receipt-owned Fast copies, "
            f"{_bytes(preview.reclaimable_bytes)} reclaimable. Verified Archive originals remain. "
            "Delete only after checking the exact files and consequences below.")
        self.cleanup_exact_toggle.setChecked(True)

    def _apply_cleanup(self):
        if self._task is not None or self._cleanup is None or not self._policy_current(): return
        preview = self._cleanup
        self._pending_cleanup_recommendation = self._cleanup_recommendation
        self._invalidate(); self.changed = True
        self._run("Deleting reviewed receipt-owned Fast copies", lambda job: (self.service.apply_cleanup(preview,
            confirmed=True, should_cancel=lambda: job.cancel_requested), self.registry.snapshot(), "cleanup"), self._applied)

    def _clear_recovery(self, *_):
        self._recovery = None; self.recovery_details.clear()
        if self._initial_operation_id is not None and self.operations.currentData() != self._initial_operation_id:
            self._hold_exact_recovery("the selection does not match the requested operation.")
        elif self.operations.currentData():
            self.recovery_summary.setText("Review this recorded operation to see completed work, remaining work, and the safe next action.")
        else:
            self.recovery_summary.setText("Choose a recorded operation to review before any recovery action.")
        self._update_actions()

    def _toggle_recovery_exact(self, expanded):
        self.recovery_details.setVisible(bool(expanded))

    def _recovery_progress_summary(self, preview):
        operation = cache_state(self._snapshot)["operations"].get(preview.operation_id, {})
        if operation.get("phase") == "complete":
            return "This operation is already recorded complete. No further file work is needed."
        if preview.kind == "prepare":
            members = operation.get("members")
            if not isinstance(members, list):
                return "Interrupted Fast-copy preparation. Exact completed-file counts are unavailable; review the receipt before resuming."
            completed = sum(isinstance(member, dict) and member.get("phase") in {"published", "available"}
                            for member in members)
            total = len(members) or (len(preview.paths) if preview.capacity is None else 0)
            if total:
                return (f"Interrupted Fast-copy preparation: {completed:,} of {total:,} copies recorded complete; "
                        f"{total - completed:,} still need recovery review. Archive originals remain protected.")
            return "Interrupted Fast-copy preparation. No completed copies are recorded; review exact files before resuming."
        retired = operation.get("retired")
        if not isinstance(retired, list):
            return "Interrupted Fast-copy cleanup. Exact removed-file counts are unavailable; review the receipt before resuming."
        total = len(preview.paths)
        completed = min(len(retired), total)
        return (f"Interrupted Fast-copy cleanup: {completed:,} of {total:,} reviewed copies recorded removed; "
                f"{total - completed:,} still need recovery review. Archive originals remain protected.")

    def _preview_recovery(self):
        if self._task is not None or not self.operations.currentData() or not self._policy_current(): return
        if self._initial_operation_id is not None and (
                self._exact_recovery_hold or self.operations.currentData() != self._initial_operation_id):
            self._hold_exact_recovery("the selection does not match the requested operation.")
            self._update_actions(); return
        operation = self.operations.currentData(); self._invalidate()
        self._run("Reviewing exact cache recovery", lambda job: self.service.preview_recovery(operation,
            should_cancel=lambda: job.cancel_requested), self._recovery_reviewed)

    def _recovery_reviewed(self, preview):
        if self._initial_operation_id is not None and preview.operation_id != self._initial_operation_id:
            self._hold_exact_recovery("the recovery review names a different operation.")
            return
        # Service-owned validated baseline projection is required for exact review.
        if self._token != preview.registry_token:
            self._recovery = None
            self.recovery_summary.setText("Recorded state changed during review. Reload and review this operation again before resuming.")
            self.status.setText("Recorded state changed. Reload Recorded State, then review the exact operation again.")
            return
        paths = getattr(preview, "paths", ())
        self.recovery_summary.setText(self._recovery_progress_summary(preview))
        self.recovery_details.setPlainText(f"Operation: {preview.operation_id}\nKind: {preview.kind}\n{preview.message}\n"
            + "\n\n".join(f"Resource: {identity}\nArchive original (retained): {archive}\nFast copy: {fast}" for identity, archive, fast in paths))
        from .model_capacity_review import capacity_review_text
        capacity_text = capacity_review_text(getattr(preview, "capacity", None), retained=True)
        if capacity_text: self.recovery_details.appendPlainText("\n" + capacity_text)
        if not paths:
            self.recovery_summary.setText("Exact original paths are unavailable. Recovery cannot be resumed here; preserve this operation for qualified review.")
            self.recovery_details.appendPlainText("Exact original path review is unavailable. Preserve this operation for its qualified recovery interface.")
            self._recovery = None
        else: self._recovery = preview
        self.tabs.setCurrentIndex(4)
        self.status.setText(
            "Exact recovery reviewed; no files changed. Resume only this recorded remaining work, or leave it held."
            if self._recovery is not None else
            "Recovery remains unavailable. Preserve the recorded operation for qualified review.")
        if self._task is None and self._pending_operation_labels:
            self._load_operation_labels(*self._pending_operation_labels)

    def _recover(self):
        if self._task is not None or self._recovery is None or not self._policy_current(): return
        if self._initial_operation_id is not None and (
                self._exact_recovery_hold or self._recovery.operation_id != self._initial_operation_id
                or self.operations.currentData() != self._initial_operation_id):
            self._hold_exact_recovery("the recovery review no longer matches the requested operation.")
            self._update_actions(); return
        preview = self._recovery; self._invalidate(); self.changed = True
        self.recovery_summary.setText("Resuming only the reviewed operation at its recorded paths…")
        self._run("Recovering reviewed cache operation", lambda job: (self.service.recover(preview, confirmed=True,
            should_cancel=lambda: job.cancel_requested, progress=self._progress(job)), self.registry.snapshot(), preview.kind), self._applied)

    def _result_recovery(self):
        if self._task is not None or not self.outcomes: return
        operation = self.outcomes[-1].operation_id
        index = self.operations.findData(operation)
        if index<0 and operation in self._operation_keys:
            self._operation_offset=(self._operation_keys.index(operation)//RECOVERY_SUMMARY_PAGE_SIZE)*RECOVERY_SUMMARY_PAGE_SIZE
            self._populate_operation_page(retain=False,start_labels=False)
            index=self.operations.findData(operation)
        if index < 0: self.status.setText("Reload Recorded State before reviewing this operation."); return
        self.operations.setCurrentIndex(index); self._preview_recovery()

    def _setup(self):
        if self._task is not None or self.setup_folders is None: return
        self._invalidate()
        try: self.setup_folders()
        except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc)); self._update_actions(); return
        self._load()

    def _stop(self):
        if self._task is not None and self._task.job_id:
            self.jobs.cancel(self._task.job_id)
            self.status.setText("Stopping at the next safe boundary. Keep this window open until work settles.")

    def request_parent_close(self, continuation):
        pending = self._task is not None
        self._close_parent = continuation if pending else None
        self.reject(); return not pending

    def reject(self):
        if self._task is not None:
            self._closing = True; self._stop(); return
        super().reject()

    def closeEvent(self, event):
        if self._task is not None:
            self.reject(); event.ignore()
        else: super().closeEvent(event)
