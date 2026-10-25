from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QThreadPool,QTimer,Qt,Signal
from PySide6.QtWidgets import (
    QAbstractItemView,QCheckBox,QComboBox,QDialog,QDialogButtonBox,QFileDialog,QFrame,
    QHBoxLayout,QHeaderView,QLabel,QLineEdit,QMenu,QMessageBox,QProgressBar,
    QPushButton,QTableWidget,QTableWidgetItem,QTabWidget,QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core import LibraryRole,OrphanCompanionAuditor,RecycleRecord,TransferRecoveryRequired
from .components import bind_status_footer,compact_button,set_progress_bar,show_operation_in_progress
from .library_profile_dialog import LibraryDestinationDialog
from .modal_feedback import FeedbackEnabledDialog
from .workers import CancelableProgressCallTask
from .history_workflow import open_recycle_history, history_dialogs_busy, close_history_dialogs


class RecycleBinDialog(FeedbackEnabledDialog):
    """Paged recovery and explicit permanent-delete review for app-owned recycle."""

    feedback_workflow="Recycle and orphan recovery"
    changed=Signal()
    library_paths_changed=Signal(object)

    def __init__(self,state,parent=None):
        super().__init__(parent); self.state=state; self.settings=state.settings
        self.pool=QThreadPool.globalInstance(); self.page=0; self.audit_page=0
        self._task=None; self._operation_kind=""; self._audit_report=None
        self._record_scope=None
        self._close_after_work=False
        self._audit_roots=self._managed_audit_roots()
        self.setWindowTitle("Recycle Bin and Orphan Recovery")
        self.resize(1180,760); self.setMinimumSize(900,600)
        root=QVBoxLayout(self)
        title=QLabel("Recycle Bin and Orphan Recovery"); title.setObjectName("pageTitle")
        root.addWidget(title)
        intro=QLabel(
            "Recycle is separate from libraries, Ingest, Problems, and protected backups. "
            "Moves preserve the complete recognized file family and a verified return path. "
            "Permanent deletion is available only here after a fresh exact-file preview."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel"); root.addWidget(intro)
        self.tabs=QTabWidget(); root.addWidget(self.tabs,1)
        self._build_recycle_tab(); self._build_orphan_tab()
        self.progress=QProgressBar(); self.progress.hide(); root.addWidget(self.progress)
        self.operation_status=QLabel(); self.operation_status.setWordWrap(True)
        self.operation_status.setObjectName("mutedLabel")
        self.cancel_button=compact_button(QPushButton("Cancel Safely")); self.cancel_button.hide()
        self.cancel_button.clicked.connect(self.cancel_work)
        operation_row=QHBoxLayout(); operation_row.addWidget(self.operation_status,1); operation_row.addWidget(self.cancel_button)
        bind_status_footer(
            self,self.operation_status,self.progress,self.cancel_button,title="Recycle activity",
        )
        root.addLayout(operation_row)
        buttons=QDialogButtonBox(QDialogButtonBox.Close); buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self.refresh_records()

    def _build_recycle_tab(self):
        tab=QWidget(); layout=QVBoxLayout(tab)
        filters=QHBoxLayout()
        self.search=QLineEdit(); self.search.setPlaceholderText("Filter by filename, original folder, or recovery status…")
        self.search.setAccessibleName("Filter Recycle Bin items")
        self.search_timer=QTimer(self); self.search_timer.setSingleShot(True); self.search_timer.setInterval(160)
        self.search.textChanged.connect(lambda *_:self.search_timer.start())
        self.search_timer.timeout.connect(self._reset_recycle_page)
        refresh=compact_button(QPushButton("Refresh")); refresh.clicked.connect(self.refresh_records)
        self.all_records_button=compact_button(QPushButton("Show All Recycle Items"))
        self.all_records_button.clicked.connect(self.show_all_records)
        self.all_records_button.hide()
        filters.addWidget(self.search,1); filters.addWidget(self.all_records_button); filters.addWidget(refresh); layout.addLayout(filters)
        self.recycle_status=QLabel(); self.recycle_status.setWordWrap(True); self.recycle_status.setObjectName("mutedLabel")
        layout.addWidget(self.recycle_status)
        self.records=QTableWidget(0,5)
        self.records.setHorizontalHeaderLabels(("Original item","Kind","Original folder","Recycled","Status"))
        self.records.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.records.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.records.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.records.verticalHeader().hide(); self.records.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeToContents)
        self.records.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeToContents)
        self.records.horizontalHeader().setSectionResizeMode(2,QHeaderView.Stretch)
        self.records.horizontalHeader().setSectionResizeMode(3,QHeaderView.ResizeToContents)
        self.records.horizontalHeader().setSectionResizeMode(4,QHeaderView.ResizeToContents)
        self.records.setAccessibleName("Recycle Bin items")
        self.records.itemSelectionChanged.connect(self._update_recycle_actions)
        self.records.setContextMenuPolicy(Qt.CustomContextMenu)
        self.records.customContextMenuRequested.connect(self._show_recycle_menu)
        layout.addWidget(self.records,1)
        pager=QHBoxLayout(); self.previous=compact_button(QPushButton("Previous Page")); self.next=compact_button(QPushButton("Next Page"))
        self.page_label=QLabel("No items"); self.page_label.setAlignment(Qt.AlignCenter)
        self.previous.clicked.connect(lambda:self._change_recycle_page(-1)); self.next.clicked.connect(lambda:self._change_recycle_page(1))
        pager.addWidget(self.previous); pager.addWidget(self.page_label,1); pager.addWidget(self.next); layout.addLayout(pager)
        actions=QHBoxLayout()
        self.restore_button=compact_button(QPushButton("Restore Original Location"),primary=True)
        self.ingest_button=compact_button(QPushButton("Move to Ingest…"))
        self.library_button=compact_button(QPushButton("Move to Library…"))
        self.purge_button=compact_button(QPushButton("Permanently Delete…"))
        self.restore_button.clicked.connect(self.restore_selected)
        self.ingest_button.clicked.connect(self.route_selected_ingest)
        self.library_button.clicked.connect(self.route_selected_library)
        self.purge_button.clicked.connect(self.purge_selected)
        actions.addWidget(self.restore_button); actions.addWidget(self.ingest_button)
        actions.addWidget(self.library_button); actions.addStretch(1); actions.addWidget(self.purge_button)
        layout.addLayout(actions); self.tabs.addTab(tab,"Recycle Bin")

    def _build_orphan_tab(self):
        tab=QWidget(); layout=QVBoxLayout(tab)
        intro=QLabel(
            "This audit only reports adjacent .tags.json, TXT, and XMP companions whose image is not resolved. "
            "Ambiguous stems, unsupported image formats, placeholders, and app-owned backup or recovery areas are held for review. The scan never writes."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel"); layout.addWidget(intro)
        toolbar=QHBoxLayout()
        self.audit_search=QLineEdit(); self.audit_search.setPlaceholderText("Filter the audit report…")
        self.audit_search.textChanged.connect(self._reset_audit_page)
        managed=compact_button(QPushButton("Audit Managed Locations"),primary=True); managed.clicked.connect(self.audit_managed)
        choose=compact_button(QPushButton("Audit Folder…")); choose.clicked.connect(self.audit_folder)
        toolbar.addWidget(self.audit_search,1); toolbar.addWidget(managed); toolbar.addWidget(choose); layout.addLayout(toolbar)
        self.audit_status=QLabel("Run an audit to build a report. No files will change.")
        self.audit_status.setWordWrap(True); self.audit_status.setObjectName("mutedLabel"); layout.addWidget(self.audit_status)
        self.audit_table=QTableWidget(0,4)
        self.audit_table.setHorizontalHeaderLabels(("Companion","Classification","Folder","Review outcome"))
        self.audit_table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.audit_table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.audit_table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.audit_table.verticalHeader().hide()
        self.audit_table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeToContents)
        self.audit_table.horizontalHeader().setSectionResizeMode(1,QHeaderView.ResizeToContents)
        self.audit_table.horizontalHeader().setSectionResizeMode(2,QHeaderView.Stretch)
        self.audit_table.horizontalHeader().setSectionResizeMode(3,QHeaderView.Stretch)
        self.audit_table.setAccessibleName("Orphan companion audit report")
        self.audit_table.itemSelectionChanged.connect(self._update_orphan_actions)
        layout.addWidget(self.audit_table,1)
        pager=QHBoxLayout(); self.audit_previous=compact_button(QPushButton("Previous Page")); self.audit_next=compact_button(QPushButton("Next Page"))
        self.audit_page_label=QLabel("No report"); self.audit_page_label.setAlignment(Qt.AlignCenter)
        self.audit_previous.clicked.connect(lambda:self._change_audit_page(-1)); self.audit_next.clicked.connect(lambda:self._change_audit_page(1))
        pager.addWidget(self.audit_previous); pager.addWidget(self.audit_page_label,1); pager.addWidget(self.audit_next); layout.addLayout(pager)
        self.recycle_orphans_button=compact_button(QPushButton("Move Selected Verified Orphans to Recycle Bin…"),primary=True)
        self.recycle_orphans_button.clicked.connect(self.recycle_selected_orphans)
        layout.addWidget(self.recycle_orphans_button,0,Qt.AlignRight)
        self.tabs.addTab(tab,"Orphan Companion Audit")

    def _managed_audit_roots(self):
        values=[]
        for profile in self.settings.library_registry().profiles:
            if str(profile.root or "").strip(): values.append(Path(profile.root))
        ingest=str(getattr(self.settings,"ingest_directory","") or "").strip()
        if ingest: values.append(Path(ingest))
        return tuple(dict.fromkeys(values))

    def _page_size(self):
        return max(12,min(200,int(getattr(self.settings,"gallery_page_size",24))))

    def _selected_record_ids(self):
        return tuple(dict.fromkeys(
            str(item.data(Qt.UserRole) or "") for item in self.records.selectedItems()
            if item.column()==0 and item.data(Qt.UserRole)
        ))

    def _record_selection_ready(self):
        rows={item.row() for item in self.records.selectedItems()}
        return bool(rows) and all(
            self.records.item(row,0) is not None
            and self.records.item(row,0).data(Qt.UserRole+1)=="active"
            for row in rows
        )

    def _selected_orphan_paths(self):
        return tuple(dict.fromkeys(
            Path(str(item.data(Qt.UserRole))) for item in self.audit_table.selectedItems()
            if item.column()==0 and item.data(Qt.UserRole)
            and bool(item.data(Qt.UserRole+1))
        ))

    def refresh_records(self):
        try:
            page=self.state.recycle.page(
                query=self.search.text(),page=self.page,page_size=self._page_size(),
                **({"record_ids":self._record_scope} if self._record_scope is not None else {}),
            )
            root=self.state.recycle.validate_root()
        except Exception as error:
            self.records.setRowCount(0); self.recycle_status.setText(str(error))
            self.page_label.setText("Recycle Bin unavailable")
            self.previous.setEnabled(False); self.next.setEnabled(False)
            self._update_recycle_actions(); return
        self.page=page.page; self.records.setRowCount(len(page.items))
        for row,record in enumerate(page.items):
            values=(
                Path(record.original_path).name,
                "Orphan companion" if record.kind=="orphan" else "Image family",
                str(Path(record.original_path).parent),record.recycled_at,
                record.status.replace("_"," ").title(),
            )
            for column,value in enumerate(values):
                item=QTableWidgetItem(value); item.setToolTip(
                    "\n".join((record.original_path,record.current_path,record.recovery)).strip()
                )
                if column==0:
                    item.setData(Qt.UserRole,record.id)
                    item.setData(Qt.UserRole+1,record.status)
                self.records.setItem(row,column,item)
        pages=max(1,(page.total+page.page_size-1)//page.page_size)
        self.page_label.setText(f"Page {page.page+1} of {pages}" if page.total else "Recycle Bin is empty")
        self.previous.setEnabled(page.page>0); self.next.setEnabled(page.page+1<pages)
        issue=(" • "+" • ".join(page.issues[:2])) if page.issues else ""
        scope_note=f"Recorded completion: {len(self._record_scope):,} exact items • " if self._record_scope is not None else ""
        self.recycle_status.setText(scope_note+f"Recoverable items: {page.total:,} • {root}{issue}")
        self._update_recycle_actions()

    def focus_record_ids(self,record_ids):
        if self._task is not None or history_dialogs_busy(self):
            raise RuntimeError("Finish the current Recycle operation before changing its exact scope.")
        exact=tuple(dict.fromkeys(record_ids))
        if not exact or len(exact)>10000 or any(not isinstance(value,str) or not value for value in exact):
            raise ValueError("Choose exact recorded Recycle items.")
        self._record_scope=exact
        self.all_records_button.show()
        self.page=0
        self.search.clear()
        self.tabs.setCurrentIndex(0)
        self.refresh_records()

    def show_all_records(self):
        if self._task is not None or history_dialogs_busy(self):return
        self._record_scope=None
        self.all_records_button.hide()
        self.page=0
        self.refresh_records()

    def _reset_recycle_page(self):
        self.page=0; self.refresh_records()

    def _change_recycle_page(self,direction):
        self.page=max(0,self.page+int(direction)); self.refresh_records()

    def _update_recycle_actions(self):
        selected=self._record_selection_ready() and self._task is None and not history_dialogs_busy(self)
        for control in (self.restore_button,self.ingest_button,self.library_button,self.purge_button):
            control.setEnabled(selected)

    def _show_recycle_menu(self,position):
        menu=QMenu(self); restore=menu.addAction("Restore Original Location")
        ingest=menu.addAction("Move to Ingest…"); library_=menu.addAction("Move to Library…")
        menu.addSeparator(); purge=menu.addAction("Permanently Delete…")
        enabled=self._record_selection_ready() and self._task is None and not history_dialogs_busy(self)
        for action in (restore,ingest,library_,purge): action.setEnabled(enabled)
        chosen=menu.exec(self.records.viewport().mapToGlobal(position))
        if chosen is restore:self.restore_selected()
        elif chosen is ingest:self.route_selected_ingest()
        elif chosen is library_:self.route_selected_library()
        elif chosen is purge:self.purge_selected()

    def _confirm_route(self,title: str,plans,destination: Path) -> tuple[bool,bool]:
        members=[member for plan in plans for member in plan.transfer.members]
        required=sum(plan.transfer.required_bytes for plan in plans)
        modes=", ".join(sorted({plan.transfer.mode for plan in plans}))
        boundary_required=any(
            plan.boundary_decision is not None
            and plan.boundary_decision.requires_explicit_confirmation
            for plan in plans
        )
        box=QMessageBox(QMessageBox.Question,title,
            f"Move the reviewed Recycle Bin items?\nItems: {len(plans):,} • Files: {len(members):,}\n\nDestination: {destination}\nTransfer modes: {modes}\nAdditional space required: {required:,} bytes",
            QMessageBox.NoButton,self)
        proceed=box.addButton("Move Exact Files",QMessageBox.AcceptRole)
        cancel=box.addButton(QMessageBox.Cancel)
        box.setDefaultButton(cancel); box.setEscapeButton(cancel)
        box.setDetailedText("\n".join(
            f"{member.source}  →  {member.target}" for member in members
        ))
        confirmation=None
        if boundary_required:
            confirmation=QCheckBox(
                "I reviewed the exact files, destination role, and Local Only boundary crossing."
            )
            confirmation.setAccessibleName("Confirm exact Local Only boundary transfer")
            proceed.setEnabled(False); confirmation.toggled.connect(proceed.setEnabled)
            box.setCheckBox(confirmation)
        box.exec()
        accepted=box.clickedButton() is proceed
        return accepted,bool(boundary_required and confirmation is not None and confirmation.isChecked())

    def restore_selected(self):
        ids=self._selected_record_ids(); records=[self.state.recycle.record(value) for value in ids]
        if not records:return
        self._review_record_action("restore", records)

    def _restore_legacy_selected(self,records):
        destinations="\n".join(
            f"{item.current}  →  {item.original}"
            for record in records for item in record.files
        )
        registry=self.settings.library_registry()
        boundary_required=any(
            (profile:=registry.get(record.source_profile_id)) is not None
            and profile.recognized_role is not None
            and profile.recognized_role is not LibraryRole.LOCAL_ONLY
            for record in records if record.source_profile_id
        )
        if not self._confirm_exact("Restore Original Locations",
            f"Restore these Recycle Bin items to their recorded original paths?\nItems to restore: {len(records):,}",
            destinations,"Restore Items",boundary_required=boundary_required):
            return
        self._start_record_operation(
            "restore",records,boundary_confirmed=boundary_required,
        )

    def route_selected_ingest(self):
        ids=self._selected_record_ids(); records=[self.state.recycle.record(value) for value in ids]
        raw=str(getattr(self.settings,"ingest_directory","") or "").strip()
        if not raw or not Path(raw).is_dir():
            QMessageBox.information(self,"Configure Ingest","Choose an available Ingest folder in Setup first."); return
        self._review_record_action("ingest",records,Path(raw))

    def route_selected_library(self):
        ids=self._selected_record_ids(); records=[self.state.recycle.record(value) for value in ids]
        dialog=LibraryDestinationDialog(
            self.settings.library_registry(),self,
            active_library_id=self.settings.active_library_id,source_count=len(records),
        )
        if dialog.exec()!=QDialog.Accepted or dialog.profile is None:return
        destination=Path(dialog.profile.root)
        self._review_record_action(
            "library",records,destination,destination_profile=dialog.profile,
        )

    def _review_record_action(self,kind,records,destination=None,*,destination_profile=None):
        if self._task is not None or history_dialogs_busy(self) or not records:return
        records=tuple(records)
        action={"restore":"restore_origin","ingest":"route_ingest","library":"route_library"}[kind]
        label=f"Reviewing exact Recycle recovery: {len(records):,} item(s)"
        started,active=self.state.begin_long_operation("recycle_bin",label,resource="io")
        if not started:show_operation_in_progress(self,active);return
        def work(progress,should_cancel):
            previews={}
            for index,record in enumerate(records,1):
                if should_cancel():raise InterruptedError("Recycle review cancelled before writing")
                previews[record.id]=self.state.recycle.preview_history(record.id,action=action,
                    destination=destination,destination_profile=destination_profile,should_cancel=should_cancel)
                progress(index,len(records),Path(record.original_path).name)
            return previews
        def reviewed(previews):
            self._finish_task()
            if self._close_after_work:
                self._notify_background_work_finished();return
            native={key:value for key,value in previews.items() if value is not None}
            if native and len(native)!=len(records):
                self.operation_status.setText("Select recorded image families separately from legacy or orphan recovery; nothing changed.")
                return
            if not native:
                if kind=="restore":self._restore_legacy_selected(records)
                else:self._start_route_preview(kind,records,destination,destination_profile=destination_profile)
                return
            dialog=open_recycle_history(self,tuple(record.id for record in records),action=action,
                destination=destination,destination_profile=destination_profile,
                initial_previews=native,on_outcomes=self._history_restored)
            dialog.busyChanged.connect(self._history_busy_changed)
            self._history_busy_changed(history_dialogs_busy(self))
        self._start_task(label,work,reviewed)

    def _history_busy_changed(self,busy):
        self.tabs.setEnabled(not busy and self._task is None)
        self._update_recycle_actions();self._update_orphan_actions()

    def _history_restored(self,outcomes):
        completed=tuple(value for value in outcomes if value.status=="complete")
        if completed:
            paths=tuple(dict.fromkeys(Path(path) for value in completed
                for path in (value.previous_path,value.current_path) if path is not None))
            self.library_paths_changed.emit(paths)
        self.refresh_records()
        self.operation_status.setText(" · ".join(f"{status}: {sum(value.status==status for value in outcomes)}"
            for status in dict.fromkeys(value.status for value in outcomes)))

    def purge_selected(self):
        if self._task is not None or history_dialogs_busy(self):return
        ids=self._selected_record_ids()
        try: plan=self.state.recycle.preview_purge(ids)
        except Exception as error:
            QMessageBox.warning(self,"Permanent deletion unavailable",str(error)); return
        if not plan.ready:
            QMessageBox.warning(self,"Permanent deletion unavailable",
                "The exact current identity, path, or recovery state is uncertain. Nothing can be deleted.\n\n"+"\n".join(plan.errors))
            return
        detail="\n".join(f"{item.source}  [{item.sha256}]" for item in plan.members)
        if not self._confirm_exact(
            "Permanently Delete Reviewed Files",
            f"Permanently delete these Recycle Bin items?\nFiles to delete: {len(plan.members):,} • Recycle Bin items: {len(plan.entry_ids):,}\n\nThis cannot be undone. No library, Ingest, Problems, backup, or unresolved path is eligible.",
            detail,"Permanently Delete",destructive=True,
        ):
            return
        self._start_purge(plan)

    def _confirm_exact(
        self,title,text,detail,accept_label,*,destructive=False,
        boundary_required=False,
    ):
        icon=QMessageBox.Critical if destructive else QMessageBox.Question
        box=QMessageBox(icon,title,text,QMessageBox.NoButton,self)
        proceed=box.addButton(accept_label,QMessageBox.DestructiveRole if destructive else QMessageBox.AcceptRole)
        cancel=box.addButton(QMessageBox.Cancel); box.setDefaultButton(cancel); box.setEscapeButton(cancel)
        confirmation=None
        if boundary_required:
            confirmation=QCheckBox(
                "I reviewed every file, original storage role, and Local Only boundary crossing."
            )
            confirmation.setAccessibleName("Confirm exact Local Only boundary restore")
            proceed.setEnabled(False); confirmation.toggled.connect(proceed.setEnabled)
            box.setCheckBox(confirmation)
        box.setDetailedText(detail); box.exec()
        return box.clickedButton() is proceed and (
            confirmation is None or confirmation.isChecked()
        )

    def _start_route_preview(
        self,kind: str,records,destination: Path,*,destination_profile=None,
    ):
        if self._task is not None or history_dialogs_busy(self) or not records:return
        label=f"Previewing Recycle Bin items for {kind}: {len(records):,}"
        started,active=self.state.begin_long_operation("recycle_bin",label,resource="io")
        if not started:show_operation_in_progress(self,active); return
        def work(progress,should_cancel):
            plans=[]; errors=[]
            for index,record in enumerate(records,1):
                if should_cancel():raise InterruptedError("Recycle route preview cancelled safely")
                try:
                    plan=self.state.recycle.preview_route(
                        record.id,destination,route=kind,
                        destination_profile=destination_profile,
                        should_cancel=should_cancel,
                    )
                    if not plan.ready:
                        errors.extend(
                            f"{Path(record.original_path).name}: {value}"
                            for value in plan.errors
                        )
                    plans.append(plan)
                except InterruptedError:raise
                except Exception as error:
                    errors.append(f"{Path(record.original_path).name}: {error}")
                progress(index,len(records),Path(record.original_path).name)
            return {
                "kind":kind,"records":records,"destination":destination,
                "plans":plans,"errors":errors,
            }
        self._start_task(label,work,self._route_preview_complete)

    def _route_preview_complete(self,result):
        self._finish_task()
        if self._close_after_work:return
        plans=list(result.get("plans",())); records=list(result.get("records",()))
        errors=list(result.get("errors",()))
        if errors or len(plans)!=len(records):
            self.operation_status.setText("Route preview needs attention • nothing moved")
            QMessageBox.warning(
                self,"Complete route is not ready",
                f"The exact {len(records):,}-item scope was held; nothing moved.\n\n"+"\n".join(errors[:12]),
            )
            return
        kind=str(result["kind"]); destination=Path(result["destination"])
        accepted,boundary_confirmed=self._confirm_route(
            "Move Recycle Items to Ingest" if kind=="ingest" else
            "Move Recycle Items to Named Library",
            plans,destination,
        )
        if not accepted:
            self.operation_status.setText("Route cancelled before writing")
            return
        self._start_record_operation(
            kind,records,destination,plans=plans,
            boundary_confirmed=boundary_confirmed,
        )

    def _start_record_operation(
        self,kind: str,records,destination: Path|None=None,*,plans=(),
        boundary_confirmed: bool=False,
    ):
        if self._task is not None or history_dialogs_busy(self):return
        label={"restore":"Restoring recycle items","ingest":"Moving recycle items to Ingest","library":"Moving recycle items to a named library"}[kind]
        started,active=self.state.begin_long_operation("recycle_bin",label,resource="io")
        if not started:show_operation_in_progress(self,active); return
        route_plans={plan.record_id:plan for plan in plans}
        def work(progress,should_cancel):
            completed=[]; errors=[]; cancelled=False
            for index,record in enumerate(records,1):
                if should_cancel():cancelled=True; break
                try:
                    if kind=="restore":
                        self.state.recycle.restore(
                            record.id,boundary_confirmed=boundary_confirmed,
                            should_cancel=should_cancel,
                        )
                    else:
                        self.state.recycle.route(
                            record.id,destination,route=kind,
                            preview=route_plans.get(record.id),
                            boundary_confirmed=boundary_confirmed,
                            should_cancel=should_cancel,
                        )
                    completed.append(record.id)
                except InterruptedError:cancelled=True; break
                except TransferRecoveryRequired as error:
                    errors.append(f"{Path(record.original_path).name}: {error}"); break
                except Exception as error:errors.append(f"{Path(record.original_path).name}: {error}")
                progress(index,len(records),Path(record.original_path).name)
            return {"completed":completed,"errors":errors,"cancelled":cancelled,"kind":kind}
        self._start_task(label,work,self._record_operation_complete)

    def _start_purge(self,plan):
        if self._task is not None:return
        label=f"Permanently deleting {len(plan.members):,} reviewed file(s)"
        started,active=self.state.begin_long_operation("recycle_bin",label,resource="io")
        if not started:show_operation_in_progress(self,active); return
        def work(progress,should_cancel):
            progress(0,len(plan.members),"Rechecking exact file identities")
            result=self.state.recycle.apply_purge(plan,should_cancel=should_cancel)
            progress(len(plan.members),len(plan.members),"Permanent deletion committed")
            return {"completed":[item.id for item in result],"errors":[],"cancelled":False,"kind":"purge"}
        self._start_task(label,work,self._record_operation_complete)

    def _start_task(self,label,callable_,completed):
        task=CancelableProgressCallTask(callable_); self._task=task; self._operation_kind=label
        set_progress_bar(self.progress,unknown_text=label); self.progress.show(); self.cancel_button.show()
        self._set_enabled(False)
        task.signals.progress.connect(self._progress)
        task.signals.complete.connect(completed)
        task.signals.cancelled.connect(self._task_cancelled)
        task.signals.failed.connect(self._task_failed)
        self.pool.start(task,-1)

    def _progress(self,index,total,name):
        self.state.update_long_operation("recycle_bin",index,total,name)
        set_progress_bar(self.progress,index,total,detail=name,unit="item")

    def _record_operation_complete(self,result):
        self._finish_task()
        completed=len(result.get("completed",()))
        errors=list(result.get("errors",()))
        prefix="Paused safely • " if result.get("cancelled") else ""
        self.operation_status.setText(prefix+f"Completed items: {completed:,} • Need attention: {len(errors):,}")
        if completed:self.changed.emit()
        self.refresh_records()
        if errors:QMessageBox.warning(self,"Some recycle items need attention","\n".join(errors[:8]))
        self._notify_background_work_finished()

    def audit_managed(self):
        self._audit_roots=self._managed_audit_roots()
        if not self._audit_roots:
            QMessageBox.information(self,"No managed locations","Configure an Ingest folder or named image library first."); return
        self._start_audit()

    def audit_folder(self):
        selected=QFileDialog.getExistingDirectory(self,"Choose folder to audit",str(Path.cwd()))
        if selected:self._audit_roots=(Path(selected),); self._start_audit()

    def _start_audit(self):
        if self._task is not None or history_dialogs_busy(self):return
        excluded=[value for value in (
            getattr(self.settings,"recycle_directory",""),
            getattr(self.settings,"external_write_backup_directory",""),
        ) if str(value or "").strip()]
        auditor=OrphanCompanionAuditor(excluded_roots=excluded)
        label=f"Auditing {len(self._audit_roots):,} folder root(s) without writing"
        started,active=self.state.begin_long_operation("recycle_bin",label,resource="io")
        if not started:show_operation_in_progress(self,active); return
        def work(progress,should_cancel):
            return auditor.scan(self._audit_roots,progress=progress,should_cancel=should_cancel)
        self._start_task(label,work,self._audit_complete)

    def _audit_complete(self,report):
        self._finish_task(); self._audit_report=report; self.audit_page=0; self.render_audit()
        held=len(report.items)-len(report.actionable)
        suffix=" • bounded scan limit reached" if report.truncated else ""
        self.operation_status.setText(
            f"Report ready • Verified orphans eligible for Recycle: {len(report.actionable):,} • Ambiguous or protected items held: {held:,}{suffix}"
        )

    def _audit_items(self):
        if self._audit_report is None:return ()
        needle=self.audit_search.text().strip().casefold()
        return tuple(item for item in self._audit_report.items if not needle or needle in " ".join((
            str(item.path),item.classification,item.explanation,
        )).casefold())

    def render_audit(self):
        items=self._audit_items(); size=self._page_size(); pages=max(1,(len(items)+size-1)//size)
        self.audit_page=max(0,min(self.audit_page,pages-1)); visible=items[self.audit_page*size:(self.audit_page+1)*size]
        self.audit_table.setRowCount(len(visible))
        labels={
            "image_missing_or_moved":"Image missing or moved externally",
            "shared_supported_stem":"Another supported image shares the stem",
            "unsupported_image_present":"Unsupported image present",
            "case_or_normalization_mismatch":"Case or name-normalization mismatch",
            "unavailable_placeholder":"Unavailable placeholder",
            "app_managed_excluded":"App-owned backup or recovery area",
        }
        for row,item in enumerate(visible):
            values=(item.path.name,labels.get(item.classification,item.classification),str(item.path.parent),item.explanation)
            for column,value in enumerate(values):
                cell=QTableWidgetItem(value); cell.setToolTip(str(item.path)+"\n"+item.explanation)
                if column==0:
                    cell.setData(Qt.UserRole,str(item.path)); cell.setData(Qt.UserRole+1,item.actionable)
                if not item.actionable:cell.setFlags(cell.flags()&~Qt.ItemIsSelectable)
                self.audit_table.setItem(row,column,cell)
        report=self._audit_report
        errors=len(report.errors) if report is not None else 0
        self.audit_status.setText(
            f"Report items: {len(items):,} • Files scanned: {getattr(report,'files_scanned',0):,} • Folders: {getattr(report,'directories_scanned',0):,} • Read errors: {errors:,}. The report made no changes."
        )
        self.audit_page_label.setText(f"Page {self.audit_page+1} of {pages}" if items else "No report items")
        self.audit_previous.setEnabled(self.audit_page>0); self.audit_next.setEnabled(self.audit_page+1<pages)
        self._update_orphan_actions()

    def _reset_audit_page(self):
        self.audit_page=0; self.render_audit()

    def _change_audit_page(self,direction):
        self.audit_page=max(0,self.audit_page+int(direction)); self.render_audit()

    def _update_orphan_actions(self):
        self.recycle_orphans_button.setEnabled(bool(self._selected_orphan_paths()) and self._task is None and not history_dialogs_busy(self))

    def recycle_selected_orphans(self):
        if self._task is not None or history_dialogs_busy(self):return
        paths=self._selected_orphan_paths()
        if not paths:return
        detail="\n".join(str(path) for path in paths)
        if not self._confirm_exact(
            "Move Verified Orphan Companions to Recycle Bin",
            f"Move exactly {len(paths):,} selected verified orphan companion(s) to Recycle Bin?\n\nNo file will be permanently deleted. Each move keeps its original path and can be restored.",
            detail,"Move to Recycle Bin",
        ):return
        label=f"Moving {len(paths):,} verified orphan companion(s) to Recycle Bin"
        started,active=self.state.begin_long_operation("recycle_bin",label,resource="io")
        if not started:show_operation_in_progress(self,active); return
        def work(progress,should_cancel):
            completed=[]; errors=[]; cancelled=False
            for index,path in enumerate(paths,1):
                if should_cancel():cancelled=True; break
                try:
                    plan=self.state.recycle.preview_recycle(path,source_root=path.parent,kind="orphan",should_cancel=should_cancel)
                    if not plan.ready:raise ValueError("; ".join(plan.errors))
                    self.state.recycle.apply_recycle(plan,reason="move verified orphan companion to Recycle Bin",should_cancel=should_cancel)
                    completed.append(str(path))
                except InterruptedError:cancelled=True; break
                except Exception as error:errors.append(f"{path.name}: {error}")
                progress(index,len(paths),path.name)
            return {"completed":completed,"errors":errors,"cancelled":cancelled,"kind":"orphan"}
        self._start_task(label,work,self._orphan_move_complete)

    def _orphan_move_complete(self,result):
        moved={Path(value) for value in result.get("completed",())}
        self._finish_task()
        if self._audit_report is not None and moved:
            self._audit_report=type(self._audit_report)(
                tuple(item for item in self._audit_report.items if item.path not in moved),
                self._audit_report.files_scanned,self._audit_report.directories_scanned,
                self._audit_report.truncated,self._audit_report.errors,
            )
        self.render_audit(); self.refresh_records()
        if moved:self.changed.emit()
        errors=list(result.get("errors",()))
        self.operation_status.setText(
            ("Paused safely • " if result.get("cancelled") else "")+
            f"Moved {len(moved):,} verified orphan(s) • {len(errors):,} need attention"
        )
        if errors:QMessageBox.warning(self,"Some orphan companions need attention","\n".join(errors[:8]))

    def _set_enabled(self,enabled):
        self.tabs.setEnabled(enabled); self._update_recycle_actions(); self._update_orphan_actions()

    def cancel_work(self):
        if self._task is None:return
        self.state.request_long_operation_cancel("recycle_bin")
        self._task.cancel(); self.cancel_button.setText("Cancelling…"); self.cancel_button.setEnabled(False)

    def _finish_task(self):
        self.state.finish_long_operation("recycle_bin"); self._task=None
        self.progress.hide(); self.cancel_button.hide(); self.cancel_button.setText("Cancel Safely"); self.cancel_button.setEnabled(True)
        self.tabs.setEnabled(True); self._update_recycle_actions(); self._update_orphan_actions()
        if self._close_after_work:QTimer.singleShot(0,self._notify_background_work_finished)

    def _task_cancelled(self):
        label=self._operation_kind; self._finish_task(); self.operation_status.setText(f"{label} cancelled safely; no uncommitted file change was kept.")
        self.refresh_records()
        self._notify_background_work_finished()

    def _task_failed(self,message):
        label=self._operation_kind; self._finish_task(); self.operation_status.setText(f"{label} needs attention")
        self.refresh_records(); QMessageBox.warning(self,"Recycle operation needs attention",message)
        self._notify_background_work_finished()

    def reject(self):
        if close_history_dialogs(self) or self._task is not None:
            self._close_after_work=True
            self.cancel_work(); return
        super().reject()

    def closeEvent(self,event):
        if close_history_dialogs(self) or self._task is not None:
            self._close_after_work=True
            self.cancel_work();event.ignore();return
        super().closeEvent(event)

    def _notify_background_work_finished(self):
        self._update_recycle_actions();self._update_orphan_actions()
        if self._close_after_work and self._task is None and not history_dialogs_busy(self):
            self._close_after_work=False
            super().reject()
