from __future__ import annotations

from PySide6.QtCore import Qt,Signal
from PySide6.QtWidgets import (
    QAbstractItemView,QDialog,QDialogButtonBox,QFormLayout,QFrame,QHBoxLayout,QLabel,QLineEdit,
    QMessageBox,QPushButton,QScrollArea,QTreeView,QVBoxLayout,QWidget,
)

from core.automation_repository import active_automation_engine
from core.confirmed_tag_identity import (
    ConfirmedTagIdentityError,ConfirmedTagIdentityService,
)
from core.content_sensitivity import effective_tag_sensitivity
from core.tag_collection import normalize_tag
from .components import BoundedSearchableComboBox,IncrementalResultModel,IncrementalResultRow
from .modal_feedback import FeedbackEnabledDialog
from .tag_editor import FlowLayout,TagChip,category_details,decode_tag_drag_ids
from .tag_identity_review import (
    confirm_removed_identity_restoration,review_removed_identities,
    removed_identity_record,
)


class TagReferencePickerDialog(FeedbackEnabledDialog):
    """Bounded stable-ID picker with optional reviewed identity creation."""

    feedback_workflow = "SDXL Tag picker"

    PAGE_SIZE=200

    def __init__(
        self,knowledge,categories,*,title="Choose SDXL Tag",exclude_ids=(),
        category_ids=(),allow_identity_creation=False,
        identity_source="confirmed_image_tag_picker",identity_service=None,
        identity_recovery_resolver=None,identity_create_label="Create Exact Tag…",
        identity_consequence="",identity_category_id="",context_notice="",parent=None,
    ):
        super().__init__(parent); self.knowledge=knowledge; self.categories=categories; self.exclude_ids={str(value) for value in exclude_ids}; self.category_ids={str(value) for value in category_ids if str(value)}; self.allow_identity_creation=bool(allow_identity_creation); self.identity_source=str(identity_source); self._selected_tag_id=""
        self.identity_category_id=str(identity_category_id or "").strip()
        self.context_notice=str(context_notice or "").strip()
        self._created_tag_ids=()
        self.identity_create_label=str(identity_create_label or "Create Exact Tag…")
        self.identity_consequence=(
            str(identity_consequence).strip()
            or "The selected images change only after the next review."
        )
        self.identity_recovery_resolver=(
            identity_recovery_resolver or
            (lambda stable_id:removed_identity_record(knowledge,stable_id))
        )
        self.identity_service=identity_service
        if self.allow_identity_creation and self.identity_service is None:
            automation=active_automation_engine(knowledge)
            self.identity_service=ConfirmedTagIdentityService(
                knowledge,evidence_rejected=automation.is_evidence_rejected,
            )
        self._records=sorted((record for record in knowledge.tags.values() if record.id not in self.exclude_ids and (not self.category_ids or record.category in self.category_ids)),key=lambda item:(item.display.casefold(),item.id))
        self.setWindowTitle(title); self.resize(760,560)
        layout=QVBoxLayout(self)
        creation_notice=(
            "Choose an existing reusable SDXL Tag, or enter one exact new name. "
            "Creating a new identity saves System Knowledge independently. "
            +self.identity_consequence
            if self.allow_identity_creation else ""
        )
        self.identity_notice=QLabel(" ".join(
            value for value in (self.context_notice,creation_notice) if value
        ))
        self.identity_notice.setObjectName("mutedLabel"); self.identity_notice.setWordWrap(True); self.identity_notice.setVisible(bool(self.identity_notice.text())); layout.addWidget(self.identity_notice)
        self.search=QLineEdit(); self.search.setPlaceholderText("Search preferred names, aliases, IDs, and category paths…" if not self.allow_identity_creation else "Search or enter one exact SDXL Tag…"); self.search.setAccessibleName("Search existing SDXL Tags" if not self.allow_identity_creation else "Search or enter one exact SDXL Tag"); layout.addWidget(self.search)
        result_row=QHBoxLayout(); self.result_count=QLabel(); self.result_count.setObjectName("mutedLabel"); self.load_more=QPushButton("Load More…"); self.load_more.setProperty("compact",True); result_row.addWidget(self.result_count,1); result_row.addWidget(self.load_more); layout.addLayout(result_row)
        self.model=IncrementalResultModel(("SDXL Tag","Category"),page_size=self.PAGE_SIZE,parent=self)
        self.results=QTreeView(); self.results.setModel(self.model); self.results.setRootIsDecorated(False); self.results.setUniformRowHeights(True); self.results.setSelectionBehavior(QAbstractItemView.SelectRows); self.results.setSelectionMode(QAbstractItemView.SingleSelection); self.results.setAccessibleName("Existing SDXL Tag choices"); self.results.header().setStretchLastSection(False); self.results.header().resizeSection(1,260); layout.addWidget(self.results,1)
        self.exact_status=QLabel(); self.exact_status.setObjectName("mutedLabel"); self.exact_status.setWordWrap(True); self.exact_status.hide(); layout.addWidget(self.exact_status)
        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.create_exact_button=buttons.addButton(self.identity_create_label,QDialogButtonBox.ActionRole); self.create_exact_button.clicked.connect(self._resolve_exact_search); self.create_exact_button.setAccessibleName("Create exact reusable SDXL Tag and continue"); self.create_exact_button.hide()
        self.choose_button=buttons.addButton("Choose Tag",QDialogButtonBox.AcceptRole); self.choose_button.setProperty("primary",True); self.choose_button.setEnabled(False); self.choose_button.clicked.connect(self._accept_existing); buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.search.textChanged.connect(self._refresh); self.search.returnPressed.connect(self._accept_search); self.load_more.clicked.connect(self.model.fetch_more); self.model.counts_changed.connect(self._counts_changed); self.results.selectionModel().currentChanged.connect(self._selection_changed); self.results.doubleClicked.connect(lambda *_:self._accept_existing() if self.selected_tag_id() else None)
        self._refresh()

    def _refresh(self,*_):
        self._selected_tag_id=""
        raw_query=self.search.text().strip(); query=raw_query.casefold(); query_key=normalize_tag(raw_query); rows=[]; exact_ids=[]
        for record in self._records:
            category=self.categories.path_label(record.category) if self.categories is not None else record.category.replace("_"," ").title()
            names=self.knowledge.identity_names(record)
            haystack=" ".join((record.id,*names,category)).casefold()
            if query and query not in haystack:continue
            if query_key and query_key in {
                normalize_tag(value) for value in names
            }:exact_ids.append(record.id)
            rows.append(IncrementalResultRow(record.id,(record.display,category),f"Stable ID: {record.id}\nCategory: {category}",f"SDXL Tag {record.display}; category {category}; stable ID {record.id}"))
        if len(exact_ids)==1:
            exact_id=exact_ids[0]
            rows.sort(key=lambda row:(row.stable_id!=exact_id,row.columns[0].casefold(),row.stable_id))
        self.model.set_rows(rows); self.choose_button.setEnabled(False)
        if self.model.rowCount():self.results.setCurrentIndex(self.model.index(0,0))
        can_resolve=bool(self.allow_identity_creation and query_key and not exact_ids)
        self._typed_exact_unresolved=can_resolve
        exact_entry=None
        if can_resolve and self.identity_service is not None:
            plan=self.identity_service.plan((raw_query,),confirmation=True)
            exact_entry=plan.entries[0] if plan.entries else None
        is_tombstone=bool(exact_entry and exact_entry.status=="tombstone")
        self._tombstone_recovery_record=(
            self.identity_recovery_resolver(exact_entry.stable_id)
            if is_tombstone else None
        )
        can_act=bool(
            exact_entry and (
                exact_entry.status=="new" or
                (is_tombstone and self._tombstone_recovery_record is not None)
            )
        )
        wrong_role=bool(
            exact_entry and exact_entry.resolved and exact_entry.stable_id
            and exact_entry.stable_id not in {record.id for record in self._records}
            and self.category_ids
        )
        tombstone_wrong_role=bool(
            is_tombstone and self._tombstone_recovery_record is not None
            and self.category_ids
            and self._tombstone_recovery_record.category not in self.category_ids
        )
        wrong_role=wrong_role or tombstone_wrong_role
        if wrong_role:can_act=False
        self.create_exact_button.setText(
            "Review Removed Tag…" if is_tombstone else self.identity_create_label
        )
        self.create_exact_button.setAccessibleName(
            "Review and restore removed reusable SDXL Tag"
            if is_tombstone else "Create exact reusable SDXL Tag and continue"
        )
        self.create_exact_button.setVisible(can_act); self.create_exact_button.setEnabled(can_act)
        self.exact_status.setObjectName("mutedLabel"); self.exact_status.style().unpolish(self.exact_status); self.exact_status.style().polish(self.exact_status)
        if can_resolve:
            if wrong_role:
                self._show_identity_error(
                    f'“{raw_query}” already belongs to a reusable Tag outside the '
                    "required role-compatible Categories. Choose a differently named "
                    "compatible Tag or correct the existing Tag in Tag Details; a "
                    "duplicate cannot be created."
                )
            elif is_tombstone and self._tombstone_recovery_record is None:
                self._show_identity_error(
                    f'“{raw_query}” matches removed stable identity '
                    f'“{exact_entry.stable_id}”, but its complete reviewed details '
                    "are not available for safe restoration. Review removed vocabulary."
                )
            elif exact_entry and exact_entry.blocked and not is_tombstone:
                self._show_identity_error(
                    exact_entry.message or
                    "That exact name needs review before it can be confirmed."
                )
            else:
                self.exact_status.setText((
                    f'“{raw_query}” matches removed stable identity '
                    f'“{exact_entry.stable_id}”. Review its restoration to continue.'
                    if is_tombstone else
                    f'No reusable identity exactly matches “{raw_query}”. '
                    +(
                        f"Create it in {self._creation_category_label()}. "
                        if self.identity_category_id else
                        "Create it as an uncategorized SDXL Tag. "
                    )
                    +self.identity_consequence
                )); self.exact_status.show()
        else:self.exact_status.hide()

    def _counts_changed(self,shown,total):
        self.result_count.setText(f"Showing {shown:,} of {total:,} matching SDXL Tags.")
        self.load_more.setVisible(shown<total); self.load_more.setText(f"Load {min(self.PAGE_SIZE,total-shown):,} More…" if shown<total else "Load More…")

    def _selection_changed(self,current,*_):self.choose_button.setEnabled(bool(self.model.stable_id(current)))
    def selected_tag_id(self):return self._selected_tag_id or self.model.stable_id(self.results.currentIndex())
    def created_tag_ids(self):return tuple(self._created_tag_ids)

    def _creation_category_label(self):
        if self.identity_category_id and self.categories is not None:
            return self.categories.path_label(self.identity_category_id)
        if self.identity_category_id:
            return self.identity_category_id.replace("_"," ").title()
        return "Uncategorized"

    def _accept_existing(self):
        stable_id=self.model.stable_id(self.results.currentIndex())
        if stable_id:self._selected_tag_id=stable_id; self.accept()

    def _accept_search(self):
        if self.create_exact_button.isVisible() and self.create_exact_button.isEnabled():
            self._resolve_exact_search()
        elif not getattr(self,"_typed_exact_unresolved",False) and self.model.rowCount():
            self._accept_existing()

    def _confirm_identity_creation(self,value: str) -> bool:
        dialog=QMessageBox(self); dialog.setWindowTitle("Create Reusable SDXL Tag?"); dialog.setIcon(QMessageBox.Question)
        dialog.setText(f'Create “{value}” in reusable System Knowledge?')
        dialog.setInformativeText(
            (
                f"The new tag will be saved immediately in {self._creation_category_label()}. "
                if self.identity_category_id else
                "The new tag will be saved immediately as Uncategorized. "
            )
            +self.identity_consequence
        )
        create=dialog.addButton("Create and Continue",QMessageBox.AcceptRole)
        cancel=dialog.addButton(QMessageBox.Cancel); dialog.setDefaultButton(cancel); dialog.exec()
        return dialog.clickedButton() is create

    def _confirm_identity_restoration(self,entry) -> bool:
        return confirm_removed_identity_restoration(
            self,(entry,),{entry.stable_id:self._tombstone_recovery_record},
            continuation=self.identity_consequence,
        )

    @staticmethod
    def _identity_error_text(plan) -> str:
        messages=[entry.message for entry in plan.blockers if entry.message]
        return " ".join(messages) or (
            "That exact name cannot be resolved to one reusable System Knowledge identity."
        )

    def _show_identity_error(self,text: str):
        self.exact_status.setText(text); self.exact_status.setObjectName("errorLabel"); self.exact_status.style().unpolish(self.exact_status); self.exact_status.style().polish(self.exact_status); self.exact_status.show()

    def _resolve_exact_search(self):
        if not self.allow_identity_creation or self.identity_service is None:return
        value=self.search.text().strip()
        if not value:return
        plan=self.identity_service.plan((value,),confirmation=True)
        tombstone=next((entry for entry in plan.entries if entry.status=="tombstone"),None)
        if tombstone is not None:
            outcome=review_removed_identities(
                self,self.identity_service,plan,source=self.identity_source,
                continuation=self.identity_consequence,
                recovery_resolver=self.identity_recovery_resolver,
                confirmation_callback=(
                    lambda entries,_records:
                    self._confirm_identity_restoration(entries[0])
                ),
            )
            if outcome.restored_ids:
                self._selected_tag_id=outcome.restored_ids[0]; self.accept()
            return
        if plan.blockers:
            self._show_identity_error(self._identity_error_text(plan)); return
        if plan.new_entries and not self._confirm_identity_creation(value):return
        try:
            resolution=(
                self.identity_service.commit(
                    plan,source=self.identity_source,
                    category=self.identity_category_id or "uncategorized",
                )
                if plan.new_entries else
                self.identity_service.require_existing((value,),confirmation=True)
            )
        except (ConfirmedTagIdentityError,OSError,RuntimeError,ValueError) as error:
            self._show_identity_error("The reusable tag was not saved. "+str(error)); return
        if resolution.stable_ids:
            self._created_tag_ids=resolution.created_ids
            self._selected_tag_id=resolution.stable_ids[0]; self.accept()


class TagReferenceEditor(QWidget):
    """Category-aware chips plus bounded picker and typed drag-link target."""

    changed=Signal(object)
    edit_requested=Signal(str)

    def __init__(self,knowledge,categories,*,relation_label: str,single: bool=False,category_ids=(),picker_factory=None,parent=None):
        super().__init__(parent); self.knowledge=knowledge; self.categories=categories; self.relation_label=relation_label; self.single=bool(single); self.category_ids=tuple(dict.fromkeys(str(value) for value in category_ids if str(value))); self._values=[]; self._current_tag_id=""; self._validator=None; self._chips=[]; self._additional_excluded=set(); self._read_only_reason=""
        self._picker_factory=picker_factory
        self.setAcceptDrops(True); root=QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(4)
        self.container=QWidget(); self.flow=FlowLayout(self.container); root.addWidget(self.container)
        choose_label="Choose Base Tag…" if self.relation_label.endswith("Base Tag") else f"Choose {self.relation_label}…"
        actions=QHBoxLayout(); self.status=QLabel(); self.status.setObjectName("mutedLabel"); self.status.setWordWrap(True); self.add_button=QPushButton(choose_label if self.single else "Add Tag…"); self.add_button.setProperty("compact",True); self.add_button.setAccessibleName(f"Choose {self.relation_label} SDXL Tag" if self.single else f"Add {self.relation_label} SDXL Tag"); actions.addWidget(self.status,1); actions.addWidget(self.add_button); root.addLayout(actions)
        self.add_button.clicked.connect(self._choose)

    def set_context(self,current_tag_id: str,validator=None):self._current_tag_id=str(current_tag_id); self._validator=validator; self._refresh_status()
    def release_context(self):self._current_tag_id=""; self._validator=None; self._picker_factory=None
    def set_excluded_ids(self,values):self._additional_excluded={str(value) for value in values if str(value)}
    def values(self):return tuple(self._values)

    def set_read_only(self,reason: str=""):
        self._read_only_reason=str(reason or "").strip(); self.setAcceptDrops(not bool(self._read_only_reason)); self.add_button.setVisible(not bool(self._read_only_reason)); self._render()

    def set_values(self,values):
        self._values=list(dict.fromkeys(str(value) for value in values if str(value))); self._render()

    def _render(self):
        self.flow.clear(); self._chips=[]
        relationship_label="" if self._read_only_reason else self.relation_label
        details_enabled=bool(self._read_only_reason)
        accessible_role=self.relation_label if self._read_only_reason else ""
        for stable_id in self._values:
            record=self.knowledge.tags.get(stable_id)
            if record is None:
                chip=TagChip(stable_id,category_label="Missing stable-ID reference",color="#CC3333",tooltip=f"Missing SDXL Tag stable ID: {stable_id}",editable=False,details_enabled=details_enabled,relationship_label=relationship_label,accessible_role=accessible_role,stable_id=stable_id)
            else:
                label,color,tooltip=category_details(record.display,self.knowledge,self.categories)
                chip=TagChip(record.display,category_label=label,color=color,tooltip=tooltip,editable=False,draggable=True,details_enabled=details_enabled,relationship_label=relationship_label,accessible_role=accessible_role,stable_id=record.id,category_id=record.category,sensitivity=effective_tag_sensitivity(record,self.categories)[0])
                chip.set_knowledge_context(self.knowledge,self.categories)
            if relationship_label:chip.remove_button.setFixedWidth(30)
            chip.remove_requested.connect(lambda _value,tag_id=stable_id:self._remove(tag_id)); chip.edit_requested.connect(self.edit_requested); self._chips.append(chip); self.flow.addWidget(chip)
            if self._read_only_reason:
                chip.draggable=False; chip.remove_button.hide(); chip.setAcceptDrops(False)
        if not self._values:
            empty=QLabel("Not set" if self.single else "No linked SDXL Tags"); empty.setObjectName("mutedLabel"); self.flow.addWidget(empty)
        self.container.updateGeometry(); self._refresh_status()

    def _refresh_status(self,text="",*,error=False):
        if not text:text=(self._read_only_reason or ("Select one SDXL Tag." if self.single else f"Drop SDXL Tag chips here to add {self.relation_label} links."))
        self.status.setText(text); self.status.setObjectName("errorLabel" if error else "mutedLabel"); self.status.style().unpolish(self.status); self.status.style().polish(self.status)

    def _validated(self,values):
        if self.single and len(values)>1:return f"{self.relation_label} accepts one SDXL Tag at a time."
        if callable(self._validator):
            try:message=self._validator(tuple(values))
            except ValueError as error:message=str(error)
            if message:return str(message)
        return ""

    def _apply(self,stable_ids):
        if self._read_only_reason:
            self._refresh_status(self._read_only_reason); return False
        requested=[value for value in dict.fromkeys(str(item).strip() for item in stable_ids) if value]
        missing=[value for value in requested if value not in self.knowledge.tags]
        if missing:
            self._refresh_status(f"The complete selection was not linked because {len(missing):,} SDXL Tag reference{'s are' if len(missing)!=1 else ' is'} no longer available.",error=True); return False
        resolved=requested
        if not resolved:self._refresh_status("The dropped SDXL Tag is no longer available.",error=True); return False
        if self.single and len(resolved)>1:self._refresh_status(f"{self.relation_label} accepts one SDXL Tag; the {len(resolved):,}-tag selection was not linked.",error=True); return False
        candidate=resolved[:1] if self.single else list(dict.fromkeys((*self._values,*resolved)))
        error=self._validated(candidate)
        if error:self._refresh_status(error,error=True); return False
        if candidate==self._values:self._refresh_status("That SDXL Tag link is already present."); return True
        self._values=candidate; self._render(); self.changed.emit(tuple(self._values)); return True

    def _remove(self,stable_id):
        if self._read_only_reason:
            self._refresh_status(self._read_only_reason); return
        candidate=[value for value in self._values if value!=stable_id]; error=self._validated(candidate)
        if error:self._refresh_status(error,error=True); return
        self._values=candidate; self._render(); self.changed.emit(tuple(self._values))

    def _choose(self):
        if self._read_only_reason:
            self._refresh_status(self._read_only_reason); return
        excluded={self._current_tag_id,*self._values,*self._additional_excluded}; factory=self._picker_factory or TagReferencePickerDialog; dialog=factory(self.knowledge,self.categories,title=f"Choose {self.relation_label} SDXL Tag",exclude_ids=excluded,category_ids=self.category_ids,parent=self)
        if dialog is None:return
        if dialog.exec()==QDialog.Accepted:self._apply((dialog.selected_tag_id(),))

    def _drag_ids(self,event):return decode_tag_drag_ids(event.mimeData(),self.knowledge)
    def dragEnterEvent(self,event):
        if self._read_only_reason:
            event.ignore(); self._refresh_status(self._read_only_reason); return
        values=self._drag_ids(event); candidate=values[:1] if self.single else tuple(dict.fromkeys((*self._values,*values)))
        error="" if values else "This is not a recognized SDXL Tag drag."
        if not error and self.single and len(values)>1:error=f"{self.relation_label} accepts one SDXL Tag; the {len(values):,}-tag selection cannot be linked here."
        if not error:error=self._validated(candidate)
        if error:event.ignore(); self._refresh_status(error,error=True); return
        event.setDropAction(Qt.LinkAction); event.accept(); self._refresh_status(f"Release to create {self.relation_label} link{'s' if len(values)!=1 else ''} for {len(values):,} SDXL Tag{'s' if len(values)!=1 else ''}.")

    def dragMoveEvent(self,event):self.dragEnterEvent(event)
    def dragLeaveEvent(self,event):self._refresh_status(); super().dragLeaveEvent(event)
    def dropEvent(self,event):
        if self._read_only_reason:
            event.ignore(); self._refresh_status(self._read_only_reason); return
        if self._apply(self._drag_ids(event)):event.setDropAction(Qt.LinkAction); event.accept()
        else:event.ignore()


class ModifierSlotEditor(QWidget):
    """One explicit single-value control for every allowed modifier family."""

    changed=Signal(object)
    REVIEW_VALUE="__modifier_slot_review_required__"

    def __init__(self,parent=None):
        super().__init__(parent)
        self._values=[]; self._groups=(); self._definitions={}; self._suggestions={}; self._labels={}
        self._available=False; self._reason=""; self._boxes={}; self._pending_changes={}; self._suppressed_suggestions=set()
        self.setAccessibleName("Named modifier composition slots")
        root=QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(6)
        self.status=QLabel(); self.status.setWordWrap(True); self.status.setObjectName("mutedLabel")
        self.status.setAccessibleName("Modifier slot guidance"); root.addWidget(self.status)
        self.slot_host=QWidget(); self.slot_form=QFormLayout(self.slot_host)
        self.slot_form.setContentsMargins(0,0,0,0); self.slot_form.setSpacing(6)
        self.slot_scroll=QScrollArea(); self.slot_scroll.setWidgetResizable(True)
        self.slot_scroll.setFrameShape(QFrame.NoFrame)
        self.slot_scroll.setHorizontalScrollBarPolicy(Qt.ScrollBarAlwaysOff)
        self.slot_scroll.setMinimumHeight(85); self.slot_scroll.setMaximumHeight(185)
        self.slot_scroll.setWidget(self.slot_host); root.addWidget(self.slot_scroll)
        self.preserved_notice=QLabel(); self.preserved_notice.setWordWrap(True)
        self.preserved_notice.setObjectName("mutedLabel")
        self.preserved_notice.setAccessibleName("Preserved modifier values")
        root.addWidget(self.preserved_notice)
        self._render()

    def values(self):return tuple(self._values)
    def preserved_values(self):return tuple(self._classify()[1])
    def pending_changes(self):
        return tuple(dict(change) for change in self._pending_changes.values())
    def clear_pending_changes(self):
        self._pending_changes.clear(); self._suppressed_suggestions.clear()
    def slot_box(self,group):return self._boxes.get(str(group))

    def set_values(self,values):
        self._values=list(dict.fromkeys(str(value) for value in values if str(value)))
        self._pending_changes.clear(); self._suppressed_suggestions.clear()
        self._render()

    def configure(self,groups,definitions,suggestions=(),*,labels=None,available=True,reason=""):
        self._groups=tuple(dict.fromkeys(str(group) for group in groups if str(group)))
        self._definitions={
            str(group):tuple(values) for group,values in definitions.items()
        }
        self._labels={str(group):str(label) for group,label in (labels or {}).items()}
        self._suggestions={
            str(item.group):item for item in suggestions
            if (str(item.group),str(item.suggested_value_id))
            not in self._suppressed_suggestions
        }
        self._available=bool(available and self._groups)
        self._reason=str(reason or "").strip()
        self._render()

    def resolved_values(self):
        grouped,_preserved,_ambiguous=self._classify()
        return {
            group:values[0] for group,values in grouped.items()
            if len(values)==1
        }

    def conflict_groups(self):
        grouped,_preserved,ambiguous=self._classify()
        return tuple(
            group for group in self._groups
            if len(grouped.get(group,()))>1 or group in ambiguous
        )

    def _classify(self):
        membership={}
        for group,definitions in self._definitions.items():
            for value in definitions:
                membership.setdefault(str(value.id),set()).add(group)
        allowed=set(self._groups); grouped={group:[] for group in self._groups}
        preserved=[]; ambiguous=set()
        for value_id in self._values:
            matches=membership.get(value_id,set()) & allowed
            if len(matches)==1:
                grouped[next(iter(matches))].append(value_id)
            else:
                preserved.append(value_id)
                if len(matches)>1:ambiguous.update(matches)
        return grouped,preserved,ambiguous

    def _label_for(self,value_id):
        for definitions in self._definitions.values():
            for value in definitions:
                if str(value.id)==str(value_id):return str(value.display)
        return str(value_id)

    def _render(self):
        while self.slot_form.rowCount():self.slot_form.removeRow(0)
        self._boxes={}
        grouped,preserved,ambiguous=self._classify()
        if not self._available:
            self.status.setText(self._reason or "Choose a Base Tag with a reviewed modifier policy to edit named slots.")
            self.status.show(); self.slot_host.hide(); self.slot_scroll.hide()
        else:
            self.status.setText(
                "Choose at most one reviewed value in each named slot. "
                "Exact-name suggestions remain separate until you accept them."
            )
            self.setToolTip(self.status.text()); self.setAccessibleDescription(self.status.text())
            self.status.hide(); self.slot_host.show(); self.slot_scroll.show()
            for group in self._groups:
                self._add_slot(group,grouped.get(group,()),group in ambiguous)
        if preserved:
            labels=", ".join(self._label_for(value) for value in preserved)
            self.preserved_notice.setText(
                "Preserved outside the current named slots: "+labels+". "
                "These stable values will not be discarded automatically."
            )
            self.preserved_notice.show()
        else:self.preserved_notice.hide()
        self.updateGeometry()

    def _add_slot(self,group,current_values,ambiguous_membership):
        definitions=self._definitions.get(group,())
        label=self._labels.get(group) or str(group).replace("modifier_","").replace("_"," ").title()
        host=QWidget(); layout=QVBoxLayout(host); layout.setContentsMargins(0,0,0,0); layout.setSpacing(3)
        box=BoundedSearchableComboBox(
            placeholder=f"Search reviewed {label.casefold()} values…",max_visible=12,
        )
        box.setAccessibleName(f"{label} modifier slot")
        box.setAccessibleDescription(
            f"Choose one reviewed {label} value or Not set. Suggestions are never selected automatically."
        )
        conflict=bool(len(current_values)>1 or ambiguous_membership)
        if conflict:box.addItem("Review required — choose one",self.REVIEW_VALUE)
        box.addItem("Not set","")
        for value in definitions:
            box.addItem(str(value.display),str(value.id),search_terms=(str(value.id),*value.aliases))
        if conflict:
            box.setCurrentIndex(box.findData(self.REVIEW_VALUE))
        elif current_values:
            box.setCurrentIndex(box.findData(current_values[0]))
        else:box.setCurrentIndex(box.findData(""))
        box.currentIndexChanged.connect(
            lambda _index,slot_group=group,control=box:
            self._slot_changed(slot_group,control)
        )
        layout.addWidget(box); self._boxes[group]=box
        note=QLabel(); note.setWordWrap(True); note.setObjectName("mutedLabel")
        note.setAccessibleName(f"{label} modifier slot status")
        if conflict:
            current_labels=", ".join(self._label_for(value) for value in current_values)
            note.setText(
                "Review required: this single-value slot currently resolves to "+
                (current_labels or "more than one family")+". Choose one value or Not set."
            )
        suggestion=self._suggestions.get(group)
        if suggestion is not None:
            suggested=self._label_for(suggestion.suggested_value_id)
            if suggestion.status in {"suggested","contradictory"}:
                if suggestion.status=="suggested":
                    note.setText(f"Suggested: {suggested} — exact tag/base-name match. Not applied.")
                else:
                    note.setText(f"Review required: exact-name suggestion {suggested} differs from the current value.")
                accept=QPushButton("Use Suggested"); accept.setProperty("compact",True)
                accept.setAccessibleName(f"Use suggested {label} value {suggested}")
                accept.clicked.connect(
                    lambda _checked=False,slot_group=group,value_id=suggestion.suggested_value_id,control=box:
                    self._use_suggestion(slot_group,value_id,control)
                )
                reject=None
                if not conflict:
                    reject=QPushButton(
                        "Keep Current" if suggestion.status=="contradictory" else
                        "Dismiss"
                    ); reject.setProperty("compact",True)
                    reject.setAccessibleName(f"Reject suggested {label} value {suggested}")
                    reject.clicked.connect(
                        lambda _checked=False,slot_group=group,value_id=suggestion.suggested_value_id:
                        self._reject_suggestion(slot_group,value_id)
                    )
                layout.addWidget(note)
                row=QHBoxLayout(); row.addStretch(1); row.addWidget(accept)
                if reject is not None:row.addWidget(reject)
                layout.addLayout(row)
                self.slot_form.addRow(label,host); return
            if suggestion.status=="ambiguous":
                candidates=", ".join(self._label_for(value) for value in suggestion.candidate_value_ids)
                note.setText(f"Review required: exact-name parsing is ambiguous ({candidates}). {suggestion.reason}")
            elif suggestion.status=="current" and not conflict:
                note.setText("Current reviewed value also matches the exact tag and Base Tag names.")
        if note.text():layout.addWidget(note)
        self.slot_form.addRow(label,host)

    def _use_suggestion(self,group,value_id,box):
        index=box.findData(str(value_id))
        if index<0:return
        box.setProperty("modifierSuggestionAcceptance",True)
        try:box.setCurrentIndex(index)
        finally:
            try:box.setProperty("modifierSuggestionAcceptance",False)
            except RuntimeError:pass

    def _reject_suggestion(self,group,value_id):
        grouped,_preserved,_ambiguous=self._classify()
        current=tuple(grouped.get(group,()))
        previous=self._pending_changes.get(str(group),{}).get(
            "previous_value_ids",current,
        )
        self._pending_changes[str(group)]={
            "group":str(group),"previous_value_ids":previous,
            "value_id":current[0] if len(current)==1 else "",
            "source":"reject_exact_name_suggestion",
            "suggested_value_id":str(value_id),
        }
        self._suppressed_suggestions.add((str(group),str(value_id)))
        self._suggestions.pop(str(group),None); self._render()
        self.changed.emit(tuple(self._values))

    def _slot_changed(self,group,box):
        value_id=str(box.currentData() or "")
        if value_id==self.REVIEW_VALUE:return
        grouped,preserved,_ambiguous=self._classify()
        current=tuple(grouped.get(group,()))
        previous=self._pending_changes.get(str(group),{}).get(
            "previous_value_ids",current,
        )
        grouped[group]=[value_id] if value_id else []
        next_values=[]
        for slot_group in self._groups:next_values.extend(grouped.get(slot_group,()))
        next_values.extend(preserved)
        source=(
            "exact_name_suggestion" if box.property("modifierSuggestionAcceptance") else
            "manual_clear" if not value_id else "manual_choice"
        )
        self._pending_changes[str(group)]={
            "group":str(group),"previous_value_ids":previous,
            "value_id":value_id,"source":source,
        }
        self._values=list(dict.fromkeys(next_values)); self._render()
        self.changed.emit(tuple(self._values))


class TokenChipEditor(QWidget):
    """Explicit free-text or reviewed-choice reusable values."""

    changed=Signal(object)

    def __init__(self,*,kind: str,allow_text: bool,placeholder: str="",parent=None):
        super().__init__(parent); self.kind=kind; self.allow_text=bool(allow_text); self._values=[]; self._choices={}; self._available=True; self._unavailable_reason=""; self._read_only_reason=""; self._remove_guard=None
        root=QVBoxLayout(self); root.setContentsMargins(0,0,0,0); root.setSpacing(4); self.container=QWidget(); self.flow=FlowLayout(self.container); root.addWidget(self.container)
        row=QHBoxLayout()
        if self.allow_text:
            self.input=QLineEdit(); self.input.setPlaceholderText(f"Enter {kind}…"); self.input.returnPressed.connect(self._add); self.input.editingFinished.connect(self._add)
        else:
            self.input=BoundedSearchableComboBox(placeholder=placeholder or f"Search reviewed {kind}s…",max_visible=12)
            self.input.currentIndexChanged.connect(self._update_add_state)
        button_kind=kind.title().replace("Sdxl","SDXL")
        self.add_button=QPushButton(f"Add {button_kind}"); self.add_button.setProperty("compact",True); self.add_button.clicked.connect(self._add); row.addWidget(self.input,1); row.addWidget(self.add_button); root.addLayout(row); self._render()

    def values(self):return tuple(self._values)
    def choice_values(self):return frozenset(self._choices)
    def set_values(self,values):self._values=list(dict.fromkeys(str(value) for value in values if str(value))); self._render(); self._update_add_state()
    def set_remove_guard(self,guard):self._remove_guard=guard

    def set_available(self,available: bool,reason: str=""):
        self._available=bool(available); self._unavailable_reason=str(reason or "").strip()
        if not self.allow_text:
            visible=not self._read_only_reason and self._available
            self.input.setVisible(visible); self.add_button.setVisible(visible)
            description=self._read_only_reason or self._unavailable_reason
            self.input.setAccessibleDescription(description)
            self.add_button.setAccessibleDescription(description)
        self._render(); self._update_add_state()

    def set_read_only(self,reason: str=""):
        self._read_only_reason=str(reason or "").strip()
        editable=not bool(self._read_only_reason)
        self.input.setVisible(editable and (self.allow_text or self._available))
        self.add_button.setVisible(editable and (self.allow_text or self._available))
        description=self._read_only_reason or self._unavailable_reason
        self.input.setAccessibleDescription(description)
        self.add_button.setAccessibleDescription(description)
        self._render(); self._update_add_state()

    def set_choices(self,choices):
        self._choices={str(value):(str(label),str(detail)) for value,label,detail in choices}; current=self.input.currentData() if not self.allow_text else None
        if not self.allow_text:
            self.input.clear()
            for value,(label,detail) in self._choices.items():self.input.addItem(f"{label} — {detail}" if detail else label,value)
            selected=self.input.findData(current)
            self.input.setCurrentIndex(selected if selected>=0 else -1)
            self._update_add_state()

    def _update_add_state(self,*_):
        if self.allow_text:return
        value=str(self.input.currentData() or "")
        self.add_button.setEnabled(bool(self._available and value in self._choices and value not in self._values))

    def _render(self):
        self.flow.clear()
        for value in self._values:
            label=self._choices.get(value,(value,""))[0]; chip=QFrame(); chip.setObjectName("typedValueChip"); chip.setStyleSheet("QFrame#typedValueChip { background:palette(base); border:1px solid palette(midlight); border-radius:9px; }")
            layout=QHBoxLayout(chip); layout.setContentsMargins(8,2,4,2); layout.setSpacing(6); text=QLabel(label); remove=QPushButton("Remove"); remove.setFlat(True); remove.setProperty("compact",True); remove.setMinimumWidth(58); remove.setFixedHeight(26); remove.setAccessibleName(f"Remove {self.kind} {label}"); remove.setToolTip(f'Remove {self.kind} “{label}”'); remove.setEnabled(bool(not self._read_only_reason and (self.allow_text or self._available))); remove.clicked.connect(lambda _checked=False,item=value:self._remove(item)); layout.addWidget(text); layout.addWidget(remove); self.flow.addWidget(chip)
        if not self._values:
            empty_text=self._read_only_reason or (self._unavailable_reason if not self._available and self._unavailable_reason else "No aliases" if self.kind=="alias" else f"No {self.kind}s")
            empty=QLabel(empty_text); empty.setObjectName("mutedLabel"); self.flow.addWidget(empty)
        elif self._read_only_reason or (not self._available and self._unavailable_reason):
            notice=QLabel(self._read_only_reason or self._unavailable_reason); notice.setObjectName("mutedLabel"); notice.setWordWrap(True); self.flow.addWidget(notice)
        self.container.updateGeometry()

    def _add(self):
        if self._read_only_reason:return
        value=self.input.text().strip() if self.allow_text else str(self.input.currentData() or "")
        if not value:return
        if self.allow_text:
            duplicate=next((item for item in self._values if normalize_tag(item)==normalize_tag(value)),None)
            if duplicate:self.input.clear(); return
        elif value not in self._choices or value in self._values:return
        self._values.append(value); self._render(); self.changed.emit(tuple(self._values))
        if self.allow_text:self.input.clear()
        else:self.input.setCurrentIndex(-1); self._update_add_state()

    def _remove(self,value):
        if self._read_only_reason or (not self.allow_text and not self._available):return
        if self._remove_guard is not None and not self._remove_guard(value):return
        self._values=[item for item in self._values if item!=value]; self._render(); self._update_add_state(); self.changed.emit(tuple(self._values))
