from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,QDialogButtonBox,QGroupBox,QHeaderView,
    QLabel,QTextEdit,QTreeView,QVBoxLayout,
)

from .components import IncrementalResultModel,IncrementalResultRow
from .modal_feedback import FeedbackEnabledDialog


class TagBroaderDiscoveryDialog(FeedbackEnabledDialog):
    """Inspect new ready and held exact Broader Tag findings."""

    feedback_workflow="Review Broader Tag discovery"

    def __init__(
        self, report, ready_candidates, held_candidates=None, parent=None,
    ):
        super().__init__(parent)
        self.report=report
        self.ready=tuple(ready_candidates)
        self.held=tuple(
            getattr(report,"held_candidates",())
            if held_candidates is None else held_candidates
        )
        self.candidates=(*self.ready,*self.held)
        self._by_source_key={item.source_key:item for item in self.candidates}
        self._selected_source_key=""
        self.setWindowTitle("Review Broader Tag Findings")
        self.resize(1160,720)
        layout=QVBoxLayout(self)
        heading=QLabel("Review exact Broader Tag findings")
        heading.setObjectName("sectionTitle"); heading.setWordWrap(True)
        layout.addWidget(heading)
        intro=QLabel(
            "This report changes nothing. Select one ready or held finding to queue its exact Review Center question. Held findings require an additional Category-impact confirmation before the stale-safe relationship transaction can save anything."
        )
        intro.setObjectName("mutedLabel"); intro.setWordWrap(True)
        layout.addWidget(intro)
        self.summary=QLabel(
            f"New ready proposals: {len(self.ready):,}. "
            f"Held for broader Category review: {len(self.held):,}. "
            f"Processed SDXL Tags: {report.processed_tags:,}."
            +(f" Exact findings not retained: {report.bounded_candidates:,}." if report.bounded_candidates else "")
        )
        self.summary.setWordWrap(True)
        self.summary.setAccessibleName("Broader Tag discovery summary")
        layout.addWidget(self.summary)

        self.model=IncrementalResultModel(
            (
                "SDXL Tag","Proposed Broader Tag","Pattern evidence",
                "Review boundary","Category impact",
            ),
            page_size=200,parent=self,
        )
        self.model.set_rows(tuple(
            IncrementalResultRow(
                item.source_key,
                (
                    item.tag,item.target,item.pattern_name,
                    self._review_boundary(item),self._category_impact(item),
                ),
                tooltip=item.basis,
                accessible_text=(
                    f"{item.tag}; proposed Broader Tag {item.target}; "
                    f"{item.pattern_name}; {self._review_boundary(item)}; "
                    f"{self._category_impact(item)}"
                ),
            )
            for item in self.candidates
        ))
        self.results=QTreeView(); self.results.setModel(self.model)
        self.results.setRootIsDecorated(False); self.results.setUniformRowHeights(True)
        self.results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results.setAccessibleName("Ready and held Broader Tag findings")
        header=self.results.header(); header.setStretchLastSection(False)
        header.setSectionResizeMode(0,QHeaderView.Stretch)
        for column,width in ((1,190),(2,220),(3,235),(4,250)):
            header.setSectionResizeMode(column,QHeaderView.Interactive)
            self.results.setColumnWidth(column,width)
        layout.addWidget(self.results,1)

        detail_group=QGroupBox("Exact evidence and downstream boundary")
        detail_layout=QVBoxLayout(detail_group)
        self.detail=QTextEdit(); self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(175)
        self.detail.setAccessibleName("Selected Broader Tag finding details")
        detail_layout.addWidget(self.detail); layout.addWidget(detail_group)

        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        self.queue_button=buttons.addButton(
            "Queue Selected Proposal…",QDialogButtonBox.AcceptRole,
        )
        self.queue_button.setEnabled(False)
        self.queue_button.setAccessibleDescription(
            "Queue only the selected exact Broader Tag finding for explicit Review Center approval. Held findings receive an additional Category-impact confirmation there."
        )
        buttons.accepted.connect(self._queue_selected)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.results.selectionModel().currentChanged.connect(
            self._selection_changed,
        )
        self.results.doubleClicked.connect(lambda *_:self._queue_selected())

    @staticmethod
    def _review_boundary(candidate):
        return {
            "ready":"Ready to queue",
            "category_change_required":"Held: Category move required",
            "broader_target_outside_category_path":(
                "Held: target outside Category path"
            ),
        }.get(candidate.review_boundary,"Held: broader review required")

    @staticmethod
    def _category_impact(candidate):
        if candidate.review_boundary=="category_change_required":
            return (
                f"{candidate.category_path} → "
                f"{candidate.proposed_category_path or candidate.proposed_category_id}"
            )
        return (
            f"Source: {candidate.category_path}; "
            f"target: {candidate.target_category_path}"
        )

    def _selected(self):
        return self._by_source_key.get(
            self.model.stable_id(self.results.currentIndex()),
        )

    def _selection_changed(self,*_):
        candidate=self._selected()
        self.queue_button.setEnabled(candidate is not None)
        self.queue_button.setText(
            "Queue Selected Proposal…"
            if candidate is not None and candidate.independently_safe
            else "Queue Held Finding for Review…"
            if candidate is not None
            else "Queue Selected Proposal…"
        )
        if candidate is None:
            self.detail.setPlainText(
                "Select one exact finding to inspect its current and proposed authority."
            )
            return
        downstream=(
            "Would make the target available to related-Tag traversal, semantic inheritance, and opt-in broader-Tag export without replacing the source Tag.\n"
            "Would not confirm a Tag on an image or rewrite Image Knowledge, captions, TXT, Metadata, or pixels."
        )
        self.detail.setPlainText(
            f"Before: no active canonical Broader Tag for {candidate.tag}.\n"
            f"After review: {candidate.tag} → {candidate.target}.\n"
            f"Exact pattern: {candidate.pattern_name} ({candidate.pattern_id})\n"
            f"Pattern source: {candidate.pattern_source.title()}\n"
            f"Ranking confidence: {candidate.confidence:.0%}\n"
            f"Evidence: {candidate.basis}\n"
            f"Review boundary: {self._review_boundary(candidate)}\n"
            f"Category impact: {self._category_impact(candidate)}\n\n"
            f"Downstream effects:\n{downstream}"
        )

    def _queue_selected(self):
        candidate=self._selected()
        if candidate is None:
            return
        self._selected_source_key=candidate.source_key
        self.accept()

    def selected_candidate(self):
        return self._by_source_key.get(self._selected_source_key)


__all__=["TagBroaderDiscoveryDialog"]
