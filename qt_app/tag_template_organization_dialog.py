from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,QDialogButtonBox,QHeaderView,QLabel,QMessageBox,QPushButton,QTextEdit,
    QTreeView,QHBoxLayout,QVBoxLayout,
)

from core.tag_template_organization import (
    TAG_TEMPLATE_ORGANIZE_CREATE,TAG_TEMPLATE_ORGANIZE_MOVE,
    TAG_TEMPLATE_ORGANIZE_REUSE,
)
from .components import IncrementalResultModel,IncrementalResultRow
from .modal_feedback import FeedbackEnabledDialog


class TagTemplateOrganizationDialog(FeedbackEnabledDialog):
    """Show one current bounded plan and request its separate atomic apply."""

    feedback_workflow="Preview reusable Tag organization plan"

    def __init__(self,report,parent=None):
        super().__init__(parent)
        self.report=report
        self.create_requested=False
        self.base_approval_requested=False
        self.items=tuple(report.items)
        self._by_id={item.result_id:item for item in self.items}
        self.setWindowTitle("Preview Reusable Tag Organization Plan")
        self.resize(1180,720)
        layout=QVBoxLayout(self)

        heading=QLabel(report.template)
        heading.setObjectName("sectionTitle"); heading.setWordWrap(True)
        layout.addWidget(heading)
        intro=QLabel(
            "This current-authority preview changes nothing. It resolves the reviewed Category Base, proposes or reuses one stable modifier-dimension child Category, and classifies the saved selected results. When every selected result is ready, Create and organize opens a separate exact-scope confirmation before one recoverable System Knowledge operation."
        )
        intro.setObjectName("mutedLabel"); intro.setWordWrap(True)
        layout.addWidget(intro)

        counts=report.action_counts
        self.summary=QLabel(
            f"Saved selected results: {len(report.selected_result_ids):,}. "
            f"Create: {counts.get(TAG_TEMPLATE_ORGANIZE_CREATE,0):,}. "
            f"Move existing: {counts.get(TAG_TEMPLATE_ORGANIZE_MOVE,0):,}. "
            f"Reuse in place: {counts.get(TAG_TEMPLATE_ORGANIZE_REUSE,0):,}. "
            f"Held: {report.held_count:,}. "
            f"Ready expansion results outside the saved selection: {report.unselected_ready_count:,}."
        )
        self.summary.setWordWrap(True)
        self.summary.setAccessibleName("Reusable Tag organization plan summary")
        layout.addWidget(self.summary)

        destination=report.destination
        self.destination=QLabel(
            f"Category Base: {destination.parent_category} ({destination.parent_base_status}). "
            f"Derived destination: {destination.category}. "
            f"Plan: {destination.category_action}."
            +(
                " Review boundary: "+" ".join(destination.hold_reasons)
                if destination.hold_reasons else ""
            )
        )
        self.destination.setWordWrap(True)
        self.destination.setAccessibleName("Reusable Tag derived Category destination")
        layout.addWidget(self.destination)

        self.model=IncrementalResultModel(
            ("Generated Tag","Typed value","Current Category","Plan","Review boundary"),
            page_size=200,parent=self,
        )
        self.model.set_rows(tuple(
            IncrementalResultRow(
                item.result_id,
                (
                    item.tag,item.value,
                    ", ".join(item.current_categories) or "New Tag",
                    item.action_label,item.review_boundary,
                ),
                tooltip=(
                    f"Existing Tag IDs: {', '.join(item.existing_tag_ids) or 'none'}; "
                    f"destination ID: {item.destination_category_id or 'unavailable'}"
                ),
                accessible_text=(
                    f"{item.tag}; {item.value}; {item.action_label}; "
                    f"{item.review_boundary}"
                ),
            ) for item in self.items
        ))
        self.results=QTreeView(); self.results.setModel(self.model)
        self.results.setRootIsDecorated(False); self.results.setUniformRowHeights(True)
        self.results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results.setAccessibleName("Reusable Tag organization plan results")
        header=self.results.header(); header.setStretchLastSection(False)
        header.setSectionResizeMode(0,QHeaderView.Stretch)
        for column,width in ((1,130),(2,230),(3,170),(4,320)):
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

        detail_heading=QLabel("Highlighted plan details")
        detail_heading.setObjectName("sectionTitle"); layout.addWidget(detail_heading)
        self.detail=QTextEdit(); self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(125)
        self.detail.setAccessibleName("Highlighted reusable Tag organization plan details")
        layout.addWidget(self.detail)
        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        self.approve_base_button=None
        if (
            report.destination.proposed_base_relationship
            and not report.destination.reviewed_base_approval
            and report.destination.parent_category_id
            and report.destination.base_match_basis
        ):
            self.approve_base_button=buttons.addButton(
                "Review Exact Category Base…",QDialogButtonBox.ActionRole,
            )
            self.approve_base_button.setAccessibleName(
                "Review exact proposed Category Base relationship"
            )
            self.approve_base_button.clicked.connect(self._request_base_approval)
        self.create_button=buttons.addButton(
            "Create and organize…",QDialogButtonBox.AcceptRole,
        )
        self.create_button.setProperty("primary",True)
        ready=bool(report.items and not report.held_count and report.destination.ready)
        self.create_button.setEnabled(ready)
        self.create_button.setAccessibleName("Create and organize reviewed reusable Tags")
        self.create_button.setToolTip(
            "Confirm and apply the complete selected scope as one recoverable System Knowledge operation."
            if ready else
            "Resolve every held selected result and destination decision before applying this all-or-nothing scope."
        )
        self.create_button.clicked.connect(self._request_create)
        buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.results.selectionModel().currentChanged.connect(self._selection_changed)
        if self.model.rowCount():self.results.setCurrentIndex(self.model.index(0,0))
        else:self._selection_changed()

    def _request_base_approval(self):
        destination=self.report.destination
        if QMessageBox.question(
            self,"Review exact Category Base",
            f"Approve the exact stable anchor as the Category Base for {destination.parent_category}?\n\n"
            f"Match basis: {destination.base_match_basis.replace('_',' ')}\n"
            f"Stable Category: {destination.parent_category_id}\n\n"
            "This records the reviewed proposal on the versioned template only. Reopen the organization preview before any Category, Tag, or relationship write. Image Knowledge and files remain unchanged.",
        )!=QMessageBox.Yes:return
        self.base_approval_requested=True; self.accept()

    def _request_create(self):
        if not self.create_button.isEnabled():return
        counts=self.report.action_counts; destination=self.report.destination
        if QMessageBox.question(
            self,"Create and organize reusable Tags",
            f"Apply all {len(self.report.items):,} reviewed selected result(s)?\n\n"
            f"Create new Tags: {counts.get(TAG_TEMPLATE_ORGANIZE_CREATE,0):,}\n"
            f"Move existing Tags: {counts.get(TAG_TEMPLATE_ORGANIZE_MOVE,0):,}\n"
            f"Reuse in place and verify typed links: {counts.get(TAG_TEMPLATE_ORGANIZE_REUSE,0):,}\n"
            f"Derived Category: {destination.category} ({destination.category_action})\n\n"
            "CollectiGen will revalidate this exact plan, then save Categories, Tags, Broader Tag links, and typed Modifier Value links as one stale-guarded operation with backup, rollback, history, and Undo. Cancellation is available until that transaction begins.\n\n"
            "Image Knowledge, TXT, Metadata, captions, pixels, file locations, projection rules, automatic generation, and fact-expansion policy remain unchanged.",
        )!=QMessageBox.Yes:return
        self.create_requested=True
        self.accept()

    def _counts_changed(self,shown,total):
        self.counts.setText(f"Showing {shown:,} of {total:,} selected plan results")
        self.load_more.setVisible(shown<total); self.load_more.setEnabled(shown<total)

    def _selected(self):
        return self._by_id.get(self.model.stable_id(self.results.currentIndex()))

    def _selection_changed(self,*_):
        item=self._selected()
        if item is None:
            self.detail.setPlainText(
                "This template has no saved selected results. Reopen its family expansion to review and save a bounded creation scope."
            )
            return
        reasons="\n• ".join(item.hold_reasons) if item.hold_reasons else "No category or identity hold was found."
        self.detail.setPlainText(
            f"Rendered canonical name: {item.tag}\n"
            f"Typed value: {item.value} ({item.value_id})\n"
            f"Expansion outcome: {item.expansion_outcome.replace('_',' ').title()}\n"
            f"Existing stable IDs: {', '.join(item.existing_tag_ids) or 'None'}\n"
            f"Current Categories: {', '.join(item.current_categories) or 'New Tag'}\n"
            f"Proposed destination: {item.destination_category}\n"
            f"Planned action: {item.action_label}\n\n"
            f"Review boundary:\n• {reasons}"
        )
