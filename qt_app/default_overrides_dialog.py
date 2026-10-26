from __future__ import annotations

from PySide6.QtCore import QItemSelectionModel, QSize
from PySide6.QtWidgets import (
    QAbstractItemView,
    QCheckBox,
    QComboBox,
    QGroupBox,
    QHeaderView,
    QHBoxLayout,
    QLabel,
    QLayout,
    QLineEdit,
    QPlainTextEdit,
    QPushButton,
    QTreeView,
    QVBoxLayout,
)

from core.default_overrides import DefaultOverrideItem, DefaultOverrideSnapshot

from .components import (
    DisclosureButton,
    IncrementalResultModel,
    IncrementalResultRow,
    bounded_window_size,
)
from .modal_feedback import FeedbackEnabledDialog


class DefaultOverridesDialog(FeedbackEnabledDialog):
    """Restore selected working System Knowledge settings to shipped values."""

    feedback_workflow = "Restore shipped System Knowledge values"
    PAGE_SIZE = 200

    def __init__(self, snapshot: DefaultOverrideSnapshot, parent=None) -> None:
        super().__init__(parent)
        self.snapshot = snapshot
        self.restore_complete_records = False
        self._reset_items = tuple(snapshot.reset_items)
        self._audit_items = tuple(snapshot.audit_items)
        self._all_items = self._reset_items
        self._by_key = {item.key: item for item in snapshot.items}
        self.setWindowTitle("Restore Shipped System Knowledge Values")
        self.resize(1180, 760)

        layout = QVBoxLayout(self)
        layout.setSizeConstraint(QLayout.SetNoConstraint)
        heading = QLabel("Restore a shipped value")
        heading.setObjectName("sectionTitle")
        layout.addWidget(heading)
        intro = QLabel(
            "This list starts with meaningful reset candidates: a current setting "
            "differs from a concrete value shipped with this version. Select settings "
            "only when you want to discard the current value. Closing keeps everything "
            "as it is."
        )
        intro.setWordWrap(True)
        intro.setObjectName("mutedLabel")
        layout.addWidget(intro)

        filter_row = QHBoxLayout()
        self.search = QLineEdit()
        self.search.setPlaceholderText(
            "Search records, settings, values, or difference types..."
        )
        self.search.setAccessibleName("Search local and shipped differences")
        self.type_filter = QComboBox()
        self.type_filter.addItem("All record types", "")
        for kind, label in sorted(
            {(item.kind, item.kind_label) for item in self._all_items},
            key=lambda value: value[1].casefold(),
        ):
            self.type_filter.addItem(label, kind)
        self.difference_filter = QComboBox()
        self.difference_filter.addItem("All difference types", "")
        for kind, label in sorted(
            {
                (item.difference_kind, item.difference_label)
                for item in self._all_items
            },
            key=lambda value: value[1].casefold(),
        ):
            self.difference_filter.addItem(label, kind)
        filter_row.addWidget(self.search, 1)
        filter_row.addWidget(self.type_filter)
        filter_row.addWidget(self.difference_filter)
        layout.addLayout(filter_row)

        self.advanced_audit = QCheckBox(
            f"Include {len(self._audit_items):,} current-only preserved "
            f"difference{'s' if len(self._audit_items) != 1 else ''} (advanced audit)"
        )
        self.advanced_audit.setToolTip(
            "These older or unclassified values have no shipped value to restore. "
            "They are available for inspection but are not ordinary reset choices."
        )
        self.advanced_audit.setVisible(bool(self._audit_items))
        self.advanced_audit.toggled.connect(self._show_advanced_audit)
        layout.addWidget(self.advanced_audit)

        self.model = IncrementalResultModel(
            (
                "Record",
                "Type",
                "Setting",
                "Current Value",
                "Shipped Value",
                "Difference",
            ),
            page_size=self.PAGE_SIZE,
            parent=self,
        )
        self.table = QTreeView()
        self.table.setModel(self.model)
        self.table.setRootIsDecorated(False)
        self.table.setUniformRowHeights(True)
        self.table.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.table.setSelectionMode(QAbstractItemView.ExtendedSelection)
        self.table.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.table.setAccessibleName("Shipped System Knowledge reset candidates")
        header = self.table.header()
        header.setMinimumSectionSize(95)
        for column in range(6):
            header.setSectionResizeMode(column, QHeaderView.Stretch)
        layout.addWidget(self.table, 1)

        result_row = QHBoxLayout()
        self.result_status = QLabel()
        self.result_status.setObjectName("mutedLabel")
        self.load_more = QPushButton("Load More...")
        self.load_more.setProperty("compact", True)
        self.load_more.clicked.connect(self.model.fetch_more)
        result_row.addWidget(self.result_status, 1)
        result_row.addWidget(self.load_more)
        layout.addLayout(result_row)

        details = QGroupBox("Selected choice")
        details_layout = QVBoxLayout(details)
        self.selection_summary = QLabel("Select one or more settings to compare actions.")
        self.selection_summary.setWordWrap(True)
        details_layout.addWidget(self.selection_summary)
        self.selection_effect = QLabel(
            "No values change when this window is closed. Image Knowledge, images, "
            "TXT, Metadata, and raw evidence are never changed here."
        )
        self.selection_effect.setWordWrap(True)
        self.selection_effect.setObjectName("mutedLabel")
        details_layout.addWidget(self.selection_effect)
        self.technical_details = QPlainTextEdit()
        self.technical_details.setReadOnly(True)
        self.technical_details.setMaximumHeight(130)
        self.technical_details.setAccessibleName("Exact selected knowledge values")
        self.technical_disclosure = DisclosureButton(
            "Show Exact Technical Values...",
            "Hide Exact Technical Values",
            self.technical_details,
        )
        details_layout.addWidget(self.technical_disclosure)
        details_layout.addWidget(self.technical_details)
        layout.addWidget(details)

        action_row = QHBoxLayout()
        action_row.addStretch(1)
        self.use_shipped_fields_button = QPushButton("Reset Selected Settings")
        self.use_shipped_fields_button.clicked.connect(lambda: self._accept(False))
        self.use_shipped_fields_button.setToolTip(
            "Replace only each selected setting with its shipped value."
        )
        self.use_shipped_records_button = QPushButton("Reset Entire Selected Records")
        self.use_shipped_records_button.clicked.connect(lambda: self._accept(True))
        self.use_shipped_records_button.setToolTip(
            "Replace every preserved local setting on each selected record."
        )
        self.close_button = QPushButton("Close")
        self.close_button.setProperty("primary", True)
        self.close_button.clicked.connect(self.reject)
        action_row.addWidget(self.use_shipped_fields_button)
        action_row.addWidget(self.use_shipped_records_button)
        action_row.addWidget(self.close_button)
        layout.addLayout(action_row)

        # Compatibility names retained for callers and focused UI tests while the
        # underlying operation keeps its existing restore transaction.
        self.restore_fields_button = self.use_shipped_fields_button
        self.restore_records_button = self.use_shipped_records_button

        self.table.selectionModel().selectionChanged.connect(
            lambda *_args: self._update_selection()
        )
        self.model.counts_changed.connect(self._update_counts)
        self.search.textChanged.connect(self._apply_filters)
        self.type_filter.currentIndexChanged.connect(self._apply_filters)
        self.difference_filter.currentIndexChanged.connect(self._apply_filters)
        self._apply_filters()
        self._update_selection()

    def _row(self, item: DefaultOverrideItem) -> IncrementalResultRow:
        shipped_value = (
            item.default_value if item.can_reset_setting else "No shipped value"
        )
        return IncrementalResultRow(
            item.key,
            (
                item.label,
                item.kind_label,
                item.field_label,
                item.current_value,
                shipped_value,
                item.difference_label,
            ),
            tooltip=(
                f"{item.kind_label} {item.stable_id}; {item.field_label}. "
                f"Current: {item.current_value}. Shipped: {shipped_value}. "
                f"{item.difference_summary}"
            ),
        )

    def selected_keys(self) -> tuple[str, ...]:
        return tuple(sorted({
            self.model.stable_id(index)
            for index in self.table.selectionModel().selectedRows(0)
            if self.model.stable_id(index)
        }))

    def selected_items(self) -> tuple[DefaultOverrideItem, ...]:
        return tuple(
            self._by_key[key] for key in self.selected_keys() if key in self._by_key
        )

    def _show_advanced_audit(self, checked: bool) -> None:
        self._all_items = tuple(self.snapshot.items) if checked else self._reset_items
        selected_difference = str(self.difference_filter.currentData() or "")
        self.type_filter.blockSignals(True)
        self.difference_filter.blockSignals(True)
        current_kind = str(self.type_filter.currentData() or "")
        self.type_filter.clear()
        self.type_filter.addItem("All record types", "")
        for kind, label in sorted(
            {(item.kind, item.kind_label) for item in self._all_items},
            key=lambda value: value[1].casefold(),
        ):
            self.type_filter.addItem(label, kind)
        index = self.type_filter.findData(current_kind)
        self.type_filter.setCurrentIndex(max(0, index))
        self.difference_filter.clear()
        self.difference_filter.addItem("All difference types", "")
        for kind, label in sorted(
            {
                (item.difference_kind, item.difference_label)
                for item in self._all_items
            },
            key=lambda value: value[1].casefold(),
        ):
            self.difference_filter.addItem(label, kind)
        difference_index = self.difference_filter.findData(selected_difference)
        self.difference_filter.setCurrentIndex(max(0, difference_index))
        self.type_filter.blockSignals(False)
        self.difference_filter.blockSignals(False)
        self._apply_filters()

    def _matching_items(self) -> list[DefaultOverrideItem]:
        query = self.search.text().strip().casefold()
        kind = str(self.type_filter.currentData() or "")
        difference = str(self.difference_filter.currentData() or "")
        result = []
        for item in self._all_items:
            if kind and item.kind != kind:
                continue
            if difference and item.difference_kind != difference:
                continue
            haystack = " ".join((
                item.label,
                item.kind_label,
                item.field_label,
                item.current_value,
                item.default_value,
                item.difference_label,
                item.stable_id,
            )).casefold()
            if query and query not in haystack:
                continue
            result.append(item)
        return result

    def _apply_filters(self, *_args) -> None:
        selected = self.selected_keys()
        preserve = self.model.loaded_count
        matching = self._matching_items()
        self.model.set_rows(
            [self._row(item) for item in matching],
            preserve_count=preserve,
            ensure_ids=selected,
        )
        selection = self.table.selectionModel()
        for key in selected:
            index = self.model.index_for_id(key)
            if index.isValid():
                selection.select(index, QItemSelectionModel.Select | QItemSelectionModel.Rows)
        self._update_selection()

    def _accept(self, complete_records: bool) -> None:
        if not self.selected_keys() or not all(
            item.can_reset_setting for item in self.selected_items()
        ):
            return
        self.restore_complete_records = complete_records
        self.accept()

    def _update_selection(self) -> None:
        items = self.selected_items()
        enabled = bool(items) and all(item.can_reset_setting for item in items)
        self.use_shipped_fields_button.setEnabled(enabled)
        self.use_shipped_records_button.setEnabled(enabled)
        if not items:
            self.selection_summary.setText(
                "Select one or more settings to compare actions."
            )
            self.selection_effect.setText(
                "No values change when this window is closed. Image Knowledge, "
                "images, TXT, Metadata, and raw evidence are never changed here."
            )
            self.technical_details.clear()
            return
        records = {(item.kind, item.stable_id) for item in items}
        entire_record_items = tuple(
            item
            for item in self.snapshot.items
            if (item.kind, item.stable_id) in records
        )
        additional = max(0, len(entire_record_items) - len(items))
        advanced_additional = sum(
            1
            for item in entire_record_items
            if item.key not in {selected.key for selected in items}
            and not item.can_reset_setting
        )
        if not enabled:
            self.selection_summary.setText(
                "The selection includes a current-only preserved difference."
            )
            self.selection_effect.setText(
                "There is no shipped value for that setting, so it cannot be reset "
                "from this ordinary workflow. It remains visible for audit only."
            )
            self.technical_details.setPlainText(self._technical_text(items))
            return
        if len(items) == 1:
            item = items[0]
            self.selection_summary.setText(
                f"{item.label} - {item.field_label}: keep '{item.current_value}' "
                f"or use shipped '{item.default_value}'."
            )
            self.selection_effect.setText(
                item.difference_summary
                + " Reset Selected Settings changes only this setting. "
                + self._entire_record_effect(additional, advanced_additional)
            )
        else:
            self.selection_summary.setText(
                f"{len(items):,} settings selected across {len(records):,} records."
            )
            self.selection_effect.setText(
                "Reset Selected Settings changes only the highlighted rows. "
                + self._entire_record_effect(additional, advanced_additional)
            )
        self.technical_details.setPlainText(self._technical_text(items))

    @staticmethod
    def _entire_record_effect(additional: int, advanced_additional: int) -> str:
        if not additional:
            return "Reset Entire Selected Records has the same effect for this selection."
        advanced = (
            f", including {advanced_additional:,} current-only audit value(s)"
            if advanced_additional else ""
        )
        return (
            "Reset Entire Selected Records also clears "
            f"{additional:,} other preserved difference(s){advanced} on those records."
        )

    @staticmethod
    def _technical_text(items: tuple[DefaultOverrideItem, ...]) -> str:
        exact = []
        for item in items:
            shipped = (
                item.default_detail or item.default_value
                if item.can_reset_setting else "No shipped value"
            )
            exact.append(
                f"{item.kind_label}: {item.label} ({item.stable_id})\n"
                f"Setting: {item.field_label} [{item.field}]\n"
                f"CURRENT\n{item.current_detail or item.current_value}\n\n"
                f"SHIPPED\n{shipped}"
            )
        return "\n\n---\n\n".join(exact)

    def _update_counts(self, shown: int, matching: int) -> None:
        total = len(self._all_items)
        noun = "preserved difference" if self.advanced_audit.isChecked() else "reset candidate"
        if not total:
            if self._audit_items and not self.advanced_audit.isChecked():
                self.result_status.setText(
                    "No ordinary reset candidates. "
                    f"{len(self._audit_items):,} current-only preserved "
                    "difference(s) remain available in advanced audit."
                )
            else:
                self.result_status.setText(
                    "No current values differ from shipped System Knowledge."
                )
        elif not matching:
            self.result_status.setText(
                f"No matches among {total:,} {noun}(s)."
            )
        elif matching < total:
            self.result_status.setText(
                f"Showing {shown:,} of {matching:,} matching {noun}(s); "
                f"{total:,} total."
            )
        elif shown < matching:
            self.result_status.setText(
                f"Showing {shown:,} of {matching:,} {noun}(s)."
            )
        else:
            self.result_status.setText(
                f"Showing all {matching:,} {noun}(s)."
            )
        self.load_more.setVisible(shown < matching)
        if shown < matching:
            self.load_more.setText(
                f"Load {min(self.PAGE_SIZE, matching - shown):,} More..."
            )

    def _configure_columns(self) -> None:
        """Use readable comparison widths at 1080p and squeeze on small screens."""
        header = self.table.header()
        if self.table.viewport().width() < 1000:
            for column in range(6):
                header.setSectionResizeMode(column, QHeaderView.Stretch)
            return
        header.setSectionResizeMode(0, QHeaderView.Stretch)
        for column, width in ((1, 115), (2, 155), (3, 240), (4, 230), (5, 155)):
            header.setSectionResizeMode(column, QHeaderView.Interactive)
            header.resizeSection(column, width)

    def resizeEvent(self, event) -> None:
        super().resizeEvent(event)
        self._configure_columns()

    def showEvent(self, event) -> None:
        screen = self.screen()
        if screen is not None:
            self.resize(
                bounded_window_size(screen.availableGeometry(), QSize(1180, 760))
            )
        super().showEvent(event)
        self._configure_columns()
