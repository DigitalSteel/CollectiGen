"""Standalone shell composition; the canonical Tag pane retains write decisions."""
from pathlib import Path
from dataclasses import dataclass,replace
from threading import Event

from PySide6.QtCore import QObject, QTimer,QThreadPool,Signal
from PySide6.QtWidgets import QDialog, QFileDialog, QLabel, QVBoxLayout, QPushButton, QComboBox, QCheckBox, QHBoxLayout, QGroupBox

from core.encyclopedia_catalog import EncyclopediaCatalog
from suite_core.manager.resource_executor import ResourceExecutorOwner
from suite_core.manager.tag_database_controller import TagDatabaseController
from suite_core.manager.tag_database_ui import TagDatabasePane
from suite_core.encyclopedia.publication import Publication, read


@dataclass(frozen=True)
class AcceptedCatalogSource:
    kind: str
    path: Path | None
    directory_pin: tuple | None = None
    native_adapter: object | None = None
    publication: object | None = None

    def __post_init__(self):
        if self.kind not in {"native", "export", "memory"}:
            raise ValueError("Known accepted reader source required")
        if self.kind == "memory":
            if self.path is not None or self.directory_pin is not None:
                raise ValueError("An in-memory view has no filesystem refresh source")
        elif not isinstance(self.path, Path) or not self.path.is_absolute():
            raise ValueError("Exact absolute accepted source required")
        if self.directory_pin is not None and (type(self.directory_pin) is not tuple
                or len(self.directory_pin) != 2 or any(type(v) is not int for v in self.directory_pin)):
            raise ValueError("Original native directory identity required")
        if self.native_adapter is not None:
            from suite_core.knowledge.native_tag_catalog_host import NativeTagCatalogAdapter
            if (self.kind!='native' or type(self.native_adapter) is not NativeTagCatalogAdapter
                    or type(self.publication) is not Publication or self.native_adapter.root!=self.path
                    or self.native_adapter.directory_pin!=self.directory_pin
                    or self.native_adapter.catalog_id!=self.publication.catalog_id
                    or self.publication.snapshot.source.kind!='effective_native'
                    or self.publication.snapshot.source.completeness!='complete'):
                raise ValueError('Exact original adapter and complete native publication binding required')
        elif self.publication is not None:
            raise ValueError('Publication binding requires its original native adapter')


class EncyclopediaCatalogHost(QObject):
    """One original retained executor for reads and the existing Tag controller.

    No new native writer/transaction or background replay. Explicit actions only.
    The window queries close_ready before its existing hosted Close handling.
    """
    catalog_accepted=Signal(object)
    def __init__(self, window, *, settings_path, executor=None):
        super().__init__(window)
        self.window = window
        self.executor = executor if executor is not None else ResourceExecutorOwner()
        if type(self.executor) is not ResourceExecutorOwner:
            raise TypeError("Original retained resource executor required")
        self.service = EncyclopediaCatalog(Path(settings_path))
        self.baseline_generation = 0
        self.controller = TagDatabaseController(executor=self.executor,
            settings_path=settings_path, apply_authorized=True, owns_executor=False,
            before_baseline_change=self.invalidate_default_baseline)
        self.future = None
        self.dialog = self.pane = None
        self._shown_result = None
        self._cancelled = False
        self._closing = False
        self.help = None
        self.entry_check = None
        self.entry_future = None
        self.entry_close_started = False
        self.entry_close_fault = False
        self.entry_action_future = None
        self.entry_action_work = self.entry_action_operation = self.entry_action_body = None
        self.entry_notice = self.entry_status = self.entry_request = None
        self.entry_action_error = ''
        self.entry_last_action = None
        self.entry_recovery_confirmed = False
        self.default_acquisition = None
        self.package_future = None
        self.package_cancel = Event()
        self.package_component = self.preparation_directory = None
        self._download_result = self._download_generation = None
        self._package_generation = None
        self.prepared_default_review = None
        self.content_offers = self.content_refresh = self.content_download = self.content_confirm = self.content_review = None
        self._content_discovery = None
        self._content_error = ''
        self.content_box = None
        from core import EventBus
        from core.background_jobs import BackgroundJobManager
        self.events=EventBus();self.jobs=BackgroundJobManager(limits={'io':1,'cpu':1})
        self.knowledge=None;self.settings=None
        self._operation_jobs={}
        self.editor_pool=QThreadPool(self);self.editor_pool.setMaxThreadCount(1)
        self.editing_bridge=self.editor_session=self.category_dialog=None
        self.editor_future=self._editor_job=None
        self._editor_refresh_needed=False
        self.organizer_receiver=self.organizer_adapter=None
        self.handoff_future=None
        self._handoff_generation=None
        self._handoff_cancelled=False
        self.accepted_source = None
        self._accepted_native_adapter=None
        self._before_view = None
        self._opening_source = None
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

    @property
    def busy(self):
        return bool(self.future is not None or self.controller.close_held or
                    self.executor.close_held or self.help is not None and self.help.close_held
                    or self.entry_future is not None or self.entry_close_fault
                    or self.entry_action_future is not None
                    or self.entry_check is not None and self.entry_check.close_held
                    or self.default_acquisition is not None and self.default_acquisition.close_held
                    or self.package_future is not None or self.editor_future is not None
                    or self.handoff_future is not None
                    or self.editor_pool.activeThreadCount()>0 or bool(self._operation_jobs)
                    or self.editing_bridge is not None and self.editing_bridge.busy)

    def begin_long_operation(self,owner,label):
        if self.future is not None or self.package_future is not None or self.editor_future is not None or self.executor.close_held or self.controller.close_held:
            return False,'the current standalone operation'
        job,started=self.jobs.try_acquire(label,resource='io',key='visible:'+owner,exclusive=True)
        if not started:return False,job.name
        self._operation_jobs[owner]=job.id
        self.events.publish('long_operation_changed',active=True,owner=owner,label=label)
        return True,''

    def finish_long_operation(self,owner):
        job_id=self._operation_jobs.pop(owner,None)
        if job_id is not None:self.jobs.release(job_id)
        self.events.publish('long_operation_changed',active=False,owner=owner)

    def editor_commands(self):
        from qt_app.command_bar import UiCommand
        if self.editing_bridge is None:
            return [UiCommand('encyclopedia:prepare-editing','Edit Tags and Categories','Home',
                callback=self.prepare_editing,enabled=lambda:not self.busy and self.accepted_source is not None and self.accepted_source.kind=='native')]
        commands=[]
        for command in self.editing_bridge.commands:
            if command.key in ('encyclopedia:new-category','encyclopedia:edit-category'):
                original=command.callback
                def category_action(callback=original):
                    self.category_dialog.show();self.category_dialog.raise_()
                    return callback()
                command=replace(command,callback=category_action)
            commands.append(command)
        return commands

    def entry_actions_available(self):
        from core.encyclopedia_entry_adapter import EncyclopediaEntryUpdateCheck
        return type(self.entry_check) is EncyclopediaEntryUpdateCheck and all(
            callable(getattr(self.entry_check,name,None)) for name in
            ('confirm_update','status','cancel_update','retry_connection','recover_update'))

    def entry_commands(self):
        if not self.entry_actions_available():return []
        from suite_core.manager.command_bar import UiCommand
        idle=lambda:not self._closing and not self.busy
        request_ready=lambda:idle() and self.entry_request is not None and self.entry_request is self.entry_check.pending_request
        phase=self._entry_phase
        commands = [
            UiCommand('encyclopedia:review-update','Review Available Update','Help',callback=self.review_entry_update,
                enabled=lambda:idle() and self.entry_notice is not None and self.entry_notice is self.entry_check.retained_notice
                and self.entry_notice.status=='available' and self.entry_notice.route=='standalone'
                and self.entry_check.pending_request is None),
            UiCommand('encyclopedia:update-status','Check Update / Recovery Status','Help',callback=self.check_entry_status,enabled=idle),
            UiCommand('encyclopedia:cancel-update','Cancel Pending Update','Help',callback=self.cancel_entry_update,
                enabled=lambda:request_ready() and phase()=='waiting_for_close'),
            UiCommand('encyclopedia:retry-update-save','Retry Saving Reader Connection','Help',callback=self.retry_entry_connection,
                enabled=lambda:request_ready() and phase() in ('applying','recovering')),
            UiCommand('encyclopedia:recover-update','Recover Interrupted Update','Help',callback=self.recover_entry_update,
                enabled=lambda:request_ready() and phase()=='applying' and not self.entry_recovery_confirmed),
            UiCommand('encyclopedia:resume-update','Resume Pending Update Request','Help',callback=self.resume_entry_action,
                enabled=self.entry_resume_ready)]
        visible={'encyclopedia:update-status'}
        if (self.entry_notice is not None and self.entry_notice.status=='available'
                and self.entry_notice.route=='standalone' and self.entry_check.pending_request is None):
            visible.add('encyclopedia:review-update')
        if self.entry_request is not None:
            if phase()=='waiting_for_close':visible.add('encyclopedia:cancel-update')
            if phase() in ('applying','recovering'):visible.add('encyclopedia:retry-update-save')
            if phase()=='applying' and not self.entry_recovery_confirmed:visible.add('encyclopedia:recover-update')
        if self.entry_action_error and self.entry_action_work is not None:visible.add('encyclopedia:resume-update')
        return [command for command in commands if command.key in visible]

    def _start_entry_action(self,operation,body,work):
        if not self.entry_actions_available() or self.busy or self._closing:return False
        self.entry_action_operation,self.entry_action_body,self.entry_action_work=operation,body,work
        self.entry_action_error=''
        self.entry_action_future=self.executor.submit(work)
        self.window.status.setText('Waiting for the original '+operation+' reply. Keep this window open.')
        return True

    def _entry_phase(self):
        return getattr(self.entry_status,'phase',None) if self.entry_status is not None else (
            'waiting_for_close' if self.entry_last_action=='waiting_for_close' else None)

    def entry_resume_ready(self):
        return bool(self.entry_actions_available() and self.entry_action_error and self.entry_action_work is not None
            and self.entry_action_future is None and self.entry_future is None and not self.executor.close_held
            and self.future is None and self.package_future is None and self.editor_future is None
            and self.handoff_future is None and not self.controller.close_held
            and (self.help is None or self.help.future is None)
            and (self.default_acquisition is None or not self.default_acquisition.close_held)
            and not self.executor.closed and self.entry_check.can_resume(self.entry_action_operation,self.entry_action_body))

    def resume_entry_action(self):
        if not self.entry_resume_ready():return False
        self.entry_action_error=''
        self.entry_action_future=self.executor.submit(self.entry_action_work)
        self.window.status.setText('Reading the retained original reply; no replacement request is created.')
        return True

    def accept_entry_notice(self,notice):
        from core.encyclopedia_entry_adapter import EncyclopediaEntryNotice
        self.entry_notice=notice if type(notice) is EncyclopediaEntryNotice else None
        if self.entry_notice is not None and notice.status=='available' and notice.route=='standalone':
            self.window.status.setText('Tag Encyclopedia '+notice.version+' is available. Choose Review Available Update; installation waits for confirmed normal Close.')
        self.window._set_commands()

    def review_entry_update(self):
        if not self.entry_actions_available() or self.busy or self._closing:return False
        notice=self.entry_notice
        if (notice is None or notice is not self.entry_check.retained_notice or notice.status!='available'
                or notice.route!='standalone' or self.entry_check.pending_request is not None):return False
        from PySide6.QtWidgets import QMessageBox
        answer=QMessageBox.question(self.window,'Review Tag Encyclopedia update',
            'Review reader '+notice.version+'\n\n'+notice.notes+'\n\nPackage SHA256: '+notice.package_sha256+
            '\nManifest SHA256: '+notice.manifest_sha256+'\n\nConfirm this exact update? The original root applies it only after you close this reader normally.',
            QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if answer!=QMessageBox.Yes:return False
        if (notice is not self.entry_check.retained_notice or self.busy or self._closing
                or self.entry_check.pending_request is not None):return False
        return self._start_entry_action('confirm',{'manifest_sha256':notice.manifest_sha256},
            lambda:self.entry_check.confirm_update(notice,confirmed=True))

    def check_entry_status(self):
        return self._start_entry_action('status',{},self.entry_check.status) if self.entry_actions_available() else False

    def _retained_entry_action(self,operation,*,confirmed=False):
        request=self.entry_request
        if not self.entry_actions_available() or request is None or request is not self.entry_check.pending_request:return False
        phase=self._entry_phase()
        if (operation=='cancel' and phase!='waiting_for_close' or operation=='recover' and phase!='applying'
                or operation=='retry-connection' and phase not in ('applying','recovering')):return False
        if operation=='recover':work=lambda:self.entry_check.recover_update(request,confirmed=confirmed)
        elif operation=='cancel':work=lambda:self.entry_check.cancel_update(request)
        else:work=lambda:self.entry_check.retry_connection(request)
        return self._start_entry_action(operation,{'request_sha256':request.request_sha256},work)

    def cancel_entry_update(self):return self._retained_entry_action('cancel')
    def retry_entry_connection(self):return self._retained_entry_action('retry-connection')

    def recover_entry_update(self):
        if not self.entry_actions_available() or self.busy or self._closing:return False
        request=self.entry_request
        if (request is None or request is not self.entry_check.pending_request or self._entry_phase()!='applying'
                or self.entry_recovery_confirmed):return False
        from PySide6.QtWidgets import QMessageBox
        answer=QMessageBox.question(self.window,'Recover interrupted update',
            'Recover the original retained update? This reader must close normally before the root can restore and verify the prior reader. User data and recovery evidence remain with their original owner.',
            QMessageBox.Yes|QMessageBox.No,QMessageBox.No)
        if answer!=QMessageBox.Yes or request is not self.entry_check.pending_request:return False
        return self._retained_entry_action('recover',confirmed=True)

    def _editor_external_busy(self):
        return bool(self._closing or self.future is not None or self.package_future is not None
            or self.editor_future is not None or self.handoff_future is not None or self.controller.close_held or self.executor.close_held
            or self.default_acquisition is not None and self.default_acquisition.close_held
            or self.entry_future is not None or self.entry_close_fault or self.entry_action_future is not None
            or self.entry_check is not None and self.entry_check.close_held)

    def configure_organizer_handoffs(self, *, receiver, adapter):
        """Explicit same-host receiver enrollment; no discovery, opening or send."""
        from core.organizer_encyclopedia_admission import OrganizerCatalogAdmissionReader
        from suite_core.media_organizer.encyclopedia_handoff_entry import OrganizerEncyclopediaHandoffReceiver
        from suite_core.knowledge.native_tag_catalog_host import NativeTagCatalogAdapter
        if (type(receiver) is not OrganizerEncyclopediaHandoffReceiver or type(adapter) is not NativeTagCatalogAdapter
                or type(receiver.read_admitted_catalog) is not OrganizerCatalogAdmissionReader
                or receiver.read_admitted_catalog.adapter is not adapter):
            raise ValueError('The original receiver and its exact selected native adapter are required')
        if self.organizer_receiver is not None:
            if self.organizer_receiver is not receiver or self.organizer_adapter is not adapter:
                raise ValueError('Retain the original Organizer receiver enrollment')
            return receiver
        if self.busy or self._closing:raise ValueError('Wait for original reader work before enrollment')
        self.organizer_receiver,self.organizer_adapter=receiver,adapter
        self.window._set_commands()
        return receiver

    def organizer_ready(self):
        return bool(not self.busy and not self._closing and not self.window.stale
            and self.organizer_receiver is not None and self.accepted_source is not None
            and self.accepted_source.kind=='native' and self.accepted_source.path==self.organizer_adapter.root
            and self.accepted_native_adapter is self.organizer_adapter
            and self.window.publication is not None and self.window.publication.catalog_id==self.organizer_adapter.catalog_id
            and self.window._selected_id and (self.editing_bridge is None or self.editing_bridge.close_ready()))

    def send_organizer_handoff(self, *, previous_display=None):
        if not self.organizer_ready():
            self.window.status.setText('Select a current native Tag and connect its original Organizer before opening search or review.')
            return False
        from core.encyclopedia_catalog import prepare_organizer_handoff
        from uuid import uuid4
        adapter,publication,stable_id=self.organizer_adapter,self.window.publication,self.window._selected_id
        self._handoff_generation=self.baseline_generation
        self._handoff_cancelled=False
        request_id=uuid4().hex
        try:
            self.handoff_future=self.executor.submit(lambda:prepare_organizer_handoff(adapter,publication,stable_id,
                request_id=request_id,previous_display=previous_display))
        except Exception as error:
            self.window.status.setText('Organizer handoff could not start. Retain the selected Tag and finish the current reader work before retrying.')
            self.window.status.setToolTip(str(error)[:1024])
            return False
        self.window.status.setText('Verifying this selected Tag revision before opening Organizer search or review. No image is changed.')
        return True

    def prepare_editing(self):
        if self.busy or self._closing or self.accepted_source is None or self.accepted_source.kind!='native':return False
        if self.dialog is not None and self.dialog.isVisible():
            self.window.status.setText('Close the default import review before opening Tag/Category editing.')
            return False
        if self.editing_bridge is not None:return True
        root=self.accepted_source.path
        self._editor_generation=self.baseline_generation
        self._editor_source=self.accepted_source
        self._editor_publication=self.window.publication
        publication,directory_pin=self._editor_publication,self.accepted_source.directory_pin
        job,started=self.jobs.try_acquire('Prepare native Tag editors',resource='io',exclusive=True)
        if not started:return False
        self._editor_job=job
        from core.encyclopedia_catalog import prepare_selected_editor
        self.editor_future=self.executor.submit(lambda:prepare_selected_editor(root,self,
            settings_path=self.service.settings_path,publication=publication,directory_pin=directory_pin,job=job))
        self.window.status.setText('Preparing the original Tag and Category editors for this selected database.')
        return True

    def _editor_saved(self):
        self.invalidate_default_baseline()
        self._editor_refresh_needed=True

    def _retire_editor(self):
        if self.editing_bridge is None:return True
        if self.busy or not self.editing_bridge.close_ready() or not self.editor_session.close():return False
        self.editing_bridge.tag_editor.page.deleteLater()
        self.editing_bridge.tag_editor.deleteLater()
        self.category_dialog.close();self.category_dialog.deleteLater()
        self.editing_bridge.deleteLater()
        self.editing_bridge=self.editor_session=self.category_dialog=None
        self.knowledge=None
        self.window._set_commands()
        return True

    def invalidate_default_baseline(self):
        """Before host publication; never reset, including away/back changes."""
        self.baseline_generation += 1

    def configure_default_acquisition(self, *, service, observe_baseline,
            package_component=None, preparation_directory=None, channel='stable'):
        """Explicit admitted service only; no feed or trust inferred here."""
        if self.default_acquisition is not None or self.busy or self._closing:
            raise RuntimeError('Retain the original acquisition owner')
        if channel not in {'stable', 'beta'}:
            raise ValueError('Explicit supported content channel required')
        if (package_component is None) != (preparation_directory is None):
            raise ValueError('Supply both admitted component and disposable preparation directory')
        self.package_component = package_component
        self.preparation_directory = Path(preparation_directory) if preparation_directory is not None else None
        self.default_content_channel = channel
        from .default_acquisition_controller import DefaultAcquisitionController
        self.default_acquisition = DefaultAcquisitionController(service=service,
            executor=self.executor, observe_baseline=observe_baseline,
            baseline_generation=lambda: self.baseline_generation)
        return self.default_acquisition

    def refresh_default_content(self):
        if self.default_acquisition is None or self.busy or self._closing:
            return False
        self.default_acquisition.refresh(channel=self.default_content_channel)
        self.window.status.setText('Checking optional Tag content releases. Local source review remains available.')
        return True

    def download_default_content(self):
        if (self.default_acquisition is None or self.content_confirm is None
                or self.busy or self._closing or not self.content_confirm.isChecked()):
            return False
        offer = self.content_offers.currentData()
        if offer is None:
            return False
        self.default_acquisition.acquire(offer, confirmed=True)
        self.window.status.setText('Downloading the selected Tag data package. Cancel is available; nothing is imported yet.')
        self.content_confirm.setChecked(False)
        return True

    def review_downloaded_content(self):
        if self.busy or self._closing or self.default_acquisition is None:
            return False
        acquired = self.default_acquisition.result
        if (acquired is None or acquired is not self._download_result
                or self._download_generation != self.baseline_generation
                or self.package_component is None or self.accepted_source is None
                or self.accepted_source.kind != 'native' or self.content_offers is None
                or self.content_offers.currentData() is not acquired.acquired.offer):
            self.window.status.setText('Select the same native catalog and refresh content before reviewing this download.')
            return False
        stock = self.default_acquisition.service.acquisition
        if stock is None:
            return False
        self._package_generation = self.baseline_generation
        self.package_cancel = cancel = Event()
        root = self.accepted_source.path
        component, destination = self.package_component, self.preparation_directory
        from uuid import uuid4
        destination = destination.with_name(destination.name + '-' + uuid4().hex)
        def work():
            from core.encyclopedia_catalog import prepare_downloaded_default_review
            if self.default_acquisition.observe_baseline() != acquired.baseline:
                raise RuntimeError('Selected catalog changed before source preparation')
            result = prepare_downloaded_default_review(acquired, destination=destination,
                trust_bytes=stock.trust, component=component, acquisition_cache_root=stock.root,
                settings_path=self.service.settings_path, catalog_root=root, should_cancel=cancel.is_set)
            if self.default_acquisition.observe_baseline() != acquired.baseline:
                raise RuntimeError('Selected catalog changed during source preparation')
            return result
        self.package_future = self.executor.submit(work)
        self.window.status.setText('Verifying downloaded Tag content and reviewing the selected catalog. Nothing is imported yet.')
        return True

    def choose_database(self):
        if self._closing or self.busy:
            return False
        root = QFileDialog.getExistingDirectory(self.window, "Open Tag Database folder")
        return self.open_database(root) if root else False

    def open_database(self, root):
        if self._closing or self.busy:
            return False
        if self.editing_bridge is not None and not self.editing_bridge.close_ready():
            self.window.status.setText('Finish Save or Cancel in Tag/Category Details before changing the catalog.')
            return False
        self.invalidate_default_baseline()
        self._accepted_native_adapter=None
        self._cancelled = False
        root = Path(root)
        same = self.accepted_source is not None and self.accepted_source.kind == "native" and self.accepted_source.path == root
        self._before_view = self.service.view
        self._opening_source = self.accepted_source
        baseline = self.window.publication if same else None
        directory = self.accepted_source.directory_pin if same else None
        self.future = self.executor.submit(self.service.open_catalog, root, baseline, directory)
        self.window.status.setText("Opening Tag Database. Cancel is available; previous view stays visible.")
        return True

    def refresh(self):
        if self._closing or self.busy:
            return False
        source = self.accepted_source
        if source is not None and source.kind == "native":
            return self.open_database(source.path)
        if source is not None and source.kind == "export":
            return self.window.open_snapshot(source.path, refreshing=True)
        return False

    @property
    def accepted_native_adapter(self):
        """Only the original adapter exposed by successful matching delivery."""
        return self._accepted_native_adapter if not self.window.stale else None

    def prepare_delivery(self, *, native_root=None, export_path=None, directory_pin=None,native_adapter=None,publication=None):
        proposed=AcceptedCatalogSource("native",Path(native_root),directory_pin,native_adapter,
            publication if native_adapter is not None else None) if native_root is not None else None
        if self.editing_bridge is not None and (native_root is None or Path(native_root)!=self.editor_session.root):
            if not self._retire_editor():raise ValueError('Finish the original editor Save/Cancel before changing catalog authority')
        # Called before the reader publishes its new catalog/revision.
        self.invalidate_default_baseline()
        if native_root is not None:
            old=self.accepted_source
            if (native_adapter is not None and old is not None and old.native_adapter is not None
                    and old.kind=='native' and old.path==Path(native_root) and old.directory_pin==directory_pin
                    and publication is not None and old.native_adapter.catalog_id==publication.catalog_id):
                native_adapter=old.native_adapter
            return replace(proposed,native_adapter=native_adapter)
        elif export_path is not None:
            return AcceptedCatalogSource("export", Path(export_path).absolute())
        return AcceptedCatalogSource("memory", None)

    def accept_delivery(self, descriptor):
        """Commit only from the reader's successful immutable delivery boundary."""
        self.accepted_source = descriptor
        self._accepted_native_adapter=descriptor.native_adapter
        if descriptor.kind != "native":
            self.service.view = None
        self.window._set_commands()
        self.catalog_accepted.emit(descriptor)

    def show_defaults(self):
        if self.editing_bridge is not None and not self.editing_bridge.close_ready():
            self.window.status.setText('Finish Save or Cancel in Tag/Category Details before reviewing imports.')
            return False
        if self._closing or self.busy:
            return False
        if not self._retire_editor():return False
        if self.dialog is None:
            host = self

            class CatalogDialog(QDialog):
                def closeEvent(self, event):
                    if host.controller.close_held or host.executor.close_held or host.package_future is not None:
                        host.controller.cancel()
                        host.package_cancel.set()
                        event.ignore()
                        host.window.status.setText("Database work is settling. Keep this window open until it finishes.")
                        return
                    super().closeEvent(event)

            self.dialog = CatalogDialog(self.window)
            self.dialog.setWindowTitle("Review optional Tag content")
            self.dialog.resize(1000, 740)
            layout = QVBoxLayout(self.dialog)
            intro = QLabel("Choose a local Tag data package and your own catalog folder. Review before connecting or importing. Opening a database never installs defaults.")
            intro.setWordWrap(True); layout.addWidget(intro)
            self.content_box = QGroupBox('Get optional Tag content')
            content_layout = QVBoxLayout(self.content_box)
            content_scope=QLabel('Download a reviewed data package, then review its local source below. Connecting and importing still require their own confirmations.')
            content_scope.setWordWrap(True);content_layout.addWidget(content_scope)
            row=QHBoxLayout()
            self.content_refresh=QPushButton('Refresh content releases')
            self.content_offers=QComboBox()
            self.content_download=QPushButton('Download selected content')
            self.content_review=QPushButton('Review downloaded content')
            self.content_refresh.setEnabled(self.default_acquisition is not None)
            self.content_download.setEnabled(False)
            self.content_review.setEnabled(False)
            for widget in (self.content_refresh,self.content_offers,self.content_download,self.content_review):
                row.addWidget(widget)
            content_layout.addLayout(row)
            self.content_confirm=QCheckBox('Download this exact selected data package; do not import it yet')
            content_layout.addWidget(self.content_confirm)
            self.content_refresh.clicked.connect(self.refresh_default_content)
            self.content_download.clicked.connect(self.download_default_content)
            self.content_review.clicked.connect(self.review_downloaded_content)
            layout.addWidget(self.content_box)
            self.content_box.setVisible(self.default_acquisition is not None)
            self.pane = TagDatabasePane(self.controller, parent=self.dialog)
            self.pane.open_encyclopedia.hide()  # The original standalone reader is already visible.
            self.pane.timer.stop()  # This original host polls once and delivers results.
            layout.addWidget(self.pane)
        self.dialog.show(); self.dialog.raise_()
        return True

    def cancel(self):
        if self.entry_action_future is not None or (self.entry_action_work is not None
                and self.entry_check is not None and self.entry_check.close_held):
            self.window.status.setText('The original update reply is still retained. Wait or Resume Pending Update Request; Cancel Pending Update is available only after its request is acknowledged.')
            return False
        self._cancelled = True
        self._handoff_cancelled=True
        if self.handoff_future is not None:self.handoff_future.cancel()
        if self.editor_future is not None:
            self.editor_future.cancel()
            self.jobs.cancel(self._editor_job.id)
        if self.future is not None:
            self.future.cancel()
        self.controller.cancel()
        self.package_cancel.set()
        if self.package_future is not None:
            self.package_future.cancel()
        if self.default_acquisition is not None:
            self.default_acquisition.cancel()
        if self.help is not None:
            self.help.request_cancel()

    def poll(self):
        if self.entry_action_future is not None and self.entry_action_future.done() and not self.executor.close_held:
            future,self.entry_action_future=self.entry_action_future,None
            operation=self.entry_action_operation
            try:
                from core.encyclopedia_entry_adapter import EncyclopediaEntryRequest,EncyclopediaEntryStatus
                result=future.result()
                if operation=='status':
                    if type(result) is not EncyclopediaEntryStatus:raise ValueError('exact_entry_status_required')
                    self.entry_status=result
                    previous_request=self.entry_request
                    self.entry_request=self.entry_check.pending_request
                    if previous_request is not self.entry_request:self.entry_recovery_confirmed=False
                    if result.status=='held':
                        self.window.status.setText('Original update status is held. Keep the retained request and review the diagnostic.')
                        self.window.status.setToolTip(result.reason)
                    else:
                        if self.entry_request is None:self.entry_recovery_confirmed=False
                        self.window.status.setText('Reader '+(result.current_version or 'unknown')+'; '+
                            ('no pending update.' if result.phase is None else 'original update phase: '+result.phase+'.')+
                            (' Close this reader normally to continue.' if result.phase=='waiting_for_close' else ''))
                else:
                    if type(result) is not EncyclopediaEntryRequest:raise ValueError('exact_entry_request_result_required')
                    self.entry_request=self.entry_check.pending_request
                    self.entry_last_action=result.status
                    if operation=='confirm':self.entry_status=None
                    if operation=='recover':self.entry_recovery_confirmed=True
                    if operation=='cancel':
                        self.entry_status=None;self.entry_recovery_confirmed=False
                    self.window.status.setText({
                        'waiting_for_close':'Update confirmed. Close this reader normally; the original root will apply and verify it after physical drain.',
                        'cancelled_before_apply':'Pending update cancelled before Apply. Continue browsing locally.',
                        'saved':'Reader connection saved. Check Update / Recovery Status for the retained original phase.',
                        'save_pending':'Reader connection is still pending. Keep the original root and retry saving when ready.',
                        'recovery_waiting_for_close':'Recovery confirmed. Close this reader normally before the original root restores and verifies the prior reader.'}[result.status])
                self.entry_action_work=self.entry_action_operation=self.entry_action_body=None
                self.entry_action_error=''
            except Exception as error:
                self.entry_action_error=type(error).__name__+': '+str(error)[:1024]
                self.window.status.setText('Original update request is held. Resume its retained reply if available; otherwise check status. No replacement request was created.')
                self.window.status.setToolTip(self.entry_action_error)
            self.window._set_commands()
        if self.handoff_future is not None and self.handoff_future.done() and not self.executor.close_held:
            future,self.handoff_future=self.handoff_future,None
            try:
                request=future.result()
                if self._closing or self._handoff_cancelled or self._handoff_generation!=self.baseline_generation:
                    self.window.status.setText('Organizer handoff discarded after cancellation or catalog change. Choose a fresh Tag before retrying.')
                elif self.organizer_receiver.receive(request):
                    self.window.status.setText('Organizer accepted the request to open search or correction review. Image changes still require its own review and confirmation.')
                else:
                    self.window.status.setText('Organizer navigation or review is held. Finish its current choice, then send a new explicit request.')
            except Exception as error:
                self.window.status.setText('Organizer handoff is held. Refresh the selected native catalog and Organizer Knowledge before retrying; no image was changed.')
                self.window.status.setToolTip(str(error)[:1024])
        if self.editor_future is not None and self.editor_future.done() and not self.executor.close_held:
            future,self.editor_future=self.editor_future,None
            job,self._editor_job=self._editor_job,None
            session=dialog=tag_editor=bridge=None
            old_knowledge,old_settings=self.knowledge,self.settings
            try:
                session,preparation,settings=future.result()
                if (self._closing or job.cancel_requested or self._editor_generation!=self.baseline_generation
                        or self.accepted_source is not self._editor_source or self.window.publication is not self._editor_publication):
                    session.close();self.knowledge=None
                    self.window.status.setText('Editor preparation discarded after cancellation or selection change. Browse remains available.')
                else:
                    from core.encyclopedia_catalog import require_selected_editor_binding
                    require_selected_editor_binding(session.adapter,self._editor_publication,self._editor_source.directory_pin)
                    session.require_editor_ready()
                    from .editing_ui import create_category_workspace,EncyclopediaEditingBridge
                    from qt_app.tag_details_host import TagDetailsEditorHost
                    host=self
                    class CategoryDialog(QDialog):
                        def closeEvent(self,event):
                            if host.editing_bridge is not None and not host.editing_bridge.close_ready():
                                event.ignore();host.window.status.setText('Finish Category Save/Cancel before closing this workspace.')
                            else:event.accept()
                    dialog=CategoryDialog(self.window);dialog.setWindowTitle('Categories');dialog.resize(1100,800)
                    layout=QVBoxLayout(dialog)
                    workspace=create_category_workspace(session,parent=dialog,pool=self.editor_pool,settings=settings)
                    layout.addWidget(workspace)
                    self.knowledge=session.tags
                    self.settings=settings
                    tag_editor=TagDetailsEditorHost(session,self,preparation=preparation,
                        category_workspace=workspace,pool=self.editor_pool,parent=dialog)
                    tag_editor.page.hide()
                    bridge=EncyclopediaEditingBridge(session,category_workspace=workspace,
                        tag_editor=tag_editor,selected_tag=lambda:self.window._selected_id,
                        selected_category=lambda:self.window.category.currentData() or '',
                        host_busy=self._editor_external_busy,parent=dialog)
                    bridge.saved.connect(self._editor_saved)
                    bridge.unavailable.connect(lambda text:self.window.status.setText(text))
                    require_selected_editor_binding(session.adapter,self._editor_publication,self._editor_source.directory_pin)
                    self.editor_session,self.category_dialog,self.editing_bridge=session,dialog,bridge
                    self.window._set_commands()
                    self.window.status.setText('Original Tag and Category editing is ready for this selected database.')
            except Exception as error:
                self.editing_bridge=self.editor_session=self.category_dialog=None
                self.knowledge,self.settings=old_knowledge,old_settings
                if session is not None:session.close()
                if dialog is not None:dialog.deleteLater()
                self.window.status.setText('Editing is held for this database. Browse remains available; review recovery or reopen before trying again.')
                self.window.status.setToolTip(str(error))
            finally:
                self.jobs.release(job.id)
        self.controller.poll()
        if self.default_acquisition is not None:
            before_download = self.default_acquisition.result
            self.default_acquisition.poll()
            if self.default_acquisition.result is not None and self.default_acquisition.result is not before_download:
                self._download_result = self.default_acquisition.result
                self._download_generation = self.baseline_generation
                self.window.status.setText('Tag data package downloaded. Review downloaded content before connecting or importing.')
            error = self.default_acquisition.error
            if error and error != self._content_error:
                self.window.status.setText('Tag content work failed. Refresh content releases, then review and confirm the selected package before retrying. No import was performed.')
                self.window.status.setToolTip(error[:1024])
            self._content_error = error
        if self.package_future is not None and self.package_future.done() and not self.executor.close_held:
            future, self.package_future = self.package_future, None
            try:
                result = future.result()
                if self.package_cancel.is_set() or self._closing or self._package_generation != self.baseline_generation:
                    self.window.status.setText('Downloaded source review discarded after cancellation or selection change. Prepared cache is retained; refresh before reviewing again.')
                else:
                    self.prepared_default_review = result
                    if self.dialog is None:
                        self.show_defaults()
                    self.pane.source_directory.setText(str(result.package.source_directory))
                    self.pane.catalog_directory.setText(str(result.connection_review.catalog_root))
                    self.pane.create_catalog.setChecked(False)
                    self.controller.connection_review = result.connection_review
                    self.controller.reason = 'Downloaded source verified. Confirm this exact local connection; Preview and Apply remain separate.'
                    self.pane.render()
                    self.window.status.setText(self.controller.reason)
            except Exception as error:
                self.window.status.setText('Downloaded content preparation is held. Retain its cache and review the reason; nothing was imported.')
                self.window.status.setToolTip(str(error))
        if self.content_box is not None:
            self.content_box.setVisible(self.default_acquisition is not None)
            discovery = self.default_acquisition.discovery if self.default_acquisition is not None else None
            if discovery is not self._content_discovery:
                self._content_discovery = discovery
                self.content_offers.clear()
                for offer in discovery.offers if discovery is not None else ():
                    self.content_offers.addItem('Data package ' + offer.release.software_version,offer)
                self.content_confirm.setChecked(False)
                if discovery is not None:
                    self.window.status.setText('Verified Tag data offer available. Confirm before downloading.' if discovery.offers else
                        'Content release checking could not establish a verified offer. Local source review remains available.')
            ready = not self.busy and not self._closing
            receipt = self.default_acquisition.result if self.default_acquisition is not None else None
            if discovery is None and receipt is not None and self.content_offers.currentData() is not receipt.acquired.offer:
                self.content_offers.clear()
                self.content_offers.addItem('Downloaded data package ' + receipt.acquired.offer.release.software_version,
                    receipt.acquired.offer)
            self.content_refresh.setEnabled(ready)
            self.content_download.setEnabled(ready and discovery is not None
                and any(offer is self.content_offers.currentData() for offer in discovery.offers)
                and self.content_confirm.isChecked())
            selected = self.content_offers.currentData()
            self.content_review.setEnabled(ready and self._download_result is not None
                and self.default_acquisition is not None and self.default_acquisition.result is self._download_result
                and self._download_generation == self.baseline_generation and selected is self._download_result.acquired.offer
                and self.package_component is not None and selected.release.software_version == self.package_component.software_version
                and self.accepted_source is not None and self.accepted_source.kind == 'native')
        if self.help is not None:
            self.help.poll()
        if self.entry_future is not None and self.entry_future.done() and not self.executor.close_held:
            future, self.entry_future = self.entry_future, None
            try:
                future.result()
                self.entry_close_fault = False
            except Exception:
                self.entry_close_fault = True
                self.window.status.setText("Close acknowledgement is unconfirmed. Keep the original window open.")
        if (self._closing and self.entry_close_started and self.entry_check is not None
                and not self.entry_check.close_admitted and self.entry_future is None
                and self.entry_check.can_resume('close') and not self.executor.close_held):
            self.entry_future = self.executor.submit(self.entry_check.request_close)
        self.window.cancel_work_button.setVisible(self.busy and not self._closing)
        self.window.cancel_close_button.setVisible(self._closing)
        if self.pane is not None:
            self.pane.render()
        if self.controller.pending:
            self.window.status.setText(self.controller.phase)
        result = self.controller.result
        if result is not None and result is not self._shown_result:
            self._shown_result = result
            publication = result if type(result) is Publication else getattr(result, "publication", None)
            if (publication is None and not self.controller.error
                    and self.controller.last_operation == "recover"
                    and self.controller.choice is not None):
                try:
                    publication = read(self.controller.choice.transactions.destination)
                except (ValueError, OSError) as error:
                    self.window.status.setText("Recovery was saved, but the reader could not reload. Retain this database and choose Reload saved catalog.")
                    self.window.status.setToolTip(str(error))
            if publication is not None:
                root = self.controller.choice.native_adapter.root
                delivered = self.window.open_publication(publication, path=self.controller.choice.transactions.destination,
                                             native_root=root, directory_pin=self.controller.choice.native_adapter.directory_pin,
                                             native_adapter=self.controller.choice.native_adapter,
                                             refreshing=self.window.publication is not None and
                                             self.window.publication.catalog_id == publication.catalog_id)
                if delivered:
                    if self.controller.error:
                        self.window.status.setText('Tags were saved and the reader refreshed, but host observations are held. Refresh observations before further review; do not apply again.')
                        self.window.status.setToolTip(self.controller.error)
                    else:self.window.status.setText("Tag Database saved and ready to browse.")
            elif getattr(result, "publication_error", ""):
                self.window.status.setText("Tags were saved. Reader refresh failed; choose Reload saved catalog in the review window. Do not import again.")
        if self.future is not None and self.future.done() and not self.executor.close_held:
            future, self.future = self.future, None
            changed_source = self.accepted_source is not self._opening_source
            if changed_source:
                self.service.view = None
                self.window.status.setText("Earlier opening discarded. The current catalog stays selected.")
            elif future.cancelled() or self._cancelled:
                self.service.view = self._before_view
                self.window.status.setText("Opening cancelled. Previous database stays visible.")
            else:
                try:
                    view = future.result()
                    refreshing = self.window.publication is not None and self.window.publication.catalog_id == view.publication.catalog_id
                    delivered = self.window.open_publication(view.publication, native_root=view.root,
                        directory_pin=view.stamp[:2],native_adapter=getattr(view,'adapter',None), refreshing=refreshing)
                    if delivered:
                        self.window.status.setText("Tag Database opened. Search or choose a category to browse.")
                    else:
                        self.service.view = self._before_view
                except Exception as error:
                    self.service.view = self._before_view
                    self.window.status.setText("Tag Database could not be opened. Previous view is retained. " + str(error))
        if self._closing and not self.busy:
            self.window.close()
        elif self._editor_refresh_needed and not self.busy and self.editing_bridge is not None and self.editing_bridge.close_ready():
            self._editor_refresh_needed=False
            self.refresh()

    def close_ready(self):
        if self.editing_bridge is not None and not self.editing_bridge.close_ready():
            self.window.status.setText('Finish Save or Cancel in the original Tag/Category Details before closing.')
            return False
        if self.busy:
            self.cancel()
            self.window.status.setText("Closing is waiting for database or update work. Cancel Close to continue browsing.")
            self._closing = True
            self.window.cancel_work_button.hide()
            self.window.cancel_close_button.setVisible(not self.entry_close_started)
            return False
        if self.entry_check is not None and not self.entry_check.close_admitted:
            self._closing = True
            self.entry_close_started = True
            self.window.cancel_close_button.hide()
            self.window.status.setText("Waiting for the original Close acknowledgement. Keep this window open while it settles.")
            if self.help is not None:
                self.help.begin_close()
            if self.entry_future is None:
                self.entry_future = self.executor.submit(self.entry_check.request_close)
            return False
        self.controller.begin_close()
        if not self._retire_editor():return False
        if not self.jobs.shutdown(wait=False):return False
        if self.default_acquisition is not None:
            self.default_acquisition.begin_close()
        if self.help is not None:
            self.help.begin_close()
        self.executor.begin_close()
        if self.dialog is not None:
            self.dialog.close()
        self.timer.stop()
        return not self.executor.close_held

    def cancel_close(self):
        if not self.executor.closed and not self.entry_close_started:
            self._closing = False
            self.window.cancel_close_button.hide()
            self.window.status.setText("Close cancelled. Current database is retained.")
