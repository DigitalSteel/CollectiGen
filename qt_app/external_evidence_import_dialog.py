from __future__ import annotations

import os
from pathlib import Path

from PySide6.QtCore import QThreadPool,Qt
from PySide6.QtWidgets import (
    QCheckBox,QComboBox,QDialogButtonBox,QFileDialog,QFormLayout,QHBoxLayout,
    QHeaderView,QLabel,QMessageBox,QProgressBar,QPushButton,QTreeWidget,QTreeWidgetItem,QVBoxLayout,
)

from core import ExternalEvidenceImportCounts,ExternalEvidenceImportPlan,ExternalEvidenceImportPlanningService

from .modal_feedback import FeedbackEnabledDialog
from .workers import CancelableProgressCallTask


def external_evidence_result_dialog(
    parent,*,outcome: str,scope_label: str,updated: int,
    counts: ExternalEvidenceImportCounts,current_reused: int=0,
    recovered: int=0,held: int=0,failed: int=0,
) -> QMessageBox:
    """Build the shared exact import-result presentation for Ingest/Dataset."""
    complete=str(outcome or "complete")=="complete"
    dialog=QMessageBox(parent)
    dialog.setIcon(
        QMessageBox.Information if complete else QMessageBox.Warning
    )
    dialog.setWindowTitle(
        "External Evidence Import Complete"
        if complete else "External Evidence Import Finished with Problems"
    )
    dialog.setText(
        f"{scope_label} • updated Image Knowledge for "
        f"{max(0,int(updated)):,} image(s) this run"
    )
    lines=counts.summary_lines(
        current_reused=current_reused,recovered=recovered,
        held=held,failed=failed,
    )
    dialog.setInformativeText(
        "\n".join((*lines,"Confirmed facts stayed unchanged. No TXT, Metadata, or image file was written."))
    )
    dialog.setStandardButtons(QMessageBox.Ok)
    return dialog


class ExternalEvidenceImportDialog(FeedbackEnabledDialog):
    """Preview one frozen Dataset evidence scope before background intake."""

    feedback_workflow="External evidence import"
    MAX_VISIBLE_ROWS=200
    ACTION_LABELS={
        "read":"Read current source",
        "reuse":"Reuse current observation",
        "held":"Held",
        "not_selected":"Not selected",
    }

    def __init__(
        self,state,parent=None,*,context_label: str,all_paths,roots=(),
        availability=None,availability_many=None,selected=(),filtered=(),named_scopes=(),
        whole_scope_label: str="",checkpoint_service=None,
    ):
        super().__init__(parent)
        self.state=state
        self.context_label=str(context_label or "Images")
        self.all_paths=tuple(dict.fromkeys(Path(value) for value in all_paths))
        self.roots=tuple(dict.fromkeys(Path(value) for value in roots))
        self.availability=availability
        self.availability_many=availability_many
        self.checkpoint_service=checkpoint_service
        self.named_scopes={
            str(scope_id):(str(name),tuple(dict.fromkeys(Path(value) for value in paths)))
            for scope_id,name,paths in named_scopes
        }
        self.whole_scope_label=(
            str(whole_scope_label).strip()
            or f"All {self.context_label} images"
        )
        self.selected=tuple(dict.fromkeys(Path(value) for value in selected))
        self.filtered=tuple(dict.fromkeys(Path(value) for value in filtered))
        self.plan: ExternalEvidenceImportPlan|None=None
        self._folder=Path()
        self._task=None
        self._closing=False
        self._preview_controls_enabled=True
        self.setWindowTitle("Import External Evidence")
        self.resize(980,690)
        layout=QVBoxLayout(self)
        heading=QLabel("Choose and preview the exact evidence scope")
        heading.setObjectName("sectionTitle"); layout.addWidget(heading)
        intro=QLabel(
            "Read adjacent TXT captions and/or embedded Metadata into attributed, "
            "reviewable findings. Imported evidence is never silently confirmed, and "
            "this job does not run AI or write TXT, Metadata, or image files."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel"); layout.addWidget(intro)

        form=QFormLayout()
        self.scope=QComboBox(); self.scope.setAccessibleName("External evidence import scope")
        self.scope.addItem(
            f"Selected {self.context_label} images ({len(self.selected):,})",("selected","")
        )
        self.scope.addItem(
            f"Current {self.context_label} search and filters ({len(self.filtered):,})",
            ("current_filter",""),
        )
        for scope_id,(name,paths) in self.named_scopes.items():
            self.scope.addItem(
                f"Named library: {name} ({len(paths):,})",
                ("named_scope",scope_id),
            )
        self.scope.addItem(
            f"{self.whole_scope_label} ({len(self.all_paths):,})",("all",""),
        )
        self.scope.addItem(
            (
                "Folder within included libraries…"
                if self.named_scopes else
                f"Folder within {self.context_label}…"
            ),
            ("folder",""),
        )
        if not self.selected:
            self.scope.setCurrentIndex(1)
        form.addRow("Scope",self.scope)

        folder_row=QHBoxLayout()
        self.folder_label=QLabel("No folder chosen")
        self.folder_label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.choose_folder=QPushButton("Choose Folder…")
        self.choose_folder.clicked.connect(self._choose_folder)
        folder_row.addWidget(self.folder_label,1); folder_row.addWidget(self.choose_folder)
        folder_container=QHBoxLayout(); folder_container.addLayout(folder_row)
        form.addRow("Folder",folder_container)

        source_row=QHBoxLayout()
        self.txt_source=QCheckBox("Adjacent TXT"); self.txt_source.setChecked(True)
        self.metadata_source=QCheckBox("Embedded Metadata"); self.metadata_source.setChecked(True)
        source_row.addWidget(self.txt_source); source_row.addWidget(self.metadata_source)
        source_row.addStretch(1); form.addRow("Sources",source_row)
        self.mode=QComboBox(); self.mode.setAccessibleName("External evidence reread mode")
        self.mode.addItem("Read only missing or changed sources (recommended)","changed")
        self.mode.addItem("Re-read every selected source","all")
        form.addRow("Read mode",self.mode)
        layout.addLayout(form)

        active=[]
        jobs=getattr(self.state,"jobs",None)
        active_provider=getattr(jobs,"active",None)
        if callable(active_provider):
            active=[str(getattr(job,"name","") or "Background work") for job in active_provider()]
        self.competing=QLabel("Other background work: "+", ".join(active))
        self.competing.setWordWrap(True); self.competing.setObjectName("mutedLabel")
        self.competing.setVisible(bool(active))
        layout.addWidget(self.competing)

        preview_row=QHBoxLayout()
        self.preview_button=QPushButton("Build Exact Preview")
        self.preview_button.setProperty("primary",True)
        self.preview_button.clicked.connect(self._build_preview)
        self.progress=QProgressBar(); self.progress.setRange(0,1); self.progress.setValue(0)
        self.progress.setTextVisible(True); self.progress.setFormat("Preview not built")
        preview_row.addWidget(self.preview_button); preview_row.addWidget(self.progress,1)
        layout.addLayout(preview_row)
        from .components import bind_status_footer
        bind_status_footer(self,self.progress,title="Evidence preview")
        self.summary=QLabel(
            "Choose the exact scope, sources, and read mode, then build the preview."
        )
        self.summary.setWordWrap(True); layout.addWidget(self.summary)

        self.results=QTreeWidget()
        self.results.setHeaderLabels(("Image","Adjacent TXT","Embedded Metadata","Readiness"))
        self.results.setRootIsDecorated(False)
        self.results.header().setSectionResizeMode(0,QHeaderView.Stretch)
        self.results.header().setSectionResizeMode(1,QHeaderView.ResizeToContents)
        self.results.header().setSectionResizeMode(2,QHeaderView.ResizeToContents)
        self.results.header().setSectionResizeMode(3,QHeaderView.Stretch)
        layout.addWidget(self.results,1)
        self.row_limit=QLabel(); self.row_limit.setWordWrap(True)
        self.row_limit.setObjectName("mutedLabel"); layout.addWidget(self.row_limit)

        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.import_button=buttons.addButton(
            "Import Ready Evidence",QDialogButtonBox.AcceptRole,
        )
        self.import_button.setProperty("primary",True); self.import_button.setEnabled(False)
        self.import_button.clicked.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.scope.currentIndexChanged.connect(self._scope_changed)
        self.txt_source.toggled.connect(self._invalidate_preview)
        self.metadata_source.toggled.connect(self._invalidate_preview)
        self.mode.currentIndexChanged.connect(self._invalidate_preview)
        self._scope_changed()

    def _scope_changed(self,*_) -> None:
        mode,_value=self.scope.currentData() or ("","")
        folder_mode=mode=="folder"
        self.choose_folder.setEnabled(folder_mode and self._preview_controls_enabled)
        self.folder_label.setEnabled(folder_mode and self._preview_controls_enabled)
        self._invalidate_preview()

    def _set_preview_controls_enabled(self,enabled: bool) -> None:
        self._preview_controls_enabled=bool(enabled)
        self.scope.setEnabled(enabled); self.txt_source.setEnabled(enabled)
        self.metadata_source.setEnabled(enabled); self.mode.setEnabled(enabled)
        mode,_value=self.scope.currentData() or ("","")
        self.choose_folder.setEnabled(enabled and mode=="folder")
        self.folder_label.setEnabled(enabled and mode=="folder")

    def _choose_folder(self) -> None:
        working=next(iter(self.roots),Path())
        selected=QFileDialog.getExistingDirectory(
            self,
            (
                "Choose a folder within the included libraries"
                if self.named_scopes else
                f"Choose a folder within {self.context_label}"
            ),
            str(working),
        )
        if not selected:
            return
        self._folder=Path(selected)
        self.folder_label.setText(str(self._folder))
        self._invalidate_preview()

    @staticmethod
    def _within(path: Path,folder: Path) -> bool:
        path_key=os.path.normcase(os.path.abspath(os.fspath(path)))
        folder_key=os.path.normcase(os.path.abspath(os.fspath(folder)))
        try:
            return os.path.commonpath((path_key,folder_key))==folder_key
        except ValueError:
            return False

    def _scope_paths(self) -> tuple[tuple[Path,...],str]:
        mode,value=self.scope.currentData() or ("","")
        if mode=="selected":
            return self.selected,f"selected {self.context_label} images"
        if mode=="current_filter":
            return self.filtered,f"current {self.context_label} search and filters"
        if mode=="named_scope":
            name,paths=self.named_scopes.get(str(value),("",()))
            return paths,f"named library {name}" if name else "named library"
        if mode=="all":
            return self.all_paths,self.whole_scope_label.casefold()
        if mode=="folder" and self._folder:
            paths=tuple(
                Path(path) for path in self.all_paths
                if self._within(Path(path),self._folder)
            )
            return paths,f"folder {self._folder}"
        return (),"chosen folder"

    def _availability(self,image: Path) -> tuple[bool,str]:
        if self.availability is None:
            return True,""
        return self.availability(Path(image))

    def _selected_sources(self) -> tuple[str,...]:
        result=[]
        if self.txt_source.isChecked(): result.append("txt")
        if self.metadata_source.isChecked(): result.append("metadata")
        return tuple(result)

    def _invalidate_preview(self,*_) -> None:
        self.plan=None; self.import_button.setEnabled(False)
        self.results.clear(); self.row_limit.clear()
        if self._task is None:
            self.progress.setRange(0,1); self.progress.setValue(0)
            self.progress.setFormat("Preview required")

    def _build_preview(self) -> None:
        if self._task is not None:
            return
        paths,label=self._scope_paths(); sources=self._selected_sources()
        if not paths:
            self.summary.setText(
                f"The chosen scope contains no current {self.context_label} images. "
                "Nothing was broadened."
            )
            return
        if not sources:
            self.summary.setText("Choose Adjacent TXT, Embedded Metadata, or both.")
            return
        service=ExternalEvidenceImportPlanningService()
        mode=str(self.mode.currentData() or "changed")
        def build(progress,cancel):
            bulk=(
                self.availability_many(paths)
                if callable(self.availability_many) else None
            )
            availability=(
                (lambda image:bulk.get(
                    Path(image),(False,"The image is no longer in the checked scope."),
                ))
                if bulk is not None else self._availability
            )
            plan=service.build(
                paths,scope_label=label,sources=sources,mode=mode,
                availability=availability,progress=progress,
                should_cancel=cancel,
            )
            return (
                self.checkpoint_service.decorate(plan)
                if self.checkpoint_service is not None else plan
            )
        task=CancelableProgressCallTask(
            build
        )
        self._task=task; self.preview_button.setEnabled(False)
        self._set_preview_controls_enabled(False)
        self.progress.setRange(0,len(paths)); self.progress.setValue(0)
        self.progress.setFormat("Checking exact source state…")
        task.signals.progress.connect(self._preview_progress)
        task.signals.complete.connect(self._preview_complete)
        task.signals.cancelled.connect(self._preview_cancelled)
        task.signals.failed.connect(self._preview_failed)
        QThreadPool.globalInstance().start(task)

    def _preview_progress(self,current: int,total: int,label: str) -> None:
        if self._closing:
            return
        self.progress.setRange(0,max(1,total)); self.progress.setValue(current)
        self.progress.setFormat(f"{current:,} of {total:,} • {label}")

    def _preview_complete(self,plan: ExternalEvidenceImportPlan) -> None:
        if self._closing:
            self._task=None
            return
        self._task=None; self.preview_button.setEnabled(True); self.plan=plan
        self._set_preview_controls_enabled(True)
        self.progress.setRange(0,1); self.progress.setValue(1)
        self.progress.setFormat("Exact preview ready")
        resume_note=(
            f" Resume {plan.resume_count:,} verified checkpoint(s); "
            f"{len(plan.pending_ready_paths):,} ready image(s) remain."
            if plan.resume_available else ""
        )
        self.summary.setText(
            f"{plan.scope_label} • {len(plan.items):,} image(s) • "
            f"TXT reads {plan.txt_reads:,} • Metadata reads {plan.metadata_reads:,} • "
            f"reuse {plan.reused_count:,} current image(s) • held {plan.held_count:,}. "
            f"Estimated remaining work: {plan.pending_source_reads:,} source read(s)."
            f"{resume_note} "
            "Raw evidence remains attributed and reviewable; confirmed Image Knowledge is unchanged."
        )
        self.results.clear()
        for item in plan.items[:self.MAX_VISIBLE_ROWS]:
            row=QTreeWidgetItem((
                item.image.name,
                self.ACTION_LABELS.get(item.txt_action,item.txt_action),
                self.ACTION_LABELS.get(item.metadata_action,item.metadata_action),
                item.detail or ("Ready" if item.ready else "Held"),
            ))
            row.setToolTip(0,str(item.image)); self.results.addTopLevelItem(row)
        remaining=max(0,len(plan.items)-self.MAX_VISIBLE_ROWS)
        self.row_limit.setText(
            f"Showing the first {self.MAX_VISIBLE_ROWS:,} of {len(plan.items):,} exact rows. "
            "Every row remains in the frozen job scope."
            if remaining else ""
        )
        useful=bool(plan.pending_ready_paths) and (
            plan.mode=="all" or plan.pending_source_reads>0
        )
        self.import_button.setEnabled(useful)
        self.import_button.setText(
            "Resume Ready Evidence" if plan.resume_available and plan.held_count else
            "Resume External Evidence" if plan.resume_available else
            "Import Ready Evidence" if plan.held_count else
            "Import External Evidence"
        )
        if not useful and plan.resume_available and plan.resume_count==len(plan.ready_paths):
            self.summary.setText(
                self.summary.text()+" Every ready image already has a verified checkpoint; no remaining work was queued."
            )
        elif not useful and plan.ready_paths:
            self.summary.setText(
                self.summary.text()+" No selected source changed; choose re-read every selected source only if a deliberate reread is needed."
            )

    def _preview_cancelled(self) -> None:
        if self._closing:
            self._task=None
            return
        self._task=None; self.preview_button.setEnabled(True)
        self._set_preview_controls_enabled(True)
        self.progress.setRange(0,1); self.progress.setValue(0)
        self.progress.setFormat("Preview cancelled safely")

    def _preview_failed(self,error: str) -> None:
        if self._closing:
            self._task=None
            return
        self._task=None; self.preview_button.setEnabled(True)
        self._set_preview_controls_enabled(True)
        self.progress.setRange(0,1); self.progress.setValue(0)
        self.progress.setFormat("Preview failed")
        self.summary.setText(
            "The preview could not be completed. No evidence was imported. "+str(error)
        )

    def reject(self) -> None:
        self._closing=True
        if self._task is not None:
            self._task.cancel(); self._task=None
        super().reject()

    def accept(self) -> None:
        if self.plan is None or not self.import_button.isEnabled():
            return
        super().accept()
