from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,QDialogButtonBox,QFormLayout,QGroupBox,QLabel,QPlainTextEdit,
    QPushButton,QSizePolicy,QVBoxLayout,
)

from core import (
    PAIRED_IMAGE_KNOWLEDGE,PROJECTION_EXCLUSION,SOURCE_ONLY,ExternalWritePolicy,
    SourceCorrectionPlan,SourceCorrectionResult,
)

from .modal_feedback import FeedbackEnabledDialog


class SourceCorrectionProtectionDialog(FeedbackEnabledDialog):
    """Resolve the shared protection policy before reading one exact TXT plan."""

    feedback_workflow = "Source-correction protection"

    def __init__(
        self,policy: ExternalWritePolicy,registry,parent=None,*,
        source_kind: str="training_txt",value_kind: str="tag",
    ):
        super().__init__(parent)
        self.policy=policy
        self.registry=registry
        self.source_kind=source_kind
        self.value_kind=value_kind
        self.source_label=(
            "managed Metadata" if source_kind=="managed_metadata"
            else "Training TXT"
        )
        self.allow_projection=(
            source_kind=="training_txt" and value_kind=="tag"
        )
        self.action=""
        self.setWindowTitle(f"Choose {self.source_label} Correction Protection")
        self.resize(700,350)
        layout=QVBoxLayout(self)
        heading=QLabel(f"Protect the exact {self.source_label} before changing it")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        intro=QLabel(
            f"The source-only action changes one exact {self.source_label} value. "
            "Image Knowledge and every unrelated source field stay unchanged."
        )
        intro.setWordWrap(True)
        intro.setObjectName("mutedLabel")
        layout.addWidget(intro)
        group=QGroupBox("Shared external-write protection")
        group_layout=QVBoxLayout(group)
        labels={
            "always":"Always (recommended)","ask":"Ask for each operation",
            "off":"Off",
        }
        group_layout.addWidget(QLabel(
            "Back up originals before external writes: "
            +labels.get(policy.backup_mode,policy.backup_mode)
        ))
        self.ask_backup=QCheckBox(
            f"Create a verified {self.source_label} backup for this correction"
        )
        self.ask_backup.setChecked(True)
        self.ask_backup.setVisible(policy.asks_each_time)
        self.ask_backup.toggled.connect(self._refresh)
        group_layout.addWidget(self.ask_backup)
        self.backup_status=QLabel()
        self.backup_status.setWordWrap(True)
        self.backup_status.setObjectName("mutedLabel")
        group_layout.addWidget(self.backup_status)
        self.configure_button=QPushButton("Open Automation Backup Settings…")
        self.configure_button.clicked.connect(self._configure)
        group_layout.addWidget(self.configure_button)
        self.projection_button=QPushButton(
            "Keep Fact, Omit from Training Instead…"
        )
        self.projection_button.setProperty("compact",True)
        self.projection_button.clicked.connect(self._projection_exclusion)
        group_layout.addWidget(self.projection_button)
        layout.addWidget(group); layout.addStretch(1)
        buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel)
        self.preview_button=buttons.button(QDialogButtonBox.Ok)
        self.preview_button.setText(f"Preview Exact {self.source_label} Correction")
        self.preview_button.setProperty("primary",True)
        buttons.accepted.connect(self._continue)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh()

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
                "Off — no external backup is created. Configured saved history "
                "may retain private recovery; the exact preview shows its protection."
            )
        elif issues:
            text="Protected correction is blocked: "+" ".join(issues)
        else:
            text=(
                f"Verified {self.source_label} backup and recovery: "
                +str(self.policy.configured_backup_directory)
            )
        self.backup_status.setText(text)
        self.configure_button.setVisible(bool(issues))
        self.projection_button.setVisible(bool(issues) and self.allow_projection)
        self.preview_button.setEnabled(not issues)

    def _continue(self):
        self.action="continue"
        self.accept()

    def _configure(self):
        self.action="configure"
        self.accept()

    def _projection_exclusion(self):
        self.action=PROJECTION_EXCLUSION
        self.accept()


class SourceCorrectionPlanDialog(FeedbackEnabledDialog):
    """Show one frozen exact-value source correction without broadening scope."""

    feedback_workflow = "Source-correction preview"

    def __init__(self,plan: SourceCorrectionPlan,parent=None):
        super().__init__(parent)
        self.plan=plan
        self.action=""
        self.privacy_confirmation=None
        metadata=getattr(plan,"source_kind","training_txt")=="managed_metadata"
        source_label="managed Metadata" if metadata else "Training TXT"
        value_label=(
            "Plain Language Description"
            if getattr(plan,"value_kind","tag")=="description" else "SDXL Tag"
        )
        self.setWindowTitle(f"Review {source_label} Correction")
        self.resize(820,680)
        self.setMaximumWidth(900)
        layout=QVBoxLayout(self)
        heading=QLabel(f"Review one exact {source_label} correction")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        scope=QGroupBox("Frozen scope")
        form=QFormLayout(scope)
        self.image_value=QLabel(plan.image.name)
        self.image_value.setWordWrap(True)
        self.image_value.setMinimumWidth(0)
        self.image_value.setSizePolicy(
            QSizePolicy.Ignored,QSizePolicy.Preferred,
        )
        self.image_value.setToolTip(str(plan.image))
        self.source_value=QLabel(str(plan.source_path))
        self.source_value.setWordWrap(True)
        self.source_value.setMinimumWidth(0)
        self.source_value.setSizePolicy(
            QSizePolicy.Ignored,QSizePolicy.Preferred,
        )
        self.source_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        self.tag_value=QLabel(plan.value)
        self.tag_value.setWordWrap(True)
        self.tag_value.setMinimumWidth(0)
        self.tag_value.setSizePolicy(
            QSizePolicy.Ignored,QSizePolicy.Preferred,
        )
        self.tag_value.setTextInteractionFlags(Qt.TextSelectableByMouse)
        form.addRow("Image",self.image_value)
        form.addRow("Source",self.source_value)
        if metadata:
            field=QLabel(str(getattr(plan,"source_field","") or ""))
            field.setTextInteractionFlags(Qt.TextSelectableByMouse)
            form.addRow("Managed field",field)
        form.addRow("Exact "+value_label,self.tag_value)
        layout.addWidget(scope)

        meanings=QGroupBox("What this correction means")
        meanings_layout=QVBoxLayout(meanings)
        choices=tuple(getattr(plan,"choices",()) or ())
        if not choices:
            selected=QLabel(
                "<b>Remove only from this managed Metadata field</b> — "
                "Selected source correction<br>Image Knowledge and every "
                "unrelated Metadata field remain unchanged."
            )
            selected.setWordWrap(True)
            selected.setObjectName("sourceCorrectionSelected")
            meanings_layout.addWidget(selected)
        for choice in choices:
            if choice.key==SOURCE_ONLY:
                suffix="Selected source correction"
            elif (
                choice.key==PAIRED_IMAGE_KNOWLEDGE
                and choice.availability=="available"
            ):
                suffix="Available as a protected paired correction"
            elif (
                choice.key==PROJECTION_EXCLUSION
                and choice.availability=="available"
            ):
                suffix="Available in Build training captions"
            else:
                suffix="Not in this action"
            label=QLabel(f"<b>{choice.label}</b> — {suffix}<br>{choice.detail}")
            label.setWordWrap(True)
            label.setObjectName(
                "sourceCorrectionSelected" if choice.key==SOURCE_ONLY
                else "" if choice.availability=="available" else "mutedLabel"
            )
            meanings_layout.addWidget(label)
        layout.addWidget(meanings)

        previews=QGroupBox(f"Exact {source_label} preview")
        previews_layout=QFormLayout(previews)
        self.before=QPlainTextEdit(plan.original_text)
        self.before.setReadOnly(True)
        self.before.setMaximumHeight(92)
        self.before.setAccessibleName(f"{source_label} before correction")
        self.after=QPlainTextEdit(plan.updated_text)
        self.after.setReadOnly(True)
        self.after.setMaximumHeight(92)
        self.after.setAccessibleName(f"{source_label} after correction")
        previews_layout.addRow("Before",self.before)
        previews_layout.addRow("After",self.after)
        layout.addWidget(previews)

        self.status=QLabel(plan.detail)
        self.status.setWordWrap(True)
        self.status.setProperty(
            "severity","warning" if plan.disposition in {
                "blocked","held","unsupported",
            }
            or plan.confirmed_in_image_knowledge else "info",
        )
        layout.addWidget(self.status)
        if not metadata:
            bom=QLabel(
                "The original UTF-8 BOM is retained."
                if getattr(plan,"utf8_bom",False) else
                "No UTF-8 BOM is present."
            )
            bom.setObjectName("mutedLabel")
            layout.addWidget(bom)
        protection=QLabel(
            "Verified backup and restore: "+str(plan.backup_directory)
            if plan.backup_enabled else
            "External backup is Off. Private saved-history recovery protects this exact correction."
            if getattr(plan,"history_operation",None) is not None else
            "Protection is Off — this operation creates no durable restore."
        )
        protection.setWordWrap(True)
        protection.setObjectName("mutedLabel")
        layout.addWidget(protection)
        if plan.privacy_confirmation_required:
            boundary=QLabel(plan.write_decision.privacy_message)
            boundary.setWordWrap(True); boundary.setProperty("severity","warning")
            layout.addWidget(boundary)
            self.privacy_confirmation=QCheckBox(
                "I confirm this exact Local Only boundary crossing"
            )
            layout.addWidget(self.privacy_confirmation)

        projection_choice=next(
            (choice for choice in choices if choice.key==PROJECTION_EXCLUSION),
            None,
        )
        self.projection_button=QPushButton("Keep Fact, Omit from Training…")
        self.projection_button.setProperty("compact",True)
        self.projection_button.setVisible(projection_choice is not None)
        self.projection_button.setEnabled(
            bool(
                projection_choice
                and projection_choice.availability=="available"
                and plan.confirmed_in_image_knowledge
            )
        )
        self.projection_button.setToolTip(
            (
                projection_choice.detail
                if plan.confirmed_in_image_knowledge else
                "This value must be a confirmed Image Knowledge fact before "
                "it can be omitted as a training projection decision."
            )
            if projection_choice is not None else ""
        )
        self.projection_button.clicked.connect(self._projection_exclusion)
        layout.addWidget(self.projection_button,0,Qt.AlignLeft)
        paired_choice=next(
            (choice for choice in choices if choice.key==PAIRED_IMAGE_KNOWLEDGE),
            None,
        )
        self.paired_button=QPushButton(
            "Correct Fact + Managed Metadata…" if metadata
            else "Correct Fact + Training TXT…"
        )
        self.paired_button.setProperty("compact",True)
        self.paired_button.setVisible(paired_choice is not None)
        self._base_paired_enabled=bool(
            paired_choice and paired_choice.availability=="available"
        )
        self.paired_button.setEnabled(
            self._base_paired_enabled and self.privacy_confirmation is None
        )
        self.paired_button.setToolTip(
            paired_choice.detail if paired_choice is not None else ""
        )
        self.paired_button.clicked.connect(self._paired_correction)
        layout.addWidget(self.paired_button,0,Qt.AlignLeft)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.apply_button=buttons.addButton(
            ("Remove from Metadata" if metadata else "Remove from Training TXT")
            if plan.will_write else
            "Nothing to Change",
            QDialogButtonBox.AcceptRole,
        )
        self.apply_button.setProperty("primary",True)
        self._base_apply_enabled=plan.will_write
        self.apply_button.setEnabled(
            self._base_apply_enabled and self.privacy_confirmation is None
        )
        if self.privacy_confirmation is not None:
            self.privacy_confirmation.toggled.connect(
                lambda checked:(
                    self.apply_button.setEnabled(
                        self._base_apply_enabled and checked
                    ),
                    self.paired_button.setEnabled(
                        self._base_paired_enabled and checked
                    ),
                )
            )
        buttons.accepted.connect(self._apply)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _apply(self):
        self.action="apply"
        self.accept()

    def _projection_exclusion(self):
        self.action=PROJECTION_EXCLUSION
        self.accept()

    def _paired_correction(self):
        self.action=PAIRED_IMAGE_KNOWLEDGE
        self.accept()


class SourceCorrectionResultDialog(FeedbackEnabledDialog):
    """Report an exact source correction and expose its safe restore."""

    feedback_workflow = "Source-correction result"

    def __init__(self,result: SourceCorrectionResult,parent=None):
        super().__init__(parent)
        self.result=result
        self.action=""
        metadata=(
            getattr(result,"source_kind","training_txt")=="managed_metadata"
        )
        source_label="Managed Metadata" if metadata else "Training TXT"
        self.setWindowTitle(f"{source_label} Correction Result")
        self.resize(700,360)
        self.setMaximumWidth(780)
        layout=QVBoxLayout(self)
        paired=result.meaning==PAIRED_IMAGE_KNOWLEDGE
        subject=(
            f"{source_label} and Image Knowledge" if paired else source_label
        )
        headings={
            "complete":subject+" changed and verified",
            "current":subject+" is already current",
            "stale":subject+" correction was held",
            "blocked":subject+" correction was held",
            "held":subject+" correction was held",
            "unsupported":subject+" correction is unavailable",
            "failed":subject+" correction needs attention",
        }
        self.heading=QLabel(headings.get(
            result.status,f"{source_label} correction did not run",
        ))
        self.heading.setObjectName("sectionTitle")
        layout.addWidget(self.heading)
        detail=QLabel(result.detail)
        detail.setWordWrap(True)
        detail.setProperty(
            "severity","warning"
            if result.status in {"failed","held","stale","blocked","unsupported"}
            else "info",
        )
        layout.addWidget(detail)
        source=QLabel("Exact source: "+str(result.source_path))
        source.setWordWrap(True)
        source.setMinimumWidth(0)
        source.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        source.setTextInteractionFlags(Qt.TextSelectableByMouse)
        source.setObjectName("mutedLabel")
        layout.addWidget(source)
        recovery=QLabel(
            "Verified recovery: "+str(result.recovery_manifest)
            if result.recovery_manifest is not None else
            "No restore location was returned. Keep current data and reopen this exact correction to review any retained recovery."
            if result.status in {"held","failed"} else
            "No durable restore was created for this operation."
        )
        recovery.setWordWrap(True)
        recovery.setMinimumWidth(0)
        recovery.setSizePolicy(QSizePolicy.Ignored,QSizePolicy.Preferred)
        recovery.setObjectName("mutedLabel")
        layout.addWidget(recovery)
        layout.addStretch(1)
        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        self.restore_button=QPushButton(
            f"Restore {source_label} + Image Knowledge" if paired
            else (
                "Restore Original Managed Metadata" if metadata
                else "Restore Original Training TXT"
            )
        )
        self.restore_button.clicked.connect(self._restore)
        buttons.addButton(self.restore_button,QDialogButtonBox.ActionRole)
        self.restore_button.setVisible(
            result.status=="complete" and result.recovery_manifest is not None
        )
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _restore(self):
        self.action="restore"
        self.accept()
