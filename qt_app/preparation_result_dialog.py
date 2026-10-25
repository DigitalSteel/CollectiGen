from __future__ import annotations

from pathlib import Path

from PySide6.QtCore import QSize,Qt
from PySide6.QtWidgets import (
    QAbstractItemView,QDialogButtonBox,QHeaderView,QLabel,QPushButton,
    QTableView,QVBoxLayout,
)

from .components import (
    IncrementalResultModel,IncrementalResultRow,bounded_window_size,
    compact_button,counted_noun,
)
from .modal_feedback import FeedbackEnabledDialog


_OUTCOME_LABELS={
    "completed":"Completed",
    "partial":"Partial",
    "skipped":"Already current",
    "conflicted":"Conflict held",
    "failed":"Failed",
}


def should_show_preparation_results(result) -> bool:
    """Keep routine one-image completion in its existing editing surface."""
    if "outcomes" not in result or result.get("preparation_undo_complete"):
        return False
    outcomes=result["outcomes"]
    return not (
        result.get("processed")==1
        and not result.get("cancelled")
        and not result.get("errors")
        and not result.get("recovery_required")
        and len(outcomes)==1
        and outcomes[0].get("status") in {"completed","skipped"}
    )


class PreparationResultDialog(FeedbackEnabledDialog):
    """Bounded, exact-scope result handoff shared by Prepare surfaces."""

    feedback_workflow="Prepare results"

    def __init__(self,result,*,parent=None):
        super().__init__(parent)
        self.result_data=dict(result or {})
        self.review_paths=tuple(dict.fromkeys(
            Path(value) for value in self.result_data.get("review_paths",())
        ))
        self.review_requested=False

        self.setWindowTitle("Prepare results")
        self.resize(980,700)
        layout=QVBoxLayout(self)

        counts=self.result_data.get("outcome_counts",{})
        unfinished=sum(int(counts.get(key,0)) for key in ("failed","partial","conflicted"))
        if self.result_data.get("history_interrupted") or self.result_data.get("recovery_required"):
            title="Preparation needs recovery"
        elif self.result_data.get("cancelled"):
            title="Preparation paused safely"
        elif counts.get("failed",0) and not any(counts.get(key,0) for key in ("completed","partial","skipped","conflicted")):
            title="Preparation failed"
        elif unfinished or self.result_data.get("errors"):
            title="Preparation finished with issues"
        else:
            title="Preparation complete"
        heading=QLabel(title)
        heading.setAccessibleName("Preparation outcome")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)

        summary_lines=[
            str(value) for value in self.result_data.get("summary_lines",())
            if str(value).strip()
        ]
        self.summary=QLabel(
            "\n".join(summary_lines)
            or f"Processed {counted_noun(self.result_data.get('processed',0),'image')}."
        )
        self.summary.setWordWrap(True)
        self.summary.setAccessibleName("Prepare outcome summary")
        layout.addWidget(self.summary)

        self.result_count=QLabel()
        self.result_count.setObjectName("mutedLabel")
        self.result_count.setAccessibleName("Loaded Prepare outcome count")
        layout.addWidget(self.result_count)

        self.results=QTableView()
        self.results.setAccessibleName("Per-image Prepare outcomes")
        self.results.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.results.setSelectionMode(QAbstractItemView.SingleSelection)
        self.results.setAlternatingRowColors(True)
        self.results.verticalHeader().hide()
        self.result_model=IncrementalResultModel(
            ("Image","Outcome","Details"),page_size=200,parent=self,
        )
        self.results.setModel(self.result_model)
        header=self.results.horizontalHeader()
        header.setSectionResizeMode(0,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(1,QHeaderView.ResizeToContents)
        header.setSectionResizeMode(2,QHeaderView.Stretch)
        layout.addWidget(self.results,1)

        self.more_button=compact_button(QPushButton("Show More"))
        self.more_button.setAccessibleName("Show more Prepare outcomes")
        self.more_button.clicked.connect(self.result_model.fetch_more)
        layout.addWidget(self.more_button,0,Qt.AlignRight)

        buttons=QDialogButtonBox(QDialogButtonBox.Close)
        buttons.rejected.connect(self.reject)
        buttons.button(QDialogButtonBox.Close).clicked.connect(self.reject)
        self.review_button=compact_button(buttons.addButton(
            f"Review Remaining Images ({len(self.review_paths):,})…",
            QDialogButtonBox.ActionRole,
        ))
        self.review_button.setAccessibleName("Review remaining prepared images")
        self.review_button.setToolTip(
            "Open only the images from this Prepare run that still have review findings."
        )
        self.review_button.clicked.connect(self._review)
        self.review_button.setVisible(bool(self.review_paths))
        layout.addWidget(buttons)

        self.result_model.counts_changed.connect(self._counts_changed)
        self.result_model.set_rows(self._rows())

    def _rows(self):
        rows=[]
        outcomes=self.result_data.get("outcomes",())
        for index,outcome in enumerate(outcomes if isinstance(outcomes,list) else ()):
            if not isinstance(outcome,dict):
                continue
            image=Path(str(outcome.get("image") or ""))
            status=str(outcome.get("status") or "")
            label=_OUTCOME_LABELS.get(status,status.replace("_"," ").title() or "Unknown")
            detail=str(outcome.get("detail") or "No changes were needed.")
            stable_id=str(image) or f"outcome-{index}"
            rows.append(IncrementalResultRow(
                stable_id=stable_id,
                columns=(image.name or "Unknown image",label,detail),
                tooltip=f"{image}\n{detail}",
                accessible_text=f"{image.name or 'Unknown image'}; {label}; {detail}",
            ))
        return rows

    def _counts_changed(self,loaded: int,total: int):
        self.result_count.setText(
            f"Showing {loaded:,} of {total:,} per-image outcome(s)"
        )
        self.more_button.setVisible(loaded<total)

    def _review(self):
        self.review_requested=True
        self.accept()

    def showEvent(self,event):
        super().showEvent(event)
        screen=self.screen()
        if screen is not None:
            self.resize(bounded_window_size(screen.availableGeometry(),QSize(980,700)))
