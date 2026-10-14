from __future__ import annotations

import json
import re
from pathlib import Path

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,QDialogButtonBox,QFileDialog,QFrame,QHeaderView,QHBoxLayout,
    QLabel,QLineEdit,QPushButton,QTabWidget,QTextEdit,QTreeWidget,
    QTreeWidgetItem,QVBoxLayout,QWidget,
)

from core.training_package import (
    TrainingPackageComparison,TrainingPackagePlan,TrainingPackageResult,
)

from .modal_feedback import FeedbackEnabledDialog


def _format_bytes(value: int) -> str:
    amount=float(max(0,int(value)))
    for unit in ("bytes","KB","MB","GB","TB"):
        if amount<1024 or unit=="TB":
            return (
                f"{int(amount):,} {unit}" if unit=="bytes" else
                f"{amount:,.1f} {unit}"
            )
        amount/=1024
    return f"{amount:,.1f} TB"


def suggested_training_package_name(output_name: str) -> str:
    stem=re.sub(r"[^A-Za-z0-9._ -]+","-",str(output_name or "").strip())
    stem=re.sub(r"\s+","-",stem).strip(" .-") or "training-output"
    return f"{stem}-training-package"


def _safe_name(value: str) -> bool:
    return bool(
        value and value not in {".",".."}
        and not re.search(r'[\\/:*?"<>|\x00-\x1f]',value)
        and value==Path(value).name
    )


class TrainingPackageDestinationDialog(FeedbackEnabledDialog):
    """Choose one immutable package directory below an existing local folder."""

    feedback_workflow="Training package destination"

    def __init__(
        self,output_name: str,parent=None,*,initial_parent: str|Path|None=None,
    ) -> None:
        super().__init__(parent)
        self.action=""
        self.setWindowTitle("Choose Training Package Destination")
        self.resize(780,430)
        layout=QVBoxLayout(self)
        heading=QLabel("Choose a new folder for the manual training package")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        intro=QLabel(
            "Create a separate folder containing the images, captions, and settings "
            "for manual training. Existing folders will not be overwritten."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel")
        layout.addWidget(intro)

        parent_label=QLabel("Existing parent folder")
        layout.addWidget(parent_label)
        parent_row=QHBoxLayout()
        self.parent_folder=QLineEdit(
            str(Path(initial_parent).expanduser().absolute())
            if initial_parent else ""
        )
        self.parent_folder.setAccessibleName("Training package parent folder")
        self.parent_folder.setPlaceholderText("Choose an existing local folder")
        self.parent_folder.setCursorPosition(0)
        browse=QPushButton("Browse…")
        browse.clicked.connect(self._browse)
        parent_row.addWidget(self.parent_folder,1); parent_row.addWidget(browse)
        layout.addLayout(parent_row)

        name_label=QLabel("New package folder name")
        layout.addWidget(name_label)
        self.package_name=QLineEdit(suggested_training_package_name(output_name))
        self.package_name.setAccessibleName("New training package folder name")
        layout.addWidget(self.package_name)
        self.destination_summary=QLabel()
        self.destination_summary.setWordWrap(True)
        self.destination_summary.setTextInteractionFlags(
            Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard
        )
        layout.addWidget(self.destination_summary)
        consequence=QLabel(
            "Your source files stay unchanged. Review the package contents next; "
            "building the package does not start training."
        )
        consequence.setWordWrap(True); consequence.setObjectName("mutedLabel")
        layout.addWidget(consequence)
        layout.addStretch(1)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.preview_button=buttons.addButton(
            "Preview Package",QDialogButtonBox.AcceptRole,
        )
        self.preview_button.setProperty("primary",True)
        self.preview_button.clicked.connect(self._accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.parent_folder.textChanged.connect(self._refresh)
        self.package_name.textChanged.connect(self._refresh)
        self._refresh()

    @property
    def destination(self) -> Path:
        return (
            Path(self.parent_folder.text().strip()).expanduser().absolute()
            /self.package_name.text().strip()
        )

    def _browse(self) -> None:
        selected=QFileDialog.getExistingDirectory(
            self,"Choose Training Package Parent Folder",
            self.parent_folder.text().strip() or str(Path.cwd()),
        )
        if selected:
            self.parent_folder.setText(selected)

    def _refresh(self,*_) -> None:
        parent_text=self.parent_folder.text().strip()
        name=self.package_name.text().strip()
        parent_path=Path(parent_text).expanduser() if parent_text else None
        valid_parent=bool(parent_path and parent_path.is_dir())
        valid_name=_safe_name(name)
        if valid_parent and valid_name:
            destination=(parent_path/name).absolute()
            state=(
                "Already exists — preview will check whether this package matches"
                if destination.exists() else
                "New directory — no existing folder will be replaced"
            )
            self.destination_summary.setText(f"Destination: {destination}\n{state}")
        elif not valid_parent:
            self.destination_summary.setText(
                "Choose an existing parent folder before previewing."
            )
        else:
            self.destination_summary.setText(
                "Use a simple folder name without path separators or reserved characters."
            )
        self.preview_button.setEnabled(valid_parent and valid_name)

    def _accept(self) -> None:
        if not self.preview_button.isEnabled():
            return
        self.action="preview"
        self.accept()


class TrainingPackagePlanDialog(FeedbackEnabledDialog):
    """Present the exact immutable package preview and its write gate."""

    feedback_workflow="Training package preview"
    MAX_VISIBLE_ROWS=250

    def __init__(
        self,plan: TrainingPackagePlan,parent=None,*,
        recovery_receipt: str|Path|None=None,
    ) -> None:
        super().__init__(parent)
        self.plan=plan
        self.recovery_receipt=(
            Path(recovery_receipt) if recovery_receipt is not None else None
        )
        self.action=""
        self.setWindowTitle("Review Manual Training Package")
        self.resize(1220,780)
        layout=QVBoxLayout(self)
        state={
            "create":"Ready to create the training package",
            "current":"Existing package is already current",
            "collision":"Choose another folder — existing contents differ",
        }.get(plan.destination_disposition,"Destination unavailable")
        if self.recovery_receipt is not None:
            state="Review the interrupted training package"
        heading=QLabel(
            f"{state} • {len(plan.items):,} included image"
            f"{'s' if len(plan.items)!=1 else ''}"
        )
        heading.setObjectName("sectionTitle"); heading.setWordWrap(True)
        layout.addWidget(heading)
        destination=QFrame(); destination.setObjectName("statusActionCard")
        destination_layout=QVBoxLayout(destination)
        destination_layout.setContentsMargins(10,7,10,7)
        destination_title=QLabel(plan.destination.name)
        destination_title.setObjectName("metricValue")
        destination_layout.addWidget(destination_title)
        destination_path=QLabel(str(plan.destination))
        destination_path.setWordWrap(True); destination_path.setObjectName("mutedLabel")
        destination_path.setTextInteractionFlags(
            Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard
        )
        destination_layout.addWidget(destination_path)
        layout.addWidget(destination)

        role_counts={
            role:sum(item.role==role for item in plan.items)
            for role in ("training","validation","holdout")
        }
        summary=QLabel(
            f"Training {role_counts['training']:,} • Validation {role_counts['validation']:,} • "
            f"Holdout {role_counts['holdout']:,} • Excluded "
            f"{plan.candidate_plan.count('exclude'):,} • "
            +("Review the exact staged package before recovery" if self.recovery_receipt is not None else
             f"Space needed to build {_format_bytes(plan.estimate.package_staging_bytes)}")
        )
        summary.setWordWrap(True)
        layout.addWidget(summary)

        if plan.issues:
            blocking=sum(issue.level=="blocking" for issue in plan.issues)
            caution=sum(issue.level!="blocking" for issue in plan.issues)
            issue_summary=QLabel(
                f"{'New-build checks' if self.recovery_receipt is not None else 'Checks'}: "
                f"{blocking:,} blocking • {caution:,} caution. "
                +" ".join(
                    f"{issue.summary} {issue.direct_fix}".strip()
                    for issue in plan.issues[:4]
                )
            )
            issue_summary.setWordWrap(True)
            issue_summary.setProperty("severity","warning" if blocking else "info")
            layout.addWidget(issue_summary)

        if self.recovery_receipt is not None:
            recovery=QLabel(
                "The exact saved receipt owns this destination. This preview is for "
                "reviewing recovery; a new Build is unavailable here. Recover will "
                "recheck the current sources and staged files before publishing."
            )
            recovery.setWordWrap(True); recovery.setProperty("severity","warning")
            recovery.setToolTip(str(self.recovery_receipt))
            layout.addWidget(recovery)

        self.privacy_confirmation=None
        if plan.decision.privacy_confirmation_required:
            boundary=QLabel(plan.decision.privacy_message)
            boundary.setWordWrap(True); boundary.setProperty("severity","warning")
            layout.addWidget(boundary)
            self.privacy_confirmation=QCheckBox(
                "I confirm this exact package destination and cross-zone scope"
            )
            self.privacy_confirmation.toggled.connect(self._refresh_action_gate)
            layout.addWidget(self.privacy_confirmation)

        tabs=QTabWidget(); self.tabs=tabs
        contents=QWidget(); contents_layout=QVBoxLayout(contents)
        self.tree=QTreeWidget()
        self.tree.setAccessibleName("Exact training package contents")
        self.tree.setHeaderLabels((
            "Source / Generated item","Role","Package path","Transform / Exact content",
        ))
        self.tree.setRootIsDecorated(False); self.tree.setAlternatingRowColors(True)
        header=self.tree.header()
        header.setSectionResizeMode(0,QHeaderView.Interactive)
        header.setSectionResizeMode(1,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2,QHeaderView.Interactive)
        header.setSectionResizeMode(3,QHeaderView.Stretch)
        self.tree.setColumnWidth(0,210); self.tree.setColumnWidth(2,310)
        for item in plan.items[:self.MAX_VISIBLE_ROWS]:
            operations=", ".join(item.transform.operations)
            detail=(
                f"{operations} • {item.transform.expected_dimensions[0]}×"
                f"{item.transform.expected_dimensions[1]} {item.transform.expected_mode} • "
                f"caption: {item.caption}"
            )
            row=QTreeWidgetItem((
                item.candidate.image.name,item.role.title(),item.image_path,detail,
            ))
            row.setToolTip(0,str(item.candidate.image))
            row.setToolTip(2,f"{item.image_path}\n{item.caption_path}")
            row.setToolTip(3,detail)
            self.tree.addTopLevelItem(row)
        for relative in plan.generated_files:
            if relative.startswith("images/"):
                continue
            self.tree.addTopLevelItem(QTreeWidgetItem((
                "Generated package control","—",relative,
                "Deterministic UTF-8 configuration, split, handoff, or manifest data",
            )))
        contents_layout.addWidget(self.tree,1)
        if len(plan.items)>self.MAX_VISIBLE_ROWS:
            bound=QLabel(
                f"Showing the first {self.MAX_VISIBLE_ROWS:,} of {len(plan.items):,} "
                "image/caption pairs. Counts and the JSON preview cover the frozen plan."
            )
            bound.setWordWrap(True); bound.setObjectName("mutedLabel")
            contents_layout.addWidget(bound)
        tabs.addTab(contents,"Package Contents")

        preview=QTextEdit(); self.manifest_preview=preview
        preview.setReadOnly(True)
        preview.setAccessibleName("Source-neutral training package preview")
        preview.setPlainText(json.dumps(
            plan.to_preview_dict(item_limit=self.MAX_VISIBLE_ROWS),
            indent=2,ensure_ascii=False,
        ))
        tabs.addTab(preview,"Technical Preview")
        layout.addWidget(tabs,1)
        consequence=QLabel(
            "Recovery keeps source images unchanged and publishes only the verified "
            "staged package if the rebuilt plan and current evidence match exactly. "
            "It does not run training or create a model."
            if self.recovery_receipt is not None else
            "Images keep their source framing unless the listed technical normalization "
            "is required. Target crop and bucket settings remain requests for a compatible "
            "trainer. Build writes only a new verified package plus its sibling recovery "
            "receipt; it does not run training or create a model."
        )
        consequence.setWordWrap(True); consequence.setObjectName("mutedLabel")
        layout.addWidget(consequence)

        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.recover_button=QPushButton("Finish Package Publication")
        self.recover_button.clicked.connect(self._recover)
        buttons.addButton(self.recover_button,QDialogButtonBox.ActionRole)
        self.recover_button.setVisible(self.recovery_receipt is not None)
        self.recover_button.setProperty("primary",self.recovery_receipt is not None)
        action_label=(
            "Verify Current Package"
            if plan.destination_disposition=="current" else
            f"Build {len(plan.items):,} Image Package"
        )
        self.build_button=buttons.addButton(action_label,QDialogButtonBox.AcceptRole)
        self.build_button.setProperty("primary",self.recovery_receipt is None)
        self.build_button.setVisible(self.recovery_receipt is None)
        self.build_button.clicked.connect(self._build)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self._refresh_action_gate()

    @property
    def privacy_confirmed(self) -> bool:
        return bool(
            self.privacy_confirmation is None
            or self.privacy_confirmation.isChecked()
        )

    def _refresh_action_gate(self,*_) -> None:
        self.build_button.setEnabled(
            self.plan.ready and self.recovery_receipt is None
            and self.privacy_confirmed
        )

    def _build(self) -> None:
        if not self.build_button.isEnabled():
            return
        self.action="build"
        self.accept()

    def _recover(self) -> None:
        self.action="recover"
        self.accept()


class TrainingPackageResultDialog(FeedbackEnabledDialog):
    """Report package publication or recovery without implying training ran."""

    feedback_workflow="Training package result"

    STATUS_LABELS={
        "created":"Package created and verified",
        "current":"Existing package verified as current",
        "recovered":"Interrupted package recovered and verified",
        "cancelled":"Package creation cancelled safely",
        "rolled_back":"Incomplete package staging removed safely",
        "recovery_required":"Package recovery needs attention",
        "failed":"Package was not created",
    }

    def __init__(self,result: TrainingPackageResult,parent=None, *, reference_inspection=False,
                 candidate_plan_available=True) -> None:
        super().__init__(parent)
        self.result=result; self.action=""
        if reference_inspection:
            self._build_reference_inspection()
            return
        self.setWindowTitle("Training Package Result")
        self.resize(800,520)
        layout=QVBoxLayout(self)
        heading=QLabel(self.STATUS_LABELS.get(result.status,result.status.title()))
        heading.setObjectName("sectionTitle"); heading.setWordWrap(True)
        layout.addWidget(heading)
        recovery_review=result.status=="recovery_required" and result.recovery_receipt is not None
        destination=QLabel(
            f"Package: {result.destination.name}" if recovery_review else
            f"Destination: {result.destination}"
        )
        destination.setWordWrap(True)
        destination.setTextInteractionFlags(
            Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard
        )
        layout.addWidget(destination)
        verification=result.verification
        successful=(result.status in ("created","current","recovered")
                    and verification is not None and verification.valid)
        needs_plan=(result.status=="recovery_required"
                    and result.recovery_phase=="staged_complete"
                    and not candidate_plan_available)
        cleanup=recovery_review and result.recovery_phase in {"prepared","staging","attention_required"}
        reviewed_phase=recovery_review and result.recovery_phase in {
            "prepared","staging","attention_required","staged_complete","promoted",
        }
        if needs_plan:
            next_action=(
                "Next: Find a related saved draft, then review a fresh package preview "
                "for this exact destination before recovery."
            )
        elif cleanup:
            next_action=("Next: Remove only this operation's incomplete staging files. "
                         "Source images and Image Knowledge stay unchanged; no package will be published.")
        elif recovery_review and reviewed_phase:
            next_action=("Next: Finish publishing the verified package." if result.recovery_phase=="staged_complete"
                         else "Next: Finalize the receipt for the verified published package.")
        elif recovery_review:
            next_action="Next: Review this recovery receipt again before changing files."
        elif result.status=="rolled_back":
            next_action="Next: Return to Saved Drafts, choose your plan and preview a fresh package. Source images and Image Knowledge stay unchanged."
        elif successful:
            next_action="Next: Review Trainer Settings before running training."
        elif result.issues:
            next_action="Next: Review the issue below before trying again."
        else:
            next_action="Next: Close this result and return to Training."
        guidance=QLabel(
            next_action+"\nNo trainer or model was installed, downloaded, loaded, uploaded, or run."
        )
        guidance.setWordWrap(True)
        self.guidance=guidance
        layout.addWidget(guidance)
        if result.issues and not cleanup and result.status!="rolled_back":
            issue_text=(
                "The prepared files are waiting; no package was published. "
                "CollectiGen must recheck the saved plan and current images first."
                if needs_plan else "Attention: "+" ".join(result.issues)
            )
            issues=QLabel(issue_text)
            issues.setWordWrap(True); issues.setProperty("severity","warning")
            layout.addWidget(issues)
        self.trainer_preview_button=QPushButton("Review Trainer Settings…")
        self.trainer_preview_button.setAutoDefault(False)
        self.trainer_preview_button.setEnabled(successful)
        self.trainer_preview_button.setVisible(successful)
        self.trainer_preview_button.clicked.connect(lambda:self._choose("trainer_preview"))
        layout.addWidget(self.trainer_preview_button,0,Qt.AlignLeft)
        self.find_draft_button=QPushButton("Find Related Saved Drafts…")
        self.find_draft_button.setAutoDefault(False)
        self.find_draft_button.setProperty("primary",True)
        self.find_draft_button.setVisible(needs_plan)
        self.find_draft_button.setEnabled(bool(
            needs_plan and verification is not None and verification.valid
            and verification.manifest.get("candidate_plan")
        ))
        self.find_draft_button.clicked.connect(lambda:self._choose("find_draft"))
        layout.addWidget(self.find_draft_button,0,Qt.AlignLeft)
        self._details_subject=("Cleanup Receipt" if result.status=="rolled_back" else
                               "Exact Recovery Details" if recovery_review else
                               "Verification and Trainer Details")
        self.details_button=QPushButton(f"Show {self._details_subject}")
        self.details_button.setCheckable(True)
        self.details_button.setAutoDefault(False)
        layout.addWidget(self.details_button,0,Qt.AlignLeft)
        self.details_panel=QWidget()
        details_layout=QVBoxLayout(self.details_panel)
        details_layout.setContentsMargins(0,0,0,0)
        if recovery_review:
            exact=QLabel(
                f"Exact destination: {result.destination}\n"
                f"Recovery receipt: {result.recovery_receipt}\n"
                f"Saved phase: {result.recovery_phase or 'unavailable'}"
            )
            exact.setWordWrap(True)
            exact.setTextInteractionFlags(Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard)
            details_layout.addWidget(exact)
        if verification is not None:
            verified=QLabel(
                f"Verification: {'valid' if verification.valid else 'invalid'} • "
                f"{verification.artifact_count:,} recorded artifacts\n"
                f"Package fingerprint: {verification.package_fingerprint or 'Unavailable'}\n"
                f"Intent fingerprint: {verification.intent_fingerprint or 'Unavailable'}"
            )
            verified.setWordWrap(True)
            verified.setTextInteractionFlags(
                Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard
            )
            details_layout.addWidget(verified)
        if result.recovery_receipt is not None and result.status!="recovery_required":
            receipt=QLabel(f"Recovery receipt: {result.recovery_receipt}")
            receipt.setWordWrap(True); receipt.setObjectName("mutedLabel")
            receipt.setTextInteractionFlags(
                Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard
            )
            details_layout.addWidget(receipt)
        next_steps=QLabel(
            ("The receipt identifies saved work, not permission to publish it. "
             "For a commit-ready stage, find a related saved draft, choose it explicitly, "
             "and preview the same destination so source and Image Knowledge evidence "
             "can be rechecked. "
             "Incomplete staging may instead be rolled back after a separate review.")
            if recovery_review else
            ("Rebuilding requires a fresh preview from your chosen saved draft. "
             "Cleanup does not resume publication or run training.")
            if result.status=="rolled_back" else
            ("Use the manual adapter configuration with a compatible trainer only after "
             "verifying its tokenizer, base-model compatibility, crop/bucket behavior, "
             "resource needs, checkpoints, and output registration. Rebuild into a new "
             "folder and compare fingerprints when you need a deterministic repeat. "
             "To resume interrupted publication, reopen the same saved candidate plan "
             "and recover its exact sibling receipt; source and Image Knowledge evidence "
             "will be rechecked before a commit-ready stage can be published.")
        )
        next_steps.setWordWrap(True); next_steps.setObjectName("mutedLabel")
        details_layout.addWidget(next_steps)
        layout.addWidget(self.details_panel)
        self.details_panel.setVisible(False)
        self.details_button.toggled.connect(self._toggle_details)
        layout.addStretch(1)
        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        self.recover_button=QPushButton(
            "Remove Incomplete Staging" if cleanup else
            "Finish Package Publication" if result.recovery_phase=="staged_complete" else
            "Finalize Package Receipt" if result.recovery_phase=="promoted" else
            "Review Recovery Again"
        )
        self.recover_button.setAutoDefault(False)
        self.recover_button.clicked.connect(lambda:self._choose("recover" if reviewed_phase else "recovery_preview"))
        buttons.addButton(self.recover_button,QDialogButtonBox.ActionRole)
        self.recover_button.setVisible(
            result.status=="recovery_required" and result.recovery_receipt is not None
        )
        self.recover_button.setEnabled(not needs_plan)
        self.recover_button.setProperty("primary",reviewed_phase and not needs_plan)
        self.saved_drafts_button=QPushButton("Return to Saved Drafts")
        self.saved_drafts_button.setAutoDefault(False)
        self.saved_drafts_button.setProperty("primary",True)
        self.saved_drafts_button.clicked.connect(lambda:self._choose("saved_drafts"))
        buttons.addButton(self.saved_drafts_button,QDialogButtonBox.ActionRole)
        self.saved_drafts_button.setVisible(result.status=="rolled_back")
        if needs_plan:
            self.recover_button.setToolTip(
                "A commit-ready stage cannot publish until its exact saved plan and current sources are rechecked."
            )
        self.verify_button=QPushButton("Verify Again")
        self.verify_button.setEnabled(result.destination.is_dir())
        self.verify_button.clicked.connect(lambda:self._choose("verify"))
        buttons.addButton(self.verify_button,QDialogButtonBox.ActionRole)
        self.verify_button.setVisible(result.destination.is_dir())
        self.compare_button=QPushButton("Compare Package…")
        self.compare_button.setEnabled(successful)
        self.compare_button.clicked.connect(lambda:self._choose("compare"))
        buttons.addButton(self.compare_button,QDialogButtonBox.ActionRole)
        self.compare_button.setVisible(successful)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    def _toggle_details(self,shown: bool) -> None:
        self.details_panel.setVisible(shown)
        self.details_button.setText(
            ("Hide " if shown else "Show ")+self._details_subject
        )

    def _choose(self,action: str) -> None:
        self.action=action
        self.accept()

    def _build_reference_inspection(self):
        """Reuse the package view without restoring plans or enabling writers."""
        verification = self.result.verification
        if verification is None or not verification.valid:
            raise ValueError("Reference inspection requires a verified package.")
        self.setWindowTitle("Referenced Training Package")
        self.resize(1040, 760)
        layout = QVBoxLayout(self)
        heading = QLabel("Recorded training data · verified package snapshot")
        heading.setObjectName("sectionTitle"); layout.addWidget(heading)
        note = QLabel("Review frozen image identities, captions and settings. Original libraries and execution receipts were not rechecked. Close to return to Models.")
        note.setWordWrap(True); layout.addWidget(note)
        identity = QLabel(f"Package: {verification.package_fingerprint}\nFolder: {verification.destination}")
        identity.setWordWrap(True); identity.setTextInteractionFlags(Qt.TextSelectableByMouse)
        layout.addWidget(identity)
        tabs = QTabWidget(); layout.addWidget(tabs, 1)
        data = QWidget(); body = QVBoxLayout(data); tabs.addTab(data, "Frozen Data")
        self.reference_candidates = tuple(verification.manifest["candidate_plan"]["candidates"])
        self.reference_package_items = {item["stable_id"]: item for item in verification.manifest["package_plan"]["items"]}
        self.reference_page = 0
        self.reference_rows = QTreeWidget()
        self.reference_rows.setHeaderLabels(["Image identity", "Decision", "Role"])
        self.reference_rows.setRootIsDecorated(False)
        self.reference_rows.header().setSectionResizeMode(0, QHeaderView.Stretch)
        self.reference_rows.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.reference_rows.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.reference_rows.setAccessibleName("Frozen training candidates")
        body.addWidget(self.reference_rows, 2)
        controls = QHBoxLayout(); body.addLayout(controls)
        self.reference_previous = QPushButton("Previous Images")
        self.reference_next = QPushButton("Next Images")
        self.reference_count = QLabel()
        for widget in (self.reference_previous, self.reference_next, self.reference_count): controls.addWidget(widget)
        controls.addStretch(1)
        self.reference_details = QTextEdit(); self.reference_details.setReadOnly(True)
        self.reference_details.setAccessibleName("Frozen caption and selection evidence")
        body.addWidget(self.reference_details, 2)
        text_controls = QHBoxLayout(); body.addLayout(text_controls)
        self.reference_text_previous = QPushButton("Previous Details")
        self.reference_text_next = QPushButton("Next Details")
        self.reference_text_count = QLabel()
        for widget in (self.reference_text_previous, self.reference_text_next, self.reference_text_count): text_controls.addWidget(widget)
        text_controls.addStretch(1)
        self._reference_text = ""; self._reference_text_page = 0
        configuration = QTextEdit(); configuration.setReadOnly(True)
        configuration.setPlainText(json.dumps(verification.manifest["training_configuration"], ensure_ascii=False, indent=2))
        tabs.addTab(configuration, "Frozen Settings")
        self.reference_previous.clicked.connect(lambda: self._reference_render(-1))
        self.reference_next.clicked.connect(lambda: self._reference_render(1))
        self.reference_rows.currentItemChanged.connect(self._reference_selected)
        self.reference_text_previous.clicked.connect(lambda: self._reference_render_text(-1))
        self.reference_text_next.clicked.connect(lambda: self._reference_render_text(1))
        buttons = QDialogButtonBox(QDialogButtonBox.Close)
        buttons.button(QDialogButtonBox.Close).setText("Back to Models")
        buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self._reference_render()

    def _reference_render(self, delta=0):
        total = len(self.reference_candidates)
        self.reference_page = max(0, min(self.reference_page + delta, max(0, (total - 1) // 50)))
        start = self.reference_page * 50
        self.reference_rows.clear()
        for candidate in self.reference_candidates[start:start + 50]:
            item = QTreeWidgetItem([str(candidate.get("stable_id", "")),
                                   str(candidate.get("decision", "")), str(candidate.get("role", ""))])
            item.setData(0, Qt.UserRole, candidate); item.setToolTip(0, item.text(0))
            self.reference_rows.addTopLevelItem(item)
        self.reference_count.setText(f"{start + 1 if total else 0:,}–{min(start + 50, total):,} of {total:,} frozen images")
        self.reference_previous.setEnabled(start > 0); self.reference_next.setEnabled(start + 50 < total)
        if total: self.reference_rows.setCurrentItem(self.reference_rows.topLevelItem(0))

    def _reference_selected(self, item, _previous=None):
        candidate = item.data(0, Qt.UserRole) if item else {}
        package_item = self.reference_package_items.get(candidate.get("stable_id"))
        self._reference_text = (f"Package caption: {package_item['caption'] if package_item else 'Not included in the package'}\n\n"
            f"Candidate caption: {candidate.get('caption', '')}\n\n"
            f"Selection reason: {candidate.get('decision_reason', '')}\n\n"
            + json.dumps({"candidate": candidate, "package_item": package_item}, ensure_ascii=False, indent=2)) if candidate else ""
        self._reference_text_page = 0; self._reference_render_text()

    def _reference_render_text(self, delta=0):
        size = 16_384; total = len(self._reference_text)
        self._reference_text_page = max(0, min(self._reference_text_page + delta, max(0, (total - 1) // size)))
        start = self._reference_text_page * size
        self.reference_details.setPlainText(self._reference_text[start:start + size])
        self.reference_text_count.setText(f"Details {self._reference_text_page + 1} / {max(1, (total + size - 1) // size)}")
        self.reference_text_previous.setEnabled(start > 0); self.reference_text_next.setEnabled(start + size < total)


class TrainingPackageComparisonDialog(FeedbackEnabledDialog):
    feedback_workflow="Training package comparison"

    def __init__(self,comparison: TrainingPackageComparison,parent=None) -> None:
        super().__init__(parent)
        self.comparison=comparison
        self.setWindowTitle("Compare Training Packages")
        self.resize(820,500)
        layout=QVBoxLayout(self)
        heading=QLabel(
            "Packages are byte-for-byte equivalent"
            if comparison.identical else
            "Packages differ or could not be verified"
        )
        heading.setObjectName("sectionTitle"); heading.setWordWrap(True)
        layout.addWidget(heading)
        paths=QLabel(
            f"First: {comparison.first.destination}\n"
            f"Second: {comparison.second.destination}"
        )
        paths.setWordWrap(True)
        paths.setTextInteractionFlags(Qt.TextSelectableByMouse|Qt.TextSelectableByKeyboard)
        layout.addWidget(paths)
        detail=QTextEdit(); detail.setReadOnly(True)
        detail.setAccessibleName("Training package comparison details")
        detail.setPlainText(
            "The valid package fingerprints match exactly."
            if comparison.identical else
            "\n".join(comparison.differences or ("No classified difference is available.",))
        )
        layout.addWidget(detail,1)
        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
