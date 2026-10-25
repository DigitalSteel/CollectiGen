"""Explicit model preparation and exact-owner recovery for the Wan runtime.

Construction is presentation only. The parent owns runtime Start and job launch;
this dialog returns a freshly checked, unstarted bound preparation.
"""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from threading import Event
from uuid import uuid4

from PySide6.QtCore import Qt, Signal
from PySide6.QtWidgets import (QDialog, QHBoxLayout, QLabel, QLineEdit, QMessageBox,
    QPlainTextEdit, QPushButton, QVBoxLayout)

from core.comfy_video_lifecycle import ComfyVideoLifecycle, INTENT_NAME, LifecycleStatus, OWNER_ID, OWNER_VERSION
from core.comfy_video_resources import WanRuntimeResourceIssuer
from core.comfy_video_runtime import RuntimeConfig
from core.model_cache import ModelCacheService
from core.model_execution_cache import RECEIPT_NAME
from core.model_workspace import _path, _pinned_parent
from .model_capacity_review import capacity_review_text
from .model_companion_workflow import resource_boundaries
from .workers import CoordinatedCallTask


def runtime_storage_state_title(state):
    return {"unreserved": "Not Prepared", "prepared": "Copies Prepared",
        "bound": "Ready to Start", "submitted": "Runtime Started",
        "uncertain": "Needs Recovery", "recovery_required": "Needs Recovery",
        "stopped": "Stopped", "discarded": "Preparation Closed"}.get(state, "Review Required")


class VideoRuntimePreparationDialog(QDialog):
    background_work_finished = Signal()
    locator_changed = Signal(str)
    state_changed = Signal(object)

    def __init__(self, jobs, registry, settings, parent=None, *, config=None, locator=None,
                 storage_root=None, protected_roots=(), service_factory=None):
        super().__init__(parent)
        if (config is None) == (locator is None):
            raise ValueError("Choose a frozen runtime configuration or one retained owner locator.")
        if config is not None and not isinstance(config, RuntimeConfig):
            raise ValueError("Choose an exact runtime configuration.")
        if locator is not None and (not Path(locator).is_absolute() or Path(locator).name != INTENT_NAME or ".." in Path(locator).parts):
            raise ValueError("Choose the exact absolute retained runtime owner locator.")
        if config is not None and (storage_root is None or not Path(storage_root).is_absolute() or ".." in Path(storage_root).parts):
            raise ValueError("Choose an explicit owner-storage folder for this preparation.")
        self.jobs, self.registry, self.settings = jobs, registry, settings
        self.config = deepcopy(config)
        self.storage_root = Path(storage_root) if storage_root is not None else None
        self.protected_roots = tuple(protected_roots)
        self.service_factory = service_factory
        self.retained_locator = Path(locator) if locator is not None else None
        self.selected_lifecycle = self.selected_status = None
        self.selected_config = self._display_config = None
        self.current_status = None
        self._lifecycle = self._issuer = self._review = self._issued_review = None
        self._status = self._recovery_review = self._boundaries = None
        self._task = None
        self._cancel = Event()
        self._closing = self._shutdown_emitted = False
        self.setWindowTitle("Prepare Video Runtime Models")
        self.resize(1040, 800)
        self.setMinimumSize(800, 560)
        layout = QVBoxLayout(self)
        intro = QLabel("Review the exact Wan model, text encoder and VAE before preparing this runtime. "
            "Models remain protected for its whole lifetime, including idle time between clips. "
            "Runtime start is a separate action in the video workspace.")
        intro.setWordWrap(True); layout.addWidget(intro)
        self.state_label = QLabel("Not Reviewed"); self.state_label.setTextFormat(Qt.PlainText)
        layout.addWidget(self.state_label)
        self.cleanup_summary = QLabel(); self.cleanup_summary.setTextFormat(Qt.PlainText)
        self.cleanup_summary.setWordWrap(True); self.cleanup_summary.hide(); layout.addWidget(self.cleanup_summary)
        self.review_text = QPlainTextEdit(); self.review_text.setReadOnly(True)
        self.review_text.setAccessibleName("Video runtime models and storage review")
        self.review_text.setPlainText("Preview verifies the fixed recipe, source and selected registered files. "
            "It does not prepare copies or start a runtime." if config is not None else
            "Read Status checks only this retained owner. Recover reconciles its recorded work; it never adopts a running process or submits a new job.")
        layout.addWidget(self.review_text, 1)
        layout.addWidget(QLabel("Retained Preparation"))
        self.locator_field = QLineEdit(str(self.retained_locator or "")); self.locator_field.setReadOnly(True)
        self.locator_field.setAccessibleName("Exact retained video runtime owner locator")
        self.locator_field.setPlaceholderText("Assigned only when Prepare Models is confirmed")
        layout.addWidget(self.locator_field)
        self.status_label = QLabel("Choose Preview Models." if config is not None else "Choose Read Status.")
        self.status_label.setTextFormat(Qt.PlainText); self.status_label.setWordWrap(True); layout.addWidget(self.status_label)
        recovery = QHBoxLayout()
        self.read_status_button = QPushButton("Read Status"); self.read_status_button.clicked.connect(self.read_status)
        self.recover_button = QPushButton("Recover"); self.recover_button.clicked.connect(self.recover)
        self.discard_button = QPushButton("Discard Unstarted"); self.discard_button.clicked.connect(self.discard_unstarted)
        self.stop_button = QPushButton("Stop Owned Runtime"); self.stop_button.clicked.connect(self.stop_owned_runtime)
        for button in (self.read_status_button, self.recover_button, self.discard_button, self.stop_button): recovery.addWidget(button)
        recovery.addStretch(1); layout.addLayout(recovery)
        actions = QHBoxLayout()
        self.preview_button = QPushButton("Preview Models"); self.preview_button.clicked.connect(self.preview)
        self.prepare_button = QPushButton("Prepare Models"); self.prepare_button.clicked.connect(self.prepare)
        self.cancel_button = QPushButton("Cancel Safely"); self.cancel_button.clicked.connect(self.cancel_work)
        self.close_button = QPushButton("Close"); self.close_button.clicked.connect(self.reject)
        self.use_button = QPushButton("Use Prepared Models"); self.use_button.setProperty("primary", True)
        self.use_button.clicked.connect(self.use_prepared)
        for button in (self.preview_button, self.prepare_button, self.cancel_button): actions.addWidget(button)
        actions.addStretch(1); actions.addWidget(self.close_button); actions.addWidget(self.use_button)
        layout.addLayout(actions)
        self._actions()

    def _context(self):
        snapshot, token = self.registry.snapshot()
        protected, managed = resource_boundaries(snapshot, self.settings, self.protected_roots)
        return snapshot, token, (protected, managed)

    def _services(self, snapshot, boundaries):
        protected, managed = boundaries
        if self.service_factory is not None:
            return self.service_factory(snapshot, protected, managed)
        issuer = WanRuntimeResourceIssuer(self.registry, protected_roots=protected)
        lifecycle = ComfyVideoLifecycle(self.registry, requirement_resolver=issuer.resolver(
            should_cancel=self._cancel.is_set), protected_roots=protected, managed_roots=managed)
        return issuer, lifecycle

    def _check_boundaries(self):
        snapshot, token, boundaries = self._context()
        if self._boundaries != boundaries:
            raise ValueError("Library or managed model boundaries changed. Close and reopen this preparation before continuing.")
        return snapshot, token

    def _progress(self, job):
        def report(current, total, message):
            unit = 1024 * 1024
            job.update((current + unit - 1) // unit, (total + unit - 1) // unit if total else 0, message)
        return report

    def preview(self):
        if self._task is not None or self._closing or self.config is None or self.retained_locator is not None: return
        self._review = self._issued_review = None
        def work(job):
            snapshot, token, boundaries = self._context()
            issuer, lifecycle = self._services(snapshot, boundaries)
            issued = issuer.issue(self.config, expected_registry=token, should_cancel=self._cancel.is_set, progress=self._progress(job))
            review = lifecycle.preview(issued.source_preview.config, issued.requirements,
                should_cancel=self._cancel.is_set, progress=self._progress(job))
            if self._cancel.is_set(): raise InterruptedError("Preview cancelled. No model preparation was started.")
            return issuer, lifecycle, issued, review, boundaries
        def done(result):
            self._issuer, self._lifecycle, self._issued_review, self._review, self._boundaries = result
            self._show_preview()
        self._run("Reviewing Video Runtime Models", work, done)

    def _show_preview(self):
        review = self._review; source = review.source_preview.config
        lines = [review.message, "Runtime source: " + str(source.source_root),
            "Python: " + str(source.python_executable), "Future runtime workspace: " + str(source.workspace_root),
            "Owner storage: " + str(self.storage_root), "Loopback port: " + str(source.port)]
        for resource in self._issued_review.requirements.resources:
            lines.append(f"{resource.role} · {resource.resource_id}\nArchive original: {resource.archive_path}")
        cache = review.cache_preview
        capacity = getattr(cache, "capacity", None)
        if cache is None:
            lines.append("Single-location use: the selected originals stay in place. No Fast copies are created or removed.")
        else:
            lines.extend((cache.message, f"New copy bytes: {cache.new_bytes:,}"))
            for row in cache.rows:
                lines.append(f"{row.resource.archive_path} → {row.destination or 'Unavailable destination'}\n{row.status} · {row.reason}")
            if capacity is not None: lines.append(capacity_review_text(capacity))
        self._show_cleanup(capacity)
        self.review_text.setPlainText("\n\n".join(lines))
        self.state_label.setText("Ready To Prepare" if review.can_prepare else "Preparation Held")
        self.status_label.setText("Review the exact copies and cleanup consequences, then Prepare Models." if review.can_prepare else review.message)

    def _show_cleanup(self, capacity, *, retained=False):
        victims = getattr(capacity, "victims", ())
        self.cleanup_summary.setText(f"{'Recorded' if retained else 'Reviewed'} Fast copies to remove: {len(victims)}. "
            "Archive originals remain. Removed Fast copies are not automatically restored if this work is cancelled.")
        self.cleanup_summary.setVisible(bool(victims))

    def _confirm(self, title, message):
        box = QMessageBox(self); box.setWindowTitle(title); box.setIcon(QMessageBox.Question)
        box.setTextFormat(Qt.PlainText); box.setText(message)
        box.setStandardButtons(QMessageBox.Ok | QMessageBox.Cancel); box.setDefaultButton(QMessageBox.Cancel)
        return box.exec() == QMessageBox.Ok

    def prepare(self):
        if self._task is not None or self._closing or self._review is None or not self._review.can_prepare or self.retained_locator is not None: return
        review = self._review
        victims = getattr(getattr(review.cache_preview, "capacity", None), "victims", ())
        message = (f"Prepare the reviewed three-model set and reserve it for this dedicated runtime?\n\n"
            f"Reviewed Fast copies to remove: {len(victims)}. Archive originals remain. "
            "Removed Fast copies are not automatically restored after cancellation.\n\n"
            "The reservation remains until exact owned stop or explicit unstarted discard. No runtime starts here.")
        if not self._confirm("Prepare Video Models?", message): return
        self.retained_locator = self.storage_root / ("video-owner-" + uuid4().hex) / INTENT_NAME
        self.locator_field.setText(str(self.retained_locator)); self.locator_changed.emit(str(self.retained_locator))
        self._status = None
        def work(job):
            self._check_boundaries()
            boundaries = (*self._boundaries[0], *self._boundaries[1])
            # Protect before any filesystem probe; require the parent-owned
            # container to exist so this child creates only its unique intent.
            container = _path(self.storage_root, boundaries)
            owner = _path(self.retained_locator.parent, boundaries)
            workspace = Path(review.source_preview.config.workspace_root)
            if workspace == owner or workspace in owner.parents or owner in workspace.parents:
                raise ValueError("Choose separate owner-storage and runtime-workspace folders.")
            if self._cancel.is_set(): raise InterruptedError("Preparation cancelled before reserving models.")
            with _pinned_parent(container, boundaries):
                owner.mkdir(exist_ok=False)
                status = self._lifecycle.prepare(review, owner, confirmed=True,
                    should_cancel=self._cancel.is_set, progress=self._progress(job))
                return status, self._bound_config(self._lifecycle, status)
        self._run("Preparing Video Runtime Models", work,
            lambda result: self._show_status(result[0], config=result[1]))

    def _bound_config(self, lifecycle, status):
        if not self._usable(status): return None
        config = lifecycle.bound_config(status.locator)
        if not isinstance(config, RuntimeConfig) or not config.protected_loader or lifecycle.status(status.locator) != status:
            raise ValueError("The bound runtime configuration changed. Read Status again before using it.")
        return config

    def _read(self, lifecycle, *, should_cancel=None):
        status = lifecycle.status(self.retained_locator)
        recovery = None
        if status.state == "recovery_required":
            held = lifecycle.cache.status(self.retained_locator.parent / RECEIPT_NAME)
            service = ModelCacheService(self.registry, protected_roots=lifecycle.protected_roots,
                managed_roots=lifecycle.managed_roots)
            recovery = service.preview_recovery(held.operation_id, should_cancel=should_cancel)
        return status, recovery

    def read_status(self):
        if self._task is not None or self._closing or self.retained_locator is None: return
        def work(job):
            snapshot, _token, boundaries = self._context()
            issuer, lifecycle = self._services(snapshot, boundaries)
            status, recovery = self._read(lifecycle, should_cancel=self._cancel.is_set)
            return issuer, lifecycle, boundaries, status, recovery, self._bound_config(lifecycle, status)
        def done(result):
            self._issuer, self._lifecycle, self._boundaries, status, recovery, config = result
            self._show_status(status, recovery, config=config)
        self._status = None
        self._run("Reading Retained Runtime Status", work, done)

    def _show_status(self, status, recovery=None, *, config=None):
        if not isinstance(status, LifecycleStatus) or Path(status.locator) != self.retained_locator:
            raise ValueError("The runtime service returned a different owner locator.")
        self._status, self._recovery_review = status, recovery
        self._display_config = deepcopy(config)
        self.current_status = status
        self.state_changed.emit(status)
        lines = [status.message, "Retained preparation: " + str(status.locator),
            "Execution identity: " + status.execution_id]
        if config is not None:
            lines.extend(("Runtime source: " + str(config.source_root),
                "Python: " + str(config.python_executable),
                "Python runtime root: " + str(config.python_runtime_root),
                "Dedicated runtime workspace: " + str(config.workspace_root),
                f"Loopback port: {config.port}; minimum free space: {config.required_free_bytes:,} bytes"))
        lines.extend(f"{identity}\nExact prepared path: {path}" for identity, path in status.bindings)
        capacity = getattr(recovery, "capacity", None)
        if recovery is not None:
            lines.append(recovery.message)
            lines.extend(" · ".join(map(str, row)) for row in recovery.paths)
            if capacity is not None: lines.append(capacity_review_text(capacity, retained=True))
        self._show_cleanup(capacity, retained=True)
        next_action = {
            "bound": "Use Prepared Models returns this unstarted preparation to the video workspace. Start remains a separate confirmation.",
            "prepared": "Recover to check the exact copies and finish binding, or Discard Unstarted when the service confirms it is safe.",
            "recovery_required": "Read the recorded copy and cleanup plan, then Recover. Protection remains until recovery proves the final state.",
            "submitted": "This retained runtime may still be running. Stop Owned Runtime requests its exact contained stop; Recover checks terminal evidence. It cannot be adopted for new jobs here.",
            "uncertain": "Runtime ownership is uncertain. Try exact Stop or Recover; missing processes alone do not release protection.",
            "unreserved": "Discard Unstarted closes only this unreserved preparation.",
            "stopped": "Ownership is closed. Close this window and review a new preparation before another runtime start.",
            "discarded": "This unstarted preparation is closed. Close this window and review a new preparation when needed.",
        }.get(status.state, "This state cannot be used. Read Status or Recover the exact retained owner; keep its protection in place.")
        lines.append(next_action)
        self.review_text.setPlainText("\n\n".join(lines))
        self.state_label.setText(runtime_storage_state_title(status.state))
        self.status_label.setText(next_action)

    def _mutate_retained(self, action):
        if self._task is not None or self._closing or self._status is None: return
        state = self._status.state
        if action == "discard" and state not in {"unreserved", "prepared", "bound"}: return
        if action == "stop" and state not in {"submitted", "uncertain", "recovery_required"}: return
        if state in {"discarded", "stopped"}: return
        messages = {
            "recover": "Reconcile this exact retained preparation? Recorded copy or least-used cleanup work may finish. Removed Fast copies are not automatically restored. A submitted runtime is only reconciled after positive stop evidence; no process is adopted and no job is submitted.",
            "discard": "Discard this unstarted preparation? The service must prove it never started. Completed model copies and Archive originals remain; independent Keep Warm and manual protections remain.",
            "stop": "Request stop of this exact owned runtime? Any work in its contained process tree will stop. Models remain protected until positive terminal and empty-process-tree evidence is verified.",
        }
        if not self._confirm({"recover": "Recover Preparation?", "discard": "Discard Unstarted Preparation?", "stop": "Stop Owned Runtime?"}[action],
            messages[action] + (f"\n\nRecorded Fast copies to remove: {len(getattr(getattr(self._recovery_review, 'capacity', None), 'victims', ()))}." if action == "recover" else "")
            + "\n\n" + str(self.retained_locator)): return
        self._status = None
        def work(job):
            self._check_boundaries()
            if action == "recover":
                status = self._lifecycle.recover(self.retained_locator, confirmed=True,
                    should_cancel=self._cancel.is_set, progress=self._progress(job))
            elif action == "discard": status = self._lifecycle.discard_unstarted(self.retained_locator, confirmed=True)
            else: status = self._lifecycle.stop(self.retained_locator, confirmed=True)
            return status, self._bound_config(self._lifecycle, status)
        self._run({"recover": "Recovering Video Preparation", "discard": "Discarding Unstarted Preparation", "stop": "Stopping Exact Owned Runtime"}[action], work,
            lambda result: self._show_status(result[0], config=result[1]))

    def recover(self): self._mutate_retained("recover")
    def discard_unstarted(self): self._mutate_retained("discard")
    def stop_owned_runtime(self): self._mutate_retained("stop")

    @staticmethod
    def _usable(status):
        return (isinstance(status, LifecycleStatus) and status.state == "bound" and status.protected_mode is True
            and status.owner_id == OWNER_ID and status.owner_version == OWNER_VERSION and bool(status.runtime_fingerprint)
            and len(status.bindings) == 3 and len({item[0] for item in status.bindings}) == 3)

    def use_prepared(self):
        if self._task is not None or self._closing or not self._usable(self._status) or self._display_config is None: return
        def work(_job):
            self._check_boundaries()
            status = self._lifecycle.status(self.retained_locator)
            if not self._usable(status): raise ValueError("The preparation changed. Read its status before continuing.")
            config = self._bound_config(self._lifecycle, status)
            if config != self._display_config:
                raise ValueError("The displayed runtime configuration changed. Read Status before using it.")
            return status, config
        def done(result):
            status, config = result
            self._show_status(status, config=config)
            if self._cancel.is_set() or self._closing: return
            self.selected_lifecycle, self.selected_status = self._lifecycle, status
            self.selected_config = deepcopy(config)
            QDialog.accept(self)
        self._run("Checking Prepared Video Models", work, done)

    def _run(self, label, work, done):
        if self._task is not None or self._closing: return
        self._cancel.clear()
        task = CoordinatedCallTask(self.jobs, label, work, resource="io")
        self._task = task
        self.status_label.setText(label + "…")
        from .job_progress import bind_job_progress
        progress = bind_job_progress(self, task, title=label, unit="MiB", cancel_button=self.cancel_button)
        task.signals.complete.connect(lambda value: self._finished(task, done, value))
        task.signals.failed.connect(lambda error: self._finished(task, self._failed, error))
        task.signals.cancelled.connect(lambda: self._finished(task, self._failed, "Operation cancelled. Read the exact retained status before continuing."))
        self._actions()
        try: task.start()
        except Exception as exc:
            progress.finish(task)
            self._finished(task, self._failed, str(exc))

    def _finished(self, task, done, value):
        if self._task is not task: return
        self._task = None
        try: done(value)
        except Exception as exc: self._failed(str(exc))
        self._actions()
        if self._closing:
            QDialog.reject(self)
            if not self._shutdown_emitted:
                self._shutdown_emitted = True; self.background_work_finished.emit()

    def _failed(self, error):
        self._status = None
        self.current_status = None
        self._display_config = None
        self.state_label.setText("Review Required")
        self.status_label.setText(str(error) + (" The locator is retained. Read Status to inspect recorded progress; model protection is not released by closing." if self.retained_locator else " Preview again after resolving the issue."))

    def _actions(self):
        idle = self._task is None and not self._closing
        retained = self.retained_locator is not None
        state = self._status.state if self._status is not None else ""
        self.preview_button.setEnabled(idle and self.config is not None and not retained)
        self.prepare_button.setEnabled(idle and not retained and self._review is not None and self._review.can_prepare)
        self.read_status_button.setEnabled(idle and retained)
        self.recover_button.setEnabled(idle and bool(state) and state not in {"stopped", "discarded"})
        self.discard_button.setEnabled(idle and state in {"unreserved", "prepared", "bound"})
        self.stop_button.setEnabled(idle and state in {"submitted", "uncertain", "recovery_required"})
        self.use_button.setEnabled(idle and self._usable(self._status) and self._display_config is not None)
        self.cancel_button.setEnabled(self._task is not None and not self._cancel.is_set())
        self.close_button.setEnabled(not self._closing)

    def cancel_work(self):
        if self._task is None: return
        self._cancel.set()
        self.status_label.setText("Stopping at a safe boundary. This window stays attached until the operation finishes; any retained preparation remains available for recovery.")
        self._actions()

    def has_active_background_work(self): return self._task is not None

    def request_background_shutdown(self):
        if self._task is None: return False
        self._closing = True; self.cancel_work()
        return True

    def reject(self):
        if not self.request_background_shutdown(): QDialog.reject(self)

    def closeEvent(self, event):
        if self.request_background_shutdown(): event.ignore()
        else: super().closeEvent(event)
