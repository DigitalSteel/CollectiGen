from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThreadPool,Qt,Signal,QTimer
from PySide6.QtGui import QKeySequence,QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,QCheckBox,QComboBox,QDialog,QGridLayout,QGroupBox,QHBoxLayout,
    QHeaderView,QLabel,QLineEdit,QMessageBox,QProgressBar,QPushButton,QSplitter,QTreeWidget,
    QTreeWidgetItem,QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core import (
    CHARACTER_ROLES,GUIDED_REVIEW_TOPICS,GuidedReviewChoice,GuidedReviewService,
    GuidedReviewStaleError,ImageRecord,OperationJournal,
)
from core.paths import USER_DATA_DIR
from .components import set_progress_bar,show_operation_in_progress
from .gallery import EmbeddedGallery
from .modal_feedback import FeedbackEnabledDialog
from .workers import CancelableProgressCallTask


class GuidedTagReviewDialog(FeedbackEnabledDialog):
    """One reusable rapid-review surface for missing Image Knowledge topics."""

    feedback_workflow = "Guided tag review"

    data_changed=Signal()
    edit_image_data=Signal(str)
    open_dataset_image=Signal(str)
    force_ai_requested=Signal(object,str)
    background_work_finished=Signal()

    PAGE_SIZE=500

    def __init__(self,state,categories,characters,parent=None,*,history_operation_factory=None,history_opener=None):
        super().__init__(parent)
        self.state=state; self.categories=categories; self.characters=characters
        self._history_operation_factory=history_operation_factory; self._history_opener=history_opener
        self._history_target=None; self._held_images=set(); self._close_requested=False
        self.service=GuidedReviewService(
            state.knowledge,categories,characters,
            OperationJournal(USER_DATA_DIR/"operation_journal.jsonl"),
        )
        self.pool=QThreadPool.globalInstance(); self._scan_task=None
        self.entries=[]; self.position=0; self._last_result=None; self._last_entry=None
        self._queue_built=False
        self._choice_timer=QTimer(self); self._choice_timer.setSingleShot(True); self._choice_timer.setInterval(140)
        self._choice_timer.timeout.connect(self.refresh_choices)
        self.setWindowTitle("Guided Tag Review")
        self.resize(1240,790); self.setMinimumSize(980,650)
        self._build(); self._wire_shortcuts(); self._show_current()

    def _build(self):
        root=QVBoxLayout(self); root.setContentsMargins(18,16,18,16); root.setSpacing(10)
        title=QLabel("Guided Tag Review"); title.setObjectName("pageTitle"); root.addWidget(title)
        intro=QLabel(
            "1. Choose a topic and scope.  2. Build the queue.  "
            "3. Review one image at a time and record one decision. "
            "Visual Style is optional. Saved decisions update Image Knowledge and its Training TXT projection."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel"); root.addWidget(intro)

        controls=QHBoxLayout(); controls.addWidget(QLabel("Topic")); self.topic=QComboBox()
        for item in GUIDED_REVIEW_TOPICS:
            label=item.label+(" (Optional)" if item.optional else "")
            self.topic.addItem(label,item.id)
        controls.addWidget(self.topic,1); controls.addWidget(QLabel("Scope")); self.scope=QComboBox()
        self.scope.addItem("Whole Library","library")
        current=Path(str(self.state.selected_image or "")) if self.state.selected_image else None
        self.scope.addItem("Current Image","current")
        current_index=self.scope.count()-1
        current_available=bool(current and current in set(self.state.dataset.images))
        model_item=self.scope.model().item(current_index)
        if model_item is not None: model_item.setEnabled(current_available)
        self.include_deferred=QCheckBox("Include Deferred"); self.include_deferred.setToolTip("Show topic decisions previously set aside for later.")
        self.build_queue=QPushButton("Build Queue"); self.build_queue.setProperty("primary",True); self.build_queue.clicked.connect(self.start_scan)
        controls.addWidget(self.scope); controls.addWidget(self.include_deferred); controls.addWidget(self.build_queue); root.addLayout(controls)

        progress_row=QHBoxLayout(); self.progress=QProgressBar(); self.progress.hide()
        self.progress_text=QLabel(); self.progress_text.setObjectName("mutedLabel"); self.progress_text.hide()
        self.cancel=QPushButton("Cancel Safely"); self.cancel.clicked.connect(self.cancel_scan); self.cancel.hide()
        progress_row.addWidget(self.progress,1); progress_row.addWidget(self.progress_text); progress_row.addWidget(self.cancel); root.addLayout(progress_row)
        from .components import bind_status_footer
        bind_status_footer(self,self.progress_text,self.progress,self.cancel,title="Guided review")
        self.status=QLabel("Build a queue to find images missing the selected topic."); self.status.setWordWrap(True); self.status.setObjectName("mutedLabel"); root.addWidget(self.status)

        self.empty_panel=QGroupBox("Start a Guided Review")
        empty_layout=QVBoxLayout(self.empty_panel)
        self.empty_message=QLabel(
            "Choose the missing information you want to review, select a scope, "
            "then choose Build Queue. Building a queue is read-only."
        )
        self.empty_message.setWordWrap(True)
        self.empty_message.setAlignment(Qt.AlignCenter)
        self.empty_message.setObjectName("mutedLabel")
        empty_layout.addStretch(1); empty_layout.addWidget(self.empty_message)
        empty_layout.addStretch(1); root.addWidget(self.empty_panel,1)

        splitter=QSplitter(Qt.Horizontal); self.review_workspace=splitter
        root.addWidget(splitter,1)
        image_panel=QWidget(); image_layout=QVBoxLayout(image_panel); image_layout.setContentsMargins(0,0,8,0)
        self.gallery=EmbeddedGallery(page_size=1,card_width=320,show_open_dataset=True)
        self.gallery.edit_requested.connect(self.open_editor); self.gallery.open_dataset_requested.connect(self.open_dataset)
        self.gallery.force_ai_requested.connect(self.force_ai_requested)
        image_layout.addWidget(self.gallery,1); splitter.addWidget(image_panel)

        decision=QWidget(); layout=QVBoxLayout(decision); layout.setContentsMargins(8,0,0,0); layout.setSpacing(8)
        self.queue_position=QLabel("No queue loaded"); self.queue_position.setObjectName("sectionTitle"); layout.addWidget(self.queue_position)
        self.image_context=QLabel(); self.image_context.setWordWrap(True); self.image_context.setObjectName("mutedLabel"); layout.addWidget(self.image_context)
        self.assignments=QLabel(); self.assignments.setWordWrap(True); self.assignments.setObjectName("mutedLabel"); layout.addWidget(self.assignments)

        proposed=QGroupBox("Proposed Findings for This Image"); self.proposed_panel=proposed
        proposed_layout=QVBoxLayout(proposed)
        proposed_help=QLabel("Reject Proposed records only the selected finding as rejected for this image. It never creates a broad absence assertion.")
        proposed_help.setWordWrap(True); proposed_help.setObjectName("mutedLabel"); proposed_layout.addWidget(proposed_help)
        self.candidates=QTreeWidget(); self.candidates.setHeaderLabels(("Proposed Value","Source","Confidence")); self.candidates.setRootIsDecorated(False)
        self.candidates.setSelectionMode(QAbstractItemView.SingleSelection); self.candidates.setMaximumHeight(150)
        self.candidates.header().setSectionResizeMode(0,QHeaderView.ResizeToContents); self.candidates.header().setSectionResizeMode(1,QHeaderView.Stretch); self.candidates.header().setSectionResizeMode(2,QHeaderView.ResizeToContents)
        self.candidates.itemSelectionChanged.connect(self._update_actions); proposed_layout.addWidget(self.candidates)
        layout.addWidget(proposed)

        known=QGroupBox("Known Values in This Topic"); self.known_panel=known
        known_layout=QVBoxLayout(known)
        self.choice_search=QLineEdit(); self.choice_search.setPlaceholderText("Search this topic in System Knowledge…")
        self.choice_search.setAccessibleDescription("Type any part of a known value, alias, or Character Source. Results stay scoped to the selected review topic.")
        self.choice_search.textChanged.connect(lambda *_:self._choice_timer.start()); known_layout.addWidget(self.choice_search)
        self.choice_count=QLabel(); self.choice_count.setObjectName("mutedLabel")
        self.choice_count.setWordWrap(True); known_layout.addWidget(self.choice_count)
        self.choice_results=QTreeWidget(); self.choice_results.setHeaderLabels(("Known Value","Context")); self.choice_results.setRootIsDecorated(False)
        self.choice_results.setSelectionMode(QAbstractItemView.SingleSelection); self.choice_results.setMinimumHeight(155)
        self.choice_results.header().setSectionResizeMode(0,QHeaderView.ResizeToContents); self.choice_results.header().setSectionResizeMode(1,QHeaderView.Stretch)
        self.choice_results.itemDoubleClicked.connect(lambda *_:self.apply_selected()); self.choice_results.itemSelectionChanged.connect(self._choice_selection_changed)
        known_layout.addWidget(self.choice_results,1)
        owner_row=QHBoxLayout(); self.outfit_owner_label=QLabel("Assign outfit to"); self.outfit_owner=QComboBox(); self.outfit_owner.setAccessibleName("Outfit owner")
        self.outfit_owner.currentIndexChanged.connect(self._update_actions); owner_row.addWidget(self.outfit_owner_label); owner_row.addWidget(self.outfit_owner,1); known_layout.addLayout(owner_row)
        self.outfit_owner_label.hide(); self.outfit_owner.hide()
        layout.addWidget(known,1)

        self.role_panel=QGroupBox("Character Identity Type"); role_layout=QGridLayout(self.role_panel)
        role_help=QLabel("Choose these only from your knowledge of the image. CollectiGen never infers originality from pixels.")
        role_help.setWordWrap(True); role_help.setObjectName("mutedLabel"); role_layout.addWidget(role_help,0,0,1,2)
        self.character_role=QComboBox()
        for value,label in CHARACTER_ROLES.items(): self.character_role.addItem(label,value)
        self.apply_role=QPushButton("Use Identity Type"); self.apply_role.clicked.connect(self.apply_character_role)
        role_layout.addWidget(self.character_role,1,0); role_layout.addWidget(self.apply_role,1,1); layout.addWidget(self.role_panel)

        actions=QHBoxLayout(); self.undo=QPushButton("Review Saved Changes…"); self.undo.clicked.connect(self.undo_last); self.undo.setEnabled(False)
        self.reject_button=QPushButton("Reject Proposed"); self.reject_button.clicked.connect(self.reject_proposed)
        self.no_value=QPushButton("No Applicable Value"); self.no_value.clicked.connect(self.confirm_absent)
        self.defer_button=QPushButton("Defer"); self.defer_button.clicked.connect(self.defer_current)
        self.editor=QPushButton("Open Editor"); self.editor.clicked.connect(self.open_editor)
        self.apply=QPushButton("Apply Selected"); self.apply.setProperty("primary",True); self.apply.clicked.connect(self.apply_selected)
        for button in (self.undo,self.reject_button,self.no_value,self.defer_button,self.editor,self.apply): button.setProperty("compact",True)
        actions.addWidget(QLabel("Record a decision")); actions.addStretch(1)
        actions.addWidget(self.no_value); actions.addWidget(self.defer_button); actions.addWidget(self.apply); layout.addLayout(actions)
        secondary_actions=QHBoxLayout()
        secondary_actions.addStretch(1); secondary_actions.addWidget(self.reject_button)
        secondary_actions.addWidget(self.editor); layout.addLayout(secondary_actions)
        nav=QHBoxLayout(); self.previous=QPushButton("Previous"); self.previous.clicked.connect(lambda:self.move_current(-1))
        self.next=QPushButton("Next"); self.next.clicked.connect(lambda:self.move_current(1)); self.page_label=QLabel(); self.page_label.setAlignment(Qt.AlignCenter)
        nav.addWidget(self.previous); nav.addWidget(self.page_label,1); nav.addWidget(self.next); layout.addLayout(nav)
        splitter.addWidget(decision); splitter.setStretchFactor(0,3); splitter.setStretchFactor(1,4)

        close_row=QHBoxLayout(); self.close_button=QPushButton("Close"); self.close_button.clicked.connect(self.request_close); close_row.addWidget(self.undo); close_row.addStretch(1); close_row.addWidget(self.close_button); root.addLayout(close_row)
        self.topic.currentIndexChanged.connect(self._topic_changed)

    def _wire_shortcuts(self):
        for sequence,callback in (
            ("Ctrl+Return",self.apply_selected),("Alt+N",self.confirm_absent),("Alt+R",self.reject_proposed),
            ("Alt+D",self.defer_current),("Alt+E",self.open_editor),
            ("Alt+Left",lambda:self.move_current(-1)),("Alt+Right",lambda:self.move_current(1)),
        ):
            QShortcut(QKeySequence(sequence),self,activated=callback)
        self.apply.setToolTip("Apply the selected known value and advance (Ctrl+Enter).")
        self.no_value.setToolTip("Confirm no value in this topic applies and advance (Alt+N).")
        self.reject_button.setToolTip("Reject only the selected proposed finding and advance (Alt+R).")
        self.defer_button.setToolTip("Set this topic aside for this image and advance (Alt+D).")
        self.editor.setToolTip("Open the universal image-data editor (Alt+E).")
        self.undo.setToolTip("Compare the exact saved decision before restoring selected Image Knowledge. Training TXT bytes are outside history restoration.")

    def _topic_changed(self,*_):
        if self._scan_task is not None:return
        self.entries=[]; self.position=0
        self._queue_built=False; self.choice_results.clear(); self.choice_count.clear()
        self._show_current()

    def _scope_paths(self):
        if self.scope.currentData()=="current":
            value=Path(str(self.state.selected_image or "")) if self.state.selected_image else None
            return [value] if value and value in set(self.state.dataset.images) else []
        return list(self.state.dataset.images)

    def start_scan(self):
        if self._scan_task is not None:return
        paths=self._scope_paths()
        if not paths:
            QMessageBox.information(self,"Open libraries first","Choose or refresh one or more included libraries before building a guided review queue."); return
        topic_label=self.topic.currentText().replace(" (Optional)","")
        started,active=self.state.begin_long_operation("guided_review",f"Building {topic_label} review queue",resource="io",dedupe_key=f"{self.topic.currentData()}:{self.scope.currentData()}")
        if not started:
            show_operation_in_progress(self,active); return
        self.entries=[]; self.position=0; self._queue_built=False
        self.review_workspace.hide(); self.empty_panel.show()
        self.empty_panel.setTitle("Building Queue")
        self._set_scan_busy(True)
        self.empty_message.setText("Building the queue… No Image Knowledge is being changed.")
        self.status.setText(f"Checking {len(paths):,} image(s) for missing {topic_label} information…")
        service=self.service; topic_id=str(self.topic.currentData()); include=self.include_deferred.isChecked()
        task=CancelableProgressCallTask(lambda progress,should_cancel:service.build_queue(
            paths,topic_id,include_deferred=include,progress=progress,should_cancel=should_cancel,
        ))
        self._scan_task=task; task.signals.progress.connect(self._scan_progress); task.signals.complete.connect(self._scan_complete); task.signals.failed.connect(self._scan_failed)
        self.pool.start(task)

    def _set_scan_busy(self,busy: bool):
        for control in (self.topic,self.scope,self.include_deferred,self.build_queue,self.choice_search,self.choice_results,self.candidates,self.apply,self.apply_role,self.no_value,self.reject_button,self.defer_button,self.previous,self.next,self.undo,self.editor,self.gallery,self.character_role,self.outfit_owner): control.setEnabled(not busy)
        self.progress.setVisible(busy); self.progress_text.setVisible(busy); self.cancel.setVisible(busy); self.cancel.setEnabled(busy)
        if busy: set_progress_bar(self.progress,unknown_text="Preparing queue…"); self.progress_text.setText("Preparing queue…")

    def _scan_progress(self,index,total,name):
        self.state.update_long_operation("guided_review",index,total,name); set_progress_bar(self.progress,index,total,detail=name)
        self.progress_text.setText(f"{index:,} of {total:,}: {name}")

    def cancel_scan(self):
        if self._scan_task and hasattr(self._scan_task,"cancel"):
            self.state.request_long_operation_cancel("guided_review"); self._scan_task.cancel(); self.cancel.setEnabled(False); self.progress_text.setText("Stopping safely after the current image…")

    def _scan_complete(self,result):
        self.state.finish_long_operation("guided_review"); self._scan_task=None; self._set_scan_busy(False)
        self.entries=list(result.entries); self.position=0; self._queue_built=True
        scope="current image" if self.scope.currentData()=="current" else f"{result.scanned:,}-image library scope"
        stopped="Stopped early • " if result.cancelled else ""
        self.status.setText(
            f"{stopped}{len(self.entries):,} image(s) need this topic in the {scope} • "
            f"{result.already_complete:,} already complete • {result.deferred:,} deferred • {result.errors:,} unavailable or invalid."
        )
        self._show_current()
        self._notify_background_work_finished()

    def _scan_failed(self,message):
        self.state.finish_long_operation("guided_review"); self._scan_task=None; self._set_scan_busy(False)
        self._queue_built=False
        self.status.setText("The guided queue could not be built. Existing Image Knowledge was unchanged.")
        QMessageBox.critical(self,"Could Not Build Queue",message)
        self._show_current(); self._notify_background_work_finished()

    def refresh_choices(self):
        if self.current_entry() is None:
            self.choice_results.clear(); self.choice_count.clear()
            self.role_panel.hide(); self._update_actions(); return
        topic_id=str(self.topic.currentData() or "character")
        page=self.service.search_choices(topic_id,self.choice_search.text(),limit=100)
        self.choice_results.clear()
        for choice in page.choices:
            item=QTreeWidgetItem((choice.label,choice.detail)); item.setData(0,Qt.UserRole,(choice.kind,choice.stable_id,choice.label,choice.detail)); item.setToolTip(0,choice.label); item.setToolTip(1,choice.detail)
            self.choice_results.addTopLevelItem(item)
        if self.choice_results.topLevelItemCount(): self.choice_results.setCurrentItem(self.choice_results.topLevelItem(0))
        shown=len(page.choices); self.choice_count.setText(f"Showing {shown:,} of {page.total:,} matching known value(s) in this topic." if shown<page.total else f"{page.total:,} matching known value(s) in this topic.")
        self.role_panel.setVisible(topic_id=="character"); self._update_actions()

    def _choice_selection_changed(self):
        entry=self.current_entry(); choice=self.selected_choice(); is_outfit=bool(entry and choice and choice.kind=="outfit")
        self.outfit_owner.clear()
        if is_outfit:
            for value,label in self.service.outfit_owner_choices(entry.image,choice.stable_id): self.outfit_owner.addItem(label,value)
            if not self.outfit_owner.count(): self.outfit_owner.addItem("Open the editor to assign an owner","")
        self.outfit_owner_label.setVisible(is_outfit); self.outfit_owner.setVisible(is_outfit); self._update_actions()

    def current_entry(self):
        return self.entries[self.position] if 0<=self.position<len(self.entries) else None

    def _show_current(self):
        entry=self.current_entry(); total=len(self.entries)
        if entry is None:
            self.review_workspace.hide(); self.empty_panel.show()
            self.empty_panel.setTitle(
                "Queue Complete" if self._queue_built else "Start a Guided Review"
            )
            self.empty_message.setText(
                "No images remain in this queue. Choose another topic or rebuild "
                "the queue to check again."
                if self._queue_built else
                "Choose the missing information you want to review, select a scope, "
                "then choose Build Queue. Building a queue is read-only."
            )
            self.queue_position.setText("No queue item selected")
            self.image_context.setText("Build a queue or choose another topic."); self.assignments.clear(); self.candidates.clear()
            self.gallery.set_unavailable("No image is active in this guided queue.",context="Current image")
            self.page_label.setText("0 of 0"); self.previous.setEnabled(False); self.next.setEnabled(False); self._update_actions(); return
        self.empty_panel.hide(); self.review_workspace.show()
        topic_label=next(item.label for item in GUIDED_REVIEW_TOPICS if item.id==entry.topic_id)
        page=self.position//self.PAGE_SIZE+1; pages=max(1,(total-1)//self.PAGE_SIZE+1)
        self.queue_position.setText(f"Image {self.position+1:,} of {total:,} • {topic_label}")
        self.image_context.setText(f"{entry.image.name} • Queue page {page:,} of {pages:,} (up to {self.PAGE_SIZE:,} decisions per page)")
        self.assignments.setText("Existing character assignments: "+("; ".join(entry.existing_assignments) if entry.existing_assignments else "None"))
        self.assignments.setVisible(entry.topic_id=="character")
        self.gallery.set_paths([entry.image],total_count=1,context="Current image")
        if not self.choice_results.topLevelItemCount():
            self.refresh_choices()
        self.candidates.clear()
        for index,candidate in enumerate(entry.candidates):
            confidence=f"{candidate.confidence:.0%}" if candidate.confidence is not None else "Not scored"
            item=QTreeWidgetItem((candidate.value,candidate.source,confidence)); item.setData(0,Qt.UserRole,index); self.candidates.addTopLevelItem(item)
        if self.candidates.topLevelItemCount():
            self.candidates.setCurrentItem(self.candidates.topLevelItem(0))
            candidate=entry.candidates[0]
            for row in range(self.choice_results.topLevelItemCount()):
                item=self.choice_results.topLevelItem(row); data=item.data(0,Qt.UserRole)
                if data and data[1]==candidate.stable_id: self.choice_results.setCurrentItem(item); break
        self.proposed_panel.setVisible(bool(entry.candidates))
        self.page_label.setText(f"{self.position+1:,} of {total:,}"); self.previous.setEnabled(self.position>0); self.next.setEnabled(self.position+1<total)
        self._choice_selection_changed()

    def _update_actions(self):
        entry=self.current_entry(); idle=self._scan_task is None
        held=entry is not None and entry.image in self._held_images
        active=entry is not None and idle and not held
        choice=self.selected_choice()
        owner_ready=not choice or choice.kind!="outfit" or bool(self.outfit_owner.currentData())
        self.apply.setEnabled(active and choice is not None and owner_ready)
        self.apply_role.setEnabled(active and str(self.topic.currentData())=="character")
        self.reject_button.setEnabled(active and self.candidates.currentItem() is not None)
        self.no_value.setEnabled(active); self.defer_button.setEnabled(active); self.editor.setEnabled(entry is not None and idle)
        self.editor.setText("Review Image Recovery…" if held else "Edit Image Data…")
        self.undo.setEnabled(idle and self._history_target is not None)
        self.previous.setEnabled(idle and entry is not None and self.position>0)
        self.next.setEnabled(idle and entry is not None and self.position+1<len(self.entries))

    def selected_choice(self):
        item=self.choice_results.currentItem()
        if item is None: return None
        data=item.data(0,Qt.UserRole)
        return GuidedReviewChoice(*data) if data else None

    def selected_candidate(self):
        entry=self.current_entry(); item=self.candidates.currentItem()
        if entry is None or item is None: return None
        index=int(item.data(0,Qt.UserRole)); return entry.candidates[index] if 0<=index<len(entry.candidates) else None

    def _decision_complete(self,result,entry):
        self._last_result=result; self._last_entry=entry
        operation_id=getattr(result,"history_operation_id","") or result.operation_id
        if operation_id:self._history_target=((entry.image,),operation_id)
        if getattr(result,"status","complete")!="complete":
            if result.status in {"interrupted","projection_failed"}:self._held_images.add(entry.image)
            next_action=(
                " Review Saved Changes retains this exact operation; open this image in the editor for recovery, then rebuild the queue."
                if result.status in {"interrupted","projection_failed"} else
                " Rebuild the queue to review the current facts." if result.status=="stale" else
                " Review the history configuration before retrying." if result.status=="held" else
                " The current queue item remains available."
            )
            self.status.setText(result.error+next_action)
            self._update_actions()
            return
        refresh_error=""
        if self.state.dataset.index:
            try:self.state.dataset.cache_managed_record(entry.image,ImageRecord(entry.image))
            except Exception as exc:refresh_error=str(exc)
        self.state.events.publish("image_data_changed",image=str(entry.image)); self.state.events.publish("reviews_changed"); self.data_changed.emit()
        self.entries=[item for item in self.entries if not (item.image==entry.image and item.topic_id==entry.topic_id)]
        if self.position>=len(self.entries): self.position=max(0,len(self.entries)-1)
        self._show_current()
        self.status.setText("Image decision and Training TXT saved. Review Saved Changes compares the exact saved Image Knowledge." +
            (" Library refresh needs attention: "+refresh_error if refresh_error else ""))

    def _apply(self,callback):
        entry=self.current_entry()
        if entry is None or self._scan_task is not None or entry.image in self._held_images:return
        try:operation=self._history_operation_factory((entry.image,)) if self._history_operation_factory else None
        except Exception as exc:
            QMessageBox.warning(self,"Review Saved History Configuration",str(exc)); return
        if operation is not None:
            started,active=self.state.begin_long_operation("guided_review","Saving guided image decision",resource="io")
            if not started:show_operation_in_progress(self,active); return
            self._history_target=((entry.image,),operation.operation_id)
            task=CancelableProgressCallTask(lambda progress,cancelled:callback(entry,history_operation=operation,should_cancel=cancelled))
            self._scan_task=task; self._choice_timer.stop(); self._set_scan_busy(True)
            self.progress_text.setText("Saving this image decision and its Training TXT projection…")
            task.signals.complete.connect(lambda result:self._apply_complete(result,entry))
            task.signals.failed.connect(self._apply_failed)
            task.signals.cancelled.connect(lambda:self._apply_failed("Stopped before this decision completed."))
            self.pool.start(task); return
        try: result=callback(entry)
        except GuidedReviewStaleError as exc:
            QMessageBox.warning(self,"Refresh This Queue Item",str(exc)); self.refresh_current(); return
        except Exception as exc:
            QMessageBox.critical(self,"Could Not Complete Guided Decision",str(exc)); return
        self._decision_complete(result,entry)

    def _apply_complete(self,result,entry):
        self.state.finish_long_operation("guided_review"); self._scan_task=None; self._set_scan_busy(False)
        try:self._decision_complete(result,entry)
        finally:self._notify_background_work_finished()

    def _apply_failed(self,message):
        self.state.finish_long_operation("guided_review"); self._scan_task=None; self._set_scan_busy(False)
        self.status.setText(message+" Review the exact saved changes before retrying.")
        self._update_actions(); self._notify_background_work_finished()

    def apply_selected(self):
        if self._scan_task is not None:return
        entry=self.current_entry(); choice=self.selected_choice()
        if entry is None or choice is None:return
        warnings=self.service.preview_conflicts(entry,choice)
        if warnings:
            message="This explicit addition introduces a current System Knowledge conflict:\n\n"+"\n".join(f"• {value}" for value in warnings)+"\n\nApply it anyway to this image?"
            if QMessageBox.question(self,"Review Tag Conflict",message)!=QMessageBox.Yes:return
        owner=str(self.outfit_owner.currentData() or "") if choice.kind=="outfit" else ""
        self._apply(lambda current,**options:self.service.apply_choice(current,choice,outfit_owner=owner,**options))

    def apply_character_role(self):
        role=str(self.character_role.currentData() or "")
        self._apply(lambda entry,**options:self.service.assign_character_role(entry,role,**options))

    def confirm_absent(self):
        entry=self.current_entry()
        if entry is None or self._scan_task is not None:return
        topic=self.topic.currentText().replace(" (Optional)","")
        message=(f"Confirm that no {topic} value applies to this image?\n\n"
                 "This records a topic-level absence in Image Knowledge and updates Training TXT. It does not reject any proposed finding. Use Review Saved Changes to compare recorded Image Knowledge before restoring it.")
        if QMessageBox.question(self,"Confirm No Applicable Value",message)==QMessageBox.Yes:
            self._apply(self.service.confirm_absent)

    def reject_proposed(self):
        candidate=self.selected_candidate()
        if candidate is None:return
        self._apply(lambda entry,**options:self.service.reject_candidate(entry,candidate,**options))

    def defer_current(self):
        self._apply(self.service.defer)

    def open_editor(self,*_):
        if self._scan_task is not None:return
        entry=self.current_entry()
        if entry is None:return
        self.edit_image_data.emit(str(entry.image))
        if entry.image in self._held_images:
            self.accept()
            return
        self.refresh_current(advance_if_complete=True)

    def open_dataset(self):
        if self._scan_task is not None:return
        entry=self.current_entry()
        if entry is not None:self.open_dataset_image.emit(str(entry.image))

    def refresh_current(self,*,advance_if_complete=False):
        entry=self.current_entry()
        if entry is None:return
        try: refreshed=self.service.entry_for_image(entry.image,entry.topic_id,include_deferred=self.include_deferred.isChecked())
        except Exception as exc:
            QMessageBox.warning(self,"Could Not Refresh Item",str(exc)); return
        if refreshed is None and advance_if_complete:
            self.entries.pop(self.position)
            if self.position>=len(self.entries):self.position=max(0,len(self.entries)-1)
        elif refreshed is not None:
            self.entries[self.position]=refreshed
        self._show_current()

    def move_current(self,amount):
        if not self.entries or self._scan_task is not None:return
        self.position=min(len(self.entries)-1,max(0,self.position+int(amount))); self._show_current()

    def undo_last(self):
        if self._scan_task is not None:return
        if self._history_target is None or self._history_opener is None:
            QMessageBox.information(self,"Saved Changes Unavailable","There is no proven saved-history route for this decision. Older untracked changes cannot be restored from an in-memory snapshot."); return
        images,operation_id=self._history_target
        try:opened=self._history_opener(images,operation_id)
        except Exception as exc:
            QMessageBox.warning(self,"Could Not Open Saved Changes",str(exc)); return
        if opened is not None:self.accept()

    def _notify_background_work_finished(self):
        if self._scan_task is None:
            if self._close_requested:super().reject()
            self.background_work_finished.emit()

    def reject(self):
        if self._scan_task and hasattr(self._scan_task,"cancel"):
            self._close_requested=True
            self.cancel_scan()
            return
        super().reject()

    def request_close(self):
        self.reject()
