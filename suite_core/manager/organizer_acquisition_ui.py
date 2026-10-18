"""Organizer presentation over the existing serial acquisition/progress panel."""
from PySide6.QtWidgets import QLabel, QPushButton, QMessageBox, QSizePolicy
from PySide6.QtCore import Qt
from threading import Event
from time import monotonic
from .encyclopedia_acquisition_ui import EncyclopediaAcquisitionPanel, _primary_action
from .ui_cards import compact_action


class OrganizerAcquisitionPanel(EncyclopediaAcquisitionPanel):
    def __init__(self, service, executor, **kwargs):
        self.open_action=None
        super().__init__(service,executor,**kwargs)
        layout=self.layout()
        layout.setAlignment(Qt.AlignTop)
        self.notes_container.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Maximum)
        self.details_container.setSizePolicy(QSizePolicy.Preferred,QSizePolicy.Maximum)
        self.recovery_confirm.setText('Restore the previous app state and connection. Keep libraries and images.')
        for index in range(layout.count()-1,-1,-1):
            if layout.itemAt(index).spacerItem() is not None:layout.takeAt(index)
        for label in self.findChildren(QLabel):
            if label.text()=='Tag Encyclopedia':label.setText('Media Organizer')
        self.activity.setObjectName('organizerInstallProgress')
        self.activity.setAccessibleName('Organizer setup progress')
        self.open_action=compact_action(QPushButton('Open Organizer'))
        self.footer.insertWidget(0,self.open_action)
        self.open_action.clicked.connect(self.open_organizer)
        self.stop_retained=compact_action(QPushButton('Stop retained Organizer'))
        self.footer.insertWidget(1,self.stop_retained)
        self.stop_retained.clicked.connect(self.stop_owned_organizer)
        self.startup_recovery=compact_action(QPushButton('Check startup recovery'))
        self.footer.insertWidget(1,self.startup_recovery)
        self.startup_recovery.clicked.connect(self.reconcile_startup)
        self.health_accept.setToolTip('Open Organizer, then close its window normally. Setup finishes after the original process and its output worker have closed.')
        self.render()

    def scope_text(self):
        return ('Media Organizer helps you organize media and preserve its metadata. '
                'Install the app here, then open it when you are ready. Installation keeps your libraries and images separate.')

    def initial_status_text(self):
        return 'Choose Find Organizer to check for the app. App files are downloaded only after you confirm Install.'

    def observation_text(self):
        selected=None if self.installed is None else self.installed.selected
        return ('Organizer installation has not been checked.' if self.installed is None else
                'No Organizer installed.' if selected is None else
                'Installed Organizer: '+selected.version+(' — open and close it normally to finish setup.' if self.installed.phase=='activated' else ''))

    def available_version_label(self):return 'Available Organizer: '

    def package_review_name(self):return 'Organizer'

    def available_status_text(self,offer):
        return f'Organizer {offer.release.software_version} is available. Choose Install or Update to review it.'

    def empty_release_status_text(self):
        return ('No installable Organizer was found. Check Again later.' if self.installed.selected is None else
                'No newer Organizer was found.')

    def pending_setup_guidance(self):
        return ('Choose Open Organizer, then close its window normally to finish setup.' if self.service.launch_available else
                'Organizer is installed. Restart Manager to make this saved app available to open.')

    def installation_status_reason(self,result):
        if not result.connection_saved:return 'The Organizer files are retained. Choose Retry Save to finish connecting the app; installation will not repeat.'
        if result.installed.pending or result.installed.connection_pending:return 'Setup needs attention. Review the available recovery action.'
        if result.installed.phase in ('rolled_back','abandoned'):
            if result.installed.selected is None:
                return 'Recovery is complete. No Organizer is active. Choose Check Again to review an installation. Your libraries and images were preserved.'
            return ('The previous Organizer version is restored. Choose Open Organizer when ready.' if self.service.launch_available else
                    'The previous Organizer version is restored. Restart Manager to open it. Your libraries and images were preserved.')
        if result.installed.selected is not None and result.installed.phase=='activated':return self.pending_setup_guidance()
        return self._organizer_text(result.reason)

    def show_installed_details(self):
        selected=self.installed.selected
        self.details.setPlainText('No managed Organizer is active. User data was preserved.' if selected is None else
            f'Managed Organizer {selected.version}\nDirectory: {selected.directory}\nSetup: {self.installed.phase}. Opening remains an explicit action.')

    @staticmethod
    def _organizer_text(text):
        # Only stock panel chrome is presented here. Publisher notes and error
        # evidence remain verbatim in their separate disclosure controls.
        return text.replace('Tag Encyclopedia','Media Organizer').replace('Reader','Organizer').replace('reader','Organizer')

    def render(self):
        super().render()
        self.status.setText(self._organizer_text(self.status.text()))
        self.refresh.setText('Check Again' if self.check_attempted or self.installed is not None else 'Find Organizer')
        if self.open_action is None:return
        selected=None if self.installed is None else self.installed.selected
        ready=(selected is not None and not self.close_held and not self.closing
               and not self.publication_pending and not self.installed.pending)
        self.open_action.setVisible(ready)
        self.open_action.setEnabled(ready and self.service.launch_available)
        self.open_action.setToolTip('Open your installed Organizer.' if self.service.launch_available else
                                   'Opening awaits the original root-owned native launch admission.')
        _primary_action(self.open_action,ready and self.service.launch_available)
        if ready and self.service.launch_available:_primary_action(self.return_action,False)
        owner=self.service.native_owner
        retained=owner is not None and owner.close_held
        failed=retained and (owner.child is None or owner.identity is None)
        self.stop_retained.setVisible(retained and (failed or self.technical.isChecked()))
        self.stop_retained.setEnabled(retained and self.future is None)
        recovery=owner is not None and owner.startup_recovery_available
        self.startup_recovery.setVisible(recovery)
        self.startup_recovery.setEnabled(recovery and self.future is None and not self.executor.close_held)
        if recovery:self.stop_retained.hide()
        if retained and self.future is None:
            self.status.setText('Organizer did not finish starting. Choose Check startup recovery before retrying.' if recovery else
                                'Organizer startup needs recovery. Choose Stop retained Organizer and wait for cleanup.' if failed else
                                'Organizer is running. Close its window normally when you finish; Manager keeps your setup safe until it closes.')

    def poll(self):
        owner=self.service.native_owner
        if owner is not None and self.future is None:
            retained=owner.close_held
            try:
                owner.poll()
                if retained and not owner.close_held and not self.closing:
                    self.reconcile_launch()
            except Exception as error:
                self.status.setText('Organizer status could not be checked. Keep Manager open and review Details for recovery.')
                self.details.setPlainText(str(error))
        super().poll()
        self.status.setText(self._organizer_text(self.status.text()))

    @property
    def close_held(self):
        return super().close_held or (self.service.native_owner is not None and self.service.native_owner.close_held)

    def close_ready(self):
        owner=self.service.native_owner
        if owner is not None:owner.begin_close()
        return super().close_ready()

    def confirm_installation(self,reviewed):
        current=None if self.installed is None else self.installed.selected
        verb='Install' if current is None else 'Update'
        box=QMessageBox(self);box.setWindowTitle(verb+' Media Organizer')
        box.setIcon(QMessageBox.Question);box.setTextFormat(Qt.PlainText)
        box.setText(f'{verb} Organizer {reviewed.offer.release.software_version}?')
        box.setInformativeText(f"Current app: {'None installed' if current is None else current.version}\n"
            f'Download: {reviewed.offer.release.artifact.size_bytes/(1024*1024):.1f} MB\n\n'
            'Your libraries and images will not be changed. Organizer will not open automatically.')
        box.setDetailedText(f'Installation folder: {reviewed.destination}\nPackage SHA256: {reviewed.offer.release.artifact.sha256}')
        box.setStandardButtons(QMessageBox.Yes|QMessageBox.No);box.setDefaultButton(QMessageBox.No)
        box.button(QMessageBox.Yes).setText(verb);box.button(QMessageBox.No).setText('Cancel')
        return box.exec()==QMessageBox.Yes

    def open_organizer(self):
        if self.close_held or self.closing or not self.service.launch_available:return
        # Native creation belongs to the original owner. It must retain the Job,
        # birth witness and drain independently of this display operation.
        snapshot=self.installed
        def work():
            from core.encyclopedia_installation import InstallationResult
            self.service.launch(confirmed=True)
            return InstallationResult(snapshot,True,
                'Organizer is opening. Close its window normally when you finish.')
        self._start('Opening Organizer',work)

    def stop_owned_organizer(self):
        owner=self.service.native_owner
        if owner is None or not owner.close_held or self.future is not None:return
        box=QMessageBox(self);box.setWindowTitle('Stop retained Organizer')
        box.setIcon(QMessageBox.Warning);box.setText('Stop the Organizer process retained by this Manager?')
        box.setInformativeText('Unsaved Organizer changes may be lost. This will not count as a normal Close or finish app setup. Manager will wait for verified cleanup.')
        box.setStandardButtons(QMessageBox.Yes|QMessageBox.No);box.setDefaultButton(QMessageBox.No)
        box.button(QMessageBox.Yes).setText('Stop Organizer');box.button(QMessageBox.No).setText('Keep running')
        if box.exec()==QMessageBox.Yes:
            try:owner.stop_failed_start();self.poll()
            except Exception as error:
                self.status.setText('Organizer cleanup is still held. Keep Manager open and review Details.')
                self.details.setPlainText(str(error))

    def reconcile_startup(self):
        owner=self.service.native_owner
        if (owner is None or not owner.startup_recovery_available or self.future is not None
                or self.executor.close_held or self.closing):return
        identity=owner.native.helper_accounting_identity;snapshot=self.installed
        self.cancel=Event();self.operation='Checking startup recovery';self.started_at=monotonic()
        def work():
            from core.encyclopedia_installation import InstallationResult
            owner.reconcile_startup(identity,cancelled=self.cancel.is_set)
            return InstallationResult(snapshot,True,'Original startup recovery completed. Choose Open Organizer to try again.')
        try:self.future=self.executor.submit(work)
        except Exception as error:
            self.operation=None;self.details.setPlainText(str(error))
            self.status.setText('Startup recovery could not start. Keep Manager open and try Check startup recovery again.')
        self.render()
