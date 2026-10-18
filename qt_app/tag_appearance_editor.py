from __future__ import annotations

import json
from copy import deepcopy
from pathlib import Path

from PySide6.QtCore import Qt,Signal
from PySide6.QtGui import QColor,QPalette
from PySide6.QtWidgets import (
    QApplication,QCheckBox,QColorDialog,QComboBox,QDialog,QDialogButtonBox,QFileDialog,
    QFormLayout,QGroupBox,QHBoxLayout,QLabel,QLineEdit,QListWidget,QMessageBox,QPushButton,
    QVBoxLayout,QWidget,
)

from core import (
    CONTENT_RATINGS,MINIMUM_TEXT_CONTRAST,TAG_APPEARANCE_CHANNELS,
    TagAppearanceError,TagAppearanceValidationError,effective_tag_sensitivity,
    new_sensitivity_rule,normalize_appearance_color,set_style_value,
    validate_tag_appearance_payload,
)
from .confidence import STATUS_COLORS
from .components import BoundedSearchableComboBox,bind_status_footer
from .tag_editor import (
    TagChip,category_details,default_tag_appearance_resolver,
    default_tag_appearance_store,refresh_all_tag_appearance_chips,
)


CHANNEL_LABELS={
    "accent":"Left accent",
    "border":"Inner border",
    "background":"Background",
    "text":"Text",
}


class AppearanceColorControl(QWidget):
    changed=Signal(str,object)

    def __init__(self,channel: str,parent=None):
        super().__init__(parent); self.channel=channel; self._loading=False
        layout=QHBoxLayout(self); layout.setContentsMargins(0,0,0,0)
        self.mode=QComboBox(); self.mode.addItem("Use inherited/theme color","inherit")
        self.mode.addItem("Choose color","color")
        if channel=="text":self.mode.addItem("Safe automatic text","auto")
        self.mode.setAccessibleName(f"{CHANNEL_LABELS[channel]} color source")
        self.swatch=QLabel(); self.swatch.setFixedSize(24,24)
        self.swatch.setAccessibleName(f"{CHANNEL_LABELS[channel]} swatch")
        self.color=QLineEdit(); self.color.setPlaceholderText("#RRGGBB"); self.color.setMaxLength(7)
        self.color.setAccessibleName(f"{CHANNEL_LABELS[channel]} exact color")
        self.choose=QPushButton("Choose…"); self.choose.setProperty("compact",True)
        self.choose.setAccessibleName(f"Choose {CHANNEL_LABELS[channel].lower()} color")
        layout.addWidget(self.mode); layout.addWidget(self.swatch); layout.addWidget(self.color,1); layout.addWidget(self.choose)
        self.mode.activated.connect(self._mode_changed)
        self.color.editingFinished.connect(self._color_finished)
        self.color.textChanged.connect(self._refresh_swatch)
        self.choose.clicked.connect(self._choose)

    def value(self):
        mode=str(self.mode.currentData() or "inherit")
        if mode=="inherit":return None
        if mode=="auto":return "auto"
        return normalize_appearance_color(self.color.text())

    def set_value(self,value):
        self._loading=True
        text=str(value or "")
        if self.channel=="text" and text.casefold()=="auto":
            self.mode.setCurrentIndex(self.mode.findData("auto"))
        elif text:
            self.mode.setCurrentIndex(self.mode.findData("color")); self.color.setText(text)
        else:
            self.mode.setCurrentIndex(self.mode.findData("inherit")); self.color.clear()
        self._update_enabled(); self._refresh_swatch(); self._loading=False

    def _update_enabled(self):
        choosing=self.mode.currentData()=="color"
        self.color.setEnabled(choosing); self.choose.setEnabled(choosing)

    def _mode_changed(self,*_):
        self._update_enabled()
        if self._loading:return
        if self.mode.currentData()=="color" and not self.color.text().strip():
            self.color.setText("#808080")
        self._emit()

    def _color_finished(self):
        if not self._loading and self.mode.currentData()=="color":self._emit()

    def _emit(self):
        try:value=self.value()
        except TagAppearanceValidationError:
            value=self.color.text().strip()
        self.changed.emit(self.channel,value)

    def _choose(self):
        initial=QColor(self.color.text())
        if not initial.isValid():initial=QColor("#808080")
        chosen=QColorDialog.getColor(initial,self,f"Choose {CHANNEL_LABELS[self.channel]} Color")
        if chosen.isValid():
            self.color.setText(chosen.name().upper()); self._emit()

    def _refresh_swatch(self,*_):
        color=QColor(self.color.text())
        if self.mode.currentData()=="color" and color.isValid():
            self.swatch.setStyleSheet(f"background:{color.name()}; border:1px solid palette(mid); border-radius:4px;")
            text=f"{CHANNEL_LABELS[self.channel]} {color.name().upper()}"
        elif self.mode.currentData()=="auto":
            self.swatch.setStyleSheet("background:linear-gradient(x1:0,y1:0,x2:1,y2:0,stop:0 #000000,stop:1 #FFFFFF); border:1px solid palette(mid); border-radius:4px;")
            text=f"{CHANNEL_LABELS[self.channel]} uses safe automatic text"
        else:
            self.swatch.setStyleSheet("background:transparent; border:1px dashed palette(mid); border-radius:4px;")
            text=f"{CHANNEL_LABELS[self.channel]} inherits"
        self.swatch.setAccessibleName(text); self.swatch.setToolTip(text)


class TagAppearanceEditor(QWidget):
    """Shared TAGUI-02 editor with an explicit appearance-authority boundary."""

    appearance_changed=Signal()
    manage_global_requested=Signal(str)

    def __init__(
        self,tags,categories,*,tag_id: str="",category_id: str="",
        allowed_scopes=("tag","category","rules"),show_global_tools=True,parent=None,
    ):
        super().__init__(parent); self.tags=tags; self.categories=categories
        self.store=default_tag_appearance_store(); self.resolver=default_tag_appearance_resolver()
        self.tag_id=str(tag_id); self.category_id=str(category_id)
        self.allowed_scopes=tuple(
            scope for scope in dict.fromkeys(str(value) for value in allowed_scopes)
            if scope in {"tag","category","rules"}
        )
        self.show_global_tools=bool(show_global_tools)
        self._loading=False; self._pending_candidate=None; self._undo=[]; self._redo=[]
        layout=QVBoxLayout(self); layout.setContentsMargins(8,8,8,8)
        intro=QLabel("Chip colors are presentation only. Category membership, sensitivity, confidence, review state, Image Knowledge, Metadata, and raw evidence remain unchanged.")
        intro.setWordWrap(True); intro.setObjectName("mutedLabel"); intro.setAccessibleName("Tag appearance data authority"); layout.addWidget(intro)
        scope_row=QHBoxLayout()
        self.scope_picker_host=QWidget(); scope_picker=QHBoxLayout(self.scope_picker_host); scope_picker.setContentsMargins(0,0,0,0)
        scope_picker.addWidget(QLabel("Edit colors for"))
        self.scope=QComboBox(); self.scope.setAccessibleName("Tag chip color scope"); self.scope.activated.connect(self._scope_changed); scope_picker.addWidget(self.scope,1)
        scope_row.addWidget(self.scope_picker_host,1)
        self.scope_spacer=QWidget(); scope_row.addWidget(self.scope_spacer,1)
        self.undo_button=QPushButton("Undo"); self.redo_button=QPushButton("Redo")
        self.undo_button.setProperty("compact",True); self.redo_button.setProperty("compact",True)
        self.undo_button.clicked.connect(self.undo); self.redo_button.clicked.connect(self.redo)
        scope_row.addWidget(self.undo_button); scope_row.addWidget(self.redo_button); layout.addLayout(scope_row)

        self.global_handoff=QWidget(); handoff_layout=QHBoxLayout(self.global_handoff); handoff_layout.setContentsMargins(0,0,0,0)
        handoff_text=QLabel("Inherited category defaults and sensitivity rules are managed once for the whole Tag Encyclopedia.")
        handoff_text.setWordWrap(True); handoff_text.setObjectName("mutedLabel")
        self.manage_global_button=QPushButton("Manage Shared Colors..."); self.manage_global_button.setProperty("compact",True)
        self.manage_global_button.clicked.connect(lambda:self.manage_global_requested.emit(self.category_id))
        handoff_layout.addWidget(handoff_text,1); handoff_layout.addWidget(self.manage_global_button); layout.addWidget(self.global_handoff)

        self.preview_group=QGroupBox("Live chip preview"); preview_layout=QVBoxLayout(self.preview_group)
        state_row=QHBoxLayout(); state_row.addWidget(QLabel("Preview state"))
        self.preview_state=QComboBox()
        for label,key in (
            ("Ordinary","ordinary"),("Selected","selected"),("Keyboard focus","focus"),
            ("Workflow needs review","workflow"),("High confidence","confidence"),
        ):self.preview_state.addItem(label,key)
        self.preview_state.activated.connect(self._refresh_preview); state_row.addWidget(self.preview_state,1); preview_layout.addLayout(state_row)
        self.preview_host=QWidget(); self.preview_layout=QHBoxLayout(self.preview_host); self.preview_layout.setContentsMargins(0,4,0,4); self.preview_layout.addStretch(1); preview_layout.addWidget(self.preview_host)
        self.preview_chip=None
        self.contrast=QLabel(); self.contrast.setWordWrap(True); self.contrast.setAccessibleName("Tag chip text contrast"); preview_layout.addWidget(self.contrast)
        layout.addWidget(self.preview_group)

        self.rule_group=QGroupBox("Sensitivity color rules"); rule_layout=QVBoxLayout(self.rule_group)
        self.rule_empty=QLabel(
            "No sensitivity color rules are configured. Add a rule only when a "
            "content rating needs colors that differ from the tag and category defaults."
        )
        self.rule_empty.setWordWrap(True); self.rule_empty.setObjectName("mutedLabel"); self.rule_empty.setAccessibleName("No sensitivity color rules")
        rule_layout.addWidget(self.rule_empty)
        self.rule_list=QListWidget(); self.rule_list.setAccessibleName("Ordered conditional appearance rules"); self.rule_list.setMaximumHeight(150); self.rule_list.currentRowChanged.connect(self._rule_selected); rule_layout.addWidget(self.rule_list)
        rule_actions=QHBoxLayout(); self.add_rule=QPushButton("Add Rule"); self.remove_rule=QPushButton("Remove Rule"); self.rule_up=QPushButton("Move Up"); self.rule_down=QPushButton("Move Down")
        for button in (self.add_rule,self.remove_rule,self.rule_up,self.rule_down):button.setProperty("compact",True); rule_actions.addWidget(button)
        rule_actions.addStretch(1); rule_layout.addLayout(rule_actions)
        self.rule_condition_host=QWidget(); condition_form=QFormLayout(self.rule_condition_host); condition_form.setContentsMargins(0,0,0,0)
        self.rule_enabled=QCheckBox("Enabled"); self.rule_enabled.toggled.connect(self._rule_enabled_changed)
        self.rule_sensitivity=QComboBox()
        for rating in CONTENT_RATINGS:self.rule_sensitivity.addItem(rating.title(),rating)
        self.rule_sensitivity.activated.connect(self._rule_rating_changed)
        condition_form.addRow("Rule state",self.rule_enabled); condition_form.addRow("Sensitivity is",self.rule_sensitivity); rule_layout.addWidget(self.rule_condition_host)
        self.add_rule.clicked.connect(self._add_rule); self.remove_rule.clicked.connect(self._remove_rule); self.rule_up.clicked.connect(lambda:self._move_rule(-1)); self.rule_down.clicked.connect(lambda:self._move_rule(1))
        layout.addWidget(self.rule_group)

        self.colors_group=QGroupBox("Color channels"); color_form=QFormLayout(self.colors_group); self.controls={}
        for channel in TAG_APPEARANCE_CHANNELS:
            control=AppearanceColorControl(channel); control.changed.connect(self._color_changed); self.controls[channel]=control
            color_form.addRow(CHANNEL_LABELS[channel],control)
        layout.addWidget(self.colors_group)
        self.affected=QLabel(); self.affected.setWordWrap(True); self.affected.setObjectName("mutedLabel"); self.affected.setAccessibleName("Affected SDXL Tag preview"); layout.addWidget(self.affected)
        self.sources=QLabel(); self.sources.setWordWrap(True); self.sources.setObjectName("mutedLabel"); self.sources.setAccessibleName("Effective tag appearance sources"); layout.addWidget(self.sources)
        self.validation_status=QLabel(); self.validation_status.setWordWrap(True); self.validation_status.setObjectName("errorLabel"); self.validation_status.setAccessibleName("Tag appearance field validation"); self.validation_status.hide(); layout.addWidget(self.validation_status)

        self.global_tools_host=QWidget(); global_tools=QVBoxLayout(self.global_tools_host); global_tools.setContentsMargins(0,0,0,0)
        stale_row=QHBoxLayout(); self.stale=QLabel(); self.stale.setWordWrap(True); self.stale.setObjectName("mutedLabel"); self.remove_stale=QPushButton("Remove Stale References"); self.remove_stale.setProperty("compact",True); self.remove_stale.clicked.connect(self._remove_stale)
        stale_row.addWidget(self.stale,1); stale_row.addWidget(self.remove_stale); global_tools.addLayout(stale_row)
        actions=QHBoxLayout(); self.import_button=QPushButton("Import Profile…"); self.export_button=QPushButton("Export Profile…"); self.retry=QPushButton("Retry Save",self)
        for button in (self.import_button,self.export_button,self.retry):button.setProperty("compact",True)
        self.status=QLabel("Appearance changes save automatically.",self); self.status.setWordWrap(True); self.status.setObjectName("mutedLabel"); self.status.setAccessibleName("Tag appearance save status")
        actions.addWidget(self.import_button); actions.addWidget(self.export_button); actions.addStretch(1); global_tools.addLayout(actions)
        bind_status_footer(self,self.status,self.retry,title="Tag appearance",follow_visibility=True)
        self.import_button.clicked.connect(self._import_profile); self.export_button.clicked.connect(self._export_profile); self.retry.clicked.connect(self._retry_save)
        layout.addWidget(self.global_tools_host)
        layout.addStretch(1)
        self.refresh_context(tag_id=self.tag_id,category_id=self.category_id)

    def refresh_context(self,*,tag_id: str="",category_id: str=""):
        self.tag_id=str(tag_id or "")
        record=self.tags.tags.get(self.tag_id) if self.tag_id else None
        self.category_id=str(category_id or getattr(record,"category","") or "")
        self._loading=True; current=str(self.scope.currentData() or "")
        self.scope.clear()
        if record is not None and "tag" in self.allowed_scopes:self.scope.addItem(f"This tag — {record.display}","tag")
        category=self.categories.get(self.category_id) if self.category_id else None
        if category is not None and "category" in self.allowed_scopes:self.scope.addItem(f"Category defaults — {self.categories.path_label(category.id)}","category")
        if "rules" in self.allowed_scopes:self.scope.addItem("Sensitivity color rules","rules")
        wanted=current if self.scope.findData(current)>=0 else next((scope for scope in self.allowed_scopes if self.scope.findData(scope)>=0),"")
        self.scope.setCurrentIndex(max(0,self.scope.findData(wanted))); self._loading=False
        self.scope_picker_host.setVisible(self.scope.count()>1)
        self.scope_spacer.setVisible(self.scope.count()<=1)
        self.global_handoff.setVisible(self.allowed_scopes==("tag",) and record is not None)
        self.global_tools_host.show()
        self.stale.setVisible(self.show_global_tools)
        self.import_button.setVisible(self.show_global_tools)
        self.export_button.setVisible(self.show_global_tools)
        self._load_scope()

    def set_scope(self,scope):
        index=self.scope.findData(str(scope or ""))
        if index>=0:
            self.scope.setCurrentIndex(index); self._scope_changed()

    def _scope_changed(self,*_):
        if not self._loading:self._load_scope()

    def _current_rule(self):
        rules=self.store.payload.get("rules",[])
        row=self.rule_list.currentRow()
        return rules[row] if 0<=row<len(rules) else None

    def _current_style(self):
        scope=str(self.scope.currentData() or "")
        if scope=="tag":return self.store.payload.get("tags",{}).get(self.tag_id,{})
        if scope=="category":return self.store.payload.get("categories",{}).get(self.category_id,{})
        rule=self._current_rule(); return rule.get("style",{}) if isinstance(rule,dict) else {}

    def _load_scope(self):
        self._loading=True; scope=str(self.scope.currentData() or "")
        available=scope in {"tag","category","rules"}
        self.preview_group.setVisible(available)
        self.rule_group.setVisible(scope=="rules")
        if scope=="rules":self._reload_rules()
        else:self.colors_group.setVisible(available)
        style=self._current_style()
        for channel,control in self.controls.items():control.set_value(style.get(channel) if isinstance(style,dict) else None)
        self._loading=False; self._update_buttons(); self._refresh_preview(); self._refresh_stale()

    def _reload_rules(self,preserve_id=""):
        rules=self.store.payload.get("rules",[]); current=self._current_rule()
        wanted=preserve_id or (str(current.get("id") or "") if isinstance(current,dict) else "")
        self.rule_list.clear(); selected=-1
        for index,rule in enumerate(rules):
            rating=str(rule.get("condition",{}).get("value") or "unknown").title()
            label=f"{index+1}. Sensitivity is {rating}"+("" if rule.get("enabled",True) else " — Disabled")
            self.rule_list.addItem(label)
            if str(rule.get("id") or "")==wanted:selected=index
        if rules:self.rule_list.setCurrentRow(selected if selected>=0 else 0)
        self._load_rule_fields()

    def _rule_selected(self,*_):
        if not self._loading:
            self._loading=True; self._load_rule_fields()
            style=self._current_style()
            for channel,control in self.controls.items():control.set_value(style.get(channel) if isinstance(style,dict) else None)
            self._loading=False; self._update_buttons(); self._refresh_preview()

    def _load_rule_fields(self):
        rule=self._current_rule(); available=isinstance(rule,dict)
        has_rules=bool(self.store.payload.get("rules",[]))
        self.rule_empty.setVisible(not has_rules)
        self.rule_list.setVisible(has_rules)
        self.remove_rule.setVisible(has_rules)
        self.rule_up.setVisible(has_rules)
        self.rule_down.setVisible(has_rules)
        self.rule_condition_host.setVisible(available)
        self.colors_group.setVisible(available)
        self.rule_enabled.setEnabled(available); self.rule_sensitivity.setEnabled(available)
        if available:
            self.rule_enabled.setChecked(bool(rule.get("enabled",True)))
            rating=str(rule.get("condition",{}).get("value") or "")
            self.rule_sensitivity.setCurrentIndex(max(0,self.rule_sensitivity.findData(rating)))
        else:
            self.rule_enabled.setChecked(False)

    def _color_changed(self,channel,value):
        if self._loading:return
        try:
            normalized=normalize_appearance_color(value,allow_auto=channel=="text") if value else None
        except TagAppearanceValidationError as error:
            self._set_validation(str(error)); return
        scope=str(self.scope.currentData() or "")
        if scope not in self.allowed_scopes:
            self._set_validation("This color scope is not available here."); return
        if scope=="tag":candidate=set_style_value(self.store.snapshot(),"tags",self.tag_id,channel,normalized)
        elif scope=="category":candidate=set_style_value(self.store.snapshot(),"categories",self.category_id,channel,normalized)
        else:
            candidate=self.store.snapshot(); row=self.rule_list.currentRow(); rules=candidate.get("rules",[])
            if not 0<=row<len(rules):self._set_validation("Add or select a conditional rule first."); return
            style=rules[row].setdefault("style",{})
            if normalized:style[channel]=normalized
            else:style.pop(channel,None)
        self._commit(candidate,"Saved chip appearance automatically.")

    def _commit(self,candidate,message,*,history=True):
        previous=self.store.snapshot(); revision=self.store.revision
        try:self.store.save(candidate,expected_revision=revision)
        except (OSError,ValueError,TagAppearanceError,TagAppearanceValidationError) as error:
            self._pending_candidate=deepcopy(candidate); self._set_status(f"Not saved — {error}",error=True); return False
        if history:
            self._undo.append(previous); self._undo=self._undo[-50:]; self._redo.clear()
        self._pending_candidate=None; self.resolver.clear(); refresh_all_tag_appearance_chips(); self.appearance_changed.emit()
        self._set_status(message); self._load_scope(); return True

    def undo(self):
        if not self._undo:return
        target=self._undo.pop(); current=self.store.snapshot()
        if self._commit(target,"Undid and saved the previous appearance.",history=False):self._redo.append(current)
        else:self._undo.append(target)

    def redo(self):
        if not self._redo:return
        target=self._redo.pop(); current=self.store.snapshot()
        if self._commit(target,"Redid and saved the appearance.",history=False):self._undo.append(current)
        else:self._redo.append(target)

    def _add_rule(self):
        used={str(rule.get("condition",{}).get("value") or "") for rule in self.store.payload.get("rules",[])}
        rating=next((value for value in CONTENT_RATINGS if value not in used),"")
        if not rating:self._set_validation("A rule already exists for every supported sensitivity rating."); return
        candidate=self.store.snapshot(); rule=new_sensitivity_rule(rating); candidate.setdefault("rules",[]).append(rule)
        if self._commit(candidate,"Added and saved a conditional appearance rule."):
            self._reload_rules(str(rule["id"]))

    def _remove_rule(self):
        row=self.rule_list.currentRow(); candidate=self.store.snapshot(); rules=candidate.get("rules",[])
        if not 0<=row<len(rules):return
        rules.pop(row); self._commit(candidate,"Removed and saved the conditional appearance rule.")

    def _move_rule(self,offset):
        row=self.rule_list.currentRow(); candidate=self.store.snapshot(); rules=candidate.get("rules",[]); target=row+offset
        if not 0<=row<len(rules) or not 0<=target<len(rules):return
        rule_id=str(rules[row].get("id") or ""); rules[row],rules[target]=rules[target],rules[row]
        if self._commit(candidate,"Reordered and saved conditional appearance rules."):self._reload_rules(rule_id)

    def _rule_enabled_changed(self,enabled):
        if self._loading:return
        row=self.rule_list.currentRow(); candidate=self.store.snapshot(); rules=candidate.get("rules",[])
        if 0<=row<len(rules):
            rules[row]["enabled"]=bool(enabled); self._commit(candidate,"Saved the conditional rule state.")

    def _rule_rating_changed(self,*_):
        if self._loading:return
        row=self.rule_list.currentRow(); candidate=self.store.snapshot(); rules=candidate.get("rules",[])
        if not 0<=row<len(rules):return
        rating=str(self.rule_sensitivity.currentData() or "")
        if any(index!=row and rule.get("condition",{}).get("value")==rating for index,rule in enumerate(rules)):
            self._set_validation(f"A rule for {rating.title()} sensitivity already exists."); self._load_scope(); return
        rules[row]["condition"]={"kind":"sensitivity","value":rating}
        self._commit(candidate,"Saved the conditional rule condition.")

    def _preview_record(self):
        record=self.tags.tags.get(self.tag_id) if self.tag_id else None
        if record is not None:return record
        records=[item for item in self.tags.tags.values() if not self.category_id or item.category==self.category_id]
        return sorted(records,key=lambda item:item.display.casefold())[0] if records else None

    def _refresh_preview(self,*_):
        if self.preview_chip is not None:
            self.preview_layout.removeWidget(self.preview_chip)
            self.preview_chip.hide(); self.preview_chip.setEnabled(False)
            self.preview_chip.deleteLater()
        record=self._preview_record()
        if record is not None:
            label,color,tooltip=category_details(record.display,self.tags,self.categories)
            self.preview_chip=TagChip(record.display,category_label=label,color=color,tooltip=tooltip,editable=False,stable_id=record.id,category_id=record.category,sensitivity=effective_tag_sensitivity(record,self.categories)[0])
            self.preview_chip.set_knowledge_context(self.tags,self.categories)
        else:
            label=self.categories.path_label(self.category_id) if self.category_id else "Uncategorized"
            color=self.categories.effective_color(self.category_id)[0] if self.category_id else ""
            self.preview_chip=TagChip("Preview SDXL Tag",category_label=label,color=color,tooltip="Appearance preview; no tag fact is created.",editable=False,category_id=self.category_id)
        self.preview_chip.setFocusPolicy(Qt.NoFocus)
        state=str(self.preview_state.currentData() or "ordinary")
        self.preview_chip.set_selected(state=="selected")
        if state=="focus":self.preview_chip.set_state_indicator("Keyboard focus preview",self.palette().color(QPalette.Highlight).name(),"dashed")
        elif state=="workflow":self.preview_chip.set_state_indicator("Needs review",STATUS_COLORS["orange"],"dashed")
        elif state=="confidence":self.preview_chip.set_state_indicator("High confidence",STATUS_COLORS["green"],"solid")
        else:self.preview_chip.set_state_indicator()
        self.preview_layout.insertWidget(0,self.preview_chip,0,Qt.AlignLeft)
        self._refresh_explanation(record)

    def _refresh_explanation(self,record):
        if record is not None:
            category_id=record.category; sensitivity=effective_tag_sensitivity(record,self.categories)[0]
            semantic=self.categories.effective_color(category_id)[0]
        else:
            category_id=self.category_id; sensitivity=""; semantic=self.categories.effective_color(category_id)[0] if category_id else ""
        palette=self.palette(); theme={"border":palette.color(QPalette.Midlight).name(),"background":palette.color(QPalette.Base).name(),"text":palette.color(QPalette.Text).name()}
        resolved=self.resolver.resolve(tag_id=getattr(record,"id",""),category_id=category_id,semantic_accent=semantic or theme["border"],sensitivity=sensitivity,theme=theme,theme_key="editor")
        warning=resolved.contrast_ratio<MINIMUM_TEXT_CONTRAST
        self.contrast.setText(f"Text contrast {resolved.contrast_ratio:.2f}:1"+(" — warning: below the 4.5:1 readable-text target. Choose a safer color or Safe automatic text." if warning else " — meets the 4.5:1 readable-text target."))
        self.contrast.setObjectName("errorLabel" if warning else "mutedLabel"); self.contrast.style().unpolish(self.contrast); self.contrast.style().polish(self.contrast)
        self.sources.setText("Effective sources — "+", ".join(f"{CHANNEL_LABELS[channel]}: {resolved.channel(channel).source}" for channel in TAG_APPEARANCE_CHANNELS))
        scope=str(self.scope.currentData() or "")
        if scope=="tag":count=1 if record is not None else 0; explanation=f"This stable SDXL Tag override affects {count:,} tag."
        elif scope=="category":
            count=sum(item.category==self.category_id for item in self.tags.tags.values()); explanation=f"This category preference currently affects {count:,} direct SDXL Tag{'s' if count!=1 else ''}."
        else:
            rule=self._current_rule(); rating=str(rule.get("condition",{}).get("value") or "") if isinstance(rule,dict) else ""
            count=sum(effective_tag_sensitivity(item,self.categories)[0]==rating for item in self.tags.tags.values()) if rating else 0
            order=self.rule_list.currentRow()+1 if rule is not None else 0
            explanation=(f"Rule {order:,} currently affects {count:,} SDXL Tag{'s' if count!=1 else ''} with exact reviewed or inherited {rating.title()} sensitivity. Later matching rules override earlier channels." if rule is not None else "Add a bounded sensitivity rule to preview its affected count.")
        self.affected.setText(explanation)

    def _refresh_stale(self):
        stale=self.store.stale_references(tag_ids=set(self.tags.tags),category_ids=set(self.categories.categories))
        self.remove_stale.setVisible(self.show_global_tools and bool(stale))
        self.stale.setVisible(self.show_global_tools)
        self.stale.setText(f"{len(stale):,} missing or deleted stable-ID appearance reference{'s' if len(stale)!=1 else ''} need review." if stale else "No stale tag or category appearance references.")
        self._update_buttons()

    def _remove_stale(self):
        stale=self.store.stale_references(tag_ids=set(self.tags.tags),category_ids=set(self.categories.categories))
        if not stale:return
        candidate=self.store.snapshot()
        for kind,stable_id in stale:candidate["tags" if kind=="tag" else "categories"].pop(stable_id,None)
        self._commit(candidate,f"Removed and saved {len(stale):,} stale appearance reference{'s' if len(stale)!=1 else ''}.")

    def _update_buttons(self):
        row=self.rule_list.currentRow(); count=len(self.store.payload.get("rules",[]))
        self.undo_button.setEnabled(bool(self._undo)); self.redo_button.setEnabled(bool(self._redo))
        self.retry.setVisible(self._pending_candidate is not None)
        self.remove_rule.setEnabled(0<=row<count); self.rule_up.setEnabled(row>0); self.rule_down.setEnabled(0<=row<count-1)

    def _set_status(self,text,error=False):
        self.validation_status.hide(); self.status.setText(text); self.status.show(); self.status.setObjectName("errorLabel" if error else "mutedLabel"); self.status.style().unpolish(self.status); self.status.style().polish(self.status); self._update_buttons()

    def _set_validation(self,text):
        self.status.setText(text); self.status.hide(); self.validation_status.setText(text); self.validation_status.show(); self._update_buttons()

    def _retry_save(self):
        if self._pending_candidate is not None:self._commit(self._pending_candidate,"Saved chip appearance after retry.")

    def _export_profile(self):
        path,_=QFileDialog.getSaveFileName(self,"Export Tag Appearance Profile","tag-appearance.json","JSON files (*.json)")
        if not path:return
        try:self.store.export_to(Path(path))
        except (OSError,ValueError,TagAppearanceError) as error:self._set_status(f"Export failed — {error}",error=True); return
        self._set_status(f"Exported the presentation profile to {Path(path).name}.")

    def _import_profile(self):
        path,_=QFileDialog.getOpenFileName(self,"Import Tag Appearance Profile","","JSON files (*.json)")
        if not path:return
        try:
            payload=json.loads(Path(path).read_text(encoding="utf-8")); validate_tag_appearance_payload(payload)
        except (OSError,UnicodeError,json.JSONDecodeError,TagAppearanceValidationError) as error:
            self._set_status(f"Import preview failed — {error}",error=True); return
        summary=f"{len(payload.get('tags',{})):,} tag overrides, {len(payload.get('categories',{})):,} category preferences, and {len(payload.get('rules',[])):,} conditional rules"
        answer=QMessageBox.question(self,"Import tag appearance?",f"Replace the current presentation profile with {summary} from {Path(path).name}?\n\nThis changes presentation only. No System Knowledge, Image Knowledge, captions, Metadata, or images will change.")
        if answer!=QMessageBox.Yes:return
        self._commit(payload,"Imported and saved the tag appearance profile.")


class TagColorWorkspaceDialog(QDialog):
    """Canonical Tag Encyclopedia workspace for shared appearance policy."""

    def __init__(self,tags,categories,*,category_id="",initial_scope="category",parent=None):
        super().__init__(parent); self.tags=tags; self.categories=categories
        self.setWindowTitle("Tag Encyclopedia Colors")
        self.resize(960,760)
        layout=QVBoxLayout(self)
        intro=QLabel(
            "Manage shared chip presentation here. Category defaults, sensitivity rules, "
            "profile transfer, and cleanup apply across the Tag Encyclopedia; exact-tag "
            "overrides remain in each tag's Colors tab."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel"); intro.setAccessibleName("Shared tag color authority")
        layout.addWidget(intro)
        category_row=QHBoxLayout(); category_row.addWidget(QLabel("Category context"))
        self.category=BoundedSearchableComboBox(placeholder="Search categories...",max_visible=14)
        self.category.setAccessibleName("Category color context")
        for record in sorted(self.categories.categories.values(),key=lambda item:self.categories.path_label(item.id).casefold()):
            self.category.addItem(self.categories.path_label(record.id),record.id)
        wanted=self.category.findData(str(category_id or ""))
        self.category.setCurrentIndex(wanted if wanted>=0 else (0 if self.category.count() else -1))
        self.category.currentIndexChanged.connect(self._category_changed); category_row.addWidget(self.category,1); layout.addLayout(category_row)
        self.editor=TagAppearanceEditor(
            tags,categories,category_id=str(self.category.currentData() or ""),
            allowed_scopes=("category","rules"),show_global_tools=True,parent=self,
        )
        self.editor.set_scope(initial_scope); layout.addWidget(self.editor,1)
        buttons=QDialogButtonBox(QDialogButtonBox.Close); buttons.rejected.connect(self.accept); layout.addWidget(buttons)

    def _category_changed(self,*_):
        current=str(self.editor.scope.currentData() or "category")
        self.editor.refresh_context(category_id=str(self.category.currentData() or ""))
        self.editor.set_scope(current)
