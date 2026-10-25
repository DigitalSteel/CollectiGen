from __future__ import annotations

from PySide6.QtCore import QThreadPool,Signal
from PySide6.QtWidgets import (
    QDialog,QHBoxLayout,QHeaderView,QLabel,QMessageBox,QPushButton,QTreeWidget,
    QTreeWidgetItem,QVBoxLayout,QWidget,
)

from core import (
    CategoryRegistry,DiscoveryRegistry,KnowledgeCurator,
    reviewed_sensitivity_proposals,
)
from core.paths import (
    CATEGORIES_PATH,DEFAULTS_DIR,DISCOVERED_TAGS_PATH,
)
from core.knowledge_curator import KnowledgeCurationStaleError

from .components import show_operation_in_progress
from .status_notifications import FooterMessageBox as QMessageBox
from .knowledge_maintenance import KnowledgeSafeFixReviewDialog
from .workers import AsyncCallTask


def _sensitivity_authority_paths():
    return (
        DEFAULTS_DIR/"foundation_tags.json",
        DEFAULTS_DIR/"community_tags.json",
        DEFAULTS_DIR/"promoted_tags.json",
    )


class SystemKnowledgeMaintenanceWidget(QWidget):
    """Tag Encyclopedia-owned deterministic System Knowledge maintenance."""

    knowledge_changed=Signal()
    content_sensitivity_changed=Signal(object)
    open_review_view=Signal(str,object)
    return_requested=Signal()
    background_work_finished=Signal()

    def __init__(self,state,parent=None,*,write_block_reason=None):
        super().__init__(parent)
        self.state=state
        self._write_block_reason=(
            write_block_reason if callable(write_block_reason) else lambda:""
        )
        self.pool=QThreadPool.globalInstance()
        self._task=None
        self._report=None
        self._review_after_scan=False
        self._handoff_scope={}
        self._shutdown_requested=False
        self._knowledge_event_callback=self._knowledge_changed_externally
        self.state.events.subscribe(
            "knowledge_changed",self._knowledge_event_callback,
        )
        self._build_ui()

    def _build_ui(self):
        layout=QVBoxLayout(self)
        layout.setContentsMargins(8,8,8,8)
        heading=QLabel("System Knowledge maintenance")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        intro=QLabel(
            "Scan for deterministic maintenance separately from ordinary Tag "
            "Encyclopedia editing. The scan changes nothing. Review Safe Fixes "
            "shows the exact proposed records first; only Apply Checked Fixes "
            "writes System Knowledge through a verified backup and recovery "
            "transaction. "
            "Image Knowledge, captions, Metadata, TXT, raw evidence, and image "
            "files are never changed here."
        )
        intro.setWordWrap(True)
        intro.setObjectName("mutedLabel")
        layout.addWidget(intro)

        actions=QHBoxLayout()
        self.scan_button=QPushButton("Scan Tag Encyclopedia")
        self.scan_button.clicked.connect(self.scan_system_knowledge)
        self.scan_button.setAccessibleDescription(
            "Build or refresh the current non-mutating maintenance report "
            "without opening the safe-fix review."
        )
        self.review_button=QPushButton("Review Safe Fixes…")
        self.review_button.clicked.connect(self.review_safe_fixes)
        self.review_button.setAccessibleDescription(
            "Build the current report when needed, then review the exact safe "
            "fixes before any System Knowledge is changed."
        )
        self.return_button=QPushButton("Back to Tag Encyclopedia")
        self.return_button.clicked.connect(self.return_requested.emit)
        for button in (self.scan_button,self.review_button,self.return_button):
            button.setProperty("compact",True)
        actions.addWidget(self.scan_button)
        actions.addWidget(self.review_button)
        actions.addStretch(1)
        actions.addWidget(self.return_button)
        layout.addLayout(actions)

        self.status=QLabel(
            "Choose Scan Tag Encyclopedia to build a current, non-mutating "
            "maintenance report."
        )
        self.status.setWordWrap(True)
        self.status.setObjectName("mutedLabel")
        self.status.setAccessibleName("System Knowledge maintenance status")
        layout.addWidget(self.status)
        from .components import bind_status_footer
        bind_status_footer(self,self.status,title="Tag Encyclopedia maintenance",follow_visibility=True)

        self.report_tree=QTreeWidget()
        self.report_tree.setHeaderLabels(
            ("Report area","Proposed records","Meaning")
        )
        self.report_tree.setRootIsDecorated(False)
        self.report_tree.setAlternatingRowColors(False)
        self.report_tree.setAccessibleName("System Knowledge maintenance report")
        header=self.report_tree.header()
        header.setSectionResizeMode(0,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2,QHeaderView.Stretch)
        layout.addWidget(self.report_tree,1)

        review_heading=QLabel("Review in Tag Encyclopedia")
        review_heading.setObjectName("sectionTitle")
        layout.addWidget(review_heading)
        review_hint=QLabel(
            "Organization, sensitivity, and missing-relationship questions remain "
            "editable review views in the primary category-and-tag canvas."
        )
        review_hint.setWordWrap(True)
        review_hint.setObjectName("mutedLabel")
        layout.addWidget(review_hint)
        review_actions=QHBoxLayout()
        self.review_view_buttons=[]
        for label,key in (
            ("Needs Organization","needs_organization"),
            ("Sensitivity Review","sensitivity"),
            ("Missing Relationships","missing_relationships"),
        ):
            button=QPushButton(label)
            button.setProperty("compact",True)
            button.clicked.connect(
                lambda _checked=False,value=key:self._open_review(value)
            )
            self.review_view_buttons.append(button)
            review_actions.addWidget(button)
        review_actions.addStretch(1)
        layout.addLayout(review_actions)
        self._sync_action_state()

    def open_scope(self,function_id,scope=None):
        """Open this destination without starting a scan or write."""
        self._handoff_scope=dict(scope) if isinstance(scope,dict) else {}
        self._invalidate_report()
        if str(function_id)=="knowledge.system_knowledge.safe_fixes":
            self.status.setText(
                "Safe-fix review opened in Tag Encyclopedia. Choose Review Safe "
                "Fixes to build a current non-mutating report; opening this "
                "destination did not run a job."
            )
        elif self._report is None:
            self.status.setText(
                "Choose Scan Tag Encyclopedia to build a current, non-mutating "
                "maintenance report. Opening this destination did not run a job."
            )

    def _open_review(self,review_view_id):
        scope=dict(self._handoff_scope)
        scope.update({
            "kind":"system_knowledge",
            "origin":"system_knowledge_maintenance",
        })
        self.open_review_view.emit(str(review_view_id),scope)

    @staticmethod
    def _set_primary(button,primary):
        primary=bool(primary)
        current=button.property("primary")
        if current is not None and bool(current)==primary:
            return
        button.setProperty("primary",primary)
        style=button.style()
        style.unpolish(button)
        style.polish(button)
        button.update()

    def _has_safe_fixes(self):
        report=self._report
        return bool(
            report is not None
            and (
                tuple(getattr(report,"changes",()) or ())
                or tuple(getattr(report,"discovery_names",()) or ())
            )
        )

    def _sync_action_state(self,busy=False):
        can_review=self._report is None or self._has_safe_fixes()
        self.scan_button.setEnabled(not busy)
        self.review_button.setEnabled(not busy and can_review)
        self.return_button.setEnabled(not busy)
        for button in self.review_view_buttons:
            button.setEnabled(not busy)
        self._set_primary(self.scan_button,False)
        self._set_primary(self.review_button,can_review)

    def _set_busy(self,busy,message=""):
        self._sync_action_state(busy)
        if message:
            self.status.setText(message)

    def _invalidate_report(self,message=""):
        self._report=None
        self.report_tree.clear()
        self._sync_action_state(self._task is not None)
        if message:
            self.status.setText(message)

    def _knowledge_changed_externally(self,**_payload):
        if self._task is None and self._report is not None:
            self._invalidate_report(
                "System Knowledge changed after the previous scan. Build a "
                "fresh report before reviewing safe fixes."
            )

    def has_active_background_work(self):
        return self._task is not None

    def request_background_shutdown(self):
        if self._task is None:
            return False
        self._shutdown_requested=True
        self._set_busy(
            True,
            "Finishing the current System Knowledge operation safely before "
            "CollectiGen closes. A verified write cannot be cancelled after "
            "it starts.",
        )
        return True

    def _notify_background_work_finished(self):
        if self._task is None:
            self.background_work_finished.emit()

    def _scan_work(self):
        knowledge=type(self.state.knowledge)(self.state.knowledge.path)
        knowledge.load()
        authority_paths=_sensitivity_authority_paths()
        sensitivity=reviewed_sensitivity_proposals(authority_paths)
        discovery=DiscoveryRegistry(DISCOVERED_TAGS_PATH)
        try:
            return KnowledgeCurator(
                knowledge,sensitivity,guard_paths=authority_paths,
            ).preview(discovery_registry=discovery)
        except KnowledgeCurationStaleError as exc:
            return exc

    def scan_system_knowledge(self,*_,review_after=False):
        if self._task is not None:
            return
        started,active=self.state.begin_long_operation(
            "knowledge","Scanning System Knowledge maintenance"
        )
        if not started:
            show_operation_in_progress(self,active)
            return
        self._invalidate_report()
        self._review_after_scan=bool(review_after)
        self._set_busy(
            True,
            "Scanning System Knowledge in the background. No records are being "
            "changed. This bounded scan finishes without an unsafe partial "
            "cancellation…",
        )
        task=AsyncCallTask(self._scan_work)
        self._task=task
        task.signals.complete.connect(self._scan_ready)
        task.signals.failed.connect(self._task_failed)
        self.pool.start(task)

    def review_safe_fixes(self):
        if self._task is not None:
            return
        if self._report is None:
            self.scan_system_knowledge(review_after=True)
            return
        self._open_safe_fix_review()

    def _scan_ready(self,report):
        if isinstance(report,KnowledgeCurationStaleError):
            self._stale_ready(report)
            return
        self.state.finish_long_operation("knowledge")
        self._task=None
        self._report=report
        self._set_busy(False)
        self.report_tree.clear()
        area_counts={}
        for change in report.changes:
            area_counts.setdefault(
                str(change.area or "Other"),set()
            ).add(str(change.tag_id or change.discovery_name or change.display))
        if report.discovery_names:
            area_counts["Discovery"]={str(value) for value in report.discovery_names}
        meanings={
            "Category":"Assign an exact reviewed category",
            "Reusable meaning":"Link deterministic reusable meaning",
            "Relation":"Add deterministic semantic relationship",
            "Sensitivity":"Apply shipped reviewed sensitivity",
            "Discovery":"Remove an already-recognized duplicate discovery entry",
        }
        for area,identities in sorted(
            area_counts.items(),key=lambda item:item[0].casefold()
        ):
            self.report_tree.addTopLevelItem(QTreeWidgetItem((
                area,f"{len(identities):,}",
                meanings.get(area,"Deterministic System Knowledge maintenance"),
            )))
        count=(
            len({str(change.tag_id) for change in report.changes if change.tag_id})
            +len(report.discovery_names)
        )
        if count:
            self.status.setText(
                f"Scan complete • {count:,} exact record or discovery change(s) "
                "available for review. Nothing has been applied."
            )
        else:
            self.report_tree.addTopLevelItem(QTreeWidgetItem((
                "No safe fixes","0",
                "No deterministic maintenance change is currently available.",
            )))
            self.status.setText(
                "Scan complete • no deterministic safe fixes are available. "
                "Review questions remain in Tag Encyclopedia views."
            )
        review_after=self._review_after_scan
        self._review_after_scan=False
        shutdown_requested=self._shutdown_requested
        self._shutdown_requested=False
        if review_after and not shutdown_requested:
            self._open_safe_fix_review()
        self._notify_background_work_finished()

    def _open_safe_fix_review(self):
        report=self._report
        if report is None:
            return
        if not self._has_safe_fixes():
            self.status.setText(
                "No deterministic safe fixes are available in the current report."
            )
            return
        categories=CategoryRegistry(DEFAULTS_DIR/"categories.json",CATEGORIES_PATH)
        dialog=KnowledgeSafeFixReviewDialog(report,categories,self)
        if dialog.exec()!=QDialog.Accepted:
            return
        self._apply_safe_fixes(
            dialog.selected_tag_ids(),dialog.selected_discovery_names()
        )

    def _apply_safe_fixes(self,selected_tag_ids,selected_discovery_names):
        if self._task is not None:
            return
        selected_tag_ids=set(selected_tag_ids or ())
        selected_discovery_names=set(selected_discovery_names or ())
        if not selected_tag_ids and not selected_discovery_names:
            return
        blocked=str(self._write_block_reason() or "")
        if blocked:
            self.status.setText(blocked)
            return
        started,active=self.state.begin_long_operation(
            "knowledge","Applying reviewed System Knowledge maintenance"
        )
        if not started:
            show_operation_in_progress(self,active)
            return
        self._set_busy(
            True,
            "Applying checked fixes in the background with a backup. The "
            "verified transaction cannot be cancelled after it starts…",
        )
        preview=self._report

        def work():
            knowledge=type(self.state.knowledge)(self.state.knowledge.path)
            knowledge.load()
            authority_paths=_sensitivity_authority_paths()
            sensitivity=reviewed_sensitivity_proposals(authority_paths)
            discovery=DiscoveryRegistry(DISCOVERED_TAGS_PATH)
            try:
                return KnowledgeCurator(
                    knowledge,sensitivity,
                    guard_paths=authority_paths,
                ).apply_reviewed(
                    preview,discovery_registry=discovery,
                    selected_tag_ids=selected_tag_ids,
                    selected_discovery_names=selected_discovery_names,
                )
            except KnowledgeCurationStaleError as exc:
                return exc

        task=AsyncCallTask(work)
        self._task=task
        task.signals.complete.connect(self._apply_ready)
        task.signals.failed.connect(self._task_failed)
        self.pool.start(task)

    def _apply_ready(self,report):
        if isinstance(report,KnowledgeCurationStaleError):
            self._stale_ready(report)
            return
        self.state.finish_long_operation("knowledge")
        self._task=None
        self._report=None
        self._set_busy(False)
        changed=len({change.tag_id for change in report.changes if change.tag_id})
        self.status.setText(
            f"Saved {changed:,} SDXL Tag record(s) • reconciled "
            f"{report.discoveries_reconciled:,} duplicate discovery entry or "
            "entries. Image-facing data was unchanged. Verified recovery: "
            f"{report.backup}"
        )
        shutdown_requested=self._shutdown_requested
        if not shutdown_requested:
            self.knowledge_changed.emit()
            if report.sensitivity_change_ids:
                self.content_sensitivity_changed.emit({
                    "tag_ids":tuple(report.sensitivity_change_ids),
                })
        self._shutdown_requested=False
        self._notify_background_work_finished()

    def _stale_ready(self,error):
        self.state.finish_long_operation("knowledge")
        self._task=None
        self._review_after_scan=False
        self._invalidate_report()
        self._set_busy(
            False,
            f"The safe-fix report is no longer current. {error}",
        )
        self._shutdown_requested=False
        self._notify_background_work_finished()

    def _task_failed(self,message):
        shutdown_requested=self._shutdown_requested
        self.state.finish_long_operation("knowledge")
        self._task=None
        self._review_after_scan=False
        self._invalidate_report()
        self._set_busy(
            False,
            "System Knowledge maintenance stopped. The previous report was "
            "discarded; scan again after resolving the reported problem.",
        )
        if not shutdown_requested:
            QMessageBox.critical(
                self,"System Knowledge maintenance stopped",message,
            )
        self._shutdown_requested=False
        self._notify_background_work_finished()

    def closeEvent(self,event):
        self.state.events.unsubscribe(
            "knowledge_changed",self._knowledge_event_callback,
        )
        super().closeEvent(event)
