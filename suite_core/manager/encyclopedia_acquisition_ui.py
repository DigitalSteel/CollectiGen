"""Applications acquisition decisions; the host retains the executor/root owner.

This panel never constructs installation authority. Production integration must
inject the qualified root-bound service; unavailable admission stays visible.
"""
from queue import SimpleQueue, Empty
from threading import Event
from pathlib import Path
from time import monotonic

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QWidget, QLabel, QPushButton, QCheckBox, QHBoxLayout, QPlainTextEdit, QGroupBox, QVBoxLayout, QMessageBox, QSizePolicy, QToolButton, QTextBrowser, QProgressBar

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
    def __init__(self, service, executor, *, parent=None, connection_changed=None, recovery_only=False, return_to_applications=None):
        super().__init__(parent)
        self.service, self.executor = service, executor
        self.connection_changed = connection_changed
        self.return_to_applications = return_to_applications
        self.recovery_only = recovery_only
        self.future = self.operation = self.reviewed = self.offer = self.installed = None
        self.observation_stale = False
        self.publication_pending = False
        self.reconcile_queued = False
        self.closing = False
        self.cancel = Event()
        self.progress = SimpleQueue()
        self.confirming = False
        self.check_attempted = False
        self.started_at = None
        self.recovery_review = None
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
        self.status.setTextFormat(Qt.PlainText)
        self.activity = QProgressBar(self)
        self.activity.setObjectName('readerInstallProgress')
        self.activity.setAccessibleName('Reader setup progress')
        self.activity.setRange(0, 0); self.activity.hide()
        layout.addWidget(self.activity)
        self.elapsed = QLabel(self); self.elapsed.hide(); layout.addWidget(self.elapsed)
        self.version_summary = QLabel("No reader selected."); self.version_summary.setWordWrap(True)
        layout.addWidget(self.version_summary)
        self.destination = QLabel(""); self.destination.setWordWrap(True); layout.addWidget(self.destination)
        self.release_notes = _ReleaseNotes(self); self.release_notes.setPlainText("Available reader notes will appear here.")
        self.release_notes.setOpenLinks(False); self.release_notes.setOpenExternalLinks(False)
        self.release_notes.setMinimumHeight(100); self.release_notes.setMaximumHeight(180)
        notes = self.notes_container = disclosure_panel("What's New", self.release_notes); layout.addWidget(notes)
        self.notes_toggle = notes.findChild(QToolButton)
        self.details = QPlainTextEdit("Release checks contact the first-party publisher. Installation requires your confirmation.",self)
        self.details.setReadOnly(True); self.details.setMaximumHeight(160)
        technical = self.details_container = disclosure_panel("Details", self.details); layout.addWidget(technical)
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
        self.return_action = compact_action(QPushButton('Return to Applications'))
        self.return_action.clicked.connect(lambda: self.return_to_applications() if self.return_to_applications is not None else None)
        self.run_recovery_reader = compact_action(QPushButton('Run reader to finish setup')) if recovery_only else None
        self.health_accept.setToolTip("Check the saved reader's setup. Run the reader and close its window normally; setup finishes automatically when that exact run is confirmed.")
        for button in (self.install, self.refresh, self.stop):
            row.addWidget(button)
        for button in (self.retry_save, self.recover, self.health_accept):
            row.addWidget(button)
        row.addWidget(self.return_action)
        row.addStretch(); layout.insertLayout(5, row)
        QWidget.setTabOrder(self.refresh, self.install)
        QWidget.setTabOrder(self.install, self.stop)
        QWidget.setTabOrder(self.stop, self.return_action)
        QWidget.setTabOrder(self.return_action, self.technical)
        if recovery_only:
            row.insertWidget(3,self.run_recovery_reader)
            self.run_recovery_reader.clicked.connect(lambda:self._start(
                'Running the original reader; close its window normally to finish setup',self.service.run_reader))
        self.recovery_confirm = QCheckBox("Restore the previous app state and connection. Keep catalogs and images.")
        self.recovery_confirm.setSizePolicy(QSizePolicy.Maximum, QSizePolicy.Fixed)
        self.recovery_summary = QLabel(self); self.recovery_summary.setWordWrap(True)
        self.recovery_summary.setTextFormat(Qt.PlainText)
        recovery_body = QWidget(self); recovery_layout = QVBoxLayout(recovery_body)
        recovery_layout.setContentsMargins(0, 0, 0, 0)
        recovery_layout.addWidget(self.recovery_summary)
        recovery_layout.addWidget(self.recovery_confirm)
        if not recovery_only:
            row.removeWidget(self.recover)
            recovery_layout.addWidget(self.recover, 0, Qt.AlignLeft)
        self.recovery_container = disclosure_panel("Recovery", recovery_body)
        self.recovery_toggle = self.recovery_container.findChild(QToolButton)
        layout.insertWidget(layout.indexOf(technical), self.recovery_container)
        self.refresh.clicked.connect(self.refresh_releases)
        self.install.clicked.connect(self.install_reviewed)
        self.stop.clicked.connect(self.request_cancel)
        self.retry_save.clicked.connect(self.retry_connection)
        self.recover.clicked.connect(self.recover_installation)
        self.health_accept.clicked.connect(self.reconcile_launch)
        self.recovery_confirm.toggled.connect(self._confirm_recovery)
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
        return "The Tag Encyclopedia reader lets you browse your tags and categories. Install the reader app here; your catalogs and images stay separate. You choose when to open it."

    def initial_status_text(self):
        return "Choose Find reader to check for the app. App files are downloaded only after you confirm Install."

    def observation_text(self):
        selected = None if self.installed is None else self.installed.selected
        return ("Reader installation has not been checked." if self.installed is None else
            "No reader installed." if selected is None else
            "Installed reader: " + selected.version + (" — setup needs a normal reader close." if self.installed.phase == 'activated' else ""))

    def available_version_label(self):
        return "Available reader: "

    def package_review_name(self):
        return "Reader"

    def recovery_content_name(self):
        return 'catalogs and images'

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
        self.started_at = monotonic()
        self.status.setText(f"{operation}. Please wait.")
        self.activity.setRange(0, 0)
        self.activity.setVisible(True)
        try:
            self.future = self.executor.submit(work)
        except Exception as error:
            self.operation = None
            self.status.setText("Could not start. Choose Check Again to retry; your installation is unchanged.")
            self.details.setPlainText(str(error))
        self.render()

    def refresh_releases(self):
        if self.recovery_only:
            return
        if self.close_held or self.closing:
            return
        self.check_attempted = True
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
        box.setInformativeText(f"Current reader: {before}\nDownload: {offer.release.artifact.size_bytes / (1024*1024):.1f} MB\n"
            f"\nYour catalogs and images will not be changed. {consequence}{compatibility}")
        box.setDetailedText(f"Installation folder: {reviewed.destination}\nPackage SHA256: {offer.release.artifact.sha256}")
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
        reviewed = self.recovery_review
        self.recovery_confirm.setChecked(False)
        if self.recovery_only:
            self._start("Recovering installation", lambda: self.service.recover(confirmed=True))
        elif reviewed is not None and reviewed == self.installed and reviewed.recovery_available:
            self._start("Recovering installation", lambda: self.service.recover(
                confirmed=True, expected_installed=reviewed))

    def _confirm_recovery(self, checked):
        self.recovery_review = self.installed if checked else None
        self.render()

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
            self.activity.setRange(0, 0)
            self.status.setText("Cancellation requested. Keep this window open while the operation finishes.")
            self.stop.setToolTip("Downloading or preparing files can stop. If publication has started, setup settles safely before closing.")
            self.stop.setEnabled(False)

    def poll(self):
        if self.future is not None and self.started_at is not None:
            self.elapsed.setText(f"Elapsed: {int(monotonic() - self.started_at)} seconds")
        while True:
            try:
                values = self.progress.get_nowait()
            except Empty:
                break
            if not self.cancel.is_set():
                if len(values)==2 and values[0]=='phase':
                    self.activity.setRange(0,0)
                    self.status.setText(values[1] + '. Please wait.')
                elif len(values)>=2 and type(values[0]) is int and type(values[1]) is int and 0<=values[0]<=values[1] and values[1]>0:
                    percent=max(0,min(100,round(values[0]*100/values[1])))
                    self.activity.setRange(0,100); self.activity.setValue(percent)
                    self.status.setText(f"Downloading reader: {percent}% of {values[1]/(1024*1024):.1f} MB. Installation has not finished.")
                else:
                    self.activity.setRange(0,0)
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
        inspect_after_failure = False
        try:
            result = future.result()
            if self.recovery_only:
                self.publication_pending=result.connection_pending
                self.status.setText(result.reason)
            elif operation == 'Checking recovery options':
                self.installed = result
                self.observation_stale = False
                self.publication_pending = result.connection_pending
                self.status.setText('Setup was interrupted. Review the available recovery action below.' if result.pending else
                    'The reader files are retained. Choose Retry Save to finish connecting the app.' if result.connection_pending else
                    self.pending_setup_guidance() if result.phase == 'activated' else
                    'The operation did not finish. Choose Check Again for a fresh review; your previous installation is retained.')
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
                self.destination.setText('')
                self.destination.setToolTip(str(result.destination))
                self.details.setPlainText(f"{self.package_review_name()} {offer.release.software_version}; {offer.release.artifact.size_bytes:,} bytes. Destination: {result.destination}\nTag: {offer.locator.tag if offer.locator is not None else offer.release.component_id.value + '/v' + offer.release.software_version}\nPublisher key: {offer.release.key_id}\nPackage SHA256: {offer.release.artifact.sha256}\n{offer.release_notes}")
                confirmation = result
            else:
                self.installed = result.installed
                self.observation_stale = False
                self.publication_pending = not result.connection_saved
                self.status.setText(self.installation_status_reason(result))
                self.show_installed_details()
                self.details.appendPlainText('\nStatus: ' + self.installation_status_reason(result))
                if not result.connection_saved and result.reason != self.installation_status_reason(result):
                    self.details.appendPlainText('\nConnection details: ' + result.reason)
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
                if operation == 'Installing reviewed package' and not self.recovery_only:
                    inspect_after_failure = True
            self.details.setPlainText(f"Operation: {operation}\nDiagnostic: {error}")
        finally:
            self.future = self.operation = None
            self.render()
        if inspect_after_failure and not self.closing:
            self._start('Checking recovery options', self.service.observe)
            return
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
                phase_options = ({'phase_progress': lambda value: self.progress.put(('phase', value))}
                    if getattr(self.service, 'phase_progress_supported', False) else {})
                self._start("Installing reviewed package", lambda: self.service.install(confirmation,
                    confirmed=True, should_cancel=self.cancel.is_set,
                    progress=lambda *values: self.progress.put(values), **phase_options))
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
        self.activity.setVisible(self.future is not None)
        self.elapsed.setVisible(self.future is not None)
        self.destination.hide()
        self.version_summary.setVisible(self.installed is not None or self.offer is not None)
        self.notes_container.setVisible(self.offer is not None)
        self.details_container.setVisible(self.check_attempted or self.installed is not None or self.recovery_only)
        self.return_action.setVisible(not self.recovery_only and self.return_to_applications is not None
            and self.installed is not None and self.installed.selected is not None and not busy and not self.publication_pending)
        self.return_action.setEnabled(not busy and not self.closing)
        _primary_action(self.return_action, self.return_action.isVisible())
        if self.recovery_only:
            self.recovery_container.show()
            self.recovery_toggle.setChecked(True)
            self.recovery_summary.setText('Review the original held update before confirming recovery. Catalogs and images are preserved.')
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
        self.refresh.setText('Check Again' if self.check_attempted or self.installed is not None else 'Find reader')
        self.refresh.setVisible(not busy and not self.publication_pending and not pending)
        self.install.setEnabled(admitted and not busy and self.offer is not None and not launch_pending)
        _primary_action(self.install, self.install.isVisible())
        _primary_action(self.refresh, selected is None and not self.refresh.isHidden() and self.install.isHidden() and self.return_action.isHidden())
        self.stop.setEnabled(busy and not self.cancel.is_set())
        self.stop.setVisible(busy and self.operation in ('Refreshing releases', 'Reviewing installation', 'Installing reviewed package'))
        self.retry_save.setText('Retry Save')
        self.retry_save.setVisible(self.publication_pending and not busy)
        _primary_action(self.retry_save, self.publication_pending and not busy)
        self.retry_save.setEnabled(admitted and not busy and self.publication_pending)
        self.health_accept.setVisible(selected is not None and not busy and (launch_pending or self.technical.isChecked()))
        self.health_accept.setEnabled(admitted and not busy)
        recovery_visible = self.installed is not None and getattr(self.installed, 'recovery_available', False)
        self.recovery_container.setVisible(recovery_visible)
        if self.recovery_review is not None and self.recovery_review != self.installed:
            self.recovery_confirm.setChecked(False)
        version = None if self.installed is None else getattr(self.installed, 'recovery_version', None)
        name = self.package_review_name()
        content = self.recovery_content_name()
        self.recover.setText(f'Restore {name} {version}' if version is not None else 'Undo installation')
        self.recovery_summary.setText(
            f'Restore {name} {version} and its saved connection. Keep your {content}.' if version is not None else
            f'Undo this installation. No {name} will be selected; your {content} will be kept.')
        self.recovery_confirm.setText(f'I confirm restoring {name} {version}.' if version is not None else 'I confirm undoing this installation.')
        self.recover.setEnabled(admitted and not busy and self.recovery_confirm.isChecked() and self.recovery_review == self.installed)
        self.recovery_confirm.setEnabled(admitted and not busy)
        if pending and not busy:
            self.recovery_toggle.setChecked(True)
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
