"""Applications acquisition decisions; the host retains the executor/root owner.

This panel never constructs installation authority. Production integration must
inject the qualified root-bound service; unavailable admission stays visible.
"""
from queue import SimpleQueue, Empty
from threading import Event
from pathlib import Path

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QWidget, QLabel, QPushButton, QCheckBox, QHBoxLayout, QPlainTextEdit, QGroupBox, QVBoxLayout, QMessageBox, QSizePolicy, QToolButton, QTextBrowser

from .ui_cards import compact_action, workspace_layout, workspace_heading, disclosure_panel


class _ReleaseNotes(QTextBrowser):
    def loadResource(self, resource_type, url):
        # Publisher text must never read local images or fetch embedded resources.
        return None


def _primary_action(button, enabled):
    if button.property('primary') != enabled:
        button.setProperty('primary', enabled)
        button.style().unpolish(button); button.style().polish(button); button.update()


class EncyclopediaAcquisitionPanel(QWidget):
    def __init__(self, service, executor, *, parent=None, connection_changed=None, recovery_only=False):
        super().__init__(parent)
        self.service, self.executor = service, executor
        self.connection_changed = connection_changed
        self.recovery_only = recovery_only
        self.future = self.operation = self.reviewed = self.offer = self.installed = None
        self.observation_stale = False
        self.publication_pending = False
        self.reconcile_queued = False
        self.closing = False
        self.cancel = Event()
        self.progress = SimpleQueue()
        self.confirming = False
        layout = workspace_layout(self)
        title = workspace_heading("Tag Encyclopedia")
        from qt_app.navigation_icons import navigation_icon
        heading = QHBoxLayout(); icon = QLabel(); icon.setPixmap(navigation_icon('Tag Encyclopedia').pixmap(32,32))
        heading.addWidget(icon); heading.addWidget(title,1); layout.addLayout(heading)
        scope = QLabel(self.scope_text())
        scope.setWordWrap(True); layout.addWidget(scope)
        self.status = QLabel(self.initial_status_text() if service.install_available else
            "Installation is unavailable in this root. Use the existing reader connection controls.")
        self.status.setWordWrap(True); layout.addWidget(self.status)
        self.version_summary = QLabel("No reader selected."); self.version_summary.setWordWrap(True)
        layout.addWidget(self.version_summary)
        self.destination = QLabel(""); self.destination.setWordWrap(True); layout.addWidget(self.destination)
        self.release_notes = _ReleaseNotes(self); self.release_notes.setPlainText("Available reader notes will appear here.")
        self.release_notes.setOpenLinks(False); self.release_notes.setOpenExternalLinks(False)
        self.release_notes.setMinimumHeight(100); self.release_notes.setMaximumHeight(180)
        notes = disclosure_panel("What's New", self.release_notes); layout.addWidget(notes)
        self.notes_toggle = notes.findChild(QToolButton)
        self.details = QPlainTextEdit("Release checks contact the first-party publisher. Installation requires your confirmation.",self)
        self.details.setReadOnly(True); self.details.setMaximumHeight(160)
        technical = disclosure_panel("Technical Details", self.details); layout.addWidget(technical)
        self.technical = technical.findChild(QToolButton)
        self.technical.toggled.connect(lambda _checked: self.render())
        layout.addStretch()
        row = self.footer = QHBoxLayout()
        self.refresh = compact_action(QPushButton("Check Again"))
        self.install = compact_action(QPushButton("Install"))
        self.stop = compact_action(QPushButton("Cancel"))
        self.retry_save = compact_action(QPushButton("Retry save connection"))
        self.recover = compact_action(QPushButton("Recover installation"))
        self.health_accept = compact_action(QPushButton("Check app status"))
        self.run_recovery_reader = compact_action(QPushButton('Run reader to finish setup')) if recovery_only else None
        self.health_accept.setToolTip("Check the saved reader's setup. Run the reader and close its window normally; setup finishes automatically when that exact run is confirmed.")
        for button in (self.install, self.refresh, self.stop):
            row.addWidget(button)
        for button in (self.retry_save, self.recover, self.health_accept):
            row.addWidget(button)
        row.addStretch(); layout.addLayout(row)
        if recovery_only:
            row.insertWidget(3,self.run_recovery_reader)
            self.run_recovery_reader.clicked.connect(lambda:self._start(
                'Running the original reader; close its window normally to finish setup',self.service.run_reader))
        self.recovery_confirm = QCheckBox("Restore the retained previous executable and saved connection; preserve user data.")
        self.recovery_confirm.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        layout.insertWidget(layout.count()-2,self.recovery_confirm)
        self.refresh.clicked.connect(self.refresh_releases)
        self.install.clicked.connect(self.install_reviewed)
        self.stop.clicked.connect(self.request_cancel)
        self.retry_save.clicked.connect(self.retry_connection)
        self.recover.clicked.connect(self.recover_installation)
        self.health_accept.clicked.connect(self.reconcile_launch)
        self.recovery_confirm.toggled.connect(self.render)
        self.timer = QTimer(self); self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll); self.timer.start()
        if recovery_only:
            title.setText('Tag Encyclopedia needs attention')
            scope.setText('Keep this window open while the original update is held. Check its status, then use an available recovery action. Your catalog is not changed by these controls.')
            self.status.setText('Check app status to review the held update. No operation will be repeated automatically.')
            self.status.setTextFormat(Qt.PlainText)
            self.details.setPlainText(service.failure_details)
            notes.hide(); self.version_summary.hide(); self.destination.hide()
            for widget in (self.refresh,self.install,self.stop):
                widget.hide()
            self.health_accept.setToolTip('Check the original held update and its recovery requirements. This does not launch a reader or declare it healthy.')
        self.render()

    @property
    def close_held(self):
        # Completed Future stays retained until its UI result is reconciled.
        return self.confirming or self.future is not None or bool(getattr(self.executor, "close_held", False))

    def scope_text(self):
        return "Install or update the reader. Your catalogs stay separate, and opening the reader is always your choice."

    def initial_status_text(self):
        return "Check for an available reader when ready."

    def observation_text(self):
        selected = None if self.installed is None else self.installed.selected
        return ("Reader installation has not been checked." if self.installed is None else
            "No reader installed." if selected is None else
            "Installed reader: " + selected.version + (" — setup needs a normal reader close." if self.installed.phase == 'activated' else ""))

    def available_version_label(self):
        return "Available reader: "

    def available_status_text(self, offer):
        return f"Reader {offer.release.software_version} is available. Choose Install or Update to review it."

    def empty_release_status_text(self):
        return ("No installable reader was found on this channel. Check Again later." if self.installed.selected is None else
            "No newer reader was found on this channel.")

    def _start(self, operation, work):
        if self.close_held or self.closing:
            return
        self.cancel = Event()
        self.operation = operation
        self.status.setText(f"{operation}. Please wait.")
        try:
            self.future = self.executor.submit(work)
        except Exception as error:
            self.operation = None
            self.status.setText(f"Could not start the operation. {error}")
        self.render()

    def refresh_releases(self):
        if self.recovery_only:
            return
        self.offer = self.reviewed = None
        self.observation_stale = self.installed is not None
        self._start("Refreshing releases", lambda: (self.service.discover(channel="beta",
            should_cancel=self.cancel.is_set), self.service.observe()))

    def review_installation(self):
        if self.recovery_only:
            return
        if self.offer is not None:
            self._start("Reviewing installation", lambda: self.service.review(self.offer))

    def install_reviewed(self):
        if self.recovery_only:
            return
        if self.offer is None or self.close_held or self.closing:
            return
        self.reviewed = None
        self.review_installation()

    def confirm_installation(self, reviewed):
        offer = reviewed.offer
        current = None if self.installed is None else self.installed.selected
        verb = "Update" if current is not None else "Install"
        box = QMessageBox(self); box.setWindowTitle(verb + " Tag Encyclopedia")
        box.setIcon(QMessageBox.Question); box.setTextFormat(Qt.PlainText)
        box.setText(f"{verb} reader {offer.release.software_version}?")
        before = "None installed" if current is None else current.version
        consequence = ("The reader must be closed before executable replacement." if current is not None else
            "The reader will not open automatically.")
        compatibility = ("\n\nCompatibility will be checked before installation. Confirm to download this exact package and install only if compatible."
            if getattr(self.service, "standalone_compatibility", False) else "")
        box.setInformativeText(f"Current reader: {before}\nPackage: {offer.release.artifact.size_bytes / (1024*1024):.1f} MB\n"
            f"Destination: {reviewed.destination}\n\nYour catalogs stay separate. {consequence}{compatibility}")
        box.setStandardButtons(QMessageBox.Yes | QMessageBox.No); box.setDefaultButton(QMessageBox.No)
        box.button(QMessageBox.Yes).setText(verb); box.button(QMessageBox.No).setText("Cancel")
        return box.exec() == QMessageBox.Yes

    def retry_connection(self):
        if self.recovery_only and not self.service.can_retry:
            return
        self._start("Saving installed connection", self.service.retry_connection)

    def recover_installation(self):
        if self.recovery_only and not self.service.can_recover:
            return
        if not self.recovery_confirm.isChecked():
            return
        self.recovery_confirm.setChecked(False)
        self._start("Recovering installation", lambda: self.service.recover(confirmed=True))

    def reconcile_launch(self):
        if not self.service.install_available or self.closing:
            return
        if self.close_held:
            self.reconcile_queued = True
            return
        self.reconcile_queued = False
        self._start("Checking app status", self.service.reconcile_launch)

    def request_cancel(self):
        self.reconcile_queued = False
        if self.close_held:
            self.cancel.set()
            self.status.setText("Cancellation requested. Keep this window open while the operation finishes.")
            self.stop.setEnabled(False)

    def poll(self):
        while True:
            try:
                values = self.progress.get_nowait()
            except Empty:
                break
            if not self.cancel.is_set():
                if len(values)>=2 and isinstance(values[0],int) and isinstance(values[1],int) and values[1]>0:
                    percent=max(0,min(100,round(values[0]*100/values[1])))
                    self.status.setText(f"Downloading reader: {percent}% of {values[1]/(1024*1024):.1f} MB. Installation has not finished.")
                else:
                    self.status.setText("Downloading reader. Installation has not finished.")
        if self.future is None:
            self.render()
            if self.reconcile_queued and not self.close_held:
                self.reconcile_launch()
            return
        if not self.future.done():
            return
        future, operation = self.future, self.operation
        confirmation = None
        try:
            result = future.result()
            if self.recovery_only:
                self.publication_pending=result.connection_pending
                self.status.setText(result.reason)
            elif operation == "Refreshing releases":
                offers, self.installed = result
                self.observation_stale = False
                self.offer = offers[0] if offers else None
                self.publication_pending = self.installed.connection_pending
                empty = self.empty_release_status_text()
                self.status.setText(empty if self.offer is None else
                    self.available_status_text(self.offer) +
                    (" Compatibility will be checked before installation." if getattr(self.service, "standalone_compatibility", False) else ""))
                if self.offer is None: self.release_notes.setPlainText("No available reader notes.")
                else: self.release_notes.setMarkdown(self.offer.release_notes)
            elif operation == "Reviewing installation":
                self.reviewed = result
                offer = result.offer
                if hasattr(result, 'installed'):
                    self.installed = result.installed; self.observation_stale = False
                folder = Path(result.destination)
                self.destination.setText("Installation folder: " + folder.parent.name + "/" + folder.name)
                self.destination.setToolTip(str(result.destination))
                self.details.setPlainText(f"Reader {offer.release.software_version}; {offer.release.artifact.size_bytes:,} bytes. Destination: {result.destination}\nTag: {offer.locator.tag if offer.locator is not None else offer.release.component_id.value + '/v' + offer.release.software_version}\nPublisher key: {offer.release.key_id}\nPackage SHA256: {offer.release.artifact.sha256}\n{offer.release_notes}")
                confirmation = result
            else:
                self.installed = result.installed
                self.observation_stale = False
                self.publication_pending = not result.connection_saved
                self.status.setText(self.installation_status_reason(result))
                self.show_installed_details()
                self.details.appendPlainText('\nStatus: ' + result.reason)
                self.offer = self.reviewed = None
                if result.connection_saved and self.connection_changed is not None:
                    self.connection_changed()
        except Exception as error:
            self.reviewed = None
            from core.encyclopedia_update_binding import StandaloneReaderCompatibilityHold
            if operation in ("Checking app status", "Refreshing releases"):
                self.observation_stale = self.installed is not None
            if operation == "Checking app status":
                self.status.setText("App status is unavailable. Close the reader if it is running, then Check app status.")
            elif operation == "Refreshing releases":
                self.status.setText("The release check could not finish. Your installation is unchanged. Choose Check Again to retry.")
            elif isinstance(error, StandaloneReaderCompatibilityHold):
                self.status.setText("Compatibility could not be confirmed. This package was not installed. Choose Check Again for a new review.")
                self.offer = None
            else:
                self.status.setText(f"{operation} could not finish. Choose Check Again for a fresh review, or recover a pending installation.")
                if operation in ('Reviewing installation', 'Installing reviewed package'):
                    self.offer = None
            self.details.setPlainText(f"Operation: {operation}\nDiagnostic: {error}")
        finally:
            self.future = self.operation = None
            self.render()
        if confirmation is not None and self.cancel.is_set():
            self.reviewed = None
            self.status.setText("Review cancelled. Your installation is unchanged.")
            self.render()
        elif confirmation is not None and not self.closing:
            self.confirming = True; self.render()
            confirmation_failed = False
            try:
                accepted = self.confirm_installation(confirmation)
            except Exception as error:
                confirmation_failed = True
                self.reviewed = None
                self.status.setText("The confirmation could not open. Choose the action again for a fresh review.")
                self.details.appendPlainText('\nConfirmation: ' + str(error))
                accepted = False
            finally:
                self.confirming = False
            if accepted and not self.closing and self.reviewed is confirmation:
                self.reviewed = None
                self._start("Installing reviewed package", lambda: self.service.install(confirmation,
                    confirmed=True, should_cancel=self.cancel.is_set,
                    progress=lambda *values: self.progress.put(values)))
            else:
                self.reviewed = None
                if not confirmation_failed:
                    self.status.setText("Installation cancelled. Your installation is unchanged.")
                self.render()

    def installation_status_reason(self, result):
        """Let the host present installation guidance in its own context."""
        installed = result.installed
        if not result.connection_saved:
            return "The reader files are retained. Choose Retry Save to save the connection; installation will not repeat."
        if installed.pending or installed.connection_pending:
            return "Setup needs attention. Check its status or choose an available recovery action."
        if installed.selected is not None and installed.phase == 'activated':
            return "Open the reader from Applications, then close it normally to finish setup. A portable Manager may need an ordinary restart first."
        return result.reason

    def pending_setup_guidance(self):
        return "Open the reader from Applications, then close it normally to finish setup. A portable Manager may need an ordinary restart first."

    def show_installed_details(self):
        if self.installed.selected is None:
            self.details.setPlainText("No managed reader is active. Retained package history remains available; user data was preserved.")
        else:
            self.details.setPlainText(f"Managed reader {self.installed.selected.version}\nDirectory: {self.installed.selected.directory}\nSetup: {'complete' if self.installed.phase == 'healthy' else 'waiting for a confirmed reader run'}. Launch remains explicit; portable Manager may require an ordinary restart for this saved selection.")

    def render(self):
        busy = self.close_held
        if self.recovery_only:
            ready=not busy and not self.closing
            self.health_accept.setEnabled(ready)
            self.retry_save.setEnabled(ready and self.service.can_retry)
            self.recover.setEnabled(ready and self.service.can_recover and self.recovery_confirm.isChecked())
            self.recovery_confirm.setEnabled(ready and self.service.can_recover)
            self.run_recovery_reader.setEnabled(ready and self.service.can_run_reader)
            return
        admitted = self.service.install_available and not self.closing
        selected = None if self.installed is None else self.installed.selected
        observation = self.observation_text()
        if self.observation_stale:
            observation = "Last observed (may be out of date): " + observation
        self.version_summary.setText(observation)
        if self.offer is not None:
            self.version_summary.setText(self.version_summary.text() + "\n" + self.available_version_label() + self.offer.release.software_version)
        pending = self.installed is not None and self.installed.pending
        launch_pending = self.installed is not None and self.installed.phase == 'activated'
        self.install.setText("Update" if selected is not None else "Install")
        self.install.setVisible(self.offer is not None and not busy and not self.publication_pending and not pending and not launch_pending)
        self.refresh.setEnabled(admitted and not busy)
        self.refresh.setVisible(not busy and not self.publication_pending and not pending)
        self.install.setEnabled(admitted and not busy and self.offer is not None and not launch_pending)
        _primary_action(self.install, self.install.isVisible())
        self.stop.setEnabled(busy and not self.cancel.is_set())
        self.stop.setVisible(busy and self.operation in ('Refreshing releases', 'Reviewing installation', 'Installing reviewed package'))
        self.retry_save.setText('Retry Save')
        self.retry_save.setVisible(self.publication_pending and not busy)
        _primary_action(self.retry_save, self.publication_pending and not busy)
        self.retry_save.setEnabled(admitted and not busy and self.publication_pending)
        self.health_accept.setVisible(selected is not None and not busy)
        self.health_accept.setEnabled(admitted and not busy)
        recovery_visible = not busy and (pending or selected is not None and self.technical.isChecked())
        self.recover.setVisible(recovery_visible)
        self.recover.setEnabled(admitted and not busy and self.recovery_confirm.isChecked())
        self.recovery_confirm.setVisible(recovery_visible)
        self.recovery_confirm.setEnabled(admitted and not busy)
        if launch_pending and self.offer is not None and not busy and not self.publication_pending:
            self.status.setText(self.pending_setup_guidance())

    def close_ready(self):
        self.closing = True
        self.reconcile_queued = False
        if self.close_held:
            self.request_cancel()
            self.poll()
        return not self.close_held

    def resume_after_cancelled_close(self):
        self.closing = False
        self.render()
