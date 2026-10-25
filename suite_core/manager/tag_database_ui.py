"""Reusable Databases transaction pane for explicit host-owned Manager binding."""
from PySide6.QtCore import Qt, QTimer
from datetime import datetime, timezone
from core.connected_version_observation import derive_version_status
from PySide6.QtWidgets import (QCheckBox, QComboBox, QHBoxLayout, QLabel, QProgressBar,
    QPushButton, QToolButton, QScrollArea, QTextBrowser, QVBoxLayout, QWidget, QLineEdit, QFileDialog, QGroupBox)

from suite_core.manager.database_presentation import project_database_review, project_connection_error, project_connected_catalog
from suite_core.manager.tag_database_controller import TagDatabaseController
from suite_core.manager.ui_cards import compact_action, workspace_heading, workspace_layout, disclosure_panel


class TagDatabasePane(QWidget):
    def __init__(self, controller, *, encyclopedia=None, parent=None):
        super().__init__(parent)
        if type(controller) is not TagDatabaseController:
            raise TypeError("Explicit host-owned Tag controller required")
        self.controller, self.encyclopedia = controller, encyclopedia
        self._review = None
        self._render_key = None
        self._shown_error = ""
        self.setWindowTitle("Databases - Tag catalog review")
        layout = QVBoxLayout(self)
        layout.setContentsMargins(0, 0, 0, 0)
        scroll = QScrollArea(); scroll.setWidgetResizable(True)
        body = QWidget(); content = workspace_layout(body)
        content.addWidget(workspace_heading("Databases"))
        self.goal = self._label("Connect a Tag catalog, then preview the vocabulary you want to install or update. Your local edits and original image assignments are retained.")
        content.addWidget(self.goal)
        self.connection_box = QGroupBox("Connection draft - review before connecting")
        connection_layout = QVBoxLayout(self.connection_box)
        connection_layout.addWidget(self._label('Choose the Tag source data you reviewed, then choose an existing catalog folder or explicitly create a new catalog. Review checks this exact selection before it is saved.'))
        self.source_directory = QLineEdit(); self.source_directory.setAccessibleName("Approved Tag source data folder")
        self.catalog_directory = QLineEdit(); self.catalog_directory.setAccessibleName("Native Tag catalog folder")
        self.connection_browse = []
        for title, field in (("Approved source data folder", self.source_directory), ("Catalog folder", self.catalog_directory)):
            connection_layout.addWidget(self._label(title))
            row = QHBoxLayout(); row.addWidget(field)
            button = compact_action(QPushButton("Browse…"))
            self.connection_browse.append(button)
            button.clicked.connect(lambda _checked=False, target=field: self.browse_connection(target))
            row.addWidget(button); connection_layout.addLayout(row)
        self.create_catalog = QCheckBox("Create a new catalog in this missing or empty folder")
        connection_layout.addWidget(self.create_catalog)
        row = QHBoxLayout()
        self.review_connection = compact_action(QPushButton("Review connection"))
        self.open_connection = compact_action(QPushButton("Open saved connection"))
        self.connect_catalog = compact_action(QPushButton("Connect reviewed catalog"))
        for button in (self.review_connection, self.connect_catalog): row.addWidget(button)
        row.addStretch(); connection_layout.addLayout(row)
        self.confirm_connection = QCheckBox("I reviewed the exact source, catalog folder and creation decision")
        connection_layout.addWidget(self.confirm_connection)
        connection_actions = QHBoxLayout()
        self.connection_options = compact_action(QPushButton("Connection settings"))
        self.connection_options.setCheckable(True)
        self.connection_options.setChecked(not controller.choices)
        self.connection_options.toggled.connect(self.connection_box.setVisible)
        connection_actions.addWidget(self.connection_options); connection_actions.addWidget(self.open_connection)
        connection_actions.addStretch(); content.addLayout(connection_actions)
        content.addWidget(self.connection_box)
        self.connection_box.setVisible(controller.settings_path is not None and self.connection_options.isChecked())
        self.connection_options.setVisible(controller.settings_path is not None)
        self.open_connection.setVisible(controller.settings_path is not None)
        self.review_connection.clicked.connect(lambda: self.connect("connection_review"))
        self.open_connection.clicked.connect(lambda: self.connect("connection_open"))
        self.connect_catalog.clicked.connect(lambda: self.connect("connection_confirm"))
        self.source_directory.textChanged.connect(self.connection_changed)
        self.catalog_directory.textChanged.connect(self.connection_changed)
        self.create_catalog.toggled.connect(self.connection_changed)
        self.confirm_connection.toggled.connect(self.render)
        self._connection_review = None
        content.addWidget(self._label("Selected catalog"))
        self.catalog = QComboBox(); self.catalog.setAccessibleName("Select Tag catalog")
        for choice in controller.choices:
            self.catalog.addItem(choice.label)
        if not controller.choices:
            self.catalog.addItem("No catalog connected")
        content.addWidget(self.catalog)
        self.connected_context = self._label("")
        self.connected_context.setAccessibleName("Connected catalog folders")
        content.addWidget(self.connected_context)
        self.content_summary = self._label('No catalog connected. Choose or open a saved connection first.')
        content.addWidget(self.content_summary)
        self.versions = self._label("")
        self.version_details = disclosure_panel('Content versions and sources', self.versions)
        content.addWidget(self.version_details)
        start = QHBoxLayout()
        self.preview = compact_action(QPushButton("Preview approved content"))
        self.discard = compact_action(QPushButton("Discard preview"))
        start.addWidget(self.preview); start.addWidget(self.discard); start.addStretch()
        self.status = self._label(""); self.status.setAccessibleName("Tag transaction status")
        self.progress = QProgressBar(); self.progress.setRange(0, 0)
        self.progress.setAccessibleName("Native Tag operation progress")
        self.cancel = compact_action(QPushButton("Cancel safely"))
        self.sections = QComboBox(); self.sections.setAccessibleName("Tag review detail section")
        self.review_summary = self._label('Preview content to see the proposed changes before you confirm Apply.')
        self.review_summary.setAccessibleName('Tag changes summary')
        content.addWidget(self.review_summary)
        self.details = QTextBrowser(); self.details.setOpenExternalLinks(False)
        self.details.setAccessibleName("Tag transaction review details")
        self.details.setMinimumHeight(180); self.details.setMaximumHeight(260)
        self.detail_body = QWidget()
        detail_layout = QVBoxLayout(self.detail_body); detail_layout.setContentsMargins(0, 0, 0, 0)
        detail_layout.addWidget(self.sections); detail_layout.addWidget(self.details)
        self.review_details = disclosure_panel("Review details", self.detail_body)
        self.detail_options = self.review_details.findChild(QToolButton)
        content.addWidget(self.review_details)
        self.confirm = QCheckBox("I reviewed this catalog, content release and protected edits")
        finish = QHBoxLayout(); finish.addStretch()
        self.apply = compact_action(QPushButton("Apply reviewed changes")); finish.addWidget(self.apply)
        self.gate = self._label("")
        self.receipts = QComboBox(); self.receipts.setAccessibleName("Approved interrupted Tag import")
        recovery = QHBoxLayout()
        self.recover = compact_action(QPushButton("Recover selected import"))
        self.reload = compact_action(QPushButton("Reload saved catalog"))
        self.observe = compact_action(QPushButton("Refresh host observations"))
        self.open_encyclopedia = compact_action(QPushButton("Open Encyclopedia"))
        for button in (self.recover, self.observe, self.open_encyclopedia): recovery.addWidget(button)
        recovery.addStretch()
        content.addStretch()
        scroll.setWidget(body); layout.addWidget(scroll)
        # Loading the saved catalog and settling work must stay reachable while
        # connection settings and content-review details scroll independently.
        footer = QVBoxLayout(); footer.setContentsMargins(12, 6, 12, 12)
        self.review_actions = QGroupBox("Review and apply to selected catalog")
        review_layout = QVBoxLayout(self.review_actions)
        review_layout.addLayout(start); review_layout.addWidget(self.confirm)
        review_layout.addLayout(finish); review_layout.addWidget(self.gate)
        footer.addWidget(self.review_actions)
        self.recovery_actions = QGroupBox("Interrupted import recovery")
        recovery_layout = QVBoxLayout(self.recovery_actions)
        recovery_layout.addWidget(self.receipts); recovery_layout.addLayout(recovery)
        footer.addWidget(self.recovery_actions)
        footer.addWidget(self.progress)
        actions = QHBoxLayout(); actions.addWidget(self.status, 1)
        actions.addWidget(self.cancel); actions.addWidget(self.reload)
        footer.addLayout(actions); layout.addLayout(footer)
        self.catalog.currentIndexChanged.connect(self.select)
        self.preview.clicked.connect(lambda: self.request("preview"))
        self.discard.clicked.connect(self.discard_review)
        self.cancel.clicked.connect(self.discard_review)
        self.apply.clicked.connect(lambda: self.request("apply", confirmed=self.confirm.isChecked()))
        self.recover.clicked.connect(lambda: self.request("recover", receipt=self.receipts.currentData()))
        self.reload.clicked.connect(lambda: self.request("reload"))
        self.observe.clicked.connect(lambda: self.request("observe"))
        self.open_encyclopedia.clicked.connect(self.show_encyclopedia)
        self.sections.currentIndexChanged.connect(self.show_details)
        QWidget.setTabOrder(self.detail_options, self.sections)
        self.confirm.toggled.connect(self.render)
        self.timer = QTimer(self); self.timer.setInterval(40); self.timer.timeout.connect(self.poll)
        self.timer.start()
        self.render()

    @staticmethod
    def _label(text):
        label = QLabel(text); label.setWordWrap(True); label.setTextFormat(Qt.PlainText)
        label.setTextInteractionFlags(Qt.TextSelectableByMouse)
        return label

    def select(self, index):
        self.controller.select(index)
        self.render()

    def browse_connection(self, field):
        selected = QFileDialog.getExistingDirectory(self, "Select local folder", field.text())
        if selected:
            field.setText(selected)

    def connection_changed(self, *_):
        self.controller.discard_connection()
        self.confirm_connection.setChecked(False)
        self.render()

    def connect(self, kind):
        self.controller.connect(kind, source=self.source_directory.text(), catalog=self.catalog_directory.text(),
            create_new=self.create_catalog.isChecked(), confirmed=self.confirm_connection.isChecked())
        self.render()

    def request(self, kind, **kwargs):
        self.controller.request(kind, **kwargs)
        self.render()

    def discard_review(self):
        self.controller.cancel()
        self.render()

    def poll(self):
        delivered = self.controller.poll()
        if (delivered and self.controller.last_operation == "connection_confirm"
                and self.controller.result is not None and not self.controller.error):
            result = self.controller.result
            for field, value in ((self.source_directory, result.source_directory),
                                 (self.catalog_directory, result.choice.transactions.host.root)):
                field.blockSignals(True); field.setText(str(value)); field.blockSignals(False)
            self.create_catalog.blockSignals(True); self.create_catalog.setChecked(False); self.create_catalog.blockSignals(False)
            self.connection_options.setChecked(False)
        if (delivered and self.controller.result is not None and self.encyclopedia is not None
                and self.controller.last_operation != "observe"
                and not self.controller.error
                and not getattr(self.controller.result, "publication_error", "")):
            self._refresh_encyclopedia(show=False)
        self.render()
        return delivered

    def _refresh_encyclopedia(self, *, show):
        if self.encyclopedia is None or self.controller.choice is None:
            return
        destination = self.controller.choice.transactions.destination
        refreshing = self.encyclopedia.path == destination
        if not self.encyclopedia.open_snapshot(destination, refreshing=refreshing):
            self.controller.error = "Encyclopedia reader refresh failed. Retain the saved catalog and retry reload."
        if show:
            self.encyclopedia.show(); self.encyclopedia.raise_()

    def show_encyclopedia(self):
        self._refresh_encyclopedia(show=True)
        self.render()

    def render(self, *_):
        c = self.controller
        version_lines = self._version_lines(c.choice, datetime.now(timezone.utc))
        key = (id(c.review), id(c.result), c.reason, c.error, c.pending, c.phase,
               c.index, id(c.choice), c.closed, c.faulted, self.confirm.isChecked(),
               id(c.connection_review), self.confirm_connection.isChecked(), c.close_held,
               self.source_directory.text(), self.catalog_directory.text(), self.create_catalog.isChecked(),
               version_lines)
        if key == self._render_key:
            return
        self._render_key = key
        self.connected_context.setText(project_connected_catalog(c.choice))
        if c.connection_review is not self._connection_review:
            self._connection_review = c.connection_review
            if c.connection_review is not None: self.detail_options.setChecked(True)
            self.confirm_connection.blockSignals(True); self.confirm_connection.setChecked(False); self.confirm_connection.blockSignals(False)
        for field in (self.source_directory, self.catalog_directory, self.create_catalog, *self.connection_browse): field.setEnabled(c.can_connect)
        self.review_connection.setEnabled(c.can_connect and bool(self.source_directory.text().strip()) and bool(self.catalog_directory.text().strip()))
        self.open_connection.setEnabled(c.can_connect)
        self.connection_options.setEnabled(c.can_connect)
        self.confirm_connection.setEnabled(c.can_connect and c.connection_review is not None)
        self.connect_catalog.setEnabled(c.can_connect and c.connection_review is not None and self.confirm_connection.isChecked())
        if c.review is not self._review:
            self._review = c.review
            self.detail_options.setChecked(c.review is not None)
            self.confirm.blockSignals(True); self.confirm.setChecked(False); self.confirm.blockSignals(False)
        self.catalog.setEnabled(bool(c.choices) and not c.close_held and not c.closed)
        self.catalog.blockSignals(True)
        if self.catalog.count() != max(1, len(c.choices)) or bool(c.choices) and self.catalog.itemText(0) == "No catalog connected":
            self.catalog.clear()
            for choice in c.choices: self.catalog.addItem(choice.label)
            if not c.choices: self.catalog.addItem("No catalog connected")
        for index, choice in enumerate(c.choices): self.catalog.setItemText(index, choice.label)
        self.catalog.setCurrentIndex(max(c.index, 0)); self.catalog.blockSignals(False)
        choice = c.choice
        service = choice.transactions if choice else None
        state = project_database_review(service.controller if service else None, c.review.database if c.review else None)
        self._sections = [(section.title, section.text()) for section in state.sections]
        if c.connection_review is not None:
            connection = c.connection_review
            self._sections.insert(0, ("Connection review", f"Approved source: {connection.source_directory}\nCatalog: {connection.catalog_root}\n"
                f"Create new catalog: {'Yes — only a missing or empty folder' if connection.create_new else 'No — open an enrolled catalog'}\n"
                f"Content version: {connection.source_pin[0]}\nInventory SHA256: {connection.source_pin[1]}\n"
                "Connecting saves this exact selection and publishes its current catalog for the reader. Preview and Apply remain separate actions."))
        if c.review and c.review.holds:
            self._sections.append(("Native holds", "\n\n".join(c.review.holds)))
        capability_summary, capability_details = self._capability_presentation()
        if capability_details:
            self._sections.append(("Capabilities and unavailable behavior", capability_details))
        connection_error = project_connection_error(c.error, c.last_operation)
        if c.error:
            self._sections.insert(0, ("Operation reason", connection_error[1] if connection_error else c.error))
        prior = ("Operation reason" if c.error and c.error != self._shown_error else
                 "Connection review" if c.connection_review is not None else self.sections.currentText().split(" (")[0])
        if c.error and c.error != self._shown_error: self.detail_options.setChecked(True)
        self._shown_error = c.error
        self.sections.blockSignals(True); self.sections.clear()
        for title, _text in self._sections: self.sections.addItem(title)
        self.sections.setCurrentIndex(max(0, self.sections.findText(prior))); self.sections.blockSignals(False)
        self.show_details()
        if c.pending:
            self.review_summary.setText('A catalog operation is in progress. Follow the status below; review and Apply wait for the original work to finish.')
        elif c.error:
            self.review_summary.setText('The last operation needs attention. Review its reason and follow the recovery or Reload guidance below before starting another change.')
        elif state.review_accepted and all(section.total is not None for section in state.sections[:4]):
            counts = [section.total for section in state.sections[:4]]
            labels = ('new tag', 'tag update', 'protected local edit', 'conflict')
            summary = ' · '.join(f'{count} {label if count == 1 else label + "s"}' for count, label in zip(counts, labels))
            self.review_summary.setText(summary + '. Review the changes below; original image assignments are kept.')
        else:
            self.review_summary.setText('No current change review. Refresh observations or choose Preview approved content when available.' if choice is not None else
                'Connect or open a saved catalog, then choose Preview approved content to review its changes.')
        if state.review_accepted:
            review = c.review.database
            self.content_summary.setText('Installed content: ' + (review.installed_version or 'Not installed')
                + ' · Provided content for this preview: ' + (review.available_version or 'Unavailable'))
        elif choice is not None and choice.observation_status == 'current':
            self.content_summary.setText('Installed content: ' + (choice.base_release.revision.content_version if choice.installed else 'Verified blank catalog')
                + ' · Provided content: ' + (choice.available_release.revision.content_version if choice.available_release else 'Unavailable'))
        else:
            self.content_summary.setText('Content version is unknown. Open a saved catalog or refresh its observations; no new content is assumed.')
        if c.review:
            versions = "\n".join(state.versions)
        elif choice and choice.native_adapter is not None:
            if choice.observation_status == "current":
                versions = "Installed approved content: " + (choice.base_release.revision.content_version if choice.installed else "verified selected blank baseline")
                versions += "\nSupplied available content: " + (choice.available_release.revision.content_version if choice.available_release else "unavailable")
            else:
                versions = "Installed approved content: unknown. Refresh host observations or finish exact pending recovery."
        elif c.result is not None:
            if hasattr(c.result, "native_result"):
                versions = "Applied approved content: " + choice.available_release.revision.content_version
            else:
                versions = "Saved catalog verified. Installed content version needs a fresh approved observation."
        else:
            versions = "Approved inputs for next preview: " + (f"{choice.base_release.revision.content_version if choice.installed else 'blank baseline'} -> {choice.available_release.revision.content_version if choice.available_release else 'unavailable'}" if choice else "unknown")
        if version_lines:
            versions += "\n" + "\n".join(version_lines)
        if choice and c.settings_path is not None:
            versions += "\nCatalog folder: " + str(choice.transactions.host.root)
        self.versions.setText(versions + ("\n" + capability_summary if capability_summary else ""))
        self.status.setText((c.reason + "\n" + c.phase if c.cancel_requested else c.phase)
                           if c.pending else connection_error[0] if connection_error else c.reason)
        self.progress.setVisible(c.pending)
        self.preview.setEnabled(c.can_request and (not choice or choice.observation_status not in {"unknown", "recovery_pending"}))
        self.discard.setEnabled(bool(c.review) and not c.pending)
        self.cancel.setEnabled(c.pending)
        self.cancel.setVisible(c.pending)
        self.confirm.setEnabled(c.can_apply)
        self.apply.setEnabled(c.can_apply and self.confirm.isChecked())
        self.gate.setText("This catalog has no qualified host Apply route. Preview remains available." if not c.apply_authorized else
            "Apply uses this exact review. Cancellation after admission waits for saved work and retains recovery.")
        self.receipts.blockSignals(True)
        selected = self.receipts.currentData(); self.receipts.clear()
        for receipt in choice.recovery_receipts if choice else ():
            self.receipts.addItem("Import receipt: " + receipt.parent.name, receipt)
        if not self.receipts.count(): self.receipts.addItem("No approved recovery receipt supplied", None)
        self.receipts.setCurrentIndex(max(0, self.receipts.findData(selected))); self.receipts.blockSignals(False)
        self.receipts.setEnabled(c.can_request and bool(choice.recovery_receipts)) if choice else self.receipts.setEnabled(False)
        self.recover.setEnabled(c.can_request and c.apply_authorized and self.receipts.currentData() is not None
                                and (choice.native_adapter is None or len(choice.recovery_receipts) == 1))
        self.observe.setEnabled(c.can_request)
        self.reload.setEnabled(c.can_request)
        self.open_encyclopedia.setEnabled(c.can_request and self.encyclopedia is not None)

    def _version_lines(self, choice, now):
        """Reproject worker-supplied dates; never perform a native read here."""
        observation = choice.version_observation if choice else None
        if observation is None:
            return ("Content update status: Unknown; no dated installed-content observation supplied.",)
        reconciled = derive_version_status(observation.binding, installed=observation, now=now)
        freshness = observation.freshness_at(now).value
        lines = ["Last observed content version: " + (observation.raw_version or "unknown"),
            "Content observation: " + freshness + "; last success " +
            (observation.observed_at.isoformat() if observation.observed_at else "unknown"),
            "Content update status: Unknown; no latest-release comparison supplied."]
        retained = self.controller.pending or bool(self.controller.error) or choice.observation_status == "recovery_pending"
        if retained:
            lines.append("Retained evidence; finish the pending operation or recovery, then refresh observations.")
        elif reconciled.installation_condition == "changed_requires_review":
            lines.append("Changed installed release requires review; the selected baseline is retained.")
        elif observation.state == "observed" and freshness == "fresh" and choice.available_release is not None:
            if observation.evidence_ref == choice.available_release.fingerprint:
                lines.append("Matches selected approved source; this does not establish the latest release.")
            else:
                lines.append("Selected approved source differs; review required. Release ordering is unknown.")
        return tuple(lines)

    def _capability_presentation(self):
        c = self.controller
        state = c.capability_state
        if state == "not_applicable":
            return "", ""
        if state == "missing":
            if c.choice.observation_status == "recovery_pending":
                text = "Capability information is unavailable during interrupted import. Recover selected import, then refresh observations."
                return text, text
            text = "Capability report unavailable. Preview or refresh host observations to check this selected catalog."
            return text, text + ("\n" + c.choice.capability_error if c.choice.capability_error else "")
        if state == "stale":
            text = "Capability observation is stale or pending. Refresh observations or reload the saved catalog."
            return text, text + "\nCached counts are withheld until the selected inventory and published revision match."
        report = c.choice.capability_report
        available = sum(row.executable for _identity, row in report.executable_policies)
        unavailable = len(report.executable_policies) - available
        heading = ("Installed with preserved legacy declarations." if state == "installed" else
            "Preview only; behavior describes the current catalog before this update." if state == "preview" else
            "Preservation findings need review." if state == "held" else "Selected-catalog capability observation available.")
        summary = heading + f" {available} validated policies; {unavailable} unavailable. Full execution is not qualified."
        lines = [summary, "Counts describe this selected recipient and inventory, including its overrides, at the last verified observation.",
            "Declarations and lexical suggestions do not grant policy execution."]
        for owner, row in report.annotations:
            if row.kind == "legacy_value_annotation":
                lines.append(f"Retained annotation: {owner} / {row.reference} — {row.reason}")
        for owner, row in report.declarations:
            lines.append(f"Retained declaration: {owner} / {row.reference}; lexical suggestions: {'available' if row.lexical_bank_available else 'unavailable'}; "
                f"native family candidate: {row.native_family_id or 'none'}; {row.reason}")
        for owner, row in report.executable_policies:
            if not row.executable:
                lines.append(f"Unavailable policy: {owner} — {row.reason}")
        for finding in (*report.preservation_findings, *report.endpoint_findings):
            lines.append("Preservation finding: " + str(finding))
        return summary, "\n\n".join(lines)

    def show_details(self, *_):
        index = self.sections.currentIndex()
        if hasattr(self, "_sections") and 0 <= index < len(self._sections):
            self.details.setPlainText(self._sections[index][1])

    def closeEvent(self, event):
        self.controller.begin_close()
        self.render()
        if self.controller.close_held:
            event.ignore()
        else:
            self.timer.stop(); event.accept()
