from __future__ import annotations

import re

from PySide6.QtCore import QThreadPool,QTimer,Signal,Qt
from PySide6.QtWidgets import (
    QHBoxLayout,QHeaderView,QLabel,QMessageBox,QProgressBar,QPushButton,
    QSplitter,QTextEdit,QTreeWidgetItem,QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core import DownstreamUpdateStore,ImageIdentityCorrectionService,OperationJournal
from core.paths import USER_DATA_DIR

from .accessibility import announce_accessibly
from .components import (
    RangeCheckTreeWidget,set_progress_bar,show_operation_in_progress,
)
from .tag_identity_review import RemovedVocabularyDialog
from .workers import AsyncCallTask,CancelableProgressCallTask
from .history_workflow import capture_operation, open_history, history_dialogs_busy, close_history_dialogs


class ImageIdentityCorrectionWidget(QWidget):
    """Dataset-owned matching-image maintenance over the shared core service."""

    refresh_requested=Signal()
    edit_image_data=Signal(str)
    knowledge_changed=Signal()
    open_tag_encyclopedia=Signal(str,object)
    downstream_updates_changed=Signal()
    background_work_finished=Signal()
    history_restored=Signal(object)

    def __init__(self,state,parent=None,*,matching_service=None):
        super().__init__(parent)
        self.state=state
        self.settings=state.settings
        self.characters=getattr(parent,"characters",None)
        self._history_correction_target=((),"")
        self._history_correction_interrupted=False
        self._history_apply_paths=()
        self.pool=QThreadPool.globalInstance()
        self._matching_service_instance=matching_service
        self.downstream_updates=DownstreamUpdateStore(
            USER_DATA_DIR/"downstream_updates.json"
        )
        self._matching_task=None
        self._matching_preview=None
        self._matching_by_path={}
        self._matching_scope={}
        self._matching_selected=set()
        self._matching_page=0
        self._matching_page_size=100
        self._matching_syncing=False
        self._matching_busy=False
        self._matching_audit_pending=False
        self._matching_completion_summary=""
        self._matching_audit_retry=QTimer(self)
        self._matching_audit_retry.setSingleShot(True)
        self._matching_audit_retry.setInterval(500)
        self._matching_audit_retry.timeout.connect(self.audit_matching_images)
        self._shutdown_requested=False
        self._matching_removed_ids=()
        self._build_ui()
        if self._matching_checkpoint_path().exists():
            QTimer.singleShot(0,self._recover_matching_correction)

    @staticmethod
    def _matching_checkpoint_path():
        return USER_DATA_DIR/"image_identity_correction_checkpoint.json"

    @classmethod
    def prepare_service(cls,state):
        return ImageIdentityCorrectionService(
            state.knowledge,
            OperationJournal(USER_DATA_DIR/"operation_journal.jsonl"),
            cls._matching_checkpoint_path(),
        )

    def _matching_service(self):
        if self._matching_service_instance is None:
            self._matching_service_instance=self.prepare_service(self.state)
        return self._matching_service_instance

    def refresh_system_knowledge(self):
        """Discard the derived identity index after canonical names change."""
        self._matching_service_instance=None

    def _build_ui(self):
        layout=QVBoxLayout(self)
        layout.setContentsMargins(8,8,8,8)
        heading=QLabel("Link confirmed image Tags to System Knowledge")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        intro=QLabel(
            "Check Matching Images compares already-confirmed image Tags with "
            "current System Knowledge. It can record a missing internal identity "
            "link, replace a reviewed alias display with its preferred name, follow "
            "an explicit duplicate-Tag redirect, and collapse only repeated "
            "occurrences of that same identity. It never adds an image fact or "
            "changes TXT, Metadata, source images, raw observations, file locations, "
            "or separately meaningful Tags."
        )
        intro.setWordWrap(True)
        intro.setObjectName("mutedLabel")
        layout.addWidget(intro)
        self.matching_scope_label=QLabel(
            "Scope: current library filter • all confirmed Tags"
        )
        self.matching_scope_label.setWordWrap(True)
        self.matching_scope_label.setAccessibleName(
            "Matching image correction scope"
        )
        layout.addWidget(self.matching_scope_label)

        actions=QHBoxLayout()
        self.matching_audit_button=QPushButton("Check Matching Images")
        self.matching_audit_button.setProperty("primary",True)
        self.matching_audit_button.clicked.connect(self.audit_matching_images)
        self.matching_include_button=QPushButton("Check All Exact")
        self.matching_include_button.clicked.connect(
            lambda:self._set_matching_shown(True)
        )
        self.matching_exclude_button=QPushButton("Uncheck All Shown")
        self.matching_exclude_button.clicked.connect(
            lambda:self._set_matching_shown(False)
        )
        self.matching_previous=QPushButton("Previous")
        self.matching_previous.clicked.connect(
            lambda:self._change_matching_page(-1)
        )
        self.matching_page_label=QLabel("0 of 0")
        self.matching_next=QPushButton("Next")
        self.matching_next.clicked.connect(lambda:self._change_matching_page(1))
        self.matching_cancel_button=QPushButton("Cancel Safely")
        self.matching_cancel_button.clicked.connect(self.cancel_matching_task)
        self.matching_cancel_button.hide()
        self.matching_return_button=QPushButton("Back to Tag Encyclopedia")
        self.matching_return_button.clicked.connect(
            self._return_from_matching_images
        )
        self.matching_return_button.hide()
        self.matching_apply_button=QPushButton(
            "Apply Checked Link Corrections…"
        )
        self.matching_apply_button.setProperty("primary",True)
        self.matching_apply_button.clicked.connect(
            self.apply_matching_corrections
        )
        self.matching_apply_button.setEnabled(False)
        self.matching_undo_button=QPushButton("Review Correction Changes…")
        self.matching_undo_button.clicked.connect(
            self.undo_matching_correction
        )
        for button in (
            self.matching_audit_button,self.matching_include_button,
            self.matching_exclude_button,self.matching_previous,
            self.matching_next,self.matching_cancel_button,
            self.matching_return_button,self.matching_apply_button,
            self.matching_undo_button,
        ):
            button.setProperty("compact",True)
        actions.addWidget(self.matching_audit_button)
        actions.addWidget(self.matching_include_button)
        actions.addWidget(self.matching_exclude_button)
        actions.addWidget(self.matching_previous)
        actions.addWidget(self.matching_page_label)
        actions.addWidget(self.matching_next)
        actions.addStretch(1)
        layout.addLayout(actions)

        completion_actions=QHBoxLayout()
        completion_actions.addWidget(self.matching_return_button)
        completion_actions.addWidget(self.matching_undo_button)
        completion_actions.addStretch(1)
        completion_actions.addWidget(self.matching_cancel_button)
        completion_actions.addWidget(self.matching_apply_button)
        self.matching_progress=QProgressBar()
        self.matching_progress.hide()
        layout.addWidget(self.matching_progress)
        self.matching_status=QLabel(
            "Choose Check Matching Images to find existing confirmed Tags that "
            "need an exact System Knowledge link or preferred-name update. The "
            "preview is read-only and does not add image facts."
        )
        self.matching_status.setWordWrap(True)
        self.matching_status.setObjectName("mutedLabel")
        self.matching_status.setAccessibleName(
            "Matching image correction status"
        )
        layout.addWidget(self.matching_status)
        from .components import bind_status_footer
        bind_status_footer(self,self.matching_status,self.matching_progress,self.matching_cancel_button,title="Image identity")

        splitter=QSplitter(Qt.Horizontal)
        self.matching_tree=RangeCheckTreeWidget()
        self.matching_tree.setHeaderLabels(
            ("Apply","Image","State","What will change")
        )
        self.matching_tree.setRootIsDecorated(False)
        self.matching_tree.setAlternatingRowColors(False)
        self.matching_tree.setAccessibleName(
            "Matching image correction preview"
        )
        self.matching_tree.header().setSectionResizeMode(
            0,QHeaderView.ResizeToContents
        )
        self.matching_tree.header().setSectionResizeMode(
            1,QHeaderView.ResizeToContents
        )
        self.matching_tree.header().setSectionResizeMode(
            2,QHeaderView.ResizeToContents
        )
        self.matching_tree.header().setSectionResizeMode(3,QHeaderView.Stretch)
        self.matching_tree.itemChanged.connect(self._matching_item_changed)
        self.matching_tree.itemSelectionChanged.connect(
            self._show_matching_detail
        )
        splitter.addWidget(self.matching_tree)
        detail=QWidget()
        detail_layout=QVBoxLayout(detail)
        detail_heading=QLabel("Selected image correction")
        detail_heading.setObjectName("sectionTitle")
        detail_layout.addWidget(detail_heading)
        self.matching_detail=QTextEdit()
        self.matching_detail.setReadOnly(True)
        self.matching_detail.setAccessibleName(
            "Selected matching image correction details"
        )
        detail_layout.addWidget(self.matching_detail,1)
        edit=QPushButton("Edit Image Data…")
        edit.setProperty("compact",True)
        edit.clicked.connect(self._edit_matching_image)
        self.matching_restore_removed=QPushButton("Review Removed Tag…")
        self.matching_restore_removed.setProperty("compact",True)
        self.matching_restore_removed.clicked.connect(
            self._review_matching_removed_tags
        )
        self.matching_restore_removed.hide()
        detail_actions=QHBoxLayout()
        detail_actions.addWidget(edit)
        detail_actions.addWidget(self.matching_restore_removed)
        detail_actions.addStretch(1)
        detail_layout.addLayout(detail_actions)
        splitter.addWidget(detail)
        splitter.setStretchFactor(0,3)
        splitter.setStretchFactor(1,2)
        splitter.setSizes((1050,650))
        layout.addWidget(splitter,1)
        layout.addLayout(completion_actions)
        self._update_matching_undo_state()
        self._render_matching_preview()

    def open_scope(self,scope=None):
        """Open a typed Dataset maintenance scope without starting work."""
        if self.has_active_background_work():
            return
        values=dict(scope) if isinstance(scope,dict) else {}
        start_audit=bool(values.pop("start_audit",False))
        if values==self._matching_scope and self._matching_preview is not None:
            if start_audit:
                self.audit_matching_images()
            return
        self._matching_completion_summary=""
        self._matching_scope=values
        self._matching_preview=None
        self._matching_by_path={}
        self._matching_selected=set()
        self._matching_page=0
        self._render_matching_preview()
        self.matching_return_button.setVisible(
            self._matching_scope.get("origin")=="tag_encyclopedia"
        )
        stable_ids=tuple(
            str(value) for value in self._matching_scope.get("stable_ids",())
            if str(value)
        )
        source_names=tuple(
            str(value) for value in self._matching_scope.get("source_names",())
            if str(value)
        )
        label=str(
            self._matching_scope.get("label")
            or self._matching_scope.get("change_label")
            or ""
        ).strip()
        if stable_ids or source_names:
            identity_count=len(set((*stable_ids,*source_names)))
            self.matching_scope_label.setText(
                "Scope: current library filter • "
                +(
                    label if label else
                    f"{identity_count:,} prefilled System Knowledge "
                    f"identit{'y' if identity_count==1 else 'ies'}"
                )
            )
            self._set_matching_status(
                "Prefilled image Tag update review opened. This "
                "handoff did not start a scan or write data. Choose Check Matching "
                "Images to build a read-only preview of existing confirmed Tags."
            )
        else:
            self.matching_scope_label.setText(
                "Scope: current library filter • all confirmed Tags"
            )
            self._set_matching_status(
                "Choose Check Matching Images to find existing confirmed Tags "
                "that need an exact System Knowledge link or preferred-name "
                "update. The preview is read-only and does not add image facts."
            )
        if start_audit:
            self._set_matching_status(
                "Starting the saved image Tag update check. No Image "
                "Knowledge will change without a separate reviewed confirmation."
            )
            self._queue_matching_audit()

    def _queue_matching_audit(self):
        if self._shutdown_requested:
            return
        self._matching_audit_pending=True
        self._update_matching_apply_state()
        self.matching_undo_button.setEnabled(False)
        self.matching_cancel_button.show()
        self.matching_cancel_button.setEnabled(True)
        self._matching_audit_retry.start(0)

    def _set_matching_status(self,message,*,announce=False):
        self.matching_status.setText(str(message))
        self.matching_status.setAccessibleDescription(str(message))
        if announce:
            announce_accessibly(self.matching_status,message)

    def _matching_set_busy(self,busy,message="",*,cancellable=True,quiet=False):
        if busy:
            started,active=self.state.begin_long_operation(
                "dataset",message or "Checking matching images",
            )
            if not started:
                if not quiet:
                    show_operation_in_progress(self,active)
                return False
        else:
            self.state.finish_long_operation("dataset")
        self._matching_busy=bool(busy)
        self.matching_audit_button.setEnabled(not busy)
        self.matching_tree.setEnabled(not busy)
        self.matching_include_button.setEnabled(not busy)
        self.matching_exclude_button.setEnabled(not busy)
        self.matching_previous.setEnabled(not busy)
        self.matching_next.setEnabled(not busy)
        self.matching_undo_button.setEnabled(
            not busy and bool(self._history_correction_target[0])
        )
        self.matching_progress.setVisible(busy)
        if busy:
            set_progress_bar(
                self.matching_progress,detail=message,unknown_text="Starting…",
            )
        self.matching_cancel_button.setVisible(busy and cancellable)
        self.matching_cancel_button.setEnabled(busy and cancellable)
        if message:
            self._set_matching_status(message)
        if not busy:
            self.matching_progress.hide()
        self._update_matching_apply_state()
        return True

    def _recover_matching_correction(self):
        if (
            self._matching_task is not None
            or not self._matching_checkpoint_path().exists()
        ):
            return
        if not self._matching_set_busy(
            True,"Recovering the interrupted matching-image checkpoint…",
            cancellable=False,
        ):
            return
        service=self._matching_service()
        task=AsyncCallTask(service.recover_interrupted)
        self._matching_task=task
        task.signals.complete.connect(self._matching_recovery_ready)
        task.signals.failed.connect(self._matching_task_failed)
        self.pool.start(task)

    def _matching_recovery_ready(self,recovery):
        self._matching_task=None
        self._matching_set_busy(False)
        if self._shutdown_requested:
            self._notify_background_work_finished()
            return
        if recovery is None:
            self._set_matching_status(
                "No interrupted matching-image correction needed recovery."
            )
        elif recovery.conflicts:
            self._set_matching_status(
                f"Recovery held {len(recovery.conflicts):,} image(s) because "
                "their Image Knowledge differs from both verified snapshots. "
                "No newer work was overwritten.",
                announce=True,
            )
        else:
            self._set_matching_status(
                f"Recovered {recovery.applied:,} verified correction(s); "
                f"{recovery.untouched:,} image(s) had not changed. "
                "Review Correction Changes remains available for applied work.",
                announce=True,
            )
            if recovery.applied:
                self.refresh_requested.emit()
                try:
                    self.downstream_updates.require_identity_recheck()
                except OSError:
                    pass
                else:
                    self.downstream_updates_changed.emit()
        self._update_matching_undo_state()

    def audit_matching_images(self):
        if self._matching_task is not None or self._shutdown_requested:
            return
        self._matching_audit_retry.stop()
        self._matching_audit_pending=True
        if not self._matching_set_busy(
            True,"Building a current matching-image preview without changing files…",
            quiet=True,
        ):
            self._update_matching_apply_state()
            self.matching_undo_button.setEnabled(False)
            self._set_matching_status(
                self._matching_completion_summary
                +" Waiting for current library work to finish; the matching-image "
                "check will start automatically. You can cancel this waiting check.",
            )
            if self._matching_preview is None:
                self.matching_detail.setPlainText(
                    "Check queued. Results will appear here after current library work finishes."
                )
            self.matching_cancel_button.show()
            self.matching_cancel_button.setEnabled(True)
            self._matching_audit_retry.start(500)
            return
        self._matching_audit_pending=False
        dataset=self.state.dataset
        roots=getattr(dataset,"roots",None)
        has_library=bool(
            roots if roots is not None else getattr(dataset,"root",None)
        )
        if not has_library or dataset.index is None:
            self._matching_set_busy(False)
            QMessageBox.information(
                self,"Open libraries first",
                "Choose or refresh one or more image libraries before checking "
                "matching images.",
            )
            self._notify_background_work_finished()
            return
        stable_ids=tuple(
            str(value) for value in self._matching_scope.get("stable_ids",())
            if str(value)
        )
        source_names=tuple(
            str(value) for value in self._matching_scope.get("source_names",())
            if str(value)
        )
        index=dataset.index
        service=self._matching_service()

        def work(progress,should_cancel):
            recovery=service.recover_interrupted()
            if recovery is not None and recovery.conflicts:
                raise RuntimeError(
                    "Interrupted matching-image recovery requires review before "
                    "a new audit: "+"; ".join(recovery.conflicts)
                )
            discovery=service.discover(
                index.all_records(),target_ids=stable_ids,
                source_names=source_names,
            )
            return service.preview(
                discovery.candidate_paths,
                image_count=discovery.image_count,
                target_ids=stable_ids,
                source_names=source_names,
                progress=progress,
                should_cancel=should_cancel,
            )

        task=CancelableProgressCallTask(work)
        self._matching_task=task
        task.signals.progress.connect(self._matching_progress_changed)
        task.signals.complete.connect(self._matching_preview_ready)
        task.signals.cancelled.connect(self._matching_cancelled)
        task.signals.failed.connect(self._matching_task_failed)
        self.pool.start(task)

    def _matching_progress_changed(self,current,total,name):
        self.state.update_long_operation("dataset",current,total,name)
        set_progress_bar(
            self.matching_progress,current,total,detail=name,
            unknown_text="Checking…",
        )
        self._set_matching_status(
            f"{current:,} of {total:,}: {name}"
            if total else "Checking the included library indexes…"
        )

    def _matching_preview_ready(self,preview):
        self._matching_task=None
        self._matching_set_busy(False)
        if self._shutdown_requested:
            self._notify_background_work_finished()
            return
        self._matching_preview=preview
        self._matching_by_path={str(item.image):item for item in preview.items}
        self._matching_selected={
            str(item.image) for item in preview.items
            if item.status=="will_change" and not preview.cancelled
        }
        self._matching_page=0
        self._render_matching_preview()
        if preview.cancelled:
            self._set_matching_status(
                f"Audit stopped safely after {preview.checked_count:,} of "
                f"{preview.candidate_count:,} indexed candidate(s). Partial "
                "read-only results remain visible, but no corrections are checked "
                "for application and no Image Knowledge was changed.",
                announce=True,
            )
        else:
            overlapping=sum(
                item.status=="will_change" and bool(item.warnings)
                for item in preview.items
            )
            held=sum(
                item.status in {
                    "unresolved","unavailable","read_only","failed",
                }
                for item in preview.items
            )
            candidate_label=(
                "candidate" if preview.candidate_count==1 else "candidates"
            )
            indexed_label="image" if preview.image_count==1 else "images"
            exact_label="image" if preview.will_change==1 else "images"
            exact_verb="has" if preview.will_change==1 else "have"
            held_label="image" if held==1 else "images"
            held_verb="is" if held==1 else "are"
            overlap_text=(
                (
                    "That image also contains unresolved work."
                    if overlapping else
                    "That image has no overlapping unresolved work."
                )
                if preview.will_change==1 else
                f"{overlapping:,} of those images also contain unresolved work."
            )
            self._set_matching_status(
                self._matching_completion_summary
                +f"Checked {preview.candidate_count:,} matching {candidate_label} from "
                f"{preview.image_count:,} indexed {indexed_label}. "
                f"{preview.will_change:,} {exact_label} {exact_verb} exact link or "
                f"preferred-name corrections; {overlap_text} {held:,} other "
                f"{held_label} {held_verb} held with no automatic change. These "
                "are existing confirmed Tags, not new image facts.",
                announce=True,
            )
            try:
                self.downstream_updates.record_identity_audit(preview)
            except OSError:
                pass
            else:
                self.downstream_updates_changed.emit()
        self._notify_background_work_finished()

    def _matching_cancelled(self):
        self._matching_task=None
        self._matching_set_busy(False)
        self._set_matching_status(
            "Matching-image audit stopped safely. No Image Knowledge was changed.",
            announce=True,
        )
        self._notify_background_work_finished()

    def _matching_task_failed(self,message):
        self._matching_task=None
        self._matching_set_busy(False)
        self._set_matching_status(
            "Matching-image work stopped without overwriting unresolved or newer data.",
            announce=True,
        )
        if not self._shutdown_requested:
            QMessageBox.critical(self,"Matching-image work stopped",str(message))
        self._update_matching_undo_state()
        self._notify_background_work_finished()

    def _matching_change_groups(self,item):
        groups={}
        for change in item.changes:
            stable_id=str(change.stable_id)
            record=self.state.knowledge.tags.get(
                self.state.knowledge.resolve_tag_id(stable_id) or stable_id
            )
            canonical=(
                record.display if record is not None else str(change.after)
            )
            group=groups.setdefault(stable_id,{
                "stable_id":stable_id,"canonical":canonical,"existing":[],
                "reasons":[],"changes":[],"spelling":None,
            })
            group["reasons"].append(change.reason)
            group["changes"].append(change)
            if change.reason in {
                "canonical_spelling","same_identity_duplicate",
            } and change.before not in group["existing"]:
                group["existing"].append(change.before)
            if change.reason=="canonical_spelling":
                group["spelling"]=(change.before,change.after)
        for group in groups.values():
            if not group["existing"]:
                group["existing"].append(group["canonical"])
        return tuple(groups.values())

    def _matching_summary(self,item):
        if not item.changes:
            return item.message or ("; ".join(item.warnings))
        values=[]
        for group in self._matching_change_groups(item):
            existing=group["existing"][0]
            canonical=group["canonical"]
            spelling=group["spelling"]
            text=(
                f'Link existing "{existing}" to System Knowledge as '
                f'"{canonical}"; visible spelling '
                +("changes" if spelling else "unchanged")
            )
            if "same_identity_duplicate" in group["reasons"]:
                text+="; collapse same-identity duplicate"
            if "stable_id_redirect" in group["reasons"]:
                text+="; follow reviewed identity redirect"
            if text not in values:
                values.append(text)
        summary="; ".join(values[:2])
        if len(values)>2:
            summary+=f"; +{len(values)-2:,} more identities"
        return summary

    def _render_matching_preview(self):
        self._matching_syncing=True
        try:
            self.matching_tree.clear()
            preview=self._matching_preview
            items=list(preview.items) if preview is not None else []
            maximum=max(0,(len(items)-1)//self._matching_page_size)
            self._matching_page=min(self._matching_page,maximum)
            start=self._matching_page*self._matching_page_size
            shown=items[start:start+self._matching_page_size]
            has_results=bool(items)
            for widget in (
                self.matching_include_button,self.matching_exclude_button,
                self.matching_previous,self.matching_page_label,
                self.matching_next,self.matching_apply_button,
            ):
                widget.setVisible(has_results)
            labels={
                "will_change":"Exact link available",
                "unresolved":"Needs review",
                "unavailable":"Unavailable",
                "read_only":"Read-only",
                "failed":"Could not inspect",
            }
            for item in shown:
                key=str(item.image)
                state_label=labels.get(item.status,item.status)
                if item.status=="will_change" and item.warnings:
                    state_label="Exact link + review note"
                row=QTreeWidgetItem((
                    "",item.image.name,state_label,
                    self._matching_summary(item),
                ))
                row.setData(0,Qt.UserRole,key)
                row.setToolTip(1,str(item.image))
                if item.status=="will_change":
                    row.setFlags(row.flags()|Qt.ItemIsUserCheckable)
                    row.setCheckState(
                        0,Qt.Checked if key in self._matching_selected
                        else Qt.Unchecked,
                    )
                else:
                    row.setFlags(row.flags()&~Qt.ItemIsUserCheckable)
                self.matching_tree.addTopLevelItem(row)
            self.matching_previous.setEnabled(self._matching_page>0)
            self.matching_next.setEnabled(self._matching_page<maximum)
            self.matching_page_label.setText(
                f"{start+1:,}–{start+len(shown):,} of {len(items):,}"
                if shown else "0 of 0"
            )
            self.matching_include_button.setEnabled(
                any(item.status=="will_change" for item in shown)
            )
            self.matching_exclude_button.setEnabled(
                any(item.status=="will_change" for item in shown)
            )
            if shown:
                self.matching_tree.setCurrentItem(
                    self.matching_tree.topLevelItem(0)
                )
            else:
                self.matching_restore_removed.hide()
                self.matching_detail.setPlainText(
                    "Saved history needs recovery. An image may already be saved. "
                    "Use Review Correction Changes to open the exact affected image's recovery before another correction."
                    if self._history_correction_interrupted else
                    (
                        "Review Correction Changes opens exact saved history when available. "
                        "Older corrections without proven history cannot be restored here. "
                        "Check Matching Images to inspect current links."
                        if self.downstream_updates.load().recovery_available else
                        "Choose Check Matching Images to build a read-only preview."
                    )
                    if preview is None else
                    "The check was cancelled. No complete result is available; "
                    "choose Check Matching Images to try again."
                    if preview.cancelled else
                    "All matching confirmed Tags already use current System "
                    "Knowledge links and preferred names."
                )
        finally:
            self._matching_syncing=False
        self._update_matching_apply_state()

    def _matching_item_changed(self,item,column):
        if self._matching_syncing or column!=0:
            return
        key=str(item.data(0,Qt.UserRole) or "")
        candidate=self._matching_by_path.get(key)
        if candidate is None or candidate.status!="will_change":
            return
        if item.checkState(0)==Qt.Checked:
            self._matching_selected.add(key)
        else:
            self._matching_selected.discard(key)
        self._update_matching_apply_state()

    def _set_matching_shown(self,checked):
        self._matching_syncing=True
        try:
            for row in range(self.matching_tree.topLevelItemCount()):
                item=self.matching_tree.topLevelItem(row)
                key=str(item.data(0,Qt.UserRole) or "")
                candidate=self._matching_by_path.get(key)
                if candidate is None or candidate.status!="will_change":
                    continue
                if checked:
                    self._matching_selected.add(key)
                else:
                    self._matching_selected.discard(key)
                item.setCheckState(
                    0,Qt.Checked if checked else Qt.Unchecked
                )
        finally:
            self._matching_syncing=False
        self._update_matching_apply_state()

    def _change_matching_page(self,amount):
        self._matching_page=max(0,self._matching_page+int(amount))
        self._render_matching_preview()

    def _show_matching_detail(self):
        item=self.matching_tree.currentItem()
        key=str(item.data(0,Qt.UserRole) or "") if item else ""
        candidate=self._matching_by_path.get(key)
        if candidate is None:
            self.matching_restore_removed.hide()
            return
        removed_ids=tuple(dict.fromkeys(
            match
            for warning in candidate.warnings
            for match in re.findall(r'removed identity "([^"]+)"',warning)
            if match in self.state.knowledge.removed_tag_ids
        ))
        self._matching_removed_ids=removed_ids
        self.matching_restore_removed.setVisible(bool(removed_ids))
        self.matching_restore_removed.setText(
            "Review Removed Tag…" if len(removed_ids)==1 else
            f"Review {len(removed_ids):,} Removed Tags…"
        )
        lines=[
            str(candidate.image),"",
            {
                "will_change":(
                    "Exact link or preferred-name correction is available; "
                    "review notes are listed separately."
                ),
                "unresolved":"Needs review; no automatic correction is offered",
                "unavailable":"Unavailable",
                "read_only":"Read-only",
                "failed":"Could not inspect",
            }.get(candidate.status,candidate.status),
        ]
        if candidate.changes:
            lines.extend(("","Existing confirmed Tags and exact result"))
            reasons={
                "canonical_spelling":"Preferred visible name",
                "stable_id_redirect":"Reviewed identity redirect",
                "missing_identity_link":"Missing internal identity link",
                "same_identity_duplicate":"Repeated occurrence of the same identity",
            }
            for group in self._matching_change_groups(candidate):
                existing=", ".join(
                    f'"{value}"' for value in group["existing"]
                )
                lines.extend((
                    f"Existing confirmed Tag: {existing}",
                    f'System Knowledge Tag: "{group["canonical"]}"',
                    (
                        f'Visible spelling: changes from "{group["spelling"][0]}" '
                        f'to "{group["spelling"][1]}".'
                        if group["spelling"] else
                        "Visible spelling: unchanged."
                    ),
                    f'Internal identity (secondary detail): {group["stable_id"]}',
                ))
                for change in group["changes"]:
                    detail=reasons.get(change.reason,change.reason)
                    if change.reason in {
                        "stable_id_redirect","missing_identity_link",
                    }:
                        detail+=f": {change.before} → {change.after}"
                    lines.append(f"• {change.location}: {detail}")
                lines.append("")
        if candidate.warnings:
            lines.extend(("","Separate work held for review",*(
                f"• {value}" for value in candidate.warnings
            )))
        if candidate.message and not candidate.changes:
            lines.extend(("",candidate.message))
        lines.extend((
            "",
            "Unchanged: owner and group placement, local order, review decisions, "
            "provenance, exact raw evidence, TXT, Metadata, source image, and file location.",
        ))
        self.matching_detail.setPlainText("\n".join(lines))

    def _review_matching_removed_tags(self):
        stable_ids=tuple(self._matching_removed_ids)
        if not stable_ids:
            return
        restored=[]
        dialog=RemovedVocabularyDialog(
            self.state.knowledge,self,initial_ids=stable_ids,
        )
        dialog.restored.connect(lambda values:restored.extend(values))
        dialog.exec()
        if not restored:
            return
        self.knowledge_changed.emit()
        self._set_matching_status(
            f"Restored {len(set(restored)):,} removed System Knowledge "
            "identity record(s). Rechecking the current image scope…",
            announce=True,
        )
        QTimer.singleShot(0,self.audit_matching_images)

    def _edit_matching_image(self):
        item=self.matching_tree.currentItem()
        key=str(item.data(0,Qt.UserRole) or "") if item else ""
        if key:
            self.edit_image_data.emit(key)

    def _return_from_matching_images(self):
        scope=dict(self._matching_scope)
        scope.update({
            "kind":"system_knowledge_identity",
            "origin":"dataset",
        })
        self.open_tag_encyclopedia.emit(
            str(scope.get("review_view_id") or "all"),scope,
        )

    def _update_matching_apply_state(self):
        busy=self._matching_busy or self.has_active_background_work()
        count=sum(
            key in self._matching_selected and item.status=="will_change"
            for key,item in self._matching_by_path.items()
        )
        self.matching_apply_button.setEnabled(not busy and count>0)
        self.matching_apply_button.setText(
            f"Apply Checked Link Corrections ({count:,})…"
            if count else "Apply Checked Link Corrections…"
        )

    def _update_matching_undo_state(self):
        available=bool(self._history_correction_target[0])
        self.matching_undo_button.setEnabled(
            not self.has_active_background_work() and available
        )
        try:
            before=self.downstream_updates.load()
            after=self.downstream_updates.record_identity_recovery(
                available or self._matching_checkpoint_path().exists()
            )
        except OSError:
            pass
        else:
            if before!=after:self.downstream_updates_changed.emit()

    def apply_matching_corrections(self):
        if self.has_active_background_work() or self._matching_preview is None:
            return
        selected=[
            item.image for item in self._matching_preview.items
            if item.status=="will_change"
            and str(item.image) in self._matching_selected
        ]
        if not selected:
            return
        try:
            history=capture_operation(self,selected,operation_kind="image_identity_correction",
                reason="Normalize exact confirmed Tag identities")
        except Exception as exc:
            QMessageBox.warning(self,"Saved history needs attention",str(exc))
            return
        if QMessageBox.question(
            self,
            "Apply matching-image link corrections",
            f"Apply {len(selected):,} checked link or preferred-name correction(s) "
            "to authoritative Image Knowledge sidecars?\n\nThese changes link "
            "already-confirmed Tags to current System Knowledge. They do not add "
            "image facts. TXT, Metadata, source images, raw observations, file "
            "locations, and separately meaningful Tags remain unchanged. Every "
            "write is stale-checked, journaled, and read-back verified. Use "
            "Review Correction Changes to compare recorded data before restoring it. "
            "Sources without configured history retain their legacy recovery evidence.",
        )!=QMessageBox.Yes:
            return
        if not self._matching_set_busy(
            True,"Applying checked Image Knowledge corrections…",
        ):
            return
        service=self._matching_service()
        preview=self._matching_preview
        self._history_apply_paths=tuple(selected)
        self._history_correction_target=((),"")
        self._history_correction_interrupted=False
        task=CancelableProgressCallTask(
            lambda progress,cancelled:service.apply(
                preview,selected_images=selected,progress=progress,
                should_cancel=cancelled,
                **({"history_operation":history} if history is not None else {}),
            )
        )
        self._matching_task=task
        task.signals.progress.connect(self._matching_progress_changed)
        task.signals.complete.connect(self._matching_apply_ready)
        task.signals.cancelled.connect(self._matching_cancelled)
        task.signals.failed.connect(self._matching_task_failed)
        self.pool.start(task)

    def _matching_apply_ready(self,result):
        self._matching_task=None
        self._history_correction_interrupted=bool(getattr(result,"history_interrupted",False))
        operation_id=getattr(result,"history_operation_id","") or getattr(result,"operation_id","")
        if operation_id:
            self._history_correction_target=(self._history_apply_paths,operation_id)
            self.matching_undo_button.setToolTip(f"Review {len(self._history_apply_paths):,} exact images from correction {operation_id}.")
        self._matching_set_busy(False)
        self._matching_preview=None
        self._matching_by_path={}
        self._matching_selected=set()
        self._matching_page=0
        self._render_matching_preview()
        self._matching_completion_summary=(
            f"Link update finished: {result.changed:,} changed, "
            f"{result.stale:,} stale, {result.failed:,} failed, and "
            f"{result.not_processed:,} not processed. TXT, Metadata, source "
            "images, raw observations, and file locations were unchanged. "
        )
        if getattr(result,"history_interrupted",False):
            self._matching_completion_summary="An image may already be saved. Review Correction Changes and open its recovery before another edit. "+self._matching_completion_summary
        self._set_matching_status(
            self._matching_completion_summary,
            announce=True,
        )
        self._update_matching_undo_state()
        try:
            self.downstream_updates.record_identity_apply(
                result.changed,
                stable_ids=tuple(
                    self._matching_scope.get("stable_ids",()) or ()
                ),
                source_names=tuple(
                    self._matching_scope.get("source_names",()) or ()
                ),
            )
        except OSError:
            pass
        else:
            self.downstream_updates_changed.emit()
        # A verified no-work audit, not a decremented count, clears the notice.
        # Queue before refresh signals can start competing library work.
        self._queue_matching_audit()
        if result.changed and not self._shutdown_requested:
            self.refresh_requested.emit()
        self._notify_background_work_finished()

    def undo_matching_correction(self):
        if self.has_active_background_work():
            return
        paths, operation_id = self._history_correction_target
        if not paths or not operation_id:
            return
        try:
            open_history(self, paths, operation_target=operation_id,
                on_complete=self._history_restore_complete,
                recovery_handler=lambda image:self.edit_image_data.emit(str(image)))
        except Exception as exc:
            QMessageBox.warning(self,"Correction history could not open",str(exc))

    def _history_restore_complete(self, paths):
        if not paths:
            return
        self._history_correction_interrupted=False
        try:
            self.downstream_updates.require_identity_recheck()
        except OSError:
            pass
        else:
            self.downstream_updates_changed.emit()
        if not self._shutdown_requested:
            self.history_restored.emit(tuple(paths))
        self._notify_background_work_finished()

    def cancel_matching_task(self):
        if self._matching_audit_pending:
            self._matching_audit_pending=False
            self._matching_audit_retry.stop()
            self.matching_cancel_button.hide()
            self._update_matching_apply_state()
            self._update_matching_undo_state()
            self._set_matching_status(
                self._matching_completion_summary
                +"Waiting check cancelled. Check Matching Images to verify remaining work.",
                announce=True,
            )
            self.matching_detail.setPlainText(
                "The waiting check was cancelled. Choose Check Matching Images to try again."
            )
            self._notify_background_work_finished()
            return
        task=self._matching_task
        if task is None or not hasattr(task,"cancel"):
            return
        self.state.request_long_operation_cancel("dataset")
        self.matching_cancel_button.setEnabled(False)
        self._set_matching_status("Stopping safely after the current image…")
        task.cancel()

    def has_active_background_work(self) -> bool:
        return self._matching_task is not None or self._matching_audit_pending or history_dialogs_busy(self)

    def request_background_shutdown(self) -> bool:
        self._shutdown_requested=True
        self._matching_audit_pending=False
        self._matching_audit_retry.stop()
        history_busy=close_history_dialogs(self)
        task=self._matching_task
        if task is None:
            return history_busy
        if hasattr(task.signals,"cancelled") and hasattr(task,"cancel"):
            self.state.request_long_operation_cancel("dataset")
            task.cancel()
            self.matching_cancel_button.setEnabled(False)
            self._set_matching_status(
                "Closing after matching-image work stops safely…"
            )
        else:
            self._set_matching_status(
                "Closing after matching-image recovery finishes safely…"
            )
        return True

    def _notify_background_work_finished(self):
        if not self.has_active_background_work():
            self.background_work_finished.emit()
