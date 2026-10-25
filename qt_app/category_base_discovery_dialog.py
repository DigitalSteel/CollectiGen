from __future__ import annotations

from PySide6.QtWidgets import (
    QAbstractItemView,QDialogButtonBox,QGroupBox,QHeaderView,
    QLabel,QTextEdit,QTreeView,QVBoxLayout,
)

from .components import IncrementalResultModel,IncrementalResultRow
from .modal_feedback import FeedbackEnabledDialog


class CategoryBaseDiscoveryDialog(FeedbackEnabledDialog):
    """Inspect ready and held exact Category Base candidates."""

    feedback_workflow="Review Category Base discovery"

    def __init__(self,report,parent=None):
        super().__init__(parent)
        self.report=report
        self.candidates=tuple(report.candidates)
        self._by_id={item.category_id:item for item in self.candidates}
        self._selected_category_id=""
        self.setWindowTitle("Review Category Base Candidates")
        self.resize(1160,720)
        layout=QVBoxLayout(self)
        heading=QLabel("Review exact Category Base candidates")
        heading.setObjectName("sectionTitle"); heading.setWordWrap(True)
        layout.addWidget(heading)
        intro=QLabel(
            "This report changes nothing. Every exact candidate can open the existing Category editor. Ready candidates use the narrow unchanged-target path; held candidates require an explicit new-target confirmation and a direct-or-descendant scope choice before anything can be saved."
        )
        intro.setObjectName("mutedLabel"); intro.setWordWrap(True)
        layout.addWidget(intro)
        broader=len(report.broader_scope_required)
        self.summary=QLabel(
            f"Exact candidates: {len(self.candidates):,}. "
            f"Behavior-preserving leaf candidates ready to apply: {len(report.independently_safe):,}. "
            f"Held for explicit direct/descendant or new-target review: {broader:,}. "
            f"Processed Categories: {report.processed_categories:,}."
            +(f" Exact candidates not retained by this bounded report: {report.bounded_candidates:,}." if report.bounded_candidates else "")
        )
        self.summary.setWordWrap(True); self.summary.setAccessibleName("Category Base candidate summary")
        layout.addWidget(self.summary)

        self.model=IncrementalResultModel(
            (
                "Category","Proposed Base SDXL Tag","Exact evidence",
                "Review boundary","Member impact",
            ),
            page_size=200,parent=self,
        )
        self.model.set_rows(tuple(
            IncrementalResultRow(
                item.category_id,
                (
                    item.category_path,item.target,item.evidence_label,
                    self._review_boundary(item),
                    (
                        f"{item.affected_direct_member_count:,} direct; "
                        f"{item.descendant_member_count:,} descendant members"
                    ),
                ),
                tooltip=f"{item.before} {item.after}",
                accessible_text=(
                    f"{item.category_path}; proposed Base {item.target}; "
                    f"{item.evidence_label}; {self._review_boundary(item)}; "
                    f"{item.affected_direct_member_count} affected direct members; "
                    f"{item.descendant_member_count} descendant members"
                ),
            )
            for item in self.candidates
        ))
        self.results=QTreeView(); self.results.setModel(self.model)
        self.results.setRootIsDecorated(False); self.results.setUniformRowHeights(True)
        self.results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results.setAccessibleName("Ready and held Category Base candidates")
        header=self.results.header(); header.setStretchLastSection(False)
        header.setSectionResizeMode(0,QHeaderView.Stretch)
        for column,width in ((1,190),(2,185),(3,235),(4,210)):
            header.setSectionResizeMode(column,QHeaderView.Interactive)
            self.results.setColumnWidth(column,width)
        layout.addWidget(self.results,1)

        detail_group=QGroupBox("Before-and-After Preview")
        detail_layout=QVBoxLayout(detail_group)
        self.detail=QTextEdit(); self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(150)
        self.detail.setAccessibleName("Selected Category Base candidate details")
        detail_layout.addWidget(self.detail); layout.addWidget(detail_group)

        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        self.open_button=buttons.addButton(
            "Open Category Base Editor…",QDialogButtonBox.AcceptRole,
        )
        self.open_button.setEnabled(False)
        self.open_button.setAccessibleDescription(
            "Open the exact Category in its existing editor. Held candidates require a broader impact and scope confirmation there."
        )
        buttons.accepted.connect(self._open_selected); buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.results.selectionModel().currentChanged.connect(self._selection_changed)
        self.results.doubleClicked.connect(lambda *_:self._open_selected())

    @staticmethod
    def _review_boundary(candidate):
        if candidate.independently_safe:return "Ready to apply"
        if not candidate.effective_target_unchanged and candidate.descendant_category_count:
            return "Held: new target + descendant scope"
        if not candidate.effective_target_unchanged:return "Held: new effective target"
        return "Held: descendant scope decision"

    def _selected(self):
        return self._by_id.get(self.model.stable_id(self.results.currentIndex()))

    def _selection_changed(self,*_):
        candidate=self._selected()
        ready=bool(candidate is not None and candidate.independently_safe)
        self.open_button.setEnabled(candidate is not None)
        self.open_button.setText(
            "Open Category Base Editor…" if ready else "Review Held Candidate…"
        )
        if candidate is None:
            self.detail.setPlainText("Select one exact candidate to inspect its current and proposed authority.")
            return
        effects="\n• ".join(candidate.downstream_effects)
        self.detail.setPlainText(
            f"Before: {candidate.before}\n"
            f"After: {candidate.after}\n"
            f"Direct members affected: {candidate.affected_direct_member_count:,} of {candidate.direct_member_count:,}\n"
            f"Review boundary: {self._review_boundary(candidate)}\n"
            f"Descendant scope: {candidate.descendant_category_count:,} Categories / {candidate.descendant_member_count:,} members\n"
            f"Existing Tag-specific Broader Tags retained: {candidate.tag_specific_base_override_count:,}\n\n"
            f"Downstream effects:\n• {effects}"
        )

    def _open_selected(self):
        candidate=self._selected()
        if candidate is None:return
        self._selected_category_id=candidate.category_id
        self.accept()

    def selected_candidate(self):
        return self._by_id.get(self._selected_category_id)
