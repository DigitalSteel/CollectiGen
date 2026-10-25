from __future__ import annotations

from html import escape

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,QDialogButtonBox,QFrame,QGroupBox,QHeaderView,QLabel,QPlainTextEdit,QTreeWidget,
    QTreeWidgetItem,QVBoxLayout,QPushButton,
)

from core import (
    LIBRARY_NAMING_IMAGE_HASH,SAFETY_READY,SHARE_SAFETY_POLICY_LABELS,
    LibraryMovePlan,LibraryRole,
)

from .components import ContextHelpButton,DisclosureButton
from .modal_feedback import FeedbackEnabledDialog


def transfer_size_text(size_bytes: int) -> str:
    size=float(max(0,int(size_bytes)))
    for unit in ("B","KiB","MiB","GiB","TiB"):
        if size<1024 or unit=="TiB":
            return f"{size:,.0f} {unit}" if unit=="B" else f"{size:,.1f} {unit}"
        size/=1024
    return f"{int(size_bytes):,} B"


class LibraryMovePreviewDialog(FeedbackEnabledDialog):
    """One exact named-library move decision shared by Ingest and Dataset."""

    feedback_workflow = "Move to Library preview"

    def __init__(
        self,plan: LibraryMovePlan,parent=None,*,
        source_context: str="Ingest",include_projection_options: bool=True,
    ) -> None:
        super().__init__(parent)
        self.plan=plan
        self.action=""
        self.source_context=source_context
        self.setWindowTitle("Preview Move to Library")
        self.resize(920,620)
        layout=QVBoxLayout(self)
        ready=len(plan.ready); pending=len(plan.pending); fallback=len(plan.fallback)
        skipped=len(plan.skipped)
        duplicates=len(plan.duplicates)
        safety_holds=len(plan.safety_holds)
        safety_blocked=len(plan.safety_blocked)
        safety_warnings=len(plan.safety_warnings)
        local_review=len(plan.local_review)
        naming_policy=plan.naming_policy
        naming_label=(
            "Rename using image hash (full SHA-256)"
            if naming_policy==LIBRARY_NAMING_IMAGE_HASH else
            "Keep current filename"
        )
        destination=plan.destination_profile
        if destination is not None:
            destination_panel=QFrame()
            destination_panel.setObjectName("statusActionCard")
            destination_layout=QVBoxLayout(destination_panel)
            destination_layout.setContentsMargins(10,7,10,7)
            destination_layout.setSpacing(2)
            image_word="image" if len(plan.items)==1 else "images"
            self.destination_heading=QLabel(
                f"Move {len(plan.items):,} {image_word} to {destination.name}"
            )
            self.destination_heading.setObjectName("metricValue")
            self.destination_heading.setAccessibleName("Named library destination")
            destination_layout.addWidget(self.destination_heading)
            self.destination_details=QLabel(
                f"{destination.display_role}  •  {destination.root}"
            )
            self.destination_details.setWordWrap(True)
            self.destination_details.setObjectName("mutedLabel")
            self.destination_details.setAccessibleName(
                "Destination storage role and exact root"
            )
            self.destination_details.setToolTip(
                f"Destination role: {destination.display_role}\n"
                f"Exact destination root: {destination.root}"
            )
            destination_layout.addWidget(self.destination_details)
            layout.addWidget(destination_panel)
        else:
            self.destination_heading=None
            self.destination_details=None
        help_sections=[]
        source_counts={}
        for item in plan.items:
            profile=item.source_profile
            key=(
                profile.name,profile.display_role
            ) if profile is not None else (
                ("Incoming images","Separate storage")
                if source_context=="Ingest" else
                ("Unrecognized source","Needs attention")
            )
            source_counts[key]=source_counts.get(key,0)+1
        source_summary=(
            "Sources: "+" • ".join(
                f"{name} — {role}: {count:,}"
                for (name,role),count in source_counts.items()
            )
        )
        help_sections.append(source_summary)
        summary_parts=[f"{len(plan.items):,} selected"]
        if ready:
            summary_parts.append(f"{ready:,} ready")
        if pending:
            summary_parts.append(f"{pending:,} need action")
        if duplicates:
            summary_parts.append(f"{duplicates:,} {'duplicate' if duplicates==1 else 'duplicates'}")
        if safety_blocked:
            summary_parts.append(f"{safety_blocked:,} safety blocked")
        if local_review:
            summary_parts.append(f"{local_review:,} Local Only review {'move' if local_review==1 else 'moves'}")
        if fallback:
            summary_parts.append(f"{fallback:,} using optional fallback")
        self.summary=QLabel("  •  ".join(summary_parts))
        self.summary.setWordWrap(True)
        self.summary.setObjectName("metricValue")
        self.summary.setAccessibleName("Move readiness summary")
        layout.addWidget(self.summary)
        self.filename_summary=QLabel(
            "Filenames: rename using image hash"
            if naming_policy==LIBRARY_NAMING_IMAGE_HASH else
            "Filenames: keep current names"
        )
        self.filename_summary.setObjectName("mutedLabel")
        self.filename_summary.setAccessibleName("Filename behavior")
        layout.addWidget(self.filename_summary)
        self.automatic_note=None
        if skipped:
            self.automatic_note=QLabel(
                f"Handled automatically: {skipped:,} "
                f"{'image has' if skipped==1 else 'images have'} missing organization details "
                f"and {'uses' if skipped==1 else 'use'} the nearest known folder in this library."
            )
            self.automatic_note.setWordWrap(True)
            self.automatic_note.setObjectName("successLabel")
            self.automatic_note.setAccessibleName("Automatic move handling")
            layout.addWidget(self.automatic_note)
        transfer_summary=(
            f"Verified copy space: {transfer_size_text(plan.required_bytes)}"
            if plan.required_bytes else
            "Transfer: same-volume moves prefer rename"
            if ready else
            "Transfer: No file changes are eligible"
        )
        move_details=(
            f"File naming: {naming_label}  •  {transfer_summary}"
        )
        help_sections.append(move_details)
        note_parts=[
            "Nothing moves until you choose a scope.",
            f"Images that need review stay in {source_context}.",
        ]
        if duplicates:
            note_parts.append("Exact duplicates stay at their source.")
        if fallback:
            note_parts.append(
                "Fallback destinations are shown below and do not confirm Image Knowledge."
            )
        help_sections.append(" ".join(note_parts))
        help_sections.append(
            "Ready images may move without the images that still need review. "
            "Exact duplicates are never overwritten or moved as a second authority. "
            "A fallback route remains provisional rather than becoming confirmed Image Knowledge."
        )
        safety_group=QGroupBox("Destination safety")
        safety_layout=QVBoxLayout(safety_group)
        safety_layout.setContentsMargins(10,7,10,7)
        safety_layout.setSpacing(2)
        if destination is not None:
            policy_label=SHARE_SAFETY_POLICY_LABELS.get(
                destination.share_safety_policy,
                destination.share_safety_policy.replace('_',' ').title(),
            )
            help_sections.append(
                f"{destination.name} policy: {policy_label}"
            )
        if safety_blocked:
            safety_text=(
                f"{safety_blocked:,} {'image is' if safety_blocked==1 else 'images are'} blocked by confirmed safety information."
            )
        elif safety_holds:
            safety_text=(
                f"{safety_holds:,} {'image needs' if safety_holds==1 else 'images need'} review before moving to this destination."
            )
        elif local_review:
            safety_text=(
                f"{local_review:,} held {'image can' if local_review==1 else 'images can'} move into Local Only storage for review."
            )
        else:
            safety_text="All eligible images pass this destination's safety check."
        if safety_warnings:
            safety_text=(
                f"Destination policy warnings: {safety_warnings:,} selected. "
                "Eligible images may move as shown, or cancel to change the selection."
                + (f" {safety_text}" if safety_holds or safety_blocked else "")
            )
        self.safety_note=QLabel(safety_text)
        self.safety_note.setWordWrap(True)
        self.safety_note.setObjectName(
            "warningLabel" if safety_holds or safety_blocked or safety_warnings else "mutedLabel"
        )
        self.safety_note.setAccessibleName("Destination safety result")
        self.safety_note.setToolTip(
            "The safety check uses the exact image SHA-256, confirmed Image Knowledge, "
            "reviewed System Knowledge, and the destination policy. Provisional AI or "
            "imported findings remain review holds rather than confirmed facts."
        )
        safety_layout.addWidget(self.safety_note)
        self.safety_warning=None
        if safety_warnings:
            reasons=sorted({reason for item in plan.safety_warnings for reason in item.safety.reasons})
            self.safety_warning=QLabel("\n".join(reasons))
            self.safety_warning.setWordWrap(True)
            self.safety_warning.setObjectName("warningLabel")
            self.safety_warning.setAccessibleName("Destination policy warnings")
            safety_layout.addWidget(self.safety_warning)
        self.safety_reason=None
        if safety_holds:
            reason_counts={}
            for item in plan.safety_holds:
                basis=(
                    item.safety.rating_basis
                    if item.safety is not None else "Safety information needs review"
                )
                detail=(
                    item.safety.reasons[0]
                    if item.safety is not None and item.safety.reasons else basis
                )
                reason=f"{basis}: {detail}" if detail!=basis else basis
                reason_counts[reason]=reason_counts.get(reason,0)+1
            reason_text="; ".join(
                f"{count:,} — {basis}"
                for basis,count in sorted(reason_counts.items(),key=lambda pair:pair[0].casefold())
            )
            self.safety_reason=QLabel(
                "Action required: "+reason_text+". Show the held images to use "
                "the matching review or correction action."
            )
            self.safety_reason.setWordWrap(True)
            self.safety_reason.setObjectName("warningLabel")
            self.safety_reason.setAccessibleName("Why destination safety held images")
            safety_layout.addWidget(self.safety_reason)
        if destination is not None:
            help_sections.append(
                "Library moves do not require Mark Reviewed. Change the rating warning limit in "
                "Setup > Library & Storage > Locations > Edit Library. Confirmed prohibited "
                "content cannot be allowed by a preference."
            )
        self.safety_group=safety_group
        layout.addWidget(safety_group)
        protection=plan.protection
        self.protection_warning=None
        if protection is not None:
            if protection.backup_enabled:
                protection_text=(
                    f"Verified originals: {protection.source_count:,} image "
                    f"{'family' if protection.source_count==1 else 'families'} • "
                    f"{protection.file_count:,} file(s) • Conservative space: "
                    f"{transfer_size_text(protection.required_free_bytes)} • Available: "
                    +(
                        transfer_size_text(protection.available_free_bytes)
                        if protection.available_free_bytes is not None else
                        "Could not verify"
                    )
                    +"\nProtected destination: "+str(protection.backup_directory)
                )
            else:
                protection_text=(
                    "Protection is Off for this operation. No new durable original "
                    "copy will be created; verified transfer and journal-backed Undo "
                    "remain available."
                )
            help_sections.append(protection_text)
            help_sections.append(
                "When protection is enabled, every original in the chosen move scope "
                "is copied and read-back verified before the first projection or move. "
                "Journal-backed Undo remains available and refuses newer destination changes."
            )
            if not protection.ready:
                self.protection_warning=QLabel(
                    "Protected organization is unavailable: "
                    +( " ".join(protection.errors) if protection.errors else
                       "the backup destination has insufficient verified free space." )
                )
                self.protection_warning.setWordWrap(True)
                self.protection_warning.setProperty("severity","warning")
                self.protection_warning.setAccessibleName(
                    "Protected organization warning"
                )
        if self.protection_warning is not None:
            layout.addWidget(self.protection_warning)
        self.tree=QTreeWidget()
        self.tree.setHeaderLabels((
            "Source image","Image data","Destination","Transfer","Route details","Safety",
        ))
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        header=self.tree.header()
        header.setSectionResizeMode(0,QHeaderView.Interactive)
        self.tree.setColumnWidth(0,190)
        header.setSectionResizeMode(1,QHeaderView.Interactive)
        self.tree.setColumnWidth(1,145)
        header.setSectionResizeMode(2,QHeaderView.Stretch)
        header.setSectionResizeMode(3,QHeaderView.Interactive)
        self.tree.setColumnWidth(3,145)
        header.setSectionResizeMode(4,QHeaderView.Interactive)
        self.tree.setColumnWidth(4,135)
        header.setSectionResizeMode(5,QHeaderView.Interactive)
        self.tree.setColumnWidth(5,125)
        self.tree.setColumnHidden(3,True)
        self.tree.setColumnHidden(4,True)
        self.tree.setMinimumHeight(160)
        for index,item in enumerate(plan.items):
            transfer=item.transfer
            transfer_text=(
                "Exact duplicate — stays in source"
                if item.exact_duplicate else
                "Already at destination"
                if transfer is None and item.destination.resolve()==item.image.parent.resolve() else
                "Verified same-volume rename"
                if transfer is not None and transfer.mode=="rename" else
                f"Verified copy/remove • {transfer_size_text(transfer.required_bytes)}"
                if transfer is not None else "Availability not checked"
            )
            exact_destination=(
                transfer.members[0].target
                if transfer is not None and transfer.members else
                item.destination/item.image.name
            )
            source_family=(
                tuple(member.source for member in transfer.members)
                if transfer is not None else (item.image,)
            )
            destination_family=(
                tuple(member.target for member in transfer.members)
                if transfer is not None else (exact_destination,)
            )
            route_text=(
                "Fallback: "+", ".join(item.missing_levels)
                if item.missing_levels else
                "Skipped: "+", ".join(item.skipped_levels)
                if item.skipped_levels else
                "Complete"
            )
            safety_label=(
                "Ready for this Local Only library"
                if item.safety is not None and destination is not None
                and destination.recognized_role is LibraryRole.LOCAL_ONLY
                and item.safety.status==SAFETY_READY else
                item.safety.label if item.safety is not None else "Not checked"
            )
            row=QTreeWidgetItem((
                (
                    f"{item.source_profile.name} — {item.image.name}"
                    if item.source_profile is not None else str(item.image)
                ),item.state.label,str(exact_destination),transfer_text,route_text,
                safety_label,
            ))
            details=[item.state.detail,*item.route_reasons]
            if item.safety is not None:
                details.extend(item.safety.reasons)
            if item.exact_duplicate and transfer is not None:
                details.append(
                    f"Exact duplicate already exists at {transfer.duplicate_target}. The source remains unchanged."
                )
            if item.errors:
                details.extend(item.errors)
            source_text="Source family:\n"+"\n".join(str(path) for path in source_family)
            destination_text=(
                "Destination family:\n"
                +"\n".join(str(path) for path in destination_family)
            )
            row.setToolTip(0,source_text+"\n\n"+transfer_text+"\n"+"\n".join(details))
            row.setToolTip(2,destination_text)
            row.setToolTip(4,"\n".join(item.route_reasons) or route_text)
            row.setToolTip(5,"\n".join(item.safety.reasons) if item.safety is not None else "Safety was not checked")
            row.setData(0,Qt.AccessibleTextRole,source_text)
            row.setData(2,Qt.AccessibleTextRole,destination_text)
            row.setData(4,Qt.AccessibleTextRole,route_text+". "+" ".join(item.route_reasons))
            row.setData(5,Qt.AccessibleTextRole,
                (safety_label+". "+" ".join(item.safety.reasons))
                if item.safety is not None else "Safety was not checked"
            )
            row.setData(0,Qt.UserRole,index)
            self.tree.addTopLevelItem(row)
        layout.addWidget(self.tree,1)
        self.exact_paths=QPlainTextEdit()
        self.exact_paths.setReadOnly(True)
        self.exact_paths.setMaximumHeight(160)
        self.exact_paths.setAccessibleName("Exact source and destination paths for selected image family")
        self.show_exact_paths=DisclosureButton("Show Exact Paths","Hide Exact Paths",self.exact_paths)
        self.show_exact_paths.setToolTip("Select an image row to inspect every source and planned destination file.")
        layout.addWidget(self.show_exact_paths)
        layout.addWidget(self.exact_paths)
        self.tree.itemSelectionChanged.connect(self._update_exact_paths)
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        else:
            self._update_exact_paths()

        self.sync_txt=None
        self.sync_metadata=None
        if include_projection_options:
            projections=QGroupBox("Optional confirmed-data synchronization before moving")
            projection_layout=QVBoxLayout(projections)
            previews=[
                item.reconciliation for item in plan.items
                if item.reconciliation is not None
            ]
            txt_changed=sum(bool(
                preview.difference("txt") and preview.difference("txt").will_write
            ) for preview in previews)
            metadata_differences=[preview.difference("metadata") for preview in previews]
            metadata_changed=sum(bool(item and item.will_write) for item in metadata_differences)
            metadata_available=any(bool(
                item and item.available and not item.error
            ) for item in metadata_differences)
            self.sync_txt=QCheckBox(
                f"Sync confirmed data to TXT ({txt_changed:,} change, {len(previews)-txt_changed:,} current)"
            )
            self.sync_txt.setChecked(True)
            self.sync_metadata=QCheckBox(
                f"Sync confirmed data to Metadata ({metadata_changed:,} change, unavailable targets will be skipped)"
            )
            self.sync_metadata.setChecked(metadata_available)
            self.sync_metadata.setEnabled(metadata_available)
            projection_layout.addWidget(self.sync_txt)
            projection_layout.addWidget(self.sync_metadata)
            projection_note=QLabel(
                "Synchronization uses this exact protected family plan plus "
                "operation-local rollback and read-back verification. It never "
                "confirms pending findings."
            )
            projection_note.setWordWrap(True)
            projection_note.setObjectName("mutedLabel")
            projection_layout.addWidget(projection_note)
            layout.addWidget(projections)

        self.boundary_confirmation=None
        self.boundary_group=None
        boundary=plan.boundary_decision
        protection_boundary=(
            protection is not None and protection.privacy_confirmation_required
        )
        if (
            boundary is not None and boundary.requires_explicit_confirmation
            or protection_boundary
        ):
            boundary_group=QGroupBox("Local Only boundary confirmation")
            self.boundary_group=boundary_group
            boundary_layout=QVBoxLayout(boundary_group)
            messages=[]
            if boundary is not None and boundary.requires_explicit_confirmation:
                messages.append(boundary.message)
            if protection_boundary and protection.privacy_message not in messages:
                messages.append(protection.privacy_message)
            boundary_note=QLabel("\n\n".join(messages))
            boundary_note.setWordWrap(True)
            boundary_note.setObjectName("warningLabel")
            boundary_note.setAccessibleName("Exact Local Only boundary consequence")
            boundary_layout.addWidget(boundary_note)
            self.boundary_confirmation=QCheckBox(
                "I confirm these exact image families may cross the Local Only boundary "
                "to the destination shown above."
            )
            self.boundary_confirmation.setAccessibleName(
                "Confirm exact Local Only boundary transfer"
            )
            boundary_layout.addWidget(self.boundary_confirmation)
            layout.addWidget(boundary_group)
            # When every image is held, no bytes can move from this preview. Ask
            # for the exact privacy confirmation only after review produces an
            # eligible move, rather than presenting an irrelevant second gate.
            eligible_all=bool(plan.items) and all(
                not item.errors and not item.exact_duplicate
                and (item.safety is None or item.safety.ready)
                for item in plan.items
            )
            boundary_group.setVisible(bool(ready) or eligible_all)

        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.buttons=buttons
        help_sections.append(
            "Same-volume transfers prefer a verified rename; other transfers "
            "verify the copied files before removing their sources. Image hash "
            "naming uses the full SHA-256. Hover over a source image for its "
            "transfer and route details, or a destination for every family path."
        )
        self.help_button=ContextHelpButton(
            "Move to Library",
            "\n\n".join("<p>"+escape(text).replace("\n","<br>")+"</p>"
                          for text in help_sections),
            self,
        )
        buttons.addButton(self.help_button,QDialogButtonBox.HelpRole)
        buttons.rejected.connect(self.reject)
        self.review_button=buttons.addButton(
            "Show Held Images",QDialogButtonBox.ActionRole,
        )
        self.review_button.clicked.connect(lambda:self._choose("review"))
        self.review_button.setEnabled(bool(pending))
        self.ready_button=buttons.addButton(
            "Move with Warnings" if any(item.safety and item.safety.warning for item in plan.ready)
            else "Move Ready Only",QDialogButtonBox.AcceptRole,
        )
        self.ready_button.clicked.connect(lambda:self._choose("ready"))
        self.ready_button.setDefault(bool(ready))
        self.ready_button.setProperty("primary",bool(ready))
        self.all_button=buttons.addButton(
            "Move All as Shown",QDialogButtonBox.AcceptRole,
        )
        self.all_button.clicked.connect(lambda:self._choose("all"))
        self.all_button.setVisible(bool(fallback))
        if pending and not ready:
            self.review_button.setDefault(True)
            self.review_button.setProperty("primary",True)
        self.review_button.setToolTip(
            "Return to the selected images and show the exact action needed for each hold."
        )
        layout.addWidget(buttons)
        if self.boundary_confirmation is not None:
            self.boundary_confirmation.toggled.connect(self._update_move_buttons)
        self._update_move_buttons()

    def _update_exact_paths(self) -> None:
        row=self.tree.currentItem()
        self.show_exact_paths.setEnabled(row is not None)
        if row is None:
            self.exact_paths.setPlainText("Select an image row to inspect its exact paths.")
            return
        item=self.plan.items[row.data(0,Qt.UserRole)]
        transfer=item.transfer
        if item.exact_duplicate:
            existing=getattr(transfer,"duplicate_target",None)
            text=(f"Exact duplicate; no file changes are planned.\nSource remains: {item.image}\n"
                  f"Existing destination: {existing or item.destination / item.image.name}")
        elif transfer is None:
            text=(f"Source: {item.image}\nProposed destination: {item.destination / item.image.name}\n"
                  "A complete family transfer is not available for this image.")
        else:
            text="\n\n".join(
                f"Source: {member.source}\nDestination: {member.target}"
                for member in transfer.members
            )
        self.exact_paths.setPlainText(text)

    def _update_move_buttons(self) -> None:
        boundary_ready=(
            self.boundary_confirmation is None
            or self.boundary_confirmation.isChecked()
        )
        protection=self.plan.protection
        ready_protection=(
            protection is None
            or protection.subset(item.image for item in self.plan.ready).ready
        )
        all_items=[
            item for item in self.plan.items
            if not item.errors and not item.exact_duplicate
        ]
        all_protection=(
            protection is None
            or protection.subset(item.image for item in all_items).ready
        )
        self.ready_button.setEnabled(
            boundary_ready and ready_protection and bool(self.plan.ready)
        )
        self.all_button.setEnabled(
            boundary_ready and all_protection and bool(self.plan.items)
            and all(
                not item.errors and not item.exact_duplicate
                and (item.safety is None or item.safety.ready)
                for item in self.plan.items
            )
        )
        if not boundary_ready:
            ready_tip=all_tip=(
                "Confirm the Local Only boundary above before moving these images."
            )
        else:
            ready_tip=(
                "Move images with complete routes, including any destination policy warnings shown above."
                if self.plan.ready else
                "No selected images currently satisfy the required move checks."
            )
            all_tip=(
                "Move every selected image exactly as listed."
                if self.all_button.isEnabled() else
                "Every selected image must have a complete route and pass safety checks."
            )
        self.ready_button.setToolTip(ready_tip)
        self.all_button.setToolTip(all_tip)

    def _choose(self,action: str) -> None:
        self.action=action
        self.accept()

    def targets(self) -> tuple[str, ...]:
        values=[]
        if self.sync_txt is not None and self.sync_txt.isChecked():
            values.append("txt")
        if self.sync_metadata is not None and self.sync_metadata.isChecked():
            values.append("metadata")
        return tuple(values)

    def boundary_confirmed(self) -> bool:
        return (
            self.boundary_confirmation is None
            or self.boundary_confirmation.isChecked()
        )
