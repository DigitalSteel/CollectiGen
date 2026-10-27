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
    read_binding: object | None = None
    read_only: bool = False
    candidate_only: bool = False

    def __post_init__(self):
        if type(self.read_only) is not bool or type(self.candidate_only) is not bool:
            raise ValueError('Exact reader authority flags required')
        if self.candidate_only and not self.read_only:
            raise ValueError('Candidate delivery must remain read-only')
        if self.read_only:
            from core.encyclopedia_read_admission import ReadAdmissionBinding,validate_read_binding
            if (type(self.read_binding) is not ReadAdmissionBinding
                    or self.kind != 'native' or self.native_adapter is not None
                    or type(self.publication) is not Publication):
                raise ValueError('Exact immutable core read binding and detached native publication required')
            binding = self.read_binding
            from core.tag_catalog_connection import TagConnectionHold
            try:
                binding.__post_init__()
                validate_read_binding(binding,root=self.path,identity=binding.observation.identity,
                    publication=self.publication,stamp=binding.stamp)
            except (TagConnectionHold,AttributeError,IndexError,TypeError) as error:
                raise ValueError(str(error)) from error
            if self.candidate_only != binding.candidate_only or self.directory_pin != binding.stamp[:2]:
                raise ValueError('Exact candidate status and directory binding required')
        elif self.read_binding is not None:
            raise ValueError('Read binding cannot grant writable delivery')
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
        elif self.publication is not None and not self.read_only:
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
        self._creation_phase = None
        self._creation_confirming = False
        self._creation_cancel = None
        self._creation_started = None
        self.dialog = self.pane = None
        self.restore_dialog = None
        self.reader_delivery_result = None
        self.reader_delivery_ok = False
        self.reader_delivery_error = ""
        self.reader_delivery_publication = None
        self.optional_content_factory = None
        self._shown_result = None
        self._cancelled = False
        self._closing = False
        self._read_generation = 0
        self._opening_generation = None
        self._pending_read_view = None
        self._accepted_service_view = None
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
        self.content_status = None
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
        self._handoff_waiting=False
        self._handoff_scope=None
        self._handoff_request_id=None
        self.accepted_source = None
        self._accepted_native_adapter=None
        self._before_view = None
        self._opening_source = None
        self._export_open = None
        self.timer = QTimer(self)
        self.timer.setInterval(40)
        self.timer.timeout.connect(self.poll)
        self.timer.start()

    @property
    def busy(self):
        return bool(self.future is not None or self.controller.close_held or self.executor.close_held or self.other_work_busy)

    @property
    def other_work_busy(self):
        return self._other_work_busy()

    def _other_work_busy(self, *, saved_feedback=False):
        help_busy = self.help is not None and self.help.close_held
        if saved_feedback and help_busy:
            from qt_app.encyclopedia_help_commands import EncyclopediaHelpCommands
            if (type(self.help) is EncyclopediaHelpCommands and self.help.executor is self.executor
                    and self.help.future is None and not self.help.closing
                    and (self.help.checker is None or not getattr(self.help.checker, 'close_held', True))):
                help_busy = False  # The idle helper only aliases this exact retained executor.
        return bool(self.future is not None or self._creation_confirming or
                    help_busy
                    or self.entry_future is not None or self.entry_close_fault
                    or self.entry_action_future is not None
                    or self.entry_check is not None and self.entry_check.close_held
                    or self.default_acquisition is not None and self.default_acquisition.close_held
                    or self.package_future is not None or self.editor_future is not None
                    or self.handoff_future is not None or self._handoff_waiting
                    or self.editor_pool.activeThreadCount()>0 or bool(self._operation_jobs)
                    or self.editing_bridge is not None and self.editing_bridge.busy)

    def saved_open_feedback_available(self):
        """Only the exact original saved read may expose non-mutating feedback."""
        c = self.controller
        feedback = c.pending_feedback
        return bool(feedback and feedback[1] == 'connection_open'
                    and not c.closed and not c.faulted and not c._unconfirmed
                    and c.executor is self.executor and self.executor._future is c._future
                    and not self.executor._uncertain and not self.executor.closed
                    and not self._closing and not self._other_work_busy(saved_feedback=True))

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
                callback=self.prepare_editing,enabled=lambda:not self.busy and self._write_actions_allowed() and self.accepted_source is not None and self.accepted_source.kind=='native')]
        commands=[]
        bridge = self.editing_bridge
        for command in self.editing_bridge.commands:
            original = command.callback
            if command.key in ('encyclopedia:new-category','encyclopedia:edit-category'):
                def category_action(callback=original):
                    if self.editing_bridge is not bridge or not self._write_actions_allowed():return False
                    self.category_dialog.show();self.category_dialog.raise_()
                    return callback()
                command=replace(command,callback=category_action)
            else:
                def editor_action(callback=original):
                    if self.editing_bridge is not bridge or not self._write_actions_allowed():return False
                    return callback()
                command=replace(command,callback=editor_action)
            def editor_available(original=command.enabled):
                return (self.editing_bridge is bridge and self._write_actions_allowed()
                    and (original is None or original()))
            command=replace(command,enabled=editor_available)
            commands.append(command)
        return commands

    def _write_actions_allowed(self):
        return not self._closing and not (self.accepted_source is not None and self.accepted_source.read_only)

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
            or self.editor_future is not None or self.handoff_future is not None or self._handoff_waiting or self.controller.close_held or self.executor.close_held
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
            self._handoff_scope=self.organizer_receiver.prepare_recipient_scope()
            self._handoff_request_id=request_id
            self.handoff_future=self.executor.submit(lambda:prepare_organizer_handoff(adapter,publication,stable_id,
                request_id=request_id,previous_display=previous_display))
        except Exception as error:
            self._handoff_scope=None
            self.window.status.setText('Organizer handoff could not start. Review its admitted profile and included libraries, then retry this selected Tag. No image was changed.')
            self.window.status.setToolTip(str(error)[:1024])
            return False
        self.window.status.setText('Verifying this selected Tag revision before opening Organizer search or review. No image is changed.')
        self.window.bar.refresh_state()
        return True

    def _organizer_handoff_settled(self, receipt):
        """Original GUI receiver calls back with this exact request generation."""
        from core.organizer_encyclopedia_handoffs import SearchReceiptStage
        from suite_core.media_organizer.encyclopedia_handoff_entry import HostedHandoffReceipt
        if type(receipt) is not HostedHandoffReceipt or receipt.stage is SearchReceiptStage.QUEUED:
            return
        if (receipt.request_id!=self._handoff_request_id or receipt.source_generation!=self._handoff_generation):
            return
        self._handoff_waiting=False
        self._handoff_scope=None
        if self._closing or receipt.source_generation!=self.baseline_generation:
            return  # An old request cannot overwrite a new catalog/Close status.
        if receipt.stage is SearchReceiptStage.CANCELLED:
            message='Organizer handoff cancelled before it opened. No image was changed.'
        elif receipt.stage is SearchReceiptStage.UNKNOWN_OUTCOME:
            message='Organizer handoff outcome could not be confirmed. Check Organizer before sending a new request; this request will not be sent again automatically.'
        elif self._handoff_cancelled:
            return
        elif receipt.stage is SearchReceiptStage.DELIVERED:
            message=('Organizer opened matching-image search. No image was changed.' if receipt.reason=='native_search_opened'
                else 'Organizer opened correction review. Image changes still require its own review and confirmation.')
        elif receipt.stage is SearchReceiptStage.EXPIRED:
            message='Organizer handoff expired while waiting. Finish opening Dataset, then send a new explicit request. No image was changed.'
        else:
            message={
                'recipient_scope_changed':'Organizer handoff is held because its profile, included libraries or privacy scope changed. Review that scope, then send a new request. No image was changed.',
                'recipient_scope_unavailable':'Organizer scope is unavailable. Review its included libraries, then send a new explicit request. No image was changed.',
                'catalog_admission_held':'Organizer handoff is held because the Tag catalog changed. Refresh the native catalog and Organizer Knowledge, then send a new request. No image was changed.',
                'dataset_preparation_failed':'Organizer could not open Dataset. Retry Dataset in Organizer, then send a new explicit request. No image was changed.',
                'dataset_shutdown':'Organizer is closing. Reopen it before sending a new request. No image was changed.',
                'stale_sender_generation':'Organizer handoff is held after the selected catalog changed. Choose a current Tag before sending a new request. No image was changed.',
            }.get(receipt.reason,'Organizer navigation or review is held. Finish its current choice, then send a new explicit request. No image was changed.')
        self.window.status.setText(message)
        self.window.status.setToolTip('')
        self.window.bar.refresh_state()

    def prepare_editing(self):
        if self.restore_dialog is not None and self.restore_dialog.isVisible():
            self.window.status.setText('Close the completed-import review before editing Tags or Categories. Its decisions are retained.')
            return False
        if self.busy or not self._write_actions_allowed() or self.accepted_source is None or self.accepted_source.kind!='native':return False
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
        self.controller.completed_review = None

    def tag_data_native_view(self):
        """Retain only the original accepted writable native reader selection."""
        view = self._accepted_service_view if hasattr(self, '_accepted_service_view') else None
        source = self.accepted_source
        if (source is None or source.kind != 'native' or source.read_only
                or view is None or self.service.view is not view
                or self._accepted_native_adapter is not view.adapter
                or source.native_adapter is not view.adapter):
            return None
        return view

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
        if self.busy or not self._write_actions_allowed() or self.default_acquisition is None:
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

    def create_database(self):
        if self.restore_dialog is not None and self.restore_dialog.isVisible():
            self.restore_dialog.raise_()
            self.window.status.setText('Close the completed-import review before creating a database. Its decisions are retained.')
            return False
        if self._closing or self.busy:
            return False
        root = QFileDialog.getExistingDirectory(self.window, 'Choose an empty folder for your new tag database')
        if not root:
            return False
        from core.encyclopedia_catalog_creation import prepare_empty_catalog
        from threading import Event
        from time import monotonic
        self._cancelled = False
        self._creation_cancel = Event()
        self._creation_started = monotonic()
        self._creation_phase = 'review'
        self.future = self.executor.submit(prepare_empty_catalog, self.service.settings_path, root)
        self.window.status.setText('Checking the empty database folder. No database has been created yet.')
        return True

    def _poll_creation(self):
        if self._creation_phase is None:
            return False
        from time import monotonic
        self.window.database_activity.show()
        self.window.database_activity.set_active(True, 'Creating a new tag database')
        self.window.database_elapsed.setText(f'{max(0, int(monotonic() - self._creation_started))}s elapsed')
        self.window.database_elapsed.show()
        self.window.cancel_work_button.setVisible(not self._closing)
        self.window._sync_start_choices()
        if self.future is None or not self.future.done() or self.executor.close_held:
            return True
        future, self.future = self.future, None
        phase, self._creation_phase = self._creation_phase, None
        self.window.database_activity.set_active(False)
        self.window.database_activity.hide()
        self.window.database_elapsed.hide()
        try:
            if future.cancelled():
                self.window.status.setText('Database creation cancelled before it started.')
                return False
            result = future.result()
            if self._closing or self._cancelled:
                self.window.status.setText('Database creation cancelled before confirmation.' if phase == 'review' else
                    'Creation settled after Stop. Keep the selected folder; use Open an existing database to check its saved result.')
                return False
            if phase == 'review':
                from PySide6.QtWidgets import QMessageBox
                from PySide6.QtCore import Qt
                box = QMessageBox(self.window)
                box.setWindowTitle('Create a tag database')
                box.setIcon(QMessageBox.Question)
                box.setTextFormat(Qt.PlainText)
                box.setText('Create an empty tag database?')
                box.setInformativeText(f'Your tags, categories and edits will be stored in the folder you chose: {result.root.name}. You can add your own tags or review optional defaults later. No images or starter tags are imported now.')
                box.setDetailedText(f'Folder: {result.root}\nExisting data is never replaced. Any recovery stage is retained on failure.')
                box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
                box.setDefaultButton(QMessageBox.No)
                box.button(QMessageBox.Yes).setText('Create database')
                box.button(QMessageBox.No).setText('Cancel')
                self._creation_confirming = True
                try:
                    accepted = box.exec() == QMessageBox.Yes
                finally:
                    self._creation_confirming = False
                if not accepted or self._closing:
                    self.window.status.setText('Creation cancelled. Choose Create or Open when ready; no database was created.')
                    return False
                from core.encyclopedia_catalog_creation import confirm_empty_catalog
                self._creation_phase = 'create'
                self.future = self.executor.submit(lambda: confirm_empty_catalog(result, result.token, should_cancel=self._creation_cancel.is_set))
                self.window.status.setText('Creating your database. Stop waits for the current safe write to finish; saved data is retained.')
                return True
            self.window.status.setText('Your empty database was created. Opening it for browsing.')
            self.open_database(result)
        except Exception as error:
            from PySide6.QtWidgets import QMessageBox
            self.window.status.setText('Could not create the database. Choose an empty folder and retry, or Open a database already created. Existing databases are unchanged.')
            self.window.status.setToolTip('Creation failed; Details explains the error.')
            if not self._closing:
                box = QMessageBox(self.window)
                box.setWindowTitle('Database creation needs attention')
                box.setIcon(QMessageBox.Warning)
                box.setText('The database could not be created.')
                box.setInformativeText('Check your selected folder. If a database was already created, open it; otherwise choose an empty folder and try again. Retained recovery files are preserved.')
                box.setDetailedText(type(error).__name__ + ': ' + str(error))
                box.exec()
        finally:
            self.window._sync_start_choices()
        return False

    def choose_database(self):
        if self._closing or self.busy:
            return False
        root = QFileDialog.getExistingDirectory(self.window, "Open Tag Database folder")
        return self.open_database(root) if root else False

    def open_database(self, root):
        if self._closing or self.busy:
            return False
        if self.restore_dialog is not None:
            self.controller.cancel()
            self.restore_dialog.invalidate_context()
            self.restore_dialog.close()
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
        self._read_generation += 1
        self._opening_generation = self._read_generation
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

    def open_export(self, path, reader, *, refreshing=False):
        """Read on the original executor; accept only in its original Qt delivery."""
        if self._closing or self.busy:
            return False
        if self.editing_bridge is not None and not self.editing_bridge.close_ready():
            self.window.status.setText('Finish Save or Cancel in Tag/Category Details before changing the catalog.')
            return False
        if self.restore_dialog is not None:
            self.controller.cancel()
            self.restore_dialog.invalidate_context()
            self.restore_dialog.close()
        from time import monotonic
        path = Path(path).absolute()
        self._cancelled = False
        self._before_view = self.service.view
        self._opening_source = self.accepted_source
        self._read_generation += 1
        self._opening_generation = self._read_generation
        self._export_open = (path, refreshing)
        self._export_started = monotonic()
        self.window.status.setText('Opening exported catalog. Cancel stops delivery after the current read finishes; previous view stays visible.')
        self._export_feedback()
        self.future = self.executor.submit(reader, path)
        self.window.bar.refresh_state()
        return True

    def _export_feedback(self):
        if self._export_open is None:
            return
        from time import monotonic
        self.window.database_activity.set_active(True, 'Opening exported catalog')
        self.window.database_activity.show()
        self.window.database_elapsed.setText(f'{max(0, int(monotonic() - self._export_started))}s elapsed')
        self.window.database_elapsed.show()
        self.window.cancel_work_button.setVisible(not self._closing)
        self.window._sync_start_choices()

    @property
    def accepted_native_adapter(self):
        """Only the original adapter exposed by successful matching delivery."""
        return self._accepted_native_adapter if not self.window.stale else None

    def prepare_delivery(self, *, native_root=None, export_path=None, directory_pin=None,native_adapter=None,publication=None):
        view = self._pending_read_view
        if (view is None and native_root is not None and native_adapter is None
                and self.accepted_source is not None and self.accepted_source.read_only):
            raise ValueError('Read-only native selection requires its staged core read binding')
        read_only = candidate_only = False
        read_binding = None
        if view is not None:
            if (self._closing or self._cancelled or self._opening_generation != self._read_generation
                    or self.accepted_source is not self._opening_source
                    or native_root is None or Path(native_root) != view.root
                    or directory_pin != view.stamp[:2] or publication is not view.publication
                    or native_adapter is not view.adapter):
                raise ValueError('Opening context changed before reader delivery')
            try:
                view.__post_init__()
                if view.read_only:self.service.validate_read_delivery(view,
                    should_cancel=lambda:self._closing or self._cancelled)
            except RuntimeError as error:
                raise ValueError(str(error)) from error
            read_only = view.read_only
            candidate_only = view.candidate_only
            read_binding = view.read_binding
        proposed=AcceptedCatalogSource("native",Path(native_root),directory_pin,native_adapter,
            publication if native_adapter is not None or read_only else None,
            read_binding,read_only,candidate_only) if native_root is not None else None
        if candidate_only:
            raise ValueError('Candidate read admission is held; production delivery is disabled')
        if read_only:
            self._prepare_read_only_delivery()
        if self.editing_bridge is not None and (native_root is None or Path(native_root)!=self.editor_session.root):
            if not self._retire_editor():raise ValueError('Finish the original editor Save/Cancel before changing catalog authority')
        if view is not None and (self._closing or self._cancelled
                or self._opening_generation != self._read_generation
                or self.accepted_source is not self._opening_source):
            raise ValueError('Opening context changed while retiring the original editor')
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

    def _prepare_read_only_delivery(self):
        if self.controller.close_held or self.package_future is not None:
            raise ValueError('Drain the original import controller before read-only delivery')
        if not self._retire_editor():
            raise ValueError('Finish the original editor Save/Cancel before read-only delivery')

    def accept_delivery(self, descriptor):
        """Commit only from the reader's successful immutable delivery boundary."""
        if type(descriptor) is not AcceptedCatalogSource:
            raise ValueError('Exact accepted source descriptor required')
        descriptor.__post_init__()
        if descriptor.read_only:
            view = self._pending_read_view
            if (view is None or descriptor.read_binding is not view.read_binding
                    or descriptor.publication is not view.publication or self._closing
                    or descriptor.candidate_only):
                raise ValueError('Retain the exact staged read-only delivery')
            self._clear_read_only_authorities()
        elif descriptor.native_adapter is not None and self.accepted_source is not None and self.accepted_source.read_only:
            # Only a separately admitted writable result restores the original controller.
            self.controller = TagDatabaseController(executor=self.executor,
                settings_path=self.service.settings_path,apply_authorized=True,owns_executor=False,
                before_baseline_change=self.invalidate_default_baseline)
        self.accepted_source = descriptor
        self._accepted_native_adapter=descriptor.native_adapter
        current_view = self.service.view
        self._accepted_service_view = current_view if (descriptor.kind == 'native'
            and current_view is not None and current_view.root == descriptor.path
            and current_view.publication is descriptor.publication) else None
        if descriptor.kind != "native":
            self.service.view = None
        self.window._set_commands()
        self.catalog_accepted.emit(descriptor)

    def _clear_read_only_authorities(self):
        """Called only after original editors and worker ownership have drained."""
        if self.editing_bridge is not None or self.controller.close_held or self.package_future is not None:
            raise ValueError('Drain original editors and import work before retiring writer authority')
        self.controller.begin_close()
        self.controller.apply_authorized = False
        self.controller.choices = ()
        self.controller.index = -1
        self.controller.review = self.controller.result = self.controller.connection_review = None
        self.controller._binding = None
        self.service.choice = self.service.import_review = self.service.connection_review = None
        self._accepted_native_adapter = None
        self.knowledge = None
        self._editor_refresh_needed = False
        self._editor_source = self._editor_publication = None
        self.prepared_default_review = None
        if self.dialog is not None:
            self.dialog.close()
            self.dialog.deleteLater()
            self.dialog = self.pane = None
        if self.restore_dialog is not None:
            self.restore_dialog.close()
            self.restore_dialog.deleteLater()
            self.restore_dialog = None
        self.reader_delivery_result = None
        self.reader_delivery_ok = False
        self.content_box = None
        self.content_status = None
        self.content_offers = self.content_refresh = self.content_download = None
        self.content_confirm = self.content_review = None
        self._content_discovery = None
        self._download_result = self._download_generation = None

    def show_completed_import(self):
        """History entry has no optional-content acquisition side effects."""
        if not self._write_actions_allowed() or self._closing or self.busy:
            return False
        if self.dialog is not None and self.dialog.isVisible() or self.controller.review is not None:
            self.window.status.setText('Finish or discard the optional Tag content review, then close it before reviewing a completed import.')
            return False
        if self.editing_bridge is not None and not self.editing_bridge.close_ready():
            self.window.status.setText('Finish Save or Cancel before reviewing a completed import.')
            return False
        if not self._retire_editor():
            return False
        if self.restore_dialog is None:
            from qt_app.completed_pack_restore_dialog import CompletedPackRestoreDialog
            self.restore_dialog = CompletedPackRestoreDialog(self)
        self.restore_dialog.render()
        self.restore_dialog.show(); self.restore_dialog.raise_()
        return True

    def show_defaults(self):
        if not self._write_actions_allowed():return False
        if self.restore_dialog is not None and self.restore_dialog.isVisible():
            self.restore_dialog.raise_()
            self.window.status.setText('Close the completed-import review before reviewing optional default tags. Its decisions are retained.')
            return False
        if self.editing_bridge is not None and not self.editing_bridge.close_ready():
            self.window.status.setText('Finish Save or Cancel in Tag/Category Details before reviewing imports.')
            return False
        if self._closing or self.busy:
            return False
        if not self._retire_editor():return False
        if self.optional_content_factory is not None and self.default_acquisition is None:
            # Opening the optional-content workspace is the deliberate opt-in.
            # Configuration creates only an owned cache; Refresh/Download remain explicit.
            factory, self.optional_content_factory = self.optional_content_factory, None
            try:
                enrollment = factory(self)
                required = {'service', 'observe_baseline', 'package_component',
                    'preparation_directory', 'channel'}
                if type(enrollment) is not dict or set(enrollment) != required:
                    raise ValueError('exact_optional_content_enrollment_required')
                self.configure_default_acquisition(**enrollment)
            except Exception as error:
                self.window.status.setText('Tag downloads are unavailable. Select an enrolled Tag Database and review local sources, or reopen after repairing the download configuration.')
                self.window.status.setToolTip(str(error))
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
            self.content_status = QLabel('Open a Tag Database without installed defaults, then refresh. Existing-content updates remain unavailable.')
            self.content_status.setWordWrap(True)
            content_layout.addWidget(self.content_status)
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
        if self._creation_cancel is not None:
            self._creation_cancel.set()
        self._read_generation += 1
        self._handoff_cancelled=True
        if self._handoff_waiting and self.organizer_receiver is not None:
            self.organizer_receiver.cancel_tracked(self._handoff_request_id,self._handoff_generation)
        if self.handoff_future is not None:self.handoff_future.cancel()
        if self.editor_future is not None:
            self.editor_future.cancel()
            self.jobs.cancel(self._editor_job.id)
        if self.future is not None:
            self.future.cancel()
        if self._export_open is not None and not self._closing:
            self.window.status.setText('Stopping exported catalog opening. Waiting for the current read to finish; previous view is retained.')
        self.controller.cancel()
        self.package_cancel.set()
        if self.package_future is not None:
            self.package_future.cancel()
        if self.default_acquisition is not None:
            self.default_acquisition.cancel()
        if self.help is not None:
            self.help.request_cancel()

    def poll(self):
        self.window.bar.refresh_state()
        self.window._sync_start_choices()
        self._export_feedback()
        if self._poll_creation():
            return
        if self._handoff_waiting and self.organizer_receiver is not None:
            self.organizer_receiver.poll_settlements()
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
                    self._handoff_scope=None
                    self.window.status.setText('Organizer handoff discarded after cancellation or catalog change. Choose a fresh Tag before retrying.')
                else:
                    self._handoff_waiting=True
                    receipt=self.organizer_receiver.receive_tracked_handoff(request,
                        prepared_scope=self._handoff_scope, source_generation=self._handoff_generation,
                        current_generation=lambda:self.baseline_generation if not self._closing and not self._handoff_cancelled else -1,
                        on_settled=self._organizer_handoff_settled)
                    if self._handoff_waiting:
                        self.window.status.setText('Organizer accepted the request and is waiting for Dataset. Search or correction review has not opened yet; no image was changed.')
                        self.window.bar.refresh_state()
            except Exception as error:
                self._handoff_waiting=False
                self._handoff_scope=None
                self.window.status.setText('This Organizer connection reached its session handoff limit. Close and reopen Organizer before sending more requests. No image was changed.'
                    if str(error)=='hosted_receipt_capacity_held' else
                    'Organizer handoff is held because its profile, included libraries or privacy scope changed. Review that scope, then send a new request. No image was changed.'
                    if str(error)=='recipient_profile_library_privacy_or_revision_changed' else
                    'Organizer handoff is held. Refresh the selected native catalog and Organizer Knowledge before retrying; no image was changed.')
                self.window.status.setToolTip(str(error)[:1024])
                self.window.bar.refresh_state()
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
        delivered = self.pane.poll() if self.pane is not None else self.controller.poll()
        if self.restore_dialog is not None and self.restore_dialog.isVisible():
            self.restore_dialog.render(delivered)
        if delivered:
            self.window.status.setText(self.controller.reason)
            self.window.status.setToolTip(self.controller.error or '')
        if self.default_acquisition is not None:
            before_download = self.default_acquisition.result
            self.default_acquisition.poll()
            if self.default_acquisition.result is not None and self.default_acquisition.result is not before_download:
                self._download_result = self.default_acquisition.result
                self._download_generation = self.baseline_generation
                self.window.status.setText('Tag data package downloaded. Review downloaded content before connecting or importing.')
            error = self.default_acquisition.error
            if error and error != self._content_error:
                if 'select_enrolled_native_catalog' in error:
                    self.window.status.setText('Open an enrolled Tag Database without installed defaults, then refresh content releases. No import was performed.')
                elif 'existing_default_content' in error or 'recovery_pending' in error:
                    self.window.status.setText('This catalog already has defaults or needs recovery. Existing-content updates are unavailable; review local sources or recovery.')
                else:
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
            self.content_refresh.setEnabled(ready and self.default_acquisition is not None)
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
                self.reader_delivery_error = ""
                try:
                    delivered = self.window.open_publication(publication, path=self.controller.choice.transactions.destination,
                                                 native_root=root, directory_pin=self.controller.choice.native_adapter.directory_pin,
                                                 native_adapter=self.controller.choice.native_adapter,
                                                 refreshing=self.window.publication is not None and
                                                 self.window.publication.catalog_id == publication.catalog_id)
                except Exception as error:
                    if self.restore_dialog is None or not self.restore_dialog.isVisible():
                        raise
                    delivered = False
                    self.reader_delivery_error = str(error)
                self.reader_delivery_result = result
                self.reader_delivery_ok = bool(delivered)
                self.reader_delivery_publication = publication
                if not delivered and getattr(result, "native_saved", False):
                    self.window.status.setText("Restoration saved; the Reader did not accept its result. Reload saved catalog; do not restore again.")
                if delivered:
                    if self.controller.error:
                        self.window.status.setText('Tags were saved and the reader refreshed, but host observations are held. Refresh observations before further review; do not apply again.')
                        self.window.status.setToolTip(self.controller.error)
                    else:self.window.status.setText("Tag Database saved and ready to browse.")
            elif getattr(result, "publication_error", ""):
                self.window.status.setText("Tags were saved. Reader refresh failed; choose Reload saved catalog in the review window. Do not import again.")
        if self.restore_dialog is not None and self.restore_dialog.isVisible():
            self.restore_dialog.render(delivered)
        if self.future is not None and self.future.done() and not self.executor.close_held:
            future, self.future = self.future, None
            export, self._export_open = self._export_open, None
            if export is not None:
                self.window.database_activity.set_active(False)
                self.window.database_activity.hide()
                self.window.database_elapsed.hide()
                self.window.cancel_work_button.hide()
            changed_source = self.accepted_source is not self._opening_source
            if self._closing:
                self.service.view = None
                self.window.status.setText('Opening discarded while Close is settling.')
            elif changed_source:
                self.service.view = self._accepted_service_view
                self.window.status.setText("Earlier opening discarded. The current catalog stays selected.")
            elif future.cancelled() or self._cancelled or self._opening_generation != self._read_generation:
                self.service.view = self._before_view
                self.window.status.setText("Opening cancelled. Previous database stays visible.")
            else:
                try:
                    view = future.result()
                    if export is not None:
                        path, refreshing = export
                        self.window.open_publication(view, path=path, refreshing=refreshing)
                        return
                    from core.encyclopedia_catalog import EncyclopediaCatalogView
                    if type(view) is not EncyclopediaCatalogView:
                        raise ValueError('Exact core catalog view required')
                    refreshing = self.window.publication is not None and self.window.publication.catalog_id == view.publication.catalog_id
                    self._pending_read_view = view
                    try:
                        delivered = self.window.open_publication(view.publication, native_root=view.root,
                            directory_pin=view.stamp[:2],native_adapter=view.adapter, refreshing=refreshing)
                    finally:
                        self._pending_read_view = None
                    if delivered:
                        if not view.publication.snapshot.tags:
                            self.window.status.setText('This empty database is open for browsing only; editing and imports are held.' if view.read_only else 'Your empty database is ready. Nothing has been imported.')
                        else:
                            self.window.status.setText('Tag Database opened for browsing only. Editing and imports are held.' if view.read_only else "Tag Database opened. Search or choose a category to browse.")
                    else:
                        if self._closing:self.service.view = None
                        elif self.accepted_source is self._opening_source:self.service.view = self._before_view
                except Exception as error:
                    if self._closing:self.service.view = None
                    elif self.accepted_source is self._opening_source:
                        self.service.view = self._before_view
                    if export is not None:
                        self.window._snapshot_failed(error)
                    else:
                        self.window.status.setText("Tag Database could not be opened. Previous view is retained. " + str(error))
        if self._closing and not self.busy:
            self.window.close()
        elif self._editor_refresh_needed and not self.busy and self.editing_bridge is not None and self.editing_bridge.close_ready():
            self._editor_refresh_needed=False
            self.refresh()

        if self.content_status is not None:
            self.content_status.setText(self.window.status.text())
            self.content_status.setToolTip(self.window.status.toolTip())

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
        if self.restore_dialog is not None:
            self.restore_dialog.close()
        self.timer.stop()
        return not self.executor.close_held

    def cancel_close(self):
        if not self.executor.closed and not self.entry_close_started:
            self._closing = False
            self.window.cancel_close_button.hide()
            self.window.status.setText("Close cancelled. Current database is retained.")
