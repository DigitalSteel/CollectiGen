"""Explicit companion review over core-owned profiles and readiness evidence."""
from __future__ import annotations

from dataclasses import dataclass, replace
from itertools import islice
import ntpath
from typing import Callable

from PySide6.QtCore import Qt, QSignalBlocker, QTimer, Signal
from PySide6.QtWidgets import QComboBox, QDialog, QHBoxLayout, QLabel, QPlainTextEdit, QPushButton, QTreeWidget, QTreeWidgetItem, QVBoxLayout

from core.model_companion_readiness import CompanionChoice, CompanionReadinessRequest, CompanionReadinessService
from core.model_resource_registry import ModelResourceError


@dataclass(frozen=True)
class CompanionPreparationHandoff:
    report: object
    requirements: object
    resolver: Callable


class ModelCompanionReadinessDialog(QDialog):
    """Choices and explicit rechecks never substitute for adapter preflight.

    The parent owns acquisition/cache dialogs and calls refresh_after_child on
    return. No network, downloader, cache writer or passive recency update lives
    here. service_factory(snapshot) refreshes current protected/managed guards.
    """

    acquisition_requested = Signal(object)
    preparation_requested = Signal(object)
    import_choices_selected = Signal(object)

    def __init__(self, registry, jobs, main_resource_id, parent=None, *, service=None,
                 settings=None, protected_roots=(), managed_roots=(), service_factory=None, initial_request=None,
                 collect_for_import=False, on_guidance_choices=None):
        super().__init__(parent)
        self.registry, self.jobs, self.settings = registry, jobs, settings
        self.collect_for_import = bool(collect_for_import)
        self.on_guidance_choices = on_guidance_choices
        self.main_resource_id = main_resource_id
        self.service = service or CompanionReadinessService(registry,
            protected_roots=protected_roots, managed_roots=managed_roots)
        self.service_factory = service_factory
        if initial_request is not None and (not isinstance(initial_request, CompanionReadinessRequest)
                or initial_request.main_resource_id != main_resource_id):
            raise ModelResourceError("The initial resource request must belong to this exact main model.")
        self.request = initial_request or CompanionReadinessRequest(main_resource_id)
        self.report = None
        self.changed = False
        self._snapshot = None
        self._token = None
        self._profiles = ()
        self._profile_choices = {}
        self._selected_slot = "main"
        self._task = self._selector = self._external_child = None
        self._closing = False
        self._closed = False
        self._close_parent = None
        self.setWindowTitle("Required Model Resources")
        self.setWindowModality(Qt.WindowModal); self.resize(1360, 880)
        body = QVBoxLayout(self)
        heading = self._label("Plan the resources for this exact model and workflow. Files waiting for import remain unregistered and unverified. "
            "Use Choices in Import returns these selections for the combined approval; no files are prepared here."
            if self.collect_for_import else
            "Review the resources for this exact model and workflow. Stored locations, verified files and companion compatibility are separate checks. Running still requires the workflow's preflight.")
        body.addWidget(heading)
        self.main_label = self._label("Main model: reading recorded identity…"); body.addWidget(self.main_label)
        context = QHBoxLayout()
        context.addWidget(QLabel("Goal")); self.goal = QComboBox()
        self.goal.addItem("Generate images or video", "generation"); self.goal.addItem("Train a model or adapter", "training")
        self.goal.setCurrentIndex(max(0, self.goal.findData(self.request.workflow_kind)))
        self.goal.setAccessibleName("Companion workflow goal"); context.addWidget(self.goal)
        context.addWidget(QLabel("Profile")); self.profile = QComboBox()
        self.profile.setAccessibleName("Exact companion profile")
        self.profile.setSizeAdjustPolicy(QComboBox.AdjustToMinimumContentsLengthWithIcon)
        self.profile.setMinimumContentsLength(30); context.addWidget(self.profile, 1)
        body.addLayout(context)
        self.summary = self._label("Loading recorded model context…"); body.addWidget(self.summary)
        self.rows = QTreeWidget(); self.rows.setRootIsDecorated(False)
        self.rows.setHeaderLabels(("Resource slot", "Need", "Chosen resource", "Availability", "Verification", "Compatibility"))
        for column, width in enumerate((240, 105, 340, 125, 130)): self.rows.setColumnWidth(column, width)
        if self.collect_for_import: self.rows.setColumnWidth(3, 195)
        self.rows.setAccessibleName("Required recommended and optional model resources")
        self.rows.itemSelectionChanged.connect(self._row_changed); body.addWidget(self.rows, 1)
        self.details = QPlainTextEdit(); self.details.setReadOnly(True)
        self.details.setAccessibleName("Selected resource evidence and exact chosen path"); self.details.setMaximumHeight(230)
        body.addWidget(self.details)
        choice_actions = QHBoxLayout()
        self.locate = self._button("Choose Registered Resource…", self._choose)
        self.clear_choice = self._button("Clear Slot Choice", self._clear_choice)
        self.acquire = self._button("Review Acquisition…", self._acquire)
        self.recheck = self._button("Recheck Chosen Files", self._verify)
        for button in (self.locate, self.clear_choice, self.acquire): choice_actions.addWidget(button)
        choice_actions.addStretch(1); choice_actions.addWidget(self.recheck); body.addLayout(choice_actions)
        self.status = self._label("No model files are checked until you deliberately choose or recheck them."); body.addWidget(self.status)
        actions = QHBoxLayout()
        self.reload = self._button("Refresh Recorded State", self.refresh_after_child)
        self.stop = self._button("Cancel Safely", self._stop)
        self.prepare = self._button("Review Preparation…", self._prepare)
        if self.collect_for_import:
            self.prepare.setText("Use Choices in Import")
            self.recheck.hide()
        self.close_button = self._button("Back Without Changes" if on_guidance_choices else "Back to Models", self.reject)
        self.return_to_guidance_button = self._button("Use Choices in Recommendation", self._return_to_guidance)
        self.return_to_guidance_button.setProperty("primary", True)
        self.return_to_guidance_button.setVisible(bool(on_guidance_choices))
        actions.addWidget(self.reload); actions.addWidget(self.stop); actions.addStretch(1)
        actions.addWidget(self.prepare); actions.addWidget(self.close_button)
        actions.addWidget(self.return_to_guidance_button); body.addLayout(actions)
        self.goal.currentIndexChanged.connect(self._goal_changed)
        self.profile.currentIndexChanged.connect(self._profile_changed)
        self._update_actions(); QTimer.singleShot(0, self.refresh_after_child)

    @staticmethod
    def _label(text):
        label = QLabel(text); label.setWordWrap(True); label.setTextFormat(Qt.PlainText); return label

    @staticmethod
    def _button(text, callback):
        button = QPushButton(text); button.clicked.connect(callback); return button

    @staticmethod
    def _profile_key(request):
        return (request.workflow_kind, request.target_id, request.adapter_id, request.adapter_version)

    def _row(self):
        item = self.rows.currentItem()
        return item.data(0, Qt.UserRole) if item is not None else None

    def _update_actions(self):
        if not hasattr(self, "close_button"): return
        idle = self._task is None and self._selector is None and self._external_child is None and not self._closing
        row = self._row()
        for widget in (self.goal, self.profile, self.rows, self.reload): widget.setEnabled(idle)
        self.locate.setEnabled(idle and row is not None and self._token is not None)
        self.clear_choice.setEnabled(idle and row is not None and any(choice.slot == row.slot for choice in self.request.choices))
        self.acquire.setEnabled(idle and row is not None and self.report is not None)
        self.recheck.setEnabled(not self.collect_for_import and idle and self._token is not None and bool(self.request.choices))
        self.prepare.setEnabled(idle and self.report is not None and
            (bool(self.request.choices) if self.collect_for_import else self.report.can_prepare))
        self.stop.setEnabled(self._task is not None); self.close_button.setEnabled(not self._closing)
        self.return_to_guidance_button.setEnabled(idle and self._guidance_choices_ready(self.report))

    def _guidance_choices_ready(self, report):
        if not self.on_guidance_choices or report is None or report.request != self.request or report.registry_token != self._token:
            return False
        selected = {choice.slot: choice for choice in self.request.choices if choice.slot != "main"}
        rows = {row.slot: row for row in report.rows}
        return bool(selected) and not any(row.verification == "failed" for row in report.rows) and all(
            (row := rows.get(slot)) is not None and row.resource_id == choice.resource_id
            and row.path == choice.path and row.role == choice.role and row.family == choice.family
            and row.availability in {"registered", "available"}
            and row.compatibility != "mismatch" and row.verification != "failed"
            for slot, choice in selected.items()
        )

    def _return_to_guidance(self):
        if (self._task is not None or self._selector is not None or self._external_child is not None
                or self._closing or not self._guidance_choices_ready(self.report)):
            return
        try:
            snapshot, token = self.registry.snapshot()
            if token != self._token:
                self.status.setText("The model registry changed. Refresh Recorded State before returning choices.")
                return
            service = self.service_factory(snapshot) if self.service_factory is not None else self.service
            fresh = service.project(self.request, snapshot=snapshot, registry_token=token)
            if (fresh.catalog_token != self.report.catalog_token or fresh.profile_status != self.report.profile_status
                    or not self._guidance_choices_ready(fresh)):
                self.status.setText("The companion profile or selected declarations changed. Refresh Recorded State and correct the choices.")
                return
            if self.on_guidance_choices(fresh) is not True:
                self.status.setText("The recommendation changed. Return without changes and reopen Required Resources.")
                return
        except (ValueError, RuntimeError, OSError) as exc:
            self.status.setText(str(exc)); return
        super().accept()

    def _fill_profiles(self):
        key = self._profile_key(self.request)
        with QSignalBlocker(self.profile):
            self.profile.clear(); self.profile.addItem("Unknown profile — manual resource review", (self.request.workflow_kind, "", "", ""))
            for profile in self._profiles:
                if profile.workflow_kind != self.request.workflow_kind: continue
                value = (profile.workflow_kind, profile.target_id, profile.adapter_id, profile.adapter_version)
                if value == (self.request.workflow_kind, "", "", ""): continue
                applicability = {"exact_profile_unverified": "Exact training profile"}.get(profile.applicability, "Applicability unknown")
                label = profile.label.removesuffix("; files need verification")
                self.profile.addItem(f"{label} · {applicability}", value)
                self.profile.setItemData(self.profile.count() - 1, f"{profile.label}\n{profile.target_id}\n{profile.adapter_id} {profile.adapter_version}\n{profile.applicability}", Qt.ToolTipRole)
            index = next((i for i in range(self.profile.count()) if tuple(self.profile.itemData(i)) == key), -1)
            if index < 0:
                self.profile.addItem("Previously chosen profile — unavailable; review required", key)
                index = self.profile.count() - 1
            self.profile.setCurrentIndex(index)

    def _remember_choices(self):
        self._profile_choices[self._profile_key(self.request)] = self.request.choices

    def _choices_for_profile(self, key):
        choices = self._profile_choices.get(key, ())
        if any(choice.slot == "main" for choice in choices): return choices
        main = tuple(choice for choice in self.request.choices if choice.slot == "main")
        return main + choices

    def _goal_changed(self, *_):
        if self._task is not None or self._selector is not None: return
        self._remember_choices()
        workflow = str(self.goal.currentData())
        key = (workflow, "", "", "")
        choices = self._choices_for_profile(key)
        self.request = CompanionReadinessRequest(self.main_resource_id, workflow_kind=workflow, choices=choices)
        self._selected_slot = "main"; self._fill_profiles(); self._project()

    def _profile_changed(self, *_):
        if self._task is not None or self._selector is not None: return
        value = self.profile.currentData()
        if value is None: return
        self._remember_choices()
        workflow, target, adapter, version = tuple(value)
        choices = self._choices_for_profile(tuple(value))
        self.request = CompanionReadinessRequest(self.main_resource_id, workflow, target, adapter, version, choices)
        self._selected_slot = "main"; self._project()

    def _project(self):
        self.report = None
        if self._snapshot is None or self._token is None: self._update_actions(); return
        try: report = self.service.project(self.request, snapshot=self._snapshot, registry_token=self._token)
        except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc)); self._update_actions(); return
        self._show_report(report)
        self.status.setText("Import planning only. Registration and verification wait for the combined plan."
            if self.collect_for_import else "Recorded state only. Choose exact files and Recheck Chosen Files for fresh verification.")

    def _show_report(self, report):
        self.report = report; self.request = report.request
        rows = tuple(islice(iter(report.rows), 66))
        if len(rows) > 65: raise ModelResourceError("This profile exceeds the bounded companion review.")
        with QSignalBlocker(self.rows):
            self.rows.clear(); selected = None
            for row in rows:
                choice = (self._resource_label(row.resource_id, row.path) if row.resource_id else
                    "Main selected; choose location" if row.slot == "main" and self.on_guidance_choices else
                    "No resource chosen")
                slot_label = {"main": "Main model", "text_encoder": "Text encoder", "vae": "VAE"}.get(row.slot, row.slot.replace("_", " ").capitalize())
                availability = ("Location not chosen" if row.slot == "main" and self.on_guidance_choices
                    and row.availability == "missing" else
                    {"registered": "Stored", "available": "Available", "missing": "Not chosen", "unavailable": "Unavailable", "unregistered_or_mismatched": "Unregistered / mismatched"}.get(row.availability, "Unknown"))
                item = QTreeWidgetItem((slot_label, row.necessity.title(), choice,
                    availability,
                    str(row.verification).replace("_", " ").capitalize(),
                    str(row.compatibility).replace("_", " ").capitalize()))
                for column in range(6): item.setToolTip(column, item.text(column))
                item.setToolTip(0, f"{slot_label}\nSlot: {row.slot}")
                item.setData(0, Qt.UserRole, row); item.setToolTip(2, f"{choice}\n{row.resource_id}\n{row.path or 'No path chosen'}")
                self.rows.addTopLevelItem(item)
                if row.slot == self._selected_slot: selected = item
            self.rows.setCurrentItem(selected or self.rows.topLevelItem(0))
        completeness = "Exact adapter resource set verified" if report.qualification == "adapter_qualified" else "Complete companion set unknown"
        profile_status = "Required files verified" if report.qualification == "adapter_qualified" else report.profile_status.replace('_', ' ')
        holds = "\n" + "\n".join(report.held[:4]) if report.held else ""
        self.summary.setText(f"Profile: {profile_status} · Evidence authority: {report.authority.replace('_', ' ')}\n"
            f"{completeness}. Running still requires the exact workflow preflight.{holds}")
        self._row_changed(); self._update_actions()

    def _row_changed(self):
        row = self._row()
        if row is None: self.details.clear(); self._update_actions(); return
        self._selected_slot = row.slot
        expected = ", ".join(row.expected_resource_ids) or "Unknown — no exact identity requirement supplied"
        report = self.report
        self.details.setPlainText(f"Slot: {row.slot} · {row.necessity}\nModel Type / Family: {row.role or 'Unknown'} / {row.family or 'Unknown'}\n"
            f"Chosen resource: {row.resource_id or 'None'}\nChosen path: {row.path or 'None'}\nExpected identities: {expected}\n"
            f"Availability: {row.availability} · Verification: {row.verification} · Compatibility: {row.compatibility}\n{row.summary}\n"
            f"Evidence authority: {getattr(report, 'authority', 'Unknown')}\nCatalog revision: {getattr(report, 'catalog_token', '') or 'Unknown'}\n"
            "Evidence sources:\n" + ("\n".join(getattr(report, "sources", ())) or "Unknown"))
        self._update_actions()

    def _resource_label(self, identity, path=""):
        record = (self._snapshot or {}).get("resources", {}).get(identity, {})
        user = record.get("user", {})
        name = user.get("display_name") if isinstance(user, dict) else ""
        if not name:
            if not path:
                location = next(iter(record.get("locations", ())), {})
                path = location.get("path", "")
            name = ntpath.basename(str(path).replace("/", "\\")) or "Model filename unknown"
        text = " ".join(str(name)[:4096].split())
        return text[:117] + "…" if len(text) > 120 else text

    def _policy_current(self):
        if self.service_factory is None: return True
        try: current = self.service_factory(self._snapshot)
        except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc)); self._update_actions(); return False
        if any(getattr(current, key) != getattr(self.service, key) for key in ("protected_roots", "managed_roots")):
            self.service = current
            self._project(); self.status.setText("Protected or managed folder boundaries changed. Recheck the chosen files before continuing.")
            return False
        return True

    def refresh_after_child(self, choice=None):
        if self._task is not None or self._selector is not None or self._external_child is not None or self._closing: return
        if isinstance(choice, bool): choice = None  # QPushButton.clicked's checked flag.
        if choice is not None:
            row = self._row()
            if (not isinstance(choice, CompanionChoice) or row is None or choice.slot != self._selected_slot
                    or choice.slot != row.slot or (choice.slot == "main" and choice.resource_id != self.main_resource_id)
                    or (row.role and choice.role != row.role) or (row.family and choice.family != row.family)
                    or (row.expected_resource_ids and choice.resource_id not in row.expected_resource_ids)):
                self.status.setText("The returned registered resource does not match the active slot and exact request. No choice was changed.")
                return False
            self.request = replace(self.request, choices=tuple(value for value in self.request.choices if value.slot != choice.slot) + (choice,))
            self._remember_choices(); self.changed = True
        self.report = None; self._token = None
        self._run("Reading recorded companion context", lambda _job: self.registry.snapshot(), self._loaded)
        return True

    def _loaded(self, result):
        self._snapshot, self._token = result
        if self.service_factory is not None: self.service = self.service_factory(self._snapshot)
        profiles = (self.service.available_profiles(self._snapshot, self.main_resource_id, allow_planned=True)
            if self.collect_for_import else self.service.available_profiles(self._snapshot, self.main_resource_id))
        self._profiles = tuple(islice(iter(profiles), 515))
        if len(self._profiles) > 514: raise ModelResourceError("The available profile list exceeds its supported bound.")
        planned_path = next((choice.path for choice in self.request.choices if choice.slot == "main"), "") if self.collect_for_import else ""
        main_choice = next((choice for choice in self.request.choices if choice.slot == "main"), None)
        main_context = (" · choose an exact location below to recheck files"
            if self.on_guidance_choices and main_choice is None else "")
        self.main_label.setText(f"Main model: {self._resource_label(self.main_resource_id, planned_path)}{main_context}")
        self.main_label.setToolTip(self.main_resource_id)
        self._fill_profiles(); self._project()

    def _choose(self):
        row = self._row()
        if self._task is not None or self._selector is not None or row is None or not self._policy_current(): return
        from .model_selector_dialog import ModelSelectorDialog
        dialog = ModelSelectorDialog(self.registry, self.jobs, parent=self, role="" if row.slot == "main" else row.role or "", family="" if row.slot == "main" else row.family or "",
            settings=self.settings, protected_roots=self.service.protected_roots,
            current_choice_label=row.resource_id or "No resource chosen", allow_manage=not self.collect_for_import,
            collect_for_import=self.collect_for_import)
        self._selector = dialog
        slot, context = row.slot, self._profile_key(self.request)
        if slot == "main": dialog.search.setText(self.main_resource_id)
        dialog.setWindowModality(Qt.WindowModal)
        dialog.finished.connect(lambda _result: self._chosen(dialog, slot, context))
        dialog.show(); self._update_actions()

    def _chosen(self, dialog, slot, context):
        if self._selector is not dialog: return
        self._selector = None
        identity = dialog.selected_resource_id
        if identity:
            self.changed = True
            if slot == "main" and identity != self.main_resource_id:
                self.status.setText("The main model is fixed for this review. Choose an exact copy of that model.")
                self.report = None; self._update_actions()
            elif self._profile_key(self.request) == context:
                choice = CompanionChoice(slot, identity, dialog.selected_path, dialog.selected_role, dialog.selected_family)
                self.request = replace(self.request, choices=tuple(value for value in self.request.choices if value.slot != slot) + (choice,))
                self._selected_slot = slot; self._remember_choices()
        if self._closing: self._finish_close(); return
        if identity and slot == "main" and identity != self.main_resource_id: return
        self.refresh_after_child()

    def _clear_choice(self):
        row = self._row()
        if self._task is not None or self._selector is not None or row is None: return
        self.request = replace(self.request, choices=tuple(choice for choice in self.request.choices if choice.slot != row.slot))
        self._remember_choices(); self._project()

    def _verify(self):
        if self.collect_for_import: return
        if self._task is not None or not self.request.choices or self._token is None or not self._policy_current(): return
        request, token, service = self.request, self._token, self.service
        self.report = None
        self._run("Rechecking explicitly chosen model files", lambda job: service.verify(request, expected_registry=token,
            should_cancel=lambda: job.cancel_requested, progress=self._progress(job)), self._verified)

    def _verified(self, report):
        self._show_report(report)
        self.status.setText("Chosen paths rechecked. Review verification and compatibility separately; preparation requires its own review.")

    def _acquire(self):
        row = self._row()
        if self._task is not None or row is None or self.report is None or not self._policy_current(): return
        try: handoff = self.service.acquisition_handoff(self.report, row.slot)
        except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc)); self._update_actions(); return
        self.status.setText("Acquisition context handed to the existing provider review. No query or download was sent here.")
        self.acquisition_requested.emit(handoff)

    def _prepare(self):
        if self.collect_for_import:
            if self._task is None and self._selector is None and self._external_child is None and not self._closing and self.report is not None:
                self.import_choices_selected.emit(self.request)
                self.accept()
            return
        if self._task is not None or self.report is None or not self.report.can_prepare or not self._policy_current(): return
        report, service = self.report, self.service
        def handoff(_job):
            return CompanionPreparationHandoff(report, service.requirements(report), service.resolver())
        self._run("Reviewing resource preparation eligibility", handoff, self._preparation_ready)

    def _preparation_ready(self, handoff):
        self.status.setText("Resource set handed to the existing preparation review. No files were copied here.")
        self.preparation_requested.emit(handoff)

    @staticmethod
    def _progress(job):
        def report(done, total, label):
            units = max(1, (total + 2**20 - 1) // 2**20)
            job.update(units if done == total else done // 2**20, units, label)
        return report

    def _run(self, title, function, callback):
        if self._task is not None: return
        from .workers import CoordinatedCallTask
        from .job_progress import bind_job_progress
        task = CoordinatedCallTask(self.jobs, title, function, resource="io")
        self._task = task; self.status.setText(title)
        binding = bind_job_progress(self, task, title=title, unit="MiB", cancel_button=self.stop)
        task.signals.complete.connect(lambda result: self._finish(task, callback, result))
        task.signals.failed.connect(lambda message: self._finish(task, self._failed, message))
        task.signals.cancelled.connect(lambda: self._finish(task, self._failed, "Recheck cancelled. Recorded choices remain; run another explicit recheck before preparing."))
        self.destroyed.connect(task.cancel); self._update_actions()
        try: task.start()
        except Exception as exc:
            binding.finish(task); task.cancel(); self._finish(task, self._failed, f"Could not start: {exc}. Retry when background capacity is available.")

    def _finish(self, task, callback, result):
        from shiboken6 import isValid
        if not isValid(self): task.cancel(); return
        if self._task is not task: return
        self.destroyed.disconnect(task.cancel); self._task = None
        if self._closing: self._finish_close(); return
        try: callback(result)
        except (ValueError, RuntimeError, OSError) as exc: self._failed(str(exc))
        self._update_actions()

    def _failed(self, message):
        self.report = None; self.status.setText(str(message))
        self.rows.clear(); self.details.clear()
        self.summary.setText("Current readiness review unavailable. Choices are retained; refresh or recheck before continuing.")

    def _stop(self):
        if self._task is not None and self._task.job_id:
            self.jobs.cancel(self._task.job_id); self.status.setText("Stopping at the next safe file boundary…")

    def request_parent_close(self, continuation):
        pending = self._task is not None or self._selector is not None or self._external_child is not None
        self._close_parent = continuation if pending else None
        self.reject(); return not pending

    def _finish_close(self):
        if self._closed or self._task is not None or self._selector is not None or self._external_child is not None: return
        self._closed = True
        continuation, self._close_parent = self._close_parent, None
        super().reject()
        if continuation: continuation()

    def reject(self):
        self._closing = True
        if self._selector is not None: self._selector.reject()
        if self._external_child is not None:
            child = self._external_child
            close = getattr(child, "request_parent_close", None)
            if callable(close): close(lambda: self._external_finished(child))
            else: child.reject()
        if self._task is not None: self._stop()
        self._finish_close()

    def track_external_child(self, dialog):
        """Retain a controller-owned acquisition/cache child through safe close."""
        if self._task is not None or self._selector is not None or self._external_child is not None or self._closing:
            raise ModelResourceError("Finish the active companion review before opening another action.")
        self._external_child = dialog
        dialog.finished.connect(lambda _result: self._external_finished(dialog))
        self._update_actions()

    def _external_finished(self, dialog):
        if self._external_child is not dialog: return
        self._external_child = None
        if self._closing: self._finish_close()
        else: self._update_actions()

    def closeEvent(self, event):
        if self._task is not None or self._selector is not None or self._external_child is not None:
            self.reject(); event.ignore()
        else: super().closeEvent(event)
