from __future__ import annotations

from dataclasses import dataclass
from typing import Callable, Mapping

from PySide6.QtCore import QItemSelectionModel,QSize,Signal,Qt
from PySide6.QtWidgets import (
    QAbstractItemView,QComboBox,QDialog,QDialogButtonBox,QHeaderView,QHBoxLayout,QLabel,QLineEdit,
    QMessageBox,QPlainTextEdit,QPushButton,QStackedWidget,QTreeView,QTreeWidget,QTreeWidgetItem,QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core.confirmed_tag_identity import (
    ConfirmedTagIdentityEntry,
    ConfirmedTagIdentityError,
    ConfirmedTagIdentityPlan,
    ConfirmedTagIdentityService,
    removed_tag_guidance, retired_refinement_hold,
)
from core.default_sync import shipped_tag_record,shipped_tag_records
from core.paths import DEFAULTS_DIR
from core.tag_collection import normalize_tag
from .components import IncrementalResultModel,IncrementalResultRow,bounded_window_size
from .modal_feedback import FeedbackEnabledDialog


@dataclass(frozen=True)
class RemovedIdentityReviewOutcome:
    plan: ConfirmedTagIdentityPlan
    restored_ids: tuple[str, ...] = ()
    cancelled: bool = False
    blocked: bool = False


def shipped_removed_identity_record(stable_id: str):
    """Resolve complete reviewed recovery data without inspecting image content."""
    return shipped_tag_record(DEFAULTS_DIR,stable_id)


def _shipped_record_for_knowledge(knowledge,stable_id: str):
    if knowledge is not None and getattr(knowledge,"path",None) is not None:
        parent=knowledge.path.parent
        if parent.name.casefold()=="user":
            record=shipped_tag_record(parent.parent/"defaults",stable_id)
            if record is not None:return record
    return shipped_removed_identity_record(stable_id)


def _shipped_records_for_knowledge(knowledge,stable_ids):
    stable_ids=tuple(dict.fromkeys(str(value) for value in stable_ids if str(value)))
    if knowledge is not None and getattr(knowledge,"path",None) is not None:
        parent=knowledge.path.parent
        if parent.name.casefold()=="user":
            return shipped_tag_records(parent.parent/"defaults",stable_ids)
    return shipped_tag_records(DEFAULTS_DIR,stable_ids)


def removed_identity_record(knowledge,stable_id: str):
    """Resolve the exact record the shared restoration service will use."""
    archive=(
        knowledge.removed_archive_for_tag(stable_id)
        if knowledge is not None
        and hasattr(knowledge,"removed_archive_for_tag") else None
    )
    archived=archive.tag_record(stable_id) if archive is not None else None
    if archive is not None and archive.recovery_kinds.get(stable_id)=="current_default":
        return _shipped_record_for_knowledge(knowledge,stable_id)
    return archived or _shipped_record_for_knowledge(knowledge,stable_id)


def _recovery_details(entries,recovery_records: Mapping[str,object]) -> str:
    rows=[]
    for entry in entries:
        record=recovery_records[entry.stable_id]
        rows.extend((
            f"SDXL Tag: {record.display}",
            f"Stable identity: {record.id}",
            f"Category: {record.category.replace('_',' ').title()}",
            f"Other names: {', '.join(record.aliases) if record.aliases else 'None'}",
            "",
        ))
    return "\n".join(rows).strip()


def confirm_removed_identity_restoration(
    parent,entries,recovery_records: Mapping[str,object],*,continuation: str,
) -> bool:
    entries=tuple(entries); count=len(entries)
    dialog=QMessageBox(parent)
    dialog.setWindowTitle(
        "Review Removed Tag" if count==1 else "Review Removed Tags"
    )
    dialog.setIcon(QMessageBox.Warning)
    if count==1:
        entry=entries[0]; record=recovery_records[entry.stable_id]
        dialog.setText(
            f'Restore “{record.display}” as stable identity “{entry.stable_id}”?'
        )
    else:
        dialog.setText(
            f"Restore {count:,} removed SDXL Tags using their original stable identities?"
        )
    dialog.setInformativeText(
        "CollectiGen will immediately restore the complete reviewed System "
        "Knowledge record, including its aliases, category, relationships, "
        "notes, and provenance, with a recovery backup. "+str(continuation).strip()
    )
    dialog.setDetailedText(_recovery_details(entries,recovery_records))
    restore=dialog.addButton(
        "Restore Tag and Continue" if count==1 else "Restore Tags and Continue",
        QMessageBox.AcceptRole,
    )
    cancel=dialog.addButton(QMessageBox.Cancel)
    dialog.setDefaultButton(cancel); dialog.exec()
    return dialog.clickedButton() is restore


def review_removed_identities(
    parent,
    service: ConfirmedTagIdentityService,
    plan: ConfirmedTagIdentityPlan,
    *,
    source: str,
    continuation: str,
    stored_ids: Mapping[str,str] | None = None,
    confirmation: bool = True,
    recovery_resolver: Callable[[str],object] | None = None,
    confirmation_callback: Callable[[tuple,Mapping[str,object]],bool] | None = None,
) -> RemovedIdentityReviewOutcome:
    """Offer one shared reviewed restoration and then re-evaluate the plan."""
    removed=tuple({
        entry.stable_id:entry for entry in plan.blockers
        if entry.status in {"tombstone","missing_identity"}
        and entry.stable_id
    }.values())
    if not removed:
        return RemovedIdentityReviewOutcome(plan)
    expanded={entry.stable_id:entry for entry in removed}
    knowledge=getattr(service,"knowledge",None)
    if knowledge is not None and hasattr(knowledge,"removed_archive_for_tag"):
        for entry in tuple(expanded.values()):
            archive=knowledge.removed_archive_for_tag(entry.stable_id)
            if archive is None:continue
            for stable_id in archive.tag_ids:
                record=archive.tag_record(stable_id)
                if record is None or stable_id in expanded:continue
                expanded[stable_id]=ConfirmedTagIdentityEntry(
                    record.display,normalize_tag(record.display),"tombstone",
                    stable_id=stable_id,
                    message=(
                        f'“{record.display}” was removed in the same reviewed '
                        "System Knowledge transaction."
                    ),
                )
    removed=tuple(expanded.values())
    resolver=recovery_resolver or (
        lambda stable_id:removed_identity_record(knowledge,stable_id)
    )
    recovery_records={
        entry.stable_id:resolver(entry.stable_id) for entry in removed
    }
    unavailable=[
        entry for entry in removed if recovery_records.get(entry.stable_id) is None
    ]
    if unavailable:
        details="\n".join(
            f'“{entry.raw_value}” — stable identity “{entry.stable_id}”'
            for entry in unavailable
        )
        QMessageBox.warning(
            parent,
            "Removed Tag Cannot Be Restored",
            "Complete reviewed recovery details are not available for the "
            "removed System Knowledge identity below, so CollectiGen cannot "
            "safely recreate it.\n\n"
            +details+
            "\n\nRestore a current Knowledge Pack or System Knowledge backup that "
            "contains the complete record, then try again. Nothing was changed.",
        )
        return RemovedIdentityReviewOutcome(plan,blocked=True)
    confirmed=(
        confirmation_callback(removed,recovery_records)
        if confirmation_callback is not None else
        confirm_removed_identity_restoration(
            parent,removed,recovery_records,continuation=continuation,
        )
    )
    if not confirmed:
        return RemovedIdentityReviewOutcome(plan,cancelled=True)
    try:
        resolution=service.restore_tombstones(
            removed,source=source,recovery_records=recovery_records,
        )
    except (ConfirmedTagIdentityError,OSError,RuntimeError,ValueError) as error:
        QMessageBox.critical(
            parent,
            "Removed Tag Was Not Restored",
            "The prior System Knowledge remains active. Nothing else changed.\n\n"
            +str(error),
        )
        return RemovedIdentityReviewOutcome(plan,blocked=True)
    refreshed=service.plan(
        (entry.raw_value for entry in plan.entries),
        stored_ids=stored_ids,
        confirmation=confirmation,
    )
    return RemovedIdentityReviewOutcome(
        refreshed,restored_ids=resolution.stable_ids,
    )


class RetiredImageTagDialog(FeedbackEnabledDialog):
    """Review an image fact separately from global vocabulary restoration."""

    def __init__(self,knowledge,categories,entry,*,recovery_record=None,pending=False,parent=None,refinement_choices=None,occurrences=()):
        super().__init__(parent)
        self.knowledge=knowledge; self.categories=categories; self.entry=entry
        self.recovery_record=recovery_record
        self.action=""; self.replacement_ids=()
        self._active_refinement=refinement_choices is not None
        if self._active_refinement:
            self._init_active_refinement(refinement_choices,occurrences); return
        self.setWindowTitle("Review Retired Image Tag")
        self.resize(820,560)
        root=QVBoxLayout(self)
        title=QLabel(f'“{entry.raw_value}” '+("is incoming evidence awaiting review." if pending else "is still on this image.")); title.setTextFormat(Qt.PlainText)
        title.setWordWrap(True); root.addWidget(title)
        self._historical_hold=retired_refinement_hold(knowledge,entry.stable_id)
        reason,alternatives=removed_tag_guidance(knowledge,entry,recovery_record)
        self.reason=QPlainTextEdit(reason); self.reason.setReadOnly(True)
        self.reason.setAccessibleName("Recorded retirement reason")
        self.reason.setMaximumHeight(105)
        root.addWidget(self.reason)
        notice=QLabel(
            "Choose replacement Tags that match the image, or ignore the old suggestion on this image. "
            "Replacement also ignores the old suggestion; raw source evidence is preserved. "
            "Image changes stay pending until Save and can be undone."
            if pending else
            "Choose replacement Tags that match the image, or remove the old Tag if it is incorrect. Image changes stay pending until Save and can be undone."
        )
        if self._historical_hold:
            notice.setText(notice.text()+" Historical modifier composition is unavailable; no historical mapping is inferred. Review exact live Tags yourself.")
        notice.setWordWrap(True); root.addWidget(notice)
        self.choices=QTreeWidget(); self.choices.setHeaderLabels(("Suggested replacement","Availability"))
        self.choices.setRootIsDecorated(False); self.choices.setSelectionMode(QAbstractItemView.SingleSelection)
        self.choices.header().setSectionResizeMode(0,QHeaderView.Stretch)
        self.choices.header().setSectionResizeMode(1,QHeaderView.ResizeToContents)
        self.choices.setAccessibleName("Replacement Tag choices")
        root.addWidget(self.choices,1)
        for tag_id,label in alternatives:self._add_choice(tag_id,label)
        self.empty=QLabel("No reviewed replacements are recorded. Use Choose Replacement to search System Knowledge.")
        self.empty.setWordWrap(True); self.empty.setVisible(not alternatives); root.addWidget(self.empty)
        self.choose_button=QPushButton("Choose Replacement…")
        self.choose_button.setToolTip("Search existing Tags or explicitly create a new reusable Tag. Select a suggestion first to search for its exact name.")
        self.choose_button.clicked.connect(self._choose_replacement); root.addWidget(self.choose_button)
        restore_notice=QLabel("Restore Globally brings the retired Tag back into System Knowledge for all images. Use it only if you want to reverse its retirement.")
        restore_notice.setWordWrap(True); root.addWidget(restore_notice)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        keep=buttons.addButton("Keep Original",QDialogButtonBox.ActionRole)
        skip=buttons.addButton("Skip Review",QDialogButtonBox.ActionRole)
        keep.clicked.connect(lambda:self._finish("keep"));skip.clicked.connect(lambda:self._finish("skip"))
        self.restore_button=buttons.addButton("Restore Globally…",QDialogButtonBox.ActionRole)
        self.restore_button.setEnabled(recovery_record is not None)
        self.restore_button.setToolTip("Review the separate global restoration, which saves System Knowledge immediately with a backup.")
        self.remove_button=buttons.addButton("Ignore on This Image" if pending else "Remove from Image",QDialogButtonBox.ActionRole)
        self.replace_button=buttons.addButton("Replace Selected",QDialogButtonBox.AcceptRole)
        self.replace_button.setProperty("primary",True); self.replace_button.setEnabled(False)
        self.restore_button.clicked.connect(lambda:self._finish("restore"))
        self.remove_button.clicked.connect(lambda:self._finish("remove"))
        self.replace_button.clicked.connect(lambda:self._finish("replace"))
        buttons.rejected.connect(self.reject); root.addWidget(buttons)
        self.choices.itemChanged.connect(self._update_selection)
        self.choices.itemDoubleClicked.connect(lambda *_:self._choose_replacement())

    def _init_active_refinement(self, choices, occurrences):
        self.setWindowTitle("Review Image Tag Details")
        self.resize(820,560)
        root=QVBoxLayout(self)
        notice=QLabel(f'Keep "{self.entry.raw_value}" and add one detail you can verify on this image. '
                      'Choose its exact occurrence below. No choice is inferred. Keep and Skip leave it unchanged. '
                      'Changes stay pending until Save and can be undone.')
        notice.setWordWrap(True); root.addWidget(notice)
        self.occurrence=QComboBox(); self.occurrence.setAccessibleName("Source fact occurrence")
        for occurrence in occurrences:self.occurrence.addItem(occurrence.label,occurrence)
        # Never silently select between different owners of the same broad fact.
        self.occurrence.setCurrentIndex(0 if len(occurrences)==1 else -1)
        self.occurrence.currentIndexChanged.connect(self._update_selection)
        root.addWidget(self.occurrence)
        self.choices=QTreeWidget(); self.choices.setHeaderLabels(("Reviewed detail","Availability"))
        self.choices.setRootIsDecorated(False)
        self.choices.header().setSectionResizeMode(0,QHeaderView.Stretch)
        root.addWidget(self.choices,1)
        for choice in choices:
            record=self.knowledge.tags.get(choice.requested_id)
            label=choice.target_display or (record.display if record else choice.requested_id)
            item=QTreeWidgetItem((label,"Available" if choice.target_id else choice.reason.replace('_',' ')))
            item.setData(0,Qt.UserRole,choice.requested_id if choice.target_id else None)
            if choice.target_id:item.setCheckState(0,Qt.Unchecked)
            self.choices.addTopLevelItem(item)
        if not choices:
            empty=QLabel("No reviewed detail choices are available. Keep the original or skip this review.")
            empty.setWordWrap(True);root.addWidget(empty)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        keep=buttons.addButton("Keep Original",QDialogButtonBox.ActionRole)
        skip=buttons.addButton("Skip Review",QDialogButtonBox.ActionRole)
        self.replace_button=buttons.addButton("Add Specific",QDialogButtonBox.AcceptRole)
        self.replace_button.setEnabled(False);self.replace_button.setProperty("primary",True)
        keep.clicked.connect(lambda:self._finish("keep"));skip.clicked.connect(lambda:self._finish("skip"))
        self.replace_button.clicked.connect(lambda:self._finish("add_specific"))
        buttons.rejected.connect(self.reject);root.addWidget(buttons)
        self.choices.itemChanged.connect(self._update_selection)

    def _add_choice(self,tag_id,label,*,checked=False):
        for index in range(self.choices.topLevelItemCount()):
            item=self.choices.topLevelItem(index)
            if item.text(0)==label:
                if tag_id:item.setData(0,Qt.UserRole,tag_id); item.setText(1,"Available")
                if checked:item.setCheckState(0,Qt.Checked)
                return
        item=QTreeWidgetItem((label,"Available" if tag_id else "Choose or create this Tag…"))
        item.setData(0,Qt.UserRole,tag_id)
        if tag_id:item.setCheckState(0,Qt.Checked if checked else Qt.Unchecked)
        item.setToolTip(0,label); self.choices.addTopLevelItem(item)

    def _update_selection(self,*_):
        self.replacement_ids=tuple(dict.fromkeys(
            str(item.data(0,Qt.UserRole)) for item in (
                self.choices.topLevelItem(index) for index in range(self.choices.topLevelItemCount())
            ) if item.data(0,Qt.UserRole) and item.checkState(0)==Qt.Checked
        ))
        self.replace_button.setEnabled(
            len(self.replacement_ids)==1 and self.occurrence.currentIndex()>=0
            if self._active_refinement else bool(self.replacement_ids)
        )

    def _choose_replacement(self):
        from .tag_relationship_editor import TagReferencePickerDialog
        selected=self.choices.currentItem()
        picker=TagReferencePickerDialog(
            self.knowledge,self.categories,title="Choose Replacement Tag",
            exclude_ids=(self.entry.stable_id,),allow_identity_creation=True,
            identity_source="retired_image_tag_replacement",
            identity_category_id=str(getattr(self.recovery_record,"category","") or ""),
            identity_consequence="Only your subsequent replacement choice changes this image, and it remains pending until Save.",
            context_notice="Choose the exact visible replacement. A new reusable Tag requires a separate creation confirmation.",
            parent=self,
        )
        if selected:picker.search.setText(selected.text(0))
        if picker.exec()!=QDialog.Accepted:return
        tag=self.knowledge.tags.get(picker.selected_tag_id())
        if tag is None or tag.id==self.entry.stable_id:return
        self._add_choice(tag.id,tag.display,checked=True)
        self.empty.hide(); self._update_selection()

    def _finish(self,action):
        self._update_selection()
        if action=="replace" and not self.replacement_ids:return
        if action=="add_specific" and (len(self.replacement_ids)!=1 or self.occurrence.currentIndex()<0):return
        self.action=action; self.accept()


class RemovedVocabularyDialog(FeedbackEnabledDialog):
    """Search and restore active Tag tombstones without recreating identities."""

    restored=Signal(object)
    recovery_source_requested=Signal()
    PAGE_SIZE=200

    def __init__(
        self,knowledge,parent=None,*,initial_ids=(),allow_recovery_handoff=False,
    ):
        super().__init__(parent); self.knowledge=knowledge
        self.service=ConfirmedTagIdentityService(knowledge)
        self._records={}
        self._initial_ids=tuple(dict.fromkeys(str(value) for value in initial_ids if str(value)))
        self._allow_recovery_handoff=bool(allow_recovery_handoff)
        self._showing_history=False
        self.setWindowTitle("Removed Vocabulary")
        root=QVBoxLayout(self)
        intro=QLabel(
            "Restore complete removed Tag records without recreating their stable "
            "identities. Only records with verified recovery details appear in the "
            "ordinary recovery list."
        )
        intro.setWordWrap(True); root.addWidget(intro)
        self.mode_summary=QLabel(); self.mode_summary.setWordWrap(True)
        self.mode_summary.setAccessibleName("Removed vocabulary summary")
        root.addWidget(self.mode_summary)
        self.empty_state=QLabel(); self.empty_state.setWordWrap(True)
        self.empty_state.setObjectName("mutedLabel")
        self.empty_state.setAccessibleName("Removed vocabulary empty state")
        self.search=QLineEdit(); self.search.setPlaceholderText(
            "Search names, aliases, stable IDs, Categories, or removal reasons…"
        )
        self.search.setAccessibleName("Search removed vocabulary")
        self.search.textChanged.connect(self._refresh); root.addWidget(self.search)
        self.model=IncrementalResultModel(
            ("Tag","Stable ID","Category","Removed","Recovery"),
            page_size=self.PAGE_SIZE,parent=self,
        )
        self.table=QTreeView(); self.table.setModel(self.model)
        self.table.setRootIsDecorated(False); self.table.setUniformRowHeights(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setAccessibleName("Removed Tags")
        self.table.setAccessibleDescription(
            "Select one or more removed stable identities to inspect or restore. "
            "A Tag removed in a reviewed batch restores with that complete batch."
        )
        header=self.table.header(); header.setSectionResizeMode(0,QHeaderView.Stretch)
        for column,width in ((1,185),(2,140),(3,150),(4,165)):
            header.setSectionResizeMode(column,QHeaderView.Fixed)
            self.table.setColumnWidth(column,width)
        self.table.selectionModel().selectionChanged.connect(self._selection_changed)
        self.empty_panel=QWidget(); empty_layout=QVBoxLayout(self.empty_panel)
        empty_layout.addStretch(1); empty_layout.addWidget(self.empty_state)
        empty_layout.addStretch(1)
        self.content_stack=QStackedWidget(); self.content_stack.addWidget(self.table)
        self.content_stack.addWidget(self.empty_panel); root.addWidget(self.content_stack,1)
        count_row=QHBoxLayout(); self.count=QLabel(); self.count.setObjectName("mutedLabel")
        self.load_more=QPushButton("Load More…"); self.load_more.setProperty("compact",True)
        self.load_more.clicked.connect(self.model.fetch_more)
        count_row.addWidget(self.count,1); count_row.addWidget(self.load_more)
        root.addLayout(count_row)
        history_row=QHBoxLayout()
        self.history_button=QPushButton(); self.history_button.setCheckable(True)
        self.history_button.setProperty("compact",True)
        self.history_button.setAccessibleName("Show legacy removal history")
        self.history_button.toggled.connect(self._toggle_history)
        self.recovery_source_button=QPushButton("Import Recovery Knowledge Pack…")
        self.recovery_source_button.setProperty("compact",True)
        self.recovery_source_button.setAccessibleDescription(
            "Open Knowledge Pack import to supply complete recovery records."
        )
        self.recovery_source_button.clicked.connect(self._request_recovery_source)
        history_row.addWidget(self.history_button)
        history_row.addWidget(self.recovery_source_button)
        history_row.addStretch(1); root.addLayout(history_row)
        self.details=QLabel("Select a removed SDXL Tag to inspect its recovery record.")
        self.details.setWordWrap(True); self.details.setObjectName("mutedLabel")
        self.details.setAccessibleName("Removed vocabulary details")
        root.addWidget(self.details)
        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        self.restore_button=buttons.addButton("Restore Selected…",QDialogButtonBox.AcceptRole)
        self.restore_button.setEnabled(False); self.restore_button.clicked.connect(self._restore)
        buttons.rejected.connect(self.reject); root.addWidget(buttons)
        self.model.counts_changed.connect(self._counts_changed)
        self._refresh()
        available=self.screen().availableGeometry()
        self.resize(bounded_window_size(available,QSize(1040,680)))

    def _active_records(self):
        rows={}
        archived_ids=set()
        shipped_ids={
            stable_id
            for archive in self.knowledge.active_removed_archives()
            for stable_id in archive.tag_ids
            if archive.recovery_kinds.get(stable_id)=="current_default"
        }
        shipped_ids.update(
            self.knowledge.removed_tag_ids-set(self.knowledge.merged_tag_ids)
        )
        shipped_records=_shipped_records_for_knowledge(
            self.knowledge,shipped_ids,
        )
        for archive in self.knowledge.active_removed_archives():
            for stable_id in archive.tag_ids:
                if stable_id not in self.knowledge.removed_tag_ids:continue
                archived_record=archive.tag_record(stable_id)
                if archived_record is None:continue
                recovery_record=(
                    shipped_records.get(stable_id)
                    if archive.recovery_kinds.get(stable_id)=="current_default"
                    else archived_record
                )
                record=recovery_record or archived_record
                archived_ids.add(stable_id)
                rows[stable_id]={
                    "record":record,"archive":archive,
                    "archived_record":archived_record,
                    "recovery_record":recovery_record,
                    "recovery":(
                        archive.recovery_kinds.get(stable_id,"archived_user")
                        if recovery_record is not None else "unavailable"
                    ),
                }
        for stable_id in sorted(
            self.knowledge.removed_tag_ids
            -archived_ids
            -set(self.knowledge.merged_tag_ids)
        ):
            record=shipped_records.get(stable_id)
            rows[stable_id]={
                "record":record,"archive":None,
                "archived_record":None,"recovery_record":record,
                "recovery":"current_default" if record is not None else "unavailable",
            }
        return rows

    @staticmethod
    def _is_restorable(info) -> bool:
        return info["recovery_record"] is not None

    @staticmethod
    def _recovery_label(kind: str) -> str:
        return {
            "current_default":"Current shipped default",
            "archived_user":"Archived full record",
            "unavailable":"Backup or pack required",
        }.get(kind,"Archived full record")

    def _refresh(self):
        selected=self._selected_ids() if self._records else self._initial_ids
        self._initial_ids=()
        self._records=self._active_records(); query=normalize_tag(self.search.text())
        restorable_count=sum(
            self._is_restorable(info) for info in self._records.values()
        )
        history_count=len(self._records)-restorable_count
        selected_exists=any(value in self._records for value in selected)
        selected_has_restorable=any(
            self._is_restorable(self._records[value])
            for value in selected if value in self._records
        )
        if selected_exists and not selected_has_restorable:
            self._showing_history=True
            self.history_button.blockSignals(True)
            self.history_button.setChecked(True)
            self.history_button.blockSignals(False)
        self.history_button.setVisible(history_count>0)
        self.history_button.setText(
            ("Hide Legacy History" if self._showing_history else "Show Legacy History")
            +f" ({history_count:,})"
        )
        self.recovery_source_button.setVisible(
            history_count>0 and self._allow_recovery_handoff
            and (self._showing_history or restorable_count==0)
        )
        if self._showing_history:
            self.mode_summary.setText(
                "Legacy removal history protects deleted stable identities from "
                "returning or being recreated as duplicates. These markers are not "
                "complete Tag records and cannot be restored by themselves."
            )
            self.search.setPlaceholderText(
                "Search legacy stable IDs or removal history…"
            )
            self.table.setAccessibleDescription(
                "Inspect legacy stable-identity removal markers. These history "
                "records cannot be selected for direct restoration."
            )
        else:
            self.mode_summary.setText((
                f"{restorable_count:,} complete removed record"
                f"{'s are' if restorable_count!=1 else ' is'} ready for reviewed "
                "restoration. "
                if restorable_count else ""
            )+(
                "Removing a Tag is different from ignoring future evidence: an "
                "ignored Tag remains live and reusable."
            ))
            self.search.setPlaceholderText(
                "Search restorable names, aliases, stable IDs, Categories, or reasons…"
            )
            self.table.setAccessibleDescription(
                "Select one or more complete removed Tags to inspect or restore. "
                "A Tag removed in a reviewed batch restores with that complete batch."
            )
        self.empty_state.setText(
            "Nothing can be restored here right now. "
            +(
                f"{history_count:,} legacy removal marker"
                f"{'s remain' if history_count!=1 else ' remains'} safely active. "
                "Open Legacy History to inspect them or import a matching Knowledge "
                "Pack with complete recovery records."
                if history_count else
                "There are no active removed Tag records or legacy removal markers."
            )
        )
        rows=[]
        for stable_id,info in self._records.items():
            if self._is_restorable(info)==self._showing_history:continue
            record=info["record"]; archive=info["archive"]
            archived_record=info["archived_record"]
            display=record.display if record is not None else "Recovery details unavailable"
            category=record.category if record is not None else "Unknown"
            removed_at=archive.removed_at if archive is not None else "Legacy tombstone"
            reason=archive.reason if archive is not None else "Removed before full lifecycle archives"
            search_text=normalize_tag(" ".join((
                stable_id,display,category,reason,
                *(record.aliases if record is not None else ()),
                *(archived_record.aliases if archived_record is not None else ()),
                archived_record.display if archived_record is not None else "",
            )))
            if query and query not in search_text:continue
            recovery=self._recovery_label(info["recovery"])
            rows.append(IncrementalResultRow(
                stable_id,(display,stable_id,category,removed_at,recovery),
                tooltip=reason,
                accessible_text=(
                    f"{display}; stable identity {stable_id}; Category {category}; "
                    f"removed {removed_at}; {recovery}; {reason}"
                ),
            ))
        rows.sort(key=lambda item:(item.columns[0].casefold(),item.stable_id))
        self.model.set_rows(rows,ensure_ids=selected)
        has_rows=bool(rows)
        self.search.setVisible(has_rows)
        self.content_stack.setCurrentWidget(
            self.table if has_rows else self.empty_panel
        )
        self.count.setVisible(has_rows)
        self.details.setVisible(has_rows)
        self.restore_button.setVisible(
            not self._showing_history and restorable_count>0
        )
        selection=self.table.selectionModel(); selection.clearSelection()
        for stable_id in selected:
            index=self.model.index_for_id(stable_id)
            if index.isValid():selection.select(
                index,QItemSelectionModel.Select|QItemSelectionModel.Rows,
            )
        if selected:
            index=self.model.index_for_id(selected[0])
            if index.isValid():selection.setCurrentIndex(index,QItemSelectionModel.NoUpdate)
        self._selection_changed()

    def _counts_changed(self,loaded,total):
        label="legacy removal markers" if self._showing_history else "restorable Tags"
        self.count.setText(f"Showing {loaded:,} of {total:,} {label}")
        self.load_more.setVisible(loaded<total)

    def _selected_ids(self):
        if not hasattr(self,"table"):return ()
        return tuple(dict.fromkeys(
            self.model.stable_id(index)
            for index in self.table.selectionModel().selectedRows()
            if self.model.stable_id(index)
        ))

    def _selection_changed(self,*_args):
        selected=self._selected_ids()
        if not selected:
            self.details.setText(
                "Select a legacy removal marker to inspect why recovery is unavailable."
                if self._showing_history else
                "Select a removed Tag to inspect its recovery record."
            )
            self.restore_button.setEnabled(False); return
        info=self._records[selected[0]]; record=info["record"]; archive=info["archive"]
        recoverable=all(
            self._records[value]["recovery_record"] is not None for value in selected
        )
        self.restore_button.setEnabled(recoverable)
        if info["recovery_record"] is None:
            self.details.setText(
                f'Stable identity “{selected[0]}” is still protected by a legacy '
                "removal marker. It is not a complete Tag record, so Restore remains "
                "unavailable. Import a matching Knowledge Pack or restore an approved "
                "System Knowledge backup containing the complete record. The marker "
                "will remain active until verified recovery succeeds."
            ); return
        if record is None:
            self.details.setText(
                f'Stable identity “{selected[0]}” has only an ID tombstone. Restore '
                "a current Knowledge Pack or System Knowledge backup containing its "
                "complete reviewed record before continuing."
            ); return
        batch_count=len(archive.tag_ids) if archive is not None else 1
        reason=archive.reason if archive is not None else "Legacy removal"
        source=archive.source if archive is not None else "Earlier System Knowledge"
        removed_at=archive.removed_at if archive is not None else "Time unavailable"
        aliases=", ".join(record.aliases) if record.aliases else "None"
        related=(
            len(archive.related_tags_before)+len(archive.categories_before)+
            len(archive.relationships_before)
            if archive is not None else 0
        )
        suffix=(
            f" This Tag belongs to a {batch_count:,}-Tag removal transaction; "
            "the complete batch restores together."
            if batch_count>1 else ""
        )
        self.details.setText(
            f"{record.display} · {record.id}\nCategory: {record.category} · Other "
            f"names: {aliases}\nRemoved: {removed_at} · Source: {source} · "
            f"Reason: {reason}\nRecovery: {self._recovery_label(info['recovery'])}; "
            f"{related:,} related record(s) will return to their exact reviewed "
            f"pre-removal state.{suffix}"
        )

    def _toggle_history(self,checked):
        self._showing_history=bool(checked)
        self.search.clear()
        self.table.clearSelection()
        self._refresh()
        if self.search.isVisible():self.search.setFocus()
        else:self.history_button.setFocus()

    def _request_recovery_source(self):
        self.recovery_source_requested.emit()
        self.accept()

    def _restore(self):
        entries=[]
        for stable_id in self._selected_ids():
            info=self._records[stable_id]; record=info["record"]
            if record is None or info["recovery_record"] is None:continue
            entries.append(ConfirmedTagIdentityEntry(
                record.display,normalize_tag(record.display),"tombstone",
                stable_id=stable_id,
            ))
        if not entries:return
        plan=ConfirmedTagIdentityPlan(tuple(entries))
        outcome=review_removed_identities(
            self,self.service,plan,source="removed_vocabulary_review",
            continuation=(
                "After restoration, the records return to Tag Encyclopedia. "
                "No Image Knowledge, captions, Metadata, or image files change."
            ),
            recovery_resolver=lambda stable_id:removed_identity_record(
                self.knowledge,stable_id
            ),
        )
        if not outcome.restored_ids:return
        self.knowledge.load(); self._refresh()
        self.restored.emit(outcome.restored_ids)
