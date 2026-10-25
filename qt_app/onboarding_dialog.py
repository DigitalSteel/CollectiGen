from __future__ import annotations

import copy
import time
from pathlib import Path

from PySide6.QtCore import QSignalBlocker, QThreadPool, QTimer, Qt
from PySide6.QtWidgets import (
    QAbstractItemView, QCheckBox, QComboBox, QDialog, QFileDialog, QFrame,
    QGridLayout, QGroupBox, QHeaderView, QHBoxLayout, QLabel, QLayout,
    QLineEdit, QMessageBox, QPushButton, QProgressBar, QScrollArea, QSpinBox,
    QStackedWidget, QTabWidget, QTableWidget, QTableWidgetItem, QTextEdit, QVBoxLayout,
    QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core.ai_interrogation import (
    JOYCAPTION_MODEL, MODEL_ACQUISITIONS, PLAIN_LANGUAGE_MODELS,
    WD_TAGGER_DESCRIPTIONS, WD_TAGGER_MODELS,
)
from core.dependencies import (
    DependencyService, RecommendedAiSetup, WD_SETUP_PRESETS,
    matching_wd_setup_preset, nvidia_gpu_profile, recommended_first_run_ai_setup,
)
from core.first_run_ai import run_quick_ai_benchmark
from core.onboarding import (
    ONBOARDING_STEPS, ONBOARDING_VERSION, apply_starting_preset,
    checkpoint_onboarding, preset_consequences, save_settings_verified,
    validate_locations,
)
from core.paths import SETTINGS_PATH, MODEL_RESOURCES_PATH

from .components import IndeterminateActivityBar,compact_button,set_progress_bar
from .modal_feedback import FeedbackEnabledDialog
from .workers import AsyncCallTask


LOCATION_SPECS = (
    ("ingest_directory", "Incoming images"),
    ("problems_directory", "Problems"),
    ("recycle_directory", "Recycle Bin"),
    ("ai_models_directory", "AI model files"),
)
WD_RUNTIME_COMPONENTS = frozenset(DependencyService.WD_PACKAGES)
JOY_RUNTIME_COMPONENTS = frozenset(DependencyService.JOY_BASE_PACKAGES)
JOY_HARDWARE_COMPONENT = DependencyService.JOY_HARDWARE_COMPONENT


class FirstRunSetupProgressDialog(FeedbackEnabledDialog):
    """Loader-style view for confirmed setup followed by an optional benchmark."""

    feedback_workflow = "First-run local AI setup"

    def __init__(
        self, settings, queue, jobs, operation_ids, parent=None, *,
        settings_path=SETTINGS_PATH, run_benchmark=True,
    ):
        super().__init__(parent)
        self.settings=settings; self.queue=queue; self.jobs=jobs
        self.settings_path=Path(settings_path)
        self.operation_ids={str(value) for value in operation_ids if str(value)}
        self.run_benchmark=bool(run_benchmark)
        self._benchmark_started=False; self._benchmark_job_id=""
        self._benchmark_holder={}; self._finished=False
        self._stop_requested=False; self._stop_errors=[]
        self._started_at=time.monotonic()
        self.setWindowTitle("Setting up CollectiGen")
        self.setModal(True); self.setMinimumWidth(620)
        layout=QVBoxLayout(self); layout.setContentsMargins(20,18,20,18); layout.setSpacing(12)
        self.phase=QLabel("Preparing confirmed local AI setup…")
        self.phase.setObjectName("sectionTitle"); self.phase.setWordWrap(True)
        self.detail=QLabel(
            "Downloads are verified before installation. Images are not read or uploaded during setup."
        )
        self.detail.setObjectName("mutedLabel"); self.detail.setWordWrap(True)
        self.activity=IndeterminateActivityBar()
        self.activity.setAccessibleName("Local AI setup activity")
        self.progress=QProgressBar(); self.progress.hide()
        self.activity_status=QLabel(); self.activity_status.setObjectName("mutedLabel")
        self.activity_status.setAlignment(Qt.AlignCenter)
        self.activity_status.setAccessibleName("Local AI setup activity status")
        self.close_button=compact_button(QPushButton("Please Wait…"),primary=True)
        self.close_button.setEnabled(False); self.close_button.clicked.connect(self.accept)
        layout.addWidget(self.phase); layout.addWidget(self.detail)
        layout.addWidget(self.activity); layout.addWidget(self.progress)
        layout.addWidget(self.activity_status)
        self.stop_button=compact_button(QPushButton("Cancel Safely"))
        self.stop_button.setAccessibleName("Cancel this local AI setup safely")
        self.stop_button.setToolTip(
            "Stop only this setup or its optional benchmark. Completed installations remain available; wait for recovery state before continuing."
        )
        self.stop_button.clicked.connect(self._request_stop)
        actions=QHBoxLayout(); actions.addStretch(1)
        actions.addWidget(self.stop_button); actions.addWidget(self.close_button)
        layout.addLayout(actions)
        from .components import bind_status_footer
        bind_status_footer(self,self.phase,self.activity_status,self.activity,self.progress,title="Setup")
        self.timer=QTimer(self); self.timer.setInterval(200)
        self.timer.timeout.connect(self._poll); self.timer.start()
        self._poll()

    def _selected_operations(self):
        return tuple(
            value for value in self.queue.snapshot()
            if value.id in self.operation_ids
        )

    def _elapsed_text(self):
        elapsed=max(0,int(time.monotonic()-self._started_at))
        hours,remainder=divmod(elapsed,3600); minutes,seconds=divmod(remainder,60)
        if hours:return f"Elapsed {hours}h {minutes}m"
        if minutes:return f"Elapsed {minutes}m {seconds}s"
        return f"Elapsed {seconds}s"

    def _show_work_progress(
        self,current=0,total=0,*,detail="",unknown_text="Working…",unit="",scope="",
    ):
        current=max(0,int(current)); total=max(0,int(total))
        elapsed=self._elapsed_text()
        if total:
            self.activity.set_active(False); self.activity.hide(); self.progress.show()
            set_progress_bar(
                self.progress,current,total,detail=detail,unknown_text=unknown_text,
                unit=unit,scope=scope,
            )
            status=elapsed
        else:
            self.progress.hide(); self.activity.show()
            self.activity.set_active(True,detail or unknown_text)
            phase=str(unknown_text or "Working…").strip().rstrip(" .…")+"…"
            status=f"{phase}  •  {elapsed}"
        self.activity_status.setText(status)
        self.activity_status.setAccessibleDescription(
            " • ".join(value for value in (str(detail or "").strip(),elapsed) if value)
        )
        self.activity_status.show()

    def _request_stop(self):
        if self._finished:
            return
        errors=[]
        accepted=False
        if self._benchmark_job_id:
            job=self.jobs.get(self._benchmark_job_id)
            if job is not None and job.state in {"queued","running","paused"}:
                try:
                    self.jobs.cancel(self._benchmark_job_id)
                    accepted=True
                except Exception as exc:
                    errors.append(str(exc))
        else:
            for operation in self._selected_operations():
                if operation.state not in {"waiting","running"}:
                    continue
                try:
                    stopped,message=self.queue.cancel_operation(operation.id)
                    accepted=accepted or stopped
                    if not stopped:
                        errors.append(message)
                except Exception as exc:
                    errors.append(str(exc))
        self._stop_errors=errors
        self._stop_requested=self._stop_requested or accepted or not errors
        self.stop_button.setEnabled(bool(errors))
        self._poll()

    def _show_stop_wait(self):
        if self._stop_requested:
            self.phase.setText("Cancellation requested; waiting for a safe stopping point.")
            self.detail.setText(
                "Completed installations stay available. Continue will unlock after this setup has retained its recovery state."
            )
        if self._stop_errors:
            self.detail.setText("Cancellation needs attention: "+"; ".join(self._stop_errors))

    def _finish(self,title,detail,*,success,cancelled=False):
        self._finished=True; self.timer.stop()
        self.stop_button.hide()
        self.phase.setText(str(title)); self.detail.setText(str(detail))
        self.activity.set_active(False); self.activity.hide(); self.activity_status.hide()
        if cancelled:
            self.progress.hide()
        else:
            self.progress.show()
            set_progress_bar(
                self.progress,1,1,detail=detail,
                scope="Ready" if success else "Needs attention",
            )
        self.close_button.setText("Continue"); self.close_button.setEnabled(True)

    def _start_benchmark(self):
        if self._benchmark_started:
            return
        self._benchmark_started=True
        service=DependencyService(self.settings)
        if not (
            service.capability_ready("wd") or service.capability_ready("joy")
        ):
            self._finish(
                "Local AI setup needs attention",
                "No installed local AI capability was ready for the optional quick benchmark.",
                success=False,
            )
            return
        self.phase.setText("Running the optional quick speed benchmark…")
        self.detail.setText(
            "Two disposable synthetic images are used. No library image, caption, Tag, or model output is retained."
        )
        self._show_work_progress(unknown_text="Starting benchmark…")

        def worker(job):
            result=run_quick_ai_benchmark(
                self.settings,progress=job.update,
                should_cancel=lambda:not job.checkpoint(),
            )
            self._benchmark_holder["result"]=result
            return True

        job=self.jobs.submit(
            "Quick first-run AI benchmark",worker,resource="gpu",
            key="first-run-ai-benchmark",exclusive=True,
            reclaim_ai_runtime=True,
        )
        self._benchmark_job_id=job.id

    def _save_benchmark(self,result):
        self.settings.ai_model_benchmarks.update(result.wd_results)
        if result.joy_result is not None:
            self.settings.ai_model_benchmarks[JOYCAPTION_MODEL]=result.joy_result
        self.settings.wd_execution_provider=result.wd_execution_provider
        self.settings.wd_batch_size=result.wd_batch_size
        save_settings_verified(self.settings,self.settings_path)

    def _poll(self):
        if self._finished:
            return
        if self._benchmark_job_id:
            job=self.jobs.get(self._benchmark_job_id)
            if job is None:
                self._finish(
                    "Quick benchmark could not be read",
                    "Setup is complete. You can run the full Hardware Benchmark later from Setup.",
                    success=False,
                ); return
            self.detail.setText(job.message or self.detail.text())
            self._show_work_progress(
                job.current,job.total,detail=job.message,
                unknown_text="Running quick benchmark…",unit="steps",
            )
            if job.state in {"queued","running","paused"}:
                self._show_stop_wait()
                return
            if self._stop_requested:
                title="Optional speed test stopped"
                detail="Completed setup remains available. No benchmark settings were saved from this stopped request. Continue now; Hardware Benchmark can be run later from Setup."
                if job.state=="failed":
                    title="Optional speed test stopped; needs attention"
                    failure=str(getattr(job,"error","") or job.message or "No additional failure details were available.")
                    detail+=" Speed test failure: "+failure
                self._finish(title,detail,success=False,cancelled=True)
                return
            if job.state=="complete" and self._benchmark_holder.get("result") is not None:
                try:
                    self._save_benchmark(self._benchmark_holder["result"])
                except Exception as exc:
                    self._finish(
                        "Benchmark finished but its settings were not saved",
                        f"Setup is complete. {exc}",success=False,
                    ); return
                result=self._benchmark_holder["result"]
                self._finish(
                    "Local AI is set up and tuned",
                    f"Quick benchmark selected {result.wd_execution_provider.upper()} with AI Tag batch size {result.wd_batch_size}. You can start using CollectiGen.",
                    success=True,
                ); return
            self._finish(
                "AI setup finished without a speed test",
                "CollectiGen could not complete the optional speed test. Your local AI setup is still available, and no library images were used or changed. Continue now and retry Hardware Benchmark from Setup.",
                success=False,
            ); return

        operations=self._selected_operations()
        active=next((
            value for value in operations if value.state in {"running","waiting"}
        ),None)
        if active is not None:
            self.phase.setText(active.label)
            self.detail.setText(active.detail)
            job=self.jobs.get(active.job_id) if active.job_id else None
            if job is not None:
                self._show_work_progress(
                    job.current,job.total,detail=active.detail,
                    unknown_text="Preparing verified components…",
                )
            else:
                complete=sum(value.state=="complete" for value in operations)
                self._show_work_progress(
                    complete,len(operations),detail=active.detail,
                    unknown_text="Waiting for setup…",unit="setup steps",
                )
            self._show_stop_wait()
            return
        if self._stop_requested:
            failure_details="; ".join(str(value.detail) for value in operations if value.state=="failed")
            detail="Completed installations remain available. Unfinished setup retains its recovery state; continue now and review or retry it from Setup. The optional speed test was not started."
            if failure_details:
                detail+=" Some setup also needs attention: "+failure_details
            self._finish("Setup stopped safely",detail,success=False,cancelled=True)
            return
        failed=[value for value in operations if value.state in {"failed","cancelled"}]
        if failed:
            self._finish(
                "Local AI setup needs attention",
                "\n".join(value.detail for value in failed),success=False,
            ); return
        if self.run_benchmark:
            self._start_benchmark(); return
        self._finish(
            "Local AI setup is complete",
            "The selected models and memory profile are ready. You can start using CollectiGen.",
            success=True,
        )

    def reject(self):
        if self._finished:
            super().reject()


class OnboardingDialog(FeedbackEnabledDialog):
    """Resumable goal-led setup backed by the existing AppSettings model."""

    feedback_workflow = "Guided Setup"

    def __init__(
        self, settings, parent=None, *, settings_path=SETTINGS_PATH,
        reentry=False, initial_step: str="", usage_service=None,
    ):
        super().__init__(parent)
        self.settings = settings
        self.settings_path = Path(settings_path)
        self.reentry = bool(reentry)
        self.state=getattr(parent,"state",None)
        self.library_registry=settings.library_registry()
        self._onboarding_active_library_id=str(settings.active_library_id or "")
        if (
            self.library_registry.profiles
            and self.library_registry.get(self._onboarding_active_library_id) is None
        ):
            self._onboarding_active_library_id=(
                self.library_registry.profiles[0].stable_id
            )
        self._gpu_detection_task=None
        self._initial_status = str(getattr(settings, "onboarding_status", "not_started"))
        self._usage_service = usage_service
        self._usage_service_injected = usage_service is not None
        self._usage_summary = None
        self._usage_saved = False
        self._usage_commit_failed = False
        saved_wd_models=tuple(dict.fromkeys(
            value for value in getattr(settings,"enabled_wd_taggers",())
            if value in WD_TAGGER_MODELS
        ))
        saved_wd_minimum=max(1,int(getattr(settings,"wd_consensus_minimum",1)))
        self._initial_enabled_wd_taggers=copy.deepcopy(
            list(getattr(settings,"enabled_wd_taggers",()))
        )
        self._initial_wd_consensus_minimum=int(
            getattr(settings,"wd_consensus_minimum",1)
        )
        self._ai_setup_queued_in_session=False
        saved_wd_preset=matching_wd_setup_preset(saved_wd_models,saved_wd_minimum)
        self._initial_wd_preset_key=(
            "balanced" if self._initial_status=="not_started" else saved_wd_preset
        )
        balanced=WD_SETUP_PRESETS["balanced"]
        self._custom_wd_model_ids=(saved_wd_models or balanced.model_ids)
        self._custom_wd_consensus_minimum=min(
            saved_wd_minimum,max(1,len(self._custom_wd_model_ids))
        )
        self._detected_gpu_profile=None
        self._hardware_note=""
        initial=(
            WD_SETUP_PRESETS[self._initial_wd_preset_key]
            if self._initial_wd_preset_key in WD_SETUP_PRESETS else None
        )
        self.recommended_ai_plan=recommended_first_run_ai_setup(
            detect=False,wd_preset_key=self._initial_wd_preset_key,
            wd_model_ids=(
                initial.model_ids if initial is not None
                else self._custom_wd_model_ids
            ),
            wd_consensus_minimum=(
                initial.consensus_minimum if initial is not None
                else self._custom_wd_consensus_minimum
            ),
        )
        self._session_checkpointed = False
        self.outcome = ""
        self.open_components_requested = False
        self._completed = set(getattr(settings, "onboarding_completed_steps", ()))
        # Keep persisted legacy step IDs while presenting useful categories.
        self.steps = ("locations", "components", "behavior", "review")
        self.step_titles = ("Library locations", "AI setup", "Preferences and defaults", "Review and finish")
        self.setWindowTitle("Guided Setup — CollectiGen")
        self.setModal(True); self.resize(980, 720); self.setMinimumSize(820, 620)

        self.dependencies = DependencyService(settings)
        self.components = self.dependencies.components(include_sizes=False)
        self.wd_ready = self.dependencies.capability_ready("wd")
        self.joy_ready = self.dependencies.capability_ready("joy")
        self.ai_setup_issue = self._current_ai_setup_issue()
        self.consensus_ready = sum(
            1 for item in self.components if item.action == "wd_model" and item.installed
        ) >= 2

        outer = QVBoxLayout(self); outer.setContentsMargins(22, 18, 22, 18); outer.setSpacing(12)
        heading = QLabel("Set up CollectiGen around your image workflow")
        heading.setObjectName("pageTitle"); heading.setAccessibleName("Guided setup title")
        intro = QLabel(
            "Choose locations and defaults. Saving setup does not scan, move or rewrite media. Local work needs no account and has no telemetry; optional downloads require separate confirmation."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel")
        outer.addWidget(heading); outer.addWidget(intro)
        self.settings_origin = QLabel(self._settings_origin_summary())
        self.settings_origin.setWordWrap(True); self.settings_origin.setTextFormat(Qt.PlainText)
        self.settings_origin.setObjectName("mutedLabel")
        self.settings_origin.setAccessibleName("Configuration source")
        outer.addWidget(self.settings_origin)
        self.step_label = QLabel(); self.step_label.setObjectName("fieldLabel"); outer.addWidget(self.step_label)

        self.pages = QStackedWidget(); outer.addWidget(self.pages, 1)
        self.pages.addWidget(self._locations_page())
        behavior_page = self._behavior_page()
        self.pages.addWidget(self._components_page())
        self.pages.addWidget(behavior_page)
        self.pages.addWidget(self._review_page())

        actions = QHBoxLayout()
        self.skip_button = compact_button(QPushButton("Skip for Now"))
        self.pause_button = compact_button(QPushButton("Save and Continue Later"))
        self.back_button = compact_button(QPushButton("Back"))
        self.next_button = compact_button(QPushButton("Next"), primary=True)
        self.next_button.setDefault(True)
        self.skip_button.setAccessibleDescription("Save the current choices as skipped and continue to the application without scanning a library.")
        self.pause_button.setAccessibleDescription("Save this exact checklist step so guided setup resumes here after restart.")
        self.back_button.setAccessibleDescription("Return to the previous guided setup step without discarding saved choices.")
        self.next_button.setAccessibleDescription("Validate and save this step, then continue without scanning or downloading anything.")
        self.skip_button.clicked.connect(self.skip_for_now)
        self.pause_button.clicked.connect(self.pause)
        self.back_button.clicked.connect(self.go_back)
        self.next_button.clicked.connect(self.go_next)
        actions.addWidget(self.skip_button); actions.addWidget(self.pause_button)
        actions.addStretch(1); actions.addWidget(self.back_button); actions.addWidget(self.next_button)
        outer.addLayout(actions)

        requested = str(getattr(settings, "onboarding_step", "locations"))
        if initial_step in ONBOARDING_STEPS:
            requested=initial_step
        index = self.steps.index(requested) if requested in self.steps else 0
        if (
            reentry and getattr(settings, "onboarding_status", "") == "completed"
            and initial_step not in ONBOARDING_STEPS
        ):
            index = 0
        self.pages.setCurrentIndex(index); self._page_changed()
        QTimer.singleShot(0,self._detect_recommended_hardware)

    def _settings_origin_summary(self):
        local = "Your saved profile settings are prefilled. Changes here stay in this app."
        extra = getattr(self.settings, "_extra", {})
        if not isinstance(extra, dict) or "suite_managed_settings" not in extra:
            return local + " Installing Suite Manager alone does not connect its settings."
        try:
            from core.suite_settings_adoption import metadata, group_fingerprint
            from core.suite_settings_policy import GROUPS
            payload = dict(extra)
            for fields in GROUPS.values():
                for key in fields:
                    payload[key] = getattr(self.settings, key)
            managed = metadata(payload)
            if managed.get("detached"):
                return local + " This profile is detached from Suite Manager."
            labels = {"appearance.theme": "theme", "appearance.accessibility": "accessibility",
                      "library.locations": "library locations"}
            inherited = []; overrides = []
            for group, origin in managed["groups"].items():
                if origin["mode"] == "override":
                    overrides.append(labels[group])
                elif (origin["value_fingerprint"] == group_fingerprint(payload, group)
                      and any(receipt["activated"] and group in receipt["groups"]
                              for receipt in managed["receipts"].values())):
                    inherited.append(labels[group])
            details = []
            if inherited: details.append("Saved Manager defaults: " + ", ".join(inherited) + ".")
            if overrides: details.append("App overrides retained: " + ", ".join(overrides) + ".")
            return local + " " + " ".join(details) + " An active Manager connection is not verified here."
        except (ImportError, ValueError, TypeError, AttributeError, KeyError):
            return local + " Manager provenance could not be verified; current local values are retained."

    def _model_usage_options(self):
        usage = QGroupBox("Optional local model-use history")
        usage_layout = QVBoxLayout(usage)
        self.remember_model_use = QCheckBox("Remember Successful Model Use")
        self.remember_model_use.setAccessibleDescription(
            "Optional local history of exact input models from supported verified successful runs. Saved only when you Finish setup."
        )
        self.model_usage_note = QLabel()
        self.model_usage_note.setWordWrap(True); self.model_usage_note.setTextFormat(Qt.PlainText)
        self.model_usage_retry = compact_button(QPushButton("Retry Reading Model-use Preference"))
        self.model_usage_retry.clicked.connect(lambda: self._load_model_usage_choice(preserve_draft=True))
        usage_layout.addWidget(self.remember_model_use); usage_layout.addWidget(self.model_usage_note)
        usage_layout.addWidget(self.model_usage_retry)
        self._load_model_usage_choice()
        return usage

    def _model_usage_owner(self):
        from core.model_usage import ModelUsageService
        from core.model_resource_registry import ModelResourceRegistry
        from core.model_selection import protected_model_roots
        if self._usage_service is None:
            registry_path = (MODEL_RESOURCES_PATH if self.settings_path == Path(SETTINGS_PATH)
                             else self.settings_path.parent / MODEL_RESOURCES_PATH.name)
            self._usage_service = ModelUsageService(ModelResourceRegistry(registry_path),
                protected_roots=protected_model_roots(self.settings))
        elif not self._usage_service_injected:
            self._usage_service.protected_roots = protected_model_roots(self.settings)
        return self._usage_service

    def _load_model_usage_choice(self, *, preserve_draft=False):
        try:
            summary = self._model_usage_owner().summary()
            draft = self.settings.onboarding_model_usage_choice
            chosen = (self.remember_model_use.isChecked() if preserve_draft else
                      draft if self._initial_status == "in_progress" and type(draft) is bool else summary["enabled"])
            self._usage_summary = summary
            self.remember_model_use.setChecked(chosen)
            self.remember_model_use.setEnabled(True)
            self.model_usage_note.setText(
                "Remember exact models from verified successful runs to suggest Fast storage placement. "
                "History stays bounded and local. No prompts, images or outputs are stored, and no past runs are imported. "
                "This choice takes effect only when you Finish. Change it or reset history later in Models → Storage → Usage and Placement. "
                f"Currently saved: {'On' if summary['enabled'] else 'Off'}."
            )
            self.model_usage_retry.hide()
        except Exception as exc:
            self._usage_summary = None
            self.remember_model_use.setEnabled(False)
            self.model_usage_note.setText(
                f"The model-use preference could not be read. It will remain unchanged if you finish now. {exc}"
            )
            self.model_usage_retry.show()

    def _save_model_usage_choice(self):
        if self._usage_summary is None:
            if self._usage_commit_failed:
                QMessageBox.critical(self, "Model-use preference needs review",
                    "The previous save could not be verified. Retry Reading Model-use Preference in AI setup before finishing.")
                return False
            return True
        try:
            service = self._model_usage_owner()
            chosen = self.remember_model_use.isChecked()
            service.set_enabled(chosen, expected_registry=self._usage_summary["registry_token"])
            self._usage_saved = True
            self._usage_summary = service.summary()
            if self._usage_summary["enabled"] != chosen:
                raise RuntimeError("The saved model-use preference did not match the reviewed choice.")
            self._usage_commit_failed = False
            return True
        except Exception as exc:
            self._usage_commit_failed = True
            # Refresh the stale review token but preserve the user's draft. A
            # second explicit Finish is required; no automatic replacement.
            self._load_model_usage_choice(preserve_draft=True)
            QMessageBox.critical(self, "Model-use preference needs review",
                f"Setup remains unfinished. Review the choice and try Finish again. {exc}")
            return False

    def _locations_page(self):
        page=QWidget(); page_layout=QVBoxLayout(page)
        page_layout.setContentsMargins(0,0,0,0)
        description = QLabel(
            "Choose a location task below. All choices share this Guided Setup step's Next or Save and Continue Later; switching tasks saves nothing. Existing folders are not changed or scanned."
        )
        description.setWordWrap(True); page_layout.addWidget(description)
        self.location_summary=QLabel()
        self.location_summary.setWordWrap(True)
        self.location_summary.setAccessibleName("Location readiness summary")
        page_layout.addWidget(self.location_summary)
        self.location_checks_button=compact_button(QPushButton("Check Locations"))
        self.location_checks_button.setAccessibleDescription(
            "Recheck all chosen folders without scanning images or changing files."
        )
        self.location_checks_button.clicked.connect(self._check_locations)
        page_layout.addWidget(self.location_checks_button,0,Qt.AlignLeft)
        self.location_status=QTextEdit()
        self.location_status.setReadOnly(True)
        self.location_status.setMaximumHeight(150)
        self.location_status.setAccessibleName("Location validation results")
        self.location_status.hide()
        page_layout.addWidget(self.location_status)
        self.location_tasks=QTabWidget()
        self.location_tasks.setDocumentMode(True)
        self.location_tasks.setAccessibleName("Guided setup location tasks")
        self.location_tasks.currentChanged.connect(lambda _index: self.location_status.hide())
        page_layout.addWidget(self.location_tasks,1)

        def task_page(widget):
            scroll=QScrollArea()
            scroll.setWidgetResizable(True)
            scroll.setFrameShape(QFrame.NoFrame)
            scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
            body=QWidget(); layout=QVBoxLayout(body)
            layout.setContentsMargins(8,12,8,12); layout.setSpacing(12)
            layout.addWidget(widget); layout.addStretch(1)
            scroll.setWidget(body)
            return scroll

        libraries=QGroupBox("Named image libraries")
        libraries_layout=QVBoxLayout(libraries)
        libraries_intro=QLabel(
            "Add every collection you want CollectiGen to work with and identify its storage type. "
            "The default controls initial focus only; all named libraries remain peer collections and can be used together."
        )
        libraries_intro.setWordWrap(True)
        libraries_intro.setObjectName("mutedLabel")
        libraries_layout.addWidget(libraries_intro)
        self.library_table=QTableWidget(0,4)
        self.library_table.setHorizontalHeaderLabels(
            ("Name","Type","Folder","Use")
        )
        self.library_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.library_table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.library_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.library_table.setAccessibleName("First-run named image libraries")
        self.library_table.verticalHeader().hide()
        self.library_table.horizontalHeader().setSectionResizeMode(
            0,QHeaderView.ResizeToContents,
        )
        self.library_table.horizontalHeader().setSectionResizeMode(
            1,QHeaderView.ResizeToContents,
        )
        self.library_table.horizontalHeader().setSectionResizeMode(
            2,QHeaderView.Stretch,
        )
        self.library_table.horizontalHeader().setSectionResizeMode(
            3,QHeaderView.ResizeToContents,
        )
        self.library_table.setMinimumHeight(135)
        libraries_layout.addWidget(self.library_table)
        library_actions=QHBoxLayout()
        self.add_library_button=compact_button(
            QPushButton("Add Library…"),primary=True,
        )
        self.edit_library_button=compact_button(QPushButton("Edit…"))
        self.default_library_button=compact_button(QPushButton("Make Default"))
        self.remove_library_button=compact_button(QPushButton("Remove…"))
        self.add_library_button.clicked.connect(self._add_library)
        self.edit_library_button.clicked.connect(self._edit_library)
        self.default_library_button.clicked.connect(self._make_default_library)
        self.remove_library_button.clicked.connect(self._remove_library)
        for control in (
            self.add_library_button,self.edit_library_button,
            self.default_library_button,self.remove_library_button,
        ):
            library_actions.addWidget(control)
        library_actions.addStretch(1)
        libraries_layout.addLayout(library_actions)
        self.library_guidance=QLabel()
        self.library_guidance.setWordWrap(True)
        self.library_guidance.setObjectName("mutedLabel")
        self.library_guidance.setAccessibleName(
            "Selected first-run library consequences"
        )
        libraries_layout.addWidget(self.library_guidance)
        availability_row=QHBoxLayout()
        self.library_availability=QLabel()
        self.library_availability.setWordWrap(True)
        self.library_availability.setAccessibleName("Selected library availability")
        self.library_availability.setObjectName("mutedLabel")
        self.check_library_button=compact_button(QPushButton("Check Again"))
        self.check_library_button.setAccessibleDescription(
            "Recheck selected library folder availability without scanning or changing files."
        )
        self.check_library_button.clicked.connect(self._show_location_status)
        availability_row.addWidget(self.library_availability,1)
        availability_row.addWidget(self.check_library_button)
        libraries_layout.addLayout(availability_row)
        self.library_table.itemSelectionChanged.connect(
            self._library_selection_changed
        )
        self.library_table.itemDoubleClicked.connect(
            lambda _item:self._edit_library()
        )
        self.location_tasks.addTab(task_page(libraries),"Image Libraries")

        group = QGroupBox("Incoming and recovery folders"); grid = QGridLayout(group)
        ai_group = QGroupBox("Optional AI model storage"); ai_grid = QGridLayout(ai_group)
        self.location_fields = {}
        for row, (key, label) in enumerate(LOCATION_SPECS):
            entry = QLineEdit(str(getattr(self.settings, key, "")))
            entry.setAccessibleName(label + " folder")
            entry.setAccessibleDescription(
                "Folder path. Guided setup validates availability, write access, overlap, shared storage, and free space before saving."
            )
            browse = compact_button(QPushButton("Browse…"))
            browse.clicked.connect(lambda _checked=False, item=key: self._browse(item))
            self.location_fields[key] = entry
            entry.textChanged.connect(self._location_choices_changed)
            target=ai_grid if key=="ai_models_directory" else grid
            target_row=0 if key=="ai_models_directory" else row
            target.addWidget(QLabel("Folder" if key=="ai_models_directory" else label),target_row,0)
            target.addWidget(entry,target_row,1)
            target.addWidget(browse,target_row,2)
        self.location_tasks.addTab(task_page(group),"Incoming && Recovery")
        self.location_tasks.addTab(task_page(ai_group),"AI Model Folder")
        self._refresh_library_profiles()
        self._show_location_status()
        return page

    def _behavior_page(self):
        page = QWidget(); page_layout = QVBoxLayout(page); page_layout.setContentsMargins(0, 0, 0, 0)
        self.preferences_scroll = QScrollArea()
        self.preferences_scroll.setWidgetResizable(True); self.preferences_scroll.setFrameShape(QFrame.NoFrame)
        self.preferences_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body = QWidget(); layout = QVBoxLayout(body)
        layout.setContentsMargins(8, 12, 8, 12); layout.setSpacing(12)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        self.preferences_scroll.setWidget(body); page_layout.addWidget(self.preferences_scroll)
        effect = QLabel("Choose defaults for future actions. Existing media is unchanged.")
        effect.setWordWrap(True); effect.setObjectName("mutedLabel"); layout.addWidget(effect)
        preset_group = QGroupBox("Starting point"); preset_grid = QGridLayout(preset_group)
        self.preset = QComboBox()
        self.preset.addItem("Conservative", "conservative")
        self.preset.addItem("Balanced", "balanced")
        self.preset.addItem("Hands-on", "hands_on")
        selected = self.preset.findData(getattr(self.settings, "onboarding_preset", "balanced"))
        self.preset.setCurrentIndex(max(0, selected))
        self.preset_summary = QLabel(); self.preset_summary.setWordWrap(True); self.preset_summary.setObjectName("mutedLabel")
        preset_grid.addWidget(self.preset, 0, 0); preset_grid.addWidget(self.preset_summary, 1, 0)
        layout.addWidget(preset_group)

        choices = QGroupBox("Review individual choices"); grid = QGridLayout(choices)
        self.import_sources = QCheckBox("Import current TXT and Metadata as reviewable evidence")
        self.include_subfolders = QCheckBox("Include subfolders")
        self.validate_start = QCheckBox("Check for new or changed files at startup")
        self.auto_wd = QCheckBox("Generate AI Tags after library checks")
        self.auto_joy = QCheckBox("Generate AI Descriptions after library checks")
        self.auto_merge = QCheckBox("Confirm only safe multi-model Tag consensus automatically")
        self.auto_wd.setEnabled(self.wd_ready); self.auto_joy.setEnabled(self.joy_ready)
        self.auto_merge.setEnabled(self.wd_ready and self.consensus_ready)
        self.update_channel = QComboBox(); self.update_channel.addItem("Stable releases", "stable"); self.update_channel.addItem("Prerelease previews (currently alpha)", "beta")
        self.content_level = QComboBox()
        for rating, label in (
            ("general", "General only by default"),
            ("sensitive", "General and Sensitive by default"),
            ("questionable", "Up to Questionable by default"),
            ("explicit", "All rated images by default"),
        ):
            self.content_level.addItem(label, rating)
        self.blur_general = QCheckBox("General")
        self.blur_sensitive = QCheckBox("Sensitive")
        self.blur_questionable = QCheckBox("Blur questionable previews")
        self.blur_explicit = QCheckBox("Blur explicit previews")
        self.blur_unknown = QCheckBox("Blur previews with no dependable rating yet")
        controls = (
            self.import_sources, self.include_subfolders, self.validate_start,
            self.auto_wd, self.auto_joy, self.auto_merge,
        )
        for row, control in enumerate(controls): grid.addWidget(control, row, 0, 1, 2)
        grid.addWidget(QLabel("Application updates"), 6, 0); grid.addWidget(self.update_channel, 6, 1)
        note = QLabel(
            "Deterministic reviewed merge and global-ignore rules may transform or suppress imported tag findings. Diagnostic conflicts remain review-only. No preset enables downloads, telemetry, destructive organization, or permissive best-effort confirmation."
        )
        note.setWordWrap(True); note.setObjectName("mutedLabel"); grid.addWidget(note, 7, 0, 1, 2)
        layout.addWidget(choices)

        sensitivity=QGroupBox("Sensitivity and preview blurring")
        sensitivity_grid=QGridLayout(sensitivity)
        sensitivity_help=QLabel(
            "Choose what may appear without blur. This changes presentation only; it never changes an image rating, Image Knowledge, or training readiness."
        )
        sensitivity_help.setWordWrap(True); sensitivity_help.setObjectName("mutedLabel")
        sensitivity_grid.addWidget(sensitivity_help,0,0,1,2)
        sensitivity_grid.addWidget(QLabel("Default unblurred range"),1,0)
        sensitivity_grid.addWidget(self.content_level,1,1)
        sensitivity_grid.addWidget(QLabel("Always blur (overrides the range)"),2,0)
        always_blur=QHBoxLayout()
        self.blur_questionable.setText("Questionable")
        self.blur_explicit.setText("Explicit")
        for control in (
            self.blur_general,self.blur_sensitive,
            self.blur_questionable,self.blur_explicit,
        ):
            always_blur.addWidget(control)
        always_blur.addStretch(1)
        sensitivity_grid.addLayout(always_blur,2,1)
        sensitivity_grid.addWidget(self.blur_unknown,3,1)
        self.content_safety_summary=QLabel()
        self.content_safety_summary.setWordWrap(True)
        self.content_safety_summary.setObjectName("mutedLabel")
        sensitivity_grid.addWidget(self.content_safety_summary,4,0,1,2)
        layout.addWidget(sensitivity); layout.addStretch(1)
        self.preset.currentIndexChanged.connect(self._apply_preset_to_controls)
        self.content_level.currentIndexChanged.connect(
            lambda _index:self._update_content_safety_summary()
        )
        for control in (
            self.blur_general,self.blur_sensitive,self.blur_questionable,
            self.blur_explicit,self.blur_unknown,
        ):
            control.toggled.connect(
                lambda _checked:self._update_content_safety_summary()
            )
        self._load_behavior_controls()
        self._update_content_safety_summary()
        return page

    def _components_page(self):
        page=QWidget(); page_layout=QVBoxLayout(page)
        page_layout.setContentsMargins(0,0,0,0)
        self.ai_setup_scroll=QScrollArea()
        self.ai_setup_scroll.setWidgetResizable(True)
        self.ai_setup_scroll.setFrameShape(QFrame.NoFrame)
        self.ai_setup_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        body=QWidget(); self.ai_setup_body=body; layout=QVBoxLayout(body)
        layout.setContentsMargins(8,12,8,12); layout.setSpacing(14)
        layout.setSizeConstraint(QLayout.SetMinimumSize)
        self.ai_setup_scroll.setWidget(body); page_layout.addWidget(self.ai_setup_scroll)
        question = QGroupBox("Would you like local AI analysis?")
        question_layout = QVBoxLayout(question)
        explanation = QLabel(
            "AI setup is optional. Continue without it and configure it later in Setup. Local AI suggestions remain reviewable before becoming Image Knowledge; selecting options does not download or enable automatic analysis."
        )
        explanation.setWordWrap(True)
        self.ai_analysis_interest = QCheckBox("Yes, help me choose and set up local AI analysis")
        self.ai_analysis_interest.setAccessibleDescription(
            "Show the optional local AI choices. This does not download, install, or enable automatic analysis."
        )
        question_layout.addWidget(explanation); question_layout.addWidget(self.ai_analysis_interest)
        layout.addWidget(question)

        self.ai_options = QGroupBox("Local AI setup")
        option_grid = QGridLayout(self.ai_options)
        option_grid.setSizeConstraint(QLayout.SetMinimumSize)
        recommended=QGroupBox("Choose models for this computer")
        self.recommended_ai_group=recommended
        recommended_layout=QVBoxLayout(recommended)
        recommended_layout.setSizeConstraint(QLayout.SetMinimumSize)
        preset_row=QHBoxLayout()
        preset_row.addWidget(QLabel("AI Tag model preset"))
        self.wd_setup_preset=QComboBox()
        for key,preset in WD_SETUP_PRESETS.items():
            self.wd_setup_preset.addItem(preset.label,key)
        self.wd_setup_preset.addItem("Custom","custom")
        selected=self.wd_setup_preset.findData(self._initial_wd_preset_key)
        self.wd_setup_preset.setCurrentIndex(max(0,selected))
        self.wd_setup_preset.setAccessibleDescription(
            "Choose a shortcut for WD AI Tag models, or Custom to select exact models and the matching-vote threshold."
        )
        preset_row.addWidget(self.wd_setup_preset,1)
        recommended_layout.addLayout(preset_row)
        self.wd_preset_summary=QLabel()
        self.wd_preset_summary.setWordWrap(True)
        self.wd_preset_summary.setObjectName("mutedLabel")
        recommended_layout.addWidget(self.wd_preset_summary)

        self.custom_wd_group=QGroupBox("Custom AI Tag models")
        custom_grid=QGridLayout(self.custom_wd_group)
        self.custom_wd_checks={}
        for index,(model_id,(display,_directory)) in enumerate(WD_TAGGER_MODELS.items()):
            control=QCheckBox(display)
            control.setChecked(model_id in self._custom_wd_model_ids)
            control.setToolTip(WD_TAGGER_DESCRIPTIONS[model_id])
            control.setAccessibleDescription(WD_TAGGER_DESCRIPTIONS[model_id])
            self.custom_wd_checks[model_id]=control
            custom_grid.addWidget(control,index//2,index%2)
        custom_grid.addWidget(QLabel("Minimum matching model votes"),3,0)
        self.custom_wd_consensus=QSpinBox()
        self.custom_wd_consensus.setRange(1,max(1,len(self._custom_wd_model_ids)))
        self.custom_wd_consensus.setValue(self._custom_wd_consensus_minimum)
        self.custom_wd_consensus.setAccessibleDescription(
            "The number of selected WD models that must agree before their tag finding reaches the configured consensus threshold."
        )
        custom_grid.addWidget(self.custom_wd_consensus,3,1)
        custom_help=QLabel(
            "Model suggestions remain reviewable evidence. A one-model selection cannot provide independent multi-model consensus."
        )
        custom_help.setWordWrap(True); custom_help.setObjectName("mutedLabel")
        custom_grid.addWidget(custom_help,4,0,1,2)
        self.custom_wd_group.setMinimumHeight(
            max(220,self.custom_wd_group.sizeHint().height())
        )
        recommended_layout.addWidget(self.custom_wd_group)

        self.recommended_ai_summary=QLabel("Checking graphics hardware…")
        self.recommended_ai_summary.setWordWrap(True)
        self.recommended_ai_summary.setObjectName("mutedLabel")
        self.ai_setup_status=QLabel()
        self.ai_setup_status.setWordWrap(True)
        self.ai_setup_status.setObjectName("fieldLabel")
        self.ai_setup_status.setAccessibleName("Local AI setup status")
        self.quick_benchmark=QCheckBox(
            "Run a quick speed benchmark after setup (recommended)"
        )
        self.quick_benchmark.setChecked(True)
        self.quick_benchmark.setAccessibleDescription(
            "After installation, time the selected local AI with disposable synthetic images and save only benchmark measurements and tuned settings."
        )
        self.setup_recommended_button=compact_button(
            QPushButton("Review and Set Up AI…"),primary=True,
        )
        self.setup_recommended_button.setEnabled(False)
        self.setup_recommended_button.clicked.connect(self._setup_recommended_ai)
        recommended_actions=QHBoxLayout()
        recommended_actions.addWidget(self.quick_benchmark)
        recommended_actions.addStretch(1)
        recommended_actions.addWidget(self.setup_recommended_button)
        recommended_layout.addWidget(self.recommended_ai_summary)
        recommended_layout.addWidget(self.ai_setup_status)
        recommended_layout.addLayout(recommended_actions)
        option_grid.addWidget(recommended,0,0,1,3)
        option_grid.setColumnStretch(0, 1)
        layout.addWidget(self.ai_options)

        note = QLabel(
            "Choosing a preset does not install it. Nothing is downloaded or installed until you choose Review and Set Up AI and confirm the exact model, size, source, and license. "
            "You can also finish offline or import a verified model pack later from Setup."
        )
        self.ai_setup_note=note
        note.setWordWrap(True); note.setObjectName("mutedLabel"); layout.addWidget(note)
        layout.addWidget(self._model_usage_options())
        layout.addStretch(1)

        wants_ai = bool(
            self.wd_ready or self.joy_ready
            or
            getattr(self.settings, "auto_run_wd_on_managed_images", False)
            or getattr(self.settings, "auto_run_joy_on_managed_images", False)
        )
        self.ai_analysis_interest.setChecked(wants_ai)
        self.ai_options.setVisible(wants_ai)
        self.ai_analysis_interest.toggled.connect(self._ai_interest_changed)
        self.wd_setup_preset.currentIndexChanged.connect(self._wd_setup_preset_changed)
        for control in self.custom_wd_checks.values():
            control.toggled.connect(self._custom_wd_selection_changed)
        self.custom_wd_consensus.valueChanged.connect(
            lambda _value:self._refresh_recommended_ai_plan()
        )
        self.custom_wd_group.setVisible(
            self.wd_setup_preset.currentData()=="custom"
        )
        self._refresh_recommended_ai_plan()
        return page

    def _ai_interest_changed(self,checked: bool) -> None:
        self.ai_options.setVisible(checked)
        if not checked and not self._ai_setup_queued_in_session:
            self.settings.enabled_wd_taggers=copy.deepcopy(
                self._initial_enabled_wd_taggers
            )
            self.settings.wd_consensus_minimum=self._initial_wd_consensus_minimum

    def _current_ai_setup_issue(self) -> str:
        try:
            issue=self.dependencies.optional_profile_runtime_issue()
        except (OSError,RuntimeError,ValueError) as exc:
            return str(exc)
        return issue if isinstance(issue,str) else ""

    def _detect_recommended_hardware(self):
        if self._gpu_detection_task is not None:
            return
        task=AsyncCallTask(lambda:nvidia_gpu_profile(0))
        self._gpu_detection_task=task
        task.signals.complete.connect(self._recommended_hardware_detected)
        task.signals.failed.connect(self._recommended_hardware_failed)
        QThreadPool.globalInstance().start(task)

    def _recommended_hardware_detected(self,profile):
        self._gpu_detection_task=None
        self._detected_gpu_profile=profile
        self._hardware_note=""
        self._refresh_recommended_ai_plan()

    def _recommended_hardware_failed(self,_message):
        self._gpu_detection_task=None
        self._detected_gpu_profile=None
        self._hardware_note=(
            "Graphics hardware could not be identified; using the safe CPU profile."
        )
        self._refresh_recommended_ai_plan()

    def _wd_setup_preset_changed(self,_index):
        key=str(self.wd_setup_preset.currentData() or "balanced")
        self.custom_wd_group.setVisible(key=="custom")
        if key=="custom":
            self.custom_wd_group.setMinimumHeight(
                max(220,self.custom_wd_group.sizeHint().height())
            )
        self.recommended_ai_group.setMinimumHeight(
            self.recommended_ai_group.sizeHint().height()
            if key=="custom" else 0
        )
        self.ai_options.updateGeometry()
        self._refresh_ai_setup_body_minimum()
        QTimer.singleShot(0,self._refresh_ai_setup_body_minimum)
        self._refresh_recommended_ai_plan()

    def _refresh_ai_setup_body_minimum(self):
        self.ai_setup_body.setMinimumHeight(0)
        self.ai_setup_body.layout().activate()
        self.ai_setup_body.setMinimumHeight(
            self.ai_setup_body.sizeHint().height()
        )

    def _custom_wd_selection_changed(self,_checked):
        selected=sum(control.isChecked() for control in self.custom_wd_checks.values())
        self.custom_wd_consensus.setMaximum(max(1,selected))
        self._refresh_recommended_ai_plan()

    def _selected_wd_configuration(self):
        key=str(self.wd_setup_preset.currentData() or "balanced")
        preset=WD_SETUP_PRESETS.get(key)
        if preset is not None:
            return key,preset.model_ids,preset.consensus_minimum,preset.summary
        model_ids=tuple(
            model_id for model_id,control in self.custom_wd_checks.items()
            if control.isChecked()
        )
        minimum=max(1,min(self.custom_wd_consensus.value(),max(1,len(model_ids))))
        summary=(
            f"{len(model_ids)} manually selected model(s); at least {minimum} matching vote(s)."
            if model_ids else
            "Choose at least one WD model before starting the selected setup."
        )
        return "custom",model_ids,minimum,summary

    def _refresh_recommended_ai_plan(self):
        if not hasattr(self,"wd_setup_preset"):
            return
        key,model_ids,minimum,_summary=self._selected_wd_configuration()
        self.recommended_ai_plan=recommended_first_run_ai_setup(
            gpu_profile=self._detected_gpu_profile,detect=False,
            wd_preset_key=key,wd_model_ids=model_ids,
            wd_consensus_minimum=minimum,
        )
        self._update_recommended_ai_summary(note=self._hardware_note)

    def _update_recommended_ai_summary(self,*,note=""):
        label=getattr(self,"recommended_ai_summary",None)
        if label is None:
            return
        plan=self.recommended_ai_plan
        gpu=(
            f"{plan.gpu_profile.name} • {plan.gpu_profile.total_memory_gib:.1f} GiB VRAM"
            if plan.gpu_profile is not None else "No supported NVIDIA GPU detected"
        )
        _key,model_ids,minimum,preset_summary=self._selected_wd_configuration()
        self.wd_preset_summary.setText(preset_summary)
        wd_names=" + ".join(WD_TAGGER_MODELS[value][0] for value in model_ids)
        joy=(
            f"JoyCaption • {plan.joycaption_quantization.upper()} memory profile • 256 output tokens"
            if plan.joycaption_enabled else
            "JoyCaption omitted because the reviewed profile requires an NVIDIA GPU"
        )
        lines=[value for value in (note,gpu) if value]
        if wd_names:
            consensus=(
                "no multi-model consensus"
                if len(model_ids)==1 else
                f"{minimum} of {len(model_ids)} matching votes"
            )
            lines.append(
                f"AI Tags: {wd_names} • {consensus} • "
                f"{plan.wd_execution_provider.upper()} • batch {plan.wd_batch_size}"
            )
        else:
            lines.append("AI Tags: Choose at least one WD model")
        lines.append(f"AI Descriptions: {joy}")
        label.setText("\n".join(lines))
        ready=self._selected_ai_setup_ready(plan)
        if self.ai_setup_issue:
            self.ai_setup_status.setText(self.ai_setup_issue)
            self.setup_recommended_button.setText("AI Setup Unavailable")
            self.setup_recommended_button.setEnabled(False)
            self.wd_setup_preset.setEnabled(False)
            self.custom_wd_group.setEnabled(False)
            self.quick_benchmark.setVisible(False)
            self.ai_setup_note.setText(
                "Nothing was downloaded or installed. Follow the recovery guidance "
                "above, or continue without local AI. You can also import a verified "
                "model pack later from Setup after the runtime is compatible."
            )
        elif ready:
            self.ai_setup_status.setText("Setup status: Ready to use")
            self.setup_recommended_button.setText("Selected AI Is Ready")
            self.setup_recommended_button.setEnabled(False)
            self.wd_setup_preset.setEnabled(True)
            self.custom_wd_group.setEnabled(True)
            self.quick_benchmark.setVisible(True)
            self.quick_benchmark.setEnabled(True)
            self.ai_setup_note.setText(
                "Choosing a preset does not install it. Nothing is downloaded or "
                "installed until you choose Review and Set Up AI and confirm the "
                "exact model, size, source, and license. You can also finish offline "
                "or import a verified model pack later from Setup."
            )
        else:
            status=(
                "Selected setup is not fully installed"
                if self.wd_ready or self.joy_ready else "Not installed"
            )
            self.ai_setup_status.setText("Setup status: "+status)
            self.setup_recommended_button.setText("Review and Set Up AI…")
            self.setup_recommended_button.setEnabled(bool(model_ids))
            self.wd_setup_preset.setEnabled(True)
            self.custom_wd_group.setEnabled(True)
            self.quick_benchmark.setVisible(True)
            self.quick_benchmark.setEnabled(True)
            self.ai_setup_note.setText(
                "Choosing a preset does not install it. Nothing is downloaded or "
                "installed until you choose Review and Set Up AI and confirm the "
                "exact model, size, source, and license. You can also finish offline "
                "or import a verified model pack later from Setup."
            )

    def _selected_ai_setup_ready(self,plan: RecommendedAiSetup) -> bool:
        wd_ready,joy_ready=self._selected_ai_setup_readiness(plan)
        return wd_ready and joy_ready

    def _selected_ai_setup_readiness(
        self,plan: RecommendedAiSetup,
    ) -> tuple[bool,bool]:
        installed={
            str(getattr(item,"name",""))
            for item in self.components if bool(getattr(item,"installed",False))
        }
        wd_required={
            *WD_RUNTIME_COMPONENTS,
            *(self.dependencies.wd_component_name(value) for value in plan.wd_model_ids),
        }
        joy_required=set()
        if plan.joycaption_enabled:
            joy_required={
                *JOY_RUNTIME_COMPONENTS,JOY_HARDWARE_COMPONENT,
                self.dependencies.plain_language_component_name(JOYCAPTION_MODEL),
            }
            if plan.joycaption_quantization=="nf4":
                joy_required.add("BitsAndBytes")
        return (
            bool(plan.wd_model_ids) and wd_required.issubset(installed),
            joy_required.issubset(installed),
        )

    def _recommended_ai_confirmation(self,plan: RecommendedAiSetup) -> str:
        wd_lines=[]
        for model_id in plan.wd_model_ids:
            terms=MODEL_ACQUISITIONS[model_id]
            wd_lines.append(
                f"• {WD_TAGGER_MODELS[model_id][0]}\n"
                f"  Source: {terms.source_url}\n"
                f"  License: {terms.license_name}\n"
                f"  Revision: {terms.revision}"
            )
        gpu=(
            f"{plan.gpu_profile.name} with {plan.gpu_profile.total_memory_gib:.1f} GiB VRAM"
            if plan.gpu_profile is not None else "no supported NVIDIA GPU"
        )
        joy=""
        if plan.joycaption_enabled:
            terms=MODEL_ACQUISITIONS[JOYCAPTION_MODEL]
            joy=(
                f"\n\nAI Descriptions\n• {PLAIN_LANGUAGE_MODELS[JOYCAPTION_MODEL][0]}\n"
                f"  Source: {terms.source_url}\n"
                f"  License: {terms.license_name}\n"
                f"  Terms: {terms.license_url}\n"
                f"  Revision: {terms.revision}\n"
                f"  Memory profile: {plan.joycaption_quantization.upper()}\n"
                "  Generation: 256 output tokens, 4 beams, deterministic\n"
                f"  {terms.terms_notice}"
            )
        else:
            joy=(
                "\n\nAI Descriptions\n• JoyCaption will not be installed because the "
                "reviewed profile requires an NVIDIA GPU."
            )
        benchmark=(
            "After installation, run a short speed-only check using two disposable synthetic images. "
            "Only timings and tuned settings are saved."
            if self.quick_benchmark.isChecked() else
            "Skip the optional quick benchmark."
        )
        nvidia=(
            "\n\nThe GPU runtime includes NVIDIA CUDA/cuDNN components under NVIDIA proprietary software terms: https://docs.nvidia.com/cuda/eula/index.html"
            if plan.wd_execution_provider=="cuda" or plan.joycaption_enabled else ""
        )
        preset_label=self.wd_setup_preset.currentText()
        consensus=(
            "No multi-model consensus is available with this one-model selection."
            if len(plan.wd_model_ids)==1 else
            f"The configured consensus threshold is {plan.wd_consensus_minimum} of {len(plan.wd_model_ids)} matching model votes."
        )
        return (
            f"Detected profile: {gpu}\n\n"
            f"AI Tag preset: {preset_label}\n"
            f"AI Tags will use {plan.wd_execution_provider.upper()} with batch size {plan.wd_batch_size}. {consensus}\n"
            +"\n".join(wd_lines)+joy+nvidia+
            f"\n\nDestination: {Path(self.settings.ai_models_directory).resolve(strict=False)}\n\n"
            "Setup may download several gigabytes from the exact sources above and installs only reviewed, hash-verified runtime profiles. Analysis runs locally; images are not uploaded. Automatic library analysis remains controlled by your separate behavior choices.\n\n"
            +benchmark+"\n\nContinue with this selected setup?"
        )

    def _apply_recommended_ai_settings(self,plan: RecommendedAiSetup):
        self.settings.enabled_wd_taggers=list(plan.wd_model_ids)
        self.settings.wd_execution_provider=plan.wd_execution_provider
        self.settings.wd_batch_size=plan.wd_batch_size
        self.settings.wd_consensus_minimum=plan.wd_consensus_minimum
        self.settings.suggest_wd_for_missing_tags=True
        if plan.gpu_profile is not None:
            self.settings.joycaption_gpu_index=plan.gpu_profile.index
        self.settings.joycaption_quantization=plan.joycaption_quantization
        self.settings.joycaption_quantization_explicit=True
        self.settings.joycaption_max_new_tokens=256
        self.settings.joycaption_num_beams=4
        self.settings.joycaption_do_sample=False
        if plan.joycaption_enabled:
            self.settings.plain_language_model=JOYCAPTION_MODEL
        self.settings.suggest_joycaption_for_missing_descriptions=plan.joycaption_enabled

    def _setup_recommended_ai(self):
        self._refresh_recommended_ai_plan()
        plan=self.recommended_ai_plan
        if self.ai_setup_issue:
            QMessageBox.information(
                self,"Local AI setup is unavailable",self.ai_setup_issue,
            )
            return
        if not plan.wd_model_ids:
            QMessageBox.information(
                self,"Choose at least one AI Tag model",
                "Select a WD model in Custom, or choose one of the preset shortcuts.",
            )
            return
        if not str(self.settings.ai_models_directory).strip():
            QMessageBox.information(
                self,"Choose the AI model folder first",
                "Return to the Locations step and choose an AI model folder before starting the selected setup.",
            )
            return
        if QMessageBox.question(
            self,"Set Up Selected Local AI",
            self._recommended_ai_confirmation(plan),
        )!=QMessageBox.Yes:
            return
        queue=getattr(self.state,"setup_queue",None)
        jobs=getattr(self.state,"jobs",None)
        if queue is None or jobs is None:
            QMessageBox.information(
                self,"Selected setup is available in the application",
                "Open Guided Setup from the CollectiGen main window so confirmed downloads can use the recoverable Setup queue.",
            )
            return
        self._apply_recommended_ai_settings(plan)
        results=[]
        try:
            save_settings_verified(self.settings,self.settings_path)
            results.append(queue.enqueue_confirmed(
                "setup_wd","Setting up selected AI Tags",
                {"model_ids":list(plan.wd_model_ids)},
            ))
            self._ai_setup_queued_in_session=True
            if plan.joycaption_enabled:
                results.append(queue.enqueue_confirmed(
                    "setup_joy","Setting up selected AI Descriptions",
                    {"model_id":JOYCAPTION_MODEL},
                ))
        except (OSError,RuntimeError,ValueError) as exc:
            title=(
                "Selected setup was only partly queued"
                if results else "Selected setup was not queued"
            )
            detail=str(exc)
            if results:
                detail+=(
                    "\n\nThe AI Tags request was already queued and remains visible "
                    "in Setup > Components. Review it there before changing this setup."
                )
            QMessageBox.warning(self,title,detail)
            return
        self.ai_analysis_interest.setChecked(True)
        operation_ids=[
            value.operation.id for value in results
            if value.operation.state in {"waiting","running"}
        ]
        progress=FirstRunSetupProgressDialog(
            self.settings,queue,jobs,operation_ids,self,
            settings_path=self.settings_path,
            run_benchmark=self.quick_benchmark.isChecked(),
        )
        progress.exec()
        self.dependencies=DependencyService(self.settings)
        self.components=self.dependencies.components(include_sizes=False)
        self.wd_ready=self.dependencies.capability_ready("wd")
        self.joy_ready=self.dependencies.capability_ready("joy")
        self.ai_setup_issue=self._current_ai_setup_issue()
        self.consensus_ready=sum(
            1 for item in self.components
            if item.action=="wd_model" and item.installed
        )>=2
        self.auto_wd.setEnabled(self.wd_ready)
        self.auto_joy.setEnabled(self.joy_ready)
        self.auto_merge.setEnabled(self.wd_ready and self.consensus_ready)
        self._refresh_recommended_ai_plan()
        self._load_behavior_controls(preserve_ai_choices=True)

    def _review_page(self):
        page = QWidget(); layout = QVBoxLayout(page); layout.setContentsMargins(8, 12, 8, 12); layout.setSpacing(10)
        title = QLabel("Review what CollectiGen will remember"); title.setObjectName("sectionTitle")
        self.review_text = QTextEdit(); self.review_text.setReadOnly(True); self.review_text.setAccessibleName("Guided setup review")
        self.first_action = QLabel(); self.first_action.setWordWrap(True); self.first_action.setObjectName("fieldLabel")
        self.first_action.setAccessibleName("First recommended action")
        cache_note = QLabel(
            "Finish saves your configuration. Library scans, media changes and model downloads remain separate actions."
        )
        self.cache_note=cache_note; cache_note.setWordWrap(True); cache_note.setObjectName("mutedLabel")
        layout.addWidget(title); layout.addWidget(self.review_text, 1); layout.addWidget(self.first_action); layout.addWidget(cache_note)
        return page

    def _browse(self, key):
        entry = self.location_fields[key]
        selected = QFileDialog.getExistingDirectory(self, "Choose folder", entry.text().strip() or str(Path.cwd()))
        if selected:
            entry.setText(selected); self._show_location_status()

    def _selected_library_profile(self):
        row=self.library_table.currentRow()
        if row<0:
            return None
        item=self.library_table.item(row,0)
        stable_id=str(item.data(Qt.UserRole) or "") if item is not None else ""
        return self.library_registry.get(stable_id)

    def _refresh_library_profiles(self,select_id=""):
        selected=str(select_id or "")
        if not selected and hasattr(self,"library_table"):
            current=self._selected_library_profile()
            selected=current.stable_id if current is not None else ""
        profiles=self.library_registry.profiles
        self.library_table.setRowCount(len(profiles))
        selected_row=-1
        for row,profile in enumerate(profiles):
            active=profile.stable_id==self._onboarding_active_library_id
            values=(
                profile.name or "Unnamed",profile.display_role,
                profile.root or "No folder","Default" if active else "Peer",
            )
            for column,value in enumerate(values):
                item=QTableWidgetItem(value)
                item.setData(Qt.UserRole,profile.stable_id)
                item.setToolTip(
                    f"{profile.root}\n{profile.display_role}. "
                    "No files are scanned, moved, uploaded, or reclassified by this definition."
                )
                self.library_table.setItem(row,column,item)
            if profile.stable_id==selected:
                selected_row=row
        if selected_row<0 and profiles:
            selected_row=next((
                row for row,profile in enumerate(profiles)
                if profile.stable_id==self._onboarding_active_library_id
            ),0)
        if selected_row>=0:
            self.library_table.selectRow(selected_row)
        else:
            self.library_table.clearSelection()
        self._library_selection_changed()
        if hasattr(self,"location_status"):
            self._show_location_status()

    def _library_selection_changed(self):
        profile=self._selected_library_profile()
        if profile is None:
            self.library_guidance.setText(
                "Add one or more image collections. Each library keeps its own name, folder, "
                "privacy boundary, and availability behavior."
            )
            self.library_availability.setText("")
            self.check_library_button.setEnabled(False)
            self.edit_library_button.setEnabled(False)
            self.default_library_button.setEnabled(False)
            self.remove_library_button.setEnabled(False)
            return
        details=profile.role_details
        explanation=(
            f"{details.label}. {details.summary}"
            if details is not None else
            "This saved library type needs attention before it can be used."
        )
        active=profile.stable_id==self._onboarding_active_library_id
        self.library_guidance.setText(
            ("Default for initial focus. " if active else "Peer image library. ")
            +explanation
        )
        self.check_library_button.setEnabled(True)
        issues=self.library_registry.validate(
            profile,replacing_id=profile.stable_id,
        )
        self.edit_library_button.setEnabled(True)
        self.default_library_button.setEnabled(not active and not issues)
        self.remove_library_button.setEnabled(not active)
        self.default_library_button.setToolTip(
            "Already the default library." if active else
            "Use this collection for initial focus. Other named libraries remain configured."
        )
        self.remove_library_button.setToolTip(
            "Choose another default library before removing this definition."
            if active else
            "Remove only this staged definition. No files or folders are changed."
        )

    def _add_library(self):
        from .library_profile_dialog import LibraryProfileDialog
        dialog=LibraryProfileDialog(self.library_registry,self)
        if not dialog.exec() or dialog.profile is None:
            return
        self.library_registry.add(dialog.profile)
        if not self._onboarding_active_library_id:
            self._onboarding_active_library_id=dialog.profile.stable_id
        self._refresh_library_profiles(dialog.profile.stable_id)

    def _edit_library(self):
        from .library_profile_dialog import LibraryProfileDialog
        current=self._selected_library_profile()
        if current is None:
            return
        dialog=LibraryProfileDialog(
            self.library_registry,self,profile=current,
        )
        if not dialog.exec() or dialog.profile is None:
            return
        self.library_registry.replace(dialog.profile)
        self._refresh_library_profiles(dialog.profile.stable_id)

    def _make_default_library(self):
        profile=self._selected_library_profile()
        if profile is None:
            return
        issues=self.library_registry.validate(
            profile,replacing_id=profile.stable_id,
        )
        if issues:
            QMessageBox.warning(
                self,"Library Details Need Attention",
                "\n".join(f"• {issue.message}" for issue in issues),
            )
            return
        self._onboarding_active_library_id=profile.stable_id
        self._refresh_library_profiles(profile.stable_id)

    def _remove_library(self):
        profile=self._selected_library_profile()
        if profile is None:
            return
        if profile.stable_id==self._onboarding_active_library_id:
            QMessageBox.information(
                self,"Choose Another Default Library",
                "Make another library the default before removing this definition. No files were changed.",
            )
            return
        if QMessageBox.question(
            self,"Remove Library Definition",
            f'Remove the staged definition for "{profile.name}"?\n\nNo image, Image Knowledge, TXT, Metadata, cache, or folder will be deleted or moved.',
            QMessageBox.Yes|QMessageBox.Cancel,QMessageBox.Cancel,
        )!=QMessageBox.Yes:
            return
        self.library_registry.remove(profile.stable_id)
        self._refresh_library_profiles()

    def _location_values(self):
        return {key: entry.text().strip() for key, entry in self.location_fields.items()}

    def _location_draft_changed(self):
        saved={key:str(getattr(self.settings,key,"") or "").strip()
               for key,_label in LOCATION_SPECS}
        return (
            self._location_values()!=saved
            or self.library_registry.to_dicts()!=self.settings.library_registry().to_dicts()
            or self._onboarding_active_library_id!=str(self.settings.active_library_id or "")
        )

    def _can_checkpoint_location_draft(self):
        validation=self._show_location_status()
        if validation.valid or not self._location_draft_changed():
            return True
        QMessageBox.warning(
            self,"Location choices are not saved",
            "The changed library or folder choices need attention and cannot be saved yet. "
            "Guided Setup is staying open so they are not silently lost. Review the "
            "location issues, correct the choices, then try again.\n\n"
            +"\n".join(validation.errors),
        )
        self._focus_location_issue(validation)
        return False

    @staticmethod
    def _size_text(value):
        return DependencyService.format_size(value) if value is not None else "free space unavailable"

    def _location_choices_changed(self,*_args):
        self.location_summary.setText(
            "Folder choices changed. Check Locations or choose Next to validate them."
        )
        self.location_status.hide()

    def _check_locations(self):
        self._show_location_status()
        self.location_status.show()

    def _focus_location_issue(self, validation):
        target=next((item.key for item in validation.locations if not item.valid),None)
        if target is None:
            labels={item.key: (
                f'Image library "{self.library_registry.get(item.key.partition(":")[2]).name}"'
                if item.key.startswith("library:") and
                self.library_registry.get(item.key.partition(":")[2]) is not None
                else dict(LOCATION_SPECS).get(item.key,"")
            ) for item in validation.locations}
            for error in validation.errors:
                matches=[(error.find(label),key) for key,label in labels.items()
                         if label and label in error]
                if matches:
                    target=min(matches)[1]
                    break
        if target and target.startswith("library:"):
            self.location_tasks.setCurrentIndex(0)
            stable_id=target.partition(":")[2]
            for row in range(self.library_table.rowCount()):
                item=self.library_table.item(row,0)
                if item is not None and item.data(Qt.UserRole)==stable_id:
                    self.library_table.selectRow(row)
                    break
            QTimer.singleShot(0,self.library_table.setFocus)
        elif target in self.location_fields:
            self.location_tasks.setCurrentIndex(
                2 if target=="ai_models_directory" else 1
            )
            QTimer.singleShot(0,self.location_fields[target].setFocus)
        else:
            self.location_tasks.setCurrentIndex(0)
            QTimer.singleShot(0,self.library_table.setFocus)
        self.location_status.show()

    def _show_location_status(self):
        result = validate_locations(
            self._location_values(),
            library_profiles=self.library_registry.profiles,
        )
        selected=self._selected_library_profile()
        if selected is not None:
            assessment=next((item for item in result.locations
                             if item.key==f"library:{selected.stable_id}"),None)
            if assessment is None or not assessment.valid:
                availability=(
                    "Needs attention. Review the location issues below, then Edit… and Check Again."
                )
            elif not assessment.available:
                availability=(
                    "Unavailable now. Reconnect the folder or Edit… its path, then Check Again. "
                    "This library definition can still be saved; use requires a fresh check."
                )
            elif not assessment.writable:
                availability=(
                    "Read-only now. Browsing may work, but writes need access. "
                    "Restore access or Edit… the path, then Check Again."
                )
            elif "will need to be created" in assessment.detail:
                availability=(
                    "Folder is not created yet. Create it outside CollectiGen or Edit… the path, "
                    "then Check Again."
                )
            else:
                availability="Available now. No library scan or file change was started."
            self.library_availability.setText(availability)
        groups=(
            ("Image libraries",[item for item in result.locations
                                if item.key.startswith("library:")]),
            ("Incoming and recovery",[item for item in result.locations
                                      if item.key in {"ingest_directory","problems_directory","recycle_directory"}]),
            ("AI model folder",[item for item in result.locations
                                if item.key=="ai_models_directory"]),
        )
        lines=[]
        if result.errors:
            lines.extend(["Needs attention",*("  "+value for value in result.errors)])
        for heading,items in groups:
            if not items:
                continue
            if lines:
                lines.append("")
            lines.append(heading)
            for item in items:
                if item.key.startswith("library:"):
                    profile=self.library_registry.get(item.key.partition(":")[2])
                    label=f'"{profile.name}"' if profile is not None else "Image library"
                else:
                    label=dict(LOCATION_SPECS)[item.key]
                free=f" · {self._size_text(item.free_bytes)} free" if item.free_bytes is not None else ""
                detail=item.detail.rstrip(".") if free else item.detail
                lines.append(f"  {label}: {detail}{free}")
        if result.warnings:
            lines.extend(["","Before use",*("  "+value for value in result.warnings)])
        self.location_status.setPlainText("\n".join(lines))
        self.location_status.setAccessibleDescription(
            "Storage validation passed." if result.valid else
            f"Storage validation needs attention: {len(result.errors):,} issue(s)."
        )
        if result.errors:
            count=len(result.errors)
            summary=f"{count:,} location {'issue needs' if count==1 else 'issues need'} correction before saving."
        elif result.warnings:
            summary=("Locations can be saved. "
                     f"{len(result.warnings):,} item(s) need a fresh check before use.")
        else:
            summary="Locations can be saved. No folders are scanned or changed here."
        self.location_summary.setText(summary)
        return result

    def _update_content_safety_summary(self):
        if not hasattr(self,"content_safety_summary"):
            return
        level=str(self.content_level.currentData() or "sensitive")
        labels={
            "general":"General",
            "sensitive":"General and Sensitive",
            "questionable":"General, Sensitive, and Questionable",
            "explicit":"all rated images",
        }
        overrides=[
            rating.title() for rating,control in (
                ("general",self.blur_general),
                ("sensitive",self.blur_sensitive),
                ("questionable",self.blur_questionable),
                ("explicit",self.blur_explicit),
            ) if control.isChecked()
        ]
        override=(
            " Always-blur overrides: "+", ".join(overrides)+"."
            if overrides else " No rating-specific always-blur override is selected."
        )
        unknown=(
            " Images without a dependable rating are also blurred."
            if self.blur_unknown.isChecked() else
            " Images without a dependable rating may appear unblurred."
        )
        self.content_safety_summary.setText(
            f"Effective result: {labels[level]} may appear unblurred by default; higher ratings are blurred."
            +override+unknown
        )

    def _apply_preset_to_controls(self):
        draft = copy.deepcopy(self.settings)
        name = str(self.preset.currentData() or "balanced")
        apply_starting_preset(
            draft, name, wd_ready=self.wd_ready, joy_ready=self.joy_ready,
            consensus_ready=self.consensus_ready,
        )
        controls = (
            (self.import_sources, draft.auto_import_source_evidence),
            (self.include_subfolders, draft.include_subfolders),
            (self.validate_start, draft.validate_library_on_startup),
            (self.blur_general, "general" in draft.gallery_hidden_ratings),
            (self.blur_sensitive, "sensitive" in draft.gallery_hidden_ratings),
            (self.blur_questionable, "questionable" in draft.gallery_hidden_ratings),
            (self.blur_explicit, "explicit" in draft.gallery_hidden_ratings),
            (self.blur_unknown, draft.gallery_blur_unknown),
        )
        for control, checked in controls:
            blocker = QSignalBlocker(control); control.setChecked(bool(checked)); del blocker
        self.update_channel.setCurrentIndex(max(0, self.update_channel.findData(draft.update_channel)))
        self.content_level.setCurrentIndex(max(0, self.content_level.findData(draft.gallery_content_level)))
        self._update_content_safety_summary()
        self.preset_summary.setText(" ".join(preset_consequences(
            name, wd_ready=self.wd_ready, joy_ready=self.joy_ready,
            consensus_ready=self.consensus_ready,
        )))

    def _load_behavior_controls(self, *, preserve_ai_choices=False):
        settings = self.settings
        controls = (
            (self.import_sources, settings.auto_import_source_evidence),
            (self.include_subfolders, settings.include_subfolders),
            (self.validate_start, settings.validate_library_on_startup),
            (self.auto_wd, self.auto_wd.isChecked() if preserve_ai_choices else settings.auto_run_wd_on_managed_images),
            (self.auto_joy, self.auto_joy.isChecked() if preserve_ai_choices else settings.auto_run_joy_on_managed_images),
            (self.auto_merge, self.auto_merge.isChecked() if preserve_ai_choices else settings.auto_merge_ai_tags),
            (self.blur_general, "general" in settings.gallery_hidden_ratings),
            (self.blur_sensitive, "sensitive" in settings.gallery_hidden_ratings),
            (self.blur_questionable, "questionable" in settings.gallery_hidden_ratings),
            (self.blur_explicit, "explicit" in settings.gallery_hidden_ratings),
            (self.blur_unknown, getattr(settings, "gallery_blur_unknown", True)),
        )
        for control, checked in controls: control.setChecked(bool(checked))
        for control, ready in (
            (self.auto_wd,self.wd_ready), (self.auto_joy,self.joy_ready),
            (self.auto_merge,self.wd_ready and self.consensus_ready),
        ):
            control.setEnabled(ready or control.isChecked())
            control.setToolTip(
                "Your choice is retained through presets and setup. Available models do not opt you in."
                if ready else
                "Required models/runtime are not ready. A saved choice is retained, but cannot run until ready; uncheck to opt out."
            )
        self.update_channel.setCurrentIndex(max(0, self.update_channel.findData(settings.update_channel)))
        self.content_level.setCurrentIndex(max(0, self.content_level.findData(settings.gallery_content_level)))
        self._update_content_safety_summary()
        name = str(self.preset.currentData() or "balanced")
        self.preset_summary.setText(" ".join(preset_consequences(
            name, wd_ready=self.wd_ready, joy_ready=self.joy_ready,
            consensus_ready=self.consensus_ready,
        )))

    def _collect(self, *, require_valid_locations=False):
        validation = self._show_location_status()
        if require_valid_locations and not validation.valid:
            QMessageBox.warning(self, "Review storage locations", "\n".join(validation.errors))
            self._focus_location_issue(validation)
            return False
        if validation.valid:
            for key, value in self._location_values().items(): setattr(self.settings, key, value)
            self.settings.set_library_registry(
                self.library_registry,
                active_library_id=self._onboarding_active_library_id,
            )
        self.settings.onboarding_preset = str(self.preset.currentData() or "balanced")
        if self._usage_summary is not None:
            self.settings.onboarding_model_usage_choice = self.remember_model_use.isChecked()
        self.settings.auto_import_source_evidence = self.import_sources.isChecked()
        self.settings.include_subfolders = self.include_subfolders.isChecked()
        self.settings.validate_library_on_startup = self.validate_start.isChecked()
        self.settings.auto_run_wd_on_managed_images = self.auto_wd.isChecked()
        self.settings.auto_run_joy_on_managed_images = self.auto_joy.isChecked()
        self.settings.auto_merge_ai_tags = self.auto_merge.isChecked()
        self.settings.update_channel = str(self.update_channel.currentData() or "stable")
        self.settings.gallery_content_level = str(self.content_level.currentData() or "sensitive")
        self.settings.gallery_hidden_ratings = [
            rating for rating, control in (
                ("general",self.blur_general),
                ("sensitive",self.blur_sensitive),
                ("questionable", self.blur_questionable),
                ("explicit", self.blur_explicit),
            )
            if control.isChecked()
        ]
        self.settings.gallery_blur_unknown = self.blur_unknown.isChecked()
        self._refresh_recommended_ai_plan()
        if self.ai_analysis_interest.isChecked():
            self.settings.enabled_wd_taggers=list(self.recommended_ai_plan.wd_model_ids)
            self.settings.wd_consensus_minimum=(
                self.recommended_ai_plan.wd_consensus_minimum
            )
        return True

    def _save_checkpoint(self, status, step):
        try:
            checkpoint_onboarding(
                self.settings, status=status, step=step,
                completed_steps=self._completed, path=self.settings_path,
            )
            self._session_checkpointed = True
            return True
        except Exception as exc:
            detail = str(exc)
            if status == "completed" and self._usage_saved:
                detail += "\nThe model-use preference was saved, but setup is not marked complete. Try Finish again."
                self.settings.onboarding_status = "in_progress"
            QMessageBox.critical(self, "Guided setup was not saved", detail); return False

    def _update_review(self):
        values = self._location_values(); unresolved = []
        lines = ["Locations"]
        profiles=self.library_registry.profiles
        if profiles:
            lines.append(f"• Named image libraries: {len(profiles):,}")
            for profile in profiles:
                marker=(
                    " • Default for initial focus"
                    if profile.stable_id==self._onboarding_active_library_id else
                    ""
                )
                lines.append(
                    f"  • {profile.name} — {profile.display_role} — {profile.root}{marker}"
                )
        else:
            lines.append("• Named image libraries: None configured")
            unresolved.append("Image libraries")
        for key, label in LOCATION_SPECS:
            value = values[key]
            lines.append(f"• {label}: {value or 'Not configured'}")
            if not value and key != "ai_models_directory": unresolved.append(label)
        installed_size = sum(max(0, int(item.size_bytes)) for item in self.components if item.installed)
        lines.extend((
            "", "Behavior",
            f"• Starting point: {self.preset.currentText()}",
            (f"• Remember Successful Model Use: {'On' if self.remember_model_use.isChecked() else 'Off'} when you Finish"
             if self._usage_summary is not None else "• Remember Successful Model Use: Unavailable; existing preference will remain unchanged"),
            f"• TXT and Metadata evidence import: {'On' if self.import_sources.isChecked() else 'Off'}",
            f"• Automatic AI Tag generation: {'On' if self.auto_wd.isChecked() else 'Off'}"+(" (waiting for ready models/runtime)" if self.auto_wd.isChecked() and not self.wd_ready else ""),
            f"• Automatic AI Description generation: {'On' if self.auto_joy.isChecked() else 'Off'}"+(" (waiting for ready models/runtime)" if self.auto_joy.isChecked() and not self.joy_ready else ""),
            f"• Safe AI consensus application: {'On' if self.auto_merge.isChecked() else 'Off'}"+(" (waiting for ready models/runtime and consensus)" if self.auto_merge.isChecked() and not (self.wd_ready and self.consensus_ready) else ""),
            f"• Startup change validation: {'On' if self.validate_start.isChecked() else 'Off'}",
            f"• Content presentation: {self.content_level.currentText()}; unknown protection {'On' if self.blur_unknown.isChecked() else 'Off'}",
            "", "Components",
            f"• Installed component and model size: {DependencyService.format_size(installed_size)}",
        ))
        if self.ai_analysis_interest.isChecked():
            selected_wd_ready,selected_joy_ready=(
                self._selected_ai_setup_readiness(self.recommended_ai_plan)
            )
            joy_state=(
                "Not selected on this hardware"
                if not self.recommended_ai_plan.joycaption_enabled else
                "Ready" if selected_joy_ready else "Selected / not ready"
            )
            lines.extend((
                f"• Local AI setup: {self.wd_setup_preset.currentText()}",
                f"• Selected AI Tag models: {len(self.recommended_ai_plan.wd_model_ids)}; matching-vote threshold {self.recommended_ai_plan.wd_consensus_minimum}",
                f"• AI Tag generation: {'Ready' if selected_wd_ready else 'Selected / not ready'}",
                f"• AI Description generation: {joy_state}",
            ))
        else:
            lines.append("• Local AI setup: Not selected")
        lines.append(
            "• Backups, recovery journals, settings, and rebuildable indexes remain machine-local under CollectiGen data."
        )
        if unresolved:
            lines.extend(("", "Unresolved optional steps", "• " + ", ".join(unresolved)))
        first = (
            "Open Components and review optional local capabilities."
            if self._has_unfinished_ai_setup() else
            "Open the included image libraries and reuse their saved Dataset views."
            if profiles else
            "Review incoming images."
            if values["ingest_directory"] else
            "Choose an image library when you are ready."
        )
        self.first_action.setText("First recommended action • "+first)
        self.review_text.setPlainText("\n".join(lines))

    def _has_unfinished_ai_setup(self):
        if not self.ai_analysis_interest.isChecked():
            return False
        self._refresh_recommended_ai_plan()
        return not self._selected_ai_setup_ready(self.recommended_ai_plan)

    def _confirm_unfinished_ai_setup(self) -> str:
        message=QMessageBox(self)
        message.setIcon(QMessageBox.Question)
        message.setWindowTitle("Finish local AI setup")
        message.setText("Local AI is selected, but the chosen setup is not ready yet.")
        if self.ai_setup_issue:
            message.setInformativeText(
                self.ai_setup_issue+"\n\nContinue without local AI, or stay on this step."
            )
            setup_button=None
        else:
            message.setInformativeText(
                "Review and set up the selected models before continuing, or "
                "continue without local AI."
            )
            setup_button=message.addButton(
                "Review and Set Up AI…",QMessageBox.AcceptRole,
            )
            message.setDefaultButton(setup_button)
        without_button=message.addButton(
            "Continue Without AI",QMessageBox.ActionRole,
        )
        stay_button=message.addButton("Stay Here",QMessageBox.RejectRole)
        if setup_button is None:
            message.setDefaultButton(stay_button)
        message.exec()
        clicked=message.clickedButton()
        if setup_button is not None and clicked is setup_button:
            return "setup"
        if clicked is without_button:
            return "without"
        return "stay"

    def _page_changed(self):
        index = self.pages.currentIndex(); self.step_label.setText(f"Step {index + 1} of {self.pages.count()} • {self.step_titles[index]}")
        self.step_label.setAccessibleName(self.step_label.text())
        reviewing=index==self.pages.count()-1
        self.skip_button.setVisible(not reviewing)
        self.pause_button.setVisible(not reviewing)
        self.back_button.setEnabled(index > 0)
        self.next_button.setText("Finish" if reviewing else "Next")
        if self.steps[index] == "locations": self._show_location_status()
        if reviewing: self._update_review()

    def go_next(self):
        index = self.pages.currentIndex()
        if self.steps[index]=="components" and self._has_unfinished_ai_setup():
            action=self._confirm_unfinished_ai_setup()
            if action=="setup":
                self._setup_recommended_ai()
                return
            if action!="without":
                return
            self.ai_analysis_interest.setChecked(False)
        if not self._collect(require_valid_locations=True): return
        self._completed.add(self.steps[index])
        if index == self.pages.count() - 1:
            self._completed.update(ONBOARDING_STEPS)
            if not self._save_checkpoint("in_progress", "review"): return
            if not self._save_model_usage_choice(): return
            if not self._save_checkpoint("completed", "review"): return
            self.open_components_requested = self._has_unfinished_ai_setup()
            self.outcome = "completed"; self.accept(); return
        next_index = index + 1
        if not self._save_checkpoint("in_progress", self.steps[next_index]): return
        self.pages.setCurrentIndex(next_index); self._page_changed()

    def go_back(self):
        index = self.pages.currentIndex()
        if index <= 0: return
        if not self._collect(require_valid_locations=False): return
        previous = index - 1
        if not self._save_checkpoint("in_progress", self.steps[previous]): return
        self.pages.setCurrentIndex(previous); self._page_changed()

    def pause(self):
        if not self._can_checkpoint_location_draft(): return
        self._collect(require_valid_locations=False)
        step = self.steps[self.pages.currentIndex()]
        if not self._save_checkpoint("in_progress", step): return
        self.outcome = "paused"; self.done(QDialog.Rejected)

    def skip_for_now(self):
        if not self._can_checkpoint_location_draft(): return
        self._collect(require_valid_locations=False)
        step = self.steps[self.pages.currentIndex()]
        if not self._save_checkpoint("skipped", step): return
        self.outcome = "skipped"; self.done(QDialog.Rejected)

    def reject(self):
        if self.reentry and self._initial_status == "completed" and not self._session_checkpointed:
            self.done(QDialog.Rejected)
            return
        self.pause()
