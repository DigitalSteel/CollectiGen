from __future__ import annotations

from PySide6.QtCore import Qt,Signal
from PySide6.QtGui import QKeySequence,QShortcut
from PySide6.QtWidgets import (
    QAbstractItemView,QCheckBox,QHBoxLayout,QLabel,QLineEdit,QMessageBox,
    QMenu,QPushButton,QTreeWidget,QTreeWidgetItem,QVBoxLayout,QWidget,
)
from .status_notifications import FooterMessageBox as QMessageBox

from .accessibility import announce_accessibly

from core import DownstreamUpdateStore,ORDER_GROUP_LABELS,ORDER_GROUPS,OrganizationProfile,TagOrderWorkspaceSession,affected_order_projection_paths


class OrderTree(QTreeWidget):
    relative_drop=Signal(object,object,bool)

    def __init__(self,parent=None):
        super().__init__(parent); self.setHeaderLabels(("Caption sequence","Context","Position source"))
        self.setSelectionMode(QAbstractItemView.ExtendedSelection); self.setDragEnabled(True); self.setAcceptDrops(True)
        self.setDropIndicatorShown(True); self.setDragDropMode(QAbstractItemView.InternalMove)

    def dropEvent(self,event):
        source=self.currentItem(); target=self.itemAt(event.position().toPoint())
        event.ignore()
        if source is not None and target is not None and source is not target:
            self.relative_drop.emit(source,target,self.dropIndicatorPosition()==QAbstractItemView.BelowItem)


class TagOrderWorkspace(QWidget):
    """Searchable staged editor for effective System Knowledge caption order."""

    order_saved=Signal(); downstream_updates_changed=Signal()
    MATERIALIZE_LIMIT=200
    FETCH_BATCH=100

    def __init__(self,tags,categories,*,settings=None,image_index_provider=None,session=None,embedded=False,defer_refresh=False,parent=None):
        super().__init__(parent); self.tags=tags; self.categories=categories; self.settings=settings; self.image_index_provider=image_index_provider; self.embedded=bool(embedded)
        if session is None:
            TagOrderWorkspaceSession.recover_interrupted(tags.path.parent)
            categories.load(); tags.load(); tags.apply_category_ordering(categories); session=TagOrderWorkspaceSession(tags,categories)
        self.session=session
        self._build()
        if not defer_refresh:self.refresh()

    def _build(self):
        root=QVBoxLayout(self); self.intro=QLabel("Arrange categories and individual SDXL Tags by their neighbors. Changes stay staged until Save Order; captions, Metadata, and image files are never rewritten here.")
        self.intro.setWordWrap(True); self.intro.setObjectName("mutedLabel"); root.addWidget(self.intro)
        filters=QHBoxLayout(); self.search=QLineEdit(); self.search.setPlaceholderText("Search SDXL Tags, categories, and paths…"); self.overrides=QCheckBox("Overrides Only")
        self.search.setAccessibleName("Search caption sequence"); self.search.setAccessibleDescription("Filter output groups, categories, category paths, and SDXL Tags without changing their saved order.")
        self.overrides.setAccessibleName("Show caption-order overrides only"); self.overrides.setAccessibleDescription("Show only categories and SDXL Tags with an explicit saved or staged order override.")
        filters.addWidget(self.search,1); filters.addWidget(self.overrides); root.addLayout(filters)
        self.tree=OrderTree(); self.tree.setColumnWidth(0,300); self.tree.setColumnWidth(1,260); self.tree.setAccessibleName("Effective caption order by output group and category"); self.tree.setAccessibleDescription("Expand a category to load its SDXL Tags. Use drag and drop or the move buttons and keyboard shortcuts.")
        self.tree.relative_drop.connect(self._drop); self.tree.itemExpanded.connect(self._expand); self.tree.itemActivated.connect(self._activate_item); self.tree.currentItemChanged.connect(self._selection_changed); self.tree.itemSelectionChanged.connect(self._selection_changed); root.addWidget(self.tree,1)
        self.tree.setContextMenuPolicy(Qt.CustomContextMenu); self.tree.customContextMenuRequested.connect(self._context_menu)
        moves=QHBoxLayout(); self.move_buttons={}
        move_shortcuts={"start":"Alt+Home","up":"Alt+Up","down":"Alt+Down","end":"Alt+End"}
        for key,label in (("start","Move to Start"),("up","Move Up"),("down","Move Down"),("end","Move to End")):
            button=QPushButton(label); shortcut=move_shortcuts[key]; button.setProperty("compact",True); button.setToolTip(f"{label} within the current sibling set ({shortcut})."); button.setAccessibleDescription(button.toolTip()); button.clicked.connect(lambda _=False,direction=key:self._move(direction)); moves.addWidget(button); self.move_buttons[key]=button
        self.inherit=QPushButton("Use Category Order"); self.inherit.setProperty("compact",True); self.inherit.setToolTip("Remove the selected override and use inherited category order (Alt+I)."); self.inherit.setAccessibleDescription(self.inherit.toolTip()); self.inherit.clicked.connect(self._restore); moves.addWidget(self.inherit); moves.addStretch(1); root.addLayout(moves)
        root.addWidget(QLabel("Current caption preview")); self.preview=QLabel(); self.preview.setWordWrap(True); self.preview.setAccessibleName("Representative staged caption preview"); self.preview.setAccessibleDescription("A short example of the effective caption sequence after staged changes."); root.addWidget(self.preview)
        self.status=QLabel(); self.status.setObjectName("mutedLabel"); self.status.setWordWrap(True); self.status.setAccessibleName("Caption-order workspace status"); self.status.setAccessibleDescription("Reports staged changes, selections, and move results."); root.addWidget(self.status)
        from .components import bind_status_footer
        bind_status_footer(self,self.status,title="Caption order")
        actions=QHBoxLayout(); self.undo_button=QPushButton("Undo"); self.redo_button=QPushButton("Redo"); self.reset_button=QPushButton("Reset to Saved"); self.save_button=QPushButton("Save Order"); self.save_button.setProperty("primary",True)
        for button in (self.undo_button,self.redo_button,self.reset_button):button.setProperty("compact",True)
        self.undo_button.setAccessibleDescription("Undo the last staged caption-order change (Ctrl+Z).")
        self.redo_button.setAccessibleDescription("Redo the last staged caption-order change (Ctrl+Y).")
        self.reset_button.setAccessibleDescription("Discard every staged caption-order change and return to the saved policy.")
        self.save_button.setAccessibleDescription("Review impact and save the staged System Knowledge ordering policy without rewriting image data.")
        actions.addWidget(self.undo_button); actions.addWidget(self.redo_button); actions.addWidget(self.reset_button); actions.addStretch(1); actions.addWidget(self.save_button); root.addLayout(actions)
        self.undo_button.clicked.connect(self._undo); self.redo_button.clicked.connect(self._redo); self.reset_button.clicked.connect(self._reset); self.save_button.clicked.connect(self._save)
        self.search.textChanged.connect(self.refresh); self.overrides.toggled.connect(self.refresh)
        for sequence,direction in (("Alt+Up","up"),("Alt+Down","down"),("Alt+Home","start"),("Alt+End","end")):QShortcut(QKeySequence(sequence),self,activated=lambda d=direction:self._move(d))
        QShortcut(QKeySequence("Alt+I"),self,activated=self._restore)
        QShortcut(QKeySequence.Undo,self,activated=self._undo); QShortcut(QKeySequence.Redo,self,activated=self._redo)
        QWidget.setTabOrder(self.search,self.overrides); QWidget.setTabOrder(self.overrides,self.tree)
        previous=self.tree
        for button in (*self.move_buttons.values(),self.inherit,self.undo_button,self.redo_button,self.reset_button,self.save_button):
            QWidget.setTabOrder(previous,button); previous=button
        if self.embedded:
            self.intro.hide(); self.search.hide()
            for button in (*self.move_buttons.values(),self.inherit,self.undo_button,self.redo_button,self.reset_button,self.save_button):button.hide()

    def _set_status(self,text,*,announce=False):
        self.status.setText(text); self.status.setAccessibleName(text)
        if announce:announce_accessibly(self.status,text)

    def refresh(self,*_):
        expanded,selected,current,scroll=self._view_state()
        query=self.search.text().strip().casefold(); overrides_only=self.overrides.isChecked(); self.tree.clear(); loaded=0; matching_total=0; selected_items=[]; current_item=None
        for group in ORDER_GROUPS:
            rows=[]
            for category in self.session.all_categories():
                effective_group,priority,_=self.session._category_effective(category.id)
                if effective_group!=group:continue
                path=self.session.path_label(category.id); parent_context=self._category_parent_context(category.id); tags=self.session.tag_order(category.id)
                matching=[tag for tag in tags if (not query or query in " ".join(self.tags.identity_names(tag)).casefold()) and (not overrides_only or tag.ordering.reviewed)]
                category_matches=not query or query in f"{category.name} {path}".casefold()
                category_override=bool(category.ordering_group and category.ordering_reviewed)
                if overrides_only and not category_override and not matching:continue
                if query and not category_matches and not matching:continue
                matching_total+=len(matching)
                rows.append((category,path,parent_context,priority,matching,category_override))
            if not rows:continue
            count_label=f"{len(rows):,} {'category' if len(rows)==1 else 'categories'}"
            lane_label=f"{ORDER_GROUP_LABELS[group]} output group · {count_label}"
            lane=QTreeWidgetItem((lane_label,"","")); lane.setData(0,Qt.UserRole,("group",group)); lane.setData(0,Qt.AccessibleTextRole,lane_label); lane.setFlags(lane.flags()&~Qt.ItemIsDragEnabled); self.tree.addTopLevelItem(lane); lane.setFirstColumnSpanned(True); lane_font=lane.font(0); lane_font.setBold(True); lane.setFont(0,lane_font)
            for category,path,parent_context,priority,matching,category_override in sorted(rows,key=lambda row:(-row[3],row[1].casefold(),row[0].id)):
                source="Category order" if category_override else "Inherited"
                item=QTreeWidgetItem((category.name,parent_context,source)); item.setData(0,Qt.UserRole,("category",category.id)); item.setData(0,Qt.AccessibleTextRole,f"Category {category.name}; path {path}; position source {source}"); item.setToolTip(0,path); item.setToolTip(1,parent_context); item.setToolTip(2,source); lane.addChild(item)
                item.setData(0,Qt.UserRole+1,len(matching)); item.setData(0,Qt.UserRole+2,tuple(tag.id for tag in matching)); item.setChildIndicatorPolicy(QTreeWidgetItem.ShowIndicator if matching else QTreeWidgetItem.DontShowIndicator)
                if query or overrides_only or ("category",category.id) in expanded:
                    loaded+=self._load_tags(item,matching,self.MATERIALIZE_LIMIT-loaded)
                if ("category",category.id) in expanded:item.setExpanded(True)
                if ("category",category.id) in selected:selected_items.append(item)
                if ("category",category.id)==current:current_item=item
                for child_index in range(item.childCount()):
                    child=item.child(child_index); key=child.data(0,Qt.UserRole)
                    if key in selected:selected_items.append(child)
                    if key==current:current_item=child
            lane.setExpanded(bool(query or overrides_only or ("group",group) in expanded))
            if ("group",group) in selected:selected_items.append(lane)
            if ("group",group)==current:current_item=lane
        if current_item is not None:self.tree.setCurrentItem(current_item)
        for item in selected_items:item.setSelected(True)
        self.tree.verticalScrollBar().setValue(scroll)
        self.tree.resizeColumnToContents(2); impact=self.session.impact(); self.preview.setText(", ".join(impact.caption_examples_after) or "No representative SDXL Tags are available.")
        prefix="Unsaved order changes. " if self.session.has_changes else "Order matches the saved policy. "
        if loaded==0:detail=f"{matching_total:,} SDXL Tags available. Expand a category to show a bounded first page."
        else:detail=f"Showing {loaded:,} of {matching_total:,} SDXL Tags."+(" Use Load more or refine the search to continue." if loaded<matching_total else "")
        self._set_status(prefix+detail)
        self.undo_button.setEnabled(self.session.can_undo); self.redo_button.setEnabled(self.session.can_redo); self.reset_button.setEnabled(self.session.has_changes); self.save_button.setEnabled(self.session.has_changes); self._selection_changed()

    def _category_parent_context(self,category_id):
        record=self.session._state.categories[category_id]
        return self.session.path_label(record.parent_id) if record.parent_id else "Top-level category"

    def _view_state(self):
        if not self.tree.topLevelItemCount():return set(),set(),None,0
        expanded=set(); selected=set(); current=None; stack=[self.tree.topLevelItem(index) for index in range(self.tree.topLevelItemCount())]
        while stack:
            item=stack.pop(); key=item.data(0,Qt.UserRole)
            if item.isExpanded():expanded.add(key)
            if item.isSelected():selected.add(key)
            if item is self.tree.currentItem():current=key
            stack.extend(item.child(index) for index in range(item.childCount()))
        return expanded,selected,current,self.tree.verticalScrollBar().value()

    def select_key(self,key):
        if not key or key[0] not in {"category","tag"}:return False
        if key[0]=="tag":
            record=self.session._state.tags.get(key[1])
            if record is None:return False
            category_item=self._find_item(("category",record.category))
            if category_item is not None:
                self._load_tags(category_item,ensure_id=record.id)
                if self._find_item(key) is None:
                    for index in reversed(range(category_item.childCount())):
                        child=category_item.child(index); child_key=child.data(0,Qt.UserRole)
                        if child_key and child_key[0]=="load_more":category_item.takeChild(index)
                    self._append_tag_item(category_item,record); self._append_load_more(category_item)
        item=self._find_item(key)
        if item is None:return False
        parent=item.parent()
        while parent is not None:parent.setExpanded(True); parent=parent.parent()
        self.tree.clearSelection(); item.setSelected(True); self.tree.setCurrentItem(item); self.tree.scrollToItem(item)
        return True

    def _find_item(self,key):
        pending=[self.tree.topLevelItem(index) for index in range(self.tree.topLevelItemCount())]
        while pending:
            item=pending.pop()
            if item.data(0,Qt.UserRole)==key:return item
            pending.extend(item.child(index) for index in range(item.childCount()))
        return None

    def _append_tag_item(self,item,record):
        category_path=self.session.path_label(record.category)
        source="Tag override" if record.ordering.reviewed else "Inherited"
        child=QTreeWidgetItem((record.display,"",source)); child.setData(0,Qt.UserRole,("tag",record.id)); child.setData(0,Qt.AccessibleTextRole,f"SDXL Tag {record.display}; category {category_path}; position source {source}"); child.setData(1,Qt.AccessibleTextRole,f"Category: {category_path}"); child.setToolTip(0,f"{record.display}\nCategory: {category_path}"); child.setToolTip(2,source); item.addChild(child)

    def _load_tags(self,item,records=None,remaining=None,*,ensure_id=""):
        if item.childCount():return sum(item.child(index).data(0,Qt.UserRole)[0]=="tag" for index in range(item.childCount()))
        category_id=item.data(0,Qt.UserRole)[1]
        if records is None:
            record_ids=item.data(0,Qt.UserRole+2)
            records=[self.session._state.tags[value] for value in record_ids] if record_ids is not None else self.session.tag_order(category_id)
        else:records=list(records)
        item.setData(0,Qt.UserRole+1,len(records)); item.setData(0,Qt.UserRole+2,tuple(record.id for record in records))
        remaining=self.FETCH_BATCH if remaining is None else max(0,min(int(remaining),self.FETCH_BATCH))
        shown=list(records[:remaining])
        ensured=next((record for record in records if record.id==ensure_id),None)
        if ensured is not None and ensured not in shown:
            if shown:shown[-1]=ensured
            else:shown.append(ensured)
        for record in shown:self._append_tag_item(item,record)
        self._append_load_more(item)
        return len(shown)

    def _append_load_more(self,item):
        record_ids=item.data(0,Qt.UserRole+2) or ()
        loaded={item.child(index).data(0,Qt.UserRole)[1] for index in range(item.childCount()) if item.child(index).data(0,Qt.UserRole)[0]=="tag"}
        remaining=len(record_ids)-len(loaded)
        if remaining<=0:return
        count=min(self.FETCH_BATCH,remaining); child=QTreeWidgetItem((f"Load {count:,} more…",f"{len(loaded):,} of {len(record_ids):,} shown",""))
        child.setData(0,Qt.UserRole,("load_more",item.data(0,Qt.UserRole)[1])); child.setData(0,Qt.AccessibleTextRole,f"Load {count:,} more SDXL Tags; {len(loaded):,} of {len(record_ids):,} currently shown")
        child.setFlags((child.flags()|Qt.ItemIsEnabled|Qt.ItemIsSelectable)&~Qt.ItemIsDragEnabled); item.addChild(child)

    def _fetch_more(self,item):
        record_ids=item.data(0,Qt.UserRole+2) or (); loaded={}
        for index in reversed(range(item.childCount())):
            child=item.child(index); key=child.data(0,Qt.UserRole)
            if key and key[0]=="load_more":item.takeChild(index)
            elif key and key[0]=="tag":loaded[key[1]]=child
        added=0
        for record_id in record_ids:
            if record_id in loaded:continue
            self._append_tag_item(item,self.session._state.tags[record_id]); added+=1
            if added>=self.FETCH_BATCH:break
        self._append_load_more(item); self._set_status(f"Showing {len(loaded)+added:,} of {len(record_ids):,} SDXL Tags in {self.session.path_label(item.data(0,Qt.UserRole)[1])}.",announce=True)
        return added

    def _activate_item(self,item,_column=0):
        key=item.data(0,Qt.UserRole)
        if key and key[0]=="load_more" and item.parent() is not None:self._fetch_more(item.parent())

    def _expand(self,item):
        key=item.data(0,Qt.UserRole)
        if key and key[0]=="category" and not item.childCount():self._load_tags(item)

    def _selection_changed(self,*_):
        keys=self._selected_keys(); scope=self._selection_scope(keys); key=keys[0] if len(keys)==1 else None; movable=bool(keys and scope is not None)
        for button in self.move_buttons.values():button.setEnabled(movable)
        can_restore=False
        if key and key[0]=="tag":can_restore=self.session._state.tags[key[1]].ordering.reviewed
        elif key and key[0]=="category":
            record=self.session._state.categories[key[1]]; can_restore=bool(record.parent_id and record.ordering_group)
        self.inherit.setEnabled(can_restore)
        if len(keys)>1:
            if scope is None:text=f"{len(keys):,} rows selected across incompatible scopes. Choose one sibling set to move together."
            else:
                kind,_=scope; text=f"{len(keys):,} {'SDXL Tags' if kind=='tag' else 'categories'} selected. They will move together within {self._scope_label(kind,keys[0][1])}."
            self._set_status(text,announce=True)

    def _selected_keys(self,fallback=None):
        items=self.tree.selectedItems()
        if fallback is not None and fallback not in items:items=[fallback]
        return [key for key in (item.data(0,Qt.UserRole) for item in items) if key and key[0] in {"category","tag"}]

    def _selection_scope(self,keys):
        if not keys or len({key[0] for key in keys})!=1:return None
        kind=keys[0][0]
        if kind=="tag":
            scopes={(self.session._state.tags[key[1]].category,self.session._state.tags[key[1]].ordering.group) for key in keys}
        else:
            scopes={(self.session._state.categories[key[1]].parent_id,self.session._category_effective(key[1])[0]) for key in keys}
        return (kind,next(iter(scopes))) if len(scopes)==1 else None

    def _siblings(self,key):
        kind,record_id=key
        if kind=="tag":
            record=self.session._state.tags[record_id]; return [row for row in self.session.tag_order(record.category) if row.ordering.group==record.ordering.group]
        record=self.session._state.categories[record_id]; group,_,_=self.session._category_effective(record_id); return self.session.category_order(group,parent_id=record.parent_id)

    @staticmethod
    def _position_text(positions):
        positions=list(positions)
        if len(positions)==1:return f"position {positions[0]}"
        if positions==list(range(positions[0],positions[-1]+1)):return f"positions {positions[0]}–{positions[-1]}"
        return "positions "+", ".join(str(value) for value in positions[:-1])+f" and {positions[-1]}"

    def _scope_label(self,kind,record_id):
        if kind=="tag":return self.session.path_label(self.session._state.tags[record_id].category)
        record=self.session._state.categories[record_id]
        return self.session.path_label(record.parent_id) if record.parent_id else f"{ORDER_GROUP_LABELS[self.session._category_effective(record.id)[0]]} output group"

    def _finish_move(self,kind,selected_ids,old_positions,target):
        records=[self.session._state.tags[value] if kind=="tag" else self.session._state.categories[value] for value in selected_ids]
        label=(records[0].display if kind=="tag" else records[0].name) if len(records)==1 else f"{len(records):,} {'SDXL Tags' if kind=='tag' else 'categories'}"
        destination=self._position_text(range(target+1,target+len(records)+1)); origin=self._position_text(old_positions); scope_label=self._scope_label(kind,selected_ids[0])
        self.refresh()
        item=self._find_item((kind,selected_ids[0]))
        if item is not None:
            parent=item.parent()
            while parent is not None:parent.setExpanded(True); parent=parent.parent()
            self.tree.scrollToItem(item)
        text=f"{label}: moved together from {origin} to {destination} within {scope_label}."; self._set_status(text,announce=True)

    def _move(self,direction):
        keys=self._selected_keys(); scope=self._selection_scope(keys)
        if scope is None:return
        kind,_=scope; rows=self._siblings(keys[0]); selected_ids={key[1] for key in keys}; selected=[row for row in rows if row.id in selected_ids]
        if len(selected)!=len(selected_ids):return
        old_positions=[index+1 for index,row in enumerate(rows) if row.id in selected_ids]; first=old_positions[0]-1; current=sum(row.id not in selected_ids for row in rows[:first]); remaining=len(rows)-len(selected)
        target={"start":0,"up":max(0,current-1),"down":min(remaining,current+1),"end":remaining}[direction]
        if target==current:
            self._set_status("Already at this end of the selected ordering set.",announce=True)
            return
        selected_ids=[row.id for row in selected]
        if kind=="tag":self.session.move_tags(selected_ids,target)
        else:self.session.move_categories(selected_ids,target)
        self._finish_move(kind,selected_ids,old_positions,target)

    def _drop(self,source,target,after=False):
        keys=self._selected_keys(source); scope=self._selection_scope(keys); target_key=target.data(0,Qt.UserRole)
        if scope is None or not target_key:
            QMessageBox.information(self,"Choose one sibling set","Move only categories that share a parent and output group, or SDXL Tags that share a category and output group."); return
        kind,_=scope; selected_id_set={key[1] for key in keys}; source_rows=self._siblings(keys[0]); selected_ids=[row.id for row in source_rows if row.id in selected_id_set]; old_positions=[index+1 for index,row in enumerate(source_rows) if row.id in selected_id_set]
        if target_key[0]==kind and target_key[1] in selected_id_set:return
        if target_key[0]=="group":
            group=target_key[1]
            if group!=scope[1][1] and not self._confirm_group_move(kind,selected_ids,group):return
            index=0
            if kind=="tag":self.session.move_tags(selected_ids,index,target_group=group,confirm_cross_group=True)
            else:self.session.move_categories(selected_ids,index,target_group=group,confirm_cross_group=True)
        elif kind==target_key[0]:
            target_scope=self._selection_scope((target_key,))
            if target_scope[1][0]!=scope[1][0]:
                self._set_status(
                    "Not moved: choose a row with the same "
                    +("category." if kind=="tag" else "Parent category."),
                    announce=True,
                )
                return
            group=target_scope[1][1]
            if group!=scope[1][1] and not self._confirm_group_move(kind,selected_ids,group):return
            rows=self._siblings(target_key)
            target_index=next(i for i,row in enumerate(rows) if row.id==target_key[1])+int(after)
            index=sum(row.id not in selected_id_set for row in rows[:target_index])
            if kind=="tag":self.session.move_tags(selected_ids,index,target_group=group,confirm_cross_group=True)
            else:self.session.move_categories(selected_ids,index,target_group=group,confirm_cross_group=True)
        else:
            QMessageBox.information(self,"Choose a matching row","Drop categories beside categories and SDXL Tags beside SDXL Tags, or use an output-group lane to change groups.");return
        self._finish_move(kind,selected_ids,old_positions,index)

    def _confirm_group_move(self,kind,selected_ids,group):
        accepted=QMessageBox.question(
            self,"Change output group",
            f"Move {len(selected_ids):,} selected {('SDXL Tags' if kind=='tag' else 'categories')} "
            f"into {ORDER_GROUP_LABELS[group]}? This changes caption grouping, not only position. "
            "Category membership and Parent categories stay the same.",
        )==QMessageBox.Yes
        if not accepted:self._set_status("Move cancelled. Caption order is unchanged.",announce=True)
        return accepted

    def _context_menu(self,position):
        item=self.tree.itemAt(position)
        if item is not None:
            if not item.isSelected():self.tree.clearSelection(); item.setSelected(True)
            self.tree.setCurrentItem(item)
        menu=QMenu(self)
        scope=self._selection_scope(self._selected_keys(item))
        for label,direction in (("Move to Start","start"),("Move Earlier","up"),("Move Later","down"),("Move to End","end")):
            action=menu.addAction(label); action.setEnabled(scope is not None)
            action.triggered.connect(lambda _=False,value=direction:self._move(value))
        menu.addSeparator()
        restore=menu.addAction("Use Category Order"); restore.setEnabled(self.inherit.isEnabled()); restore.triggered.connect(self._restore)
        menu.exec(self.tree.viewport().mapToGlobal(position))

    def _restore(self):
        item=self.tree.currentItem(); key=item.data(0,Qt.UserRole) if item else None
        if not key:return
        if key[0]=="tag":
            record=self.session._state.tags[key[1]]
            if not record.ordering.reviewed:return
            label=record.display; scope=self.session.path_label(record.category); self.session.restore_tag_inheritance(key[1])
        elif key[0]=="category":
            record=self.session._state.categories[key[1]]
            if not record.parent_id or not record.ordering_group:return
            label=record.name; scope=self._scope_label("category",key[1]); self.session.restore_category_inheritance(key[1])
        else:return
        self.refresh(); self._set_status(f"{label}: restored inherited category order within {scope}.",announce=True)

    def _undo(self):
        if self.session.undo():self.refresh(); self._set_status("Undid the last staged caption-order change.",announce=True)
    def _redo(self):
        if self.session.redo():self.refresh(); self._set_status("Redid the last staged caption-order change.",announce=True)
    def _reset(self):
        if self.session.has_changes:self.session.reset(); self.refresh(); self._set_status("Discarded staged changes and restored the saved caption-order policy.",announce=True)

    def has_unsaved_changes(self):
        return self.session.has_changes

    def discard_staged_changes(self):
        self.session.reset(); self.refresh()

    def reload_from_live(self):
        if self.session.has_changes:return False
        self.categories.load(); self.tags.load(); self.tags.apply_category_ordering(self.categories); self.session=TagOrderWorkspaceSession(self.tags,self.categories); self.refresh(); return True

    def _organization_depends(self):
        profile=getattr(self.settings,"organization_profile",{}) if self.settings is not None else {}; return OrganizationProfile.from_dict(profile).depends_on_tag_order

    def _impact(self):
        organization_depends=self._organization_depends(); impact=self.session.impact(organization_depends_on_order=organization_depends)
        try:
            index=self.image_index_provider() if callable(self.image_index_provider) else None
            paths=(
                None if index is None else affected_order_projection_paths(
                    impact,self.session._state.tags,index.all_records(),
                    aliases_for=self.tags.effective_aliases,
                )
            )
        except Exception:paths=None
        return self.session.impact(organization_depends_on_order=organization_depends,affected_image_paths=paths)

    def _save(self):
        impact=self._impact()
        if impact.affected_images is None:projection_detail="No active indexed library is available, so affected image projections cannot be counted here."
        elif impact.affected_images:projection_detail=f"{impact.affected_images:,} indexed image(s) use affected SDXL Tags and may have stale TXT captions or Metadata."
        else:projection_detail="No indexed images use the affected SDXL Tags, so no image projection update is expected."
        message=(f"Save {len(impact.changed_categories):,} category policy change(s) and {len(impact.changed_tags):,} SDXL Tag override change(s)?\n\nApproximately {impact.affected_tags:,} known SDXL Tags may produce a different projection order. {projection_detail}\n\nBefore: {', '.join(impact.caption_examples_before)}\nAfter: {', '.join(impact.caption_examples_after)}\n\nSaving changes System Knowledge only. Captions, Metadata, confirmed facts, raw evidence, and image files remain unchanged.\n\n"+("The active organization profile explicitly depends on tag order." if impact.organization_affected else "No folder changes: the active organization profile uses explicit folder levels, not caption order."))
        if QMessageBox.question(self,"Save Caption Order",message)!=QMessageBox.Yes:return
        try:result=self.session.save(organization_depends_on_order=self._organization_depends(),affected_image_paths=impact.affected_image_paths)
        except Exception as error:QMessageBox.critical(self,"Caption order was not saved",f"The prior Knowledge files were restored.\n\n{error}");return
        persisted=False
        if (
            result.impact.captions_may_be_stale
            or result.impact.metadata_may_be_stale
            or result.impact.organization_affected
        ):
            try:
                DownstreamUpdateStore(
                    self.tags.path.parent/"downstream_updates.json"
                ).record_projection_change(
                    affected_tag_ids=result.impact.affected_tag_ids,
                    changed_category_ids=result.impact.changed_categories,
                    label="Saved caption-order change",
                    cached_affected_image_count=result.impact.affected_images,
                    captions=result.impact.captions_may_be_stale,
                    metadata=result.impact.metadata_may_be_stale,
                    organization=result.impact.organization_affected,
                )
            except OSError:
                pass
            else:
                persisted=True; self.downstream_updates_changed.emit()
        self.order_saved.emit(); box=QMessageBox(self); box.setWindowTitle("Caption order saved"); box.setText("System Knowledge order was saved. Image data was not rewritten."); box.setInformativeText(("Review the separate Caption and Metadata jobs through Review Downstream Updates on Dashboard." if persisted else "Open Dataset to preview any Caption or Metadata updates when you are ready.") if result.impact.captions_may_be_stale else "No image projection update is expected.")
        box.addButton(QMessageBox.Close); box.exec()
        self.refresh()
