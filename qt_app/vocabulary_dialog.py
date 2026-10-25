from __future__ import annotations

from dataclasses import replace

from PySide6.QtCore import Qt, QTimer
from PySide6.QtWidgets import (
    QAbstractItemView, QApplication, QComboBox, QDialog, QDialogButtonBox, QHBoxLayout, QLabel,
    QLineEdit, QPushButton, QStyledItemDelegate, QTreeWidget, QTreeWidgetItem, QVBoxLayout,
)

from core import NewVocabularyItem
from .components import BoundedSearchableComboBox
from .modal_feedback import FeedbackEnabledDialog


class _ChoiceDelegate(QStyledItemDelegate):
    def __init__(
        self,choices,*,editable=False,placeholder="Type to filter choices…",
        accessible_name="Choice",parent=None,
    ) -> None:
        super().__init__(parent)
        self.choices=list(choices)
        self.editable=editable
        self.placeholder=placeholder
        self.accessible_name=accessible_name

    def createEditor(self, parent, _option, _index):
        if self.editable:
            editor=BoundedSearchableComboBox(
                parent,placeholder=self.placeholder,max_visible=12,
            )
            editor.setAccessibleName(self.accessible_name)
        else:
            editor=QComboBox(parent)
            editor.setInsertPolicy(QComboBox.NoInsert)
            editor.setMaxVisibleItems(12)
        for choice in self.choices:
            value,label=choice[:2]
            search_terms=choice[2] if len(choice)>2 else ()
            if self.editable:
                editor.addItem(
                    str(label),str(value),search_terms=search_terms,
                )
            else:
                editor.addItem(str(label),str(value))
        editor.activated.connect(lambda _index:self._accept(editor))
        if self.editable:
            QTimer.singleShot(0,lambda:editor.lineEdit().selectAll())
        else:
            QTimer.singleShot(0,editor.showPopup)
        return editor

    def _accept(self, editor) -> None:
        self.commitData.emit(editor)
        self.closeEditor.emit(editor)

    def setEditorData(self, editor, index) -> None:
        value=str(index.data(Qt.UserRole+1) or "")
        choice=editor.findData(value)
        if choice>=0: editor.setCurrentIndex(choice)

    def setModelData(self, editor, model, index) -> None:
        value=str(editor.currentData() or "")
        if not value: return
        model.setData(index,value,Qt.UserRole+1)
        model.setData(index,editor.currentText(),Qt.DisplayRole)


class _CategoryDelegate(_ChoiceDelegate):
    def setEditorData(self, editor, index) -> None:
        value=str(index.siblingAtColumn(0).data(Qt.UserRole+2) or "")
        choice=editor.findData(value)
        if choice>=0: editor.setCurrentIndex(choice)

    def setModelData(self, editor, model, index) -> None:
        value=str(editor.currentData() or "")
        if not value: return
        decision=index.siblingAtColumn(0)
        model.setData(decision,NewVocabularyDialog.ADD,Qt.UserRole+1)
        model.setData(decision,NewVocabularyDialog.LABELS[NewVocabularyDialog.ADD],Qt.DisplayRole)
        model.setData(decision,value,Qt.UserRole+2)
        display=f"{editor.currentText()} (Chosen)"
        model.setData(decision,display,Qt.UserRole+4)
        model.setData(index,display,Qt.DisplayRole)
        model.setData(index,"Chosen category",Qt.ToolTipRole)


class _AliasDelegate(_ChoiceDelegate):
    def setEditorData(self, editor, index) -> None:
        value=str(index.siblingAtColumn(0).data(Qt.UserRole+3) or "")
        choice=editor.findData(value)
        editor.setCurrentIndex(choice)
        if choice<0:
            editor.setEditText("")

    def setModelData(self, editor, model, index) -> None:
        choice=editor.findText(editor.currentText(),Qt.MatchFixedString)
        if choice<0:
            return
        value=str(editor.itemData(choice) or "")
        if not value:
            return
        decision=index.siblingAtColumn(0)
        model.setData(decision,NewVocabularyDialog.ALIAS,Qt.UserRole+1)
        model.setData(decision,NewVocabularyDialog.LABELS[NewVocabularyDialog.ALIAS],Qt.DisplayRole)
        model.setData(decision,value,Qt.UserRole+3)
        model.setData(index,editor.itemText(choice),Qt.DisplayRole)
        model.setData(index,f"Use {editor.itemText(choice)} as the canonical SDXL Tag",Qt.ToolTipRole)


class _VocabularyRow(QTreeWidgetItem):
    """Sort display text naturally while retaining stable intake identity."""

    def __lt__(self,other):
        column=self.treeWidget().sortColumn() if self.treeWidget() else 0
        if column==2:
            left=int(self.data(2,Qt.UserRole+10) or 0)
            right=int(other.data(2,Qt.UserRole+10) or 0)
        else:
            left=self.text(column).casefold()
            right=other.text(column).casefold()
        if left!=right:
            return left<right
        left_tie=(self.text(1).casefold(),int(self.data(0,Qt.UserRole) or 0))
        right_tie=(other.text(1).casefold(),int(other.data(0,Qt.UserRole) or 0))
        return left_tie<right_tie


class NewVocabularyDialog(FeedbackEnabledDialog):
    feedback_workflow = "New SDXL Tag names review"

    ADD="add"
    ALIAS="alias"
    IGNORE="ignore"
    LATER="later"
    SELECTED_SCOPE="selected"
    VISIBLE_SCOPE="visible"
    LABELS={ADD:"Remember Tag",ALIAS:"Use Existing Tag",IGNORE:"Always Ignore Tag",LATER:"Decide Later"}
    BULK_LABELS={ADD:"Remember Tags",IGNORE:"Always Ignore Tags",LATER:"Decide Later"}

    def __init__(self, items: list[NewVocabularyItem], category_choices=(), parent=None,
                 knowledge_choices=(),category_creator=None) -> None:
        super().__init__(parent)
        self.items = items
        self.category_creator=category_creator
        self._category_query=""
        self.knowledge_labels={
            str(choice[0]):str(choice[1]) for choice in knowledge_choices
        }
        self.setWindowTitle("New SDXL Tag names found")
        self.setMinimumSize(900, 520)
        self.resize(1180, 680)
        root = QVBoxLayout(self)
        noun="name" if len(items)==1 else "names"
        title = QLabel(f"CollectiGen found {len(items):,} SDXL Tag {noun} it has never seen before.")
        title.setObjectName("sectionTitle")
        root.addWidget(title)
        explanation = QLabel(
            "Choose a durable outcome for each name. Remember Tag keeps the name and queues its meaning for Review Center. "
            "Use Existing Tag records the name as another name for one canonical System Knowledge tag, so character recognition, rules, search, and other tag behavior use the same meaning. "
            "Always Ignore Tag creates the same reversible automatic rule used by Rules & Automation; automatic refresh and future managed evidence intake "
            "suppress that incoming SDXL Tag before review while preserving confirmed Image Knowledge and raw evidence. Decide Later changes nothing, so the name may be offered again."
        )
        explanation.setWordWrap(True)
        root.addWidget(explanation)
        search_row = QHBoxLayout()
        search_row.addWidget(QLabel("Find an SDXL Tag"))
        self.search = QLineEdit()
        self.search.setPlaceholderText("Type to filter this list")
        self.search.textChanged.connect(self._filter)
        search_row.addWidget(self.search, 1)
        root.addLayout(search_row)
        selection = QHBoxLayout()
        select_all = QPushButton("Select Visible")
        clear_selection = QPushButton("Clear")
        select_all.setToolTip("Select every Tag name shown by the current filter.")
        clear_selection.setToolTip("Clear the current Tag-name selection.")
        select_all.clicked.connect(self._select_visible)
        selection.addWidget(select_all); selection.addWidget(clear_selection)
        selection.addSpacing(14); selection.addWidget(QLabel("Decision"))
        self.decision_scope=QComboBox()
        self.decision_scope.setAccessibleName("Decision target")
        self.decision_scope.addItem("Selected Tags",self.SELECTED_SCOPE)
        self.decision_scope.addItem("All Visible Tags",self.VISIBLE_SCOPE)
        self.decision_scope.setToolTip(
            "Selected Tags is the safe default. All Visible Tags applies to every "
            "row shown by the current filter."
        )
        self.all_decision=QComboBox()
        self.all_decision.setAccessibleName("Decision to apply")
        for value in (self.ADD,self.IGNORE,self.LATER): self.all_decision.addItem(self.BULK_LABELS[value],value)
        self.all_decision.setCurrentIndex(self.all_decision.findData(self.LATER))
        self.apply_decision=QPushButton("Apply")
        self.apply_decision.setAccessibleName("Apply decision to selected Tags")
        self.apply_decision.clicked.connect(self._apply_bulk_decision)
        selection.addWidget(self.all_decision)
        selection.addWidget(QLabel("Apply To"))
        selection.addWidget(self.decision_scope)
        selection.addWidget(self.apply_decision)
        selection.addStretch(1)
        category_row = QHBoxLayout()
        category_row.addWidget(QLabel("Set Category for Selected Tags"))
        self.category=BoundedSearchableComboBox(
            placeholder="Type any part of a category path…",max_visible=12,
        )
        self.category.setAccessibleName("Category for selected SDXL Tags")
        for category_id,label in category_choices:
            self.category.addItem(str(label),str(category_id))
        self.category.lineEdit().textEdited.connect(self._remember_category_query)
        self.new_category_button=QPushButton("New Category…")
        self.new_category_button.setAccessibleName("Create a missing category")
        self.new_category_button.setToolTip(
            "Create one Category through the verified Category Workspace, then "
            "assign it to the selected Tag rows without saving those Tag decisions yet."
        )
        self.new_category_button.clicked.connect(self._create_category)
        self.new_category_button.setVisible(callable(self.category_creator))
        self.apply_category=QPushButton("Apply Category")
        self.apply_category.clicked.connect(self._apply_category)
        category_row.addWidget(self.category,1); category_row.addWidget(self.new_category_button)
        category_row.addWidget(self.apply_category); category_row.addStretch(1)
        # Decisions are applied to the current row selection.  A checkable tree
        # would introduce a second, competing selection state here.
        self.table = QTreeWidget()
        self.table.setHeaderLabels(("Decision", "New SDXL Tag", "Images", "Sources", "Category", "Use Existing Tag"))
        self.table.setRootIsDecorated(False)
        self.table.setAlternatingRowColors(True)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setColumnWidth(0, 145)
        self.table.setColumnWidth(1, 205)
        self.table.setColumnWidth(2, 84)
        self.table.setColumnWidth(3, 185)
        self.table.setColumnWidth(4, 170)
        self.table.header().setStretchLastSection(True)
        category_labels={
            str(choice[0]):str(choice[1]) for choice in category_choices
        }
        for index, item in enumerate(items):
            category_id=str(item.suggested_category or "uncategorized")
            guess=category_labels.get(category_id,category_id.replace("_", " ").title())
            if item.confidence: guess+=f" ({item.confidence:.0%})"
            sources=", ".join(item.sources) or "Confirmed Image Knowledge"
            target_id=str(item.alias_target_id or "")
            target_label=self.knowledge_labels.get(target_id,"")
            target_text=f"Suggested: {target_label}" if target_label else "—"
            reason=str((item.suggestion or {}).get("reason") or "").strip()
            row = _VocabularyRow((self.LABELS[self.LATER], item.tag, f"{item.seen_count:,}", sources, guess, target_text))
            row.setFlags(row.flags() & ~Qt.ItemIsUserCheckable)
            row.setFlags(row.flags() | Qt.ItemIsEditable)
            row.setData(0, Qt.UserRole, index)
            row.setData(0, Qt.UserRole+1, self.LATER)
            row.setData(0, Qt.UserRole+2, category_id)
            row.setData(0, Qt.UserRole+3, str(item.alias_target_id or ""))
            row.setData(0, Qt.UserRole+4, guess)
            row.setData(0, Qt.UserRole+5, reason)
            row.setData(2, Qt.UserRole+10, int(item.seen_count))
            row.setToolTip(0,"Choose Remember Tag, Use Existing Tag, Always Ignore Tag, or Decide Later.")
            row.setToolTip(3,"Typed tag source(s): "+sources)
            row.setToolTip(4,"Click to choose a category; this also chooses Remember Tag."+(f"\n\n{reason}" if reason else ""))
            row.setToolTip(
                5,
                (f"Suggested canonical System Knowledge Tag: {target_label}. Click to review or change it; this also chooses Use Existing Tag."
                 if target_label else "Click to choose its canonical tag; this also chooses Use Existing Tag."),
            )
            if item.confidence:
                row.setToolTip(4,f"Suggested category confidence: {item.confidence:.0%}"+(f"\n\n{reason}" if reason else ""))
            self.table.addTopLevelItem(row)
        self.table.setSortingEnabled(True)
        self.table.sortItems(2,Qt.DescendingOrder)
        clear_selection.clicked.connect(self.table.clearSelection)
        self.table.itemSelectionChanged.connect(self._selection_changed)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.decision_delegate=_ChoiceDelegate([(value,self.LABELS[value]) for value in (self.ADD,self.ALIAS,self.IGNORE,self.LATER)],parent=self.table)
        self.category_delegate=_CategoryDelegate(
            category_choices,editable=True,
            placeholder="Type any part of a category path…",
            accessible_name="Category for this SDXL Tag",parent=self.table,
        )
        self.alias_delegate=_AliasDelegate(
            knowledge_choices,editable=True,
            placeholder="Type any part of a known name or alias…",
            accessible_name="Existing SDXL Tag for this name",parent=self.table,
        )
        self.table.setItemDelegateForColumn(0,self.decision_delegate)
        self.table.setItemDelegateForColumn(4,self.category_delegate)
        self.table.setItemDelegateForColumn(5,self.alias_delegate)
        self.decision_delegate.commitData.connect(lambda _editor:QTimer.singleShot(0,self._refresh_decision_rows))
        self.category_delegate.commitData.connect(lambda _editor:QTimer.singleShot(0,self._refresh_decision_rows))
        self.alias_delegate.commitData.connect(lambda _editor:QTimer.singleShot(0,self._refresh_decision_rows))
        self.table.itemClicked.connect(self._edit_cell)
        self.table.itemActivated.connect(self._edit_cell)
        self.decision_scope.currentIndexChanged.connect(self._refresh_decision_scope)
        root.addLayout(selection)
        root.addLayout(category_row)
        self.category_status=QLabel()
        self.category_status.setObjectName("mutedLabel")
        self.category_status.setWordWrap(True)
        self.category_status.setAccessibleName("Category creation result")
        self.category_status.hide()
        root.addWidget(self.category_status)
        root.addWidget(self.table, 1)
        decisions = QHBoxLayout()
        decisions.addWidget(QLabel("Click Category to remember a name; click Use Existing Tag to make an alias. Decision updates automatically.")); decisions.addStretch(1)
        self.count = QLabel(); decisions.addWidget(self.count)
        root.addLayout(decisions)
        self.save_error=QLabel()
        self.save_error.setObjectName("errorLabel")
        self.save_error.setWordWrap(True)
        self.save_error.hide()
        root.addWidget(self.save_error)
        buttons = QDialogButtonBox(QDialogButtonBox.Save|QDialogButtonBox.Cancel)
        self.add_button=buttons.button(QDialogButtonBox.Save)
        self.add_button.setText("Save Decisions")
        self.add_button.setProperty("primary", True)
        buttons.accepted.connect(self._accept_decisions)
        buttons.rejected.connect(self.reject)
        root.addWidget(buttons)
        if self.table.topLevelItemCount(): self.table.setCurrentItem(self.table.topLevelItem(0)); self._sync_category_control()
        self._refresh_decision_scope()
        self._update_count()

    def selected_items(self) -> list[NewVocabularyItem]:
        return self.decisions()[self.ADD]

    def decisions(self) -> dict[str,list[NewVocabularyItem]]:
        selected={self.ADD:[],self.ALIAS:[],self.IGNORE:[],self.LATER:[]}
        for index in range(self.table.topLevelItemCount()):
            row = self.table.topLevelItem(index)
            decision=str(row.data(0,Qt.UserRole+1) or self.LATER)
            item=self.items[int(row.data(0, Qt.UserRole))]
            selected.setdefault(decision,[]).append(replace(
                item,suggested_category=str(row.data(0,Qt.UserRole+2) or "uncategorized"),
                alias_target_id=str(row.data(0,Qt.UserRole+3) or ""),
            ))
        return selected

    def _filter(self, text: str) -> None:
        query = text.strip().casefold()
        for index in range(self.table.topLevelItemCount()):
            row = self.table.topLevelItem(index)
            row.setHidden(bool(query and query not in row.text(1).casefold()))
        self._refresh_decision_scope()

    def _select_visible(self) -> None:
        self.table.blockSignals(True)
        try:
            self.table.clearSelection()
            for index in range(self.table.topLevelItemCount()):
                row = self.table.topLevelItem(index)
                if not row.isHidden():
                    row.setSelected(True)
        finally:
            self.table.blockSignals(False)
        self._selection_changed()

    def _edit_cell(self, item, column: int) -> None:
        if column==4:
            self._set_rows_decision([item],self.ADD)
        elif column==5:
            self._set_rows_decision([item],self.ALIAS)
        if column in (0,4,5): self.table.editItem(item,column)

    def _apply_bulk_decision(self) -> None:
        decision=str(self.all_decision.currentData() or self.LATER)
        scope=str(self.decision_scope.currentData() or self.SELECTED_SCOPE)
        rows=(
            self._visible_decision_rows()
            if scope==self.VISIBLE_SCOPE else self.table.selectedItems()
        )
        self._set_rows_decision(rows,decision)

    def _visible_decision_rows(self):
        return [
            self.table.topLevelItem(index)
            for index in range(self.table.topLevelItemCount())
            if not self.table.topLevelItem(index).isHidden()
        ]

    def _refresh_decision_scope(self, *_args) -> None:
        selected=len(self.table.selectedItems())
        visible=len(self._visible_decision_rows())
        selected_index=self.decision_scope.findData(self.SELECTED_SCOPE)
        visible_index=self.decision_scope.findData(self.VISIBLE_SCOPE)
        self.decision_scope.setItemText(
            selected_index,f"Selected Tags ({selected:,})"
        )
        self.decision_scope.setItemText(
            visible_index,f"All Visible Tags ({visible:,})"
        )
        scope=str(self.decision_scope.currentData() or self.SELECTED_SCOPE)
        target_count=visible if scope==self.VISIBLE_SCOPE else selected
        self.apply_decision.setEnabled(target_count>0)
        target_label=(
            "all visible Tags" if scope==self.VISIBLE_SCOPE else "selected Tags"
        )
        self.apply_decision.setAccessibleName(
            f"Apply decision to {target_count:,} {target_label}"
        )
        self.apply_decision.setToolTip(
            f"Apply the chosen decision to {target_count:,} {target_label}."
        )

    def _set_rows_decision(self, rows, decision: str) -> None:
        for row in rows:
            row.setData(0,Qt.UserRole+1,decision)
            row.setText(0,self.LABELS[decision])
            self._refresh_row(row)
        self._update_count()

    def _refresh_row(self,row) -> None:
        decision=str(row.data(0,Qt.UserRole+1) or self.LATER)
        if decision==self.ALIAS:
            target_id=str(row.data(0,Qt.UserRole+3) or "")
            row.setText(5,self.knowledge_labels.get(target_id,"Choose an existing tag"))
            row.setToolTip(5,"Choose the canonical System Knowledge tag this observed name should identify.")
        else:
            target_id=str(row.data(0,Qt.UserRole+3) or "")
            target_label=self.knowledge_labels.get(target_id,"")
            row.setText(5,f"Suggested: {target_label}" if target_label else "—")
            row.setToolTip(
                5,
                (f"Suggested canonical System Knowledge Tag: {target_label}. Click to review or change it and switch this decision to Use Existing Tag."
                 if target_label else "Click to choose a canonical tag and switch this decision to Use Existing Tag."),
            )
        row.setText(4,"—" if decision in {self.ALIAS,self.IGNORE} else str(row.data(0,Qt.UserRole+4) or "Uncategorized"))
        reason=str(row.data(0,Qt.UserRole+5) or "").strip()
        row.setToolTip(4,"Click to choose a category and switch this decision to Remember Tag."+(f"\n\n{reason}" if reason else ""))

    def _refresh_decision_rows(self) -> None:
        for index in range(self.table.topLevelItemCount()):
            self._refresh_row(self.table.topLevelItem(index))
        self._sync_category_control(); self._update_count()

    def _accept_decisions(self) -> None:
        # Moving focus commits an open inline combo before the dialog snapshots
        # its durable outcomes.  Reconcile the displayed Decision as an extra
        # guard against a platform closing an editor before its model role lands.
        self.setFocus(Qt.OtherFocusReason)
        QApplication.processEvents()
        by_label={label:decision for decision,label in self.LABELS.items()}
        for index in range(self.table.topLevelItemCount()):
            row=self.table.topLevelItem(index)
            if row.text(0) in by_label:
                row.setData(0,Qt.UserRole+1,by_label[row.text(0)])
        self._update_count()
        outcomes=self.decisions()
        missing_alias=[item.tag for item in outcomes[self.ALIAS] if not item.alias_target_id]
        if missing_alias:
            self.save_error.setText("Choose an existing System Knowledge tag for: "+", ".join(missing_alias[:5]))
            self.save_error.show()
            return
        if not outcomes[self.ADD] and not outcomes[self.ALIAS] and not outcomes[self.IGNORE]:
            self.save_error.setText("Choose Remember Tag, Use Existing Tag, or Always Ignore Tag for at least one name before saving.")
            self.save_error.show()
            return
        self.save_error.hide()
        self.accept()

    def _set_selected_decision(self, decision: str) -> None:
        self._set_rows_decision(self.table.selectedItems(),decision)

    def _selection_changed(self) -> None:
        self._sync_category_control()
        self._refresh_decision_scope()

    def _remember_category_query(self,text) -> None:
        self._category_query=str(text)

    def _category_rows(self):
        rows=self.table.selectedItems()
        if not rows and self.table.currentItem() is not None:rows=[self.table.currentItem()]
        return rows

    def _apply_category_value(self,category_id,label,rows) -> None:
        for row in rows:
            row.setData(0,Qt.UserRole+1,self.ADD); row.setText(0,self.LABELS[self.ADD])
            display=f"{label} (Chosen)"
            row.setData(0,Qt.UserRole+2,category_id); row.setData(0,Qt.UserRole+4,display); row.setText(4,display); row.setToolTip(4,"Chosen category")
            self._refresh_row(row)

    def _create_category(self) -> None:
        if not callable(self.category_creator):return
        rows=list(self._category_rows())
        initial_name=self._category_query.strip()
        try:created=self.category_creator(initial_name,self)
        except Exception as error:
            self.category_status.setObjectName("errorLabel")
            self.category_status.setText(f"The Category could not be created: {error}")
            self.category_status.show(); return
        if not created:return
        category_id,label=(str(created[0]),str(created[1]))
        existing=self.category.findData(category_id)
        if existing<0:self.category.addItem(label,category_id)
        else:self.category.setItemText(existing,label)
        choices=[choice for choice in self.category_delegate.choices if str(choice[0])!=category_id]
        choices.append((category_id,label)); choices.sort(key=lambda choice:str(choice[1]).casefold())
        self.category_delegate.choices=choices
        self.category.setCurrentIndex(self.category.findData(category_id))
        self._category_query=""
        self._apply_category_value(category_id,label,rows)
        self.category_status.setObjectName("mutedLabel")
        count=len(rows); noun="Tag" if count==1 else "Tags"
        self.category_status.setText(
            f"Created {label} in System Knowledge and assigned it to {count:,} selected {noun}. "
            "Save Decisions still controls whether those Tags are remembered."
        )
        self.category_status.show(); self._sync_category_control(); self._update_count()

    def _apply_category(self) -> None:
        category_id=str(self.category.currentData() or "")
        if not category_id: return
        rows=self._category_rows()
        label=self.category.currentText()
        self._apply_category_value(category_id,label,rows)
        self._sync_category_control(); self._update_count()

    def _sync_category_control(self) -> None:
        rows=self.table.selectedItems()
        if not rows and self.table.currentItem() is not None: rows=[self.table.currentItem()]
        self.category.setEnabled(bool(rows))
        categories={str(row.data(0,Qt.UserRole+2) or "") for row in rows}
        if len(categories)==1:
            index=self.category.findData(next(iter(categories)))
            if index>=0: self.category.setCurrentIndex(index); return
        self.category.setCurrentIndex(-1)
        self.category.lineEdit().setPlaceholderText("Choose a category")

    def _update_count(self, *_args) -> None:
        decisions=self.decisions(); added=len(decisions[self.ADD]); aliased=len(decisions[self.ALIAS]); ignored=len(decisions[self.IGNORE]); later=len(decisions[self.LATER])
        self.count.setText(f"Remember {added:,}  •  Alias {aliased:,}  •  Ignore {ignored:,}  •  Later {later:,}")
        self.add_button.setEnabled(bool(added or aliased or ignored))
