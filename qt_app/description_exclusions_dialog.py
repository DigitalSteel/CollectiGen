"""Shared review surface for global Plain Language Description exclusions."""

from __future__ import annotations

from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog,
    QHBoxLayout,
    QHeaderView,
    QLabel,
    QLineEdit,
    QPushButton,
    QTreeWidgetItem,
    QVBoxLayout,
)

from core.description_exclusions import restore_description_exclusions
from .components import RangeCheckTreeWidget
from .status_notifications import FooterMessageBox as QMessageBox


class DescriptionExclusionsDialog(QDialog):
    """Inspect and explicitly restore globally ignored incoming descriptions."""

    def __init__(self, settings, parent=None) -> None:
        super().__init__(parent)
        self.settings = settings
        self.changed = False
        self.setWindowTitle("Global Description Ignores")
        self.resize(760, 430)

        layout = QVBoxLayout(self)
        intro = QLabel(
            "Global ignores reject matching incoming descriptions. They do not "
            "change confirmed Image Knowledge or raw source evidence."
        )
        intro.setWordWrap(True)
        intro.setObjectName("mutedLabel")
        layout.addWidget(intro)

        controls = QHBoxLayout()
        self.check_all_button = QPushButton("Check All")
        self.check_all_button.setToolTip(
            "Check every description currently visible in this search."
        )
        self.uncheck_all_button = QPushButton("Uncheck All")
        self.uncheck_all_button.setToolTip(
            "Uncheck every description currently visible in this search."
        )
        self.search = QLineEdit()
        self.search.setPlaceholderText("Search ignored descriptions…")
        self.search.setAccessibleName("Search global ignored descriptions")
        controls.addWidget(self.check_all_button)
        controls.addWidget(self.uncheck_all_button)
        controls.addWidget(self.search, 1)
        layout.addLayout(controls)

        self.ignored_items = RangeCheckTreeWidget()
        self.ignored_items.setHeaderLabels(("Restore", "Description"))
        self.ignored_items.setRootIsDecorated(False)
        self.ignored_items.setAccessibleName("Global ignored descriptions")
        header = self.ignored_items.header()
        header.setSectionResizeMode(0, QHeaderView.Fixed)
        header.setSectionResizeMode(1, QHeaderView.Stretch)
        self.ignored_items.setColumnWidth(0, 82)
        layout.addWidget(self.ignored_items, 1)

        self.empty_label = QLabel()
        self.empty_label.setAlignment(Qt.AlignCenter)
        self.empty_label.setObjectName("mutedLabel")
        layout.addWidget(self.empty_label, 1)

        actions = QHBoxLayout()
        self.selection_status = QLabel()
        self.selection_status.setObjectName("mutedLabel")
        actions.addWidget(self.selection_status, 1)
        self.restore_button = QPushButton("Restore Checked")
        self.restore_button.setEnabled(False)
        self.close_button = QPushButton("Close")
        self.close_button.setProperty("primary", True)
        actions.addWidget(self.restore_button)
        actions.addWidget(self.close_button)
        layout.addLayout(actions)

        self.search.textChanged.connect(self._filter_items)
        self.ignored_items.itemChanged.connect(self._update_actions)
        self.check_all_button.clicked.connect(lambda: self._set_visible_checked(True))
        self.uncheck_all_button.clicked.connect(lambda: self._set_visible_checked(False))
        self.restore_button.clicked.connect(self._restore_checked)
        self.close_button.clicked.connect(self.close)
        self._reload_items()

    def _configured_values(self) -> list[object]:
        configured = getattr(self.settings, "ignored_plain_language_sentences", ())
        if isinstance(configured, (str, bytes)):
            return []
        try:
            values = list(configured)
        except TypeError:
            return []
        return sorted(values, key=lambda value: str(value).casefold())

    def _reload_items(self) -> None:
        self.ignored_items.blockSignals(True)
        self.ignored_items.clear()
        for value in self._configured_values():
            item = QTreeWidgetItem(("", str(value)))
            item.setCheckState(0, Qt.Unchecked)
            self.ignored_items.addTopLevelItem(item)
        self.ignored_items.blockSignals(False)
        self._filter_items()
        self._update_actions()

    def _filter_items(self) -> None:
        query = self.search.text().casefold().strip()
        visible = 0
        for index in range(self.ignored_items.topLevelItemCount()):
            item = self.ignored_items.topLevelItem(index)
            matches = not query or query in item.text(1).casefold()
            item.setHidden(not matches)
            visible += int(matches)
        configured = self.ignored_items.topLevelItemCount()
        self.ignored_items.setVisible(bool(configured))
        self.empty_label.setVisible(not configured or not visible)
        if not configured:
            self.empty_label.setText("No descriptions are currently ignored everywhere.")
        elif not visible:
            self.empty_label.setText("No ignored descriptions match this search.")
        self._update_actions()

    def _set_visible_checked(self, checked: bool) -> None:
        self.ignored_items.blockSignals(True)
        for index in range(self.ignored_items.topLevelItemCount()):
            item = self.ignored_items.topLevelItem(index)
            if not item.isHidden():
                item.setCheckState(0, Qt.Checked if checked else Qt.Unchecked)
        self.ignored_items.blockSignals(False)
        self._update_actions()

    def _checked_values(self) -> list[str]:
        return [
            self.ignored_items.topLevelItem(index).text(1)
            for index in range(self.ignored_items.topLevelItemCount())
            if self.ignored_items.topLevelItem(index).checkState(0) == Qt.Checked
        ]

    def _update_actions(self, *_args) -> None:
        checked = self._checked_values()
        hidden_checked = sum(
            self.ignored_items.topLevelItem(index).isHidden()
            and self.ignored_items.topLevelItem(index).checkState(0) == Qt.Checked
            for index in range(self.ignored_items.topLevelItemCount())
        )
        if not checked:
            self.selection_status.setText("No descriptions checked")
        elif hidden_checked:
            self.selection_status.setText(
                f"{len(checked):,} checked ({hidden_checked:,} hidden by search)"
            )
        else:
            self.selection_status.setText(f"{len(checked):,} checked")
        self.restore_button.setEnabled(bool(checked))
        has_visible = any(
            not self.ignored_items.topLevelItem(index).isHidden()
            for index in range(self.ignored_items.topLevelItemCount())
        )
        self.check_all_button.setEnabled(has_visible)
        self.uncheck_all_button.setEnabled(has_visible and bool(checked))

    def _restore_checked(self) -> None:
        values = self._checked_values()
        if not values:
            return
        try:
            changed = restore_description_exclusions(self.settings, values)
        except Exception as exc:
            QMessageBox.warning(self, "Ignored descriptions were not saved", str(exc))
            return
        if changed:
            self.changed = True
        self._reload_items()
