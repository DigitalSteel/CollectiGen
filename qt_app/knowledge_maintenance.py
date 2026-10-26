from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QDialogButtonBox,
    QHeaderView,
    QLabel,
    QTreeWidgetItem,
    QVBoxLayout,
)

from .components import RangeCheckTreeWidget
from .modal_feedback import FeedbackEnabledDialog


class KnowledgeSafeFixReviewDialog(FeedbackEnabledDialog):
    """Review deterministic System Knowledge changes before applying a subset."""

    feedback_workflow = "Knowledge safe-fix review"

    def __init__(self, report, categories, parent=None):
        super().__init__(parent)
        self.report = report
        self.categories = categories
        self.setWindowTitle("Review Safe Fixes")
        self.resize(980, 620)
        layout = QVBoxLayout(self)
        heading = QLabel("Review deterministic System Knowledge fixes")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        intro = QLabel(
            "Check the exact records to change. Nothing is written until Apply Checked "
            "Fixes. The write creates a backup first and never changes Image Knowledge, "
            "captions, Metadata, raw evidence, or image files."
        )
        intro.setWordWrap(True)
        intro.setObjectName("mutedLabel")
        layout.addWidget(intro)
        scope = QLabel(
            "Proposed safe fixes are listed below. Findings that require interpretation "
            "are not automatic and remain in their dedicated Tag Encyclopedia review views."
        )
        scope.setWordWrap(True)
        scope.setObjectName("mutedLabel")
        layout.addWidget(scope)
        self.tree = RangeCheckTreeWidget()
        self.tree.setHeaderLabels(
            ("Apply", "Area", "SDXL Tag or entry", "Proposed change")
        )
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        self.tree.header().setSectionResizeMode(0, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(1, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(2, QHeaderView.ResizeToContents)
        self.tree.header().setSectionResizeMode(3, QHeaderView.Stretch)
        by_tag = {}
        for change in report.changes:
            if not change.tag_id:
                continue
            entry = by_tag.setdefault(
                change.tag_id,
                {"display": change.display, "areas": [], "details": []},
            )
            if change.area not in entry["areas"]:
                entry["areas"].append(change.area)
            detail = change.detail
            if change.area == "Category" and change.target_id:
                detail = (
                    f"Assign reviewed category "
                    f"{categories.path_label(change.target_id)}"
                )
            if detail not in entry["details"]:
                entry["details"].append(detail)
        for tag_id, entry in sorted(
            by_tag.items(), key=lambda item: item[1]["display"].casefold()
        ):
            item = QTreeWidgetItem(
                (
                    "",
                    ", ".join(entry["areas"]),
                    entry["display"],
                    "; ".join(entry["details"]),
                )
            )
            item.setData(0, Qt.UserRole, ("tag", tag_id))
            item.setCheckState(0, Qt.Checked)
            self.tree.addTopLevelItem(item)
        for name in sorted(report.discovery_names, key=str.casefold):
            item = QTreeWidgetItem(
                (
                    "",
                    "Discovery",
                    name,
                    "Remove duplicate discovery entry; the SDXL Tag is already known",
                )
            )
            item.setData(0, Qt.UserRole, ("discovery", name))
            item.setCheckState(0, Qt.Checked)
            self.tree.addTopLevelItem(item)
        layout.addWidget(self.tree, 1)
        self.summary = QLabel()
        self.summary.setObjectName("mutedLabel")
        self.summary.setWordWrap(True)
        layout.addWidget(self.summary)
        buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.apply_button = buttons.addButton(
            "Apply Checked Fixes", QDialogButtonBox.AcceptRole
        )
        self.apply_button.setProperty("primary", True)
        self.apply_button.clicked.connect(self.accept)
        buttons.rejected.connect(self.reject)
        layout.addWidget(buttons)
        self.tree.itemChanged.connect(self._update_summary)
        self._update_summary()

    def _checked_values(self):
        tag_ids = set()
        discoveries = set()
        for index in range(self.tree.topLevelItemCount()):
            item = self.tree.topLevelItem(index)
            if item.checkState(0) != Qt.Checked:
                continue
            kind, value = item.data(0, Qt.UserRole)
            (tag_ids if kind == "tag" else discoveries).add(str(value))
        return tag_ids, discoveries

    def selected_tag_ids(self):
        return self._checked_values()[0]

    def selected_discovery_names(self):
        return self._checked_values()[1]

    def _update_summary(self, *_):
        tag_ids, discoveries = self._checked_values()
        count = len(tag_ids) + len(discoveries)
        self.summary.setText(
            f"{len(tag_ids):,} SDXL Tag record(s) and {len(discoveries):,} duplicate "
            "discovery entry or entries selected. Unchecked changes remain untouched."
        )
        self.apply_button.setEnabled(bool(count))
