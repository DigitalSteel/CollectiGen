"""Applications acquisition decisions; the host retains the executor/root owner.

This panel never constructs installation authority. Production integration must
inject the qualified root-bound service; unavailable admission stays visible.
"""
from queue import SimpleQueue, Empty
from threading import Event

from PySide6.QtCore import QTimer, Qt
from PySide6.QtWidgets import QWidget, QLabel, QPushButton, QCheckBox, QHBoxLayout, QPlainTextEdit, QGroupBox, QVBoxLayout

from .ui_cards import compact_action, workspace_layout


class EncyclopediaAcquisitionPanel(QWidget):
    def __init__(self, service, executor, *, parent=None, connection_changed=None, recovery_only=False):
        super().__init__(parent)
        self.service, self.executor = service, executor
        self.connection_changed = connection_changed
        self.recovery_only = recovery_only
        self.future = self.operation = self.reviewed = self.offer = self.installed = None
        self.publication_pending = False
        self.reconcile_queued = False
        self.closing = False
        self.cancel = Event()
        self.progress = SimpleQueue()
        layout = workspace_layout(self)
        title = QLabel("Get or update Tag Encyclopedia")
        title.setObjectName("sectionHeading"); layout.addWidget(title)
        scope = QLabel("Refresh first-party releases, review the exact Windows x64 package, then confirm installation. Existing reader packages and user data stay separate. Launch remains an explicit action after installation.")
        scope.setWordWrap(True); layout.addWidget(scope)
        self.status = QLabel("Refresh releases when ready." if service.install_available else
            "Installation awaits the original Manager root's qualified transaction admission. Existing-package Connect and Launch remain available through their own controls.")
        self.status.setWordWrap(True); layout.addWidget(self.status)
        self.details = QPlainTextEdit("Release refresh contacts the first-party release service. No transfer occurs until confirmation. Updates require the retained reader to close and drain; portable Manager may need an ordinary restart before Launch.")
        self.details.setReadOnly(True); self.details.setMaximumHeight(220); layout.addWidget(self.details)
        self.confirm = QCheckBox("I reviewed this package and its destination; install it while the reader is closed.")
        layout.addWidget(self.confirm)
        row = QHBoxLayout()
        self.refresh = compact_action(QPushButton("Refresh releases"))
        self.review = compact_action(QPushButton("Review installation"))
        self.install = compact_action(QPushButton("Install reviewed package"))
        self.stop = compact_action(QPushButton("Cancel"))
        self.retry_save = compact_action(QPushButton("Retry save connection"))
        self.recover = compact_action(QPushButton("Recover installation"))
        self.health_accept = compact_action(QPushButton("Check app status"))
        self.run_recovery_reader = compact_action(QPushButton('Run reader to finish setup')) if recovery_only else None
        self.health_accept.setToolTip("Check the saved reader's setup. Run the reader and close its window normally; setup finishes automatically when that exact run is confirmed.")
        for button in (self.refresh, self.review, self.install, self.stop):
            row.addWidget(button)
        row.addStretch()
        layout.addLayout(row)
        recovery_row = QHBoxLayout()
        for button in (self.retry_save, self.recover, self.health_accept):
            recovery_row.addWidget(button)
        recovery_row.addStretch(); layout.addLayout(recovery_row)
        if recovery_only:
            recovery_row.insertWidget(3,self.run_recovery_reader)
            self.run_recovery_reader.clicked.connect(lambda:self._start(
                'Running the original reader; close its window normally to finish setup',self.service.run_reader))
        self.recovery_confirm = QCheckBox("Restore the retained previous executable and saved connection; preserve user data.")
        layout.addWidget(self.recovery_confirm); layout.addStretch()
        self.refresh.clicked.connect(self.refresh_releases)
        self.review.clicked.connect(self.review_installation)
        self.install.clicked.connect(self.install_reviewed)
        self.stop.clicked.connect(self.request_cancel)
        self.retry_save.clicked.connect(self.retry_connection)
        self.recover.clicked.connect(self.recover_installation)
        self.health_accept.clicked.connect(self.reconcile_launch)
        self.confirm.toggled.connect(self.render)
        self.recovery_confirm.toggled.connect(self.render)
        self.timer = QTimer(self); self.timer.setInterval(100)
        self.timer.timeout.connect(self.poll); self.timer.start()
        if recovery_only:
            title.setText('Tag Encyclopedia needs attention')
            scope.setText('Keep this window open while the original update is held. Check its status, then use an available recovery action. Your catalog is not changed by these controls.')
            self.status.setText('Check app status to review the held update. No operation will be repeated automatically.')
            self.status.setTextFormat(Qt.PlainText)
            self.details.setPlainText(service.failure_details)
            layout.removeWidget(self.details)
            disclosure=QGroupBox('Details'); disclosure.setCheckable(True); disclosure.setChecked(False)
            detail_layout=QVBoxLayout(disclosure); detail_layout.addWidget(self.details)
            self.details.hide(); disclosure.toggled.connect(self.details.setVisible)
            layout.insertWidget(3,disclosure)
            for widget in (self.refresh,self.review,self.install,self.confirm,self.stop):
                widget.hide()
            self.health_accept.setToolTip('Check the original held update and its recovery requirements. This does not launch a reader or declare it healthy.')
        self.render()

    @property
    def close_held(self):
        # Completed Future stays retained until its UI result is reconciled.
        return self.future is not None or bool(getattr(self.executor, "close_held", False))

    def _start(self, operation, work):
        if self.close_held or self.closing:
            return
        self.cancel = Event()
        self.operation = operation
        self.confirm.setChecked(False)
        self.status.setText(f"{operation}. Please wait; Cancel waits for the worker to settle.")
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
        if self.reviewed is None or not self.confirm.isChecked():
            return
        reviewed = self.reviewed
        self.reviewed = None
        self._start("Installing reviewed package", lambda: self.service.install(reviewed,
            confirmed=True, should_cancel=self.cancel.is_set,
            progress=lambda *values: self.progress.put(values)))

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
            self.status.setText("Cancellation requested. Waiting for transfer or transaction settlement; keep Manager open.")
            self.stop.setEnabled(False)

    def poll(self):
        while True:
            try:
                values = self.progress.get_nowait()
            except Empty:
                break
            if not self.cancel.is_set():
                self.status.setText("Downloading reviewed package: " + " / ".join(str(value) for value in values) + ". Installation has not been published.")
        if self.future is None:
            self.render()
            if self.reconcile_queued and not self.close_held:
                self.reconcile_launch()
            return
        if not self.future.done():
            return
        future, operation = self.future, self.operation
        try:
            result = future.result()
            if self.recovery_only:
                self.publication_pending=result.connection_pending
                self.status.setText(result.reason)
            elif operation == "Refreshing releases":
                offers, self.installed = result
                self.offer = offers[0] if offers else None
                self.publication_pending = self.installed.connection_pending
                self.status.setText("No newer signed Encyclopedia release is available." if self.offer is None else
                    f"Reader {self.offer.release.software_version} is available. Review installation before confirming.")
            elif operation == "Reviewing installation":
                self.reviewed = result
                offer = result.offer
                self.details.setPlainText(f"Reader {offer.release.software_version}; {offer.release.artifact.size_bytes:,} bytes. Destination: {result.destination}\nTag: {offer.locator.tag if offer.locator is not None else offer.release.component_id.value + '/v' + offer.release.software_version}\nPublisher key: {offer.release.key_id}\nPackage SHA256: {offer.release.artifact.sha256}\n{offer.release_notes}")
                self.status.setText("Review complete. Confirm this package and destination to install.")
            else:
                self.installed = result.installed
                self.publication_pending = not result.connection_saved
                self.status.setText(result.reason)
                self.show_installed_details()
                self.offer = self.reviewed = None
                if result.connection_saved and self.connection_changed is not None:
                    self.connection_changed()
        except Exception as error:
            self.reviewed = None
            if operation == "Checking app status":
                self.status.setText("App status is unavailable. Close the reader if it is running, then Check app status.")
            elif operation == "Refreshing releases":
                self.status.setText("Release refresh could not finish. Current installation is unchanged. Choose Refresh releases to retry; see the diagnostic below.")
            else:
                self.status.setText(f"{operation} could not finish. Review again for Retry, or Recover installation if a transaction is pending.")
            self.details.setPlainText(f"Operation: {operation}\nDiagnostic: {error}")
        finally:
            self.future = self.operation = None
            self.render()

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
        self.refresh.setEnabled(admitted and not busy)
        self.review.setEnabled(admitted and not busy and self.offer is not None)
        self.install.setEnabled(admitted and not busy and self.reviewed is not None and self.confirm.isChecked())
        self.confirm.setEnabled(not busy and self.reviewed is not None)
        self.stop.setEnabled(busy and not self.cancel.is_set())
        self.retry_save.setEnabled(admitted and not busy and self.publication_pending)
        self.health_accept.setEnabled(admitted and not busy)
        self.recover.setEnabled(admitted and not busy and self.recovery_confirm.isChecked())
        self.recovery_confirm.setEnabled(admitted and not busy)

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
