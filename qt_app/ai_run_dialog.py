from __future__ import annotations

from pathlib import Path
from threading import Event
import time

from PySide6.QtCore import QSize,Qt,QThreadPool,Signal,QTimer
from PySide6.QtWidgets import (
    QAbstractItemView,QCheckBox,QComboBox,QDialogButtonBox,QGridLayout,QGroupBox,
    QHeaderView,QLabel,QLineEdit,QProgressBar,QPushButton,QSizePolicy,QSpinBox,QTableView,
    QVBoxLayout,QWidget,
)

from core import (
    OperationJournal,PreparationOptions,PreparationRunResult,
    UnifiedPreparationService,USER_DATA_DIR,configured_plain_language_backend,
    configured_wd_backends,
)

from .components import (
    IncrementalResultModel,IncrementalResultRow,ai_estimate_text,
    bounded_window_size,compact_button,set_progress_bar,show_operation_in_progress,
    window_status_footer,
)
from .dependency_dialog import ensure_capability
from .modal_feedback import FeedbackEnabledDialog
from .workers import CancelableProgressCallTask,AsyncCallTask,ProgressCallSignals


_OUTCOME_LABELS={
    "completed":"Completed",
    "partial":"Partial",
    "skipped":"Already current",
    "conflicted":"Conflict held",
    "failed":"Failed",
}


class RunAIDialog(FeedbackEnabledDialog):
    """One shared, cancellable AI-only workflow for every image surface."""

    feedback_workflow="Run AI"
    images_changed=Signal(object)

    def __init__(
        self,state,settings,images,*,owner: str,scope: str="selection",
        active_image: str | Path | None=None,pool=None,parent=None,
        preselect: str="",force: bool=False,journal=None,
        history_operation_factory=None,history_opener=None,
        initial_options: PreparationOptions | None=None,scope_admission=None,
        lmstudio_connection_service=None,lmstudio_discovery=None,lmstudio_dispatch=None,
    ):
        super().__init__(parent)
        self.state=state
        self.settings=settings
        self.images=tuple(dict.fromkeys(Path(value) for value in images))
        self.owner=str(owner)
        self.scope=str(scope)
        self.active_image=Path(active_image) if active_image else None
        self.pool=pool or QThreadPool.globalInstance()
        self.journal=journal or OperationJournal(
            USER_DATA_DIR/"operation_journal.jsonl"
        )
        self.result_data: PreparationRunResult | None=None
        self.review_requested=False
        self._task=None
        self.history_operation_factory=history_operation_factory
        self.history_opener=history_opener
        self._history_operation=None
        self._history_target=None
        self._setup_opened_for_run=False
        self._submitted_options: PreparationOptions | None=None
        self.scope_admission=scope_admission
        self._lm_service=lmstudio_connection_service
        self._lm_discovery=lmstudio_discovery
        self._lm_dispatch=lmstudio_dispatch
        self._lm_snapshot=None
        self._lm_auth_endpoint=None
        self._lm_refresh_task=None
        self._lm_refresh_outcome=None
        self._lm_refresh_invocation=None
        self._lm_analysis_outcome=None
        self._lm_cancel=Event()
        self._lm_close_pending=False
        self._local_draft=None
        self._lm_settle=QTimer(self)
        self._lm_settle.setInterval(20)
        self._lm_settle.timeout.connect(self._settle_external)
        self.latest_retry_result: PreparationRunResult | None=None
        self._latest_retry_dialog: RunAIDialog | None=None
        self.review_paths_requested: tuple[Path,...]=()

        self.setWindowTitle("Run AI")
        self.resize(980,700)
        layout=QVBoxLayout(self)

        heading=QLabel("Run AI on images")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        self.scope_label=QLabel(self._scope_text())
        self.scope_label.setWordWrap(True)
        self.scope_label.setAccessibleName("AI run image scope")
        layout.addWidget(self.scope_label)
        self.retry_context=QLabel(
            "Only the recorded failed images are in this new review. Completed, partial, "
            "already-current and conflicted images are excluded. Check the options, then "
            "choose Run AI; nothing starts by opening this review."
        )
        self.retry_context.setWordWrap(True)
        self.retry_context.setVisible(self.scope=="retry")
        layout.addWidget(self.retry_context)

        choices=QGroupBox("Generate")
        choice_layout=QGridLayout(choices)
        self.tags=QCheckBox("AI Tags")
        self.tags.setChecked(preselect in {"","tags","both"})
        self.tags.setToolTip(
            "Run every enabled AI Tag model and save its typed findings as "
            "reviewable Image Knowledge evidence."
        )
        self.descriptions=QCheckBox("AI Descriptions")
        self.descriptions.setChecked(preselect in {"","description","descriptions","both"})
        self.descriptions.setToolTip(
            "Run the preferred AI Description model and save its result as a "
            "reviewable finding."
        )
        choice_layout.addWidget(self.tags,0,0)
        choice_layout.addWidget(self.descriptions,0,1)
        self.confirm_tags=QCheckBox("Confirm safe agreed Tags")
        self.confirm_tags.setChecked(bool(getattr(settings,"auto_merge_ai_tags",False)))
        self.confirm_tags.setToolTip(
            "Confirm only known Tags supported by the configured independent-vote "
            "minimum when they introduce no conflict. Ambiguous results stay in review."
        )
        choice_layout.addWidget(self.confirm_tags,1,0,1,2)
        layout.addWidget(choices)

        freshness=QGroupBox("Existing AI results")
        freshness_layout=QGridLayout(freshness)
        freshness_layout.addWidget(QLabel("Reuse matching results from the last"),0,0)
        self.age_value=QSpinBox()
        self.age_value.setRange(1,999)
        self.age_value.setValue(max(1,int(getattr(settings,"prepare_ai_skip_value",7))))
        freshness_layout.addWidget(self.age_value,0,1)
        self.age_unit=QComboBox()
        self.age_unit.addItem("Hours","hours")
        self.age_unit.addItem("Days","days")
        unit=str(getattr(settings,"prepare_ai_skip_unit","days"))
        self.age_unit.setCurrentIndex(max(0,self.age_unit.findData(unit)))
        freshness_layout.addWidget(self.age_unit,0,2)
        self.force=QCheckBox("Force a fresh run")
        self.force.setChecked(bool(force))
        self.force.setToolTip(
            "Ignore matching current AI results and replace them with a fresh run. "
            "Use this after changing models or generation settings."
        )
        freshness_layout.addWidget(self.force,0,3)
        freshness_layout.setColumnStretch(4,1)
        layout.addWidget(freshness)
        if initial_options is not None:
            self.tags.setChecked(initial_options.suggest_sdxl_tags)
            self.descriptions.setChecked(initial_options.suggest_plain_language)
            self.confirm_tags.setChecked(initial_options.confirms_safe_tags)
            self.age_value.setValue(initial_options.skip_ai_value)
            index=self.age_unit.findData(initial_options.skip_ai_unit)
            if index>=0:
                self.age_unit.setCurrentIndex(index)
            self.force.setChecked(initial_options.force_all_ai)

        self.description_provider=QComboBox()
        self.description_provider.setAccessibleName("Description provider")
        self.description_provider.addItem("Local AI models","local")
        self.description_provider.addItem("LM Studio server","lmstudio")
        layout.addWidget(self.description_provider)
        self.lm_panel=QGroupBox("LM Studio descriptions")
        lm_layout=QGridLayout(self.lm_panel)
        self.lm_connection=compact_button(QPushButton("Connection Preferences…"))
        self.lm_refresh=compact_button(QPushButton("Refresh Models"))
        lm_layout.addWidget(self.lm_connection,0,0)
        lm_layout.addWidget(self.lm_refresh,0,1)
        self.lm_models=QComboBox()
        self.lm_models.setAccessibleName("Observed loaded vision instance")
        self.lm_models.addItem("Refresh, then choose an exact loaded instance",None)
        lm_layout.addWidget(self.lm_models,1,0,1,2)
        self.lm_status=QLabel("Choose a saved connection, then refresh models explicitly.")
        self.lm_status.setWordWrap(True)
        lm_layout.addWidget(self.lm_status,2,0,1,2)
        self.lm_options=compact_button(QPushButton("Description Options…"))
        lm_layout.addWidget(self.lm_options,3,0,1,2)
        self.lm_advanced=QWidget()
        advanced=QGridLayout(self.lm_advanced)
        self.lm_prompt=QLineEdit("Describe this image.")
        self.lm_prompt.setMaxLength(1024)
        self.lm_prompt.setAccessibleName("External description prompt")
        self.lm_output=QSpinBox();self.lm_output.setRange(1,4096);self.lm_output.setValue(256)
        advanced.addWidget(QLabel("Prompt"),0,0);advanced.addWidget(self.lm_prompt,0,1)
        advanced.addWidget(QLabel("Maximum output tokens"),1,0);advanced.addWidget(self.lm_output,1,1)
        lm_layout.addWidget(self.lm_advanced,4,0,1,2)
        from .lmstudio_connection_dialog import discovery_auth_controls
        self.lm_auth_panel,self.lm_auth_token,self.lm_auth_toggle=discovery_auth_controls(self)
        lm_layout.addWidget(self.lm_auth_panel,5,0,1,2)
        self.lm_auth_toggle.toggled.connect(self._expand_discovery_auth)
        self.lm_advanced.hide();self.lm_panel.hide()
        layout.addWidget(self.lm_panel)
        self.lm_options.clicked.connect(lambda:self.lm_advanced.setVisible(not self.lm_advanced.isVisible()))
        self.lm_connection.clicked.connect(self._open_lm_connection)
        self.lm_refresh.clicked.connect(self._refresh_external)
        self.lm_models.currentIndexChanged.connect(self._external_choice_changed)
        self.lm_prompt.textChanged.connect(self._external_choice_changed)

        self.policy=QLabel()
        self.policy.setObjectName("mutedLabel")
        self.policy.setWordWrap(True)
        layout.addWidget(self.policy)
        self.estimate=QLabel()
        self.estimate.setObjectName("mutedLabel")
        self.estimate.setWordWrap(True)
        layout.addWidget(self.estimate)
        from .ai_runtime_controls import runtime_policy_text
        self.runtime_memory_note=QLabel(runtime_policy_text(self.settings))
        self.runtime_memory_note.setWordWrap(True)
        self.runtime_memory_note.setAccessibleName("AI runtime memory policy")
        layout.addWidget(self.runtime_memory_note)

        self.idle_spacer=QWidget()
        self.idle_spacer.setSizePolicy(QSizePolicy.Minimum,QSizePolicy.Expanding)
        layout.addWidget(self.idle_spacer,1)

        self.summary=QLabel()
        self.summary.setWordWrap(True)
        self.summary.setAccessibleName("AI run outcome summary")
        self.summary.hide()
        layout.addWidget(self.summary)

        self.result_title=QLabel("Per-image outcomes")
        self.result_title.setObjectName("sectionTitle")
        self.result_title.hide()
        layout.addWidget(self.result_title)

        from .ai_runtime_controls import TaskMemoryHoldPanel
        self.memory_hold=TaskMemoryHoldPanel(self)
        layout.addWidget(self.memory_hold)
        self._memory_retry_options=None
        self.results=QTableView()
        self.results.setAccessibleName("Per-image AI outcomes")
        self.results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results.setAlternatingRowColors(True)
        self.results.verticalHeader().hide()
        self.result_model=IncrementalResultModel(
            ("Image","Outcome","Details"),page_size=200,parent=self,
        )
        self.results.setModel(self.result_model)
        header=self.results.horizontalHeader()
        header.setSectionResizeMode(0,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2,QHeaderView.Stretch)
        self.results.hide()
        layout.addWidget(self.results,1)
        self.more_button=compact_button(QPushButton("Show More"))
        self.more_button.clicked.connect(self.result_model.fetch_more)
        self.more_button.hide()
        layout.addWidget(self.more_button,0,Qt.AlignRight)
        self.result_model.counts_changed.connect(
            lambda loaded,total:self.more_button.setVisible(loaded<total)
        )

        self.progress=QProgressBar()
        self.progress.hide()
        layout.addWidget(self.progress)
        from .components import bind_status_footer,IndeterminateActivityBar
        self.lm_activity=IndeterminateActivityBar(self)
        self.lm_activity.hide()
        bind_status_footer(self,self.progress,self.lm_activity,title="AI analysis")
        self.buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.cancel_button=self.buttons.button(QDialogButtonBox.Cancel)
        self.cancel_button.clicked.connect(self._cancel_or_close)
        self.review_button=compact_button(
            self.buttons.addButton("Review Findings…",QDialogButtonBox.ActionRole)
        )
        self.review_button.clicked.connect(self._review)
        self.review_button.hide()
        self.history_button=compact_button(self.buttons.addButton("Review AI Changes…",QDialogButtonBox.ActionRole))
        self.history_button.clicked.connect(self._open_history)
        self.history_button.hide()
        self.retry_history_button=compact_button(self.buttons.addButton(
            "Review Retry AI Changes…",QDialogButtonBox.ActionRole,
        ))
        self.retry_history_button.clicked.connect(self._open_retry_history)
        self.retry_history_button.hide()
        self.retry_button=compact_button(self.buttons.addButton(
            "Review Failed Images for Retry…",QDialogButtonBox.ActionRole,
        ))
        self.retry_button.clicked.connect(self._open_failed_retry)
        self.retry_button.hide()
        self.run_button=compact_button(
            self.buttons.addButton("Run AI",QDialogButtonBox.AcceptRole),primary=True,
        )
        self.run_button.clicked.connect(self._start)
        layout.addWidget(self.buttons)

        self.tags.toggled.connect(self._choices_changed)
        self.descriptions.toggled.connect(self._choices_changed)
        self.confirm_tags.toggled.connect(self._choices_changed)
        self.force.toggled.connect(self._choices_changed)
        self.age_value.valueChanged.connect(self._choices_changed)
        self.age_unit.currentIndexChanged.connect(self._choices_changed)
        self.description_provider.currentIndexChanged.connect(self._provider_changed)
        self._choices_changed()

    def showEvent(self,event):
        super().showEvent(event)
        screen=self.screen()
        if screen is not None:
            self.resize(bounded_window_size(screen.availableGeometry(),QSize(980,700)))

    def _scope_text(self) -> str:
        count=len(self.images)
        if self.scope=="retry":
            return f"Failed-only retry: {count:,} image(s)"
        if self.scope=="active" and self.active_image is not None:
            return f"Active image: {self.active_image.name}"
        return f"Explicit selection: {count:,} image(s)"

    def _external_mode(self):
        return self.description_provider.currentData()=="lmstudio"

    def _expand_discovery_auth(self, expanded):
        if expanded and self.screen() is not None:
            size=bounded_window_size(self.screen().availableGeometry(),QSize(980,900))
            self.resize(max(self.width(),size.width()),max(self.height(),size.height()))

    def _external_choice_changed(self,*_):
        if hasattr(self,"run_button"):
            self._choices_changed()

    def _connection_service(self):
        if self._lm_service is None:
            from core.settings import DEFAULT_SETTINGS_PATH
            from core.lmstudio_connection_settings import LmStudioConnectionSettings
            self._lm_service=LmStudioConnectionSettings(DEFAULT_SETTINGS_PATH)
        return self._lm_service

    def _invalidate_external(self):
        self._lm_snapshot=None
        self.lm_models.clear()
        self.lm_models.addItem("Refresh, then choose an exact loaded instance",None)

    def _provider_changed(self,*_):
        self.lm_auth_token.clear()
        self._lm_auth_endpoint=None
        external=self._external_mode()
        if external:
            self._local_draft=(self.tags.isChecked(),self.descriptions.isChecked(),self.confirm_tags.isChecked())
            self.tags.setChecked(False);self.descriptions.setChecked(True);self.confirm_tags.setChecked(False)
            self._invalidate_external()
            try:
                snapshot=self._connection_service().observe()
                self._lm_auth_endpoint=snapshot.selection.endpoint if snapshot.selection else None
                self.lm_status.setText(("Image analysis through LM Studio is not available in this build. You can inspect models and save the connection." if not callable(self._lm_dispatch) else
                    "Saved connection available. Refresh to inspect loaded instances.") if snapshot.selection else
                    "No connection saved. Open Connection Preferences first.")
            except Exception:
                self.lm_status.setText("Connection preferences need recovery. Open Connection Preferences.")
        elif self._local_draft is not None:
            for widget,value in zip((self.tags,self.descriptions,self.confirm_tags),self._local_draft):widget.setChecked(value)
        self.lm_panel.setVisible(external)
        self._choices_changed()

    def _open_lm_connection(self):
        if self._task is not None or self._lm_refresh_task is not None:return
        self.lm_auth_token.clear()
        from .lmstudio_connection_dialog import LmStudioConnectionDialog
        dialog=LmStudioConnectionDialog(self._connection_service(),self,discovery=self._lm_discovery)
        dialog.exec()
        try:
            snapshot=self._connection_service().observe()
            self._lm_auth_endpoint=snapshot.selection.endpoint if snapshot.selection else None
        except Exception:
            self._lm_auth_endpoint=None
        self._invalidate_external()
        self.lm_status.setText("Connection review closed. Refresh models before running.")
        self._choices_changed()

    def _refresh_external(self):
        if not self._external_mode() or self._task is not None or self._lm_refresh_task is not None:return
        token=self.lm_auth_token.text() or None
        self.lm_auth_token.clear()
        self._invalidate_external()
        try:
            snapshot=self._connection_service().observe()
            if snapshot.selection is None:raise ValueError()
        except Exception:
            self.lm_status.setText("Save a usable connection in Connection Preferences, then refresh.")
            return
        from core.lmstudio_transport import discover,DiscoveryInvocation
        discovery=self._lm_discovery or discover
        endpoint=snapshot.selection.endpoint
        if token is not None and endpoint!=self._lm_auth_endpoint:
            self.lm_status.setText("The connection address changed. Review Connection Preferences, then re-enter the token. Nothing was sent.")
            return
        self._lm_cancel=Event();cancel=self._lm_cancel
        try:
            invocation=DiscoveryInvocation(endpoint,token,discovery,cancel.is_set)
        except Exception:
            self.lm_status.setText("Use a valid local address and a token of at most 4096 printable ASCII characters without spaces. Nothing was sent.")
            return
        self._lm_refresh_invocation=invocation
        self._lm_started=time.monotonic()
        self._lm_refresh_outcome=None
        task=AsyncCallTask(invocation)
        self._lm_refresh_task=task
        self._lm_refresh_snapshot=snapshot
        task.signals.complete.connect(lambda value:self._receive_external("refresh",value[0]=="ok",value[1]))
        task.signals.failed.connect(lambda _message:self._receive_external("refresh",False,None))
        self.lm_status.setText("Inspecting model metadata… No analysis or model loading requested.")
        set_progress_bar(self.progress,detail="Inspecting server model metadata…",unknown_text="Waiting for metadata…")
        self.lm_activity.set_active(True,"Reading the model list; no model loading or analysis.")
        self.lm_activity.show()
        self._choices_changed();self._lm_settle.start()
        try:
            self.pool.start(task)
        except Exception:
            invocation.discard();self._lm_refresh_invocation=None
            self._lm_refresh_task=None;self._lm_refresh_outcome=None
            self._lm_settle.stop();self.progress.hide()
            self.lm_activity.set_active(False);self.lm_activity.hide()
            self.lm_status.setText("Model refresh could not start. Enter a token again if needed, then explicitly retry.")
            self._choices_changed()

    def _receive_external(self,kind,ok,value):
        if kind=="refresh":self._lm_refresh_outcome=(ok,value)
        else:self._lm_analysis_outcome=(ok,value)

    def _settle_external(self):
        if self._lm_refresh_task is not None or isinstance(self._task,AsyncCallTask):
            elapsed=max(0,int(time.monotonic()-self._lm_started))
            phase="Waiting for client cleanup" if self._lm_cancel.is_set() else ("Inspecting model metadata" if self._lm_refresh_task is not None else "External description request")
            detail="No analysis or model loading requested. Cancel closes after client cleanup." if self._lm_refresh_task is not None else "Server work may continue after cancellation."
            self.lm_status.setText(f"{phase} • {elapsed}s elapsed. {detail}")
        task=self._lm_refresh_task
        if task is not None and task.is_finished() and self._lm_refresh_outcome is not None:
            if self._lm_refresh_invocation is not None:
                self._lm_refresh_invocation.discard();self._lm_refresh_invocation=None
            ok,inventory=self._lm_refresh_outcome
            self._lm_refresh_task=None;self._lm_refresh_outcome=None
            self.progress.hide()
            self.lm_activity.set_active(False);self.lm_activity.hide()
            from core.lmstudio_discovery import ModelInventory
            try:current=self._connection_service().observe()==self._lm_refresh_snapshot
            except Exception:current=False
            if self._lm_cancel.is_set() or not current or not self._external_mode():
                self.lm_status.setText("Refresh cancelled or connection changed. Refresh again when ready.")
            elif not ok or type(inventory) is not ModelInventory:
                self.lm_status.setText("Model refresh unavailable. Check the connection and explicitly retry.")
            else:
                self._lm_snapshot=self._lm_refresh_snapshot
                for model in inventory.models:
                    if model.model_type=="llm" and model.vision is True:
                        for instance in model.loaded_instances:
                            self.lm_models.addItem(model.display_name+" — "+instance.instance_id,(model,instance))
                self.lm_status.setText("No loaded image-capable model was reported. Check LM Studio, then explicitly refresh again." if self.lm_models.count()==1 else
                    "Choose an image model already loaded on the server. Its capabilities are reported by LM Studio.")
                if not callable(self._lm_dispatch):
                    self.lm_status.setText("Image analysis through LM Studio is not available in this build. You can inspect models and save the connection.")
            self._choices_changed()
            if self._lm_close_pending:
                self._lm_settle.stop();super().reject();return
        task=self._task
        if isinstance(task,AsyncCallTask) and task.is_finished() and self._lm_analysis_outcome is not None:
            ok,value=self._lm_analysis_outcome;self._lm_analysis_outcome=None
            if ok:self._complete(value)
            else:self._failed("External analysis could not finish. Its server outcome may be unknown; review saved changes before retrying.")
        if self._lm_refresh_task is None and not isinstance(self._task,AsyncCallTask):self._lm_settle.stop()

    def _external_ready(self):
        return (callable(self._lm_dispatch) and self._lm_snapshot is not None
            and self.lm_models.currentData() is not None and bool(self.lm_prompt.text().strip())
            and self._lm_refresh_task is None)

    def prepare_for_privacy_capture(self):
        self.lm_auth_token.clear()

    def _external_backend(self):
        if not self._external_ready():raise ValueError("external_analysis_unavailable")
        if self._connection_service().observe()!=self._lm_snapshot:
            self._invalidate_external();raise ValueError("external_connection_changed")
        from core.lmstudio_backend import LMStudioContext,LMStudioDescriptionBackend
        model,instance=self.lm_models.currentData()
        endpoint=self._lm_snapshot.selection.endpoint
        dispatch=self._lm_dispatch
        return LMStudioDescriptionBackend(LMStudioContext(model_key=model.key,
            expected_instance_id=instance.instance_id,prompt=self.lm_prompt.text().strip(),
            max_output_tokens=self.lm_output.value(),observed_instance_config=instance.config),
            dispatch=lambda **kwargs:dispatch(endpoint=endpoint,**kwargs))

    def _model_ids(self):
        if self._external_mode():return []
        values=[]
        if self.tags.isChecked():
            values.extend(backend.model_id for backend in configured_wd_backends(self.settings))
        if self.descriptions.isChecked():
            values.append(str(getattr(self.settings,"plain_language_model","")))
        return [value for value in values if value]

    def _choices_changed(self,*_):
        enabled=self.tags.isChecked() or self.descriptions.isChecked()
        external=self._external_mode()
        idle=self._task is None and self._lm_refresh_task is None
        self.description_provider.setEnabled(idle)
        self.tags.setEnabled(idle and not external)
        self.descriptions.setEnabled(idle and not external)
        self.lm_connection.setEnabled(idle)
        self.lm_refresh.setEnabled(idle)
        self.lm_auth_panel.setEnabled(idle)
        for widget in (self.lm_models,self.lm_options,self.lm_prompt,self.lm_output):widget.setEnabled(idle)
        self.run_button.setEnabled(enabled and idle and (not external or self._external_ready()))
        self.runtime_memory_note.setVisible(not external)
        self.confirm_tags.setEnabled(self.tags.isChecked() and self._task is None)
        minimum=max(2,int(getattr(self.settings,"wd_consensus_minimum",2)))
        if self.tags.isChecked() and self.confirm_tags.isChecked():
            policy=(
                f"Safe confirmation requires {minimum:,} independent Tag vote(s). "
                "Known, conflict-free Tags may be confirmed; conflicts and ambiguity remain review findings."
            )
        else:
            policy="Generated results remain review findings until you confirm them."
        if self.force.isChecked():
            policy+=" Matching results will be replaced by a fresh run."
        else:
            policy+=(
                f" Matching results up to {self.age_value.value():,} "
                f"{self.age_unit.currentText().casefold()} old will be reused."
            )
        self.policy.setText(policy)
        self.estimate.setText("Descriptions remain findings for review. LM Studio controls its own models and resources." if external else ai_estimate_text(
            self.settings,len(self.images),self._model_ids(),
        ) if enabled else "Choose AI Tags, AI Descriptions, or both.")

    def _options(self) -> PreparationOptions:
        return PreparationOptions(
            read_external_evidence=False,
            suggest_sdxl_tags=self.tags.isChecked(),
            suggest_plain_language=self.descriptions.isChecked(),
            confirm_safe_tags=self.tags.isChecked() and self.confirm_tags.isChecked(),
            prepare_preferred_descriptions=False,
            skip_ai_value=self.age_value.value(),
            skip_ai_unit=str(self.age_unit.currentData() or "days"),
            force_all_ai=self.force.isChecked(),
            minimum_tag_votes=max(2,int(getattr(self.settings,"wd_consensus_minimum",2))),
            count_external_evidence_vote=True,
        )

    def _start(self):
        if self._task is not None or self._lm_refresh_task is not None or not self.images:
            return
        external_backend=None
        if self._external_mode():
            try:external_backend=self._external_backend()
            except Exception:
                self.lm_status.setText("Analysis unavailable or connection changed. Review the connection and refresh; no request started.")
                self._choices_changed();return
        self._setup_opened_for_run=False
        if self.scope_admission is not None and not self.scope_admission(self.images,self):
            self.summary.setText("The exact image scope changed. No AI run started; correct the source and review this selection again.")
            self.summary.show()
            return
        try:
            # Preflight saved-history storage before offering model setup. This
            # object remains local until the later scope/readiness gates pass.
            pending_history=self.history_operation_factory(self.images) if self.history_operation_factory is not None else None
        except Exception as exc:
            self.summary.setText(f"Saved history needs attention: {exc}\nReview history storage in Settings or narrow the selection. No AI run started.")
            self.summary.show()
            return
        ready=True if external_backend is not None else self._ensure_capabilities()
        if (ready or self._setup_opened_for_run) and self.scope_admission is not None and not self.scope_admission(self.images,self):
            self.summary.setText(
                "The image scope changed during model setup or before launch. No AI run or "
                "history capture started. Cancel this review, refresh the image workspace, "
                "and select current images before opening Run AI again. If you restore this "
                "exact selection instead, choose Run AI again here."
            )
            self.summary.show()
            return
        if not ready:
            self.summary.setText(
                "AI model setup is still needed. No AI run started; this selection and its "
                "choices are retained. Choose Run AI again to reopen Setup. After the models "
                "are ready, review this scope and choose Run AI once more."
            )
            self.summary.show()
            return
        if self._setup_opened_for_run:
            self.summary.setText(
                "AI models are ready. No AI run started. Check the selected images and "
                "choices, then choose Run AI again."
            )
            self.summary.show()
            return
        self._history_operation=pending_history
        started,active=self.state.begin_long_operation(
            self.owner,f"Running AI for {len(self.images):,} image(s)",resource="cpu" if external_backend is not None else "gpu",
        )
        if not started:
            show_operation_in_progress(self,active)
            if self.scope=="retry":
                self.summary.setText(
                    f"Another task is still working: {active}. No retry started. "
                    "Keep this failed-only selection and choose Run AI again when it finishes."
                )
                self.summary.show()
            return
        if self.scope=="retry":
            self.summary.hide()
            window_status_footer(self).show_message("")
        self._history_target=(tuple(self.images),self._history_operation.operation_id) if self._history_operation is not None else None
        options=self._memory_retry_options or self._options()
        self._memory_retry_options=None
        self.memory_hold.clear()
        self._submitted_options=options
        import copy
        self._submitted_memory_settings=copy.deepcopy(vars(self.settings))
        tag_backends=configured_wd_backends(self.settings) if options.suggest_sdxl_tags else []
        description_backend=(
            (external_backend or configured_plain_language_backend(self.settings))
            if options.suggest_plain_language else None
        )
        service=UnifiedPreparationService(
            self.state.knowledge,self.state.ai,
            journal=self.journal,
        )

        def run(progress,should_cancel):
            return service.run(
                self.images,options,tag_backends=tag_backends,
                description_backend=description_backend,
                minimum_tag_agreement=max(
                    2,int(getattr(self.settings,"wd_consensus_minimum",2)),
                ),
                progress=progress,should_cancel=should_cancel,
                **({"history_operation":self._history_operation} if self._history_operation is not None else {}),
            )

        self._set_busy(True)
        if external_backend is not None:
            self._lm_cancel=Event();cancel=self._lm_cancel
            self._lm_started=time.monotonic()
            task=AsyncCallTask(lambda:run(task.signals.progress.emit,cancel.is_set))
            task.signals=ProgressCallSignals()
            self._task=task
            task.signals.progress.connect(self._progress)
            task.signals.complete.connect(lambda value:self._receive_external("analysis",True,value))
            task.signals.failed.connect(lambda _message:self._receive_external("analysis",False,None))
            self._lm_settle.start();self.pool.start(task);return
        task=CancelableProgressCallTask(run)
        self._task=task
        task.signals.progress.connect(self._progress)
        task.signals.complete.connect(self._complete)
        task.signals.cancelled.connect(self._cancelled)
        task.signals.failed.connect(self._failed)
        self.pool.start(task)

    def _ensure_capabilities(self) -> bool:
        if self._external_mode():return self._external_ready()
        if self.tags.isChecked() and not ensure_capability(
            self,self.settings,"wd","AI Tags",
            on_setup_opened=self._mark_model_setup_opened,
        ):
            return False
        if self.descriptions.isChecked() and not ensure_capability(
            self,self.settings,"joy","AI Descriptions",
            on_setup_opened=self._mark_model_setup_opened,
        ):
            return False
        return True

    def _mark_model_setup_opened(self):
        self._setup_opened_for_run=True

    def _set_busy(self,busy: bool):
        self.lm_auth_panel.setEnabled(not busy)
        if busy:self.lm_auth_token.clear()
        for widget in (self.description_provider,self.lm_connection,self.lm_refresh,self.lm_models,self.lm_options,self.lm_prompt,self.lm_output):
            widget.setEnabled(not busy)
        self.history_button.setEnabled(not busy)
        for widget in (
            self.tags,self.descriptions,self.confirm_tags,self.force,
            self.age_value,self.age_unit,
        ):
            widget.setEnabled(not busy)
        if self._external_mode():
            self.tags.setEnabled(False);self.descriptions.setEnabled(False);self.confirm_tags.setEnabled(False)
        self.run_button.setEnabled(not busy and (
            self.tags.isChecked() or self.descriptions.isChecked()
        ))
        self.cancel_button.setText(("Cancel Request" if self._external_mode() else "Cancel Safely") if busy else "Close")
        self.cancel_button.setEnabled(True)
        if busy:
            set_progress_bar(
                self.progress,detail="Preparing external request…" if self._external_mode() else "Starting local AI…",unknown_text="Preparing analysis…",
            )
            self.progress.show()
        else:
            self.progress.hide()
            self._choices_changed()

    def _progress(self,current: int,total: int,label: str):
        if self._external_mode():label=label.split(";",1)[0]
        self.state.update_long_operation(self.owner,current,total,label)
        set_progress_bar(
            self.progress,current,total,detail=label,unit="work steps",
            scope=f"{len(self.images):,} image(s)",unknown_text="Running local AI…",
        )

    def _complete(self,result: PreparationRunResult):
        self.state.finish_long_operation(self.owner)
        self._task=None
        self.result_data=result
        self._set_busy(False)
        self._show_result(result)
        self.images_changed.emit(tuple(self.images))

    def _show_memory_hold(self,errors):
        from .ai_runtime_controls import memory_hold_detail
        from dataclasses import replace
        detail=memory_hold_detail(errors)
        options=self._submitted_options
        if not detail or options is None:
            self.memory_hold.clear();return False
        scope=tuple(self.images)
        baseline=self._options()
        self.memory_hold.hold(detail,self.settings,options,
            scope_description=f"{len(scope):,} reviewed image(s); selected AI stages and quality unchanged",
            retry=self._retry_memory_request,
            settings_snapshot=getattr(self,"_submitted_memory_settings",None),
            is_current=lambda:tuple(self.images)==scope and self._options()==baseline and self._task is None,
        )
        return True

    def _retry_memory_request(self,options):
        self._memory_retry_options=options
        try:self._start()
        finally:self._memory_retry_options=None

    def _show_result(self,result: PreparationRunResult):
        if self._external_mode():
            self.lm_status.setText("Client request finished. Check per-image outcomes; server work may continue." if result.cancelled or result.errors else
                "Descriptions saved as findings. Choose Review Findings to review and confirm them.")
        from .ai_runtime_controls import ai_result_headline
        held=False if self._external_mode() else self._show_memory_hold([] if result.cancelled else [*result.errors,*(error for outcome in result.outcomes for error in outcome.errors)])
        counts=result.outcome_counts()
        prefix=("Saved history needs recovery; completed changes were kept. " if result.history_interrupted else
                ("Request cancelled; server work may continue. " if self._external_mode() else "Paused safely. ") if result.cancelled else ai_result_headline(result)+". ")
        self.summary.setText(
            prefix
            +f"{counts['completed']:,} completed  •  {counts['partial']:,} partial  •  "
            +f"{counts['skipped']:,} already current  •  "
            +f"{counts['conflicted']:,} conflicted  •  {counts['failed']:,} failed"
        )
        self.summary.show()
        rows=[]
        for outcome in result.outcomes:
            label=_OUTCOME_LABELS.get(outcome.status,outcome.status.title())
            detail=outcome.detail()
            rows.append(IncrementalResultRow(
                stable_id=str(outcome.image),
                columns=(outcome.image.name,label,detail),
                tooltip=f"{outcome.image}\n{detail}",
                accessible_text=f"{outcome.image.name}; {label}; {detail}",
            ))
        self.result_model.set_rows(rows)
        self.idle_spacer.hide()
        self.result_title.show()
        self.results.show()
        self.review_button.setVisible(bool(result.review_paths))
        self.run_button.hide()
        self.history_button.setVisible(self._history_target is not None and self.history_opener is not None)
        self.retry_button.setVisible(bool(self._failed_retry_paths(result)))

    def _failed_retry_paths(self,result: PreparationRunResult) -> tuple[Path,...]:
        if result.cancelled or result.history_interrupted or result.images!=len(self.images):
            return ()
        if len(result.outcomes)!=len(self.images):
            return ()
        outcomes={outcome.image:outcome for outcome in result.outcomes}
        if len(outcomes)!=len(self.images) or set(outcomes)!=set(self.images):
            return ()
        return tuple(path for path in self.images if outcomes[path].status=="failed")

    def _open_failed_retry(self):
        if (self._task is not None or self.result_data is None
                or self.latest_retry_result is not None
                or (self._latest_retry_dialog is not None
                    and self._latest_retry_dialog._history_target is not None)):
            return
        paths=self._failed_retry_paths(self.result_data)
        if not paths:
            self.retry_button.hide()
            return
        if self.scope_admission is not None and not self.scope_admission(paths,self):
            self.summary.setText(
                "The failed-image selection is not ready. The original report remains below; "
                "correct its source before opening another review."
            )
            return
        child=RunAIDialog(
            self.state,self.settings,paths,owner=self.owner,scope="retry",
            pool=self.pool,parent=self,journal=self.journal,
            history_operation_factory=self.history_operation_factory,
            history_opener=self.history_opener,
            initial_options=self._submitted_options or self._options(),
            scope_admission=self.scope_admission,
            lmstudio_connection_service=self._lm_service,lmstudio_discovery=self._lm_discovery,
            lmstudio_dispatch=self._lm_dispatch,
        )
        if self._external_mode():child.description_provider.setCurrentIndex(1)
        self._latest_retry_dialog=child
        child.images_changed.connect(self.images_changed.emit)
        child.exec()
        latest=child.latest_retry_result or child.result_data
        if latest is None and child._history_target is None:
            return
        self.latest_retry_result=latest
        self.retry_button.hide()
        self.result_title.setText("Original run outcomes (before retry)")
        self.retry_history_button.setVisible(
            child._latest_history_dialog() is not None and self.history_opener is not None
        )
        if latest is not None:
            counts=latest.outcome_counts()
            self.summary.setText(self.summary.text()+
                f"\nLatest failed-only retry: {counts['completed']:,} completed, "
                f"{counts['partial']:,} partial, {counts['conflicted']:,} conflicted, "
                f"{counts['failed']:,} failed. Original per-image outcomes remain below."
            )
            self.review_button.setVisible(bool(latest.review_paths))
            self.review_button.setText("Review Retry Findings…")
        else:
            self.summary.setText(self.summary.text()+
                "\nThe retry stopped without a complete result. Review its saved changes "
                "before starting another run; the original report remains below."
            )
            self.review_button.hide()
        if child.review_requested and latest is not None and latest.review_paths:
            self._review()

    def _open_retry_history(self):
        child=self._latest_retry_dialog
        if child is not None:
            target=child._latest_history_dialog()
            if target is not None:
                target._open_history()

    def _latest_history_dialog(self):
        child=self._latest_retry_dialog
        if child is not None:
            latest=child._latest_history_dialog()
            if latest is not None:
                return latest
        return self if self._history_target is not None else None

    def _open_history(self):
        if self._task is None and self._history_target is not None and self.history_opener is not None:
            return self.history_opener(*self._history_target)

    def _cancelled(self):
        self.state.finish_long_operation(self.owner)
        self._task=None
        self._set_busy(False)
        self.summary.setText(
            "AI run cancelled safely. Completed image results remain saved; review saved changes before starting another run."
        )
        self.summary.show()
        self.history_button.setVisible(self._history_target is not None and self.history_opener is not None)
        self.images_changed.emit(tuple(self.images))

    def _failed(self,message: str):
        self.state.finish_long_operation(self.owner)
        self._task=None
        self._set_busy(False)
        held=self._show_memory_hold([message])
        self.summary.setText(
            ("AI held for GPU memory. Review the visible memory choices.\n\n" if held else "AI could not finish. Completed image results remain saved.\n\n")+str(message)
        )
        self.summary.show()
        self.history_button.setVisible(self._history_target is not None and self.history_opener is not None)
        self.images_changed.emit(tuple(self.images))

    def _review(self):
        result=self.latest_retry_result or self.result_data
        self.review_paths_requested=tuple(result.review_paths) if result is not None else ()
        self.review_requested=True
        self.accept()

    def _cancel_or_close(self):
        self.lm_auth_token.clear()
        if self._lm_refresh_invocation is not None:self._lm_refresh_invocation.discard()
        if self._lm_refresh_task is not None:
            self._lm_close_pending=True;self._lm_cancel.set()
            self.cancel_button.setText("Closing…");self.cancel_button.setEnabled(False)
            self.cancel_button.setMinimumWidth(self.cancel_button.fontMetrics().horizontalAdvance("Closing…")+24)
            return
        if isinstance(self._task,AsyncCallTask):
            self.state.request_long_operation_cancel(self.owner)
            self._lm_cancel.set();self.cancel_button.setText("Cancelling client request…")
            self.cancel_button.setEnabled(False);return
        if self._task is not None:
            self.state.request_long_operation_cancel(self.owner)
            self._task.cancel()
            self.cancel_button.setText("Cancelling…")
            self.cancel_button.setEnabled(False)
            return
        self.reject()

    def reject(self):
        self.lm_auth_token.clear()
        if self._lm_refresh_invocation is not None:
            self._lm_refresh_invocation.discard()
        if self._task is not None or self._lm_refresh_task is not None:
            self._cancel_or_close()
            return
        super().reject()
