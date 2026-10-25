"""Successful-use preferences and advisory placement share canonical model state."""
from __future__ import annotations

import shutil
from pathlib import Path

from PySide6.QtCore import Qt, QSignalBlocker, Signal
from PySide6.QtWidgets import (QCheckBox, QComboBox, QDialog, QHBoxLayout, QLabel,
    QPlainTextEdit, QPushButton, QTabWidget, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget)

from core.model_storage_policy import load_policy, save_policy
from core.model_storage_recommendations import known_placement_requirements, recommend_placements
from core.model_usage import ModelUsageService
from core.model_workspace import _path
from core.training_source_io import _assert_unredirected


def _size(value):
    if value is None: return "Unknown"
    for divisor, label in ((1024**3, "GiB"), (1024**2, "MiB"), (1024, "KiB")):
        if value >= divisor: return f"{value / divisor:,.2f} {label}"
    return f"{value:,} bytes"


def _free_space(snapshot, *, protected_roots=()):
    policy = load_policy(snapshot.get("workspace", {}))
    if policy.mode != "tiered": return None
    path = _path(policy.fast_root, protected_roots)
    _assert_unredirected(path)
    free = shutil.disk_usage(path).free
    _assert_unredirected(path)
    return free


class ModelUsageDialog(QDialog):
    prepare_requested = Signal(str)
    folders_requested = Signal()
    background_work_finished = Signal()

    def __init__(self, registry, jobs, parent=None, *, service=None, space_reader=None, policy_writer=None):
        super().__init__(parent)
        self.registry, self.jobs = registry, jobs
        self.service = service if service is not None else ModelUsageService(registry)
        self.space_reader = space_reader if space_reader is not None else _free_space
        self.policy_writer = policy_writer if policy_writer is not None else lambda changes, token: save_policy(
            registry, changes, expected_registry=token)
        self._task = self._review = self._summary = self._snapshot = None
        self._status_error = False
        self._placement_status = self._history_status = ""
        self._save_notice = None
        self._closing = self._closed = False
        self._close_parent = None
        self.parent_closing = False
        self.changed = False
        self.setWindowTitle("Model Usage and Placement"); self.resize(1200, 840)
        self.setWindowModality(Qt.WindowModal)
        layout = QVBoxLayout(self)
        layout.addWidget(self._label("Review Fast-storage placement, or choose whether to remember successful model use. Nothing changes until you confirm a reviewed choice."))
        self.tasks = QTabWidget()
        self.placement_task = QWidget()
        self.history_task = QWidget()
        self.tasks.addTab(self.placement_task, "Placement and Retention")
        self.tasks.addTab(self.history_task, "Successful-use History")
        self.tasks.currentChanged.connect(self._actions)
        layout.addWidget(self.tasks, 1)
        placement = QVBoxLayout(self.placement_task)
        placement.addWidget(self._label("Suggestions use favorites and verified successful runs. Review one model's complete set before changing retention."))
        history = QVBoxLayout(self.history_task)
        self.remember = QCheckBox("Remember Successful Model Use")
        self.remember.toggled.connect(lambda: self._invalidate("history"))
        history.addWidget(self.remember)
        history.addWidget(self._label("Optional and local: records exact model identities, successful-use counts and dates, not prompts or images. Scans, selections and failed runs do not count; outside use stays unknown."))
        preferences = QHBoxLayout()
        self.review_setting = self._button("Review Tracking Change", lambda: self._review_change("tracking"))
        self.review_reset = self._button("Review History Reset…", lambda: self._review_change("reset"))
        for button in (self.review_setting, self.review_reset): preferences.addWidget(button)
        preferences.addStretch(1); history.addLayout(preferences)
        history.addStretch(1)
        self.preference_review = QPlainTextEdit(); self.preference_review.setReadOnly(True)
        self.preference_review.setMaximumHeight(105)
        self.preference_review.setAccessibleName("Reviewed tracking preference or history reset")
        self.review_label = self._label("Review this change before confirming")
        self.review_notice = self._label("")
        layout.addWidget(self.review_notice)
        layout.addWidget(self.review_label)
        layout.addWidget(self.preference_review)
        review_action = QHBoxLayout(); review_action.addStretch(1)
        self.discard_review = self._button("Discard This Review", lambda: self._invalidate())
        self.confirm = self._button("Confirm Reviewed Change", self._apply_change)
        review_action.addWidget(self.discard_review)
        review_action.addWidget(self.confirm); layout.addLayout(review_action)
        controls = QHBoxLayout()
        self.refresh = self._button("Refresh Suggestions", self.reload)
        self.measure = self._button("Check Fast Free Space", lambda: self.reload(measure=True))
        self.folders = self._button("Setup Storage Folders…", self.folders_requested.emit)
        for button in (self.refresh, self.measure, self.folders): controls.addWidget(button)
        controls.addStretch(1); placement.addLayout(controls)
        self.capacity = self._label("Reading saved model state…"); placement.addWidget(self.capacity)
        self.rows = QTreeWidget(); self.rows.setColumnCount(5)
        self.rows.setHeaderLabels(("Model", "Recorded successes", "Favorite", "Placement review", "New space"))
        self.rows.setRootIsDecorated(False)
        self.rows.setColumnWidth(0, 320); self.rows.setColumnWidth(1, 175)
        self.rows.setColumnWidth(2, 110); self.rows.setColumnWidth(3, 240)
        self.rows.currentItemChanged.connect(self._selection_changed)
        placement.addWidget(self.rows, 1)
        retention = QHBoxLayout()
        retention.addWidget(QLabel("Selected model retention"))
        self.retention = QComboBox()
        for label, value in (("Use family / type / default policy", "inherit"),
                ("Keep on Fast storage (keep warm)", "keep_fast"),
                ("Prepare when needed; allow reviewed cleanup", "on_demand"),
                ("Keep in Archive until explicitly prepared", "archive_only")):
            self.retention.addItem(label, value)
        self.retention.currentIndexChanged.connect(lambda: self._invalidate("retention"))
        retention.addWidget(self.retention, 1)
        placement.addLayout(retention)
        retention = QHBoxLayout()
        self.companions = QCheckBox("Include known required companions")
        self.companions.setChecked(True); self.companions.toggled.connect(lambda: self._invalidate("retention"))
        retention.addWidget(self.companions)
        self.review_retention = self._button("Review Retention Change", self._review_retention)
        retention.addStretch(1); retention.addWidget(self.review_retention); placement.addLayout(retention)
        placement.addWidget(self._label("Keep warm retains verified Fast copies and protects them from cleanup. Changing retention does not copy files or load GPU memory."))
        self.details_toggle = QPushButton("Show Selected Model Details")
        self.details_toggle.setCheckable(True)
        self.details_toggle.toggled.connect(self._toggle_details)
        placement.addWidget(self.details_toggle, 0, Qt.AlignLeft)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True)
        self.details.setAccessibleName("Placement reasons, complete resources and protections")
        self.details.setMaximumHeight(220)
        self.details.hide()
        placement.addWidget(self.details)
        self.status = self._label("No settings have changed."); layout.addWidget(self.status)
        footer = QHBoxLayout(); footer.addStretch(1)
        self.prepare = self._button("Review Selected Model for Use…", self._prepare)
        self.close_button = self._button("Close", self.reject)
        footer.addWidget(self.prepare); footer.addWidget(self.close_button); layout.addLayout(footer)
        self._actions(); self.reload()

    @staticmethod
    def _label(text):
        label = QLabel(text); label.setWordWrap(True); label.setTextFormat(Qt.PlainText); return label

    @staticmethod
    def _button(text, slot):
        button = QPushButton(text); button.clicked.connect(slot); return button

    def _active_task(self):
        return "retention" if self.tasks.currentWidget() is self.placement_task else "history"

    def _review_task(self):
        return "retention" if self._review and self._review[0] == "retention" else "history"

    def _invalidate(self, scope=None):
        if self._review is not None and (scope is None or scope == self._review_task()):
            self._review = None
            self.preference_review.clear()
        self._actions()

    def _toggle_details(self, visible):
        self.details.setVisible(visible)
        self.details_toggle.setText("Hide Selected Model Details" if visible else "Show Selected Model Details")

    def _actions(self):
        idle = self._task is None and not self._closing
        ready = idle and self._summary is not None
        visible_review = self._review is not None and self._active_task() == self._review_task()
        waiting_review = self._review is not None and not visible_review
        self.remember.setEnabled(ready)
        self.review_setting.setEnabled(ready and not waiting_review)
        self.review_reset.setVisible(bool(self._summary and
            (self._summary["retained_successes"] or self._summary["pending_count"])))
        self.review_reset.setEnabled(ready and not waiting_review)
        self.review_notice.setVisible(waiting_review)
        if waiting_review:
            destination = "Placement and Retention" if self._review_task() == "retention" else "Successful-use History"
            self.review_notice.setText(f"A reviewed change is waiting in {destination}. Return there to confirm or discard it before reviewing another change.")
        for widget in (self.review_label, self.preference_review, self.confirm, self.discard_review):
            widget.setVisible(visible_review)
        self.confirm.setEnabled(ready and visible_review)
        self.discard_review.setEnabled(ready and visible_review)
        if idle and self._summary is not None and not self._status_error:
            if self._active_task() == "retention":
                self.status.setText(self._placement_status)
            else:
                pending = (f"Unsaved tracking choice: {'On' if self.remember.isChecked() else 'Off'}. "
                    "Review Tracking Change to save. "
                    if self.remember.isChecked() != self._summary["enabled"] else "")
                self.status.setText(pending + self._history_status)
        for widget in (self.refresh, self.folders, self.rows): widget.setEnabled(idle)
        self.measure.setEnabled(ready and self._snapshot is not None
            and load_policy(self._snapshot.get("workspace", {})).mode == "tiered")
        self.details_toggle.setEnabled(ready and self.rows.currentItem() is not None)
        self.prepare.setVisible(self._active_task() == "retention")
        self.prepare.setEnabled(ready and self.rows.currentItem() is not None)
        for widget in (self.retention, self.companions):
            widget.setEnabled(ready and self.rows.currentItem() is not None)
        self.review_retention.setEnabled(ready and self.rows.currentItem() is not None and not waiting_review)

    def reload(self, *_args, measure=False, preserve_draft=None):
        if self._task is not None or self._closing: return
        self._status_error = False
        self._invalidate()
        def read(job):
            summary = self.service.summary()
            snapshot, token = self.registry.snapshot()
            if token != summary["registry_token"]:
                raise ValueError("Model state changed during this review. Refresh suggestions.")
            free, message = None, ""
            if measure:
                try: free = self.space_reader(snapshot)
                except (ValueError, RuntimeError, OSError):
                    message = "Fast free space is unavailable. Check the folder and retry."
            proposed = recommend_placements(snapshot, known_placement_requirements(snapshot), free_bytes=free)
            return snapshot, summary, proposed, message
        def loaded(value):
            self._loaded(value)
            if preserve_draft is not None:
                self._restore_draft(preserve_draft)
        self._run("Reviewing model usage and placement", read, loaded)

    def _draft_state(self):
        item = self.rows.currentItem()
        return (self.remember.isChecked(),
            item.data(0, Qt.UserRole)["resource_id"] if item is not None else None,
            self.retention.currentData(), self.companions.isChecked())

    def _restore_draft(self, draft):
        remember, resource_id, retention, companions = draft
        with QSignalBlocker(self.remember): self.remember.setChecked(remember)
        if resource_id is not None:
            for index in range(self.rows.topLevelItemCount()):
                item = self.rows.topLevelItem(index)
                if item.data(0, Qt.UserRole)["resource_id"] == resource_id:
                    self.rows.setCurrentItem(item)
                    with QSignalBlocker(self.retention):
                        self.retention.setCurrentIndex(self.retention.findData(retention))
                    with QSignalBlocker(self.companions): self.companions.setChecked(companions)
                    break

    def _loaded(self, value):
        self._snapshot, self._summary, proposed, message = value
        with QSignalBlocker(self.remember): self.remember.setChecked(self._summary["enabled"])
        self.rows.clear()
        labels = {"held": "Needs review", "in_place": "Use in place", "recorded_prepared": "Recorded Fast copy", "suggested": "Consider Fast storage"}
        for row in proposed["rows"]:
            item = QTreeWidgetItem((row["name"], str(row["successful_use_count"]) if row["last_success_at"] else "Unknown",
                "Yes" if row["favorite"] else "", labels[row["status"]], _size(row["additional_bytes"])))
            item.setData(0, Qt.UserRole, row); item.setToolTip(0, row["resource_id"])
            self.rows.addTopLevelItem(item)
        cap_label = (_size(proposed['cache_limit_bytes']) if proposed.get('cache_limit_mode', 'fixed') == 'fixed'
                     else 'No fixed cache cap')
        self.capacity.setText(f"Fast limit: {cap_label} · Recorded copies: {_size(proposed['charged_bytes'])} · "
            f"Proposed new copies: {_size(proposed['proposed_new_bytes'])} · Free: {_size(proposed['free_bytes'])} · Reserve: {_size(proposed['minimum_free_bytes'])}")
        self.details.setPlainText(proposed["message"])
        self._placement_status = message or (f"{len(proposed['rows'])} of {proposed['resource_count']} registered resources shown. "
            + self._summary["message"])
        self._history_status = (f"Tracking {'on' if self._summary['enabled'] else 'off'} · "
            f"{self._summary['retained_successes']} retained successful-run observations. " + self._summary["message"])
        if self._save_notice is not None:
            scope, notice = self._save_notice
            if scope == "retention": self._placement_status = notice + " " + self._placement_status
            else: self._history_status = notice + " " + self._history_status
            self._save_notice = None
        self.status.setText(self._placement_status if self._active_task() == "retention"
                            else self._history_status)

    def _selection_changed(self, *_):
        self._invalidate("retention")
        item = self.rows.currentItem()
        if item is None:
            self.details.clear()
            self.details_toggle.setChecked(False)
        if item is not None:
            row = item.data(0, Qt.UserRole)
            selected = dict(load_policy(self._snapshot.get("workspace", {})).resource_policies).get(row["resource_id"], "inherit")
            with QSignalBlocker(self.retention): self.retention.setCurrentIndex(self.retention.findData(selected))
            lines = [row["name"], row["ranking_reason"], row["reason"],
                "Complete companion set known: " + ("Yes" if row["complete"] else "No"),
                f"Active or manual protection: {'Yes' if row['pinned'] else 'No'} · Keep on Fast storage: {'Yes' if row['keep_fast'] else 'No'}",
                "Complete set: " + _size(row["total_bytes"])]
            for copy in row["copies"]:
                lines.append(f"{copy['destination']} · {_size(copy['size'])}")
            lines.append("Registry estimates only. Prepare for Use rechecks files, required companions and space before copying.")
            self.details.setPlainText("\n\n".join(lines))
        self._actions()

    def _review_retention(self):
        item = self.rows.currentItem()
        if self._task is not None or item is None: return
        row = item.data(0, Qt.UserRole)
        identities = row["required_ids"] if self.companions.isChecked() and row["complete"] else (row["resource_id"],)
        policy = dict(load_policy(self._snapshot.get("workspace", {})).resource_policies)
        choice = self.retention.currentData()
        lines = ["Review individual retention overrides:"]
        labels = {self.retention.itemData(i): self.retention.itemText(i) for i in range(self.retention.count())}
        for identity in identities:
            record = self._snapshot["resources"][identity]
            name = record.get("user", {}).get("display_name") or identity
            lines.append(f"{name}: {labels[policy.get(identity, 'inherit')]} → {labels[choice]}")
            if choice == "inherit": policy.pop(identity, None)
            else: policy[identity] = choice
        if self.companions.isChecked() and not row["complete"]:
            lines.append("Companion requirements are unknown; only this selected resource changes.")
        lines.append("No files are copied or deleted. Keep warm blocks cleanup between runs; removing it never releases an active or manual pin. Capacity limits still apply.")
        self._review = ("retention", {"resource_policies": policy}, self._summary["registry_token"])
        self.preference_review.setPlainText("\n".join(lines)); self._actions()

    def _review_change(self, kind):
        if self._task is not None or self._summary is None: return
        enabled = self.remember.isChecked()
        self._review = (kind, enabled, self._summary["registry_token"])
        if kind == "tracking":
            self.preference_review.setPlainText(f"Remember Successful Model Use: {'On' if self._summary['enabled'] else 'Off'} → {'On' if enabled else 'Off'}.\n"
                "Only future eligible runs are recorded. Disabling stops new observations and preserves saved counts. Pending older callbacks cannot add use after this change. No model files or pins change.")
        else:
            self.preference_review.setPlainText(f"Reset {self._summary['retained_successes']} retained successful-run observations. Tracking stays {'On' if self._summary['enabled'] else 'Off'}.\n"
                "Late callbacks cannot restore cleared history. Model files, favorites and pins remain. Existing registry recovery backups retain their normal history; this is not a secure erase.")
        self._actions()

    def _apply_change(self):
        if self._task is not None or self._review is None or self._active_task() != self._review_task(): return
        kind, enabled, token = self._review
        draft = self._draft_state()
        def write(job):
            if job.cancel_requested: raise InterruptedError("Preference change cancelled before writing.")
            if kind == "retention": self.policy_writer(enabled, token)
            elif kind == "tracking": self.service.set_enabled(enabled, expected_registry=token)
            else: self.service.reset(expected_registry=token)
        def saved(_):
            self.changed = True; self._review = None; self.preference_review.clear()
            self._save_notice = ("retention" if kind == "retention" else "history",
                "Retention preference saved." if kind == "retention" else
                "Successful-use tracking saved." if kind == "tracking" else
                "Successful-use history reset.")
            self.reload(preserve_draft=draft)
        self._run("Saving reviewed model usage preference", write, saved)

    def _prepare(self):
        item = self.rows.currentItem()
        if item is not None and self._task is None:
            self.prepare_requested.emit(item.data(0, Qt.UserRole)["resource_id"])

    def _run(self, title, work, completed):
        from .workers import CoordinatedCallTask
        from .job_progress import bind_job_progress
        task = CoordinatedCallTask(self.jobs, title, work, resource="io")
        self._task = task; self.status.setText(title + "…")
        binding = bind_job_progress(self, task, title=title)
        def finish(value=None, error=""):
            if self._task is not task: return
            self._task = None
            if error:
                self._review = None; self._status_error = True; self.status.setText(error)
            elif not self._closing:
                try: completed(value)
                except (ValueError, RuntimeError, OSError) as exc: self.status.setText(str(exc))
            if self._closing: self._finish_close()
            else: self._actions()
            self.background_work_finished.emit()
        task.signals.complete.connect(lambda value: finish(value))
        task.signals.failed.connect(lambda error: finish(error=error))
        task.signals.cancelled.connect(lambda: finish(error="Stopped safely. Refresh to review the current saved state."))
        self._actions()
        try: task.start()
        except Exception as exc: binding.finish(task); finish(error=str(exc))

    def has_active_background_work(self): return self._task is not None

    def request_parent_close(self, continuation):
        self.parent_closing = True
        pending = self._task is not None
        self._close_parent = continuation if pending else None
        self.reject(); return not pending

    def _finish_close(self):
        if self._closed or self._task is not None: return
        self._closed = True
        continuation, self._close_parent = self._close_parent, None
        QDialog.reject(self)
        if continuation: continuation()

    def reject(self):
        self._closing = True
        if self._task is not None:
            if self._task.job_id: self.jobs.cancel(self._task.job_id)
        else: self._finish_close()

    def closeEvent(self, event):
        if self._task is not None: self.reject(); event.ignore()
        else: super().closeEvent(event)
