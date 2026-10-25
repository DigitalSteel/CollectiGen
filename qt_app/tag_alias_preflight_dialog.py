from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QAbstractItemView,QDialog,QDialogButtonBox,QHeaderView,QLabel,QPushButton,
    QTreeWidget,QTreeWidgetItem,QVBoxLayout,
)

from core import TagAliasPreflight


class TagAliasPreflightDialog(QDialog):
    """One consequence-first review for every Other Name ingress."""

    def __init__(self,plan: TagAliasPreflight,tags,*,parent=None) -> None:
        super().__init__(parent)
        self.plan=plan; self.tags=tags; self.choice="cancel"
        self.setWindowTitle("Review Other Names")
        self.resize(1180,620); self.setMinimumSize(900,500)
        layout=QVBoxLayout(self)
        heading=QLabel(
            "Review Other Names before saving"
            if plan.action=="attach" else
            "Review Other Names before removing them"
        )
        heading.setObjectName("sectionTitle"); layout.addWidget(heading)
        explanation=QLabel(
            "Each Other Name must identify exactly one existing Tag. Review identity ownership and the current saved-library impact together. Saving changes reusable System Knowledge only; it does not rewrite Image Knowledge, TXT, Metadata, AI evidence, images, or files."
            if plan.action=="attach" else
            "Removing an Other Name is safe only after confirmed image uses have been corrected. TXT, Metadata, AI evidence, and raw observations remain exact and are shown separately; removing the reusable name does not edit those sources."
        )
        explanation.setWordWrap(True); explanation.setObjectName("mutedLabel")
        layout.addWidget(explanation)

        self.table=QTreeWidget()
        self.table.setHeaderLabels((
            "Other Name","Preferred Tag","Identity check","Confirmed images",
            "TXT","Metadata","AI",
        ))
        self.table.setRootIsDecorated(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        header=self.table.header(); header.setStretchLastSection(False)
        header.setSectionResizeMode(0,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2,QHeaderView.Stretch)
        for column in range(3,7):
            header.setSectionResizeMode(column,QHeaderView.ResizeToContents)
        for entry in plan.entries:
            impact=entry.impact
            values=(
                entry.alias,entry.target_name or entry.target_id,
                self._status_text(entry),
                f"{impact.confirmed_images:,}" if impact.available else "Not available",
                f"{impact.txt_images:,}" if impact.available else "Not available",
                f"{impact.metadata_images:,}" if impact.available else "Not available",
                f"{impact.ai_images:,}" if impact.available else "Not available",
            )
            row=QTreeWidgetItem(values)
            row.setData(0,Qt.UserRole,entry.target_id)
            row.setToolTip(
                2,
                self._status_detail(entry)
                +(
                    f"\n\nCurrent cached observations: {impact.observed_images:,} images total."
                    if impact.available else
                    "\n\nThe current saved-library index is unavailable. No file scan was started."
                ),
            )
            self.table.addTopLevelItem(row)
        layout.addWidget(self.table,1)

        blockers=len(plan.blockers)
        summary=QLabel(
            (
                f"{len(plan.entries)-blockers:,} ready or unchanged • "
                f"{blockers:,} need another decision before saving"
            )
        )
        summary.setWordWrap(True)
        summary.setObjectName("errorLabel" if blockers else "mutedLabel")
        summary.setAccessibleName(summary.text()); layout.addWidget(summary)

        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.continue_button=buttons.addButton(
            "Save Other Names" if plan.action=="attach" else "Remove Other Names",
            QDialogButtonBox.AcceptRole,
        )
        self.continue_button.setProperty("primary",True)
        self.continue_button.setEnabled(plan.can_apply)
        self.continue_button.clicked.connect(self._continue)
        merge_ids=self.merge_tag_ids()
        if blockers and merge_ids:
            merge=buttons.addButton("Merge Duplicate Tags…",QDialogButtonBox.ActionRole)
            merge.clicked.connect(lambda:self._choose("merge"))
        elif blockers and self.current_owner_ids():
            inspect=buttons.addButton("Inspect Current Owner",QDialogButtonBox.ActionRole)
            inspect.clicked.connect(lambda:self._choose("inspect"))
        if any(entry.removed_ids for entry in plan.blockers):
            restore=buttons.addButton("Review Removed Tag…",QDialogButtonBox.ActionRole)
            restore.clicked.connect(lambda:self._choose("restore"))
        if any(
            entry.status in {"confirmed_use","impact_unavailable"}
            for entry in plan.blockers
        ):
            matching=buttons.addButton("Review Matching Images…",QDialogButtonBox.ActionRole)
            matching.clicked.connect(lambda:self._choose("matching"))
        buttons.rejected.connect(self.reject); layout.addWidget(buttons)

    def _continue(self) -> None:
        self.choice="continue"; self.accept()

    def _choose(self,choice: str) -> None:
        self.choice=choice; self.reject()

    def merge_tag_ids(self) -> tuple[str,...]:
        if not self.plan.blockers or any(
            entry.status not in {"ambiguous","owner_collision","batch_collision"}
            for entry in self.plan.blockers
        ):
            return ()
        values=tuple(dict.fromkeys(
            value
            for entry in self.plan.blockers
            for value in (entry.target_id,*entry.owner_ids)
            if value in self.tags.tags
        ))
        return values if len(values)>=2 else ()

    def removed_tag_ids(self) -> tuple[str,...]:
        return tuple(dict.fromkeys(
            value for entry in self.plan.blockers for value in entry.removed_ids
        ))

    def current_owner_ids(self) -> tuple[str,...]:
        return tuple(dict.fromkeys(
            value
            for entry in self.plan.blockers
            for value in entry.owner_ids
            if value in self.tags.tags
        ))

    def matching_scope(self) -> dict:
        return {
            "kind":"alias_lifecycle",
            "origin":"other_name_preflight",
            "stable_ids":tuple(dict.fromkeys(
                entry.target_id for entry in self.plan.entries if entry.target_id
            )),
            "source_names":tuple(dict.fromkeys(
                entry.alias for entry in self.plan.entries if entry.alias
            )),
            "label":"Correct matching image Tags before removing Other Names",
        }

    def _owner_names(self,entry) -> str:
        return ", ".join(
            self.tags.tags[value].display if value in self.tags.tags else value
            for value in entry.owner_ids
        )

    def _status_text(self,entry) -> str:
        owner_names=self._owner_names(entry)
        labels={
            "ready":"Ready to attach",
            "unchanged":"Already identifies this Tag",
            "retired":"Ready — restores a retired Other Name",
            "ready_remove":"Ready to remove",
            "confirmed_use":"Review matching images first",
            "impact_unavailable":"Current image impact must be checked",
            "owner_collision":f"Already identifies {owner_names}",
            "ambiguous":f"Ambiguous ownership: {owner_names or 'multiple records'}",
            "batch_collision":"Selected for more than one preferred Tag",
            "removed_identity":"Matches a removed Tag",
            "target_missing":"Preferred Tag is no longer available",
            "stale":"Other Name ownership changed; reload",
            "invalid":"Invalid empty Other Name",
        }
        return labels.get(entry.status,entry.status.replace("_"," ").title())

    def _status_detail(self,entry) -> str:
        if entry.status in {"owner_collision","batch_collision","ambiguous"}:
            return (
                "One spelling cannot identify multiple live Tags. Compare the identities and use the existing duplicate-merge workflow only if they mean the same thing."
            )
        if entry.status=="removed_identity":
            return (
                "This spelling belongs to removed-vocabulary history. Review and restore that stable identity instead of silently recreating or reassigning it."
            )
        if entry.status in {"confirmed_use","impact_unavailable"}:
            return (
                "Keep this Other Name until the reviewed matching-image job confirms that no saved image fact depends on it."
            )
        if entry.status=="retired":
            return "Saving will deliberately reactivate the retired Other Name for this preferred Tag."
        return self._status_text(entry)
