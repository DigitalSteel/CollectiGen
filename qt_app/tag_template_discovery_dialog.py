from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,QDialogButtonBox,QGroupBox,QHeaderView,QLabel,QTextEdit,
    QHBoxLayout,QPushButton,QTreeView,QVBoxLayout,
)

from .components import IncrementalResultModel,IncrementalResultRow
from .modal_feedback import FeedbackEnabledDialog


class TagTemplateDiscoveryDialog(FeedbackEnabledDialog):
    """Show bounded exact reusable-family candidates without applying them."""

    feedback_workflow="Review reusable Tag family discovery"

    def __init__(self,report,parent=None):
        super().__init__(parent)
        self.report=report
        self.candidates=tuple(report.candidates)
        self._by_id={item.id:item for item in self.candidates}
        self.selected_candidate=None
        self.setWindowTitle("Discover Reusable Tag Families")
        self.resize(1180,760)
        layout=QVBoxLayout(self)

        heading=QLabel("Exact reusable Tag family candidates")
        heading.setObjectName("sectionTitle"); heading.setWordWrap(True)
        layout.addWidget(heading)
        intro=QLabel(
            "This preview changes nothing. It compares canonical Tag names and declared Other Names with reviewed Base Tags, Allowed Details, and typed modifier values. A ready row can open a bounded current-authority expansion preview, where a separate confirmation can approve a disabled template. Tag and Category creation remain unavailable."
        )
        intro.setObjectName("mutedLabel"); intro.setWordWrap(True)
        layout.addWidget(intro)
        self.summary=QLabel(
            f"Candidate families: {len(self.candidates):,}. "
            f"Ready for later review: {len(report.ready_for_review):,}. "
            f"Held for authority review: {len(report.held):,}. "
            f"Processed System Knowledge Tags: {report.processed_tags:,}."
            +(f" Tags outside this bounded pass: {report.bounded_tags:,}." if report.bounded_tags else "")
            +(f" Candidate families not retained by this bounded report: {report.bounded_candidates:,}." if report.bounded_candidates else "")
        )
        self.summary.setWordWrap(True)
        self.summary.setAccessibleName("Reusable Tag family discovery summary")
        layout.addWidget(self.summary)

        self.model=IncrementalResultModel(
            ("Template","Stable anchor","Dimension","Existing records","Review boundary"),
            page_size=200,parent=self,
        )
        self.model.set_rows(tuple(
            IncrementalResultRow(
                item.id,
                (
                    item.template,item.anchor,item.modifier_family,
                    f"{item.matching_record_count:,} record(s) / {len(item.matches):,} decomposition(s)",
                    self._review_boundary(item),
                ),
                tooltip=(
                    f"Anchor stable IDs: {', '.join(item.anchor_ids) or 'Ambiguous'}. "
                    f"Exact token position: {item.token_position}."
                ),
                accessible_text=(
                    f"{item.template}; anchor {item.anchor}; {item.modifier_family}; "
                    f"{item.matching_record_count} matching records; "
                    f"{self._review_boundary(item)}"
                ),
            )
            for item in self.candidates
        ))
        self.results=QTreeView(); self.results.setModel(self.model)
        self.results.setRootIsDecorated(False); self.results.setUniformRowHeights(True)
        self.results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results.setAccessibleName("Ready and held reusable Tag family candidates")
        header=self.results.header(); header.setStretchLastSection(False)
        header.setSectionResizeMode(0,QHeaderView.Stretch)
        for column,width in ((1,195),(2,130),(3,175),(4,220)):
            header.setSectionResizeMode(column,QHeaderView.Interactive)
            self.results.setColumnWidth(column,width)
        layout.addWidget(self.results,2)
        candidate_paging=QHBoxLayout()
        self.candidate_counts=QLabel(); self.candidate_counts.setObjectName("mutedLabel")
        self.candidate_load_more=QPushButton("Load More Families")
        self.candidate_load_more.setProperty("compact",True)
        self.candidate_load_more.clicked.connect(self.model.fetch_more)
        self.model.counts_changed.connect(self._candidate_counts_changed)
        candidate_paging.addWidget(self.candidate_counts); candidate_paging.addStretch(1)
        candidate_paging.addWidget(self.candidate_load_more); layout.addLayout(candidate_paging)
        self._candidate_counts_changed(self.model.loaded_count,self.model.total_count)

        detail_group=QGroupBox("Exact authority boundary")
        detail_layout=QVBoxLayout(detail_group)
        self.detail=QTextEdit(); self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(125)
        self.detail.setAccessibleName("Selected reusable Tag family authority details")
        detail_layout.addWidget(self.detail); layout.addWidget(detail_group)

        match_group=QGroupBox("Every retained exact matching record and decomposition")
        match_layout=QVBoxLayout(match_group)
        self.match_model=IncrementalResultModel(
            ("Existing Tag","Exact matched name","Value","Current Category","Evidence"),
            page_size=200,parent=self,
        )
        self.match_model.counts_changed.connect(self._match_counts_changed)
        self.matches=QTreeView(); self.matches.setModel(self.match_model)
        self.matches.setRootIsDecorated(False); self.matches.setUniformRowHeights(True)
        self.matches.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.matches.setAccessibleName("Exact records matching the selected reusable Tag family")
        match_header=self.matches.header(); match_header.setStretchLastSection(False)
        match_header.setSectionResizeMode(0,QHeaderView.Stretch)
        for column,width in ((1,190),(2,130),(3,220),(4,260)):
            match_header.setSectionResizeMode(column,QHeaderView.Interactive)
            self.matches.setColumnWidth(column,width)
        match_layout.addWidget(self.matches)
        match_paging=QHBoxLayout()
        self.match_counts=QLabel(); self.match_counts.setObjectName("mutedLabel")
        self.match_load_more=QPushButton("Load More Matches")
        self.match_load_more.setProperty("compact",True)
        self.match_load_more.clicked.connect(self.match_model.fetch_more)
        match_paging.addWidget(self.match_counts); match_paging.addStretch(1)
        match_paging.addWidget(self.match_load_more); match_layout.addLayout(match_paging)
        layout.addWidget(match_group,2)

        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        self.preview_button=buttons.addButton(
            "Preview Ready Family…",QDialogButtonBox.AcceptRole,
        )
        self.preview_button.setAccessibleName("Preview selected ready reusable Tag family")
        self.preview_button.clicked.connect(self._accept_selected)
        buttons.rejected.connect(self.reject); layout.addWidget(buttons)
        self.results.selectionModel().currentChanged.connect(self._selection_changed)
        if self.model.rowCount():self.results.setCurrentIndex(self.model.index(0,0))
        else:self._selection_changed()

    def _candidate_counts_changed(self,shown,total):
        self.candidate_counts.setText(f"Showing {shown:,} of {total:,} candidate families")
        self.candidate_load_more.setVisible(shown<total)
        self.candidate_load_more.setEnabled(shown<total)

    def _match_counts_changed(self,shown,total):
        self.match_counts.setText(f"Showing {shown:,} of {total:,} exact decompositions")
        self.match_load_more.setVisible(shown<total)
        self.match_load_more.setEnabled(shown<total)

    @staticmethod
    def _review_boundary(candidate):
        if not candidate.ready_for_review:return "Held: authority review required"
        if candidate.held_match_count:
            return f"Ready; {candidate.held_match_count:,} decomposition(s) held"
        return "Ready for template review"

    def _selected(self):
        return self._by_id.get(self.model.stable_id(self.results.currentIndex()))

    def _selection_changed(self,*_):
        candidate=self._selected()
        self.preview_button.setEnabled(bool(candidate and candidate.ready_for_review))
        if candidate is None:
            self.detail.setPlainText(
                "No exact reusable family candidate was retained by this bounded pass."
            )
            self.match_model.set_rows(())
            self._match_counts_changed(0,0)
            return
        reasons="\n• ".join(candidate.hold_reasons) if candidate.hold_reasons else (
            "No authority conflict was found. This remains a proposal until a later reviewed approval step."
        )
        self.detail.setPlainText(
            f"Stable anchor: {candidate.anchor} ({', '.join(candidate.anchor_ids) or 'ambiguous'})\n"
            f"Anchor evidence: {candidate.anchor_basis}; exact form(s): {', '.join(candidate.anchor_forms)}\n"
            f"Token order: modifier {candidate.token_position} of the stable anchor\n"
            f"Allowed Details source: {candidate.policy_source}\n"
            f"Current Category boundary: {candidate.category_boundary}\n\n"
            f"Review boundary:\n• {reasons}"
        )
        self.match_model.set_rows(tuple(
            IncrementalResultRow(
                f"{candidate.id}:{index}:{item.tag_id}:{item.value_id}",
                (
                    item.tag,item.matched_name,item.value,item.category,
                    (
                        f"{item.name_basis}; {item.base_resolution}; "
                        f"{item.modifier_resolution}"
                        +(f"; held: {'; '.join(item.hold_reasons)}" if item.hold_reasons else "")
                    ),
                ),
                tooltip=(
                    f"Tag ID: {item.tag_id}; modifier value ID: {item.value_id}; "
                    f"Category ID: {item.category_id or 'uncategorized'}"
                ),
            )
            for index,item in enumerate(candidate.matches)
        ))
        self._match_counts_changed(
            self.match_model.loaded_count,self.match_model.total_count,
        )

    def _accept_selected(self):
        candidate=self._selected()
        if candidate is None or not candidate.ready_for_review:return
        self.selected_candidate=candidate
        self.accept()
