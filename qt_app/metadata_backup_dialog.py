from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,QDialogButtonBox,QGroupBox,QHeaderView,QLabel,QPushButton,
    QTreeWidget,QTreeWidgetItem,QVBoxLayout,
)

from core import ExternalWritePolicy,MetadataBackupPlan,MetadataBackupResult

from .modal_feedback import FeedbackEnabledDialog


def _format_bytes(value: int) -> str:
    amount=float(max(0,int(value)))
    units=("bytes","KB","MB","GB","TB")
    for unit in units:
        if amount<1024 or unit==units[-1]:
            return f"{int(amount):,} {unit}" if unit=="bytes" else f"{amount:,.1f} {unit}"
        amount/=1024
    return f"{amount:,.1f} TB"


class MetadataBackupProtectionDialog(FeedbackEnabledDialog):
    """Resolve the Automation-owned protection policy before Metadata reads."""

    feedback_workflow="Metadata backup protection"

    def __init__(self,policy: ExternalWritePolicy,registry,parent=None):
        super().__init__(parent)
        self.policy=policy; self.registry=registry; self.action=""
        self.setWindowTitle("Choose Metadata Backup Protection")
        self.resize(720,380)
        layout=QVBoxLayout(self)
        heading=QLabel("Protect the source images before changing managed Metadata")
        heading.setObjectName("sectionTitle"); layout.addWidget(heading)
        intro=QLabel(
            "Back Up Tags to Metadata writes the complete confirmed Tag set and "
            "current confirmed Descriptions only to CollectiGen-managed XMP fields. "
            "It does not create or change Training TXT."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel"); layout.addWidget(intro)
        group=QGroupBox("Shared external-write protection")
        group_layout=QVBoxLayout(group)
        labels={"always":"Always (recommended)","ask":"Ask for each operation","off":"Off"}
        group_layout.addWidget(QLabel(
            "Back up originals before external writes: "
            +labels.get(policy.backup_mode,policy.backup_mode)
        ))
        self.ask_backup=QCheckBox(
            "Create verified source-image and Image Knowledge backups for this operation"
        )
        self.ask_backup.setChecked(True)
        self.ask_backup.setVisible(policy.asks_each_time)
        self.ask_backup.toggled.connect(self._refresh)
        group_layout.addWidget(self.ask_backup)
        self.backup_status=QLabel(); self.backup_status.setWordWrap(True)
        self.backup_status.setObjectName("mutedLabel"); group_layout.addWidget(self.backup_status)
        self.configure_button=QPushButton("Open Automation Backup Settings…")
        self.configure_button.clicked.connect(self._configure)
        group_layout.addWidget(self.configure_button)
        layout.addWidget(group); layout.addStretch(1)
        buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel)
        self.preview_button=buttons.button(QDialogButtonBox.Ok)
        self.preview_button.setText("Preview Exact Metadata Targets")
        self.preview_button.setProperty("primary",True)
        buttons.accepted.connect(self._continue); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons); self._refresh()

    @property
    def backup_enabled(self) -> bool:
        return self.policy.backup_enabled(
            self.ask_backup.isChecked() if self.policy.asks_each_time else None
        )

    def _refresh(self,*_):
        issues=self.policy.backup_destination_issues(
            self.registry,enabled=self.backup_enabled,
        )
        if not self.backup_enabled:
            text=(
                "No external Metadata backup will be retained for this operation. "
                "Configured private history keeps its required recovery copies. "
                "Earlier protected operations remain available."
            )
        elif issues:
            text="Protected writing is blocked: "+" ".join(issues)
        else:
            text=(
                "Verified source-image and Image Knowledge backups plus recovery: "
                +str(self.policy.configured_backup_directory)
            )
        self.backup_status.setText(text)
        self.configure_button.setVisible(bool(issues))
        self.preview_button.setEnabled(not issues)

    def _continue(self):
        self.action="continue"; self.accept()

    def _configure(self):
        self.action="configure"; self.accept()


class MetadataBackupPlanDialog(FeedbackEnabledDialog):
    """Review one frozen Metadata-only plan and its recovery consequence."""

    feedback_workflow="Metadata backup plan"
    MAX_VISIBLE_ROWS=200
    LABELS={
        "change":"Change and verify managed Metadata",
        "current":"Already current — no write",
        "unsupported":"Held — Metadata unavailable",
        "blocked":"Held — preview problem",
        "missing":"Held — no saved Image Knowledge",
    }

    def __init__(
        self,plan: MetadataBackupPlan,parent=None,*,restore_available: bool=False,
        recovery_required: bool=False,
    ):
        super().__init__(parent)
        self.plan=plan; self.action=""; self.privacy_confirmation=None
        self.setWindowTitle("Review Back Up Tags to Metadata")
        self.resize(1180,720)
        layout=QVBoxLayout(self)
        heading=QLabel("Review exact managed Metadata targets")
        heading.setObjectName("sectionTitle"); layout.addWidget(heading)
        summary=QLabel(
            f"Previewed {len(plan.items):,} "
            f"{'image' if len(plan.items)==1 else 'images'} • Change {plan.write_count:,} • "
            f"Already current {plan.count('current'):,} • Unsupported {plan.count('unsupported'):,} • "
            f"Missing authority {plan.count('missing'):,} • Blocked {plan.count('blocked'):,}"
        )
        summary.setWordWrap(True); layout.addWidget(summary)
        protection=(
            "Verified source-image and Image Knowledge backups: "+str(plan.backup_directory)
            if plan.backup_enabled else
            "Off — private history still retains saved Image Knowledge and required recovery copies"
            if plan.history_operations else "Off — no external Metadata backup for this new operation"
        )
        detail=QLabel(
            "Managed fields: XMP:Subject and XMP:Description • "
            f"Conservative required free space: {_format_bytes(plan.required_free_bytes)} • "
            "Available: "
            +( _format_bytes(plan.available_free_bytes) if plan.available_free_bytes is not None else "Could not verify" )
            +"\nExternal-write protection: "+protection
        )
        detail.setWordWrap(True); detail.setObjectName("mutedLabel"); layout.addWidget(detail)
        if not plan.space_sufficient:
            warning=QLabel(
                "The protected destination is unavailable or does not have enough "
                "verified free space. Nothing can be written."
            )
            warning.setWordWrap(True); warning.setProperty("severity","warning"); layout.addWidget(warning)
        if recovery_required:
            recovery=QLabel(
                "An interrupted protected operation matches this exact image scope. "
                "Restore its verified changes before starting another Metadata backup."
            )
            recovery.setWordWrap(True); recovery.setProperty("severity","warning"); layout.addWidget(recovery)
        elif restore_available:
            recovery=QLabel(
                "The most recent protected Metadata operation for this exact image scope "
                "is restorable. Newer data is held. Completed image restores are retained if a later image cannot finish."
                if plan.history_operations else
                "The most recent protected Metadata operation for this exact image scope "
                "is restorable. Restore changes nothing if any target is newer."
            )
            recovery.setWordWrap(True); recovery.setObjectName("mutedLabel"); layout.addWidget(recovery)
        if plan.privacy_confirmation_required:
            boundary=QLabel(plan.privacy_message); boundary.setWordWrap(True)
            boundary.setProperty("severity","warning"); layout.addWidget(boundary)
            self.privacy_confirmation=QCheckBox(
                "I confirm this exact cross-zone Metadata backup and recovery destination"
            )
            layout.addWidget(self.privacy_confirmation)
        self.tree=QTreeWidget()
        self.tree.setHeaderLabels(("Image","Managed Metadata target","Plan","Exact field changes or reason"))
        self.tree.setRootIsDecorated(False); self.tree.setAlternatingRowColors(False)
        self.tree.header().setSectionResizeMode(0,QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(1,QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(2,QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(3,QHeaderView.Stretch)
        for item in plan.items[:self.MAX_VISIBLE_ROWS]:
            row=QTreeWidgetItem((
                item.image.name,"XMP:Subject + XMP:Description",
                self.LABELS.get(item.disposition,item.disposition.title()),item.detail,
            ))
            row.setToolTip(0,str(item.image)); row.setToolTip(3,item.detail)
            self.tree.addTopLevelItem(row)
        layout.addWidget(self.tree,1)
        consequence=QLabel(
            "Only the listed managed XMP fields and their verified projection state can "
            "change. Exact generator data, standard Metadata, Image Knowledge facts, "
            "adjacent TXT, and file locations remain unchanged."
        )
        consequence.setWordWrap(True); consequence.setObjectName("mutedLabel"); layout.addWidget(consequence)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.restore_button=QPushButton("Restore Last Metadata Backup")
        self.restore_button.clicked.connect(self._restore)
        buttons.addButton(self.restore_button,QDialogButtonBox.ActionRole)
        self.restore_button.setVisible(restore_available)
        self.apply_button=buttons.addButton(
            (
                f"Back Up Tags for {plan.write_count:,} "
                f"{'Image' if plan.write_count==1 else 'Images'}"
            ) if plan.write_count else "Nothing to Change",
            QDialogButtonBox.AcceptRole,
        )
        self.apply_button.setProperty("primary",True)
        self._base_apply_enabled=(
            plan.space_sufficient and not recovery_required and bool(plan.write_count)
        )
        self.apply_button.setEnabled(
            self._base_apply_enabled and self.privacy_confirmation is None
        )
        if self.privacy_confirmation is not None:
            self.privacy_confirmation.toggled.connect(
                lambda checked:self.apply_button.setEnabled(self._base_apply_enabled and checked)
            )
        buttons.accepted.connect(self._apply); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply(self):
        self.action="apply"; self.accept()

    def _restore(self):
        self.action="restore"; self.accept()


class MetadataBackupResultDialog(FeedbackEnabledDialog):
    """Keep exact per-image Metadata outcomes inspectable after the preview closes."""

    feedback_workflow="Metadata backup result"
    LABELS={
        "verified":"Changed and verified",
        "current":"Already current",
        "unsupported":"Unsupported / unavailable",
        "blocked":"Blocked before write",
        "missing":"No saved Image Knowledge",
        "failed":"Failed",
        "not_processed":"Not processed",
    }

    def __init__(self,result: MetadataBackupResult,parent=None):
        super().__init__(parent)
        self.setWindowTitle("Back Up Tags to Metadata Results")
        self.resize(1080,650)
        layout=QVBoxLayout(self)
        heading=QLabel("Managed Metadata backup results")
        heading.setObjectName("sectionTitle"); layout.addWidget(heading)
        summary=QLabel(
            f"Changed and verified {result.changed:,} • Already current {result.current:,} • "
            f"Unsupported {result.count('unsupported'):,} • Blocked {result.count('blocked')+result.count('missing'):,} • "
            f"Failed {result.failed:,} • Automatically recovered {result.recovered:,} • "
            f"Not processed {result.count('not_processed'):,}"
        )
        if result.cancelled:
            summary.setText("Cancelled safely after completed images • "+summary.text())
        summary.setWordWrap(True); layout.addWidget(summary)
        recovery=QLabel(
            "Protected recovery: "+str(result.recovery_manifest)
            if result.recovery_manifest is not None else
            "External Metadata backup was Off. Saved Image Knowledge and recovery copies remain in your configured private history storage."
            if result.native_history else
            "Protection was Off for this operation, so no durable restore was created."
        )
        recovery.setWordWrap(True); recovery.setObjectName("mutedLabel"); layout.addWidget(recovery)
        tree=QTreeWidget()
        tree.setHeaderLabels(("Image","Managed fields","Result","Detail"))
        tree.setRootIsDecorated(False); tree.setAlternatingRowColors(False)
        tree.header().setSectionResizeMode(0,QHeaderView.ResizeToContents)
        tree.header().setSectionResizeMode(1,QHeaderView.ResizeToContents)
        tree.header().setSectionResizeMode(2,QHeaderView.ResizeToContents)
        tree.header().setSectionResizeMode(3,QHeaderView.Stretch)
        for item in result.items:
            label=self.LABELS.get(item.disposition,item.disposition.title())
            if item.recovered:
                label+=" — original restored"
            row=QTreeWidgetItem((
                item.image.name,"XMP:Subject + XMP:Description",label,item.detail or "—",
            ))
            row.setToolTip(0,str(item.image)); row.setToolTip(3,item.detail)
            tree.addTopLevelItem(row)
        layout.addWidget(tree,1)
        note=QLabel(
            "Training TXT was not created or changed. Run Back Up Tags to Metadata "
            "again with the same exact scope to inspect or restore the last protected operation."
        )
        note.setWordWrap(True); note.setObjectName("mutedLabel"); layout.addWidget(note)
        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject); layout.addWidget(buttons)
