from __future__ import annotations

from PySide6.QtCore import QSize, Qt
from PySide6.QtWidgets import (
    QAbstractItemView,
    QComboBox,
    QDialogButtonBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLayout,
    QPushButton,
    QTextEdit,
    QTreeView,
    QVBoxLayout,
)

from core import KnowledgePackExportPlan, KnowledgePackImportPlan

from .components import (
    IncrementalResultModel,
    IncrementalResultRow,
    bounded_window_size,
    counted_noun,
)
from .modal_feedback import FeedbackEnabledDialog


def _selected_review_details(dialog, layout, *, title):
    """Same bounded plain-text pattern as Training's selected finding."""
    details = QTextEdit()
    details.setReadOnly(True)
    details.setAcceptRichText(False)
    details.setAccessibleName(title)
    details.setPlaceholderText("Select a review item to read its full details.")
    details.setMinimumHeight(62)
    details.setMaximumHeight(104)
    layout.addWidget(details)
    dialog.selected_details = details
    def update(*_args):
        index = dialog.table.currentIndex()
        if not index.isValid():
            details.clear()
            return
        def value(column):
            return str(dialog.model.index(index.row(), column).data() or "")
        action_column = 3 if dialog.model.columnCount() == 5 else 2
        details.setPlainText(
            f"{value(0)} — {value(action_column)}\n\n"
            f"{value(dialog.model.columnCount()-1)}\n\nStable ID: {value(1)}"
        )
    dialog.table.selectionModel().currentChanged.connect(update)
    dialog.model.modelReset.connect(update)


class KnowledgePackExportDialog(FeedbackEnabledDialog):
    """Bounded review of content boundary and exact portable rule effects."""

    feedback_workflow = "Knowledge Pack export review"
    PAGE_SIZE = 200

    def __init__(self, plan: KnowledgePackExportPlan, parent=None) -> None:
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle("Review Knowledge Pack Export")
        self.resize(1080, 680)

        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        heading = QLabel("Share reusable image-identification Knowledge")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        scope = QLabel("Exports all reviewed custom Knowledge for image identification and training captions. Images, per-image facts and built-in Knowledge are not included.")
        scope.setWordWrap(True)
        layout.addWidget(scope)

        summary = plan.summary_dict()
        total = sum(summary.values())
        self.summary = QLabel(
            f"{counted_noun(total,'portable record')}  •  "
            f"{counted_noun(len(plan.content_findings),'content-boundary finding')}  •  "
            f"{counted_noun(len(plan.rule_effects),'exact Knowledge rule effect')}"
        )
        self.summary.setWordWrap(True)
        self.summary.setAccessibleName(self.summary.text().replace("•", ";"))
        layout.addWidget(self.summary)

        boundary_label = plan.content_rating_boundary.replace("_", " ").title()
        conservative = sum(
            "conservative Explicit boundary" in item.basis
            for item in plan.content_findings
        )
        boundary_detail = (
            f" {conservative:,} item(s) lack a complete reviewed lower rating, so the pack stays Explicit."
            if conservative else
            " Every portable content reference is covered by the displayed reviewed boundary."
        )
        self.boundary = QLabel(
            f"Verified content-rating boundary: {boundary_label}." + boundary_detail
        )
        self.boundary.setObjectName("mutedLabel")
        self.boundary.setWordWrap(True)
        layout.addWidget(self.boundary)

        if plan.issues:
            issue_text = QLabel("Export blocked: " + " ".join(plan.issues))
            issue_text.setObjectName("errorLabel")
            issue_text.setWordWrap(True)
            layout.addWidget(issue_text)

        self.model = IncrementalResultModel(
            ("Review item", "Stable ID", "Pack action", "Basis / consequence"),
            page_size=self.PAGE_SIZE,
            parent=self,
        )
        rows = [
            IncrementalResultRow(
                f"content:{index}:{finding.stable_id}",
                (
                    finding.label,
                    finding.stable_id,
                    f"Content: {finding.rating.title()}",
                    finding.basis,
                ),
                tooltip=finding.basis,
            )
            for index, finding in enumerate(plan.content_findings)
        ]
        rows.extend(
            IncrementalResultRow(
                effect.stable_id,
                (effect.label, effect.stable_id, effect.action, effect.consequence),
                tooltip=effect.consequence,
            )
            for effect in plan.rule_effects
        )
        self.model.set_rows(rows)
        self.table = QTreeView()
        self.table.setModel(self.model)
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAccessibleName(
            "Knowledge Pack content boundary and exact rule consequences"
        )
        header = self.table.header()
        header.setMinimumSectionSize(100)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        for column in (0, 1, 3):
            header.setSectionResizeMode(column, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        self.table.setColumnHidden(1, True)
        _selected_review_details(self, layout, title="Selected export item: full ID and consequence")

        result_row = QHBoxLayout()
        self.result_status = QLabel()
        self.result_status.setObjectName("mutedLabel")
        self.load_more = QPushButton("Load More…")
        self.load_more.setProperty("compact", True)
        self.load_more.clicked.connect(self.model.fetch_more)
        result_row.addWidget(self.result_status, 1)
        result_row.addWidget(self.load_more)
        layout.addLayout(result_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.export_button = buttons.button(QDialogButtonBox.Ok)
        self.export_button.setText("Export Pack")
        self.export_button.setToolTip("Export the complete reviewed pack. Selecting a row does not limit its scope.")
        self.export_button.setProperty("primary", True)
        self.export_button.setEnabled(plan.can_export)
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.model.counts_changed.connect(self._update_counts)
        self._update_counts(self.model.loaded_count, self.model.total_count)

    def showEvent(self, event) -> None:
        screen = self.screen()
        if screen is not None:
            self.resize(
                bounded_window_size(screen.availableGeometry(), QSize(1080, 680))
            )
        super().showEvent(event)

    def _update_counts(self, shown: int, total: int) -> None:
        if not total:
            self.result_status.setText(
                "This pack has no content-boundary or Knowledge-rule review rows."
            )
        elif shown < total:
            self.result_status.setText(
                f"Showing {shown:,} of {total:,} exact content and rule review rows."
            )
        else:
            self.result_status.setText(
                f"Showing all {total:,} exact content and rule review rows."
            )
        self.load_more.setVisible(shown < total)


class KnowledgePackImportDialog(FeedbackEnabledDialog):
    """Bounded, exact preview for one stale-guarded Knowledge Pack import."""

    feedback_workflow = "Knowledge Pack import preview"
    PAGE_SIZE = 200

    def __init__(self, plan: KnowledgePackImportPlan, parent=None) -> None:
        super().__init__(parent)
        self.plan = plan
        self.setWindowTitle("Review Knowledge Pack Import")
        self.resize(1080, 680)

        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        heading = QLabel("Review reusable image-identification Knowledge")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        scope = QLabel("Imports reusable Knowledge for image identification and training captions, not images or per-image facts. Selecting or filtering rows does not limit the import.")
        scope.setWordWrap(True)
        layout.addWidget(scope)

        summary = plan.summary_dict()
        total = sum(summary.values())
        holds = sum(
            item.outcome == "hold" for item in plan.relationship_decisions
        ) + sum(
            item.outcome == "hold" for item in plan.record_decisions
        ) + sum(
            item.outcome == "hold" for item in plan.removal_decisions
        )
        changes = sum(
            item.outcome in {"add", "override", "disable_default"}
            for item in plan.relationship_decisions
        ) + sum(
            item.outcome in {"add", "override", "disable_default"}
            for item in plan.record_decisions
        ) + sum(
            item.outcome == "add" for item in plan.removal_decisions
        )
        self.summary = QLabel(
            f"{counted_noun(total,'portable record')} in this pack  •  "
            f"{counted_noun(changes,'reviewed change')}  •  {counted_noun(holds,'hold')}"
        )
        self.summary.setWordWrap(True)
        self.summary.setAccessibleName(self.summary.text().replace("•", ";"))
        layout.addWidget(self.summary)
        type_labels = (
            ("tags", "Custom Tags"),
            ("categories", "Categories"),
            ("characters", "Characters"),
            ("series", "Series"),
            ("outfits", "Outfits"),
            ("patterns", "Patterns"),
            ("merge_rules", "Knowledge rules"),
            ("consumer_policies", "Consumer policies"),
            ("relationships", "Canonical relationships"),
            ("removed_tag_archives", "Removed vocabulary batches"),
            ("legacy_removed_tag_ids", "Legacy removal decisions"),
        )
        contents = [
            f"{label}: {int(summary.get(key, 0)):,}"
            for key, label in type_labels
            if int(summary.get(key, 0))
        ]
        self.contents = QLabel(
            "Contents: " + ("  •  ".join(contents) if contents else "No records")
        )
        self.contents.setObjectName("mutedLabel")
        self.contents.setWordWrap(True)
        self.contents.setAccessibleName(self.contents.text().replace("•", ";"))
        layout.addWidget(self.contents)

        boundary_label = (
            plan.content_rating_boundary.replace("_", " ").title()
            if plan.content_rating_boundary
            else "Not declared"
        )
        boundary = QLabel(
            f"Content-rating boundary: {boundary_label}; "
            f"{counted_noun(len(plan.content_findings),'content-rating finding')} inspected.\n"
            "Your existing custom Knowledge takes priority. Import checks the reviewed files again and keeps recovery backups; uncertain changes stop for review."
        )
        boundary.setObjectName("mutedLabel")
        boundary.setWordWrap(True)
        layout.addWidget(boundary)
        self.readiness = QLabel(
            f"Decisions needing attention: {holds:,}. Choose Needs attention to inspect the reasons."
            if holds else "Ready to import the reviewed pack." if plan.can_apply else
            "Import blocked. Read the whole-pack reasons below."
        )
        self.readiness.setWordWrap(True)
        layout.addWidget(self.readiness)

        if plan.issues:
            issue_text = QTextEdit()
            issue_text.setReadOnly(True)
            issue_text.setAcceptRichText(False)
            issue_text.setPlainText("Import blocked:\n" + "\n".join(plan.issues))
            issue_text.setAccessibleName("Whole-pack import blockers")
            issue_text.setMaximumHeight(80)
            layout.addWidget(issue_text)
            self.issue_details = issue_text

        self.model = IncrementalResultModel(
            ("Record", "Stable ID", "Type", "Outcome", "Details"),
            page_size=self.PAGE_SIZE,
            parent=self,
        )
        rows = [
            IncrementalResultRow(
                decision.stable_id,
                (
                    decision.label,
                    decision.stable_id,
                    decision.kind.replace("_", " ").title(),
                    {
                        "add": "Add",
                        "override": "Override default",
                        "disable_default": "Disable default",
                        "already_present": "Already present",
                        "hold": "Hold",
                    }.get(decision.outcome, decision.outcome.replace("_", " ").title()),
                    decision.reason + (
                        " Exact consequence: " + decision.consequence
                        if decision.consequence else ""
                    ),
                ),
                tooltip=decision.reason + (
                    "\n\nExact consequence: " + decision.consequence
                    if decision.consequence else ""
                ),
                foreground="#b42318" if decision.outcome == "hold" else "",
            )
            for decision in plan.record_decisions
        ]
        rows.extend(
            IncrementalResultRow(
                decision.stable_id,
                (
                    decision.label,
                    decision.stable_id,
                    decision.kind.replace("_", " ").title(),
                    {
                        "add": "Add",
                        "override": "Override default",
                        "disable_default": "Disable default",
                        "already_present": "Already present",
                        "hold": "Hold",
                    }.get(decision.outcome, decision.outcome.replace("_", " ").title()),
                    decision.reason,
                ),
                tooltip=decision.reason,
                foreground="#b42318" if decision.outcome == "hold" else "",
            )
            for decision in plan.relationship_decisions
        )
        rows.extend(
            IncrementalResultRow(
                decision.stable_id,
                (
                    decision.label,
                    decision.stable_id,
                    "Removed Vocabulary",
                    {
                        "add": "Add recovery batch",
                        "already_present": "Already present",
                        "hold": "Hold",
                    }.get(decision.outcome, decision.outcome.replace("_", " ").title()),
                    decision.reason,
                ),
                tooltip=decision.reason,
                foreground="#b42318" if decision.outcome == "hold" else "",
            )
            for decision in plan.removal_decisions
        )
        rows.extend(
            IncrementalResultRow(
                f"content:{index}:{finding.stable_id}",
                (
                    finding.label,
                    finding.stable_id,
                    "Content Boundary Evidence",
                    finding.rating.title(),
                    finding.basis,
                ),
                tooltip=finding.basis,
            )
            for index, finding in enumerate(plan.content_findings)
        )
        self.model.set_rows(rows)
        self._all_review_rows = tuple(rows)
        self.review_filter = QComboBox()
        self.review_filter.setAccessibleName("Inspect pack decisions")
        self.review_filter.addItems(("All decisions", f"Needs attention ({holds:,})"))
        layout.addWidget(self.review_filter)

        self.table = QTreeView()
        self.table.setModel(self.model)
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.SingleSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAccessibleName(
            "Knowledge Pack import decisions with reasons and exact rule consequences"
        )
        header = self.table.header()
        header.setMinimumSectionSize(100)
        for column in (2, 3):
            header.setSectionResizeMode(column, QHeaderView.ResizeToContents)
        for column in (0, 1, 4):
            header.setSectionResizeMode(column, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)
        self.table.setColumnHidden(1, True)
        _selected_review_details(self, layout, title="Selected import decision: full ID, reason and consequence")
        self.review_filter.currentIndexChanged.connect(self._filter_review)

        result_row = QHBoxLayout()
        self.result_status = QLabel()
        self.result_status.setObjectName("mutedLabel")
        self.load_more = QPushButton("Load More…")
        self.load_more.setProperty("compact", True)
        self.load_more.clicked.connect(self.model.fetch_more)
        result_row.addWidget(self.result_status, 1)
        result_row.addWidget(self.load_more)
        layout.addLayout(result_row)

        buttons = QDialogButtonBox(QDialogButtonBox.Ok | QDialogButtonBox.Cancel)
        self.apply_button = buttons.button(QDialogButtonBox.Ok)
        self.apply_button.setText("Import Pack")
        self.apply_button.setProperty("primary", True)
        self.apply_button.setEnabled(plan.can_apply)
        self.apply_button.setToolTip(
            "Resolve every hold before applying the pack."
            if not plan.can_apply
            else "Import the complete reviewed pack. Selecting or filtering rows does not change the scope."
        )
        buttons.accepted.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)

        self.model.counts_changed.connect(self._update_counts)
        self._update_counts(self.model.loaded_count, self.model.total_count)

    def showEvent(self, event) -> None:
        screen = self.screen()
        if screen is not None:
            self.resize(
                bounded_window_size(screen.availableGeometry(), QSize(1080, 680))
            )
        super().showEvent(event)

    def _update_counts(self, shown: int, total: int) -> None:
        needs_attention = self.review_filter.currentIndex() == 1
        if not total:
            self.result_status.setText(
                "No items need attention in this view. Whole-pack blockers are shown above."
                if self.review_filter.currentIndex() == 1 else
                "This pack has no record, relationship, or removed-vocabulary decisions."
            )
        elif shown < total:
            self.result_status.setText(
                f"Showing {shown:,} of {total:,} items needing attention."
                if needs_attention else f"Showing {shown:,} of {total:,} import decisions."
            )
        else:
            self.result_status.setText(
                f"Showing all {total:,} items needing attention; the whole-pack scope is unchanged."
                if needs_attention else f"Showing all {total:,} import decisions."
            )
        self.load_more.setVisible(shown < total)
        if shown < total:
            self.load_more.setText(
                f"Load {min(self.PAGE_SIZE, total - shown):,} More…"
            )

    def _filter_review(self, index: int) -> None:
        self.selected_details.clear()
        self.model.set_rows(
            [row for row in self._all_review_rows if row.columns[3] == "Hold"]
            if index == 1 else self._all_review_rows
        )
