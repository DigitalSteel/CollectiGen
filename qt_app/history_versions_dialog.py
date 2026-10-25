"""Shared exact-selection saved Image Knowledge comparison and restoration."""
from __future__ import annotations

from dataclasses import dataclass
import json

from PySide6.QtCore import Qt, QTimer, Signal
from PySide6.QtWidgets import (QAbstractItemView, QCheckBox, QComboBox, QDialogButtonBox,
    QHBoxLayout, QHeaderView, QLabel, QListWidget, QListWidgetItem, QPlainTextEdit,
    QProgressBar, QPushButton, QSplitter, QTabWidget, QTableWidget, QTableWidgetItem,
    QTreeWidget, QTreeWidgetItem, QVBoxLayout, QWidget)

from core.history_browser import BrowserSelection
from core.image_knowledge_history import RestoreUnit, ImageKnowledgeHistory
from .components import EmptyStatePanel, bind_status_footer, guard_scrollable_value_controls
from .modal_feedback import FeedbackEnabledDialog
from .workers import CoordinatedCallTask


UNIT_LABELS = {"descriptions": "Descriptions and their decisions", "tags": "Tags and required tag state",
    "absence": "Confirmed absences", "exclusion": "Export exclusions", "characters": "Characters",
    "objects": "Objects", "contexts": "Contexts", "decisions": "Image decisions", "review": "Review status",
    "record": "Complete saved Image Knowledge", "record_absence": "Recorded absence of Image Knowledge"}
MAX_VISIBLE_RECORDS = 2000
MAX_VALUE_TEXT = 6000
FIELD_LABELS = {"plain_language": "Descriptions", "sdxl_tags": "Confirmed Tags",
    "confirmed_absent": "Confirmed absences", "export_excluded": "Export exclusions",
    "source": "Review evidence", "rejected_descriptions": "Rejected descriptions",
    "finding_decisions": "Finding decisions", "dismissed_findings": "Deferred findings",
    "locations": "Tag locations", "identity": "Tag identity", "absence": "Confirmed absences",
    "exclusion": "Export exclusions", "rejected": "Rejected Tags", "id": "ID"}


def comparison_label(unit):
    kind, _, key = unit.partition(":")
    labels = {**UNIT_LABELS, "descriptions": "Descriptions", "tags": "Tags",
        "characters": "Characters", "contexts": "Image contexts"}
    label = labels.get(kind, kind.replace("_", " ").capitalize())
    return label + (": " + key if key else "")


def comparison_value_text(raw, unit):
    """Bounded display only; exact serialized values stay on the comparison row."""
    try:
        value = json.loads(raw)
    except (ValueError, TypeError, RecursionError):
        return "Unavailable display; inspect exact saved data."
    kind = unit.partition(":")[0]
    remaining = 120
    def text(value, depth=0):
        nonlocal remaining
        remaining -= 1
        if remaining < 0 or depth > 5:
            return "… (more in exact saved data)"
        if value is None: return "None"
        if isinstance(value, bool): return "Yes" if value else "No"
        if isinstance(value, str): return value[:MAX_VALUE_TEXT] or "Empty text"
        if isinstance(value, dict):
            if not value: return "No fields"
            parts = []
            for key, child in value.items():
                if remaining <= 0:
                    parts.append("… (more in exact saved data)"); break
                label = FIELD_LABELS.get(key, key.replace("_", " ").capitalize())
                parts.append(label + ": " + text(child, depth + 1))
            return "\n".join(parts)[:MAX_VALUE_TEXT]
        if isinstance(value, list):
            if not value: return "No entries"
            parts = []
            for child in value:
                if remaining <= 0:
                    parts.append("… (more in exact saved data)"); break
                parts.append(text(child, depth + 1))
            return "\n".join(parts)[:MAX_VALUE_TEXT]
        return str(value)
    if kind == "descriptions" and isinstance(value, dict):
        result = text(value["plain_language"]) if "plain_language" in value else "Missing (not recorded)"
        other = {key: child for key, child in value.items() if key != "plain_language"}
        if other: result += "\n" + text(other)
    elif kind == "review" and isinstance(value, list) and len(value) == 2 and isinstance(value[0], bool):
        result = text(value[1]) if value[0] else "Missing (not recorded)"
    elif value is None and kind in {"record", "record_absence", "characters", "objects", "contexts"}:
        result = "No saved Image Knowledge (record absent)" if kind in {"record", "record_absence"} else "Missing (not recorded)"
    else:
        result = text(value)
    return result[:MAX_VALUE_TEXT] + ("\n… (more in exact saved data)" if len(result) >= MAX_VALUE_TEXT else "")


@dataclass(frozen=True)
class _Delivery:
    serial: int
    action: str
    value: object = None
    error: str = ""


class HistoryVersionsDialog(FeedbackEnabledDialog):
    feedback_workflow = "History & Versions"
    restorationFinished = Signal(object)
    busyChanged = Signal(bool)

    def __init__(self, browser, jobs, parent=None, *, recovery_handler=None, close_callback=None):
        super().__init__(parent)
        self.browser, self.jobs = browser, jobs
        self.recovery_handler = recovery_handler
        self._close_callback = close_callback
        self._reported_busy = False
        self._task = None
        self._serial = 0
        self._closing = False
        self._cancel_requested = False
        self._cursor = None
        self._records = []
        self._states = []
        self._selected = {}
        self._comparison = None
        self._choices = {}
        self._choice_rows = []
        self._rendering = False
        self._choice_reviewed = False
        self._index_image = None
        self._metadata_complete = False
        self._render_timer = QTimer(self)
        self._render_timer.setSingleShot(True)
        self._render_timer.timeout.connect(self.render_records)
        self.setWindowTitle("History & Versions")
        self.resize(1520, 920)
        self.setModal(False)
        layout = QVBoxLayout(self)
        self.scope = self._label(f"{len(browser.members):,} exact selected images · Saved Image Knowledge only" +
            (f" · Fixed operation: {browser.operation_target}" if browser.operation_target else ""))
        layout.addWidget(self.scope)
        layout.addWidget(self._label("Browse recorded changes, select one target per image, and compare the data before restoring. Pixels, TXT, embedded Metadata, global knowledge and frozen training packages are outside this restoration."))

        controls = QHBoxLayout()
        self.refresh = QPushButton("Reload History")
        self.refresh.clicked.connect(lambda: self.load_page(reset=True))
        controls.addWidget(self.refresh)
        self.next_page = QPushButton("Load More")
        self.next_page.clicked.connect(lambda: self.load_page())
        controls.addWidget(self.next_page)
        self.index_button = QPushButton("Update Selected History List")
        self.index_button.setToolTip("Refresh one bounded portion of the existing history index. No versions are restored or repaired.")
        self.index_button.clicked.connect(self.update_index)
        controls.addWidget(self.index_button)
        self.recovery_button = QPushButton("Open Selected Image Recovery…")
        self.recovery_button.clicked.connect(self.open_recovery)
        self.recovery_button.setVisible(recovery_handler is not None)
        controls.addWidget(self.recovery_button)
        controls.addStretch(1)
        self.counts = self._label("Not loaded")
        controls.addWidget(self.counts)
        layout.addLayout(controls)

        self.tabs = QTabWidget()
        self.changes = self._tree(("Change / selected image", "Recorded time", "Origin / producer", "Reason / coverage"))
        self.tabs.addTab(self.changes, "Changes")
        version_page = QWidget()
        version_layout = QVBoxLayout(version_page)
        version_layout.setContentsMargins(0, 0, 0, 0)
        self.image_filter = QComboBox()
        self.image_filter.setAccessibleName("Selected image versions")
        self.image_filter.addItem("All selected images", None)
        for member in browser.members:
            self.image_filter.addItem(str(member.image), member.image)
        version_layout.addWidget(self.image_filter)
        self.versions = self._tree(("Image / version", "Recorded time", "Origin / producer", "Recorded coverage"))
        version_layout.addWidget(self.versions)
        self.tabs.addTab(version_page, "Versions")
        self.empty = EmptyStatePanel("Load history for the fixed image selection. No new history will be registered.", "History not loaded", minimum_height=90)
        self.browse_split = QSplitter(Qt.Vertical)
        browse = QWidget()
        browse_layout = QVBoxLayout(browse)
        browse_layout.setContentsMargins(0, 0, 0, 0)
        browse_layout.addWidget(self.tabs)
        browse_layout.addWidget(self.empty)
        self.browse_split.addWidget(browse)

        compare_panel = QWidget()
        compare_layout = QVBoxLayout(compare_panel)
        compare_layout.setContentsMargins(0, 0, 0, 0)
        select_row = QHBoxLayout()
        self.units = QComboBox()
        self.units.setAccessibleName("Data category to compare")
        for key, label in UNIT_LABELS.items():
            self.units.addItem(label, key)
        select_row.addWidget(QLabel("Data to compare"))
        select_row.addWidget(self.units)
        self.companions = QPushButton("Select Additional Categories…")
        self.companions.clicked.connect(self.toggle_categories)
        select_row.addWidget(self.companions)
        self.compare_button = QPushButton("Compare Selected Data")
        self.compare_button.clicked.connect(self.compare)
        select_row.addStretch(1)
        select_row.addWidget(self.compare_button)
        compare_layout.addLayout(select_row)
        self.additional = QListWidget()
        self.additional.setAccessibleName("Additional restore categories and dependencies")
        self.additional.setMaximumHeight(130)
        for key, label in UNIT_LABELS.items():
            if key in {"record", "record_absence"}: continue
            item = QListWidgetItem(label)
            item.setData(Qt.UserRole, key)
            item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
            item.setCheckState(Qt.Unchecked)
            self.additional.addItem(item)
        self.additional.hide()
        compare_layout.addWidget(self.additional)
        self.comparison = QTableWidget(0, 7)
        self.comparison.setAccessibleName("Historical current and proposed saved data")
        self.comparison.setHorizontalHeaderLabels(("Image", "Data unit", "Change / dependencies", "Historical", "Current", "Proposed", "Your choice"))
        self.comparison.setSelectionBehavior(QAbstractItemView.SelectRows)
        self.comparison.setEditTriggers(QAbstractItemView.NoEditTriggers)
        self.comparison.horizontalHeader().setSectionResizeMode(QHeaderView.Stretch)
        self.comparison.verticalHeader().setVisible(False)
        self.comparison.itemSelectionChanged.connect(self.show_detail)
        compare_layout.addWidget(self.comparison, 1)
        self.exact_data = QCheckBox("Show exact saved data (advanced)")
        self.exact_data.setToolTip("Show the original serialized comparison, including unknown fields. This does not change restoration choices.")
        self.exact_data.toggled.connect(self.show_detail)
        compare_layout.addWidget(self.exact_data)
        self.detail = QPlainTextEdit()
        self.detail.setAccessibleName("Selected data comparison details")
        self.detail.setReadOnly(True)
        self.detail.setMaximumHeight(140)
        self.detail.setPlaceholderText("Select a comparison row to read its values. Exact saved data is available above.")
        compare_layout.addWidget(self.detail)
        choose = QHBoxLayout()
        self.keep = QPushButton("Keep Current for Selected Rows")
        self.historical = QPushButton("Use Historical for Selected Rows")
        self.keep.clicked.connect(lambda: self.choose_rows("current"))
        self.historical.clicked.connect(lambda: self.choose_rows("historical"))
        choose.addWidget(self.keep)
        choose.addWidget(self.historical)
        choose.addStretch(1)
        self.review_choices = QPushButton("Review Proposed Changes")
        self.review_choices.clicked.connect(self.review)
        choose.addWidget(self.review_choices)
        compare_layout.addLayout(choose)
        self.browse_split.addWidget(compare_panel)
        self.browse_split.setSizes([350, 400])
        layout.addWidget(self.browse_split, 1)

        self.status = self._label("History has not loaded yet.")
        self.progress = QProgressBar()
        self.progress.setRange(0, 0)
        self.progress.hide()
        self.cancel = QPushButton("Cancel")
        self.cancel.clicked.connect(self.cancel_work)
        self.cancel.hide()
        layout.addWidget(self.status)
        layout.addWidget(self.progress)
        actions = QHBoxLayout()
        actions.addWidget(self.cancel)
        self.clear_selection = QPushButton("Clear Selected Targets")
        self.clear_selection.clicked.connect(self.clear_targets)
        actions.addWidget(self.clear_selection)
        actions.addStretch(1)
        close = QDialogButtonBox(QDialogButtonBox.Close)
        close.rejected.connect(self.reject)
        actions.addWidget(close)
        self.apply_button = QPushButton("Restore Selected Data")
        self.apply_button.setProperty("primary", True)
        self.apply_button.clicked.connect(self.apply)
        actions.addWidget(self.apply_button)
        layout.addLayout(actions)
        bind_status_footer(self, self.status, self.progress, self.cancel, title="History & Versions")
        self.changes.itemChanged.connect(self.target_changed)
        self.versions.itemChanged.connect(self.target_changed)
        self.changes.itemSelectionChanged.connect(self.index_selection)
        self.versions.itemSelectionChanged.connect(self.index_selection)
        self.units.currentIndexChanged.connect(self.invalidate_comparison)
        self.additional.itemChanged.connect(self.invalidate_comparison)
        self.image_filter.currentIndexChanged.connect(lambda: self.load_page(reset=True))
        self.tabs.currentChanged.connect(lambda: self.load_page(reset=True))
        guard_scrollable_value_controls(self)
        for button in self.findChildren(QPushButton): button.setAutoDefault(False)
        self.update_actions()
        QTimer.singleShot(0, lambda: self.load_page(reset=True))

    @staticmethod
    def _label(text):
        label = QLabel(text)
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True)
        return label

    @staticmethod
    def _tree(headers):
        tree = QTreeWidget()
        tree.setHeaderLabels(headers)
        tree.setUniformRowHeights(True)
        tree.setSelectionMode(QAbstractItemView.ExtendedSelection)
        tree.header().setSectionResizeMode(QHeaderView.Stretch)
        tree.setMinimumHeight(130)
        return tree

    def _launch(self, action, operation):
        if self._task is not None or self._closing: return
        self._serial += 1
        serial = self._serial
        returned = {}
        self._cancel_requested = False
        def work(job):
            try:
                value = operation(lambda: job.cancel_requested)
                delivery = _Delivery(serial, action, value)
            except Exception as exc:
                delivery = _Delivery(serial, action, error=str(exc))
            returned["delivery"] = delivery
            return delivery
        task = CoordinatedCallTask(self.jobs, "History: " + action, work, resource="io")
        self._task = task
        task.signals.complete.connect(lambda value: self._finish(task, returned.get("delivery", value)))
        task.signals.failed.connect(lambda error: self._finish(task, returned.get("delivery"), error))
        task.signals.cancelled.connect(lambda: self._finish(task, returned.get("delivery"), "Cancelled at a safe boundary."))
        self.status.setText("Working: " + action)
        self.update_actions()
        try: task.start()
        except Exception as exc: self._finish(task, None, str(exc))

    def _finish(self, task, delivery, error=""):
        if self._task is not task: return
        self._task = None
        if isinstance(delivery, _Delivery) and delivery.serial == self._serial:
            if delivery.error: self.status.setText(delivery.error)
            elif self._cancel_requested and delivery.action != "restore":
                self.status.setText("Cancelled; no new comparison was authorized.")
            elif delivery.action == "browse": self.loaded(delivery.value)
            elif delivery.action == "compare":
                self._comparison = delivery.value
                self._choices = {}
                self.render_comparison()
            elif delivery.action == "review choices":
                self.render_choice_review(delivery.value)
            elif delivery.action == "restore":
                counts = {}
                for outcome in delivery.value: counts[outcome.status] = counts.get(outcome.status, 0) + 1
                self.status.setText(" · ".join(f"{key}: {value}" for key, value in counts.items()) + ". Each image is a separate transaction; reload to inspect the new versions.")
                self.detail.setPlainText("\n".join(f"{row.image.name}: {row.status}. {row.message}" for row in delivery.value))
                self._comparison = None
                self._choice_reviewed = False
                self.restorationFinished.emit(delivery.value)
            elif delivery.action == "update index":
                self.status.setText("Index is ready. Reload History." if delivery.value else "Index update is incomplete. Run another bounded update for this selected image.")
        else: self.status.setText(error or "A stale result was ignored.")
        self.update_actions()
        if self._closing: super().reject()

    def load_page(self, *, reset=False):
        if self._task is not None or self._closing: return
        if reset:
            self._records, self._states = [], []
            self._cursor = None
            self.invalidate_comparison()
        elif self._cursor is None: return
        if len(self._records) + len(self._states) >= MAX_VISIBLE_RECORDS:
            self.status.setText("This view reached 2000 records. Choose one image in Versions to narrow the page; selected targets stay explicit.")
            return
        cursor = self._cursor
        image = self.image_filter.currentData() if self.tabs.currentIndex() == 1 else None
        self._launch("browse", lambda cancelled: self.browser.page(cursor, image=image, cancelled=cancelled))

    def loaded(self, page):
        self._records.extend(page.versions)
        self._states.extend(page.states)
        self._cursor = page.next_cursor
        self._metadata_complete = page.complete and not any(state.status not in {"no_record", "not_recorded"} for state in self._states)
        self.render_records()
        self.empty.setVisible(not self._records)
        self.empty.setTitle("No versions loaded" if self._states else "No recorded versions")
        self.empty.setText("Review the per-image states above. Opening history never registers missing records." if self._states else "No committed versions were recorded for this scope.")
        self.status.setText("All matching selected-image metadata loaded." if self._metadata_complete else
            "Partial metadata page. Load More to see remaining versions and selected operation members." if self._cursor else
            "Some selected images are held or not loaded. Review their states; operation membership remains partial.")

    def render_records(self):
        self._rendering = True
        self.changes.clear(); self.versions.clear()
        groups, members = {}, set()
        for row in self._records:
            version = row.version
            timestamp = version.source_timestamp or version.committed_at or version.observed_at or "Not recorded"
            producer = " / ".join(value for value in (version.origin, version.producer) if value) or "Not recorded"
            coverage = ", ".join(version.coverage) or "Not recorded"
            item = QTreeWidgetItem([row.image.name + " · " + version.version_id[:10], timestamp, producer, coverage])
            selection = BrowserSelection(row.image, version_id=version.version_id)
            supported = bool(ImageKnowledgeHistory.restore_capabilities(version))
            self._add_target(item, selection, supported=supported)
            if not supported: item.setText(3, "Unsupported saved schema or restoration coverage; retained read-only")
            item.setToolTip(0, str(row.image) + "\nVersion: " + version.version_id)
            self.versions.addTopLevelItem(item)
            key = (version.scope_key, version.operation_id)
            if key not in groups:
                group = QTreeWidgetItem([version.operation_kind or version.operation_id or "Operation not recorded", timestamp, producer, version.reason or "Reason not recorded"])
                group.setFlags(group.flags() & ~Qt.ItemIsUserCheckable)
                group.setToolTip(0, "Operation: " + version.operation_id + "\nMembers are limited to the fixed image selection and loaded metadata pages.")
                self.changes.addTopLevelItem(group)
                groups[key] = group
            member_key = (key, row.image)
            if member_key not in members:
                members.add(member_key)
                child = QTreeWidgetItem([row.image.name, timestamp, producer, coverage])
                self._add_target(child, BrowserSelection(row.image, operation_id=version.operation_id), supported=supported)
                if not supported: child.setText(3, "Unsupported saved schema or restoration coverage; retained read-only")
                groups[key].addChild(child)
        for state in self._states:
            for tree in (self.changes, self.versions):
                item = QTreeWidgetItem([state.image.name, state.status.replace("_", " ").title(), "", state.message])
                item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
                item.setData(0, Qt.UserRole + 1, state.image)
                item.setToolTip(3, state.message)
                tree.addTopLevelItem(item)
        self.changes.expandAll()
        self._rendering = False
        self.counts.setText(f"{len(self._records):,} versions loaded · {len(self._selected):,} exact targets selected · " + ("Loaded scope complete" if self._metadata_complete else "Partial scope"))

    def _add_target(self, item, selection, *, supported=True):
        item.setData(0, Qt.UserRole, selection)
        item.setData(0, Qt.UserRole + 1, selection.image)
        item.setFlags(item.flags() & ~Qt.ItemIsUserCheckable)
        if not supported or self.browser.operation_target and selection.operation_id != self.browser.operation_target:
            return
        item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
        item.setCheckState(0, Qt.Checked if self._selected.get(str(selection.image)) == selection else Qt.Unchecked)

    def target_changed(self, item, column):
        if self._rendering: return
        selection = item.data(0, Qt.UserRole)
        if selection is None: return
        key = str(selection.image)
        if item.checkState(0) == Qt.Checked: self._selected[key] = selection
        elif self._selected.get(key) == selection: self._selected.pop(key, None)
        self.invalidate_comparison()
        # Do not destroy an item while Qt is still delivering its change signal.
        self._render_timer.start(0)

    def clear_targets(self):
        self._selected.clear()
        self.invalidate_comparison()
        self.render_records()

    def toggle_categories(self):
        self.additional.setVisible(self.additional.isHidden())

    def selected_units(self):
        primary = self.units.currentData()
        if primary in {"record", "record_absence"}: return (RestoreUnit(primary),)
        keys = [primary] + [self.additional.item(index).data(Qt.UserRole) for index in range(self.additional.count()) if self.additional.item(index).checkState() == Qt.Checked]
        return tuple(RestoreUnit(key) for key in dict.fromkeys(keys))

    def invalidate_comparison(self, *args):
        self._comparison = None
        self._choices = {}
        self._choice_rows = []
        self._choice_reviewed = False
        self.comparison.setRowCount(0)
        self.detail.clear()
        self.update_actions()

    def compare(self):
        units = self.selected_units()
        selections = tuple(BrowserSelection(item.image, item.version_id, item.operation_id, units) for item in self._selected.values())
        self.invalidate_comparison()
        self._launch("compare", lambda cancelled: self.browser.preview(selections, cancelled=cancelled))

    def render_comparison(self):
        # Choice-aware values are resolved by the shared typed core, not Qt.
        self._choice_reviewed = False
        self._launch("review choices", lambda cancelled: self.browser.review_choices(self._comparison, choices=self._choices, cancelled=cancelled))

    def render_choice_review(self, review):
        self._choice_rows = list(review.rows)
        self.comparison.setRowCount(len(self._choice_rows))
        self._rendering = True
        for index, row in enumerate(self._choice_rows):
            label = comparison_label(row.label)
            values = [row.image.name, label, row.status, *(comparison_value_text(value, row.label) for value in (row.historical, row.current, row.proposed))]
            for column, value in enumerate(values):
                item = QTableWidgetItem(value[:350])
                item.setToolTip(value[:4000])
                self.comparison.setItem(index, column, item)
            combo = QComboBox()
            combo.setAccessibleName("Restore choice for " + label + " on " + row.image.name)
            for text, value in (("Choose…", ""), ("Keep current", "current"), ("Use historical", "historical")):
                combo.addItem(text, value)
            combo.setCurrentIndex(max(0, combo.findData(self._choices.get(str(row.image), {}).get(row.choice_key, ""))))
            combo.setEnabled(row.changed)
            combo.currentIndexChanged.connect(lambda _, r=row, c=combo: self.choice_changed(r, c.currentData()))
            self.comparison.setCellWidget(index, 6, combo)
        self._rendering = False
        self._choice_reviewed = bool(review.ready)
        self.status.setText(review.message)
        self.show_detail()
        self.update_actions()

    def choice_changed(self, row, value):
        if self._rendering: return
        self._choices.setdefault(str(row.image), {})[row.choice_key] = value
        self._choice_reviewed = False
        self.status.setText("Choices changed. Review Proposed Changes to recompute the exact result and dependency checks.")
        self.update_actions()

    def choose_rows(self, choice):
        for index in {item.row() for item in self.comparison.selectedItems()}:
            combo = self.comparison.cellWidget(index, 6)
            if combo.isEnabled(): combo.setCurrentIndex(combo.findData(choice))

    def review(self):
        self._launch("review choices", lambda cancelled: self.browser.review_choices(self._comparison, choices=self._choices, cancelled=cancelled))

    def show_detail(self):
        index = self.comparison.currentRow()
        if 0 <= index < len(self._choice_rows):
            row = self._choice_rows[index]
            values = (row.historical, row.current, row.proposed)
            if not self.exact_data.isChecked():
                values = tuple(comparison_value_text(value, row.label) for value in values)
            self.detail.setPlainText(f"{row.image}\n{comparison_label(row.label)}: {row.status}\nHistorical:\n{values[0]}\nCurrent:\n{values[1]}\nProposed:\n{values[2]}")

    def apply(self):
        if not self._choice_reviewed or self._comparison is None: return
        comparison, choices = self._comparison, {key: dict(value) for key, value in self._choices.items()}
        self._choice_reviewed = False
        self._launch("restore", lambda cancelled: self.browser.apply(comparison, choices=choices, cancelled=cancelled))

    def index_selection(self):
        tree = self.changes if self.tabs.currentIndex() == 0 else self.versions
        selected = tree.selectedItems()
        self._index_image = selected[0].data(0, Qt.UserRole + 1) if len(selected) == 1 else None
        self.update_actions()

    def update_index(self):
        image = self._index_image
        if image is not None: self._launch("update index", lambda cancelled: self.browser.update_index(image, cancelled=cancelled))

    def open_recovery(self):
        if self._task is None and self.recovery_handler is not None and self._index_image is not None:
            self.recovery_handler(self._index_image)

    def update_actions(self):
        idle = self._task is None and not self._closing
        busy = self._task is not None
        if busy != self._reported_busy:
            self._reported_busy = busy
            self.busyChanged.emit(busy)
        for control in (self.refresh, self.tabs, self.units, self.additional, self.companions, self.clear_selection): control.setEnabled(idle)
        self.next_page.setEnabled(idle and self._cursor is not None and len(self._records) + len(self._states) < MAX_VISIBLE_RECORDS)
        self.index_button.setEnabled(idle and self._index_image is not None)
        self.index_button.setVisible(any(state.status == "not_loaded" for state in self._states))
        self.recovery_button.setVisible(self.recovery_handler is not None and any(state.status == "needs_recovery" for state in self._states))
        self.recovery_button.setEnabled(idle and self.recovery_handler is not None and
            any(state.image == self._index_image and state.status == "needs_recovery" for state in self._states))
        self.compare_button.setEnabled(idle and bool(self._selected))
        for control in (self.comparison, self.keep, self.historical, self.review_choices): control.setEnabled(idle and self._comparison is not None)
        self.apply_button.setEnabled(idle and self._choice_reviewed and self._comparison is not None)
        self.apply_button.setText("Restore Complete Version" if self.units.currentData() == "record" else "Restore Recorded Absence" if self.units.currentData() == "record_absence" else "Restore Selected Data")
        self.cancel.setVisible(not idle and not self._closing)
        self.progress.setVisible(not idle)

    def cancel_work(self):
        self._cancel_requested = True
        if self._task is not None and self._task.job_id:
            self.jobs.cancel(self._task.job_id)

    def reject(self):
        if self._close_callback is not None and not self._closing:
            self._close_callback()
            return
        self._closing = True
        if self._task is not None:
            self.cancel_work()
            self.update_actions()
            return
        super().reject()

    def closeEvent(self, event):
        event.ignore()
        self.reject()
