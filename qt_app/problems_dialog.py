from __future__ import annotations

from pathlib import Path
import uuid

from PySide6.QtCore import QThreadPool,QTimer,QUrl,Signal,Qt
from PySide6.QtGui import QDesktopServices
from PySide6.QtWidgets import (
    QDialog,QDialogButtonBox,QFileDialog,QFrame,QGroupBox,QHBoxLayout,QLabel,
    QMessageBox,QProgressBar,QPushButton,QScrollArea,QSplitter,QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core import CharacterDatabase,DatasetScope,ImageRecord,OrganizationRouter
from core.library_storage import LibraryStorageInspector
from core.paths import USER_DATA_DIR
from .components import bind_status_footer,compact_button,set_progress_bar
from .gallery import GalleryScaleControl,ImageCardGrid,content_safety_policy,filter_content_paths
from .image_data_editor import ImageDataEditor
from .modal_feedback import FeedbackEnabledDialog
from .workers import AsyncCallTask,start_qt_delivery_task
from .history_workflow import open_organization_history, history_dialogs_busy, close_history_dialogs


class ProblemsDialog(FeedbackEnabledDialog):
    """Safety-aware, paged recovery gallery for images set aside by workflows."""

    feedback_workflow = "Problems recovery"

    changed=Signal(); library_paths_changed=Signal(object); force_ai_requested=Signal(object,str); content_ratings_requested=Signal(object); edit_tag_requested=Signal(str,object); create_tag_requested=Signal(str,object)

    def __init__(self,state,parent=None):
        super().__init__(parent); self.state=state; self.settings=state.settings
        self.current_path=None; self.images=[]; self.page=0; self._scan_task=None
        self._operation_task=None; self._operation_previous_count=""
        self._operation_control_states={}; self._close_after_operation=False
        self.pool=QThreadPool.globalInstance()
        self.setWindowTitle("Problems"); self.resize(1180,760); self.setMinimumSize(900,600)
        root=QVBoxLayout(self)
        title=QLabel("Problems"); title.setObjectName("pageTitle"); root.addWidget(title)

        toolbar=QHBoxLayout(); self.location=QLabel(); self.location.setObjectName("mutedLabel")
        self.location.setTextInteractionFlags(Qt.TextSelectableByMouse); toolbar.addWidget(self.location,1)
        choose=QPushButton("Choose Folder…"); choose.clicked.connect(self.choose_folder); self.choose_button=choose
        refresh=QPushButton("Refresh"); refresh.clicked.connect(self.refresh); self.refresh_button=refresh
        toolbar.addWidget(choose); toolbar.addWidget(refresh); root.addLayout(toolbar)
        self.activity_status=QLabel("",self); self.activity_status.setWordWrap(True); self.activity_status.setObjectName("mutedLabel"); self.activity_status.setAccessibleName("Problems background activity"); self.activity_status.hide()
        self.progress=QProgressBar(); set_progress_bar(self.progress,unknown_text="Scanning…"); self.progress.hide(); root.addWidget(self.progress)
        self._activity_footer=bind_status_footer(
            self,self.activity_status,self.progress,title="Problems activity",
        )

        splitter=QSplitter(Qt.Horizontal); root.addWidget(splitter,1)
        gallery_side=QWidget(); gallery_layout=QVBoxLayout(gallery_side); gallery_layout.setContentsMargins(0,0,8,0)
        gallery_status=QHBoxLayout(); self.count=QLabel("Choose a Problems folder to begin."); self.count.setObjectName("mutedLabel")
        self.density=GalleryScaleControl(self.settings.gallery_page_size,self.settings.gallery_card_width)
        self.density.changed.connect(self.density_changed)
        gallery_status.addWidget(self.count,1); gallery_status.addWidget(self.density); gallery_layout.addLayout(gallery_status)
        self.gallery_scroll=QScrollArea(); self.gallery_scroll.setWidgetResizable(True); self.gallery_scroll.setFrameShape(QFrame.NoFrame)
        self.gallery=ImageCardGrid(); self.gallery.chosen.connect(self.select_path); self.gallery.edit_requested.connect(self.edit_path); self.gallery.force_ai_requested.connect(self.force_ai_requested)
        self.gallery.file_action_requested.connect(self._gallery_file_action)
        self.gallery_scroll.setWidget(self.gallery); gallery_layout.addWidget(self.gallery_scroll,1)
        pager=QHBoxLayout(); self.previous=QPushButton("Previous Page"); self.previous.clicked.connect(lambda:self.change_page(-1))
        self.page_label=QLabel("No images"); self.page_label.setAlignment(Qt.AlignCenter)
        self.next=QPushButton("Next Page"); self.next.clicked.connect(lambda:self.change_page(1))
        pager.addWidget(self.previous); pager.addWidget(self.page_label,1); pager.addWidget(self.next); gallery_layout.addLayout(pager)
        splitter.addWidget(gallery_side)

        self.context=QWidget(); context_layout=QVBoxLayout(self.context); context_layout.setContentsMargins(8,0,0,0)
        self.name=QLabel("Select an image"); self.name.setObjectName("sectionTitle"); self.name.setWordWrap(True); context_layout.addWidget(self.name)
        recovery=QGroupBox("Recovery options"); recovery_layout=QVBoxLayout(recovery)
        self.details=QLabel("Select an image to review its data or recovery options."); self.details.setWordWrap(True)
        self.details.setTextInteractionFlags(Qt.TextSelectableByMouse); recovery_layout.addWidget(self.details)
        self.edit_button=compact_button(QPushButton("Edit Image Data…"),primary=True); self.edit_button.clicked.connect(self.edit_current)
        self.repair_button=compact_button(QPushButton("Repair in Generate…")); self.repair_button.clicked.connect(self.repair_current)
        self.open_button=compact_button(QPushButton("Open Externally")); self.open_button.clicked.connect(self.open_external)
        self.restore_button=compact_button(QPushButton("Review Location History…")); self.restore_button.clicked.connect(self.restore_original)
        self.organize_button=compact_button(QPushButton("Organize into Library…")); self.organize_button.clicked.connect(self.organize)
        recovery_layout.addWidget(self.edit_button,0,Qt.AlignLeft); recovery_layout.addWidget(self.repair_button,0,Qt.AlignLeft)
        recovery_layout.addWidget(self.open_button,0,Qt.AlignLeft)
        recovery_layout.addStretch(1); recovery_layout.addWidget(self.restore_button,0,Qt.AlignLeft); recovery_layout.addWidget(self.organize_button,0,Qt.AlignLeft)
        context_layout.addWidget(recovery,1); splitter.addWidget(self.context)
        splitter.setStretchFactor(0,7); splitter.setStretchFactor(1,3); splitter.setSizes((820,360))

        buttons=QDialogButtonBox(QDialogButtonBox.Close); buttons.rejected.connect(self.reject); root.addWidget(buttons)
        self._set_actions(False); self.context.hide(); self.refresh()

    def _root(self) -> Path:
        return Path(self.settings.problems_directory)

    def choose_folder(self):
        selected=QFileDialog.getExistingDirectory(self,"Choose Problems folder",self.settings.problems_directory or str(Path.cwd()))
        if not selected: return
        self.settings.problems_directory=selected; self.settings.save(); self.refresh()

    def refresh(self):
        if self._scan_task is not None or self._operation_task is not None or history_dialogs_busy(self): return
        root=self._root(); self.current_path=None; self.context.hide(); self._set_actions(False)
        if not root.is_dir():
            self.images=[]; self.location.setText("No valid Problems folder is configured.")
            self.count.setText("Choose a Problems folder to begin."); self.render_page(); return
        self.location.setText(str(root)); self.activity_status.setText("Scanning Problems in the background…"); self.activity_status.show()
        set_progress_bar(self.progress,unknown_text="Scanning…"); self.progress.show(); self._show_activity_footer(); self.refresh_button.setEnabled(False); self.choose_button.setEnabled(False)
        task=AsyncCallTask(lambda:list(DatasetScope(root,include_subfolders=True).iter_images())); self._scan_task=task
        task.signals.complete.connect(self._scan_complete); task.signals.failed.connect(self._scan_failed); self.pool.start(task)

    def _scan_complete(self,images):
        self._scan_task=None; self.activity_status.hide(); self.progress.hide(); self.refresh_button.setEnabled(True); self.choose_button.setEnabled(True)
        self.images=[Path(value) for value in images]; self.page=0; self.render_page()
        self._close_after_finished_operation()

    def _scan_failed(self,message):
        self._scan_task=None; self.activity_status.hide(); self.progress.hide(); self.refresh_button.setEnabled(True); self.choose_button.setEnabled(True)
        self.images=[]; self.render_page()
        QMessageBox.critical(self,"Could not read Problems",message)
        self._close_after_finished_operation()

    def visible_paths(self):
        visible,_=filter_content_paths(self.images); size=int(self.density.current_data()[0]); start=self.page*size
        return visible[start:start+size]

    def render_page(self):
        visible,_hidden=filter_content_paths(self.images); page_size,card_width=self.density.current_data(); page_size=int(page_size)
        pages=max(1,(len(visible)+page_size-1)//page_size); self.page=min(self.page,pages-1)
        paths=self.visible_paths(); width=max(400,self.gallery_scroll.viewport().width())
        policy=content_safety_policy(); missing_ratings=[]
        if policy is not None and (
            self.settings.gallery_hidden_ratings or self.settings.gallery_content_level!="explicit"
            or getattr(self.settings,"gallery_blur_unknown",True)
        ):
            missing_ratings=policy.unclassified(paths)
        columns=max(2,min(12,width//(max(112,int(card_width))+14)))
        self.gallery.set_paths(
            paths,columns=columns,card_width=int(card_width),allow_edit=True,active=self.current_path,
            configure=lambda _path,card:card.set_generation_actions(True),
            empty_text=(
                "Previews without a content rating remain protected."
                if missing_ratings else "No recoverable images are in this folder."
            ),
        )
        if missing_ratings: QTimer.singleShot(0,lambda values=missing_ratings:self.content_ratings_requested.emit(values))
        self.count.setText(f"{len(visible):,} image group(s)")
        self.page_label.setText(f"Page {self.page+1} of {pages}" if visible else "No problem images")
        self.previous.setEnabled(self.page>0); self.next.setEnabled(self.page+1<pages)
        self.previous.setVisible(pages>1); self.next.setVisible(pages>1); self.page_label.setVisible(pages>1)
        self.density.setVisible(bool(visible))

    def density_changed(self,page_size=None,card_width=None):
        if page_size is None or card_width is None: page_size,card_width=self.density.current_data()
        self.settings.gallery_page_size=int(page_size); self.settings.gallery_card_width=int(card_width); self.settings.save()
        self.page=0; self.render_page()

    def resizeEvent(self,event):
        super().resizeEvent(event)
        if self.images: QTimer.singleShot(0,self.render_page)

    def change_page(self,direction):
        self.page=max(0,self.page+int(direction)); self.current_path=None; self.context.hide(); self.render_page()

    def select_path(self,value: str):
        path=Path(value); self.current_path=path; self.context.show(); self.name.setText(path.name)
        for card_path,card in self.gallery.cards.items(): card.set_active_selected(card_path==str(path))
        reason,original=self._recorded_move_context(path)
        self.details.setText(
            f"Current folder\n{path.parent}\n\nFile\n{path.name}\n\n"+
            (f"Recorded original location\n{original}" if original else "No reversible move is recorded. The image can still be edited or organized into the default library.")+
            (f"\n\nRecorded move reason\n{reason}" if reason else "")
        )
        self._set_actions(True,can_restore=bool(original))

    def _recorded_move_context(self,path: Path) -> tuple[str,str]:
        move=self.state.moves.record_for_path(path)
        if move is None:
            return "",""
        original=next((str(entry["from"]) for entry in move.files
                       if Path(entry["to"]).resolve()==path.resolve()),"")
        return (str(getattr(move,"reason","") or "") if original else "",original)

    def _set_actions(self,enabled: bool,*,can_restore: bool=False):
        enabled=bool(enabled and self._operation_task is None and self._scan_task is None
                     and not self._close_after_operation and not history_dialogs_busy(self))
        for control in (self.edit_button,self.repair_button,self.open_button,self.organize_button): control.setEnabled(enabled)
        self.restore_button.setEnabled(enabled and can_restore)

    def _restore_current_actions(self,path: Path) -> None:
        if self.current_path!=path:
            self._set_actions(False)
            return
        self._set_actions(True,can_restore=self.state.moves.record_for_path(path) is not None)

    def _start_background_operation(self,path: Path,label: str,work,on_complete,on_failed):
        """Run one selected-image action without holding the Qt event loop."""
        if self._operation_task is not None or history_dialogs_busy(self):
            return False
        self._set_actions(False)
        controls=(
            self.choose_button,self.refresh_button,self.density,
            self.previous,self.next,
        )
        self._operation_control_states={
            control:control.isEnabled() for control in controls
        }
        self._operation_gallery_enabled=self.gallery.isEnabled()
        self.gallery.setEnabled(False)
        for control in controls:
            control.setEnabled(False)
        self._operation_previous_count=self.count.text(); self.activity_status.setText(label); self.activity_status.show()
        set_progress_bar(self.progress,unknown_text=label); self.progress.show(); self._show_activity_footer()
        task=AsyncCallTask(work); self._operation_task=task
        task.signals.complete.connect(
            lambda result,owner=task,target=path:
            self._background_operation_complete(owner,target,result,on_complete),
        )
        task.signals.failed.connect(
            lambda message,owner=task,target=path:
            self._background_operation_failed(owner,target,message,on_failed),
        )
        start_qt_delivery_task(self.pool,task)
        return True

    def _finish_background_operation(self,task,path: Path) -> bool:
        if task is not self._operation_task:
            return False
        self._operation_task=None; self.activity_status.hide(); self.progress.hide()
        self.count.setText(self._operation_previous_count); self._operation_previous_count=""
        self.gallery.setEnabled(getattr(self,"_operation_gallery_enabled",True))
        for control,enabled in self._operation_control_states.items():
            control.setEnabled(enabled)
        self._operation_control_states={}
        self._restore_current_actions(path)
        return True

    def _show_activity_footer(self):
        self._activity_footer._attach()
        if self._activity_footer.footer is not None:
            self._activity_footer.footer.show()
        if self._activity_footer.row is not None:
            self._activity_footer.row.show()

    def _background_operation_complete(self,task,path: Path,result,on_complete) -> None:
        if not self._finish_background_operation(task,path):
            return
        on_complete(result,path==self.current_path)
        self._close_after_finished_operation()

    def _background_operation_failed(self,task,path: Path,message,on_failed) -> None:
        if not self._finish_background_operation(task,path):
            return
        on_failed(str(message),path==self.current_path)
        self._close_after_finished_operation()

    def _close_after_finished_operation(self) -> None:
        if (self._close_after_operation and self._operation_task is None
                and self._scan_task is None and not history_dialogs_busy(self)):
            self._close_after_operation=False
            super().reject()

    def reject(self):
        if close_history_dialogs(self) or self._operation_task is not None or self._scan_task is not None:
            self._close_after_operation=True
            self.activity_status.setText(
                "Finishing the current recovery action before closing…"
            )
            self.activity_status.show(); self._show_activity_footer()
            return
        super().reject()

    def closeEvent(self,event):
        if close_history_dialogs(self) or self._operation_task is not None or self._scan_task is not None:
            self._close_after_operation=True
            self.activity_status.setText(
                "Finishing the current recovery action before closing…"
            )
            self.activity_status.show(); self._show_activity_footer()
            event.ignore()
            return
        super().closeEvent(event)

    def edit_path(self,value: str):
        self.select_path(value); QTimer.singleShot(0,self.edit_current)

    def edit_current(self):
        if not self.current_path: return
        path=Path(self.current_path)
        self._start_background_operation(
            path,"Loading image data in the background…",
            lambda:ImageRecord(path),
            self._edit_record_loaded,self._edit_record_failed,
        )

    def _edit_record_loaded(self,record,current: bool) -> None:
        if not current or self._close_after_operation:
            return
        dialog=ImageDataEditor(
            record,settings=self.settings,parent=self,allow_ai=False,
            knowledge=self.state.knowledge,
            content_safety=content_safety_policy(),
        )
        dialog.edit_tag_requested.connect(self.edit_tag_requested)
        dialog.create_tag_requested.connect(self.create_tag_requested)
        if dialog.exec()==QDialog.Accepted:
            self.render_page(); self.changed.emit()

    def _edit_record_failed(self,message: str,current: bool) -> None:
        if current and not self._close_after_operation:
            QMessageBox.critical(self,"Could not inspect image data",message)

    def open_external(self):
        if self.current_path: QDesktopServices.openUrl(QUrl.fromLocalFile(str(self.current_path)))

    def _generation_source_ready(self,path: Path) -> bool:
        if (self._scan_task is not None or self._operation_task is not None
                or self._close_after_operation or not self.isVisible()
                or path not in self.visible_paths()):
            return False
        availability=LibraryStorageInspector().inspect_image(path)
        if not path.is_file() or not availability.readable:
            QMessageBox.information(self,"Problem image unavailable",
                "Restore local access to this image, then refresh Problems before opening Generate.")
            return False
        return True

    def _open_generation_for(self,path: Path,*,animate: bool=False) -> None:
        if not self._generation_source_ready(path):
            return
        self.select_path(str(path))
        if animate:
            from .video_generation_workflow import open_video_generation_workspace
            open_video_generation_workspace(self,source_path=path)
            return
        reason,original=self._recorded_move_context(path)
        from .generation_workflow import open_generation_workspace
        open_generation_workspace(self,paths=(path,),remix=True,repair_context={
            "reason":reason,"original_location":original,"source_workspace":"Problems",
        })

    def repair_current(self) -> None:
        if self.current_path is not None:
            self._open_generation_for(Path(self.current_path))

    def _gallery_file_action(self,paths,action_id: str) -> None:
        if not paths or action_id not in {"remix","animate_this_image"}:
            return
        self._open_generation_for(Path(paths[0]),animate=action_id=="animate_this_image")

    def restore_original(self):
        if not self.current_path or self._operation_task is not None or self._scan_task is not None or history_dialogs_busy(self): return
        path=Path(self.current_path); move=self.state.moves.record_for_path(path)
        if move is None: return
        if not move.operation_id:
            QMessageBox.information(self,"Recorded location unavailable","This image has no exact recorded move identity."); return
        dialog=open_organization_history(self,operation_targets=(move.operation_id,),
            on_outcomes=self._location_restored)
        dialog.busyChanged.connect(lambda _busy:self._restore_current_actions(path))
        return dialog

    def _location_restored(self,outcomes):
        completed=tuple(value for value in outcomes if value.status=="complete")
        if not completed:return
        paths=tuple(dict.fromkeys(Path(path) for value in completed
            for path in (value.previous_path,value.current_path) if path is not None))
        removed={value.previous_path for value in completed if value.previous_path!=value.current_path}
        self.images=[path for path in self.images if path not in removed]
        root=self._root().absolute()
        for value in completed:
            path=value.current_path
            if path is not None and path.absolute().is_relative_to(root) and path not in self.images:
                self.images.append(path)
        if self.current_path in removed:
            self.current_path=None; self.context.hide(); self._set_actions(False)
        self.render_page()
        self.library_paths_changed.emit(paths)

    def _notify_background_work_finished(self):
        self._close_after_finished_operation()

    def _restore_complete(self,_result,current: bool) -> None:
        self.changed.emit()
        if not self._close_after_operation:
            self.refresh()

    def _restore_failed(self,message: str,current: bool) -> None:
        if current:
            QMessageBox.critical(self,"Could not restore image",message)

    def organize(self):
        if not self.current_path: return
        working=Path(self.settings.working_directory)
        if not working.is_dir(): QMessageBox.information(self,"Configure the default library","Choose a valid default library in Setup first."); return
        path=Path(self.current_path)
        self._start_background_operation(
            path,"Preparing organization preview in the background…",
            lambda:self._organization_suggestion(path,working),
            self._organization_preview_ready,self._organization_preview_failed,
        )

    def _organization_suggestion(self,path: Path,working: Path):
        database=CharacterDatabase(USER_DATA_DIR); database.load()
        return OrganizationRouter(
            working,database,self.settings.organization_profile,
        ).suggest(ImageRecord(path))

    def _organization_preview_ready(self,suggestion,current: bool) -> None:
        if not current or self._close_after_operation:
            return
        destination=suggestion.destination
        details=f"Move this image group into the default library at:\n\n{destination}\n\nConfidence: {suggestion.confidence*100:.0f}%"
        if QMessageBox.question(self,"Organize problem image",details)!=QMessageBox.Yes: return
        path=Path(self.current_path)
        self._start_background_operation(
            path,"Organizing image group in the background…",
            lambda:self.state.moves.move(
                path,destination,reason="organize from Problems",
                group_id="problems-"+uuid.uuid4().hex,
            ),
            self._organization_complete,self._organization_failed,
        )

    def _organization_preview_failed(self,message: str,current: bool) -> None:
        if current and not self._close_after_operation:
            QMessageBox.critical(self,"Could not prepare image organization",message)

    def _organization_complete(self,_result,current: bool) -> None:
        self.changed.emit()
        if not self._close_after_operation:
            self.refresh()

    def _organization_failed(self,message: str,current: bool) -> None:
        if current:
            QMessageBox.critical(self,"Could not organize image",message)
