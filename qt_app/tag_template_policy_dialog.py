from __future__ import annotations

from PySide6.QtWidgets import (
    QCheckBox,QDialogButtonBox,QLabel,QMessageBox,QVBoxLayout,
)
from .status_notifications import FooterMessageBox as QMessageBox

from core.tag_template_policy import TagTemplatePolicyService
from .modal_feedback import FeedbackEnabledDialog


class TagTemplatePolicyDialog(FeedbackEnabledDialog):
    """Review per-template generation and fact-association policies separately."""

    feedback_workflow="Review reusable Tag automation policies"

    def __init__(self,record,plan,parent=None):
        super().__init__(parent); self.record=record; self.plan=plan
        self.setWindowTitle("Review Reusable Tag Automation Policies")
        self.resize(650,390)
        layout=QVBoxLayout(self)
        heading=QLabel(record.template); heading.setObjectName("sectionTitle")
        heading.setWordWrap(True); layout.addWidget(heading)
        intro=QLabel(
            "These two template policies are independent. Generation may create or organize only the saved selected results through the recoverable System Knowledge transaction. Fact expansion may only suggest reviewed materialized template associations in an exact character, object, context, or image scope; the normal image Save Changes step remains required."
        )
        intro.setWordWrap(True); intro.setObjectName("mutedLabel"); layout.addWidget(intro)
        counts=plan.action_counts
        summary=QLabel(
            f"Reviewed selected scope: {len(record.selected_result_ids):,} of {record.max_per_run:,} maximum. "
            f"Create: {counts.get('create_tag',0):,}; move: {counts.get('move_existing',0):,}; "
            f"reuse: {counts.get('reuse_in_place',0):,}; held: {plan.held_count:,}."
        )
        summary.setWordWrap(True); summary.setAccessibleName(
            "Reusable Tag automation policy scope"
        ); layout.addWidget(summary)
        self.generation=QCheckBox(
            "Enable reviewed automatic generation for this exact template scope"
        )
        self.generation.setChecked(bool(record.generation_enabled))
        self.generation.setEnabled(bool(record.selected_result_ids and not plan.held_count))
        self.generation.setAccessibleName("Enable reusable Tag generation")
        layout.addWidget(self.generation)
        generation_note=QLabel(
            "Generation is bounded by this template's saved selection and per-run cap. It never changes Image Knowledge, TXT, Metadata, captions, pixels, or file locations."
        )
        generation_note.setWordWrap(True); generation_note.setObjectName("mutedLabel")
        layout.addWidget(generation_note)
        self.fact_expansion=QCheckBox(
            "Enable reviewed association suggestions for this exact template scope"
        )
        self.fact_expansion.setChecked(bool(record.fact_expansion_enabled))
        self.fact_expansion.setEnabled(bool(record.selected_result_ids))
        self.fact_expansion.setAccessibleName("Enable reusable Tag fact expansion")
        layout.addWidget(self.fact_expansion)
        fact_note=QLabel(
            "Suggestions require exact owner scope and respect confirmed, absent, and rejected decisions. They are review-first unless both this template and the global deterministic expansion policy allow automatic staging."
        )
        fact_note.setWordWrap(True); fact_note.setObjectName("mutedLabel")
        layout.addWidget(fact_note)
        self.buttons=QDialogButtonBox(
            QDialogButtonBox.Save|QDialogButtonBox.Cancel
        )
        self.buttons.button(QDialogButtonBox.Save).setText("Save Reviewed Policies")
        self.buttons.accepted.connect(self._accept); self.buttons.rejected.connect(self.reject)
        layout.addWidget(self.buttons)

    @property
    def preview(self):
        return TagTemplatePolicyService.preview(
            self.record,self.plan,generation_enabled=self.generation.isChecked(),
            fact_expansion_enabled=self.fact_expansion.isChecked(),
        )

    def _accept(self):
        try:preview=self.preview
        except (ValueError,RuntimeError) as exc:
            QMessageBox.warning(self,"Policies need review",str(exc)); return
        if preview.generation_enabled and not preview.ready_for_generation:
            QMessageBox.warning(
                self,"Generation is held",
                "Resolve the complete selected organization plan before enabling generation.",
            ); return
        self.accept()


__all__=["TagTemplatePolicyDialog"]
