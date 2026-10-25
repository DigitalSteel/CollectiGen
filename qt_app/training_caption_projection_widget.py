from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThreadPool,Signal,Qt
from PySide6.QtWidgets import (
    QComboBox,QDoubleSpinBox,QGridLayout,QGroupBox,QHBoxLayout,QHeaderView,
    QLabel,QLineEdit,QMessageBox,QProgressBar,QPushButton,QSplitter,QTextEdit,
    QTabWidget,QTreeWidget,QTreeWidgetItem,QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core import active_automation_engine

from .components import EmptyStatePanel,set_progress_bar,show_operation_in_progress
from .gallery import ProtectedImageView,content_safety_policy
from .workers import CancelableProgressCallTask


class TrainingCaptionProjectionWidget(QWidget):
    """Dataset-owned preview of automatic training-projection rule effects."""

    edit_image_data=Signal(str)
    export_requested=Signal(object)
    projection_exclusion_requested=Signal(object)
    return_requested=Signal()
    background_work_finished=Signal()

    PAGE_SIZE=100

    def __init__(self,state,parent=None):
        super().__init__(parent)
        self.state=state
        self.pool=QThreadPool.globalInstance()
        self._task=None
        self._shutdown_requested=False
        self._paths=()
        self._scope_label="Current Dataset result"
        self._focus={}
        self._scope_contextual=False
        self._impacts=[]
        self._impact_by_path={}
        self._errors=[]
        self._page=0
        self._error_page=0
        self._preview_run=False
        self._preview_failure=""
        self._retained_family=""
        self._invalidation_message=""
        self._build_ui()

    def _build_ui(self):
        layout=QVBoxLayout(self)
        layout.setContentsMargins(8,8,8,8)
        heading=QLabel("Review automatic training-caption rules")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        intro=QLabel(
            "See which confirmed Tags automatic rules combine or omit for training. "
            "Preview keeps your image facts and files unchanged."
        )
        intro.setWordWrap(True)
        intro.setObjectName("mutedLabel")
        layout.addWidget(intro)

        actions=QHBoxLayout()
        self.preview_button=QPushButton("Preview Automatic Rule Effects")
        self.preview_button.setProperty("primary",True)
        self.preview_button.clicked.connect(self.preview_projections)
        self.export_button=QPushButton("Review Exact Training TXT Export…")
        self.export_button.clicked.connect(self._request_export)
        self.return_button=QPushButton("Back to Dataset")
        self.return_button.clicked.connect(self.return_requested.emit)
        for button in (
            self.preview_button,self.export_button,self.return_button,
        ):
            button.setProperty("compact",True)
        actions.addWidget(self.preview_button)
        actions.addWidget(self.export_button)
        actions.addStretch(1)
        actions.addWidget(self.return_button)
        layout.addLayout(actions)

        self.scope_label=QLabel()
        self.scope_label.setWordWrap(True)
        self.scope_label.setObjectName("mutedLabel")
        self.scope_label.setAccessibleName("Training projection preview scope")
        layout.addWidget(self.scope_label)
        self.result_summary=QLabel()
        self.result_summary.setWordWrap(True)
        self.result_summary.setAccessibleName("Caption rule preview result")
        layout.addWidget(self.result_summary)

        self.focus_group=QGroupBox("Exact per-image training decision")
        focus_layout=QGridLayout(self.focus_group)
        self.focus_value=QLabel()
        self.focus_value.setWordWrap(True)
        self.focus_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.focus_detail=QLabel()
        self.focus_detail.setWordWrap(True)
        self.focus_detail.setObjectName("mutedLabel")
        self.focus_action=QPushButton()
        self.focus_action.setProperty("primary",True)
        self.focus_action.setProperty("compact",True)
        self.focus_action.clicked.connect(self._request_projection_exclusion)
        focus_layout.addWidget(self.focus_value,0,0,1,2)
        focus_layout.addWidget(self.focus_detail,1,0,1,2)
        focus_layout.addWidget(self.focus_action,2,0,1,1)
        focus_layout.setColumnStretch(1,1)
        layout.addWidget(self.focus_group)
        self.focus_group.hide()

        self.result_tabs=QTabWidget()
        self.effects_page=QWidget()
        effects_layout=QVBoxLayout(self.effects_page)
        effects_layout.setContentsMargins(0,8,0,0)
        filter_actions=QHBoxLayout()
        self.customize_filters=QPushButton("Customize Filters…")
        self.customize_filters.setCheckable(True)
        self.customize_filters.setProperty("compact",True)
        self.clear_filters_button=QPushButton("Clear Filters")
        self.clear_filters_button.setProperty("compact",True)
        self.clear_filters_button.clicked.connect(self._clear_view_filters)
        filter_actions.addWidget(self.customize_filters)
        filter_actions.addWidget(self.clear_filters_button)
        filter_actions.addStretch(1)
        effects_layout.addLayout(filter_actions)
        self.filters_panel=QWidget()
        filters=QHBoxLayout(self.filters_panel)
        filters.setContentsMargins(0,0,0,0)
        filters.addWidget(QLabel("Minimum rule confidence"))
        self.threshold=QDoubleSpinBox()
        self.threshold.setRange(.5,1)
        self.threshold.setSingleStep(.01)
        self.threshold.setValue(.95)
        self.threshold.setToolTip("Filter the displayed rule effects; this does not change which rules run.")
        self.threshold.valueChanged.connect(self._reset_page)
        filters.addWidget(self.threshold)
        self.family_filter=QComboBox()
        self.family_filter.addItem("All rule families","")
        self.family_filter.currentIndexChanged.connect(self._reset_page)
        filters.addWidget(self.family_filter)
        self.search=QLineEdit()
        self.search.setPlaceholderText("Filter images or rule explanations…")
        self.search.textChanged.connect(self._reset_page)
        filters.addWidget(self.search,1)
        effects_layout.addWidget(self.filters_panel)
        self.customize_filters.toggled.connect(self.filters_panel.setVisible)
        self.filters_panel.hide()

        progress_row=QHBoxLayout()
        self.progress=QProgressBar()
        self.progress.hide()
        self.cancel_button=QPushButton("Cancel Safely")
        self.cancel_button.setProperty("compact",True)
        self.cancel_button.clicked.connect(self.cancel_preview)
        self.cancel_button.hide()
        progress_row.addWidget(self.progress,1)
        progress_row.addWidget(self.cancel_button)
        layout.addLayout(progress_row)

        self.status=QLabel()
        self.status.setWordWrap(True)
        self.status.setObjectName("mutedLabel")
        layout.addWidget(self.status)
        from .components import bind_status_footer
        bind_status_footer(self,self.status,self.progress,self.cancel_button,title="Training captions")

        self.splitter=QSplitter(Qt.Horizontal)
        self.results=QTreeWidget()
        self.results.setHeaderLabels(("Image","Confidence","Automatic rule effect"))
        self.results.setRootIsDecorated(False)
        self.results.setAlternatingRowColors(False)
        self.results.header().setSectionResizeMode(0,QHeaderView.Stretch)
        self.results.header().setSectionResizeMode(1,QHeaderView.ResizeToContents)
        self.results.header().setSectionResizeMode(2,QHeaderView.ResizeToContents)
        self.results.itemSelectionChanged.connect(self._show_current)
        self.splitter.addWidget(self.results)

        detail=QWidget()
        detail_layout=QVBoxLayout(detail)
        self.protected_preview=ProtectedImageView(
            policy=content_safety_policy(),
            settings=getattr(self.state,"settings",None),
        )
        self.protected_preview.setMinimumHeight(280)
        self.preview_caption=QLabel()
        self.preview_caption.setAlignment(Qt.AlignCenter)
        self.preview_caption.setObjectName("mutedLabel")
        self.protected_preview.protection_changed.connect(
            self._update_preview_caption
        )
        edit=QPushButton("Edit Image Data…")
        self.edit_button=edit
        edit.setProperty("compact",True)
        edit.clicked.connect(self._edit_current)
        detail_layout.addWidget(self.protected_preview,3)
        detail_layout.addWidget(self.preview_caption)
        detail_layout.addWidget(edit,0,Qt.AlignCenter)
        self.detail=QTextEdit()
        self.detail.setReadOnly(True)
        detail_layout.addWidget(self.detail,2)
        self.splitter.addWidget(detail)
        self.splitter.setStretchFactor(0,3)
        self.splitter.setStretchFactor(1,2)
        self.splitter.setSizes((1050,650))
        effects_layout.addWidget(self.splitter,1)

        self.empty=EmptyStatePanel(
            "Choose Preview Automatic Rule Effects to inspect this exact "
            "Dataset scope without changing Image Knowledge or files.",
            "Automatic training-caption rule preview",minimum_height=260,
        )
        effects_layout.addWidget(self.empty,1)
        self.splitter.hide()

        pager=QHBoxLayout()
        self.previous_button=QPushButton("Previous")
        self.previous_button.clicked.connect(lambda:self._change_page(-1))
        self.page_label=QLabel("0 of 0")
        self.page_label.setAlignment(Qt.AlignCenter)
        self.next_button=QPushButton("Next")
        self.next_button.clicked.connect(lambda:self._change_page(1))
        for button in (self.previous_button,self.next_button):
            button.setProperty("compact",True)
        pager.addWidget(self.previous_button)
        pager.addWidget(self.page_label,1)
        pager.addWidget(self.next_button)
        effects_layout.addLayout(pager)
        self.result_tabs.addTab(self.effects_page,"Rule Effects")

        self.problems_page=QWidget()
        problems_layout=QVBoxLayout(self.problems_page)
        problems_layout.setContentsMargins(0,8,0,0)
        problem_hint=QLabel(
            "These images could not be checked. Restore file access or correct "
            "the image data, then choose Retry Preview for the same scope."
        )
        problem_hint.setWordWrap(True)
        problems_layout.addWidget(problem_hint)
        self.errors_tree=QTreeWidget()
        self.errors_tree.setHeaderLabels(("Image","Read problem"))
        self.errors_tree.setRootIsDecorated(False)
        self.errors_tree.setColumnWidth(0,260)
        self.errors_tree.setAccessibleName("Images with caption rule read problems")
        self.errors_tree.itemSelectionChanged.connect(self._show_read_problem)
        problems_layout.addWidget(self.errors_tree,2)
        self.error_detail=QTextEdit()
        self.error_detail.setReadOnly(True)
        self.error_detail.setAccessibleName("Selected image read problem details")
        problems_layout.addWidget(self.error_detail,1)
        self.error_edit_button=QPushButton("Edit Image Data…")
        self.error_edit_button.setProperty("compact",True)
        self.error_edit_button.clicked.connect(self._edit_read_problem)
        problems_layout.addWidget(self.error_edit_button,0,Qt.AlignRight)
        error_pager=QHBoxLayout()
        self.error_previous_button=QPushButton("Previous Problems")
        self.error_previous_button.clicked.connect(lambda:self._change_error_page(-1))
        self.error_page_label=QLabel()
        self.error_page_label.setAlignment(Qt.AlignCenter)
        self.error_next_button=QPushButton("Next Problems")
        self.error_next_button.clicked.connect(lambda:self._change_error_page(1))
        error_pager.addWidget(self.error_previous_button)
        error_pager.addWidget(self.error_page_label,1)
        error_pager.addWidget(self.error_next_button)
        problems_layout.addLayout(error_pager)
        self.result_tabs.addTab(self.problems_page,"Read Problems")
        self.result_tabs.setTabVisible(1,False)
        layout.addWidget(self.result_tabs,1)
        export_hint=QLabel(
            "For the final caption—including descriptions, ordering and your "
            "chosen export profile—choose Review Exact Training TXT Export."
        )
        export_hint.setWordWrap(True)
        export_hint.setObjectName("mutedLabel")
        layout.addWidget(export_hint)
        self._update_scope_text()
        self._render()

    def open_scope(self,paths,*,label="Current Dataset result",focus=None):
        """Load one frozen Dataset scope without starting a preview."""
        if self._task is not None:
            return False
        unique=[]
        seen=set()
        for value in paths:
            if not value:
                continue
            path=Path(value)
            key=str(path).casefold()
            if key in seen:
                continue
            seen.add(key)
            unique.append(path)
        self._paths=tuple(unique)
        self._scope_label=str(label or "Current Dataset result")
        self._focus=dict(focus) if isinstance(focus,dict) else {}
        self._scope_contextual=bool(self._focus.get("contextual"))
        self._clear_view_filters()
        self.customize_filters.setChecked(False)
        self._invalidation_message=""
        self._invalidate_preview()
        self._update_scope_text()
        self._render_focus()
        self.status.setText(
            "Destination opened without scanning. Choose Preview Automatic "
            "Rule Effects, or review the exact profile-aware TXT export."
        )
        return True

    def clear_contextual_scope(self):
        """Forget an Ingest-authorized path when its return trail is used."""
        if not self._scope_contextual:
            return
        self._paths=()
        self._focus={}
        self._scope_contextual=False
        self._invalidate_preview()
        self._update_scope_text()
        self._render_focus()

    def update_projection_exclusion(self,*,excluded: bool,detail: str):
        if not self._focus:
            return
        self._focus["excluded"]=bool(excluded)
        self._focus["available"]=True
        self._focus["detail"]=str(detail or "")
        self._render_focus()

    def _render_focus(self):
        focus=self._focus
        visible=bool(focus)
        self.focus_group.setVisible(visible)
        self.return_button.setText(
            "Back to Ingest" if self._scope_contextual else "Back to Dataset"
        )
        self.export_button.setToolTip(
            "Dataset export is unavailable for a contextual Ingest image. "
            "Return to Ingest after saving or reviewing this exact decision."
            if self._scope_contextual else
            "Review the exact profile-aware Training TXT export for this frozen Dataset scope."
        )
        self.edit_button.setEnabled(not self._scope_contextual)
        if not visible:
            return
        image=Path(str(focus.get("image") or ""))
        value=str(focus.get("value") or "")
        excluded=bool(focus.get("excluded"))
        available=bool(focus.get("available",True))
        self.focus_value.setText(
            f"{image.name}  •  Confirmed SDXL Tag: {value}"
        )
        self.focus_detail.setText(str(focus.get("detail") or ""))
        self.focus_action.setText(
            "Include in Future Training Again"
            if excluded else "Omit from Future Training"
        )
        self.focus_action.setEnabled(
            available and self._task is None and bool(self._paths)
        )
        self.focus_action.setToolTip(
            "Change only this image's versioned training exclusion. The "
            "confirmed fact remains and TXT/Metadata are not written."
        )

    def _request_projection_exclusion(self):
        if not self._focus or self._task is not None:
            return
        request=dict(self._focus)
        request["exclude"]=not bool(self._focus.get("excluded"))
        self.projection_exclusion_requested.emit(request)

    def derived_results_changed(self,paths=(),*,scope="images"):
        """Keep a frozen preview when exact changed images are outside it."""
        affected={str(Path(value)).casefold() for value in paths if value}
        if scope!="all" and not any(str(path).casefold() in affected for path in self._paths):
            return
        self.invalidate_after_authority_refresh(self._paths)

    def invalidate_after_authority_refresh(self,current_paths):
        """Discard a snapshot after Dataset or reusable-rule authority changes."""
        if not self._paths:
            return
        current={str(Path(value)).casefold() for value in current_paths if value}
        missing=(
            not self._scope_contextual
            and any(str(path).casefold() not in current for path in self._paths)
        )
        if missing:
            self._paths=()
            message=(
                "The frozen Dataset scope changed and this automatic-rule "
                "preview was discarded. Choose a current Dataset scope again."
            )
        else:
            message=(
                "Dataset or reusable rule authority changed. The previous "
                "automatic-rule preview was discarded; preview this frozen "
                "scope again before comparing rule effects."
            )
        self._invalidation_message=message
        if self._task is not None:
            self.cancel_preview()
            return
        self._invalidate_preview()
        self._update_scope_text()
        self._render_focus()
        self.status.setText(message)
        self._invalidation_message=""

    def _update_scope_text(self):
        count=len(self._paths)
        image_label="image" if count==1 else "images"
        self.scope_label.setText(
            f"Scope: {self._scope_label} • {count:,} {image_label}. "
            "This frozen scope is never broadened automatically."
        )
        self.preview_button.setEnabled(bool(self._paths) and self._task is None)
        self.export_button.setEnabled(
            bool(self._paths) and self._task is None
            and not self._scope_contextual
        )
        if hasattr(self,"focus_action"):
            self._render_focus()

    def _invalidate_preview(self):
        self._impacts=[]
        self._impact_by_path={}
        self._errors=[]
        self._page=0
        self._error_page=0
        self._preview_run=False
        self._preview_failure=""
        self.family_filter.blockSignals(True)
        self.family_filter.clear()
        self.family_filter.addItem("All rule families","")
        self.family_filter.blockSignals(False)
        self.results.clear()
        self.protected_preview.clear("No image selected")
        self.preview_caption.clear()
        self.detail.clear()
        self._render()

    def _set_busy(self,busy,message=""):
        self.preview_button.setEnabled(not busy and bool(self._paths))
        self.export_button.setEnabled(
            not busy and bool(self._paths) and not self._scope_contextual
        )
        self.return_button.setEnabled(not busy)
        self.result_tabs.setEnabled(not busy)
        self.customize_filters.setEnabled(not busy)
        self.clear_filters_button.setEnabled(not busy)
        self.threshold.setEnabled(not busy)
        self.family_filter.setEnabled(not busy)
        self.search.setEnabled(not busy)
        self.results.setEnabled(not busy)
        if hasattr(self,"focus_action"):
            self.focus_action.setEnabled(
                not busy and bool(self._paths)
                and bool(self._focus.get("available",True))
            )
        self.progress.setVisible(busy)
        self.cancel_button.setVisible(busy)
        self.cancel_button.setEnabled(busy)
        if busy:
            set_progress_bar(
                self.progress,detail=message,unknown_text="Starting…",
            )
        if message:
            self.status.setText(message)

    def has_active_background_work(self):
        return self._task is not None

    def request_background_shutdown(self):
        if self._task is None:
            return False
        self._shutdown_requested=True
        self.cancel_preview()
        return True

    def _notify_background_work_finished(self):
        if self._task is None:
            self.background_work_finished.emit()
            self._publish_visible_status("warning" if self._errors or self._preview_failure else "info")

    def show_export_outcome(self,message,*,severity="info"):
        """Keep the exact export result visible at this retained entry point."""
        self.status.setText(message)
        self._publish_visible_status(severity)

    def _publish_visible_status(self,severity):
        if self.isVisible():
            from .components import refresh_status_footer
            refresh_status_footer(self)
            for binding in getattr(self,"_status_footer_bindings",()):
                if binding.footer is not None:
                    binding.footer.show_message(self.status.text(),severity=severity)

    def preview_projections(self):
        if self._task is not None:
            return
        if not self._paths:
            QMessageBox.information(
                self,"No Dataset scope",
                "Return to Dataset and choose a current library result first.",
            )
            return
        started,active=self.state.begin_long_operation(
            "dataset",
            f"Previewing automatic caption rules for {len(self._paths):,} image(s)",
        )
        if not started:
            show_operation_in_progress(self,active)
            return
        paths=self._paths
        self._retained_family=str(self.family_filter.currentData() or "")
        self._invalidate_preview()
        self._set_busy(
            True,
            "Checking automatic rule effects in the background. No Image "
            "Knowledge or files are being changed…",
        )

        def work(progress,should_cancel):
            engine=active_automation_engine(self.state.knowledge)
            impacts=[]
            errors=[]
            for index,path in enumerate(paths,1):
                if should_cancel():
                    raise InterruptedError()
                try:
                    impacts.extend(engine.preview((path,),automatic_only=True))
                except Exception as exc:
                    errors.append((str(path),str(exc)))
                progress(index,len(paths),path.name)
            return impacts,errors

        task=CancelableProgressCallTask(work)
        self._task=task
        task.signals.progress.connect(self._progress_changed)
        task.signals.complete.connect(self._preview_ready)
        task.signals.cancelled.connect(self._preview_cancelled)
        task.signals.failed.connect(self._preview_failed)
        self.pool.start(task)

    def _progress_changed(self,current,total,name):
        self.state.update_long_operation("dataset",current,total,name)
        set_progress_bar(self.progress,current,total,detail=name)
        self.status.setText(
            f"Checking automatic caption rules: {current:,} of {total:,} • {name}"
        )

    def _finish_task(self):
        self.state.finish_long_operation("dataset")
        self._task=None
        self._set_busy(False)

    def _preview_ready(self,result):
        self._finish_task()
        if self._invalidation_message:
            message=self._invalidation_message
            self._invalidate_preview()
            self._update_scope_text()
            self.status.setText(message)
            self._invalidation_message=""
            self._shutdown_requested=False
            self._notify_background_work_finished()
            return
        self._impacts=list(result[0])
        self._errors=list(result[1])
        self._preview_run=True
        families=sorted({
            rule.split(":",1)[0]
            for impact in self._impacts for rule in impact.rules
        })
        self.family_filter.blockSignals(True)
        self.family_filter.clear()
        self.family_filter.addItem("All rule families","")
        for family in families:
            self.family_filter.addItem(family,family)
        retained=self.family_filter.findData(self._retained_family)
        if retained>=0:
            self.family_filter.setCurrentIndex(retained)
        self.family_filter.blockSignals(False)
        self._page=0
        self._render()
        if self._shutdown_requested:
            self._shutdown_requested=False
            self._notify_background_work_finished()
            return
        error_count=len(self._errors)
        self.status.setText(
            "Review Read Problems, then retry after correcting access or image data."
            if error_count else
            "Preview complete. Review an image’s Tag changes, or continue to the exact TXT export."
        )
        if error_count and not self._impacts:
            self.result_tabs.setCurrentWidget(self.problems_page)
        self._notify_background_work_finished()

    def _preview_cancelled(self):
        self._finish_task()
        if self._invalidation_message:
            message=self._invalidation_message
            self._invalidate_preview()
            self._update_scope_text()
            self.status.setText(message)
            self._invalidation_message=""
        else:
            self.status.setText(
                "Automatic-rule preview cancelled safely. No Image Knowledge or "
                "files were changed."
            )
        self._shutdown_requested=False
        self._notify_background_work_finished()

    def _preview_failed(self,message):
        shutdown_requested=self._shutdown_requested
        self._finish_task()
        invalidation_message=self._invalidation_message
        if invalidation_message:
            self._invalidate_preview()
            self._update_scope_text()
            self.status.setText(invalidation_message)
            self._invalidation_message=""
        else:
            self._preview_failure=str(message)
            self._render()
            self.result_tabs.setCurrentWidget(self.problems_page)
            self.status.setText(
                "Preview stopped. Open Read Problems, correct the problem, then choose Retry Preview."
            )
        self._shutdown_requested=False
        self._notify_background_work_finished()

    def cancel_preview(self):
        task=self._task
        if task is None:
            return
        self.state.request_long_operation_cancel("dataset")
        self.cancel_button.setEnabled(False)
        self.status.setText("Stopping safely after the current image…")
        task.cancel()

    def _filtered_impacts(self):
        threshold=self.threshold.value()
        family=str(self.family_filter.currentData() or "")
        query=self.search.text().strip().casefold()
        return [
            impact for impact in self._impacts
            if impact.confidence>=threshold
            and (not family or any(rule.startswith(family+":") for rule in impact.rules))
            and (
                not query
                or query in (impact.image.name+" "+" ".join(impact.rules)).casefold()
            )
        ]

    def _render(self):
        if not hasattr(self,"results"):
            return
        filtered=self._filtered_impacts()
        filters_active=(self.threshold.value()!=.95
                        or bool(self.family_filter.currentData())
                        or bool(self.search.text().strip()))
        self.clear_filters_button.setVisible(filters_active)
        self.preview_button.setText(
            "Retry Preview" if self._errors or self._preview_failure
            else "Preview Automatic Rule Effects"
        )
        self.result_summary.setVisible(self._preview_run or bool(self._preview_failure))
        self.result_summary.setText(
            "The preview could not finish. Review Read Problems and retry."
            if self._preview_failure else
            f"{len(self._paths)-len(self._errors):,} of {len(self._paths):,} images checked  •  "
            f"{len(self._impacts):,} with rule effects  •  {len(self._errors):,} read problems"
        )
        maximum_page=max(0,(len(filtered)-1)//self.PAGE_SIZE)
        self._page=min(self._page,maximum_page)
        start=self._page*self.PAGE_SIZE
        shown=filtered[start:start+self.PAGE_SIZE]
        self.results.clear()
        self._impact_by_path={str(impact.image):impact for impact in shown}
        for impact in shown:
            removed=len(set(impact.before)-set(impact.after))
            added=len(set(impact.after)-set(impact.before))
            effect=(
                ("Conflict review • " if impact.warnings else "")
                +f"−{removed} / +{added}"
            )
            item=QTreeWidgetItem((impact.image.name,f"{impact.confidence:.0%}",effect))
            item.setData(0,Qt.UserRole,str(impact.image))
            item.setToolTip(0,"\n".join([*impact.rules,*impact.warnings]))
            self.results.addTopLevelItem(item)
        self.splitter.setVisible(bool(shown))
        self.empty.setVisible(not shown)
        if not shown:
            self.empty.setText(
                "Rule effects were found, but none match the current filters. "
                "Choose Customize Filters to adjust minimum rule confidence or search."
                if self._preview_run and self._impacts else
                "No automatic rule changes were found in the checked images. "
                "Review Read Problems for any images that could not be checked."
                if self._preview_run and self._errors else
                "No automatic rule changes were found. You can still review "
                "the exact training TXT export for this scope."
                if self._preview_run else
                "Choose Preview Automatic Rule Effects to inspect this exact "
                "Dataset scope without changing Image Knowledge or files."
            )
            self.protected_preview.clear("No image selected")
            self.preview_caption.clear()
            self.detail.clear()
        else:
            self.results.setCurrentItem(self.results.topLevelItem(0))
        self.previous_button.setEnabled(self._page>0)
        self.next_button.setEnabled(self._page<maximum_page)
        self.page_label.setText(
            f"{start+1:,}–{start+len(shown):,} of {len(filtered):,}"
            if shown else "0 of 0"
        )
        self._render_read_problems()

    def _clear_view_filters(self):
        for control in (self.threshold,self.family_filter,self.search):
            control.blockSignals(True)
        self.threshold.setValue(.95)
        self.family_filter.setCurrentIndex(0)
        self.search.clear()
        for control in (self.threshold,self.family_filter,self.search):
            control.blockSignals(False)
        self._reset_page()

    def _render_read_problems(self):
        problems=([("",self._preview_failure)] if self._preview_failure else self._errors)
        self.result_tabs.setTabVisible(1,bool(problems))
        self.result_tabs.setTabText(1,f"Read Problems ({len(problems):,})")
        maximum=max(0,(len(problems)-1)//self.PAGE_SIZE)
        self._error_page=min(self._error_page,maximum)
        start=self._error_page*self.PAGE_SIZE
        shown=problems[start:start+self.PAGE_SIZE]
        self.errors_tree.clear()
        self.error_detail.clear()
        self.error_edit_button.setEnabled(False)
        for path,message in shown:
            item=QTreeWidgetItem((Path(path).name if path else "Preview could not start",
                                  str(message).splitlines()[0] if message else "Read failed"))
            item.setData(0,Qt.UserRole,(path,message))
            self.errors_tree.addTopLevelItem(item)
        self.error_previous_button.setEnabled(self._error_page>0)
        self.error_next_button.setEnabled(self._error_page<maximum)
        self.error_page_label.setText(
            f"{start+1:,}–{start+len(shown):,} of {len(problems):,}" if shown else "0 of 0"
        )
        if shown:
            self.errors_tree.setCurrentItem(self.errors_tree.topLevelItem(0))

    def _show_read_problem(self):
        item=self.errors_tree.currentItem()
        if item is None:return
        path,message=item.data(0,Qt.UserRole)
        self.error_detail.setPlainText(
            (f"Image\n{path}\n\n" if path else "")+f"Read problem\n{message}\n\n"
            "Restore access to the image and its saved data, or use the image editor "
            "to inspect and correct it. Retry Preview checks the original scope again."
        )
        self.error_edit_button.setEnabled(
            not self._scope_contextual and self._task is None
            and any(str(value).casefold()==str(path).casefold() for value in self._paths)
        )

    def _edit_read_problem(self):
        item=self.errors_tree.currentItem()
        if item is not None and self.error_edit_button.isEnabled():
            self.edit_image_data.emit(str(item.data(0,Qt.UserRole)[0]))

    def _change_error_page(self,amount):
        self._error_page=max(0,self._error_page+int(amount))
        self._render_read_problems()

    def _reset_page(self,*_):
        self._page=0
        self._render()

    def _change_page(self,amount):
        self._page=max(0,self._page+int(amount))
        self._render()

    def _current_impact(self):
        item=self.results.currentItem()
        return (
            self._impact_by_path.get(str(item.data(0,Qt.UserRole)))
            if item is not None else None
        )

    def _show_current(self):
        impact=self._current_impact()
        if impact is None:
            return
        self.protected_preview.policy=content_safety_policy()
        self.protected_preview.set_path(impact.image)
        self._update_preview_caption()
        warnings="\n".join(impact.warnings) if impact.warnings else "None"
        self.detail.setPlainText(
            "Complete confirmed Image Knowledge\n"+", ".join(impact.before)
            +"\n\nAutomatic-rule projection\n"+", ".join(impact.after)
            +"\n\nActive automatic rules\n"+"\n".join(impact.rules)
            +"\n\nPossible conflicts\n"+warnings
            +"\n\nThe final Training TXT export is recomputed from current "
            "Image Knowledge using the selected versioned Dataset profile."
        )

    def _update_preview_caption(self):
        path=self.protected_preview.path
        if path is None:
            self.preview_caption.clear()
            return
        rating=self.protected_preview.rating
        label=(
            "Checking content rating" if self.protected_preview.checking else
            "No dependable rating yet" if rating=="unknown" else rating.title()
        )
        self.preview_caption.setText(
            f"{Path(path).name}  •  Sensitivity: {label}"
        )

    def _edit_current(self):
        impact=self._current_impact()
        if impact is not None and not self._scope_contextual:
            self.edit_image_data.emit(str(impact.image))

    def _request_export(self):
        if self._task is None and self._paths and not self._scope_contextual:
            self.export_requested.emit(tuple(self._paths))
