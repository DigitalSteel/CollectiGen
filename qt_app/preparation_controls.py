from __future__ import annotations

from collections.abc import Callable

from PySide6.QtCore import QSignalBlocker,Signal,Qt
from PySide6.QtWidgets import (
    QCheckBox,QComboBox,QDialog,QDialogButtonBox,QGridLayout,QGroupBox,
    QHBoxLayout,QLabel,QMenu,QMessageBox,QProgressDialog,QPushButton,QScrollArea,QSizePolicy,QSpinBox,QTabWidget,QToolButton,
    QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core import CONFIDENCE_PRESETS,DEFAULT_SIMILARITY_PERCENT,PreparationOptions

from .components import compact_button,guard_scrollable_value_controls


def ai_tag_runtime_recovery(message: str) -> tuple[str,str] | None:
    """Turn a native AI Tag loader failure into an actionable preparation hold."""
    detail=str(message).strip()
    lowered=detail.casefold()
    if not (
        "ai tag runtime could not load" in lowered
        or "onnxruntime_pybind11_state" in lowered
        or ("onnxruntime" in lowered and "dll load failed" in lowered)
    ):
        return None
    return (
        "AI Tag Runtime Could Not Load",
        "AI Tags could not start because the local ONNX Runtime failed to load. "
        "Any preparation already completed was kept.\n\n"
        "Open AI Tag Setup to check the runtime and review Repair for the Shared "
        "AI Tag runtime. Restart CollectiGen if the repair asks you to; this "
        "in-session return to your selected "
        "images will not survive a restart. After restarting, select the images "
        "and open Prepare Images for a fresh review.\n\n"
        "Do not copy DLLs or install unrelated packages.\n\n"
        f"Technical detail: {detail}",
    )


def show_ai_tag_runtime_recovery(parent,recovery,on_setup=None) -> None:
    """Show a direct Setup choice when a page can retain its review in-session."""
    title,message=recovery
    if on_setup is None:
        QMessageBox.warning(parent,title,message)
        return
    summary,separator,technical=message.partition("\n\nTechnical detail: ")
    box=QMessageBox(QMessageBox.Warning,title,summary,parent=parent)
    if separator:
        box.setDetailedText(technical)
    setup=box.addButton("Open AI Tag Setup",QMessageBox.AcceptRole)
    close=box.addButton("Close",QMessageBox.RejectRole)
    box.setDefaultButton(setup)
    box.setEscapeButton(close)
    box.exec()
    if box.clickedButton() is setup:
        on_setup()


class PreparationControls(QWidget):
    """Shared preparation launcher and option editor used by Ingest/Dataset."""

    requested=Signal()
    undo_requested=Signal()
    options_changed=Signal()

    def __init__(
        self,settings,parent=None,*,inline_options: bool=True,
        persist_changes: bool=True,
    ):
        super().__init__(parent)
        self.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Maximum)
        self.settings=settings
        self._inline_options=bool(inline_options)
        self._persist_changes=bool(persist_changes)
        self._submitted_options: PreparationOptions | None=None
        self._allow_memory_pressure=False
        self._submitted_paths=None
        self._submitted_fingerprints=None
        self._submitted_mode="full"
        self._setup_return_review=None
        self._setup_return_task=None
        self.impact_context_provider=None
        self.duplicate_preflight_runner=None
        self._scope_count=0
        self._scope_description="No images are in scope."
        self._scope_detail_provider: Callable[[PreparationOptions],str] | None=None
        self._summary_context=""
        self._summary_context_tooltip=""
        self._leading_action: QWidget | None=None
        self._refresh_context=False
        self._busy=False

        layout=QVBoxLayout(self)
        layout.setContentsMargins(0,0,0,0)
        layout.setSpacing(5)

        self.section_grid=QGridLayout()
        self.section_grid.setContentsMargins(0,0,0,0)
        self.section_grid.setHorizontalSpacing(7)
        self.section_grid.setVerticalSpacing(5)

        self.sources_group=QGroupBox("Sources")
        sources_layout=QVBoxLayout(self.sources_group)
        sources_layout.setContentsMargins(8,5,8,6)
        sources_layout.setSpacing(3)
        self.read_external=QCheckBox("Read TXT / Metadata")
        self.read_external.setToolTip(
            "Read adjacent TXT and supported image Metadata as attributed "
            "evidence. This does not confirm or rewrite either source."
        )
        self.read_external.setAccessibleDescription(self.read_external.toolTip())
        sources_layout.addWidget(self.read_external)
        sources_layout.addStretch(1)

        self.generation_group=QGroupBox("AI Analysis")
        generation_layout=QVBoxLayout(self.generation_group)
        generation_layout.setContentsMargins(8,5,8,6)
        generation_layout.setSpacing(3)
        self.suggest_tags=QCheckBox("Generate AI Tags")
        self.suggest_plain=QCheckBox("Generate AI Descriptions")
        self.suggest_tags.setToolTip(
            "Run the enabled local AI Tag models and save their exact results "
            "as reviewable evidence."
        )
        self.suggest_plain.setToolTip(
            "Run the selected local caption model and save its AI Description "
            "as reviewable evidence."
        )
        self.suggest_tags.setAccessibleDescription(self.suggest_tags.toolTip())
        self.suggest_plain.setAccessibleDescription(self.suggest_plain.toolTip())
        generation_choices=QHBoxLayout()
        generation_choices.setContentsMargins(0,0,0,0)
        generation_choices.setSpacing(8)
        generation_choices.addWidget(self.suggest_tags)
        generation_choices.addWidget(self.suggest_plain)
        generation_choices.addStretch(1)
        generation_layout.addLayout(generation_choices)

        freshness=QHBoxLayout()
        freshness.setContentsMargins(0,0,0,0)
        freshness.setSpacing(5)
        freshness_label=QLabel("Reuse AI within")
        freshness.addWidget(freshness_label)
        self.skip_value=QSpinBox()
        self.skip_value.setRange(1,999)
        self.skip_value.setValue(max(1,min(999,int(
            getattr(settings,"prepare_ai_skip_value",7)
        ))))
        self.skip_value.setAccessibleName("AI result reuse amount")
        freshness_label.setBuddy(self.skip_value)
        freshness.addWidget(self.skip_value)
        self.skip_unit=QComboBox()
        self.skip_unit.addItem("Hours","hours")
        self.skip_unit.addItem("Days","days")
        self.skip_unit.addItem("Months (30 days)","months")
        unit=str(getattr(settings,"prepare_ai_skip_unit","days") or "days")
        self.skip_unit.setCurrentIndex(max(0,self.skip_unit.findData(unit)))
        self.skip_unit.setAccessibleName("AI result reuse unit")
        freshness.addWidget(self.skip_unit)
        self.force_all=QCheckBox("Force all scans")
        reuse_tooltip=(
            "Reuse only exact matching AI results completed within this age. "
            "Changed images, models, prompts, or settings are always rescanned."
        )
        freshness_label.setToolTip(reuse_tooltip)
        self.skip_value.setToolTip(reuse_tooltip)
        self.skip_unit.setToolTip(reuse_tooltip)
        self.skip_value.setAccessibleDescription(reuse_tooltip)
        self.skip_unit.setAccessibleDescription(reuse_tooltip)
        self.force_all.setToolTip(
            "Ignore the reuse age for this run and run every selected AI scan."
        )
        self.force_all.setAccessibleDescription(self.force_all.toolTip())
        freshness.addWidget(self.force_all)
        freshness.addStretch(1)
        self.reuse_options=QWidget()
        self.reuse_options.setLayout(freshness)

        self.application_group=QGroupBox("Automatic Changes")
        application_layout=QVBoxLayout(self.application_group)
        application_layout.setContentsMargins(8,5,8,6)
        application_layout.setSpacing(3)
        self.confirm_tags=QCheckBox("Confirm Safe Tags")
        self.prepare_descriptions=QCheckBox("Fill / Upgrade Descriptions")
        self.confirm_tags.setToolTip(
            "Confirm only known, conflict-free Tags that meet the selected vote "
            "policy. Generated evidence remains reviewable when this is off."
        )
        self.prepare_descriptions.setToolTip(
            "Fill an empty Description or upgrade prior caption-model output "
            "only when it is exactly attributable and unchanged."
        )
        self.confirm_tags.setAccessibleDescription(self.confirm_tags.toolTip())
        self.prepare_descriptions.setAccessibleDescription(
            self.prepare_descriptions.toolTip()
        )
        self.read_external.setChecked(bool(getattr(
            settings,"prepare_read_external_evidence",True,
        )))
        self.suggest_tags.setChecked(bool(
            getattr(settings,"suggest_wd_for_missing_tags",True)
        ))
        self.suggest_plain.setChecked(bool(
            getattr(settings,"suggest_joycaption_for_missing_descriptions",True)
        ))
        self.confirm_tags.setChecked(bool(
            getattr(settings,"prepare_confirm_safe_tags",True)
        ))
        self.prepare_descriptions.setChecked(bool(
            getattr(settings,"prepare_preferred_descriptions",True)
        ))
        automatic_choices=QHBoxLayout()
        automatic_choices.setContentsMargins(0,0,0,0)
        automatic_choices.setSpacing(8)
        automatic_choices.addWidget(self.confirm_tags)
        automatic_choices.addWidget(self.prepare_descriptions)
        automatic_choices.addStretch(1)
        application_layout.addLayout(automatic_choices)

        self.confidence_preset=QComboBox()
        for key in ("balanced","conservative","permissive"):
            self.confidence_preset.addItem(CONFIDENCE_PRESETS[key]["label"],key)
        self.confidence_preset.addItem("Custom","custom")
        configured=str(
            getattr(settings,"prepare_confidence_preset","balanced") or "balanced"
        )
        configured_policy=CONFIDENCE_PRESETS.get(
            configured,CONFIDENCE_PRESETS["balanced"],
        )
        configured_minimum=getattr(settings,"prepare_tag_vote_minimum",None)
        if configured_minimum is None:
            configured_minimum=(
                int(configured_policy["minimum_tag_votes"])
                if configured=="permissive"
                else max(
                    int(configured_policy["minimum_tag_votes"]),
                    int(getattr(settings,"wd_consensus_minimum",1)),
                )
            )
        self.minimum_votes=QSpinBox()
        self.minimum_votes.setRange(1,99)
        self.minimum_votes.setValue(max(1,min(99,int(configured_minimum))))
        self.minimum_votes.setAccessibleName("Minimum independent Tag votes")
        self.external_vote=QCheckBox("Count TXT / Metadata as one vote")
        self.external_vote.setChecked(bool(getattr(
            settings,"prepare_external_evidence_vote",
            configured_policy["external_evidence_vote"],
        )))
        configured=self._matching_preset() or "custom"
        self.confidence_preset.setCurrentIndex(max(
            0,self.confidence_preset.findData(configured),
        ))
        # Presets only changed the two direct controls below, which made the
        # dialog look more complex without adding a distinct decision.  Keep
        # the value as a compatibility hook for saved settings and automation
        # metadata, but let people edit the actual vote policy directly.
        self.confidence_preset.setAccessibleName(
            "Legacy preparation automation confidence preset"
        )
        self.confidence_preset.hide()
        application_layout.addWidget(self.confidence_preset)
        vote_controls=QHBoxLayout()
        vote_controls.setContentsMargins(0,0,0,0)
        vote_controls.setSpacing(5)
        vote_label=QLabel("Minimum Tag votes")
        vote_label.setBuddy(self.minimum_votes)
        vote_controls.addWidget(vote_label)
        vote_controls.addWidget(self.minimum_votes)
        self.external_vote.setText("TXT / Metadata vote")
        vote_controls.addWidget(self.external_vote)
        vote_controls.addStretch(1)
        self.vote_options=QWidget()
        self.vote_options.setLayout(vote_controls)
        self.confidence_detail=QLabel()
        self.confidence_detail.setWordWrap(True)
        self.confidence_detail.setObjectName("mutedLabel")
        application_layout.addWidget(self.confidence_detail)

        for group in (
            self.sources_group,self.generation_group,self.application_group,
        ):
            group.setAccessibleName(group.title()+" preparation options")
            group.setVisible(self._inline_options)
        layout.addLayout(self.section_grid)
        self._arrange_sections(1200)

        self.tuning_toggle=QPushButton("Customize AI Reuse and Confirmation")
        self.tuning_toggle.setCheckable(True)
        self.tuning_toggle.setAccessibleName("Customize AI reuse and confirmation")
        self.tuning_toggle.setSizePolicy(QSizePolicy.Maximum,QSizePolicy.Fixed)
        self.tuning_summary=QLabel()
        self.tuning_summary.setWordWrap(True)
        self.tuning_summary.setObjectName("mutedLabel")
        self.tuning_options=QWidget()
        tuning_layout=QVBoxLayout(self.tuning_options)
        tuning_layout.setContentsMargins(8,0,8,0)
        tuning_layout.setSpacing(5)
        tuning_layout.addWidget(self.reuse_options)
        tuning_layout.addWidget(self.vote_options)
        self.tuning_options.hide()
        self.tuning_toggle.setVisible(self._inline_options)
        self.tuning_summary.setVisible(self._inline_options)
        layout.addWidget(self.tuning_toggle)
        layout.addWidget(self.tuning_summary)
        layout.addWidget(self.tuning_options)
        self.tuning_toggle.toggled.connect(self._show_tuning_options)

        action_row=QHBoxLayout(); self.action_row=action_row
        action_row.setContentsMargins(0,0,0,0)
        self.action_button=QToolButton()
        self.action_button.setProperty("primary",True)
        self.action_button.setProperty("featured",True)
        self.action_button.setProperty("preparationLauncher",True)
        self.action_button.setMinimumSize(180,38)
        self.action_button.setSizePolicy(QSizePolicy.Maximum,QSizePolicy.Fixed)
        self.action_button.setText("Prepare Images")
        self.action_button.setPopupMode(QToolButton.MenuButtonPopup)
        self.action_button.setAccessibleName("Prepare images")
        focused_menu=QMenu(self.action_button); self.focused_actions={}
        for label,mode in (
            ("Read TXT / Metadata Only","evidence"),
            ("Generate AI Tags Only","tags"),
            ("Generate AI Descriptions Only","descriptions"),
            ("Generate All AI","all_ai"),
            ("Force All AI Scans…","force_all_ai"),
        ):
            action=focused_menu.addAction(label)
            self.focused_actions[mode]=action
            action.triggered.connect(
                lambda _checked=False,value=mode:self._action_requested(value)
            )
        self.focused_actions["force_all_ai"].setVisible(False)
        self.action_button.setMenu(focused_menu)
        self.action_button.clicked.connect(lambda _checked=False:self._action_requested("full"))
        action_row.addWidget(self.action_button)
        self.undo_button=QPushButton("Review Preparation Changes…")
        self.undo_button.setSizePolicy(QSizePolicy.Maximum,QSizePolicy.Fixed)
        self.undo_button.setMinimumHeight(38)
        self.undo_button.clicked.connect(self.undo_requested)
        self.undo_button.hide()
        action_row.addWidget(self.undo_button)
        self.summary=QLabel()
        self.summary.setObjectName("mutedLabel")
        self.summary.setWordWrap(True)
        self.summary.setMinimumWidth(0)
        action_row.addStretch(1)
        layout.addLayout(action_row)
        if self._inline_options:
            layout.addWidget(self.summary)
        else:
            action_row.insertWidget(1,self.summary,1)

        for control in (
            self.read_external,self.suggest_tags,self.suggest_plain,
            self.force_all,
        ):
            control.toggled.connect(self._update_summary)
        for control in (
            self.read_external,self.suggest_tags,self.suggest_plain,
        ):
            control.toggled.connect(self._source_or_generation_changed)
        self.suggest_tags.toggled.connect(self._update_enabled_states)
        self.confirm_tags.toggled.connect(self._application_changed)
        self.prepare_descriptions.toggled.connect(self._application_changed)
        self.skip_value.valueChanged.connect(self._freshness_changed)
        self.skip_unit.currentIndexChanged.connect(self._freshness_changed)
        self.confidence_preset.currentIndexChanged.connect(self._preset_changed)
        self.minimum_votes.valueChanged.connect(self._confidence_value_changed)
        self.external_vote.toggled.connect(self._confidence_value_changed)
        self._update_summary()

    def resizeEvent(self,event) -> None:
        super().resizeEvent(event)
        self._arrange_sections(event.size().width())

    def _show_tuning_options(self,expanded: bool) -> None:
        self.tuning_options.setVisible(expanded)
        self.tuning_toggle.setText(
            "Hide AI Reuse and Confirmation" if expanded
            else "Customize AI Reuse and Confirmation"
        )

    def _arrange_sections(self,width: int) -> None:
        if width<680:
            mode="stacked"
        elif width<1050:
            mode="wrapped"
        else:
            mode="wide"
        if getattr(self,"_section_mode","")==mode:
            return
        self._section_mode=mode
        for group in (
            self.sources_group,self.generation_group,self.application_group,
        ):
            self.section_grid.removeWidget(group)
        for column in range(3):
            self.section_grid.setColumnStretch(column,0)
        if mode=="stacked":
            self.section_grid.addWidget(self.sources_group,0,0)
            self.section_grid.addWidget(self.generation_group,1,0)
            self.section_grid.addWidget(self.application_group,2,0)
            self.section_grid.setColumnStretch(0,1)
            return
        if mode=="wrapped":
            self.section_grid.addWidget(self.sources_group,0,0)
            self.section_grid.addWidget(self.generation_group,0,1)
            self.section_grid.addWidget(self.application_group,1,0,1,2)
            self.section_grid.setColumnStretch(0,1)
            self.section_grid.setColumnStretch(1,2)
            return
        self.section_grid.addWidget(self.sources_group,0,0)
        self.section_grid.addWidget(self.generation_group,0,1)
        self.section_grid.addWidget(self.application_group,0,2)
        self.section_grid.setColumnStretch(0,1)
        self.section_grid.setColumnStretch(1,2)
        self.section_grid.setColumnStretch(2,2)

    def set_scope(
        self,count: int,*,button_text: str,description: str,enabled: bool=True,
        detail_provider: Callable[[PreparationOptions],str] | None=None,
    ) -> None:
        self._scope_count=max(0,int(count))
        self._scope_description=str(description or "").strip()
        self._scope_detail_provider=detail_provider
        self.action_button.setText(button_text)
        self.action_button.setAccessibleName(button_text)
        text_width=self.action_button.fontMetrics().horizontalAdvance(button_text)
        # The styled split-button reserves a separate menu-button subcontrol.
        # Include it explicitly because the platform size hint can otherwise
        # clip the final count character at larger fonts or display scaling.
        self.action_button.setMinimumWidth(max(180,text_width+64))
        self.action_button.setMinimumHeight(max(38,self.action_button.sizeHint().height()))
        self.action_button.setEnabled(bool(enabled and self._scope_count))
        self._update_summary()

    def set_context(
        self,*,refresh: bool=False,secondary: bool=False,
        leading_action: QWidget | None=None,summary: str="",
        summary_tooltip: str="",
    ) -> None:
        """Adapt the compact launcher without changing its saved options."""
        self._refresh_context=bool(refresh)
        self._summary_context=str(summary or "").strip()
        self._summary_context_tooltip=str(summary_tooltip or "").strip()
        if self._leading_action is not leading_action:
            if self._leading_action is not None:
                self.action_row.removeWidget(self._leading_action)
            self._leading_action=leading_action
            if leading_action is not None:
                self.action_row.insertWidget(0,leading_action)
        secondary=bool(secondary)
        if bool(self.action_button.property("secondaryLauncher"))!=secondary:
            self.action_button.setProperty("secondaryLauncher",secondary)
            style=self.action_button.style()
            style.unpolish(self.action_button); style.polish(self.action_button)
            self.action_button.setMinimumHeight(max(38,self.action_button.sizeHint().height()))
            self.action_button.updateGeometry()
            self.updateGeometry()
        labels=(
            {
                "evidence":"Re-read TXT / Metadata",
                "tags":"Refresh AI Tags",
                "descriptions":"Refresh AI Descriptions",
                "all_ai":"Refresh All AI",
            }
            if self._refresh_context else
            {
                "evidence":"Read TXT / Metadata Only",
                "tags":"Generate AI Tags Only",
                "descriptions":"Generate AI Descriptions Only",
                "all_ai":"Generate All AI",
            }
        )
        for mode,label in labels.items():
            self.focused_actions[mode].setText(label)
        self.focused_actions["force_all_ai"].setVisible(self._refresh_context)
        self._update_summary()

    def set_busy(self,busy: bool) -> None:
        self._busy=bool(busy)
        for control in (
            self.read_external,self.suggest_tags,self.suggest_plain,
            self.confirm_tags,self.prepare_descriptions,self.force_all,
            self.skip_value,self.skip_unit,
        ):
            control.setEnabled(not busy)
        self._update_enabled_states()
        self.action_button.setEnabled(not busy and self._scope_count>0)
        self.undo_button.setEnabled(not busy)

    def set_undo_available(self,available: bool) -> None:
        self.undo_button.setVisible(bool(available))

    def refresh_from_settings(self) -> None:
        """Reload saved defaults without treating cross-page sync as user input."""
        controls=(
            self.read_external,self.suggest_tags,self.suggest_plain,self.confirm_tags,
            self.prepare_descriptions,self.skip_value,self.skip_unit,
            self.confidence_preset,self.minimum_votes,self.external_vote,
        )
        blockers=[QSignalBlocker(control) for control in controls]
        self.read_external.setChecked(bool(getattr(
            self.settings,"prepare_read_external_evidence",True,
        )))
        self.suggest_tags.setChecked(bool(getattr(
            self.settings,"suggest_wd_for_missing_tags",True,
        )))
        self.suggest_plain.setChecked(bool(getattr(
            self.settings,"suggest_joycaption_for_missing_descriptions",True,
        )))
        self.confirm_tags.setChecked(bool(getattr(
            self.settings,"prepare_confirm_safe_tags",True,
        )))
        self.prepare_descriptions.setChecked(bool(getattr(
            self.settings,"prepare_preferred_descriptions",True,
        )))
        self.skip_value.setValue(max(1,min(999,int(getattr(
            self.settings,"prepare_ai_skip_value",7,
        )))))
        self.skip_unit.setCurrentIndex(max(0,self.skip_unit.findData(str(
            getattr(self.settings,"prepare_ai_skip_unit","days") or "days"
        ))))
        self.minimum_votes.setValue(max(1,min(99,int(getattr(
            self.settings,"prepare_tag_vote_minimum",2,
        )))))
        self.external_vote.setChecked(bool(getattr(
            self.settings,"prepare_external_evidence_vote",True,
        )))
        preset=self._matching_preset() or "custom"
        self.confidence_preset.setCurrentIndex(max(
            0,self.confidence_preset.findData(preset),
        ))
        del blockers
        self.force_all.setChecked(False)
        self._submitted_options=None
        self._update_enabled_states()
        self._update_summary()

    def take_submitted_options(self) -> PreparationOptions:
        """Transfer reviewed consent once; later tasks return to safe defaults."""
        value=self.options()
        self._submitted_options=None
        self._allow_memory_pressure=False
        return value

    def options(self) -> PreparationOptions:
        if self._submitted_options is not None:
            return self._submitted_options
        return PreparationOptions(
            read_external_evidence=self.read_external.isChecked(),
            suggest_sdxl_tags=self.suggest_tags.isChecked(),
            suggest_plain_language=self.suggest_plain.isChecked(),
            confirm_safe_tags=(
                self.confirm_tags.isChecked() and self.suggest_tags.isChecked()
            ),
            prepare_preferred_descriptions=(
                self.prepare_descriptions.isChecked()
            ),
            skip_ai_value=self.skip_value.value(),
            skip_ai_unit=str(self.skip_unit.currentData() or "days"),
            force_all_ai=self.force_all.isChecked(),
            allow_memory_pressure=self._allow_memory_pressure,
            confidence_preset=str(
                self.confidence_preset.currentData() or "balanced"
            ),
            minimum_tag_votes=self.minimum_votes.value(),
            count_external_evidence_vote=self.external_vote.isChecked(),
        )

    @staticmethod
    def _has_selected_work(options: PreparationOptions) -> bool:
        return any((
            options.read_external_evidence,options.suggest_sdxl_tags,
            options.suggest_plain_language,options.has_automated_application,
        ))

    def remember_setup_return(self,options,paths,fingerprint_choice) -> None:
        """Hold one accepted review for a new, explicit post-Setup decision."""
        self._setup_return_review=(
            self._submitted_mode,options,tuple(paths),fingerprint_choice,
        )

    def discard_setup_return(self) -> None:
        self._setup_return_review=None

    def check_setup_return_sources(
        self,on_ready: Callable[[],None],
        on_state: Callable[[str,str],None] | None=None,
    ) -> bool:
        """Check the retained scope off the UI thread before leaving Setup."""
        def state(status: str="",detail: str="") -> None:
            if on_state is not None:
                on_state(status,detail)

        review=self._setup_return_review
        if review is None or self.impact_context_provider is None or self._setup_return_task is not None:
            return False
        paths=review[2]
        context=self.impact_context_provider()
        if context is None or tuple(context.images)!=paths:
            state(
                "Selection changed",
                "The selected images changed. Return to the image workspace, refresh it, "
                "and choose Prepare Images again. Nothing started.",
            )
            QMessageBox.information(
                self.window(),"Preparation Scope Changed",
                "The selected images changed while Setup was open. Nothing started. "
                "Return to the image workspace, refresh it, and choose Prepare Images again.",
            )
            return False

        from core.library_storage import LibraryStorageInspector
        from .workers import CoordinatedCallTask

        progress=QProgressDialog(
            "Checking selected images before returning…","Cancel",0,0,self.window(),
        )
        progress.setWindowTitle("Return to Preparation")
        progress.setWindowModality(Qt.WindowModal)
        progress.setMinimumDuration(0)
        progress.setAutoClose(False)
        progress.setAutoReset(False)
        state("Checking images","Checking the retained images before reopening the review. Cancel keeps your choices in Setup.")

        def work(job):
            inspector=LibraryStorageInspector()
            for index,path in enumerate(paths,1):
                if not job.checkpoint():
                    return None
                availability=inspector.inspect_image(path)
                if not availability.ready_for_write:
                    return (path,availability.label,availability.detail)
                job.update(index,len(paths),"Checking selected images")
            return ()

        task=CoordinatedCallTask(
            context.jobs,"Checking preparation return sources",work,resource="io",
        )
        self._setup_return_task=task

        def finish():
            if self._setup_return_task is task:
                self._setup_return_task=None
            progress.close()
            progress.deleteLater()

        def ready(result):
            if self._setup_return_task is not task:
                return
            finish()
            if result is None:
                state()
                return
            fresh=self.impact_context_provider()
            if not context.is_current() or fresh is None or tuple(fresh.images)!=paths:
                state(
                    "Selection changed",
                    "The selected images changed during the check. Return to the image "
                    "workspace, refresh it, and choose Prepare Images again. Nothing started.",
                )
                QMessageBox.information(
                    self.window(),"Preparation Scope Changed",
                    "The selected images changed during the check. Nothing started. "
                    "Return to the image workspace, refresh it, and choose Prepare Images again.",
                )
                return
            if result:
                path,label,detail=result
                state(
                    "Source unavailable",
                    "A selected image is not ready. Restore access and choose Return to "
                    "Preparation again, or refresh the image workspace and select current "
                    "images. Nothing started.",
                )
                QMessageBox.warning(
                    self.window(),"Preparation Source Unavailable",
                    f"{path.name} is not ready: {label}. {detail}\n\n"
                    "Nothing started. Restore access and choose Return to Preparation "
                    "again, or refresh the image workspace and select current images.",
                )
                return
            state()
            on_ready()

        def failed(message):
            if self._setup_return_task is not task:
                return
            finish()
            state(
                "Check needs attention",
                "Selected images could not be checked. Choose Return to Preparation "
                "again; no image work started.",
            )
            QMessageBox.warning(
                self.window(),"Preparation Check Unavailable",
                "The selected images could not be checked. Nothing started. "
                f"Try Return to Preparation again.\n\n{message}",
            )

        def cancelled():
            if self._setup_return_task is task:
                task.cancel()
                finish()
                state()

        progress.canceled.connect(cancelled)
        task.signals.complete.connect(ready)
        task.signals.failed.connect(failed)
        task.signals.cancelled.connect(cancelled)
        progress.show()
        task.start()
        return True

    def reopen_setup_return(self) -> bool:
        review=self._setup_return_review
        self._setup_return_review=None
        if review is None or self.impact_context_provider is None:
            return False
        mode,options,paths,fingerprint_choice=review
        context=self.impact_context_provider()
        if context is None or tuple(context.images)!=paths:
            QMessageBox.information(
                self,"Preparation Scope Changed",
                "The selected images changed while Setup was open. Nothing started. "
                "Choose Prepare Images again to review the current scope.",
            )
            return False
        self._action_requested(mode,prefill=(options,fingerprint_choice))
        return True

    @staticmethod
    def _prefill_options(editor,options: PreparationOptions) -> None:
        for control,value in (
            (editor.read_external,options.read_external_evidence),
            (editor.suggest_tags,options.suggest_sdxl_tags),
            (editor.suggest_plain,options.suggest_plain_language),
            (editor.confirm_tags,options.confirms_safe_tags),
            (editor.prepare_descriptions,options.prepares_preferred_descriptions),
            (editor.force_all,options.force_all_ai),
            (editor.external_vote,options.uses_external_evidence_vote),
        ):
            control.setChecked(bool(value))
        editor.skip_value.setValue(options.skip_ai_value)
        editor.skip_unit.setCurrentIndex(max(0,editor.skip_unit.findData(options.skip_ai_unit)))
        editor.minimum_votes.setValue(options.tag_agreement_requirement())
        editor.confidence_preset.setCurrentIndex(max(
            0,editor.confidence_preset.findData(options.normalized_confidence_preset),
        ))
        editor._update_enabled_states()
        editor._update_summary()

    def _action_requested(self,mode: str="full",*,prefill=None) -> None:
        if self._inline_options:
            self.requested.emit()
            return
        dialog,editor=self.create_options_dialog(mode)
        try:
            if prefill is not None:
                options,fingerprint_choice=prefill
                self._prefill_options(editor,options)
                if fingerprint_choice is not None:
                    dialog.create_duplicate_fingerprints.setChecked(bool(fingerprint_choice))
            if dialog.exec()!=QDialog.Accepted:
                return
            submitted=editor.options()
            context=getattr(dialog,"impact_context",None)
            if context is not None and not context.is_current():
                QMessageBox.information(
                    self,"Preparation Scope Changed",
                    "The image scope or model configuration changed. Open Prepare Images again to review it.",
                )
                return
            preflight_requested=bool(
                getattr(dialog,"duplicate_preflight_requested",lambda:False)()
            )
            if preflight_requested:
                runner=self.duplicate_preflight_runner
                if not callable(runner):
                    QMessageBox.warning(
                        self,"Duplicate Check Unavailable",
                        "The Ingest duplicate check is not available in this "
                        "preparation workspace. No defaults were saved and "
                        "preparation did not start.",
                    )
                    return
                completed=runner(
                    dialog,context,
                    include_similar=bool(dialog.duplicate_preflight_similar.isChecked()),
                    lower_similarity=float(dialog.duplicate_preflight_lower.value()),
                    upper_similarity=float(dialog.duplicate_preflight_upper.value()),
                )
                if not completed:
                    return
                if context is not None and not context.is_current():
                    QMessageBox.information(
                        self,"Preparation Scope Changed",
                        "The image scope or model configuration changed during "
                        "the duplicate check. No defaults were saved and "
                        "preparation did not start.",
                    )
                    return
            fingerprint_choice=(
                bool(dialog.create_duplicate_fingerprints.isChecked())
                if hasattr(dialog,"create_duplicate_fingerprints") else None
            )
            if mode=="full" and dialog.save_defaults.isChecked():
                saved_fields=(
                    "ingest_create_duplicate_fingerprints",
                    "ingest_check_duplicates","ingest_include_similar_duplicates",
                    "ingest_duplicate_similarity_minimum","ingest_duplicate_similarity_maximum",
                    "prepare_read_external_evidence",
                    "suggest_wd_for_missing_tags",
                    "suggest_joycaption_for_missing_descriptions",
                    "prepare_ai_skip_value","prepare_ai_skip_unit",
                    "prepare_confidence_preset","prepare_tag_vote_minimum",
                    "prepare_external_evidence_vote","prepare_confirm_safe_tags",
                    "prepare_preferred_descriptions",
                )
                missing=object()
                before={name:getattr(self.settings,name,missing) for name in saved_fields}
                try:
                    if fingerprint_choice is not None:
                        self.settings.ingest_create_duplicate_fingerprints=fingerprint_choice
                    if hasattr(dialog,"duplicate_preflight_check"):
                        self.settings.ingest_check_duplicates=dialog.duplicate_preflight_check.isChecked()
                        self.settings.ingest_include_similar_duplicates=dialog.duplicate_preflight_similar.isChecked()
                        self.settings.ingest_duplicate_similarity_minimum=dialog.duplicate_preflight_lower.value()
                        self.settings.ingest_duplicate_similarity_maximum=dialog.duplicate_preflight_upper.value()
                    editor.persist_settings()
                except Exception as exc:
                    for name,value in before.items():
                        if value is missing:
                            delattr(self.settings,name)
                        else:
                            setattr(self.settings,name,value)
                    QMessageBox.warning(
                        self,"Preparation Defaults Not Saved",
                        "Could not save these choices as defaults. Preparation did not "
                        f"start. Reopen Prepare Images to retry.\n\n{exc}",
                    )
                    return
                self.refresh_from_settings()
            self._submitted_fingerprints=fingerprint_choice
            self._submitted_options=submitted
            self._submitted_paths=context.images if context is not None else None
            self._submitted_mode=mode
            self.requested.emit()
        finally:
            dialog.deleteLater()

    def take_submitted_fingerprints(self):
        choice=self._submitted_fingerprints
        self._submitted_fingerprints=None
        return choice

    def take_submitted_paths(self):
        """Consume the exact scope accepted in the shared options dialog."""
        paths=self._submitted_paths
        self._submitted_paths=None
        return paths

    def create_options_dialog(
        self,mode: str="full",
    ) -> tuple[QDialog,"PreparationControls"]:
        """Build the shared modal so focused tests can inspect its exact state."""
        mode=mode if mode in {
            "full","evidence","tags","descriptions","all_ai","force_all_ai",
        } else "full"
        refresh=self._refresh_context
        dialog=QDialog(self)
        context=(self.impact_context_provider()
                 if self.impact_context_provider is not None else None)
        dialog.impact_context=context
        scope_count=len(context.images) if context is not None else self._scope_count
        scope_description=(
            self._scope_description if scope_count==self._scope_count
            else f"{scope_count:,} images in the current preparation scope"
        )
        dialog.setWindowTitle(({
            "full":"Re-run Preparation",
            "evidence":"Re-read TXT / Metadata",
            "tags":"Refresh AI Tags",
            "descriptions":"Refresh AI Descriptions",
            "all_ai":"Refresh All AI",
            "force_all_ai":"Force All AI Scans",
        } if refresh else {
            "full":"Prepare Images",
            "evidence":"Read TXT / Metadata",
            "tags":"Generate AI Tags",
            "descriptions":"Generate AI Descriptions",
            "all_ai":"Generate All AI",
            "force_all_ai":"Force All AI Scans",
        })[mode])
        dialog.setMinimumWidth(760)
        dialog.setMaximumWidth(960)
        layout=QVBoxLayout(dialog)
        intro=QLabel((
            f"Scope: {scope_description} — already complete. Choose what to refresh.\n"
            "Changes: Image Knowledge only. Source images, TXT and Metadata stay unchanged."
            if refresh else
            f"Scope: {scope_description}.\n"
            "Changes: Image Knowledge only. Source images, TXT and Metadata stay unchanged."
        ))
        intro.setTextFormat(Qt.PlainText)
        intro.setWordWrap(True)
        intro.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Maximum)
        intro.setAccessibleName("Preparation scope")
        layout.addWidget(intro)
        detail=QLabel()
        detail.setTextFormat(Qt.PlainText)
        detail.setObjectName("mutedLabel")
        detail.setWordWrap(True)
        detail.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Maximum)
        layout.addWidget(detail)
        sections=QTabWidget()
        sections.setAccessibleName("Preparation choices")
        layout.addWidget(sections,1)
        dialog.choice_sections=sections
        body=QWidget(); body_layout=QVBoxLayout(body)
        body_layout.setContentsMargins(4,4,4,4)
        duplicate_body=QWidget(); duplicate_layout=QVBoxLayout(duplicate_body)
        duplicate_layout.setContentsMargins(4,4,4,4)
        from .ai_runtime_controls import runtime_policy_text
        runtime_note=QLabel(runtime_policy_text(self.settings))
        runtime_note.setWordWrap(True)
        runtime_note.setAccessibleName("Preparation AI memory policy")
        body_layout.addWidget(runtime_note)
        scroll=QScrollArea(); scroll.setWidgetResizable(True)
        scroll.setFrameShape(QScrollArea.NoFrame)
        scroll.setWidget(body); sections.addTab(scroll,"Preparation Steps")
        dialog.options_scroll=scroll
        duplicate_scroll=QScrollArea(); duplicate_scroll.setWidgetResizable(True)
        duplicate_scroll.setFrameShape(QScrollArea.NoFrame)
        duplicate_scroll.setWidget(duplicate_body)
        sections.addTab(duplicate_scroll,"Duplicates")
        dialog.duplicates_scroll=duplicate_scroll
        fingerprints=QCheckBox("Create missing duplicate fingerprints")
        fingerprints.setChecked(bool(getattr(
            self.settings,"ingest_create_duplicate_fingerprints",True,
        )))
        fingerprints.setToolTip(
            "Prepare reusable fingerprints for the chosen images only. "
            "Valid cached entries are reused; changed images are refreshed. "
            "This does not compare libraries or change Image Knowledge."
        )
        fingerprints.setAccessibleDescription(fingerprints.toolTip())
        duplicate_layout.addWidget(fingerprints)
        fingerprint_help=QLabel(
            "Fingerprints cover only the selected images. They do not compare libraries."
        )
        fingerprint_help.setObjectName("mutedLabel")
        fingerprint_help.setWordWrap(True)
        duplicate_layout.addWidget(fingerprint_help)
        dialog.create_duplicate_fingerprints=fingerprints
        editor=PreparationControls(
            self.settings,dialog,inline_options=True,persist_changes=False,
        )
        editor.set_scope(
            scope_count,button_text="Start Preparation",
            description=scope_description,
        )
        editor._arrange_sections(1000)
        editor.action_button.hide()
        editor.undo_button.hide()
        editor.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Maximum)
        editor._apply_focus_mode(mode)
        body_layout.addWidget(editor)
        memory_choice=QPushButton("Review VRAM / Parallel Choice...")
        memory_choice.setAccessibleName("Review preparation VRAM and parallel override")
        memory_choice.setSizePolicy(QSizePolicy.Maximum,QSizePolicy.Fixed)
        body_layout.addWidget(memory_choice)
        memory_status=QLabel("Safe default: hold if VRAM is insufficient or unverified. No external queue, cancel or unload action is available here.")
        memory_status.setWordWrap(True);memory_status.setAccessibleName("Preparation task memory choice")
        body_layout.addWidget(memory_status)
        dialog.memory_choice_button=memory_choice;dialog.memory_choice_status=memory_status
        def choose_memory_override():
            from .ai_runtime_controls import review_preparation_memory_override
            editor._allow_memory_pressure=review_preparation_memory_override(dialog,self.settings,editor.options(),scope_description=scope_description)
            memory_status.setText("Explicit run-anyway / parallel override for this preparation only. Scope and quality unchanged; other tasks keep the safe default." if editor._allow_memory_pressure else "Safe default retained. No override approved.")
        memory_choice.clicked.connect(choose_memory_override)
        def invalidate_memory_choice():
            editor._allow_memory_pressure=False
            memory_choice.setEnabled(editor.suggest_tags.isChecked() or editor.suggest_plain.isChecked())
            memory_status.setText("Safe default: hold if VRAM is insufficient or unverified. Review again after changing task options.")
        editor.options_changed.connect(invalidate_memory_choice)
        invalidate_memory_choice()
        if context is not None and callable(self.duplicate_preflight_runner):
            preflight_group=QGroupBox("Duplicate Finding")
            preflight_group.setAccessibleName("Optional Ingest duplicate preflight")
            preflight_layout=QVBoxLayout(preflight_group)
            preflight_layout.setContentsMargins(8,5,8,6)
            preflight_layout.setSpacing(5)
            check_duplicates=QCheckBox("Check for Duplicates…")
            check_duplicates.setChecked(bool(getattr(self.settings,"ingest_check_duplicates",False)))
            check_duplicates.setAccessibleDescription(
                "Compare this exact incoming scope with the explicitly included "
                "Dataset libraries before preparation. The result is review "
                "evidence and never removes, skips, or changes an image."
            )
            preflight_layout.addWidget(check_duplicates)
            exact_help=QLabel(
                "The default check finds byte-identical files and images with "
                "identical decoded pixels. Preparation remains unchanged unless "
                "you explicitly continue after reviewing the summary."
            )
            exact_help.setWordWrap(True); exact_help.setObjectName("mutedLabel")
            preflight_layout.addWidget(exact_help)
            similar=QCheckBox("Also include visually similar candidates")
            similar.setChecked(bool(getattr(self.settings,"ingest_include_similar_duplicates",False)))
            similar.setAccessibleDescription(
                "Opt in to review perceptual candidates inside the configured "
                "inclusive similarity range. These candidates are not exact duplicates."
            )
            preflight_layout.addWidget(similar)
            range_row=QHBoxLayout()
            range_label=QLabel("Inclusive similarity range")
            lower=QSpinBox(); lower.setRange(70,100); lower.setValue(int(getattr(self.settings,"ingest_duplicate_similarity_minimum",DEFAULT_SIMILARITY_PERCENT)))
            lower.setSuffix("% minimum")
            upper=QSpinBox(); upper.setRange(70,100); upper.setValue(max(lower.value(),int(getattr(self.settings,"ingest_duplicate_similarity_maximum",100))))
            upper.setSuffix("% maximum")
            lower.setAccessibleName("Visual similarity minimum")
            upper.setAccessibleName("Visual similarity maximum")
            range_label.setBuddy(lower)
            range_help=(
                "Both endpoints are included. Lower values find more visual "
                "candidates and can take longer. Exact matches are independent "
                "of this range."
            )
            for control in (range_label,lower,upper):
                control.setToolTip(range_help)
            lower.setAccessibleDescription(range_help)
            upper.setAccessibleDescription(range_help)
            range_row.addWidget(range_label); range_row.addWidget(lower)
            range_row.addWidget(upper); range_row.addStretch(1)
            preflight_layout.addLayout(range_row)
            duplicate_layout.addWidget(preflight_group)
            dialog.duplicate_preflight_check=check_duplicates
            dialog.duplicate_preflight_similar=similar
            dialog.duplicate_preflight_lower=lower
            dialog.duplicate_preflight_upper=upper
            dialog.duplicate_preflight_requested=check_duplicates.isChecked

            def update_preflight_controls() -> None:
                enabled=check_duplicates.isChecked()
                similar.setEnabled(enabled)
                lower.setEnabled(enabled and similar.isChecked())
                upper.setEnabled(enabled and similar.isChecked())

            def align_preflight_range(value: int,source) -> None:
                if lower.value()>upper.value():
                    if source is lower:upper.setValue(value)
                    else:lower.setValue(value)

            check_duplicates.toggled.connect(update_preflight_controls)
            similar.toggled.connect(update_preflight_controls)
            lower.valueChanged.connect(lambda value:align_preflight_range(value,lower))
            upper.valueChanged.connect(lambda value:align_preflight_range(value,upper))
            update_preflight_controls()
        body_layout.addStretch(1)
        duplicate_layout.addStretch(1)
        guard_scrollable_value_controls(body)
        guard_scrollable_value_controls(duplicate_body)
        duplicate_summary=QLabel()
        duplicate_summary.setObjectName("mutedLabel")
        duplicate_summary.setWordWrap(True)
        layout.addWidget(duplicate_summary)
        dialog.duplicate_summary=duplicate_summary

        def update_duplicate_summary() -> None:
            fingerprint_text="Create missing fingerprints" if fingerprints.isChecked() else "Skip fingerprints"
            if hasattr(dialog,"duplicate_preflight_check") and check_duplicates.isChecked():
                comparison="Compare included Dataset libraries before preparation"
                if similar.isChecked():
                    comparison+=f"; include visual candidates from {lower.value()}–{upper.value()}%"
            else:
                comparison="No duplicate comparison"
            duplicate_summary.setText(f"Duplicates: {fingerprint_text} · {comparison}.")

        fingerprints.toggled.connect(update_duplicate_summary)
        if hasattr(dialog,"duplicate_preflight_check"):
            for control in (check_duplicates,similar):
                control.toggled.connect(update_duplicate_summary)
            lower.valueChanged.connect(update_duplicate_summary)
            upper.valueChanged.connect(update_duplicate_summary)
        update_duplicate_summary()
        if mode=="full":
            save_defaults=QCheckBox("Remember preparation and duplicate choices")
            save_defaults.setChecked(True)
            save_defaults.setAccessibleDescription(
                "When checked, Start saves the selected preparation defaults. "
                "Fingerprint creation, duplicate comparison, visual matching and its range are included. "
                "Selection, force scans and memory-pressure overrides remain task-only."
            )
            layout.addWidget(save_defaults)
            dialog.save_defaults=save_defaults
        persistence=QLabel(
            "Start remembers preparation and duplicate choices only "
            "when checked; Cancel, Preview Impact and a cancelled duplicate check save nothing."
            if mode=="full" else
            "These choices apply to this run only. Saved preparation defaults stay unchanged."
        )
        persistence.setWordWrap(True)
        persistence.setObjectName("mutedLabel")
        layout.addWidget(persistence)
        validation=QLabel()
        validation.setObjectName("warningLabel")
        validation.setWordWrap(True)
        validation.hide()
        layout.addWidget(validation)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        if context is not None:
            preview=buttons.addButton("Preview Impact…",QDialogButtonBox.ActionRole)
            preview.setAutoDefault(False)
            preview.setAccessibleDescription(
                "Check the exact image scope and current Description evidence without saving or running preparation."
            )

            def show_impact():
                from .preparation_impact_dialog import PreparationImpactDialog
                selected_options=editor.options()
                impact=PreparationImpactDialog(
                    context,selected_options,parent=dialog,
                    is_current=lambda:(
                        context.is_current() and editor.options()==selected_options
                    ),
                )
                try:
                    impact.exec()
                finally:
                    impact.deleteLater()

            preview.clicked.connect(show_impact)
        start=buttons.addButton(
            "Start Refresh" if refresh else "Start Preparation",
            QDialogButtonBox.AcceptRole,
        )
        start.setProperty("primary",True)
        start.setDefault(True)
        buttons.rejected.connect(dialog.reject)
        layout.addWidget(buttons)

        def has_work() -> bool:
            return self._has_selected_work(editor.options()) or bool(
                hasattr(dialog,"create_duplicate_fingerprints")
                and dialog.create_duplicate_fingerprints.isChecked()
            )

        def refresh_detail() -> None:
            provider=self._scope_detail_provider
            text=str(provider(editor.options()) if provider else "").strip()
            detail.setText(text)
            detail.setVisible(bool(text))
            if has_work():
                validation.hide()

        def submit() -> None:
            if not scope_count:
                validation.setText("No images remain in this preparation scope. Close and choose images again.")
                validation.show()
                return
            if context is not None and not context.is_current():
                validation.setText(
                    "The image scope or model configuration changed. Close and reopen Prepare Images."
                )
                validation.show()
                start.setEnabled(False)
                return
            if not has_work():
                validation.setText("Choose at least one preparation step.")
                validation.show()
                validation.setFocus()
                return
            dialog.accept()

        editor.options_changed.connect(refresh_detail)
        start.clicked.connect(submit)
        refresh_detail()
        dialog.adjustSize()
        dialog.resize(
            min(900,max(760,dialog.sizeHint().width())),
            min(720,dialog.sizeHint().height()),
        )
        return dialog,editor

    def _apply_focus_mode(self,mode: str) -> None:
        """Configure one focused launcher without changing remembered defaults."""
        if mode=="full":
            return
        controls=(
            self.read_external,self.suggest_tags,self.suggest_plain,
            self.confirm_tags,self.prepare_descriptions,
        )
        blockers=[QSignalBlocker(control) for control in controls]
        self.read_external.setChecked(mode=="evidence")
        self.suggest_tags.setChecked(mode in {"tags","all_ai","force_all_ai"})
        self.suggest_plain.setChecked(mode in {
            "descriptions","all_ai","force_all_ai",
        })
        self.confirm_tags.setChecked(False)
        self.prepare_descriptions.setChecked(False)
        self.force_all.setChecked(mode=="force_all_ai")
        del blockers
        self.sources_group.setVisible(mode=="evidence")
        self.generation_group.setVisible(mode in {
            "tags","descriptions","all_ai","force_all_ai",
        })
        self.application_group.hide()
        self.vote_options.hide()
        self.tuning_toggle.setVisible(mode!="evidence")
        self.tuning_summary.setVisible(mode!="evidence")
        self._update_enabled_states()
        self._update_summary()

    def persist_settings(self) -> None:
        """Commit the reviewed dialog choices as the next-run defaults."""
        options=self.options()
        self.settings.prepare_read_external_evidence=options.read_external_evidence
        self.settings.suggest_wd_for_missing_tags=options.suggest_sdxl_tags
        self.settings.suggest_joycaption_for_missing_descriptions=(
            options.suggest_plain_language
        )
        self.settings.prepare_ai_skip_value=options.skip_ai_value
        self.settings.prepare_ai_skip_unit=options.skip_ai_unit
        self.settings.prepare_confidence_preset=options.normalized_confidence_preset
        self.settings.prepare_tag_vote_minimum=options.tag_agreement_requirement()
        self.settings.prepare_external_evidence_vote=(
            options.uses_external_evidence_vote
        )
        self.settings.prepare_confirm_safe_tags=options.confirms_safe_tags
        self.settings.prepare_preferred_descriptions=(
            options.prepares_preferred_descriptions
        )
        self.settings.save()

    def _source_or_generation_changed(self,*_args) -> None:
        if self._persist_changes:
            self.settings.prepare_read_external_evidence=(
                self.read_external.isChecked()
            )
            self.settings.suggest_wd_for_missing_tags=self.suggest_tags.isChecked()
            self.settings.suggest_joycaption_for_missing_descriptions=(
                self.suggest_plain.isChecked()
            )
            self.settings.save()

    def _freshness_changed(self,*_args) -> None:
        if self._persist_changes:
            self.settings.prepare_ai_skip_value=self.skip_value.value()
            self.settings.prepare_ai_skip_unit=str(self.skip_unit.currentData() or "days")
            self.settings.save()
        self._update_summary()

    def _preset_changed(self,*_args) -> None:
        preset=str(self.confidence_preset.currentData() or "balanced")
        if preset in CONFIDENCE_PRESETS:
            policy=CONFIDENCE_PRESETS[preset]
            minimum_blocker=QSignalBlocker(self.minimum_votes)
            evidence_blocker=QSignalBlocker(self.external_vote)
            self.minimum_votes.setValue(int(policy["minimum_tag_votes"]))
            self.external_vote.setChecked(bool(policy["external_evidence_vote"]))
            del minimum_blocker,evidence_blocker
        else:
            matching=self._matching_preset()
            if matching:
                blocker=QSignalBlocker(self.confidence_preset)
                self.confidence_preset.setCurrentIndex(
                    self.confidence_preset.findData(matching)
                )
                del blocker
        self._persist_confidence()
        self._update_summary()

    def _confidence_value_changed(self,*_args) -> None:
        preset=self._matching_preset() or "custom"
        blocker=QSignalBlocker(self.confidence_preset)
        self.confidence_preset.setCurrentIndex(
            self.confidence_preset.findData(preset)
        )
        del blocker
        self._persist_confidence()
        self._update_summary()

    def _persist_confidence(self) -> None:
        if not self._persist_changes:
            return
        self.settings.prepare_confidence_preset=str(
            self.confidence_preset.currentData() or "custom"
        )
        self.settings.prepare_tag_vote_minimum=self.minimum_votes.value()
        self.settings.prepare_external_evidence_vote=(
            self.external_vote.isChecked()
        )
        self.settings.save()

    def _application_changed(self,*_args) -> None:
        if self._persist_changes:
            self.settings.prepare_confirm_safe_tags=self.confirm_tags.isChecked()
            self.settings.prepare_preferred_descriptions=(
                self.prepare_descriptions.isChecked()
            )
            self.settings.save()
        self._update_enabled_states()
        self._update_summary()

    def _matching_preset(self) -> str:
        configured=(self.minimum_votes.value(),self.external_vote.isChecked())
        return next((
            key for key,policy in CONFIDENCE_PRESETS.items()
            if configured==(
                int(policy["minimum_tag_votes"]),
                bool(policy["external_evidence_vote"]),
            )
        ),"")

    def _update_enabled_states(self,*_args) -> None:
        enabled=(
            not self._busy
            and self.suggest_tags.isChecked()
            and self.confirm_tags.isChecked()
        )
        self.confidence_preset.setEnabled(enabled)
        self.minimum_votes.setEnabled(enabled)
        self.external_vote.setEnabled(enabled)

    def _update_summary(self,*_args) -> None:
        choices=[]
        if self.read_external.isChecked(): choices.append("TXT/Metadata evidence")
        if self.suggest_tags.isChecked(): choices.append("AI Tags")
        if self.suggest_plain.isChecked(): choices.append("AI Descriptions")
        confirm_tags=(
            self.confirm_tags.isChecked() and self.suggest_tags.isChecked()
        )
        if confirm_tags: choices.append("safe Tag confirmation")
        if self.prepare_descriptions.isChecked():
            choices.append("Description fill/upgrade")
        work=", ".join(choices) if choices else "no preparation steps selected"
        reuse=(
            "all AI scans will run"
            if self.force_all.isChecked()
            else f"matching AI from the last {self.skip_value.value()} "
                 f"{str(self.skip_unit.currentText()).lower()} will be reused"
        )
        scope=self._scope_description or f"{self._scope_count:,} image(s)"
        preset=str(self.confidence_preset.currentData() or "balanced")
        preset_label=(
            CONFIDENCE_PRESETS[preset]["label"]
            if preset in CONFIDENCE_PRESETS else "Custom"
        )
        required=self.minimum_votes.value()
        external=(
            " Matching typed TXT/Metadata adds one vote only when a current "
            "AI tagger also found that Tag."
            if self.external_vote.isChecked()
            else " TXT/Metadata does not add a vote."
        )
        authority=(
            f"Warning: one current AI model may confirm a known Tag.{external}"
            if required==1
            else (
                f"Requires at least {required} independent votes before a known, "
                f"conflict-free Tag can be confirmed. Each distinct current AI "
                f"tagger counts once.{external}"
            )
        )
        detail=(
            authority
            + " Ambiguities and conflicts stay in review; ignored or rejected "
              "findings stay suppressed; stale, unavailable, or failed writes "
              "confirm nothing. Description fill/upgrade still requires an exact "
              "match from the selected preferred captioner."
            if confirm_tags
            else (
                "Turn on Generate AI Tags and Confirm Safe Tags to use these "
                "confidence controls. AI results and raw evidence are still saved."
            )
        )
        warning=required==1 and confirm_tags
        ai_selected=self.suggest_tags.isChecked() or self.suggest_plain.isChecked()
        tuning=[
            ("Force: run every selected AI scan" if self.force_all.isChecked()
             else f"Reuse matching AI: {self.skip_value.value()} "
                  f"{str(self.skip_unit.currentText()).lower()}")
            if ai_selected else "AI analysis off",
            (f"Tag confirmation: {required} vote{'s' if required!=1 else ''}; "
             + ("TXT/Metadata counts" if self.external_vote.isChecked()
                else "AI votes only")) if confirm_tags else "Tag confirmation off",
        ]
        self.tuning_summary.setText(" • ".join(tuning))
        self.tuning_summary.setToolTip(reuse+". "+detail)
        self.confidence_detail.setText(
            "⚠ One AI model may confirm Tags" if warning else ""
        )
        self.confidence_detail.setVisible(warning)
        self.confidence_detail.setToolTip(detail)
        self.confidence_detail.setAccessibleDescription(detail)
        self.application_group.setToolTip(detail)
        self.confidence_preset.setAccessibleDescription(detail)
        self.confidence_preset.setToolTip(detail)
        self.minimum_votes.setAccessibleDescription(detail)
        self.minimum_votes.setToolTip(detail)
        self.external_vote.setAccessibleDescription(detail)
        self.external_vote.setToolTip(detail)
        self.confirm_tags.setAccessibleDescription(detail)
        object_name=(
            "warningLabel"
            if warning
            else "mutedLabel"
        )
        if self.confidence_detail.objectName()!=object_name:
            self.confidence_detail.setObjectName(object_name)
            self.confidence_detail.style().unpolish(self.confidence_detail)
            self.confidence_detail.style().polish(self.confidence_detail)
        self._update_enabled_states()
        confidence_summary=(
            preset_label if confirm_tags else "Tag confirmation off"
        )
        visible=[f"{self._scope_count:,} image(s)"]
        if self.read_external.isChecked():
            visible.append("TXT/Metadata")
        ai=[]
        if self.suggest_tags.isChecked():ai.append("Tags")
        if self.suggest_plain.isChecked():ai.append("Descriptions")
        if ai:visible.append("AI "+" + ".join(ai))
        applied=[]
        if confirm_tags:applied.append("Safe Tags")
        if self.prepare_descriptions.isChecked():applied.append("Descriptions")
        visible.append("Apply "+" + ".join(applied) if applied else "Review only")
        self.summary.setText(
            self._summary_context or " • ".join(visible)
        )
        preview=(
            f"Scope: {scope}. Steps: {work}. {reuse}. {confidence_summary} "
            "automation confidence. Preparation writes Image Knowledge JSON "
            "only; source TXT, Metadata, and image files are not rewritten. "
            "Nothing changes until Prepare is chosen."
        )
        summary_detail=self._summary_context_tooltip or preview
        self.summary.setToolTip(summary_detail)
        self.summary.setAccessibleDescription(summary_detail)
        self.action_button.setToolTip(preview)
        self.action_button.setAccessibleDescription(preview)
        self.options_changed.emit()
