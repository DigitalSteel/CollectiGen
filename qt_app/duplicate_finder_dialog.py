from __future__ import annotations

import hashlib
from dataclasses import replace
from datetime import datetime
from pathlib import Path
from typing import Callable

from PySide6.QtCore import QThreadPool,QTimer,QUrl,Qt,Signal
from PySide6.QtGui import QDesktopServices,QImage,QPixmap
from PySide6.QtWidgets import (
    QAbstractItemView,QApplication,QButtonGroup,QCheckBox,QComboBox,QDialog,
    QDialogButtonBox,
    QFileDialog,QFrame,QGridLayout,QHBoxLayout,QHeaderView,QLabel,QLineEdit,
    QGraphicsBlurEffect,QMessageBox,QMenu,QProgressBar,QPushButton,QRadioButton,QSpinBox,QSplitter,
    QTableWidget,QTableWidgetItem,QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core import (
    DEFAULT_SIMILARITY_PERCENT,DatasetScope,DuplicateScope,
    DuplicateSearchReport,TransferRecoveryRequired,duplicate_scope_item,
    normalize_tag,relation_between,
)
from core.image_workflow_state import ImageKnowledgeCoverage

from .components import DisclosureButton,bind_status_footer,compact_button,set_progress_bar
from .confidence import set_visual_status
from .gallery import ImageKnowledgeBadge,ProtectedImageView,content_safety_policy
from .modal_feedback import FeedbackEnabledDialog
from .workers import AsyncCallTask,CoordinatedCallTask


class DistinctHistoryDialog(QDialog):
    """Review and deliberately remove durable reviewed-distinct decisions."""

    changed=Signal(str)

    def __init__(self,state,parent=None):
        super().__init__(parent); self.state=state; self._task=None
        self._preview_generation=0; self._preview_tasks=[]; self.rows=()
        self.setWindowTitle("Reviewed Distinct History"); self.resize(980,680)
        root=QVBoxLayout(self)
        title=QLabel("Reviewed Distinct History"); title.setObjectName("pageTitle")
        root.addWidget(title)
        intro=QLabel(
            "These pairs were deliberately kept as distinct. Select a pair to "
            "review it, then remove only that mark if it should appear in duplicate results again."
        )
        intro.setWordWrap(True); root.addWidget(intro)
        self.table=QTableWidget(0,3)
        self.table.setHorizontalHeaderLabels(("Marked","First image","Second image"))
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.verticalHeader().hide()
        self.table.horizontalHeader().setSectionResizeMode(0,QHeaderView.ResizeToContents)
        self.table.horizontalHeader().setSectionResizeMode(1,QHeaderView.Stretch)
        self.table.horizontalHeader().setSectionResizeMode(2,QHeaderView.Stretch)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        root.addWidget(self.table,1)
        previews=QSplitter(Qt.Horizontal)
        policy=content_safety_policy()
        self.left=ProtectedImageView(policy=policy,settings=state.settings)
        self.right=ProtectedImageView(policy=policy,settings=state.settings)
        self.left.setMinimumHeight(180); self.right.setMinimumHeight(180)
        previews.addWidget(self.left); previews.addWidget(self.right); root.addWidget(previews,1)
        self.reason=QLabel("Select a reviewed pair."); self.reason.setWordWrap(True)
        self.reason.setObjectName("mutedLabel"); root.addWidget(self.reason)
        controls=QHBoxLayout(); self.remove=compact_button(QPushButton("Remove Mark"),primary=True)
        self.remove.setEnabled(False); self.remove.clicked.connect(self._remove_selected)
        controls.addWidget(self.remove); controls.addStretch(1)
        buttons=QDialogButtonBox(QDialogButtonBox.Close); buttons.rejected.connect(self.reject)
        controls.addWidget(buttons); root.addLayout(controls)
        self._refresh()

    def _refresh(self):
        self.rows=self.state.duplicates.decisions.history()
        self.table.setRowCount(len(self.rows))
        for row,decision in enumerate(self.rows):
            try:
                marked=datetime.fromisoformat(decision.created_at.replace("Z","+00:00")).astimezone().strftime("%Y-%m-%d %H:%M")
            except ValueError:marked=decision.created_at or "Unknown"
            for column,text in enumerate((marked,decision.first_path.name,decision.second_path.name)):
                item=QTableWidgetItem(text); item.setData(Qt.UserRole,decision.key)
                item.setToolTip(
                    f"{decision.first_path}\n{decision.second_path}\n{decision.reason}"
                )
                self.table.setItem(row,column,item)
        if self.rows:self.table.selectRow(0)
        else:
            self.left.clear(); self.right.clear(); self.reason.setText("No reviewed-distinct marks are recorded.")
            self.remove.setEnabled(False)

    def _selected(self):
        rows={item.row() for item in self.table.selectedItems()}
        return self.rows[min(rows)] if rows and min(rows)<len(self.rows) else None

    def _selection_changed(self):
        decision=self._selected(); self._preview_generation+=1
        for task in self._preview_tasks:task.cancel()
        self._preview_tasks=[]
        if decision is None:
            self.left.clear(); self.right.clear(); self.remove.setEnabled(False); return
        self.reason.setText(f"Reason: {decision.reason}")
        self.remove.setEnabled(self._task is None)
        generation=self._preview_generation
        for side,path in (("left",decision.first_path),("right",decision.second_path)):
            preview=self.left if side=="left" else self.right
            preview.clear("Loading preview…"); preview.path=Path(path)
            task=AsyncCallTask(lambda g=generation,s=side,p=Path(path):(g,s,p,QImage(str(p))))
            task.signals.complete.connect(self._preview_ready); self._preview_tasks.append(task)
            QThreadPool.globalInstance().start(task)

    def _preview_ready(self,result):
        generation,side,path,image=result
        if generation!=self._preview_generation:return
        preview=self.left if side=="left" else self.right
        preview.set_image(QPixmap.fromImage(image) if not image.isNull() else QPixmap(),path)

    def _remove_selected(self):
        decision=self._selected()
        if decision is None or self._task is not None:return
        box=QMessageBox(
            QMessageBox.Question,"Remove Reviewed-Distinct Mark",
            f"Let this pair appear in future duplicate results again?\n\n"
            f"{decision.first_path}\n{decision.second_path}\n\nNo image or Image Knowledge will change.",
            QMessageBox.NoButton,self,
        )
        remove=box.addButton("Remove Mark",QMessageBox.AcceptRole)
        cancel=box.addButton(QMessageBox.Cancel); box.setDefaultButton(cancel)
        box.setEscapeButton(cancel); box.exec()
        if box.clickedButton() is not remove:return
        task=CoordinatedCallTask(
            self.state.jobs,"Removing reviewed-distinct mark",
            lambda _job:self.state.duplicates.decisions.remove_key(decision.key),
            resource="io",
        )
        self._task=task; self.remove.setEnabled(False)
        task.signals.complete.connect(lambda removed,key=decision.key:self._removed(key,removed))
        task.signals.failed.connect(self._failed); task.start()

    def _removed(self,key,removed):
        self._task=None
        if removed:self.changed.emit(key)
        self._refresh()

    def _failed(self,message):
        self._task=None; self.remove.setEnabled(self._selected() is not None)
        QMessageBox.warning(self,"Distinct mark was not changed",str(message))

    def reject(self):
        if self._task is not None:self._task.cancel()
        for task in self._preview_tasks:task.cancel()
        super().reject()


class DuplicateFinderDialog(FeedbackEnabledDialog):
    """Scoped two-image duplicate review with explicit safe resolution."""

    feedback_workflow="Duplicate finding and safe resolution"
    edit_requested=Signal(str)
    organize_requested=Signal(str)
    changed=Signal(object)

    GROUP_PAGE_SIZE=24
    def __init__(
        self,state,parent=None,*,current_paths=(),selected_paths=(),
        current_library_id: str="",
        frozen_scope: DuplicateScope|None=None,
        frozen_mode: str="training",
        initial_report: DuplicateSearchReport|None=None,
        include_similar: bool=True,
        incoming_identities: frozenset[str]|None=None,
        frozen_currentness: Callable[[],bool]|None=None,
        frozen_authority_currentness: Callable[[],bool]|None=None,
    ):
        super().__init__(parent)
        self.state=state; self.settings=state.settings
        self.current_paths=tuple(dict.fromkeys(Path(value) for value in current_paths))
        self.initial_selected_paths=tuple(dict.fromkeys(Path(value) for value in selected_paths))
        self.current_library_id=str(current_library_id or "")
        self.frozen_scope=frozen_scope
        self.frozen_mode=(
            str(frozen_mode) if frozen_scope is not None else ""
        )
        if self.frozen_mode not in {"","training","preflight"}:
            raise ValueError("Frozen duplicate review mode must be training or preflight.")
        self.training_mode=self.frozen_mode=="training"
        self.include_similar=bool(include_similar)
        self.incoming_identities=(
            None if incoming_identities is None else frozenset(incoming_identities)
        )
        self.frozen_currentness=frozen_currentness
        self.frozen_authority_currentness=frozen_authority_currentness
        self._close_when_idle=False
        self.pool=QThreadPool.globalInstance(); self.report=None
        self._task=None; self._restore_task=None
        self._details_task=None; self._details_generation=0
        self._preview_tasks=[]; self._preview_generation=0
        self._pair_thumbnail_task=None; self._pair_thumbnail_generation=0
        self._pair_thumbnail_labels={}
        self._details={}; self._group_page=0
        self._active_group_id=""; self._active_pair_ids=()
        self._selected=set(); self._folder=None
        self._syncing_view=False; self._recycle_context=None

        self.setWindowTitle("Find Duplicate and Similar Images")
        self.resize(1320,900); self.setMinimumSize(1000,700)
        root=QVBoxLayout(self)
        title=QLabel("Find Duplicates"); title.setObjectName("pageTitle")
        root.addWidget(title)
        intro=QLabel(
            "Exact matches and visually similar images use separate evidence. "
            "Nothing is removed automatically."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel"); root.addWidget(intro)
        self._build_scope(root); self._build_results(root); self._build_actions(root)
        bind_status_footer(
            self,self.search_status,self.progress,self.cancel,title="Duplicate search",
        )
        buttons=QDialogButtonBox(QDialogButtonBox.Close); buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        self._scope_changed(); self._update_actions()
        if self.frozen_scope is not None:
            self.scope.blockSignals(True); self.scope.clear()
            self.scope.addItem(self.frozen_scope.label,self.frozen_mode)
            self.scope.blockSignals(False)
            self.scope.setEnabled(False); self._scope_changed()
            self.lower.setEnabled(False); self.upper.setEnabled(False)
            if self.training_mode:
                self.lower.setToolTip("Training evidence consistently uses the displayed 88–100% range.")
                self.left_choice_panel.hide(); self.right_choice_panel.hide()
                self.history.hide(); self.resolve.hide()
                self.reference_label.hide(); self.reference.hide(); self.recommendation.hide()
                intro.setText("Compare this exact training scope, then close to refresh the plan. Inclusion, roles, reviewed-distinct decisions and recycle choices stay in their owning workflows.")
            else:
                self.setWindowTitle("Review Ingest and Dataset Duplicates")
                title.setText("Review Ingest and Dataset Duplicates")
                self.search.setText("Refresh Frozen Scope")
                self.force.hide()
                intro.setText(
                    "This frozen preflight scope compares the exact submitted "
                    "incoming images only with explicitly included Dataset "
                    "libraries. The regular pair choices, reviewed-distinct, "
                    "currentness, content-safety, organization, and Recycle "
                    "preview rules still apply."
                )
        if initial_report is not None:
            if self.frozen_scope is None or initial_report.scope_id!=self.frozen_scope.id:
                raise ValueError("The initial duplicate report does not match the frozen scope.")
            if (
                bool(initial_report.include_similar)!=self.include_similar
                or initial_report.incoming_identities!=self.incoming_identities
            ):
                raise ValueError("The initial duplicate report does not match the frozen comparison mode.")
            self.lower.setValue(int(round(initial_report.lower_similarity)))
            self.upper.setValue(int(round(initial_report.upper_similarity)))
            self._search_complete(initial_report)
        elif self.frozen_scope is None and self.state.duplicates.has_session_reports():
            QTimer.singleShot(0,self._start_session_restore)
        self._frozen_freshness_timer=QTimer(self)
        self._frozen_freshness_timer.setInterval(300)
        self._frozen_freshness_timer.timeout.connect(self._check_frozen_currentness)
        if self.frozen_mode=="preflight":self._frozen_freshness_timer.start()

    def _build_scope(self,root):
        panel=QFrame(); panel.setObjectName("sectionCard"); grid=QGridLayout(panel)
        grid.addWidget(QLabel("Scope"),0,0)
        self.scope=QComboBox()
        self.scope.addItem("Current Dataset search and filters","current_filter")
        self.scope.addItem("Selected Dataset images","selected")
        self.scope.addItem("All included libraries","included_libraries")
        self.scope.addItem("One named library","named_library")
        self.scope.addItem("One Tag in included libraries","tag")
        self.scope.addItem("All Incoming Images","ingest")
        self.scope.addItem("Choose a folder…","folder")
        self.scope.currentIndexChanged.connect(self._scope_changed)
        self.scope.setAccessibleName("Duplicate search scope")
        grid.addWidget(self.scope,0,1,1,2)
        self.library=QComboBox(); self.library.setAccessibleName("Named library scope")
        for profile in self.settings.library_registry().profiles:
            self.library.addItem(f"{profile.name} • {profile.display_role}",profile.stable_id)
        if self.current_library_id:
            index=self.library.findData(self.current_library_id)
            if index>=0:self.library.setCurrentIndex(index)
        grid.addWidget(self.library,0,3)
        self.tag=QLineEdit(); self.tag.setPlaceholderText("Exact canonical Tag or Other Name…")
        self.tag.setAccessibleName("Tag scope")
        grid.addWidget(self.tag,0,4)
        self.choose_folder=compact_button(QPushButton("Choose Folder…")); self.choose_folder.clicked.connect(self._choose_folder)
        self.folder_label=QLabel("No folder chosen"); self.folder_label.setObjectName("mutedLabel")
        grid.addWidget(self.choose_folder,1,0); grid.addWidget(self.folder_label,1,1,1,4)

        grid.addWidget(QLabel("Similarity range"),2,0)
        self.lower=QSpinBox(); self.lower.setRange(70,100); self.lower.setValue(DEFAULT_SIMILARITY_PERCENT); self.lower.setSuffix("% minimum")
        self.upper=QSpinBox(); self.upper.setRange(70,100); self.upper.setValue(100); self.upper.setSuffix("% maximum")
        self.lower.valueChanged.connect(self._range_changed); self.upper.valueChanged.connect(self._range_changed)
        grid.addWidget(self.lower,2,1); grid.addWidget(self.upper,2,2)
        range_help=QLabel(
            "Exact matches are always included. Lower ranges find more candidates and run more slowly."
        )
        range_help.setWordWrap(True); range_help.setObjectName("mutedLabel")
        range_help.setToolTip(
            "Rotation and center-crop variants participate in the explained visual-similarity score."
        )
        grid.addWidget(range_help,2,3,1,2)
        self.force=QCheckBox("Rebuild fingerprints")
        self.force.setToolTip("Ignore unchanged cache rows for this run. This changes no image, Metadata, or Image Knowledge.")
        self.search=compact_button(QPushButton("Find Duplicates"),primary=True); self.search.clicked.connect(self.start_search)
        self.cancel=compact_button(QPushButton("Cancel Safely")); self.cancel.clicked.connect(self.cancel_work); self.cancel.hide()
        actions=QHBoxLayout(); actions.addWidget(self.force); actions.addStretch(1); actions.addWidget(self.cancel); actions.addWidget(self.search)
        grid.addLayout(actions,3,0,1,5)
        self.progress=QProgressBar(); self.progress.hide(); grid.addWidget(self.progress,4,0,1,5)
        self.search_status=QLabel("Ready to search.")
        self.search_status.setWordWrap(True); self.search_status.setObjectName("mutedLabel")
        grid.addWidget(self.search_status,5,0,1,5)
        root.addWidget(panel)

    def _build_results(self,root):
        splitter=QSplitter(Qt.Horizontal); root.addWidget(splitter,1)
        left=QWidget(); left.setMinimumWidth(250); left_layout=QVBoxLayout(left)
        self.result_summary=QLabel("No duplicate search has run."); self.result_summary.setWordWrap(True)
        self.result_summary.setObjectName("mutedLabel"); left_layout.addWidget(self.result_summary)
        self.groups=QTableWidget(0,1)
        self.groups.setHorizontalHeaderLabels(("Sets",))
        self.groups.setSelectionBehavior(QAbstractItemView.SelectRows); self.groups.setSelectionMode(QAbstractItemView.SingleSelection)
        self.groups.setEditTriggers(QAbstractItemView.NoEditTriggers); self.groups.verticalHeader().hide()
        self.groups.horizontalHeader().setSectionResizeMode(0,QHeaderView.Stretch)
        self.groups.itemSelectionChanged.connect(self._group_selected)
        self.groups.setAccessibleName("Duplicate result sets")
        left_layout.addWidget(self.groups,1)
        group_pager=QHBoxLayout(); self.group_previous=compact_button(QPushButton("Previous")); self.group_next=compact_button(QPushButton("Next"))
        self.group_page_label=QLabel("No sets"); self.group_page_label.setAlignment(Qt.AlignCenter)
        self.group_previous.hide(); self.group_page_label.hide(); self.group_next.hide()
        self.group_previous.clicked.connect(lambda:self._change_group_page(-1)); self.group_next.clicked.connect(lambda:self._change_group_page(1))
        group_pager.addWidget(self.group_previous); group_pager.addWidget(self.group_page_label,1); group_pager.addWidget(self.group_next)
        left_layout.addLayout(group_pager); splitter.addWidget(left)

        right=QFrame(); right.setObjectName("sectionCard"); right_layout=QVBoxLayout(right)
        gallery_header=QHBoxLayout(); self.member_summary=QLabel("Choose a duplicate set")
        self.member_summary.setObjectName("sectionTitle"); gallery_header.addWidget(self.member_summary,1)
        right_layout.addLayout(gallery_header)

        compare_panel=QWidget(); compare_layout=QVBoxLayout(compare_panel)
        self.left_choice=QComboBox(); self.right_choice=QComboBox()
        self.left_choice.hide(); self.right_choice.hide()
        self.left_choice.currentIndexChanged.connect(self._comparison_changed); self.right_choice.currentIndexChanged.connect(self._comparison_changed)
        self.link_views=QCheckBox("Link zoom and pan"); self.link_views.setChecked(True)
        fit=compact_button(QPushButton("Fit Both")); fit.clicked.connect(self._fit_both)
        zoom_out=compact_button(QPushButton("−")); zoom_out.setAccessibleName("Zoom both comparison images out"); zoom_out.clicked.connect(lambda:self._zoom_both(1/1.15))
        zoom_in=compact_button(QPushButton("+")); zoom_in.setAccessibleName("Zoom both comparison images in"); zoom_in.clicked.connect(lambda:self._zoom_both(1.15))
        self.reveal_pair=compact_button(QPushButton("Reveal Temporarily"))
        self.reveal_pair.setAccessibleName("Reveal both comparison images temporarily")
        self.reveal_pair.clicked.connect(self._toggle_pair_reveal); self.reveal_pair.hide()
        view_controls=QHBoxLayout(); view_controls.addWidget(self.link_views); view_controls.addWidget(self.reveal_pair)
        view_controls.addStretch(1); view_controls.addWidget(zoom_out); view_controls.addWidget(zoom_in); view_controls.addWidget(fit)
        compare_layout.addLayout(view_controls)
        compare=QSplitter(Qt.Horizontal)
        policy=content_safety_policy()
        self.left_preview=ProtectedImageView(policy=policy,settings=self.settings)
        self.right_preview=ProtectedImageView(policy=policy,settings=self.settings)
        (
            self.left_preview_frame,self.left_knowledge_frame,
            self.left_knowledge_badge,
        )=self._preview_frame(
            self.left_preview,"Left duplicate image",
        )
        (
            self.right_preview_frame,self.right_knowledge_frame,
            self.right_knowledge_badge,
        )=self._preview_frame(
            self.right_preview,"Right duplicate image",
        )
        for preview in (self.left_preview,self.right_preview):
            preview.protection_changed.connect(self._sync_pair_protection_controls)
        self.left_preview.setMinimumHeight(220); self.right_preview.setMinimumHeight(220)
        compare.addWidget(self.left_preview_frame); compare.addWidget(self.right_preview_frame); compare_layout.addWidget(compare,1)
        choices=QHBoxLayout()
        for side in ("left","right"):
            keep=QRadioButton("Keep"); recycle=QRadioButton("Recycle")
            group=QButtonGroup(self); group.addButton(keep); group.addButton(recycle)
            setattr(self,f"{side}_keep",keep); setattr(self,f"{side}_recycle",recycle)
            setattr(self,f"_{side}_resolution_group",group)
            keep.setChecked(True)
            keep.setAccessibleName(f"Keep {side} duplicate image")
            recycle.setAccessibleName(f"Recycle {side} duplicate image")
            recycle.toggled.connect(
                lambda checked,s=side:self._resolution_changed(s,checked)
            )
            panel=QFrame(); row=QHBoxLayout(panel); row.setContentsMargins(8,2,8,2)
            setattr(self,f"{side}_choice_panel",panel)
            row.addWidget(QLabel(f"{side.title()} image:")); row.addWidget(keep)
            row.addWidget(recycle); row.addStretch(1); choices.addWidget(panel,1)
        compare_layout.addLayout(choices)
        self.pair_evidence=QLabel("Choose two images to compare their exact relation.")
        self.pair_evidence.setWordWrap(True); self.pair_evidence.setObjectName("mutedLabel"); compare_layout.addWidget(self.pair_evidence)
        compare_layout.removeWidget(self.pair_evidence); compare_layout.insertWidget(1,self.pair_evidence)
        self._connect_linked_views()

        self.evidence=QWidget(); evidence_layout=QHBoxLayout(self.evidence)
        evidence_layout.setContentsMargins(0,0,0,0)
        (
            self.left_detail_card,self.left_detail_title,
            self.left_detail_values,
        )=self._detail_card("Left image details")
        (
            self.right_detail_card,self.right_detail_title,
            self.right_detail_values,
        )=self._detail_card("Right image details")
        evidence_layout.addWidget(self.left_detail_card,1)
        evidence_layout.addWidget(self.right_detail_card,1)
        self.evidence.setAccessibleName("Duplicate evidence by image")
        details_row=QHBoxLayout()
        self.details_toggle=DisclosureButton("Show Details","Hide Details",self.evidence)
        self.details_toggle.setAccessibleName("Show or hide duplicate evidence details")
        details_row.addWidget(self.details_toggle); details_row.addStretch(1)
        compare_layout.addLayout(details_row)
        compare_layout.addWidget(self.evidence)
        right_layout.addWidget(compare_panel,1)
        splitter.addWidget(right); splitter.setStretchFactor(0,1); splitter.setStretchFactor(1,3)

    def _preview_frame(self,preview,name):
        frame=QFrame(); frame.setObjectName("imageCardStatus")
        frame.setAccessibleName(name); set_visual_status(frame,"green")
        layout=QGridLayout(frame); layout.setContentsMargins(5,5,5,5)
        knowledge_frame=QFrame(); knowledge_frame.setObjectName("imageCardStatus")
        set_visual_status(knowledge_frame,"neutral")
        knowledge_layout=QGridLayout(knowledge_frame)
        knowledge_layout.setContentsMargins(4,4,4,4)
        knowledge_layout.addWidget(preview,0,0)
        badge=ImageKnowledgeBadge(knowledge_frame)
        badge.setAccessibleName(f"{name} Image Knowledge coverage")
        knowledge_layout.addWidget(badge,0,0,Qt.AlignRight|Qt.AlignTop)
        layout.addWidget(knowledge_frame,0,0)
        side="left" if name.casefold().startswith("left") else "right"
        for surface in (frame,preview,preview.viewer.viewport()):
            surface.setContextMenuPolicy(Qt.CustomContextMenu)
            surface.customContextMenuRequested.connect(
                lambda position,s=side,w=surface:
                self._open_image_menu(s,w.mapToGlobal(position))
            )
        return frame,knowledge_frame,badge

    @staticmethod
    def _detail_card(title):
        card=QFrame(); card.setObjectName("sectionCard")
        layout=QGridLayout(card); heading=QLabel(title); heading.setObjectName("sectionTitle")
        heading.setWordWrap(True); layout.addWidget(heading,0,0,1,2)
        fields={}
        for row,label in enumerate((
            "Relationship","Dimensions","Format and size","Location",
            "Modified","Image Knowledge","Metadata","Provenance","Derivative",
        ),1):
            key=label.casefold().replace(" ","_")
            name=QLabel(label); name.setObjectName("mutedLabel")
            value=QLabel("Checking…"); value.setWordWrap(True)
            value.setTextInteractionFlags(Qt.TextSelectableByMouse)
            layout.addWidget(name,row,0,Qt.AlignTop); layout.addWidget(value,row,1)
            fields[key]=value
        layout.setColumnStretch(1,1)
        return card,heading,fields

    def _image_menu(self,side):
        record=self._record(
            self.left_choice.currentData() if side=="left"
            else self.right_choice.currentData()
        )
        if record is None:return None
        menu=QMenu(self)
        if not self.training_mode:
            edit=menu.addAction("Edit Image Data…")
            edit.triggered.connect(lambda:self.edit_requested.emit(str(record.path)))
            organize=menu.addAction("Organize…")
            organize.setEnabled(bool(record.source_profile_id))
            organize.triggered.connect(lambda:self.organize_requested.emit(str(record.path)))
            menu.addSeparator()
        view=menu.addAction("Open Image")
        view.triggered.connect(
            lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(str(record.path)))
        )
        location=menu.addAction("Open File Location")
        location.triggered.connect(
            lambda:QDesktopServices.openUrl(QUrl.fromLocalFile(str(record.path.parent)))
        )
        copy=menu.addAction("Copy File Path")
        copy.triggered.connect(
            lambda:QApplication.clipboard().setText(str(record.path))
        )
        return menu

    def _open_image_menu(self,side,global_position):
        menu=self._image_menu(side)
        if menu is None:return
        menu.exec(global_position)

    def _build_actions(self,root):
        recommendation=QFrame(); recommendation.setObjectName("sectionCard"); layout=QGridLayout(recommendation)
        self.reference_label=QLabel("Reference"); self.reference_label.hide()
        self.reference=QComboBox(); self.reference.setAccessibleName("Explicit reference image")
        self.reference.currentIndexChanged.connect(self._reference_changed); self.reference.hide()
        self.recommendation=QLabel("Choose a set to review.")
        self.recommendation.setWordWrap(True); self.recommendation.setObjectName("mutedLabel"); layout.addWidget(self.recommendation,0,0,1,3)
        self.history=compact_button(QPushButton("Distinct History…")); self.history.clicked.connect(self._open_distinct_history)
        self.resolve=compact_button(QPushButton("Mark Distinct"),primary=True); self.resolve.clicked.connect(self._resolve_current_choices)
        actions=QHBoxLayout(); actions.addWidget(self.history); actions.addStretch(1); actions.addWidget(self.resolve)
        layout.addLayout(actions,1,0,1,3); root.addWidget(recommendation)

    def _scope_changed(self):
        kind=str(self.scope.currentData() or "")
        self.library.setVisible(kind=="named_library"); self.tag.setVisible(kind=="tag")
        self.choose_folder.setVisible(kind=="folder"); self.folder_label.setVisible(kind=="folder")
        if kind=="selected" and len(self.initial_selected_paths)<2:
            self.search_status.setText("Select at least two Dataset images before using the selected-image scope.")

    def _range_changed(self):
        if self.lower.value()>self.upper.value():
            sender=self.sender()
            if sender is self.lower:self.upper.setValue(self.lower.value())
            else:self.lower.setValue(self.upper.value())

    def _choose_folder(self):
        selected=QFileDialog.getExistingDirectory(self,"Choose Duplicate Search Folder",str(self._folder or Path.home()))
        if selected:
            self._folder=Path(selected); self.folder_label.setText(str(self._folder))

    def _source_item(
        self,path: Path,*,fallback_root=None,fallback_id="folder",
        fallback_label="Chosen folder",registry=None,ingest_root=None,
    ):
        registry_supplied=registry is not None
        registry=registry or self.settings.library_registry()
        profile=registry.profile_for_path(path)
        if profile is not None:
            return duplicate_scope_item(
                path,source_id=profile.stable_id,source_root=profile.root,
                source_label=profile.name,source_profile_id=profile.stable_id,
            )
        if ingest_root is None and not registry_supplied:
            ingest=str(getattr(self.settings,"ingest_directory","") or "").strip()
            ingest_root=Path(ingest).expanduser().resolve() if ingest else None
        if ingest_root is not None:
            try:path.resolve().relative_to(ingest_root)
            except ValueError:pass
            else:return duplicate_scope_item(path,source_id="ingest",source_root=ingest_root,source_label="Incoming Images")
        root=Path(fallback_root or path.parent)
        return duplicate_scope_item(path,source_id=fallback_id,source_root=root,source_label=fallback_label)

    def _tag_paths(self,query: str):
        keys={normalize_tag(query)}-{""}
        known=self.state.knowledge.find(query) if query.strip() else None
        if known is not None:
            keys.add(normalize_tag(getattr(known,"name","")))
            keys.update(normalize_tag(value) for value in getattr(known,"aliases",()) if normalize_tag(value))
        paths=[]
        for _profile,manager in self.state.dataset.scoped_profile_managers():
            cached=dict(manager.validated_records)
            if not cached and manager.index is not None:cached=manager.index.records_by_path()
            for path,record in cached.items():
                if keys.intersection(normalize_tag(value) for value in record.tags):paths.append(Path(path))
        return tuple(dict.fromkeys(paths))

    def _build_scope_value(
        self,*,kind: str|None=None,tag_query: str|None=None,
        library_id: str|None=None,folder: Path|None=None,
        progress=None,should_cancel=None,
    ) -> DuplicateScope:
        if self.frozen_scope is not None:
            if should_cancel and should_cancel():raise InterruptedError()
            return self.frozen_scope
        kind=str(self.scope.currentData() or "current_filter") if kind is None else str(kind)
        label=""; paths=(); fallback_root=None; fallback_id=kind
        if progress:progress(0,0,"Preparing duplicate search scope")
        if kind=="current_filter":
            label="Current Dataset search and filters"; paths=self.current_paths
        elif kind=="selected":
            label="Selected Dataset images"; paths=self.initial_selected_paths
        elif kind=="included_libraries":
            label="All included readable libraries"; paths=tuple(self.state.dataset.readable_images)
        elif kind=="named_library":
            profile_id=str(self.library.currentData() or "") if library_id is None else str(library_id)
            profile,manager=self.state.dataset.profile_manager(profile_id)
            if profile is None or manager is None:raise ValueError("Choose an available named library.")
            if self.state.dataset.library_availability(profile_id)!="current":raise ValueError(f"{profile.name} is not currently readable; refresh it before pixel comparison.")
            label=f"Named library: {profile.name}"; paths=tuple(manager.readable_images)
        elif kind=="tag":
            value=self.tag.text().strip() if tag_query is None else str(tag_query).strip()
            if not value:raise ValueError("Enter one exact Tag or Other Name.")
            label=f"Included-library images tagged {value}"; paths=self._tag_paths(value)
        elif kind=="ingest":
            value=str(getattr(self.settings,"ingest_directory","") or "").strip()
            if not value or not Path(value).is_dir():raise ValueError("Choose an available Incoming Images folder in Setup first.")
            fallback_root=Path(value); fallback_id="ingest"; label="All Incoming Images"
            paths=tuple(DatasetScope(fallback_root,include_subfolders=True).iter_images())
        elif kind=="folder":
            selected_folder=self._folder if folder is None else Path(folder)
            if selected_folder is None or not selected_folder.is_dir():raise ValueError("Choose an available folder first.")
            fallback_root=selected_folder; fallback_id="folder:"+hashlib.sha256(str(selected_folder.resolve()).encode()).hexdigest()[:16]
            label=f"Folder: {selected_folder}"
            if progress:progress(0,0,f"Scanning {selected_folder.name} for supported images")
            paths=tuple(DatasetScope(selected_folder,include_subfolders=True).iter_images())
        registry=self.settings.library_registry()
        ingest_value=str(getattr(self.settings,"ingest_directory","") or "").strip()
        ingest_root=Path(ingest_value).expanduser().resolve() if ingest_value else None
        items=[]; total=len(paths); interval=max(1,total//200)
        for position,path in enumerate(paths,1):
            if should_cancel and should_cancel():raise InterruptedError("Duplicate scope preparation cancelled safely.")
            items.append(self._source_item(
                Path(path),fallback_root=fallback_root,fallback_id=fallback_id,
                fallback_label=label,registry=registry,ingest_root=ingest_root,
            ))
            if progress and (position==total or position%interval==0):
                progress(position,total,f"Preparing image identities {position:,} of {total:,}")
        items=tuple(items)
        items=tuple({item.identity:item for item in items}.values())
        if len(items)<2:raise ValueError(f"{label or 'This scope'} contains fewer than two supported, locally readable images.")
        digest=hashlib.sha256((kind+"\n"+"\n".join(sorted(item.identity for item in items))).encode("utf-8")).hexdigest()
        return DuplicateScope(f"{kind}:{digest}",label,items)

    def start_search(self):
        if self._task is not None:return
        if self._restore_task is not None:
            self._restore_task.cancel(); self._restore_task=None
        if self.frozen_mode=="preflight" and not self._frozen_scope_current():
            QMessageBox.warning(
                self,"Frozen scope changed",
                "Close this review and run the Ingest duplicate check again. "
                "No duplicate decision or file changed.",
            )
            self._check_frozen_currentness(); return
        kind=str(self.scope.currentData() or "current_filter")
        if kind=="folder" and self._folder is None:self._choose_folder()
        if kind=="folder" and self._folder is None:return
        tag_query=self.tag.text().strip(); library_id=str(self.library.currentData() or "")
        folder=Path(self._folder) if self._folder is not None else None
        lower=float(self.lower.value()); upper=float(self.upper.value()); force=self.force.isChecked()
        def work(job):
            scope=self._build_scope_value(
                kind=kind,tag_query=tag_query,library_id=library_id,folder=folder,
                progress=job.update,should_cancel=lambda:not job.checkpoint(),
            )
            return self.state.duplicates.search(
                scope,lower_similarity=lower,upper_similarity=upper,force=force,
                include_similar=self.include_similar,
                incoming_identities=self.incoming_identities,
                progress=job.update,should_cancel=lambda:not job.checkpoint(),
            )
        key=self.state.duplicates.search_key(
            self.frozen_scope,
            lower,upper,force,include_similar=self.include_similar,
            incoming_identities=self.incoming_identities,
        ) if self.frozen_scope is not None else ""
        task=CoordinatedCallTask(
            self.state.jobs,"Finding duplicate and similar images",work,
            resource="cpu",key=key,
        )
        self._task=task; task.signals.progress.connect(self._search_progress)
        task.signals.complete.connect(self._search_complete); task.signals.cancelled.connect(self._search_cancelled); task.signals.failed.connect(self._search_failed)
        self.progress.show(); set_progress_bar(self.progress,unknown_text="Preparing scope…")
        self.cancel.show(); self.search.setEnabled(False)
        self.search_status.setText("Preparing the duplicate scope in the shared background coordinator…")
        task.start()

    def _start_session_restore(self):
        if self.report is not None or self._task is not None or self._restore_task is not None:
            return
        kind=str(self.scope.currentData() or "current_filter")
        if kind=="folder" and self._folder is None:return
        tag_query=self.tag.text().strip(); library_id=str(self.library.currentData() or "")
        folder=Path(self._folder) if self._folder is not None else None
        def work(job):
            scope=self._build_scope_value(
                kind=kind,tag_query=tag_query,library_id=library_id,folder=folder,
                progress=job.update,should_cancel=lambda:not job.checkpoint(),
            )
            return self.state.duplicates.latest_session_report(
                scope,include_similar=self.include_similar,
                incoming_identities=self.incoming_identities,
            )
        task=CoordinatedCallTask(
            self.state.jobs,"Restoring duplicate review results",work,
            resource="cpu",
        )
        self._restore_task=task
        task.signals.complete.connect(self._session_restore_ready)
        task.signals.cancelled.connect(lambda:setattr(self,"_restore_task",None))
        task.signals.failed.connect(lambda _message:setattr(self,"_restore_task",None))
        task.start()

    def _session_restore_ready(self,report):
        self._restore_task=None
        if report is None or self.report is not None:return
        self.lower.setValue(int(round(report.lower_similarity)))
        self.upper.setValue(int(round(report.upper_similarity)))
        self.report=report; self._group_page=0; self._active_group_id=""
        self._active_pair_ids=(); self._selected.clear(); self._details={}
        self._render_groups()
        self.search_status.setText(
            "Restored the latest completed results from this app session. "
            "Choose Find Duplicates to refresh after external file changes."
        )

    def cancel_work(self):
        task=self._task
        if task is not None:
            task.cancel()
            # A coordinated observer deliberately receives no completion signal
            # after detaching, so settle this dialog immediately.
            self._task=None; self.progress.hide(); self.cancel.hide()
            self.search.setEnabled(True); self._update_actions()
            self.search_status.setText(
                "Duplicate work stopped safely for this review; completed "
                "fingerprints remain available for the next run."
            )

    def _search_progress(self,current,total,message):
        set_progress_bar(self.progress,current,total)
        self.search_status.setText(str(message or "Updating the duplicate index…"))

    def _finish_task(self):
        self._task=None; self.progress.hide(); self.cancel.hide(); self.search.setEnabled(True)
        if self._close_when_idle:
            QTimer.singleShot(0,self.reject)

    def _search_complete(self,report):
        self._finish_task(); self.report=report; self._group_page=0
        self._active_group_id=""; self._active_pair_ids=()
        self._selected.clear(); self._details={}
        self.search_status.setText(
            f"Index current • {report.indexed:,} new/changed • {report.reused:,} unchanged reused • {report.removed:,} stale cache row(s) retired"
        )
        self._render_groups()

    def _search_cancelled(self):
        self._finish_task(); self.search_status.setText("Duplicate indexing stopped safely; completed fingerprints remain available for the next run.")

    def _search_failed(self,message):
        self._finish_task(); self.search_status.setText("Duplicate search needs attention • no image changed")
        QMessageBox.warning(self,"Duplicate search could not finish",str(message))

    def _render_groups(self):
        report=self.report
        if report is None:
            self.groups.setRowCount(0); return
        pairs=self._result_pairs()
        total=len(pairs); pages=max(1,(total+self.GROUP_PAGE_SIZE-1)//self.GROUP_PAGE_SIZE)
        self._group_page=min(self._group_page,pages-1); start=self._group_page*self.GROUP_PAGE_SIZE
        values=pairs[start:start+self.GROUP_PAGE_SIZE]; self.groups.setRowCount(len(values))
        self._pair_thumbnail_generation+=1; generation=self._pair_thumbnail_generation
        if self._pair_thumbnail_task is not None:self._pair_thumbnail_task.cancel()
        self._pair_thumbnail_labels={}; thumbnail_requests=[]
        for row,(group_id,relation) in enumerate(values):
            first=report.records[relation.first_identity]
            second=report.records[relation.second_identity]
            item=QTableWidgetItem(f"Set {start+row+1:,}")
            item.setData(Qt.UserRole,(group_id,first.identity,second.identity))
            item.setToolTip(f"{first.path.name}\n{second.path.name}")
            self.groups.setItem(row,0,item)
            widget=QWidget(); outer=QVBoxLayout(widget)
            outer.setContentsMargins(4,2,4,4); outer.setSpacing(2)
            heading=QLabel(f"Set {start+row+1:,}"); heading.setObjectName("mutedLabel")
            outer.addWidget(heading)
            thumbs=QHBoxLayout(); thumbs.setContentsMargins(0,0,0,0); thumbs.setSpacing(4)
            for record in (first,second):
                thumbnail=QLabel(record.path.name); thumbnail.setAlignment(Qt.AlignCenter)
                thumbnail.setWordWrap(True); thumbnail.setToolTip(str(record.path))
                thumbnail.setFixedSize(78,58); thumbnail.setObjectName("thumbnail")
                side=len(thumbnail_requests)%2
                self._pair_thumbnail_labels[(generation,row,side)]=thumbnail
                thumbnail_requests.append((row,side,Path(record.path)))
                thumbs.addWidget(thumbnail,1)
            outer.addLayout(thumbs)
            widget.setAttribute(Qt.WA_TransparentForMouseEvents)
            self.groups.setCellWidget(row,0,widget); self.groups.setRowHeight(row,88)
        if thumbnail_requests:self._load_pair_thumbnails(generation,thumbnail_requests)
        set_word="set" if total==1 else "sets"
        self.group_page_label.setText(f"{self._group_page+1} / {pages} • {total:,} {set_word}" if total else "No sets")
        paged=total>0 and pages>1
        self.group_previous.setVisible(paged); self.group_page_label.setVisible(paged); self.group_next.setVisible(paged)
        self.group_previous.setEnabled(self._group_page>0); self.group_next.setEnabled(self._group_page+1<pages)
        issue_suffix=f" • {len(report.issues):,} held/unreadable item(s)" if report.issues else ""
        bound_suffix=" • result bound reached; narrow scope" if report.truncated else ""
        image_word="image" if report.matched_images==1 else "images"
        comparison=(
            f"{report.lower_similarity:g}–{report.upper_similarity:g}% visual range"
            if report.include_similar else "exact matches only"
        )
        self.result_summary.setText(f"{total:,} {set_word} • {report.matched_images:,} {image_word} • {comparison}{issue_suffix}{bound_suffix}")
        if values:
            target=next((
                row for row,(group_id,relation) in enumerate(values)
                if group_id==self._active_group_id
                and (relation.first_identity,relation.second_identity)==self._active_pair_ids
            ),0)
            self.groups.selectRow(target)
        else:self._clear_group()

    def _load_pair_thumbnails(self,generation,requests):
        def load():
            values=[]
            for row,side,path in requests:
                image=QImage(str(path))
                if not image.isNull():
                    image=image.scaled(78,58,Qt.KeepAspectRatio,Qt.SmoothTransformation)
                values.append((row,side,path,image))
            return generation,tuple(values)
        task=AsyncCallTask(load); self._pair_thumbnail_task=task
        task.signals.complete.connect(self._pair_thumbnails_ready)
        task.signals.failed.connect(lambda _message:setattr(self,"_pair_thumbnail_task",None))
        self.pool.start(task)

    def _pair_thumbnails_ready(self,result):
        generation,values=result
        if generation!=self._pair_thumbnail_generation:return
        self._pair_thumbnail_task=None; policy=content_safety_policy()
        for row,side,path,image in values:
            label=self._pair_thumbnail_labels.get((generation,row,side))
            if label is None or image.isNull():continue
            blurred=False
            if policy is not None:
                try:
                    state=policy.preview_state(path,honor_session_reveal=False)
                    blurred=bool(state.get("blurred"))
                except Exception:blurred=True
            if blurred:
                effect=QGraphicsBlurEffect(label); effect.setBlurRadius(18)
                label.setGraphicsEffect(effect)
            label.setPixmap(QPixmap.fromImage(image))

    def _result_pairs(self):
        if self.report is None:return ()
        return tuple(
            (group.id,relation) for group in self.report.groups
            for relation in group.relations
        )

    def _change_group_page(self,delta):
        self._group_page=max(0,self._group_page+int(delta)); self._render_groups()

    def _group_selected(self):
        rows={item.row() for item in self.groups.selectedItems()}
        if not rows or self.report is None:return
        value=self.groups.item(min(rows),0).data(Qt.UserRole)
        if not isinstance(value,(tuple,list)) or len(value)!=3:return
        self._active_group_id=str(value[0])
        self._active_pair_ids=(str(value[1]),str(value[2]))
        self._selected.clear(); self._details={}; self._render_group()

    def _active_group(self):
        if self.report is None:return None
        return next((group for group in self.report.groups if group.id==self._active_group_id),None)

    def _clear_group(self):
        self._active_group_id=""; self._active_pair_ids=()
        self.member_summary.setText("No duplicate set selected")
        self.left_choice.clear(); self.right_choice.clear(); self.reference.clear()
        self.left_preview.clear(); self.right_preview.clear()
        self._render_evidence(); self._update_actions()

    def _render_group(self):
        group=self._active_group()
        if group is None or self.report is None:self._clear_group(); return
        if len(self._active_pair_ids)!=2:self._clear_group(); return
        records=[self.report.records[value] for value in self._active_pair_ids]
        pair_index=next((
            index for index,(group_id,relation) in enumerate(self._result_pairs(),1)
            if group_id==self._active_group_id
            and (relation.first_identity,relation.second_identity)==self._active_pair_ids
        ),0)
        self.member_summary.setText(f"Set {pair_index:,} • 2 images")
        self._populate_choices(records); self._load_details(records)

    def _populate_choices(self,records):
        for combo in (self.left_choice,self.right_choice,self.reference):combo.blockSignals(True); combo.clear()
        for record in records:
            label=f"{record.path.name} • {record.width} × {record.height} • {record.source_label}"
            self.left_choice.addItem(label,record.identity); self.right_choice.addItem(label,record.identity); self.reference.addItem(label,record.identity)
        self.left_choice.setCurrentIndex(0)
        self.right_choice.setCurrentIndex(min(1,len(records)-1))
        self.reference.setCurrentIndex(0)
        for combo in (self.left_choice,self.right_choice,self.reference):combo.blockSignals(False)
        for side,record in zip(("left","right"),records):
            recycle=str(record.path) in self._selected
            keep_control=getattr(self,f"{side}_keep"); recycle_control=getattr(self,f"{side}_recycle")
            keep_control.blockSignals(True); recycle_control.blockSignals(True)
            recycle_control.setChecked(recycle); keep_control.setChecked(not recycle)
            keep_control.blockSignals(False); recycle_control.blockSignals(False)
            set_visual_status(
                getattr(self,f"{side}_preview_frame"),"red" if recycle else "green",
            )
        self._comparison_changed(); self._reference_changed()

    def _record(self,identity):
        return self.report.records.get(str(identity or "")) if self.report is not None else None

    def _comparison_changed(self):
        first=self._record(self.left_choice.currentData()); second=self._record(self.right_choice.currentData())
        self._preview_generation+=1; generation=self._preview_generation
        for task in self._preview_tasks:
            try:
                if task.is_finished():continue
                task.cancel(); self.pool.tryTake(task)
            except RuntimeError:pass
        self._preview_tasks=[]
        self._load_comparison_preview("left",first,generation)
        self._load_comparison_preview("right",second,generation)
        self.pair_evidence.setToolTip("")
        if first is None or second is None:
            self.pair_evidence.setText("Choose two images to compare their exact relation.")
        elif first.identity==second.identity:
            self.pair_evidence.setText("Choose two different images for pair evidence.")
        elif self.state.duplicates.decisions.is_distinct(first,second):
            self.pair_evidence.setText("Reviewed distinct pair • this exact identity/content pair is intentionally kept; no image data was changed.")
        else:
            relation=relation_between(first,second)
            self.pair_evidence.setText(f"{relation.label} • {relation.similarity:.1f}%")
            self.pair_evidence.setToolTip(relation.evidence)
        self._render_evidence(); self._sync_pair_protection_controls()
        self._update_actions()

    def _load_comparison_preview(self,side,record,generation):
        preview=self.left_preview if side=="left" else self.right_preview
        if record is None:
            preview.clear(); return
        path=Path(record.path); preview.clear("Loading preview…"); preview.path=path
        task=AsyncCallTask(
            lambda g=generation,s=side,p=path:(g,s,p,QImage(str(p)))
        )
        task.signals.complete.connect(self._comparison_preview_ready)
        task.signals.failed.connect(
            lambda message,g=generation,s=side,p=path:
            self._comparison_preview_failed(g,s,p,message)
        )
        self._preview_tasks.append(task); self.pool.start(task)

    def _comparison_preview_ready(self,result):
        generation,side,path,image=result
        if generation!=self._preview_generation:return
        preview=self.left_preview if side=="left" else self.right_preview
        pixmap=QPixmap.fromImage(image) if not image.isNull() else QPixmap()
        preview.set_image(pixmap,path)
        if pixmap.isNull():preview.viewer.image_label.setText("Preview unavailable")
        self._sync_pair_protection_controls()

    def _comparison_preview_failed(self,generation,side,path,_message):
        if generation!=self._preview_generation:return
        preview=self.left_preview if side=="left" else self.right_preview
        preview.clear("Preview unavailable"); preview.path=Path(path)
        self._sync_pair_protection_controls()

    def _sync_pair_protection_controls(self):
        previews=(self.left_preview,self.right_preview)
        for preview in previews:preview.reveal_button.hide()
        protected=[preview for preview in previews if preview.path is not None and preview._requires_blur]
        if not protected:
            self.reveal_pair.hide(); return
        all_revealed=all(preview._temporarily_revealed for preview in protected)
        self.reveal_pair.setText("Hide Previews" if all_revealed else "Reveal Temporarily")
        self.reveal_pair.setAccessibleName(
            "Restore protection for both comparison images"
            if all_revealed else "Reveal both comparison images temporarily"
        )
        self.reveal_pair.setToolTip(
            "Restore protection for both comparison previews."
            if all_revealed else
            "Reveal both protected comparison previews until either image changes or this window closes."
        )
        self.reveal_pair.show()

    def _toggle_pair_reveal(self):
        protected=[
            preview for preview in (self.left_preview,self.right_preview)
            if preview.path is not None and preview._requires_blur
        ]
        reveal=any(not preview._temporarily_revealed for preview in protected)
        for preview in protected:
            if reveal:preview.reveal_temporarily()
            elif preview._temporarily_revealed:preview.toggle_temporary_reveal()
        self._sync_pair_protection_controls()

    def _connect_linked_views(self):
        first=self.left_preview.viewer; second=self.right_preview.viewer
        first.view_scale_changed.connect(lambda value:self._sync_scale(first,second,value))
        second.view_scale_changed.connect(lambda value:self._sync_scale(second,first,value))
        first.horizontalScrollBar().valueChanged.connect(lambda _value:self._sync_scroll(first,second,True))
        second.horizontalScrollBar().valueChanged.connect(lambda _value:self._sync_scroll(second,first,True))
        first.verticalScrollBar().valueChanged.connect(lambda _value:self._sync_scroll(first,second,False))
        second.verticalScrollBar().valueChanged.connect(lambda _value:self._sync_scroll(second,first,False))

    def _sync_scale(self,source,target,value):
        if self._syncing_view or not self.link_views.isChecked():return
        self._syncing_view=True
        try:target.set_zoom_scale(value)
        finally:self._syncing_view=False

    def _sync_scroll(self,source,target,horizontal):
        if self._syncing_view or not self.link_views.isChecked():return
        first=source.horizontalScrollBar() if horizontal else source.verticalScrollBar()
        second=target.horizontalScrollBar() if horizontal else target.verticalScrollBar()
        ratio=first.value()/max(1,first.maximum())
        self._syncing_view=True
        try:second.setValue(round(ratio*second.maximum()))
        finally:self._syncing_view=False

    def _fit_both(self):
        self._syncing_view=True
        try:self.left_preview.viewer.fit_to_view(); self.right_preview.viewer.fit_to_view()
        finally:self._syncing_view=False

    def _zoom_both(self,factor):
        self._syncing_view=True
        try:self.left_preview.viewer.zoom_by(factor); self.right_preview.viewer.zoom_by(factor)
        finally:self._syncing_view=False

    def _resolution_changed(self,side,recycle):
        record=self._record(
            self.left_choice.currentData() if side=="left"
            else self.right_choice.currentData()
        )
        if record is None:return
        path=str(record.path)
        if recycle:self._selected.add(path)
        else:self._selected.discard(path)
        set_visual_status(
            self.left_preview_frame if side=="left" else self.right_preview_frame,
            "red" if recycle else "green",
        )
        self._sync_reference_from_choices(); self._reference_changed()

    def _sync_reference_from_choices(self):
        for index in range(self.reference.count()):
            record=self._record(self.reference.itemData(index))
            if record is not None and str(record.path) not in self._selected:
                self.reference.setCurrentIndex(index); return
        self.reference.setCurrentIndex(-1)

    def _resolve_current_choices(self):
        if self._selected:self._preview_recycle()
        else:self._mark_distinct()

    def _open_distinct_history(self):
        dialog=DistinctHistoryDialog(self.state,self)
        dialog.changed.connect(self._distinct_history_changed)
        dialog.exec()

    def _distinct_history_changed(self,_key):
        self.state.duplicates.clear_session_reports()
        self.state.duplicates.invalidate_badges()
        if self.report is None or self._task is not None:
            self._update_actions(); return
        report=self.report
        task=CoordinatedCallTask(
            self.state.jobs,"Refreshing duplicate pairs after history change",
            lambda job:self.state.duplicates.regroup(
                report,progress=job.update,should_cancel=lambda:not job.checkpoint(),
            ),resource="cpu",
        )
        self._task=task; self.resolve.setEnabled(False)
        task.signals.complete.connect(self._distinct_history_ready)
        task.signals.cancelled.connect(self._search_cancelled)
        task.signals.failed.connect(self._distinct_failed); task.start()

    def _distinct_history_ready(self,report):
        self._finish_task(); self.report=report
        self.state.duplicates.record_session_report(report)
        self.state.duplicates.record_badge_results(report)
        self._active_group_id=""; self._active_pair_ids=(); self._selected.clear()
        self._render_groups()
        self.search_status.setText("Distinct history updated; duplicate pairs refreshed.")

    def _load_details(self,records):
        self._details_generation+=1; generation=self._details_generation
        if self._details_task is not None:self._details_task.cancel()
        if self._pair_thumbnail_task is not None:
            self._pair_thumbnail_task.cancel(); self._pair_thumbnail_task=None
        self._render_evidence()
        task=AsyncCallTask(lambda:(generation,self.state.duplicates.member_details(records,knowledge=self.state.knowledge)))
        self._details_task=task; task.signals.complete.connect(self._details_ready); task.signals.failed.connect(lambda message,g=generation:self._details_failed(g,message))
        self.pool.start(task)

    def _details_ready(self,result):
        generation,values=result
        if generation!=self._details_generation:return
        self._details_task=None; self._details={value.identity:value for value in values}; self._render_evidence(); self._reference_changed()

    def _details_failed(self,generation,message):
        if generation!=self._details_generation:return
        self._details_task=None
        for fields in (self.left_detail_values,self.right_detail_values):
            for value in fields.values():value.setText(f"Unavailable: {message}")

    @staticmethod
    def _modified(record):
        try:return datetime.fromtimestamp(record.mtime_ns/1_000_000_000).astimezone().strftime("%Y-%m-%d %H:%M")
        except Exception:return "Unavailable"

    def _render_evidence(self):
        records=(
            self._record(self.left_choice.currentData()),
            self._record(self.right_choice.currentData()),
        )
        first,second=records
        relation=(
            relation_between(first,second)
            if first is not None and second is not None and first.identity!=second.identity
            else None
        )
        distinct=bool(
            relation is not None and self.state.duplicates.decisions.is_distinct(first,second)
        )
        for side,record,title,fields,knowledge_frame,badge in (
            (
                "left",first,self.left_detail_title,self.left_detail_values,
                self.left_knowledge_frame,self.left_knowledge_badge,
            ),
            (
                "right",second,self.right_detail_title,self.right_detail_values,
                self.right_knowledge_frame,self.right_knowledge_badge,
            ),
        ):
            if record is None:
                title.setText(f"{side.title()} image details")
                for value in fields.values():value.setText("—")
                set_visual_status(knowledge_frame,"neutral")
                badge.coverage=ImageKnowledgeCoverage(); badge.update(); continue
            detail=self._details.get(record.identity)
            title.setText(record.path.name)
            stale=self.report is not None and record.identity in self.report.stale_identities
            relation_text=("Saved cache • " if stale else "")+(
                "Reviewed distinct" if distinct else
                f"{relation.label} • {relation.similarity:.1f}%" if relation else
                "No pair selected"
            )
            values={
                "relationship":relation_text,
                "dimensions":f"{record.width} × {record.height}",
                "format_and_size":f"{record.format} • {record.size:,} bytes",
                "location":f"{record.source_label}\n{record.path}",
                "modified":self._modified(record),
                "image_knowledge":detail.completeness_label if detail else "Checking…",
                "metadata":detail.metadata_summary if detail else "Checking…",
                "provenance":detail.provenance_summary if detail else "Checking…",
                "derivative":detail.derivative_summary if detail else "Checking…",
            }
            for key,value in values.items():fields[key].setText(value)
            coverage=ImageKnowledgeCoverage(
                tags=detail.has_tags if detail else None,
                descriptions=detail.has_descriptions if detail else None,
                tags_detail=("Tags: saved." if detail and detail.has_tags else "Tags: none saved." if detail else "Tags: checking…"),
                descriptions_detail=("Descriptions: saved." if detail and detail.has_descriptions else "Descriptions: none saved." if detail else "Descriptions: checking…"),
                tags_stored=detail.has_tags if detail else None,
                descriptions_stored=detail.has_descriptions if detail else None,
                problem=bool(detail and detail.visual_status=="red"),
            )
            set_visual_status(
                knowledge_frame,detail.visual_status if detail else "neutral",
            )
            badge.coverage=coverage; badge.setToolTip(
                coverage.tags_detail+"\n"+coverage.descriptions_detail
            ); badge.update()

    def _reference_changed(self):
        if self.report is None or len(self._active_pair_ids)!=2:
            self.recommendation.setText("Choose a set to review.")
            self._update_actions(); return
        records=[self.report.records[value] for value in self._active_pair_ids]
        recycled=[record for record in records if str(record.path) in self._selected]
        kept=[record for record in records if str(record.path) not in self._selected]
        if recycled:
            if kept:
                self.recommendation.setText(
                    f"{len(kept)} green image will be kept; {len(recycled)} red image "
                    "will enter the recoverable Recycle Bin preview."
                )
            else:
                self.recommendation.setText("Set at least one image to Keep before reviewing a Recycle plan.")
            self._update_actions(); return
        recommendation=self.state.duplicates.recommend_reference(records,self._details)
        if recommendation.identity:
            record=self.report.records[recommendation.identity]
            reason=recommendation.rationale.removeprefix("Suggested only because it has ").split(". Review provenance",1)[0]
            self.recommendation.setText(
                f"Both images are set to Keep. If you recycle one, "
                f"{record.path.name} has stronger evidence to keep — {reason}."
            )
        else:self.recommendation.setText("Both images are set to Keep. Mark Distinct keeps both and hides only this exact pair from future results.")
        self.recommendation.setToolTip(recommendation.rationale)
        self._update_actions()

    def _update_actions(self):
        first=self._record(self.left_choice.currentData()); second=self._record(self.right_choice.currentData())
        pair=first is not None and second is not None and first.identity!=second.identity
        already_distinct=bool(pair and self.state.duplicates.decisions.is_distinct(first,second))
        recycled=[record for record in (first,second) if record is not None and str(record.path) in self._selected]
        kept=[record for record in (first,second) if record is not None and str(record.path) not in self._selected]
        if recycled:
            self.resolve.setText("Review Recycle Plan…")
            ready=bool(pair and kept and self._task is None)
            self.resolve.setEnabled(ready)
            self.resolve.setToolTip(
                "Review the recoverable Recycle Bin move for the red image. Green images remain untouched."
                if ready else "Set at least one image to Keep before recycling the other image."
            )
        else:
            self.resolve.setText("Mark Distinct")
            self.resolve.setEnabled(pair and not already_distinct and self._task is None)
            self.resolve.setToolTip(
                "This exact pair is already marked intentionally distinct."
                if already_distinct else
                "Keep both images and hide only this exact identity/content pair from future duplicate results."
                if pair else "Choose a set first."
            )
        self.history.setEnabled(bool(self.state.duplicates.decisions.snapshot_keys()))
        if self.training_mode:
            self.resolve.setEnabled(False)

        if self.frozen_mode=="preflight" and not self._frozen_scope_current():
            for control in (self.resolve,self.search):
                control.setEnabled(False)

    def _mark_distinct(self):
        if self.frozen_mode=="preflight" and not self._frozen_scope_current():
            QMessageBox.warning(self,"Frozen scope changed","Close this review and run the Ingest duplicate check again. No reviewed decision changed.")
            return
        first=self._record(self.left_choice.currentData()); second=self._record(self.right_choice.currentData())
        if first is None or second is None or first.identity==second.identity:return
        box=QMessageBox(QMessageBox.Question,"Keep Both and Mark Distinct",
            f"Record these two current files as intentionally distinct?\n\n{first.path}\n{second.path}\n\nThis durable reviewed decision hides only this exact identity/content pair. It changes no pixels, Metadata, Image Knowledge, TXT, or location.",QMessageBox.NoButton,self)
        mark=box.addButton("Mark Distinct",QMessageBox.AcceptRole); cancel=box.addButton(QMessageBox.Cancel); box.setDefaultButton(cancel); box.setEscapeButton(cancel); box.exec()
        if box.clickedButton() is not mark:return
        report=self.report
        def work(job):
            if not self._frozen_authority_scope_current():
                raise RuntimeError(
                    "The frozen preflight scope changed before the reviewed "
                    "decision could be saved."
                )
            updated=self.state.duplicates.mark_distinct(
                report,first,second,progress=job.update,
                should_cancel=lambda:not job.checkpoint(),
            )
            return first,second,updated
        task=CoordinatedCallTask(
            self.state.jobs,"Marking duplicate pair intentionally distinct",
            work,resource="cpu",
        )
        self._task=task; task.signals.progress.connect(self._search_progress)
        task.signals.complete.connect(self._mark_distinct_ready)
        task.signals.cancelled.connect(self._distinct_cancelled)
        task.signals.failed.connect(self._distinct_failed)
        self.progress.show(); set_progress_bar(self.progress,unknown_text="Checking current fingerprints…")
        self.cancel.hide(); self.search.setEnabled(False)
        self.search_status.setText("Checking both current files before saving the reviewed decision…")
        self._update_actions(); task.start()

    def _mark_distinct_ready(self,result):
        first,second,report=result
        self._finish_task(); self.report=report
        self.state.duplicates.record_session_report(report)
        self._active_group_id=""; self._active_pair_ids=()
        self._selected.clear(); self._render_groups()
        self.search_status.setText("Reviewed distinct decision saved; no file or Image Knowledge changed.")

    def _distinct_cancelled(self):
        self._finish_task(); self._update_actions()
        self.search_status.setText("Mark Distinct stopped before a new reviewed decision was saved.")

    def _distinct_failed(self,message):
        self._finish_task(); self._update_actions()
        self.search_status.setText("Reviewed-distinct decision not changed; no image or Image Knowledge changed.")
        QMessageBox.warning(self,"Reviewed decision could not be changed",str(message))

    def _selected_records(self):
        if self.report is None:return ()
        by_path={str(value.path):value for value in self.report.records.values()}
        return tuple(by_path[value] for value in sorted(self._selected) if value in by_path)

    def _preview_recycle(self):
        if self.training_mode:return
        if self.frozen_mode=="preflight" and not self._frozen_scope_current():
            QMessageBox.warning(self,"Frozen scope changed","Close this review and run the Ingest duplicate check again. Nothing moved.")
            return
        records=self._selected_records(); reference=self._record(self.reference.currentData())
        if not records or reference is None:return
        if any(record.identity==reference.identity for record in records):
            QMessageBox.information(self,"Keep one image","Set one image to Keep before reviewing a Recycle plan."); return
        stale=[record.path for record in (*records,reference) if not self.state.duplicates.index.is_current(record)]
        if stale:
            QMessageBox.warning(self,"Fingerprints changed","Re-run the duplicate search before moving files. Changed or unavailable:\n\n"+"\n".join(str(value) for value in stale[:8])); return
        def work(job):
            if not self._frozen_authority_scope_current():
                raise RuntimeError(
                    "The frozen preflight scope changed before Recycle Bin "
                    "preview."
                )
            plans=[]; errors=[]
            for index,record in enumerate(records,1):
                if not job.checkpoint():raise InterruptedError()
                try:
                    plan=self.state.recycle.preview_recycle(
                        record.path,source_root=record.source_root,
                        source_profile_id=record.source_profile_id,
                        should_cancel=lambda:not job.checkpoint(),
                    )
                    if not plan.ready:errors.extend(f"{record.path.name}: {value}" for value in plan.errors)
                    plans.append((record,plan))
                except Exception as exc:errors.append(f"{record.path.name}: {exc}")
                job.update(index,len(records),f"Previewing {record.path.name} for Recycle Bin")
            if not self._frozen_authority_scope_current():
                raise RuntimeError(
                    "The frozen preflight scope changed while Recycle Bin "
                    "preview was prepared."
                )
            return reference,tuple(plans),tuple(errors)
        task=CoordinatedCallTask(self.state.jobs,"Previewing duplicate families for Recycle Bin",work,resource="io",key="duplicate-recycle-preview:"+hashlib.sha256("\n".join(record.identity for record in records).encode()).hexdigest())
        self._task=task; self._recycle_context=records; self.search.setEnabled(False); self.cancel.show(); self.progress.show(); set_progress_bar(self.progress,0,len(records))
        task.signals.progress.connect(self._search_progress); task.signals.complete.connect(self._recycle_preview_ready); task.signals.cancelled.connect(self._search_cancelled); task.signals.failed.connect(self._search_failed); task.start()

    def _recycle_preview_ready(self,result):
        self._finish_task(); reference,plans,errors=result
        if self.frozen_mode=="preflight" and not self._frozen_scope_current():
            self._recycle_context=None
            QMessageBox.warning(
                self,"Frozen scope changed",
                "Close this review and run the Ingest duplicate check again. Nothing moved.",
            )
            return
        if errors or len(plans)!=len(self._recycle_context or ()):
            QMessageBox.warning(self,"Complete recycle selection is not ready","Nothing moved.\n\n"+"\n".join(errors[:12])); self._recycle_context=None; return
        members=[member for _record,plan in plans for member in plan.transfer.members]
        boundary_required=any(plan.boundary_decision is not None and plan.boundary_decision.requires_explicit_confirmation for _record,plan in plans)
        box=QMessageBox(QMessageBox.Question,"Move Explicit Duplicate Selection to Recycle Bin",
            f"Keep the green image {reference.path.name} and move exactly {len(plans):,} selected image family(s) containing {len(members):,} file(s) to the app-managed Recycle Bin?\n\nNo file is permanently deleted. Image Knowledge, TXT, XMP, source identity, journal, Undo, and recovery stay with each complete family.",QMessageBox.NoButton,self)
        move=box.addButton("Move to Recycle Bin",QMessageBox.AcceptRole); cancel=box.addButton(QMessageBox.Cancel); box.setDefaultButton(cancel); box.setEscapeButton(cancel)
        if boundary_required:
            confirmation=QCheckBox("I reviewed the exact files, storage roles, and Local Only boundary crossing.")
            move.setEnabled(False); confirmation.toggled.connect(move.setEnabled); box.setCheckBox(confirmation)
        box.setDetailedText("\n".join(
            f"{member.source}  →  {member.target}\nSize: {member.size:,} bytes • source identity: mtime {member.mtime_ns}"
            for member in members
        )); box.exec()
        if box.clickedButton() is not move:
            self._recycle_context=None; self.search_status.setText("Recycle move cancelled before writing."); return
        self._apply_recycle(plans,boundary_required)

    def _apply_recycle(self,plans,boundary_confirmed):
        def work(job):
            moved=[]; errors=[]; recovery_required=False
            for index,(record,plan) in enumerate(plans,1):
                if not job.checkpoint():raise InterruptedError()
                if not self._frozen_authority_scope_current():
                    raise RuntimeError(
                        "The frozen preflight scope changed before a Recycle "
                        "Bin write. Nothing further was moved."
                    )
                try:
                    self.state.recycle.apply_recycle(
                        plan,reason="move explicitly selected duplicate candidate to Recycle Bin",
                        boundary_confirmed=bool(boundary_confirmed),
                        should_cancel=lambda:not job.checkpoint(),
                    )
                    moved.append(record)
                except TransferRecoveryRequired as exc:
                    errors.append(f"{record.path.name}: {exc}"); recovery_required=True; break
                except Exception as exc:errors.append(f"{record.path.name}: {exc}")
                job.update(index,len(plans),f"Moving {record.path.name} to Recycle Bin")
            return tuple(moved),tuple(errors),recovery_required
        task=CoordinatedCallTask(self.state.jobs,"Moving selected duplicate families to Recycle Bin",work,resource="io",key="duplicate-recycle-apply:"+hashlib.sha256("\n".join(record.identity for record,_plan in plans).encode()).hexdigest())
        self._task=task; self.search.setEnabled(False); self.cancel.show(); self.progress.show(); set_progress_bar(self.progress,0,len(plans))
        task.signals.progress.connect(self._search_progress); task.signals.complete.connect(self._recycle_apply_ready); task.signals.cancelled.connect(self._search_cancelled); task.signals.failed.connect(self._search_failed); task.start()

    def _recycle_apply_ready(self,result):
        self._finish_task(); moved,errors,recovery_required=result; self._recycle_context=None
        if self.report is not None and moved:
            identities={record.identity for record in moved}; remaining={key:value for key,value in self.report.records.items() if key not in identities}
            self.report=self.state.duplicates.regroup(replace(self.report,records=remaining))
            self.state.duplicates.clear_session_reports()
            self.state.duplicates.record_badge_results(self.report)
            self._active_group_id=""; self._active_pair_ids=()
            self._selected.clear(); self._render_groups(); self.changed.emit([str(record.path) for record in moved])
        self.search_status.setText(f"Moved {len(moved):,} selected duplicate family(s) to Recycle Bin • {len(errors):,} need attention")
        if errors:
            QMessageBox.warning(self,"Recycle recovery needs attention" if recovery_required else "Some duplicate families were not moved","\n".join(errors[:12]))

    def reject(self):
        if self._restore_task is not None:
            self._restore_task.cancel(); self._restore_task=None
        if self.frozen_scope is not None and self._task is not None:
            self.cancel_work()
        if self._task is not None:self._task.cancel()
        if self._details_task is not None:self._details_task.cancel()
        for task in self._preview_tasks:
            try:
                if task.is_finished():continue
                task.cancel(); self.pool.tryTake(task)
            except RuntimeError:pass
        self._preview_tasks=[]
        super().reject()

    def _frozen_scope_current(self) -> bool:
        if self.frozen_mode!="preflight" or self.frozen_currentness is None:
            return True
        try:return bool(self.frozen_currentness())
        except Exception:return False

    def _frozen_authority_scope_current(self) -> bool:
        if self.frozen_mode!="preflight":
            return True
        callback=self.frozen_authority_currentness or self.frozen_currentness
        if callback is None:return True
        try:return bool(callback())
        except Exception:return False

    def _check_frozen_currentness(self) -> None:
        if self.frozen_mode!="preflight" or self._frozen_scope_current():
            return
        self.search_status.setText(
            "The frozen Ingest or Dataset scope changed. Close this review and "
            "run the preflight again before making a reviewed decision or moving files."
        )
        self._update_actions()

    def prepare_for_privacy_capture(self):
        for preview in (self.left_preview,self.right_preview):preview.prepare_for_privacy_capture()
        self._sync_pair_protection_controls()
