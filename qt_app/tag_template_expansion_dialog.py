from __future__ import annotations

from PySide6.QtCore import Qt,Signal
from PySide6.QtWidgets import (
    QAbstractItemView,QDialogButtonBox,QHeaderView,QLabel,QPushButton,QTextEdit,
    QTreeView,QHBoxLayout,QVBoxLayout,
)

from core.tag_template_expansion import (
    TAG_TEMPLATE_ALIAS_EQUIVALENT,TAG_TEMPLATE_EXACT_EXISTING,
    TAG_TEMPLATE_MISSING,
)
from core.tag_template_store import DEFAULT_TAG_TEMPLATE_MAX_PER_RUN
from .components import IncrementalResultModel,IncrementalResultRow
from .modal_feedback import FeedbackEnabledDialog


class TagTemplateExpansionSelectionModel(IncrementalResultModel):
    """Keep explicit stable-ID choices across bounded result paging."""

    selection_changed=Signal(int)

    def __init__(
        self,headers,*,selectable_ids=(),
        max_selected=DEFAULT_TAG_TEMPLATE_MAX_PER_RUN,page_size=200,parent=None,
    ):
        super().__init__(headers,page_size=page_size,parent=parent)
        self._selectable_ids=frozenset(map(str,selectable_ids))
        self.max_selected=max(1,int(max_selected))
        self._selected_ids=set()

    def data(self,index,role=Qt.DisplayRole):
        if (
            role==Qt.CheckStateRole and index.isValid() and index.column()==0
            and self.stable_id(index) in self._selectable_ids
        ):
            return (
                Qt.Checked if self.stable_id(index) in self._selected_ids
                else Qt.Unchecked
            )
        return super().data(index,role)

    def flags(self,index):
        flags=super().flags(index)
        if (
            index.isValid() and index.column()==0
            and self.stable_id(index) in self._selectable_ids
        ):
            flags|=Qt.ItemIsUserCheckable
        return flags

    def setData(self,index,value,role=Qt.EditRole):
        if (
            role!=Qt.CheckStateRole or not index.isValid() or index.column()!=0
            or self.stable_id(index) not in self._selectable_ids
        ):
            return False
        stable_id=self.stable_id(index)
        if value==Qt.Checked or value==Qt.CheckState.Checked:
            if (
                stable_id not in self._selected_ids
                and len(self._selected_ids)>=self.max_selected
            ):
                return False
            self._selected_ids.add(stable_id)
        else:
            self._selected_ids.discard(stable_id)
        self.dataChanged.emit(index,index,[Qt.CheckStateRole])
        self.selection_changed.emit(len(self._selected_ids))
        return True

    @property
    def selected_ids(self):
        return tuple(
            row.stable_id for row in self._rows
            if row.stable_id in self._selected_ids
        )

    def set_selected_ids(self,stable_ids):
        selected=[]; seen=set()
        for stable_id in map(str,stable_ids):
            if stable_id in self._selectable_ids and stable_id not in seen:
                seen.add(stable_id); selected.append(stable_id)
                if len(selected)>=self.max_selected:break
        self._selected_ids=set(selected)
        if self.loaded_count:
            self.dataChanged.emit(
                self.index(0,0),self.index(self.loaded_count-1,0),
                [Qt.CheckStateRole],
            )
        self.selection_changed.emit(len(self._selected_ids))


class TagTemplateExpansionDialog(FeedbackEnabledDialog):
    """Show one bounded reviewed-value expansion without applying it."""

    feedback_workflow="Preview reusable Tag family expansion"

    def __init__(
        self,report,parent=None,*,max_selected=DEFAULT_TAG_TEMPLATE_MAX_PER_RUN,
        initial_selected_ids=(),
    ):
        super().__init__(parent)
        self.report=report
        self.items=tuple(report.items)
        self._by_id={item.id:item for item in self.items}
        self.initial_selected_result_ids=tuple(dict.fromkeys(
            map(str,initial_selected_ids),
        ))
        selectable_ids=tuple(item.id for item in self.items if not item.held)
        selectable_id_set=set(selectable_ids)
        self.unavailable_initial_result_ids=tuple(
            stable_id for stable_id in self.initial_selected_result_ids
            if stable_id not in selectable_id_set
        )
        self.approve_requested=False
        self.selected_result_ids=()
        self.setWindowTitle("Preview Reusable Tag Family Expansion")
        self.resize(1160,720)
        layout=QVBoxLayout(self)

        structured=hasattr(report,"draft")
        bounded=int(getattr(report,"bounded_values",getattr(report,"bounded_results",0)))
        template=(
            report.draft.display_template if structured else report.candidate.template
        )
        heading=QLabel(template)
        heading.setObjectName("sectionTitle"); heading.setWordWrap(True)
        layout.addWidget(heading)
        intro=QLabel(
            "Review possible Tags for identifying images and preparing training captions. "
            "Select ready results you may want to create later. Approving saves a disabled "
            "template; it creates no Tags and changes no Image Knowledge."
        )
        intro.setObjectName("mutedLabel"); intro.setWordWrap(True)
        intro.setToolTip(
            (
                "This preview includes every valid nonempty combination of the ordered "
                "typed selectors, including repeated-slot equality and reviewed constraints. "
                if structured else
                "This preview includes every retained reviewed value in the selected "
                "typed dimension. "
            )
            +"It checks canonical names, declared Other Names, active ignore rules, "
            "and deleted-name history. Selection retains an exact scope for later "
            "creation planning; the saved template is versioned and disabled."
        )
        intro.setAccessibleDescription(intro.toolTip())
        layout.addWidget(intro)
        counts=report.outcome_counts
        self.summary=QLabel(
            f"Potential values: {report.potential_count:,}. "
            f"Exact existing: {counts.get(TAG_TEMPLATE_EXACT_EXISTING,0):,}. "
            f"Alias-equivalent: {counts.get(TAG_TEMPLATE_ALIAS_EQUIVALENT,0):,}. "
            f"New (missing): {counts.get(TAG_TEMPLATE_MISSING,0):,}. "
            f"Ready for later planning: {report.ready_count:,}. Held: {report.held_count:,}."
            +(f" Results outside this bounded preview: {bounded:,}." if bounded else "")
        )
        self.summary.setWordWrap(True)
        self.summary.setAccessibleName("Reusable Tag family expansion summary")
        layout.addWidget(self.summary)

        self.model=TagTemplateExpansionSelectionModel(
            ("Generated Tag","Typed value","Outcome","Existing identity","Review boundary"),
            selectable_ids=selectable_ids,
            max_selected=max_selected,page_size=200,parent=self,
        )
        self.model.set_rows(tuple(
            IncrementalResultRow(
                item.id,
                (
                    item.generated_tag,item.value,item.outcome_label,
                    ", ".join(item.existing_tags) or "None",
                    item.review_boundary,
                ),
                tooltip=(
                    f"Value ID: {item.value_id}; existing Tag IDs: "
                    f"{', '.join(item.existing_tag_ids) or 'none'}; {item.identity_basis}"
                ),
                accessible_text=(
                    f"{item.generated_tag}; {item.value}; {item.outcome_label}; "
                    f"{item.review_boundary}"
                ),
            ) for item in self.items
        ))
        self.model.set_selected_ids(self.initial_selected_result_ids)
        self.results=QTreeView(); self.results.setModel(self.model)
        self.results.setRootIsDecorated(False); self.results.setUniformRowHeights(True)
        self.results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results.setAccessibleName("Reusable Tag family expansion results")
        header=self.results.header(); header.setStretchLastSection(False)
        header.setSectionResizeMode(0,QHeaderView.Stretch)
        for column,width in ((1,130),(2,175),(3,190),(4,300)):
            header.setSectionResizeMode(column,QHeaderView.Interactive)
            self.results.setColumnWidth(column,width)
        layout.addWidget(self.results,3)

        paging=QHBoxLayout()
        self.counts=QLabel(); self.counts.setObjectName("mutedLabel")
        self.load_more=QPushButton("Load More Results")
        self.load_more.setProperty("compact",True)
        self.load_more.clicked.connect(self.model.fetch_more)
        self.model.counts_changed.connect(self._counts_changed)
        paging.addWidget(self.counts); paging.addStretch(1); paging.addWidget(self.load_more)
        layout.addLayout(paging)
        self._counts_changed(self.model.loaded_count,self.model.total_count)

        selection=QHBoxLayout()
        self.selection_count=QLabel()
        self.selection_count.setObjectName("mutedLabel")
        self.selection_count.setAccessibleName("Selected reusable Tag expansion result count")
        self.select_ready=QPushButton("Select All Ready")
        if report.ready_count>self.model.max_selected:
            self.select_ready.setText(
                f"Select Ready Up to {self.model.max_selected:,}"
            )
        self.select_ready.setProperty("compact",True)
        self.select_ready.clicked.connect(lambda:self.model.set_selected_ids(
            item.id for item in self.items if not item.held
        ))
        self.clear_selection=QPushButton("Clear Selection")
        self.clear_selection.setProperty("compact",True)
        self.clear_selection.clicked.connect(lambda:self.model.set_selected_ids(()))
        self.model.selection_changed.connect(self._selection_count_changed)
        selection.addWidget(self.selection_count); selection.addStretch(1)
        selection.addWidget(self.select_ready); selection.addWidget(self.clear_selection)
        layout.addLayout(selection)
        self._selection_count_changed(len(self.model.selected_ids))

        self.saved_selection_status=QLabel()
        self.saved_selection_status.setObjectName("mutedLabel")
        self.saved_selection_status.setWordWrap(True)
        self.saved_selection_status.setAccessibleName(
            "Saved reusable Tag template selection status"
        )
        restored_count=len(self.model.selected_ids)
        unavailable_count=len(self.unavailable_initial_result_ids)
        self.saved_selection_status.setText(
            f"Restored {restored_count:,} ready result(s) from the saved reviewed selection."
            +(
                f" {unavailable_count:,} previously selected result(s) are no longer ready and remain unselected for explicit review."
                if unavailable_count else ""
            )
        )
        self.saved_selection_status.setVisible(bool(self.initial_selected_result_ids))
        layout.addWidget(self.saved_selection_status)

        detail_heading=QLabel("Highlighted result details")
        detail_heading.setObjectName("sectionTitle")
        layout.addWidget(detail_heading)
        self.detail=QTextEdit(); self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(135)
        self.detail.setAccessibleName("Highlighted expansion result details")
        layout.addWidget(self.detail)
        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        self.approve_button=buttons.addButton(
            "Approve Template…",QDialogButtonBox.AcceptRole,
        )
        self.approve_button.setAccessibleName("Approve reviewed reusable Tag template")
        self.approve_button.setEnabled(bool(report.ready_count and not bounded))
        self.approve_button.clicked.connect(self._accept_approval)
        buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.results.selectionModel().currentChanged.connect(self._selection_changed)
        if self.model.rowCount():self.results.setCurrentIndex(self.model.index(0,0))
        else:self._selection_changed()

    def _counts_changed(self,shown,total):
        self.counts.setText(f"Showing {shown:,} of {total:,} expansion results")
        self.load_more.setVisible(shown<total); self.load_more.setEnabled(shown<total)

    def _selected(self):
        return self._by_id.get(self.model.stable_id(self.results.currentIndex()))

    def _selection_changed(self,*_):
        item=self._selected()
        if item is None:
            self.detail.setPlainText("No reviewed values were retained by this bounded preview.")
            return
        reasons="\n• ".join(item.hold_reasons) if item.hold_reasons else "No identity or authority conflict was found."
        self.detail.setPlainText(
            f"Rendered canonical name: {item.generated_tag}\n"
            f"Typed value: {item.value} ({item.value_id})\n"
            f"Identity result: {item.identity_basis}\n"
            f"Existing stable IDs: {', '.join(item.existing_tag_ids) or 'None'}\n"
            f"Current Categories: {', '.join(item.current_categories) or 'No existing record'}\n\n"
            f"Review boundary:\n• {reasons}"
        )

    def _selection_count_changed(self,count):
        self.selection_count.setText(
            f"Selected for later creation planning: {int(count):,} of {self.report.ready_count:,} ready results; per-plan limit {self.model.max_selected:,}."
            +(
                " Limit reached."
                if int(count)>=self.model.max_selected
                and self.report.ready_count>self.model.max_selected else ""
            )
        )

    def _accept_approval(self):
        if not self.approve_button.isEnabled():return
        self.selected_result_ids=self.model.selected_ids
        self.approve_requested=True
        self.accept()
