from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize,Qt,QThreadPool,Signal
from PySide6.QtWidgets import (
    QAbstractItemView,QCheckBox,QComboBox,QDialogButtonBox,QGridLayout,QGroupBox,
    QHeaderView,QLabel,QProgressBar,QPushButton,QSizePolicy,QSpinBox,QTableView,
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
from .workers import CancelableProgressCallTask


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
        from .components import bind_status_footer
        bind_status_footer(self,self.progress,title="AI analysis")
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

    def _model_ids(self):
        values=[]
        if self.tags.isChecked():
            values.extend(backend.model_id for backend in configured_wd_backends(self.settings))
        if self.descriptions.isChecked():
            values.append(str(getattr(self.settings,"plain_language_model","")))
        return [value for value in values if value]

    def _choices_changed(self,*_):
        enabled=self.tags.isChecked() or self.descriptions.isChecked()
        self.run_button.setEnabled(enabled and self._task is None)
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
        self.estimate.setText(ai_estimate_text(
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
        if self._task is not None or not self.images:
            return
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
        ready=self._ensure_capabilities()
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
            self.owner,f"Running AI for {len(self.images):,} image(s)",resource="gpu",
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
            configured_plain_language_backend(self.settings)
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
        task=CancelableProgressCallTask(run)
        self._task=task
        task.signals.progress.connect(self._progress)
        task.signals.complete.connect(self._complete)
        task.signals.cancelled.connect(self._cancelled)
        task.signals.failed.connect(self._failed)
        self.pool.start(task)

    def _ensure_capabilities(self) -> bool:
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
        self.history_button.setEnabled(not busy)
        for widget in (
            self.tags,self.descriptions,self.confirm_tags,self.force,
            self.age_value,self.age_unit,
        ):
            widget.setEnabled(not busy)
        self.run_button.setEnabled(not busy and (
            self.tags.isChecked() or self.descriptions.isChecked()
        ))
        self.cancel_button.setText("Cancel Safely" if busy else "Close")
        self.cancel_button.setEnabled(True)
        if busy:
            set_progress_bar(
                self.progress,detail="Starting local AI…",unknown_text="Starting local AI…",
            )
            self.progress.show()
        else:
            self.progress.hide()
            self._choices_changed()

    def _progress(self,current: int,total: int,label: str):
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
        from .ai_runtime_controls import ai_result_headline
        held=self._show_memory_hold([] if result.cancelled else [*result.errors,*(error for outcome in result.outcomes for error in outcome.errors)])
        counts=result.outcome_counts()
        prefix=("Saved history needs recovery; completed changes were kept. " if result.history_interrupted else
                "Paused safely. " if result.cancelled else ai_result_headline(result)+". ")
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
        )
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
        if self._task is not None:
            self.state.request_long_operation_cancel(self.owner)
            self._task.cancel()
            self.cancel_button.setText("Cancelling…")
            self.cancel_button.setEnabled(False)
            return
        self.reject()

    def reject(self):
        if self._task is not None:
            self._cancel_or_close()
            return
        super().reject()
