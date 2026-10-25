"""Typed Collection decisions inside the shared saved-history workspace."""
import json
import hashlib
from PySide6.QtCore import Qt
from PySide6.QtWidgets import (QHBoxLayout, QPushButton, QTreeWidget, QTreeWidgetItem,
    QHeaderView, QCheckBox, QPlainTextEdit, QSplitter)

from .history_domain_panel import HistoryDomainPanel


class CollectionHistoryPanel(HistoryDomainPanel):
    def __init__(self, browser, jobs, parent=None):
        super().__init__(jobs, parent)
        self.browser = browser
        self._loaded = False
        self._cursor = None
        self._version = None
        self._comparison = None
        self._observation = None
        self._observation_offset = 0
        self._observation_next = None
        self._value_row = None
        self._value_offset = 0
        self._member_keys = set()
        self._member_offset = 0
        self._next_member = None
        self._companion_offset = 0
        self._rendering = False
        self.body.addWidget(self.label("Collection: " + browser.group_id))
        self.body.addWidget(self.label("Choose a saved version and the exact details or membership decisions to restore. "
            "Current image identity and automatic rules are checked again. Frozen training artifacts stay unchanged."))
        controls = QHBoxLayout()
        self.reload_button = QPushButton("Reload History")
        self.reload_button.clicked.connect(lambda: self.load(reset=True))
        self.more = QPushButton("Load More Versions")
        self.more.clicked.connect(lambda: self.load())
        self.index_button = QPushButton("Update History List")
        self.index_button.setToolTip("Process the next bounded portion of retained history, then reload.")
        self.index_button.clicked.connect(self.update_index)
        self.observe_button = QPushButton("Review Current Saved State")
        self.observe_button.clicked.connect(self.review_observation)
        self.observe_button.setToolTip("Record the current saved Collection after a capture gap. Missing changes and unsaved drafts are not reconstructed.")
        for button in (self.reload_button, self.more, self.index_button, self.observe_button):
            controls.addWidget(button)
        controls.addStretch(1)
        self.body.addLayout(controls)
        self.versions = QTreeWidget()
        self.versions.setHeaderLabels(("Recorded time", "Version", "Origin / restored version"))
        self.versions.header().setSectionResizeMode(QHeaderView.Stretch)
        self.versions.currentItemChanged.connect(self.version_changed)
        self.members = QTreeWidget()
        self.members.setHeaderLabels(("Exact library / image", "Historical decision", "Current decision"))
        self.members.header().setSectionResizeMode(QHeaderView.Stretch)
        self.members.itemChanged.connect(self.member_changed)
        self.members.currentItemChanged.connect(self.observation_row_changed)
        split = QSplitter(Qt.Vertical)
        split.addWidget(self.versions)
        split.addWidget(self.members)
        self.body.addWidget(split, 1)
        fields = QHBoxLayout()
        self.fields = {}
        for key, title in (("name", "Name"), ("description", "Description"), ("rules", "Automatic Rules")):
            check = QCheckBox(title)
            check.toggled.connect(self.invalidate)
            self.fields[key] = check
            fields.addWidget(check)
        fields.addStretch(1)
        self.previous_members = QPushButton("Previous Members")
        self.previous_members.clicked.connect(lambda: self.load_members(max(0, self._member_offset - 100)))
        self.next_members = QPushButton("Next Members")
        self.next_members.clicked.connect(lambda: self.load_members(self._next_member))
        fields.addWidget(self.previous_members)
        fields.addWidget(self.next_members)
        self.body.addLayout(fields)
        self.selection = self.label("No decisions selected")
        self.body.addWidget(self.selection)
        self.detail = QPlainTextEdit()
        self.detail.setReadOnly(True)
        self.detail.setMinimumHeight(125)
        self.detail.setMaximumHeight(210)
        self.body.addWidget(self.detail)
        value_paging = QHBoxLayout()
        self.previous_value = QPushButton("Previous Value Page")
        self.previous_value.clicked.connect(lambda: self.show_value_page(max(0, self._value_offset - 4096)))
        self.next_value = QPushButton("Next Value Page")
        self.next_value.clicked.connect(lambda: self.show_value_page(self._value_offset + 4096))
        value_paging.addStretch(1)
        for button in (self.previous_value, self.next_value):
            value_paging.addWidget(button)
            button.hide()
        self.body.addLayout(value_paging)
        review = QHBoxLayout()
        self.overwrite = QCheckBox("Replace conflicts in the selected decisions")
        self.overwrite.toggled.connect(self.invalidate)
        self.confirm_rules = QCheckBox("I reviewed the automatic rule consequences")
        self.confirm_rules.toggled.connect(self.update_actions)
        self.confirm_rules.hide()
        self.companions = QPushButton("Next Rule Consequences")
        self.companions.clicked.connect(self.show_companions)
        review.addWidget(self.overwrite)
        review.addWidget(self.confirm_rules)
        review.addStretch(1)
        review.addWidget(self.companions)
        self.body.addLayout(review)
        observation_review = QHBoxLayout()
        self.confirm_observation = QCheckBox("Record the full current saved Collection; missing changes are not reconstructed")
        self.confirm_observation.toggled.connect(self.update_actions)
        self.previous_observation = QPushButton("Previous Changes")
        self.previous_observation.clicked.connect(lambda: self.show_observation(max(0, self._observation_offset - 100)))
        self.next_observation = QPushButton("Next Changes")
        self.next_observation.clicked.connect(lambda: self.show_observation(self._observation_next))
        for control in (self.confirm_observation, self.previous_observation, self.next_observation):
            observation_review.addWidget(control)
            control.hide()
        self.body.addLayout(observation_review)
        actions = QHBoxLayout()
        self.clear = QPushButton("Clear Decisions")
        self.clear.clicked.connect(self.clear_selection)
        self.compare_button = QPushButton("Compare Selected Decisions")
        self.compare_button.clicked.connect(self.compare)
        self.apply_button = QPushButton("Restore Selected Decisions")
        self.apply_button.setProperty("primary", True)
        self.apply_button.clicked.connect(self.apply)
        self.record_button = QPushButton("Record Current Saved State")
        self.record_button.setProperty("primary", True)
        self.record_button.clicked.connect(self.record_observation)
        self.record_button.hide()
        close = QPushButton("Close")
        close.clicked.connect(self.closeRequested)
        for button in (self.clear, self.compare_button, close, self.apply_button, self.record_button):
            actions.addWidget(button)
        self.body.addLayout(actions)
        for button in self.findChildren(QPushButton):
            button.setAutoDefault(False)
        self.update_actions()

    def activate(self):
        if not self._loaded:
            if self.browser.observation_only:
                self._loaded = True
                self.review_observation()
            else:
                self.load(reset=True)

    def invalidate(self, *_args):
        self._comparison = None
        self._observation = None
        self._observation_offset = 0
        self._observation_next = None
        self._value_row = None
        self.previous_value.hide()
        self.next_value.hide()
        self.browser.invalidate()
        self.detail.clear()
        self.confirm_rules.setChecked(False)
        self.confirm_rules.hide()
        self.confirm_observation.setChecked(False)
        self.members.setHeaderLabels(("Exact library / image", "Historical decision", "Current decision"))
        self.update_actions()

    def clear_selection(self):
        self._member_keys.clear()
        for check in self.fields.values():
            check.setChecked(False)
        self.overwrite.setChecked(False)
        self.invalidate()
        if self._version is not None:
            self.load_members(self._member_offset)

    def load(self, *, reset=False):
        if self._task is not None:
            return
        if reset:
            self._cursor = None
            self.versions.clear()
            self.version_changed(None)
        elif self._cursor is None:
            return
        self.launch("browse Collection", lambda cancelled: self.browser.page(self._cursor, cancelled=cancelled), self.loaded)

    def loaded(self, page):
        self._loaded = True
        self._cursor = page.next_cursor
        for version in page.versions:
            self.versions.addTopLevelItem(self.version_item(version))
        self.status.setText("Choose an exact version; no decisions are selected automatically." if self.versions.topLevelItemCount()
            else "No retained versions for this Collection. Missing history is not inferred.")

    @staticmethod
    def version_item(version):
        item = QTreeWidgetItem((version.created_at, version.version_id[:12], version.origin +
            (" · restored from " + version.restored_from[:12] if version.restored_from else "")))
        item.setData(0, Qt.UserRole, version)
        item.setToolTip(1, "Version: " + version.version_id + "\nOperation: " + version.operation_id)
        return item

    def update_index(self):
        self.invalidate()
        self.launch("update Collection history list", lambda cancelled: self.browser.update_index(cancelled=cancelled),
            lambda ready: self.status.setText("History list ready. Reload History to browse." if ready else
                "More history remains. Update History List again to continue the bounded scan."))

    def version_changed(self, item, *_args):
        self._version = item.data(0, Qt.UserRole) if item is not None else None
        self._member_keys.clear()
        self._member_offset = 0
        self._next_member = None
        self.members.clear()
        for check in self.fields.values():
            check.setChecked(False)
        self.overwrite.setChecked(False)
        self.invalidate()
        if self._version is not None:
            self.load_members(0)

    def load_members(self, offset):
        if self._version is None or offset is None or self._task is not None:
            return
        version = self._version
        def loaded(page):
            self._member_offset, self._next_member = offset, page.next_offset
            self._rendering = True
            self.members.clear()
            for row in page.rows:
                item = QTreeWidgetItem((" / ".join(row.key), self.member_text(row.historical_json), self.member_text(row.current_json)))
                item.setToolTip(1, row.historical_json)
                item.setToolTip(2, row.current_json)
                item.setData(0, Qt.UserRole, row.key)
                item.setFlags(item.flags() | Qt.ItemIsUserCheckable)
                item.setCheckState(0, Qt.Checked if row.key in self._member_keys else Qt.Unchecked)
                self.members.addTopLevelItem(item)
            self._rendering = False
            self.status.setText(f"Showing {offset + 1 if page.total else 0:,}–{min(offset + 100, page.total):,} of {page.total:,} exact membership decisions.")
        self.launch("browse Collection members", lambda _cancelled: self.browser.member_page(version, offset=offset), loaded)

    def member_changed(self, item, _column):
        if self._rendering or self._observation is not None:
            return
        key = item.data(0, Qt.UserRole)
        if item.checkState(0) == Qt.Checked:
            self._member_keys.add(key)
        else:
            self._member_keys.discard(key)
        self.invalidate()

    def compare(self):
        if self.browser.observation_only or self._observation is not None:
            return
        version = self._version
        fields = tuple(key for key, check in self.fields.items() if check.isChecked())
        members = tuple(sorted(self._member_keys))
        overwrite = self.overwrite.isChecked()
        self.invalidate()
        self.launch("compare Collection", lambda cancelled: self.browser.preview(version, fields=fields,
            member_keys=members, overwrite=overwrite, cancelled=cancelled), self.compared)

    @staticmethod
    def member_text(raw):
        value = json.loads(raw)
        if value["status"] == "absent":
            return "No membership decision (absent)"
        if value["status"] == "ignored":
            return "Excluded from this Collection"
        source = (value.get("row") or {}).get("membership_source", "manual")
        return "Included (active) · " + ("automatic rule" if source == "automatic" else "manually selected")

    @staticmethod
    def row_text(row):
        def value_text(raw):
            value = json.loads(raw)
            if row.kind == "member":
                return CollectionHistoryPanel.member_text(raw)
            return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False, indent=2)
        return (str(row.key) + (" · conflict" if row.conflict else "") +
            "\nCurrent: " + value_text(row.current_json) + "\nProposed: " + value_text(row.proposed_json))

    def compared(self, comparison):
        self._comparison = comparison
        self._companion_offset = 0
        plan = comparison.plan
        self.confirm_rules.setChecked(False)
        self.confirm_rules.setVisible(bool(plan.companions))
        self.detail.setPlainText("\n\n".join(self.row_text(row) for row in plan.rows[:100]) +
            (f"\n\n{len(plan.rows):,} selected decisions; first 100 shown. Exact members remain listed above." if len(plan.rows) > 100 else "") +
            "\n\n" + "\n".join(plan.held_reasons))
        self.status.setText(f"{len(plan.companions):,} automatic rule consequences. Review them before restoring." if plan.companions
            else "Review current and proposed values before restoring." if plan.can_apply else
            "Restoration is held. Review conflicts and the displayed reasons, then compare again.")

    def show_companions(self):
        if self._comparison is None:
            return
        page = self._comparison.plan.companion_page(offset=self._companion_offset, limit=100)
        self.detail.setPlainText("Automatic rule consequences\n\n" + "\n\n".join(self.row_text(row) for row in page.rows))
        self.status.setText(f"Rule consequences {self._companion_offset + 1:,}–{min(self._companion_offset + 100, page.total):,} of {page.total:,}.")
        self._companion_offset = page.next_offset or 0

    def apply(self):
        if self.browser.observation_only or self._observation is not None:
            return
        comparison = self._comparison
        if (comparison is None or not comparison.plan.can_apply
                or (comparison.plan.companions and not self.confirm_rules.isChecked())):
            return
        self._comparison = None
        self.confirm_rules.hide()
        def restored(outcomes):
            self.status.setText(" · ".join(value.message for value in outcomes))
            self.restorationFinished.emit(outcomes)
        self.launch("restore", lambda cancelled: self.browser.apply(comparison, cancelled=cancelled), restored)

    def review_observation(self):
        if self._task is not None:
            return
        self.invalidate()
        self._rendering = True
        self.members.clear()
        self._rendering = False
        self.launch("review current saved Collection", lambda cancelled: self.browser.preview_observation(cancelled=cancelled), self.observation_reviewed)

    def observation_reviewed(self, comparison):
        self._observation = comparison
        self.confirm_observation.setChecked(False)
        self.show_observation(0)

    def show_observation(self, offset):
        if self._observation is None or offset is None:
            return
        plan = self._observation.plan
        self._value_row = None
        self.previous_value.hide()
        self.next_value.hide()
        page = plan.page(offset=offset, limit=100)
        self._observation_offset, self._observation_next = offset, page.next_offset
        self._rendering = True
        self.members.clear()
        self.members.setHeaderLabels(("Saved Collection decision", "Last recorded value", "Current saved value"))
        for row in page.rows:
            def summary(raw):
                if len(raw) > 4096:
                    return f"Saved value: {len(raw):,} characters · select to inspect pages"
                value = json.loads(raw)
                if row.kind == "member":
                    return self.member_text(raw)
                return value if isinstance(value, str) else json.dumps(value, ensure_ascii=False)
            item = QTreeWidgetItem((str(row.key), summary(row.current_json), summary(row.proposed_json)))
            item.setData(0, Qt.UserRole + 1, row)
            item.setToolTip(1, self.bounded_value(row.current_json))
            item.setToolTip(2, self.bounded_value(row.proposed_json))
            self.members.addTopLevelItem(item)
        self._rendering = False
        description = {"initial_baseline": "No prior version exists. Record this saved Collection as its initial baseline.",
            "gap": "The current saved Collection differs from its last recorded version. Record an observed state after the missing interval.",
            "unchanged": "The current saved Collection is already represented in history; no new version is needed."}[plan.mode]
        self.detail.setPlainText(description + "\n\nMissing changes are not reconstructed. Unsaved editor choices, image files and frozen training artifacts stay unchanged.\n\n" + "\n".join(plan.held_reasons))
        self.selection.setText(f"{page.total:,} changed saved decisions · showing {offset + 1 if page.total else 0:,}–{min(offset + 100, page.total):,}")
        self.status.setText("Current-state recording is held. Review the displayed reason." if plan.held_reasons else description)
        self.update_actions()

    def observation_row_changed(self, item, *_args):
        if self._rendering or self._observation is None or item is None:
            return
        row = item.data(0, Qt.UserRole + 1)
        if row is None:
            return
        self._value_row = row
        self.show_value_page(0)

    @staticmethod
    def bounded_value(raw):
        if len(raw) <= 4096:
            return raw
        return (raw[:4096] + f"\n\nFirst 4,096 of {len(raw):,} characters. Select this row to inspect all value pages.\n"
            + "Exact value SHA-256: " + hashlib.sha256(raw.encode("utf-8")).hexdigest())

    def show_value_page(self, offset):
        row = self._value_row
        if row is None or self._observation is None:
            return
        total = max(len(row.current_json), len(row.proposed_json))
        if offset < 0 or offset >= max(total, 1):
            return
        self._value_offset = offset
        self.detail.setPlainText(str(row.key) + f" · characters {offset + 1:,}–{min(offset + 4096, total):,} of {total:,}\n\n"
            + "Last recorded value:\n" + row.current_json[offset:offset + 4096]
            + "\n\nCurrent saved value:\n" + row.proposed_json[offset:offset + 4096]
            + "\n\nThe full current saved Collection is recorded; missing changes are not reconstructed.")
        self.previous_value.setVisible(total > 4096)
        self.next_value.setVisible(total > 4096)
        self.previous_value.setEnabled(self._task is None and offset > 0)
        self.next_value.setEnabled(self._task is None and offset + 4096 < total)

    def record_observation(self):
        comparison = self._observation
        if (comparison is None or not comparison.plan.can_confirm
                or comparison.plan.mode == "unchanged" or not self.confirm_observation.isChecked()):
            return
        def recorded(result):
            self._version = None
            self.clear_selection()
            self.versions.setCurrentItem(None)
            for index in range(self.versions.topLevelItemCount() - 1, -1, -1):
                if self.versions.topLevelItem(index).data(0, Qt.UserRole) == result.version:
                    self.versions.takeTopLevelItem(index)
            self.versions.insertTopLevelItem(0, self.version_item(result.version))
            if self.versions.topLevelItemCount() > 2000:
                self.versions.takeTopLevelItem(2000)
            self.members.clear()
            self.detail.clear()
            self._value_row = None
            self.previous_value.hide()
            self.next_value.hide()
            self.status.setText("Current saved Collection recorded. Missing changes were not reconstructed. Close this view to continue your separate editor draft.")
            from core.collection_history_browser import CollectionHistoryOutcome
            self.restorationFinished.emit((CollectionHistoryOutcome(self.browser.group_id, "observed", result.outcome),))
        self.launch("record current saved Collection", lambda cancelled: self.browser.confirm_observation(comparison, cancelled=cancelled), recorded, writes=True)

    def update_actions(self, *_args):
        idle = self._task is None and not self._closing
        observing = self._observation is not None
        restore_mode = not observing and not self.browser.observation_only
        for control in (self.reload_button, self.index_button, self.versions, self.members, self.clear, self.overwrite, *self.fields.values()):
            control.setEnabled(idle)
        self.more.setEnabled(idle and self._cursor is not None and self.versions.topLevelItemCount() < 2000)
        self.observe_button.setEnabled(idle)
        for control in (self.versions, self.clear, self.overwrite, self.companions, self.compare_button,
                self.apply_button, self.previous_members, self.next_members, *self.fields.values()):
            control.setVisible(restore_mode)
        for control in (self.confirm_observation, self.previous_observation, self.next_observation, self.record_button):
            control.setVisible(observing)
        self.previous_observation.setEnabled(idle and observing and self._observation_offset > 0)
        self.next_observation.setEnabled(idle and observing and self._observation_next is not None)
        self.confirm_observation.setEnabled(idle and observing)
        self.record_button.setEnabled(idle and observing and self._observation.plan.can_confirm
            and self._observation.plan.mode != "unchanged" and self.confirm_observation.isChecked())
        self.previous_members.setEnabled(idle and self._version is not None and self._member_offset > 0)
        self.next_members.setEnabled(idle and self._next_member is not None)
        selected = sum(check.isChecked() for check in self.fields.values())
        if not observing:
            self.selection.setText(f"{selected} detail fields · {len(self._member_keys):,} exact member decisions selected")
        self.compare_button.setEnabled(idle and restore_mode and self._version is not None and bool(selected or self._member_keys))
        self.companions.setEnabled(idle and self._comparison is not None and bool(self._comparison.plan.companions))
        self.confirm_rules.setEnabled(idle)
        self.apply_button.setEnabled(idle and restore_mode and self._comparison is not None and self._comparison.plan.can_apply
            and (not self._comparison.plan.companions or self.confirm_rules.isChecked()))
        self.progress.setVisible(not idle)
        self.cancel.setVisible(not idle and not self._closing)
