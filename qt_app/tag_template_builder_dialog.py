from __future__ import annotations

from dataclasses import replace
from typing import Any

from PySide6.QtCore import QSignalBlocker,Qt
from PySide6.QtWidgets import (
    QAbstractItemView,QCheckBox,QComboBox,QDialog,QDialogButtonBox,QFormLayout,
    QHBoxLayout,QHeaderView,QInputDialog,QLabel,QListWidget,QListWidgetItem,
    QMessageBox,QPushButton,QScrollArea,QSpinBox,QTreeWidget,QTreeWidgetItem,
    QTabWidget,QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core.tag_template_builder import (
    MAX_TAG_TEMPLATE_ANCHORS,MAX_TAG_TEMPLATE_SLOTS,
    TagTemplateAnchor,TagTemplateBuilderService,TagTemplateDraft,
    TagTemplateSlot,TagTemplateToken,
)
from core.tag_template_store import DEFAULT_TAG_TEMPLATE_MAX_PER_RUN
from .components import BoundedSearchableComboBox
from .modal_feedback import FeedbackEnabledDialog


class ForbiddenCombinationDialog(QDialog):
    """Choose one exact stable value per current typed selector."""

    def __init__(self,slots,definitions,parent=None):
        super().__init__(parent); self.setWindowTitle("Exclude Exact Combination")
        layout=QVBoxLayout(self)
        intro=QLabel(
            "Choose the exact value combination that must not produce a reusable Tag. The stable value IDs are stored; wording is not parsed."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel"); layout.addWidget(intro)
        form=QFormLayout(); self.choices={}
        for slot in slots:
            combo=QComboBox(); combo.setAccessibleName(
                f"Forbidden {slot.family_label} value"
            )
            by_id={str(item.id):item for item in definitions.get(slot.family_id,())}
            for stable_id in slot.included_value_ids:
                value=by_id.get(stable_id)
                if value is not None:combo.addItem(str(value.display),stable_id)
            self.choices[slot.id]=combo
            form.addRow("{"+slot.placeholder+"}",combo)
        layout.addLayout(form)
        buttons=QDialogButtonBox(QDialogButtonBox.Ok|QDialogButtonBox.Cancel)
        buttons.accepted.connect(self.accept); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

    @property
    def combination(self):
        return {
            slot_id:str(combo.currentData() or "")
            for slot_id,combo in self.choices.items() if combo.currentData()
        }


class TagTemplateBuilderDialog(FeedbackEnabledDialog):
    """Assemble stable Tag chips and typed selectors without ambiguous text."""

    feedback_workflow="Build reusable Tags"

    def __init__(
        self,tags,categories,foundation,parent=None,*,record=None,
        modifier_roles=None,
    ):
        super().__init__(parent); self.tags=tags; self.categories=categories
        self.foundation=foundation; self.modifier_roles=dict(modifier_roles or {})
        self.record=record; self._syncing=False
        self.definitions=foundation.modifier_group_definitions(
            tags,categories,self.modifier_roles,
        )
        self.anchors={}; self.slots={}; self.tokens=[]; self.forbidden=[]
        self.setWindowTitle("Edit Reusable Tag Template" if record else "Build Reusable Tags")
        self.resize(1120,790)
        outer=QVBoxLayout(self)
        intro=QLabel(
            "Build reviewed Tag vocabulary for identifying images and preparing training captions. "
            "This does not tag images; preview the combinations before creating vocabulary."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel"); outer.addWidget(intro)
        self.tasks=QTabWidget()
        self.pattern_scroll=QScrollArea(); self.pattern_scroll.setWidgetResizable(True)
        content=QWidget(); layout=QVBoxLayout(content); self.pattern_scroll.setWidget(content)
        self.tasks.addTab(self.pattern_scroll,"Build Tag Pattern")
        self.limits_task=QWidget(); limits_layout=QVBoxLayout(self.limits_task)
        self.tasks.addTab(self.limits_task,"Limits and Exclusions")
        outer.addWidget(self.tasks,1)

        chooser=QFormLayout()
        anchor_row=QHBoxLayout(); self.anchor_choice=BoundedSearchableComboBox(
            placeholder="Search reviewed SDXL Tags…",max_visible=20,
        )
        self.anchor_choice.setAccessibleName("Stable literal SDXL Tag chip")
        for item in sorted(tags.tags.values(),key=lambda value:(value.display.casefold(),value.id)):
            self.anchor_choice.addItem(
                f"{item.display} — {item.id}",item.id,
                search_terms=tuple(getattr(item,"aliases",())),
            )
        self.anchor_role=QComboBox(); self.anchor_role.addItem("Primary Base Tag","base")
        self.anchor_role.addItem("Component","component")
        self.anchor_role.addItem("Implication","implication")
        self.add_anchor=QPushButton("Add Tag Chip")
        self.add_anchor.clicked.connect(self._add_anchor)
        anchor_row.addWidget(self.anchor_choice,2); anchor_row.addWidget(self.anchor_role)
        anchor_row.addWidget(self.add_anchor); chooser.addRow("Reviewed Tag",anchor_row)
        slot_row=QHBoxLayout(); self.family_choice=QComboBox()
        self.family_choice.setAccessibleName("Reviewed modifier family selector")
        for family_id,values in sorted(self.definitions.items(),key=lambda item:item[0]):
            if not values:continue
            label_resolver=getattr(foundation,"modifier_group_label",None)
            label=str(
                getattr(values[0],"group_display","")
                or (label_resolver(family_id,categories) if callable(label_resolver) else "")
                or family_id.replace("modifier_","").replace("_"," ").title()
            )
            self.family_choice.addItem(f"{label} — {len(values):,} reviewed values",(family_id,label))
        self.add_slot=QPushButton("Add Variable Detail")
        self.add_slot.clicked.connect(self._add_slot)
        slot_row.addWidget(self.family_choice,1); slot_row.addWidget(self.add_slot)
        chooser.addRow("Variable detail",slot_row); layout.addLayout(chooser)

        self.token_tree=QTreeWidget()
        self.token_tree.setHeaderLabels(("Order","Chip / selector","Stable identity","Role / requirement"))
        self.token_tree.setRootIsDecorated(False); self.token_tree.setSelectionMode(QAbstractItemView.SingleSelection)
        self.token_tree.setMaximumHeight(260)
        self.token_tree.setAccessibleName("Ordered reusable Tag template chips")
        self.token_tree.itemSelectionChanged.connect(self._token_selected)
        header=self.token_tree.header(); header.setSectionResizeMode(1,QHeaderView.Stretch)
        for column,width in ((0,70),(2,260),(3,170)):
            header.setSectionResizeMode(column,QHeaderView.Interactive); self.token_tree.setColumnWidth(column,width)
        layout.addWidget(self.token_tree)
        actions=QHBoxLayout(); self.up=QPushButton("Move Up"); self.down=QPushButton("Move Down")
        self.repeat=QPushButton("Repeat Selector"); self.separator=QPushButton("Add Separator…")
        self.remove=QPushButton("Remove Chip")
        self.up.clicked.connect(lambda:self._move(-1)); self.down.clicked.connect(lambda:self._move(1))
        self.repeat.clicked.connect(self._repeat_slot); self.separator.clicked.connect(self._add_separator)
        self.remove.clicked.connect(self._remove_token)
        for button in (self.up,self.down,self.repeat,self.separator,self.remove):
            button.setProperty("compact",True); actions.addWidget(button)
        actions.addStretch(1); layout.addLayout(actions)

        values_heading=QLabel("Values for Selected Selector")
        values_heading.setObjectName("sectionTitle"); layout.addWidget(values_heading)
        self.slot_status=QLabel("Select a typed selector chip to review its exact values.")
        self.slot_status.setObjectName("mutedLabel"); self.slot_status.setWordWrap(True)
        layout.addWidget(self.slot_status)
        self.required=QCheckBox("Always include this detail in every generated Tag")
        self.required.setAccessibleName("Require selected typed selector")
        self.required.toggled.connect(self._required_changed); layout.addWidget(self.required)
        self.values=QListWidget(); self.values.setMaximumHeight(180)
        self.values.setAccessibleName("Included reviewed values for selected typed selector")
        self.values.itemChanged.connect(self._values_changed); layout.addWidget(self.values)

        limits_intro=QLabel(
            "Optional safeguards for this pattern. Exclude only exact reviewed combinations; the per-run limit bounds the preview."
        )
        limits_intro.setWordWrap(True); limits_intro.setObjectName("mutedLabel")
        limits_layout.addWidget(limits_intro)
        constraint_actions=QHBoxLayout(); self.constraint_status=QLabel()
        self.constraint_status.setObjectName("mutedLabel")
        self.add_forbidden=QPushButton("Exclude Exact Combination…")
        self.clear_forbidden=QPushButton("Clear Combination Exclusions")
        self.add_forbidden.clicked.connect(self._add_forbidden)
        self.clear_forbidden.clicked.connect(self._clear_forbidden)
        constraint_actions.addWidget(self.constraint_status,1)
        constraint_actions.addWidget(self.add_forbidden); constraint_actions.addWidget(self.clear_forbidden)
        limits_layout.addLayout(constraint_actions)
        bounds=QHBoxLayout(); bounds.addWidget(QLabel("Maximum results per run"))
        self.max_per_run=QSpinBox(); self.max_per_run.setRange(1,20_000)
        self.max_per_run.setValue(record.max_per_run if record else DEFAULT_TAG_TEMPLATE_MAX_PER_RUN)
        self.max_per_run.setAccessibleName("Maximum generated Tags per run")
        bounds.addWidget(self.max_per_run); bounds.addStretch(1); limits_layout.addLayout(bounds)
        limits_layout.addStretch(1)
        self.status=QLabel(); self.status.setObjectName("mutedLabel"); self.status.setWordWrap(True)
        self.status.setAccessibleName("Structured template validation status"); outer.addWidget(self.status)

        buttons=QDialogButtonBox(QDialogButtonBox.Cancel)
        self.preview_button=buttons.addButton("Preview Complete Expansion…",QDialogButtonBox.AcceptRole)
        self.preview_button.setAccessibleName("Preview complete reusable Tag expansion")
        self.preview_button.clicked.connect(self._accept_preview)
        buttons.rejected.connect(self.reject); outer.addWidget(buttons)
        if record:self._load(TagTemplateBuilderService.draft_from_record(record))
        self._refresh()

    def _load(self,draft):
        self.anchors={item.tag_id:item for item in draft.anchors}
        self.slots={item.id:item for item in draft.slots}
        self.tokens=list(draft.tokens)
        self.forbidden=[dict(item) for item in draft.constraints.get("forbidden_combinations",()) if isinstance(item,dict)]
        self.max_per_run.setValue(draft.max_per_run)

    def _add_anchor(self):
        stable_id=str(self.anchor_choice.currentData() or "")
        record=self.tags.tags.get(stable_id)
        if record is None:return
        if stable_id in self.anchors:
            QMessageBox.information(self,"Tag chip already used","Reuse is represented by the existing stable chip; duplicate literal authority is not added."); return
        if len(self.anchors)>=MAX_TAG_TEMPLATE_ANCHORS:
            QMessageBox.warning(self,"Anchor limit",f"A template supports at most {MAX_TAG_TEMPLATE_ANCHORS} literal chips."); return
        role=str(self.anchor_role.currentData() or "component")
        if role=="base" and any(item.role=="base" for item in self.anchors.values()):
            role="component"
        self.anchors[stable_id]=TagTemplateAnchor(stable_id,record.display,role)
        self.tokens.append(TagTemplateToken("anchor",stable_id)); self._refresh(select=len(self.tokens)-1)

    def _add_slot(self):
        data=self.family_choice.currentData()
        if not isinstance(data,tuple):return
        if len(self.slots)>=MAX_TAG_TEMPLATE_SLOTS:
            QMessageBox.warning(self,"Selector limit",f"A template supports at most {MAX_TAG_TEMPLATE_SLOTS} typed selectors."); return
        family_id,label=data; suffix=1
        while f"slot_{family_id}_{suffix}" in self.slots:suffix+=1
        slot_id=f"slot_{family_id}_{suffix}"; definitions=self.definitions.get(family_id,())
        self.slots[slot_id]=TagTemplateSlot(
            slot_id,family_id,label,family_id.removeprefix("modifier_"),
            tuple(str(item.id) for item in definitions),
        )
        self.tokens.append(TagTemplateToken("modifier_slot",slot_id)); self._refresh(select=len(self.tokens)-1)

    def _selected_index(self):
        item=self.token_tree.currentItem()
        return int(item.data(0,Qt.UserRole)) if item is not None else -1

    def _move(self,offset):
        index=self._selected_index(); target=index+offset
        if index<0 or target<0 or target>=len(self.tokens):return
        self.tokens[index],self.tokens[target]=self.tokens[target],self.tokens[index]
        self._refresh(select=target)

    def _repeat_slot(self):
        index=self._selected_index()
        if index<0 or self.tokens[index].kind!="modifier_slot":return
        self.tokens.insert(index+1,self.tokens[index]); self._refresh(select=index+1)

    def _add_separator(self):
        text,ok=QInputDialog.getText(self,"Visible Separator","Punctuation or spacing only")
        if not ok:return
        if not text or any(character.isalnum() for character in text):
            QMessageBox.warning(self,"Invalid separator","Separators may contain punctuation or spacing only; literal Tag wording must use stable chips."); return
        index=self._selected_index(); insert=index+1 if index>=0 else len(self.tokens)
        self.tokens.insert(insert,TagTemplateToken("separator",text=text)); self._refresh(select=insert)

    def _remove_token(self):
        index=self._selected_index()
        if index<0:return
        token=self.tokens.pop(index)
        if not any(item.kind==token.kind and item.reference_id==token.reference_id for item in self.tokens):
            if token.kind=="anchor":self.anchors.pop(token.reference_id,None)
            elif token.kind=="modifier_slot":
                self.slots.pop(token.reference_id,None)
                self.forbidden=[
                    {key:value for key,value in item.items() if key!=token.reference_id}
                    for item in self.forbidden
                ]
                self.forbidden=[item for item in self.forbidden if item]
        self._refresh(select=min(index,len(self.tokens)-1))

    def _token_selected(self):
        index=self._selected_index(); token=self.tokens[index] if 0<=index<len(self.tokens) else None
        slot=self.slots.get(token.reference_id) if token and token.kind=="modifier_slot" else None
        self.repeat.setEnabled(slot is not None); self.required.setEnabled(slot is not None)
        self.values.setEnabled(slot is not None); self._syncing=True
        try:
            self.values.clear(); self.required.setChecked(bool(slot and slot.required))
            if slot is None:
                self.slot_status.setText("Select a typed selector chip to review its exact values.")
            else:
                self.slot_status.setText(
                    f"Choose which reviewed {slot.family_label} values {{{slot.placeholder}}} may use. "
                    "Unchecked values are excluded."
                )
                included=set(slot.included_value_ids)
                for value in self.definitions.get(slot.family_id,()):
                    item=QListWidgetItem(f"{value.display} — {value.id}")
                    item.setData(Qt.UserRole,str(value.id)); item.setFlags(item.flags()|Qt.ItemIsUserCheckable)
                    item.setCheckState(Qt.Checked if str(value.id) in included else Qt.Unchecked)
                    self.values.addItem(item)
        finally:self._syncing=False

    def _required_changed(self,checked):
        if self._syncing:return
        index=self._selected_index()
        if index<0:return
        token=self.tokens[index]; slot=self.slots.get(token.reference_id)
        if slot is not None:self.slots[slot.id]=replace(slot,required=bool(checked)); self._refresh(select=index)

    def _values_changed(self,_item):
        if self._syncing:return
        index=self._selected_index()
        if index<0:return
        token=self.tokens[index]; slot=self.slots.get(token.reference_id)
        if slot is None:return
        included=[]; excluded=[]
        for row in range(self.values.count()):
            item=self.values.item(row); target=included if item.checkState()==Qt.Checked else excluded
            target.append(str(item.data(Qt.UserRole)))
        if not included:
            QMessageBox.warning(self,"Keep one value","A typed selector must retain at least one reviewed value.")
            self._token_selected(); return
        self.slots[slot.id]=replace(
            slot,included_value_ids=tuple(included),excluded_value_ids=tuple(excluded),
            held_value_ids=tuple(value for value in slot.held_value_ids if value in {*included,*excluded}),
        )
        self._refresh(select=index)

    def _add_forbidden(self):
        if not self.slots:return
        dialog=ForbiddenCombinationDialog(tuple(self.slots.values()),self.definitions,self)
        if dialog.exec()!=QDialog.Accepted:return
        combination=dialog.combination
        if combination and combination not in self.forbidden:self.forbidden.append(combination)
        self._refresh(select=self._selected_index())

    def _clear_forbidden(self):
        self.forbidden=[]; self._refresh(select=self._selected_index())

    def _refresh(self,*,select=None):
        selected=self._selected_index() if select is None else select
        self.token_tree.clear()
        for index,token in enumerate(self.tokens):
            if token.kind=="anchor":
                anchor=self.anchors.get(token.reference_id)
                label=anchor.label if anchor else "Unavailable Tag"
                role=anchor.role.replace("_"," ").title() if anchor else "Held"
                identity=token.reference_id
            elif token.kind=="modifier_slot":
                slot=self.slots.get(token.reference_id)
                label="{"+slot.placeholder+"}" if slot else "{unavailable}"
                role=("Required" if slot and slot.required else "Optional subset")
                identity=f"{token.reference_id} / {slot.family_id if slot else 'unavailable'}"
            else:
                label=repr(token.text); identity="visible separator"; role="Formatting only"
            item=QTreeWidgetItem((str(index+1),label,identity,role)); item.setData(0,Qt.UserRole,index)
            self.token_tree.addTopLevelItem(item)
        if 0<=selected<self.token_tree.topLevelItemCount():
            self.token_tree.setCurrentItem(self.token_tree.topLevelItem(selected))
        if not self.slots:
            self.constraint_status.setText(
                "Add a variable detail on Build Tag Pattern before excluding a combination."
            )
        elif self.forbidden:
            self.constraint_status.setText(f"Exact combinations excluded: {len(self.forbidden):,}.")
        else:
            self.constraint_status.setText("No exact combinations excluded.")
        self.add_forbidden.setEnabled(bool(self.slots))
        self.clear_forbidden.setEnabled(bool(self.forbidden))
        self._validate_status(); self._token_selected()

    def draft(self):
        constraints={"forbidden_combinations":tuple(dict(item) for item in self.forbidden)}
        if self.record:constraints.update({
            key:value for key,value in self.record.constraints.items()
            if key!="forbidden_combinations"
        })
        return TagTemplateDraft(
            tuple(self.anchors.values()),tuple(self.slots.values()),tuple(self.tokens),
            self.record.id if self.record else "",
            self.record.version if self.record else 0,
            constraints,
            dict(self.record.destination_policy) if self.record else {},
            self.max_per_run.value(),
        )

    def _validate_status(self):
        try:
            draft=self.draft(); draft.validate()
            self.status.setText(
                f"Ready to preview: {draft.display_template}. Preview changes nothing."
            )
            self.preview_button.setEnabled(True)
        except ValueError as exc:
            self.status.setText(f"Held: {exc}"); self.preview_button.setEnabled(False)

    def _accept_preview(self):
        try:self.draft().validate()
        except ValueError as exc:
            QMessageBox.warning(self,"Template needs review",str(exc)); return
        self.accept()


__all__=["ForbiddenCombinationDialog","TagTemplateBuilderDialog"]
