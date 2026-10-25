"""Setup's read-only module inventory and dependency review surface."""
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (
    QDialog, QDialogButtonBox, QHBoxLayout, QLabel, QPushButton,
    QSplitter, QTabWidget, QTextBrowser, QTreeWidget, QTreeWidgetItem,
    QVBoxLayout, QWidget,
)

from core.module_management import (
    ModuleSnapshot, PACKAGE_GATE, dependency_plan, empty_snapshot,
    host_context, removal_plan,
)


class ModulesPage(QWidget):
    def __init__(self, parent=None, *, provider=None):
        super().__init__(parent)
        self.provider = provider or empty_snapshot
        self.snapshot = empty_snapshot()
        self.stale = False
        self.plan_dialog = None
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        layout.setSpacing(12)
        self.scope = QLabel(host_context())
        self.scope.setWordWrap(True)
        layout.addWidget(self.scope)
        self.notice = QLabel(PACKAGE_GATE)
        self.notice.setWordWrap(True)
        layout.addWidget(self.notice)
        toolbar = QHBoxLayout()
        self.refresh_button = QPushButton("Refresh inventory")
        self.refresh_button.clicked.connect(self.refresh_inventory)
        toolbar.addWidget(self.refresh_button)
        toolbar.addStretch()
        layout.addLayout(toolbar)
        split = QSplitter(Qt.Horizontal)
        self.tabs = QTabWidget()
        self.tables = {}
        for group in ("Installed", "Available", "Remote"):
            tree = QTreeWidget()
            tree.setHeaderLabels(["Module", "Version", "Evidence"])
            tree.setRootIsDecorated(False)
            tree.setAccessibleName(group + " modules")
            tree.setColumnWidth(0, 230)
            tree.setColumnWidth(1, 145)
            tree.currentItemChanged.connect(self.show_details)
            self.tables[group] = tree
            self.tabs.addTab(tree, group)
        self.tabs.currentChanged.connect(self.show_details)
        split.addWidget(self.tabs)
        self.details = QTextBrowser()
        self.details.setAccessibleName("Module details and dated release notes")
        self.details.setOpenExternalLinks(False)
        split.addWidget(self.details)
        split.setStretchFactor(0, 3)
        split.setStretchFactor(1, 2)
        layout.addWidget(split, 1)
        actions = QHBoxLayout()
        self.preview_button = QPushButton("Review dependencies")
        self.preview_button.clicked.connect(lambda: self.review_plan(False))
        self.removal_button = QPushButton("Review removal holds")
        self.removal_button.clicked.connect(lambda: self.review_plan(True))
        actions.addWidget(self.preview_button)
        actions.addWidget(self.removal_button)
        actions.addStretch()
        self.package_buttons = []
        for label in ("Download", "Install", "Update", "Remove"):
            button = QPushButton(label)
            button.setEnabled(False)
            button.setToolTip(PACKAGE_GATE)
            actions.addWidget(button)
            self.package_buttons.append(button)
        layout.addLayout(actions)
        self.status = QLabel()
        self.status.setWordWrap(True)
        self.status.setAccessibleName("Module inventory status")
        layout.addWidget(self.status)
        self.refresh_inventory()

    def selected_record(self):
        item = self.tabs.currentWidget().currentItem()
        return next((r for r in self.snapshot.records if item and r.key == item.data(0, Qt.UserRole)), None)

    def set_provider(self, provider):
        """Host injection seam; a provider is read-only and returns a bounded snapshot."""
        self.provider = provider
        self.refresh_inventory()

    def refresh_inventory(self):
        selected = self.selected_record()
        try:
            snapshot = self.provider()
            if type(snapshot) is not ModuleSnapshot:
                raise ValueError("Invalid inventory")
        except Exception:
            self.stale = True
            self.status.setText("Inventory unavailable. Previous records are stale; refresh before reviewing.")
            self.show_details()
            return
        self.snapshot, self.stale = snapshot, False
        for tree in self.tables.values():
            tree.clear()
        selected_tree = None
        for row in snapshot.records:
            item = QTreeWidgetItem([row.name, row.software_version, row.origin.value])
            item.setData(0, Qt.UserRole, row.key)
            for column in range(3):
                item.setToolTip(column, item.text(column))
            tree = self.tables[row.group]
            tree.addTopLevelItem(item)
            if selected and selected.key == row.key:
                tree.setCurrentItem(item)
                selected_tree = tree
        if selected_tree is not None:
            self.tabs.setCurrentWidget(selected_tree)
        self.notice.setText(("Synthetic fixture — package states are demonstrations only. " if snapshot.synthetic else "") + PACKAGE_GATE)
        count = len(snapshot.records)
        self.status.setText(f"{count} {'record' if count == 1 else 'records'} · inventory revision {snapshot.revision}. Viewing and previews make no changes.")
        self.show_details()

    def show_details(self, *_args):
        row = self.selected_record()
        enabled = row is not None and not self.stale
        self.preview_button.setEnabled(enabled)
        self.removal_button.setEnabled(enabled)
        if row is None:
            group = self.tabs.tabText(self.tabs.currentIndex())
            empty = {"Installed": "No configured module inventory. A development checkout is not a verified installed module package.",
                     "Available": "No published module releases are connected. Planned or unpublished modules cannot be installed.",
                     "Remote": "No remote module inventory is supplied. Remote module execution is unsupported."}
            message = empty[group] if self.tabs.currentWidget().topLevelItemCount() == 0 else "Select a module to inspect its exact version, evidence, dated notes and dependencies."
            self.details.setPlainText(message + "\n\nRefresh inventory to recheck the supplied records. Viewing makes no changes.")
            return
        dependencies = "\n".join(f"• {d.component_id.value} = {d.software_version}" for d in row.dependencies) or "None declared (not a completeness guarantee)."
        self.details.setPlainText("\n".join((row.name, row.component_id.value, "Version: " + row.software_version,
            "Evidence: " + row.origin.value, "Inventory: " + (row.inventory_state or "Not supplied"),
            "Compatibility: " + ("Compatible (fixture declaration)" if row.compatible is True else "Incompatible" if row.compatible is False else "Unknown"),
            "Interfaces: " + (", ".join(row.interfaces) or "Not supplied"), "", "What's New · " + (row.released_on or "Date not supplied"),
            row.notes or "Release notes not supplied.", "", "Dependencies (exact versions)", dependencies,
            "", "Record is stale — refresh before review." if self.stale else "Preview only; package actions remain unavailable.")))

    def review_plan(self, removal=False):
        row = self.selected_record()
        if row is None or self.stale:
            return
        try:
            fresh = self.provider()
            if type(fresh) is not ModuleSnapshot:
                raise ValueError("Invalid inventory")
        except Exception:
            self.stale = True
            self.status.setText("Inventory unavailable. Refresh before reviewing; no changes were made.")
            self.show_details()
            return
        plan = (removal_plan if removal else dependency_plan)(fresh, row.key, self.snapshot.revision)
        if fresh != self.snapshot:
            self.stale = True
            self.status.setText("Inventory changed. Refresh and select the current module before reviewing again.")
            self.show_details()
            # A provider must change its revision whenever any record changes.
            if fresh.revision == self.snapshot.revision:
                self.status.setText("Inventory changed without a new revision. The preview is held; refresh the inventory.")
                return
        dialog = QDialog(self)
        dialog.setWindowTitle(plan.title)
        dialog.setModal(True)
        dialog.resize(820, 530)
        column = QVBoxLayout(dialog)
        text = QTextBrowser()
        text.setAccessibleName("Read-only module plan")
        text.setPlainText(plan.text())
        column.addWidget(text)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(dialog.reject)
        column.addWidget(close)
        self.plan_dialog = dialog
        dialog.open()
