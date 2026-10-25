from __future__ import annotations

from PySide6.QtCore import QEvent,Qt
from PySide6.QtWidgets import (
    QCheckBox,QComboBox,QDialogButtonBox,QFormLayout,QGroupBox,QHeaderView,QHBoxLayout,QLabel,QLineEdit,QPushButton,
    QRadioButton,QTreeWidget,QTreeWidgetItem,QVBoxLayout,
)

from core.dataset_export import DatasetCaptionExportPlan
from core.external_write_policy import ExternalWritePolicy

from .components import counted_noun
from .modal_feedback import FeedbackEnabledDialog
from .training_caption_review_view import CaptionPageSpinBox,PagedCaptionText


def _format_bytes(value: int) -> str:
    amount=float(max(0,int(value)))
    units=("bytes","KB","MB","GB","TB")
    unit=units[0]
    for candidate in units:
        unit=candidate
        if amount<1024 or candidate==units[-1]:
            break
        amount/=1024
    return (
        f"{int(amount):,} {unit}" if unit=="bytes" else
        f"{amount:,.1f} {unit}"
    )


class TrainingTXTExportDestinationDialog(FeedbackEnabledDialog):
    """Choose one deliberate TXT location and resolve shared backup policy."""

    feedback_workflow="Training TXT export destination"

    def __init__(
        self,policy: ExternalWritePolicy,registry,parent=None,
    ):
        super().__init__(parent)
        self.policy=policy
        self.registry=registry
        self.action=""
        self.setWindowTitle("Choose Training TXT Destination")
        self.resize(760,470)
        layout=QVBoxLayout(self)
        heading=QLabel("Choose where the generated Training TXT should live")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        intro=QLabel(
            "Both choices use the same selected profile and exact aggregate "
            "preview. Neither choice changes Image Knowledge, images, or Metadata."
        )
        intro.setWordWrap(True)
        intro.setObjectName("mutedLabel")
        layout.addWidget(intro)
        destination=QGroupBox("TXT destination")
        destination_layout=QVBoxLayout(destination)
        self.adjacent=QRadioButton(
            "Beside each source image — create or replace its adjacent TXT"
        )
        self.adjacent.setChecked(True)
        self.separate=QRadioButton(
            "Separate training folder — keep source-adjacent TXT unchanged"
        )
        self.separate.toggled.connect(self._refresh)
        self.adjacent.toggled.connect(self._refresh)
        destination_layout.addWidget(self.adjacent)
        destination_layout.addWidget(self.separate)
        layout.addWidget(destination)
        protection=QGroupBox("Shared external-write protection")
        protection_layout=QVBoxLayout(protection)
        mode_labels={
            "always":"Always (recommended)",
            "ask":"Ask for each operation",
            "off":"Off",
        }
        protection_layout.addWidget(QLabel(
            "Back up originals before external writes: "
            +mode_labels.get(policy.backup_mode,policy.backup_mode)
        ))
        self.ask_backup=QCheckBox(
            "Create verified backups for replaced TXT in this export"
        )
        self.ask_backup.setChecked(True)
        self.ask_backup.setVisible(policy.asks_each_time)
        self.ask_backup.toggled.connect(self._refresh)
        protection_layout.addWidget(self.ask_backup)
        self.backup_status=QLabel()
        self.backup_status.setWordWrap(True)
        self.backup_status.setObjectName("mutedLabel")
        protection_layout.addWidget(self.backup_status)
        self.configure_button=QPushButton(
            "Open Automation Backup Settings…"
        )
        self.configure_button.clicked.connect(self._configure)
        protection_layout.addWidget(self.configure_button)
        layout.addWidget(protection)
        self.consequence=QLabel()
        self.consequence.setWordWrap(True)
        self.consequence.setObjectName("mutedLabel")
        layout.addWidget(self.consequence)
        layout.addStretch(1)
        buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel)
        self.continue_button=buttons.button(QDialogButtonBox.Ok)
        self.continue_button.setProperty("primary",True)
        buttons.accepted.connect(self._continue)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh()

    @property
    def destination_mode(self) -> str:
        return "adjacent" if self.adjacent.isChecked() else "separate"

    @property
    def backup_enabled(self) -> bool:
        return self.policy.backup_enabled(
            self.ask_backup.isChecked() if self.policy.asks_each_time else None
        )

    def _refresh(self,*_):
        enabled=self.backup_enabled
        issues=self.policy.backup_destination_issues(
            self.registry,enabled=enabled,
        )
        directory=self.policy.configured_backup_directory
        if not enabled:
            status=(
                "No verified backup or automatic restore will be created for "
                "this export. Earlier protected operations remain available. "
                "The exact plan repeats this consequence before writing."
            )
        elif issues:
            status="Protected writing is blocked: "+" ".join(issues)
        else:
            status=f"Verified replacement backups and recovery: {directory}"
        self.backup_status.setText(status)
        self.configure_button.setVisible(bool(issues))
        self.continue_button.setEnabled(not issues)
        self.continue_button.setText(
            "Preview Adjacent TXT"
            if self.destination_mode=="adjacent" else "Choose Training Folder"
        )
        self.consequence.setText(
            (
                "Adjacent mode may replace source-adjacent TXT after the exact "
                "preview. That TXT remains external evidence; confirmed Image "
                "Knowledge is not replaced by the generated caption."
                if self.destination_mode=="adjacent" else
                "Separate-folder mode writes a dedicated training tree and "
                "leaves every source-adjacent TXT unchanged."
            )
        )

    def _continue(self):
        self.action="continue"
        self.accept()

    def _configure(self):
        self.action="configure"
        self.accept()


class TrainingTXTExportPlanDialog(FeedbackEnabledDialog):
    """Review one exact Training TXT export plan before any write."""

    feedback_workflow="Training TXT export plan"
    MAX_VISIBLE_ROWS=200

    LABELS={
        "create":"Create new TXT",
        "replace":"Replace after verified backup",
        "current":"Already current — no write",
        "excluded":"Excluded by rating limit",
        "empty":"Profile output is empty",
        "collision":"Held — destination collision",
        "failed":"Held — preview failed",
    }

    def __init__(
        self,plan: DatasetCaptionExportPlan,parent=None,*,
        restore_available: bool=False,recovery_required: bool=False,
    ):
        super().__init__(parent)
        self.plan=plan
        self.action=""
        self.setWindowTitle("Review Training TXT Export")
        self.resize(1180,720)
        layout=QVBoxLayout(self)
        heading=QLabel(
            "Review exact adjacent Training TXT export"
            if plan.destination_mode=="adjacent" else
            "Review exact separate-folder Training TXT export"
        )
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        results=(("Create",plan.count("create")),("Replace",plan.count("replace")),
                 ("Already current",plan.count("current")),("Excluded",plan.count("excluded")),
                 ("Empty",plan.count("empty")),("Held",plan.count("collision")+plan.count("failed")))
        summary=QLabel(" • ".join((
            f"Previewed {counted_noun(len(plan.items),'image')}",
            *(f"{name} {count:,}" for name,count in results if count),
        )))
        summary.setWordWrap(True)
        layout.addWidget(summary)
        destination_text=(
            "Beside each source image"
            if plan.destination_mode=="adjacent" else str(plan.output_directory)
        )
        manifest_text=(
            plan.manifest_disposition.title()
            if plan.manifest_disposition!="none" else "No separate export manifest"
        )
        backup_text=(
            "Verified backups and recovery: "
            +str(plan.backup_directory or "Operation-local protected backup")
            if plan.backup_enabled else "Off — no recovery for this new export"
        )
        details=QFormLayout()
        available=(_format_bytes(plan.available_free_bytes)
                   if plan.available_free_bytes is not None else "Could not verify")
        for name,value in (
            ("Destination",destination_text),
            ("Caption profile",f"{plan.profile.name} v{plan.profile.version}"),
            ("Export files",f"{plan.write_count:,} TXT writes • Manifest: {manifest_text}"),
            ("Free space",f"Required: {_format_bytes(plan.required_free_bytes)} • Available: {available}"),
            ("Write protection",backup_text),
        ):
            label=QLabel(value)
            label.setTextFormat(Qt.PlainText)
            label.setWordWrap(True)
            label.setTextInteractionFlags(Qt.TextSelectableByMouse)
            label.setObjectName("mutedLabel")
            details.addRow(name+":",label)
        layout.addLayout(details)
        if not plan.space_sufficient:
            space=QLabel(
                (
                    "CollectiGen could not verify free space in the chosen "
                    "folder. Nothing can be exported until the destination is "
                    "available for an exact space check."
                    if plan.available_free_bytes is None else
                    "The chosen folder does not have enough free space for the "
                    "planned TXT, verified backups, and atomic manifest write. "
                    "Nothing can be exported until space is available."
                )
            )
            space.setWordWrap(True)
            space.setProperty("severity","warning")
            layout.addWidget(space)
        if recovery_required:
            recovery=QLabel(
                "An interrupted recovery record exists for this exact destination "
                "and target scope. Restore its verified changes before starting "
                "another export."
            )
            recovery.setWordWrap(True)
            recovery.setProperty("severity","warning")
            layout.addWidget(recovery)
        elif restore_available:
            recovery=QLabel(
                "The most recent protected operation for this exact destination "
                "and target scope remains restorable. Restore stops without changing "
                "anything if newer output is present."
            )
            recovery.setWordWrap(True)
            recovery.setObjectName("mutedLabel")
            layout.addWidget(recovery)
        self.privacy_confirmation=None
        if plan.privacy_confirmation_required:
            boundary=QLabel(plan.privacy_message)
            boundary.setWordWrap(True)
            boundary.setProperty("severity","warning")
            layout.addWidget(boundary)
            self.privacy_confirmation=QCheckBox(
                "I confirm this exact cross-zone export destination"
            )
            layout.addWidget(self.privacy_confirmation)
        self.search=QLineEdit()
        self.search.setPlaceholderText("Find an image, target or caption in this plan…")
        self.search.setAccessibleName("Search all planned Training TXT results")
        self.search.setClearButtonEnabled(True)
        self.search.installEventFilter(self)
        self.result_filter=QComboBox()
        self.result_filter.setAccessibleName("Filter planned Training TXT results")
        self.result_filter.addItem("All results","")
        for key in self.LABELS:
            self.result_filter.addItem(self._result_label(key),key)
        filters=QHBoxLayout()
        filters.addWidget(self.search,1)
        filters.addWidget(self.result_filter)
        layout.addLayout(filters)
        self.tree=QTreeWidget()
        self.tree.setHeaderLabels(("Image","Training TXT target","Result","Caption or reason preview"))
        self.tree.setRootIsDecorated(False)
        self.tree.setUniformRowHeights(True)
        self.tree.setAccessibleName("Paged exact Training TXT export results")
        self.tree.setAlternatingRowColors(False)
        self.tree.header().setSectionResizeMode(0,QHeaderView.Interactive)
        self.tree.header().setSectionResizeMode(1,QHeaderView.Interactive)
        self.tree.header().setSectionResizeMode(2,QHeaderView.Interactive)
        self.tree.header().setSectionResizeMode(3,QHeaderView.Stretch)
        self.tree.setColumnWidth(0,170)
        self.tree.setColumnWidth(1,250)
        self.tree.setColumnWidth(2,210)
        layout.addWidget(self.tree,1)
        paging=QHBoxLayout()
        self.previous=QPushButton("Previous")
        self.next=QPushButton("Next")
        self.page=CaptionPageSpinBox()
        self.page.setPrefix("Page ")
        self.page.setAccessibleName("Training TXT review page")
        self.page_status=QLabel()
        self.page_status.setWordWrap(True)
        for control in (self.previous,self.next):
            control.setAutoDefault(False)
        paging.addWidget(self.previous)
        paging.addWidget(self.page)
        paging.addWidget(self.next)
        paging.addWidget(self.page_status,1)
        layout.addLayout(paging)
        scope=QLabel(
            f"Export scope: all {len(plan.items):,} planned images. "
            "Search and paging change this review only."
        )
        scope.setWordWrap(True)
        scope.setObjectName("mutedLabel")
        layout.addWidget(scope)
        self.selected_target=QLineEdit()
        self.selected_target.setReadOnly(True)
        self.selected_target.setAccessibleName("Selected Training TXT target")
        self.selected_target.installEventFilter(self)
        self.selected_target.setPlaceholderText("Select a planned image to inspect its exact TXT target.")
        selected_target_row=QHBoxLayout()
        selected_target_row.addWidget(QLabel("Selected TXT target"))
        selected_target_row.addWidget(self.selected_target,1)
        layout.addLayout(selected_target_row)
        self.caption=PagedCaptionText("Selected exact caption or reason")
        self.caption.setMaximumHeight(140)
        layout.addWidget(self.caption)
        self.search.textChanged.connect(self._filter_results)
        self.result_filter.currentIndexChanged.connect(self._filter_results)
        self.page.valueChanged.connect(self._render_page)
        self.previous.clicked.connect(lambda:self.page.setValue(self.page.value()-1))
        self.next.clicked.connect(lambda:self.page.setValue(self.page.value()+1))
        self.tree.currentItemChanged.connect(self._show_selected_result)
        self._filter_results()
        protection_text=(
            "Existing targets are backed up and verified before any replacement; "
            if plan.backup_enabled else
            "Backup is Off, so replaced TXT cannot be restored automatically; "
        )
        consequence=QLabel(
            (
                "Export writes only the planned adjacent TXT targets. "
                +protection_text
                +"unchanged TXT keeps its timestamp. Image Knowledge, source "
                "images, and Metadata remain unchanged."
                if plan.destination_mode=="adjacent" else
                "Export writes only the planned separate-folder TXT targets and "
                "its manifest. "+protection_text
                +"unchanged TXT keeps its timestamp. Image Knowledge, source "
                "images, Metadata, and adjacent source TXT remain unchanged."
            )
        )
        consequence.setWordWrap(True)
        consequence.setObjectName("mutedLabel")
        layout.addWidget(consequence)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.restore_button=QPushButton("Restore Last Export")
        self.restore_button.clicked.connect(self._restore)
        buttons.addButton(self.restore_button,QDialogButtonBox.ActionRole)
        self.restore_button.setVisible(restore_available)
        write_label=(
            f"Export {plan.write_count:,} Changed TXT"
            if plan.write_count else
            "Update Export Manifest"
            if plan.manifest_disposition not in {"current","none"} else
            "Nothing to Export"
        )
        self.export_button=buttons.addButton(
            write_label,QDialogButtonBox.AcceptRole,
        )
        self.export_button.setProperty("primary",True)
        self._base_export_enabled=(
            plan.space_sufficient and not recovery_required and (
                bool(plan.write_count)
                or plan.manifest_disposition not in {"current","none"}
            )
        )
        self.export_button.setEnabled(
            self._base_export_enabled
            and self.privacy_confirmation is None
        )
        if self.privacy_confirmation is not None:
            self.privacy_confirmation.toggled.connect(
                lambda checked:self.export_button.setEnabled(
                    self._base_export_enabled and checked
                )
            )
        buttons.accepted.connect(self._export)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def eventFilter(self,obj,event):
        if ((obj is self.search or obj is getattr(self,"selected_target",None))
                and event.type()==QEvent.KeyPress
                and event.key() in (Qt.Key_Return,Qt.Key_Enter)):
            return True
        return super().eventFilter(obj,event)

    def _result_label(self,key):
        if key=="replace" and not self.plan.backup_enabled:
            return "Replace TXT — no backup"
        return self.LABELS.get(key,key.title())

    def _filter_results(self,*_args):
        query=self.search.text().strip().casefold()
        disposition=self.result_filter.currentData()
        self._review_indices=tuple(
            index for index,item in enumerate(self.plan.items)
            if (not disposition or item.disposition==disposition)
            and (not query or query in "\n".join((
                str(item.image),str(item.target or ""),item.caption,item.error,
            )).casefold())
        )
        self.page.blockSignals(True)
        self.page.setRange(1,max(1,(len(self._review_indices)+self.MAX_VISIBLE_ROWS-1)//self.MAX_VISIBLE_ROWS))
        self.page.setValue(1)
        self.page.blockSignals(False)
        self._render_page()

    def _render_page(self,*_args):
        self.tree.clear()
        start=(self.page.value()-1)*self.MAX_VISIBLE_ROWS
        indices=self._review_indices[start:start+self.MAX_VISIBLE_ROWS]
        for index in indices:
            item=self.plan.items[index]
            target=(
                (
                    str(item.target)
                    if self.plan.destination_mode=="adjacent" else
                    item.target.relative_to(self.plan.output_directory).as_posix()
                )
                if item.target is not None else "Unavailable"
            )
            detail=item.error or item.caption or "—"
            row=QTreeWidgetItem((
                item.image.name,target,
                self._result_label(item.disposition),
                detail[:240].replace("\n"," ")+ ("…" if len(detail)>240 else ""),
            ))
            row.setToolTip(0,str(item.image))
            row.setToolTip(1,str(item.target or ""))
            row.setToolTip(3,"Select this image to read the complete caption or reason below.")
            row.setData(0,Qt.UserRole,index)
            self.tree.addTopLevelItem(row)
        self.previous.setEnabled(self.page.value()>1)
        self.next.setEnabled(self.page.value()<self.page.maximum())
        for control in (self.previous,self.page,self.next):
            control.setVisible(self.page.maximum()>1)
        self.page_status.setText(
            f"Results {start+1:,}–{start+len(indices):,} of {len(self._review_indices):,}"
            if indices else "No matching planned images. Clear search or choose All results."
        )
        if indices:
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        else:
            self._show_selected_result(None)

    def _show_selected_result(self,row,*_args):
        if row is None:
            self.selected_target.clear()
            self.caption.set_text("")
            return
        item=self.plan.items[row.data(0,Qt.UserRole)]
        self.selected_target.setText(str(item.target or "Unavailable"))
        self.selected_target.setCursorPosition(0)
        self.caption.set_text(item.error or item.caption or "No caption output.")

    def _export(self):
        self.action="export"
        self.accept()

    def _restore(self):
        self.action="restore"
        self.accept()
