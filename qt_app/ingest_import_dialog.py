from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QCheckBox,
    QComboBox,
    QDialogButtonBox,
    QFrame,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QPlainTextEdit,
    QPushButton,
    QTreeWidget,
    QTreeWidgetItem,
    QVBoxLayout,
)

from core import IngestImportAction, IngestImportPlan

from .components import DisclosureButton
from .library_move_dialog import transfer_size_text
from .modal_feedback import FeedbackEnabledDialog


class IngestImportPreviewDialog(FeedbackEnabledDialog):
    """Exact Copy-to-Ingest versus Move-to-Ingest decision."""

    feedback_workflow = "Add Images preview"
    preview_page_size = 250

    def __init__(self, plan: IngestImportPlan, parent=None, *, fixed_action: IngestImportAction | None = None) -> None:
        super().__init__(parent)
        self.plan = plan
        self.fixed_action = fixed_action
        self.preview_page = 0
        self.setWindowTitle("Review Remaining Images" if fixed_action is not None else "Preview Add Images")
        self.resize(980, 650)
        layout = QVBoxLayout(self)

        destination = plan.destination_profile
        destination_panel = QFrame()
        destination_panel.setObjectName("statusActionCard")
        destination_layout = QVBoxLayout(destination_panel)
        destination_layout.setContentsMargins(10, 7, 10, 7)
        destination_layout.setSpacing(2)
        destination_name = destination.name if destination is not None else "Ingest"
        destination_role = destination.display_role if destination is not None else plan.destination_role
        destination_root = destination.root if destination is not None else plan.ingest_root
        self.destination_heading = QLabel(f"Destination: {destination_name}")
        self.destination_heading.setObjectName("metricValue")
        destination_layout.addWidget(self.destination_heading)
        self.destination_details = QLabel(f"{destination_role}  •  {destination_root}")
        self.destination_details.setWordWrap(True)
        self.destination_details.setObjectName("mutedLabel")
        self.destination_details.setAccessibleName("Destination storage role and exact root")
        self.destination_details.setToolTip(
            f"Destination role: {destination_role}\nExact destination root: {destination_root}"
        )
        destination_layout.addWidget(self.destination_details)
        layout.addWidget(destination_panel)

        consequence = QLabel(plan.subfolder_consequence)
        consequence.setWordWrap(True)
        consequence.setObjectName("mutedLabel")
        layout.addWidget(consequence)

        action_row = QHBoxLayout()
        action_label = QLabel("Add method")
        self.action_label = action_label
        self.action_combo = QComboBox()
        self.action_combo.addItem("Copy to Ingest", "copy")
        self.action_combo.addItem("Move to Ingest", "move")
        if fixed_action is not None:
            index = self.action_combo.findData(fixed_action)
            if index < 0:
                raise ValueError("The saved Add Images action is unavailable.")
            self.action_combo.setCurrentIndex(index)
            self.action_combo.setEnabled(False)
            action_label.setText("Saved add method")
        self.action_combo.setAccessibleName("Choose Copy to Ingest or Move to Ingest")
        action_label.setBuddy(self.action_combo)
        self.action_explanation = QLabel()
        self.action_explanation.setWordWrap(True)
        self.action_explanation.setObjectName("mutedLabel")
        action_row.addWidget(action_label)
        action_row.addWidget(self.action_combo)
        action_row.addWidget(self.action_explanation, 1)
        layout.addLayout(action_row)

        self.summary = QLabel()
        self.summary.setWordWrap(True)
        self.summary.setObjectName("metricValue")
        self.summary.setAccessibleName("Add Images readiness summary")
        layout.addWidget(self.summary)

        self.capacity = QLabel()
        self.capacity.setWordWrap(True)
        self.capacity.setObjectName("mutedLabel")
        self.capacity.setAccessibleName("Required and available destination space")
        layout.addWidget(self.capacity)

        self.error_summary = QLabel()
        self.error_summary.setWordWrap(True)
        self.error_summary.setObjectName("warningLabel")
        self.error_summary.hide()
        layout.addWidget(self.error_summary)

        self.tree = QTreeWidget()
        self.tree.setHeaderLabels((
            "Source image and role", "Ingest destination", "Complete family", "Result",
        ))
        self.tree.setRootIsDecorated(False)
        self.tree.setAlternatingRowColors(True)
        header = self.tree.header()
        header.setSectionResizeMode(0, QHeaderView.Interactive)
        self.tree.setColumnWidth(0, 300)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        header.setSectionResizeMode(2, QHeaderView.ResizeToContents)
        header.setSectionResizeMode(3, QHeaderView.Interactive)
        self.tree.setColumnWidth(3, 260)
        layout.addWidget(self.tree, 1)
        self.exact_paths = QPlainTextEdit()
        self.exact_paths.setReadOnly(True)
        self.exact_paths.setMaximumHeight(160)
        self.exact_paths.setAccessibleName("Exact source and destination paths for selected image family")
        self.show_exact_paths = DisclosureButton("Show Exact Paths", "Hide Exact Paths", self.exact_paths)
        self.show_exact_paths.setToolTip("Select an image row to inspect every source and planned destination file.")
        layout.addWidget(self.show_exact_paths)
        layout.addWidget(self.exact_paths)
        self.tree.itemSelectionChanged.connect(self._update_exact_paths)
        self.preview_pager = QHBoxLayout()
        self.previous_button = QPushButton("Previous Preview Page")
        self.previous_button.setProperty("compact", True)
        self.previous_button.clicked.connect(lambda: self._change_page(-1))
        self.preview_page_label = QLabel()
        self.preview_page_label.setAlignment(Qt.AlignCenter)
        self.next_button = QPushButton("Next Preview Page")
        self.next_button.setProperty("compact", True)
        self.next_button.clicked.connect(lambda: self._change_page(1))
        self.preview_pager.addWidget(self.previous_button)
        self.preview_pager.addWidget(self.preview_page_label, 1)
        self.preview_pager.addWidget(self.next_button)
        layout.addLayout(self.preview_pager)

        if plan.unsupported:
            names = ", ".join(path.name for path in plan.unsupported[:4])
            if len(plan.unsupported) > 4:
                names += f", and {len(plan.unsupported) - 4:,} more"
            unsupported = QLabel(
                f"Unsupported entries will be skipped ({len(plan.unsupported):,}): {names}"
            )
            unsupported.setWordWrap(True)
            unsupported.setObjectName("mutedLabel")
            tooltip = "\n".join(str(path) for path in plan.unsupported[:100])
            if len(plan.unsupported) > 100:
                tooltip += f"\n… and {len(plan.unsupported) - 100:,} more"
            unsupported.setToolTip(tooltip)
            layout.addWidget(unsupported)

        self.boundary_confirmation = None
        boundary = plan.boundary_decision
        if boundary is not None and boundary.requires_explicit_confirmation:
            boundary_group = QGroupBox("Local Only boundary confirmation")
            boundary_layout = QVBoxLayout(boundary_group)
            boundary_note = QLabel(boundary.message)
            boundary_note.setWordWrap(True)
            boundary_note.setObjectName("warningLabel")
            boundary_layout.addWidget(boundary_note)
            self.boundary_confirmation = QCheckBox(
                "I confirm these exact complete image families and destinations may cross the Local Only boundary."
            )
            self.boundary_confirmation.setAccessibleName(
                "Confirm exact Add Images Local Only boundary transfer"
            )
            boundary_layout.addWidget(self.boundary_confirmation)
            layout.addWidget(boundary_group)

        self.buttons = QDialogButtonBox(QDialogButtonBox.Cancel)
        self.buttons.rejected.connect(self.reject)
        self.add_button = self.buttons.addButton(
            "Copy Ready Images to Ingest", QDialogButtonBox.AcceptRole,
        )
        self.add_button.clicked.connect(self.accept)
        self.add_button.setDefault(True)
        layout.addWidget(self.buttons)

        self.action_combo.currentIndexChanged.connect(self._update_action)
        if self.boundary_confirmation is not None:
            self.boundary_confirmation.toggled.connect(self._update_action)
        self._update_action()
        if not plan.items and plan.errors:
            self.action_label.hide()
            self.action_combo.hide()
            self.action_explanation.hide()
            self.tree.hide()
            self.show_exact_paths.hide()
            self.resize(980, 300)
            self.setMaximumHeight(400)

    def action(self) -> IngestImportAction:
        return str(self.action_combo.currentData())  # type: ignore[return-value]

    def boundary_confirmed(self) -> bool:
        return bool(
            self.boundary_confirmation is None
            or self.boundary_confirmation.isChecked()
        )

    def _update_action(self, *_args) -> None:
        action = self.action()
        copy = action == "copy"
        self.action_explanation.setText(
            "Original files stay in place. Undo removes only unchanged imported copies while the originals still match."
            if copy else
            "Original files are removed only after every destination family member passes read-back verification."
        )
        ready = self.plan.ready(action)
        blocked = self.plan.blocked(action)
        required = self.plan.required_bytes(action)
        available = self.plan.available_bytes(action)
        summary_parts = [
            f"{len(ready):,} of {len(self.plan.items):,} ready to {'copy' if copy else 'move'}"
        ]
        if self.plan.duplicates:
            summary_parts.append(f"{len(self.plan.duplicates):,} duplicates skipped")
        if blocked:
            summary_parts.append(f"{len(blocked):,} need attention")
        self.summary.setText(
            "  •  ".join(summary_parts) if self.plan.items else "No images are ready to add."
        )
        self.capacity.setText(
            f"Space needed: {transfer_size_text(required)}  •  "
            f"Available: {transfer_size_text(available)}"
        )
        errors = [*self.plan.errors, *self.plan.action_errors(action)]
        self.error_summary.setText("\n".join(errors))
        self.error_summary.setVisible(bool(errors))

        self._render_rows(action)

        boundary_ready = self.boundary_confirmed()
        self.add_button.setText(
            "Continue Remaining Images" if self.fixed_action is not None else
            "Copy Ready Images to Ingest" if copy else "Move Ready Images to Ingest"
        )
        self.add_button.setEnabled(bool(ready) and boundary_ready)

    def _render_rows(self, action: IngestImportAction) -> None:
        self.tree.clear()
        total = len(self.plan.items)
        pages = max(1, (total + self.preview_page_size - 1) // self.preview_page_size)
        self.preview_page = min(self.preview_page, pages - 1)
        start = self.preview_page * self.preview_page_size
        stop = min(total, start + self.preview_page_size)
        for index, item in enumerate(self.plan.items[start:stop], start):
            transfer = item.transfer_for(action)
            if item.duplicate_of is not None:
                result = f"Duplicate — already in Ingest as {item.duplicate_of.name}"
            else:
                item_errors = [*item.errors, *(transfer.errors if transfer else ())]
                result = "Ready" if not item_errors and transfer is not None else (
                    "; ".join(item_errors) or "Transfer preview unavailable"
                )
            if transfer is None:
                family = "Unavailable"
            else:
                method = (
                    "source-preserving copy" if transfer.mode == "copy" else
                    "same-volume rename" if transfer.mode == "rename" else
                    "verified copy, then source removal"
                )
                family = (
                    f"{len(transfer.members):,} files • {method} • "
                    f"{transfer_size_text(transfer.required_bytes)}"
                )
            row = QTreeWidgetItem((
                f"{item.source}\n{item.source_role}",
                str(item.destination / item.source.name),
                family,
                result,
            ))
            row.setToolTip(0, str(item.source))
            row.setToolTip(1, str(item.destination / item.source.name))
            row.setToolTip(3, result)
            row.setData(0, Qt.UserRole, index)
            self.tree.addTopLevelItem(row)
        if self.tree.topLevelItemCount():
            self.tree.setCurrentItem(self.tree.topLevelItem(0))
        else:
            self._update_exact_paths()
        self.preview_page_label.setText(
            f"Preview {start + 1 if total else 0:,}–{stop:,} of {total:,} image(s)"
        )
        self.previous_button.setEnabled(self.preview_page > 0)
        self.next_button.setEnabled(self.preview_page + 1 < pages)
        visible = total > self.preview_page_size
        self.previous_button.setVisible(visible)
        self.next_button.setVisible(visible)
        self.preview_page_label.setVisible(visible)

    def _update_exact_paths(self) -> None:
        row = self.tree.currentItem()
        self.show_exact_paths.setEnabled(row is not None)
        if row is None:
            self.exact_paths.setPlainText("Select an image row to inspect its exact paths.")
            return
        item = self.plan.items[row.data(0, Qt.UserRole)]
        transfer = item.transfer_for(self.action())
        if item.duplicate_of is not None:
            text = f"Exact duplicate already in Ingest at {item.duplicate_of}.\nSource remains unchanged: {item.source}"
        elif transfer is None:
            text = (f"Source: {item.source}\nProposed destination: {item.destination / item.source.name}\n"
                    "A complete family transfer is not available for this action.")
        else:
            text = "\n\n".join(
                f"Source: {member.source}\nDestination: {member.target}"
                for member in transfer.members
            )
        self.exact_paths.setPlainText(text)

    def _change_page(self, delta: int) -> None:
        self.preview_page = max(0, self.preview_page + int(delta))
        self._render_rows(self.action())
