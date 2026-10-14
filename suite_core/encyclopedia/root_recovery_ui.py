"""Existing acquisition recovery controls over one original held root."""
from dataclasses import dataclass


def persist_root_failure(hold,*,phase):
    """Existing local logs; frame coordinates only, never exception prose/data."""
    try:
        import json
        from hashlib import sha256
        from pathlib import Path
        from core.logging_setup import configure_logging
        from core.support_bundle import redact_diagnostic_text
        owner=getattr(hold,'owner',None) or getattr(getattr(hold,'backend',None),'owner',None)
        root=getattr(owner,'data_root',None)
        if root is None:return False
        codes={'control_channel_idle_timeout','control_channel_partial_frame',
            'control_channel_invalid_frame','normal_control_reader_exit',
            'native_reader_health_held','original_root_control_factory_changed',
            'original_reader_bootstrap_required','compiled_standalone_profile_unavailable',
            'original_control_ready_deadline','control_channel_eof'}
        errors=[];error=hold.error;seen=set()
        while error is not None and id(error) not in seen and len(errors)<16:
            seen.add(id(error));frames=[];tb=error.__traceback__
            while tb is not None and len(frames)<64:
                module=tb.tb_frame.f_globals.get('__name__','')
                internal=type(module) is str and (module.startswith(('core.','suite_core.','qt_app.','tools.'))
                    or module in ('core','encyclopedia_root_entry','encyclopedia_reader_entry'))
                frames.append({'module':module if internal else '<external>',
                    'function':tb.tb_frame.f_code.co_name if internal else '<external>',
                    'line':tb.tb_lineno})
                tb=tb.tb_next
            code=error.args[0] if len(error.args)==1 and type(error.args[0]) is str and error.args[0] in codes else 'not_recorded'
            errors.append({'type':type(error).__name__,'code':code,'frames':frames})
            error=error.__cause__ or (None if error.__suppress_context__ else error.__context__)
        transaction=getattr(owner,'record',{}).get('standalone_update',{}).get('phase')
        if transaction not in ('waiting_for_close','applying','recovering'):transaction='not_recorded'
        payload={'event':'encyclopedia_root_held','phase':phase,'transaction_phase':transaction,
            'control_retained':hold.control is not None,'errors':errors}
        text=redact_diagnostic_text(json.dumps(payload,separators=(',',':')),app_root=getattr(owner,'root',None))
        # Separate configured roots cannot write each other's retained handlers.
        logger_name='collectigen.encyclopedia.root.'+sha256(str(Path(root).resolve()).encode()).hexdigest()[:16]
        logger=configure_logging(root,name=logger_name)
        logger.error('%s',text)
        hold.diagnostic_recorded=any(not getattr(h,'failed',False) and hasattr(h,'baseFilename') for h in logger.handlers)
        return hold.diagnostic_recorded
    except Exception:
        hold.diagnostic_recorded=False
        return False  # Diagnostic failure cannot replace the original held error.


def _schedule_dialog_ready(dialog,panel,callback):
    """Optional caller-owned widget probe; errors retain normal settlement."""
    if callback is None:return
    if not callable(callback):raise TypeError('dialog_ready_callback_required')
    from PySide6.QtCore import QTimer
    dialog.dialog_ready_error=None
    def ready():
        try:callback(dialog)
        except BaseException as error:
            dialog.dialog_ready_error=error
            panel.status.setText('Dialog callback failed. Use the original controls to settle setup or recovery before closing.')
            panel.details.setPlainText(type(error).__name__+': '+str(error)[:1024])
    dialog.show()
    QTimer.singleShot(0,ready)


def create_original_enrollment_dialog(adapter,executor):
    """Original install panel and owner; no reader birth or health assertion."""
    from core.encyclopedia_enrollment import StandaloneEncyclopediaEnrollment
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    from suite_core.manager.encyclopedia_acquisition_ui import EncyclopediaAcquisitionPanel
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog,QVBoxLayout,QPushButton
    if type(adapter) is not StandaloneEncyclopediaEnrollment or type(executor) is not ResourceExecutorOwner:
        raise TypeError('original_enrollment_and_executor_required')
    if adapter.channel not in ('stable','beta'):raise ValueError('explicit_enrollment_channel_required')

    class EnrollmentPanel(EncyclopediaAcquisitionPanel):
        def retry_connection(self):
            # The stock result retains its pre-publication observation. Refresh
            # on the same worker after successful save/removal of the intent.
            def retry_and_observe():
                from dataclasses import replace
                result=self.service.retry_connection()
                return replace(result,installed=self.service.observe()) if result.connection_saved else result
            self._start('Saving installed connection',retry_and_observe)

        def refresh_releases(self):
            self.offer=self.reviewed=None
            self._start('Refreshing releases',lambda:(self.service.discover(channel=adapter.channel,
                should_cancel=self.cancel.is_set),self.service.observe()))

        def show_installed_details(self):
            selected=self.installed.selected
            self.details.setPlainText('No managed reader is active.' if selected is None else
                f'Installed reader {selected.version}\nDirectory: {selected.directory}'+
                '\nOpen the installed reader explicitly. The original root still verifies its actual startup and normal Close.')

        def render(self):
            super().render()
            self.retry_save.setVisible(self.publication_pending)
            pending=self.installed is not None and (self.installed.pending or self.installed.phase=='activated')
            self.recover.setVisible(pending)
            self.recovery_confirm.setVisible(pending)
            self.health_accept.setVisible(True)

    class EnrollmentDialog(QDialog):
        def __init__(self):
            super().__init__()
            self.adapter,self.executor=adapter,executor
            self.backend=None;self.handoff_future=self.cleanup_future=None
            self.close_requested=self.cleanup_entered=self.released=False
            self.setWindowTitle('Set up Tag Encyclopedia');self.resize(1000,740)
            self.panel=EnrollmentPanel(adapter.installation,executor,parent=self)
            self.panel.details.setPlainText('Review a signed reader package before installing. User data stays separate. After its exact connection is saved, choose Open installed Tag Encyclopedia; no reader starts automatically.')
            layout=QVBoxLayout(self);layout.addWidget(self.panel)
            self.open_reader=QPushButton('Open installed Tag Encyclopedia');self.open_reader.setEnabled(False)
            self.open_reader.clicked.connect(self.open_installed);layout.addWidget(self.open_reader)
            self.cancel_close=QPushButton('Cancel Close');self.cancel_close.hide()
            self.cancel_close.clicked.connect(self.resume_installation);layout.addWidget(self.cancel_close)
            self.close_button=QPushButton('Close setup');self.close_button.clicked.connect(self.close);layout.addWidget(self.close_button)
            self.timer=QTimer(self);self.timer.setInterval(40);self.timer.timeout.connect(self.poll);self.timer.start()

        def reject(self):self.close()
        def accept(self):self.close()
        def done(self,result):self.close()

        def ready_to_open(self):
            installed=self.panel.installed
            return bool(not self.cleanup_entered and not self.close_requested and self.handoff_future is None
                and self.cleanup_future is None and not self.panel.close_held and not executor.close_held
                and (self.backend is not None or installed is not None and installed.selected is not None
                    and not installed.pending and not installed.connection_pending and not self.panel.publication_pending))

        def open_installed(self):
            if not self.ready_to_open():return False
            if self.backend is not None:
                self.finish();return True
            self.panel.closing=True;self.panel.render();self.panel.timer.stop()
            self.handoff_future=executor.submit(adapter.handoff_reader)
            self.panel.status.setText('Finishing original installation handoff. No reader has started yet.')
            self.open_reader.setEnabled(False)
            return True

        def resume_installation(self):
            if self.cleanup_entered or self.handoff_future is not None:return False
            self.close_requested=False;self.cancel_close.hide()
            if self.backend is None:self.panel.resume_after_cancelled_close()
            self.panel.status.setText('Close cancelled. Original setup ownership is retained.')
            return True

        def finish(self):
            self.timer.stop();self.panel.timer.stop();executor.begin_close()
            QDialog.done(self,0)

        def closeEvent(self,event):
            event.ignore()
            if self.backend is not None:
                self.panel.status.setText('The original reader handoff is retained. Continue opening it for original health/recovery; setup cannot release this root.')
                return
            self.close_requested=True
            if self.handoff_future is not None:
                self.panel.status.setText('Close is waiting for the admitted handoff. The reader will remain unopened until you explicitly continue.')
                return
            if self.cleanup_future is not None:return
            if not self.panel.close_ready() or executor.close_held:
                self.cancel_close.setVisible(not self.cleanup_entered)
                self.panel.status.setText('Close is waiting for the original install worker to settle. Cancel Close to keep reviewing setup.')
                return
            self.cleanup_entered=True;self.cancel_close.hide()
            self.cleanup_future=executor.submit(adapter.close)
            self.panel.status.setText('Closing original setup ownership and custody. Keep this window open until it finishes.')

        def poll(self):
            self.panel.poll()
            if self.handoff_future is not None and self.handoff_future.done() and not executor.close_held:
                future,self.handoff_future=self.handoff_future,None
                try:
                    backend=future.result()
                    from .bootstrap_entry import StandaloneEncyclopediaBootstrap
                    if type(backend) is not StandaloneEncyclopediaBootstrap:raise TypeError('original_enrollment_backend_required')
                    self.backend=backend
                    if self.close_requested:
                        self.close_requested=False
                        self.panel.status.setText('Original handoff completed. Choose Open installed Tag Encyclopedia when ready; no reader was started after Close.')
                    else:self.finish();return
                except Exception as error:
                    self.panel.status.setText('Original handoff is held. Retain setup and its diagnostic; no replacement owner is created.')
                    self.panel.details.setPlainText(type(error).__name__+': '+str(error)[:1024])
            if self.cleanup_future is not None and self.cleanup_future.done() and not executor.close_held:
                future,self.cleanup_future=self.cleanup_future,None
                try:
                    terminal=future.result()
                    import re
                    if type(terminal) is not str or re.fullmatch('[0-9a-f]{64}',terminal) is None:
                        raise ValueError('original_enrollment_close_receipt_required')
                except Exception as error:
                    self.close_requested=False
                    from core.manager_launch_owner import LaunchHold
                    if (type(error) is LaunchHold
                            and len(error.args)==1 and error.args[0] in (
                                'enrollment_close_waits_for_original_settlement',
                                'enrollment_close_waits_for_durable_recovery',
                                'enrollment_close_waits_for_handoff_or_recovery',
                                'enrollment_close_waits_for_connection_recovery')
                            and adapter.owner._standalone_enrollment_finalization is None):
                        # This exact original-owner refusal precedes custody
                        # finalization. Restore choices; never undo begun cleanup.
                        self.cleanup_entered=False
                        self.panel.resume_after_cancelled_close()
                        self.panel.status.setText('Setup needs settlement before Close. Open the installed reader and close it normally, or confirm Recover installation. Original ownership is retained.')
                    else:
                        self.panel.status.setText('Original setup Close is held. Retry Close to finish the same retained cleanup.')
                    self.panel.details.setPlainText(type(error).__name__+': '+str(error)[:1024])
                else:self.released=True;self.finish();return
            if self.close_requested and self.backend is None and self.handoff_future is None and self.cleanup_future is None and not self.panel.close_held:
                self.close()
            self.open_reader.setEnabled(self.ready_to_open())

    return EnrollmentDialog()


def present_original_enrollment(adapter,executor,*,dialog_ready=None):
    if dialog_ready is not None and not callable(dialog_ready):raise TypeError('dialog_ready_callback_required')
    from PySide6.QtWidgets import QApplication
    app=QApplication.instance() or QApplication([])
    dialog=create_original_enrollment_dialog(adapter,executor)
    _schedule_dialog_ready(dialog,dialog.panel,dialog_ready)
    dialog.exec()
    if getattr(dialog,'dialog_ready_error',None) is not None:raise dialog.dialog_ready_error
    if dialog.backend is not None:return dialog.backend
    if not dialog.released:raise RuntimeError('original_enrollment_still_retained')
    return None


@dataclass(frozen=True)
class RootRecoveryStatus:
    connection_pending: bool
    reason: str


class OriginalRootRecoveryService:
    install_available = True  # Enables status, never installation in recovery-only mode.

    def __init__(self, hold):
        from .bootstrap_entry import StandaloneEncyclopediaBootstrap
        from core.manager_launch_owner import DeploymentOwner
        from .root_holds import OriginalDeploymentHold
        owner_only=type(hold) is OriginalDeploymentHold
        if owner_only:
            if type(hold.owner) is not DeploymentOwner:
                raise TypeError('Original failed DeploymentOwner required')
        elif type(getattr(hold,'backend',None)) is not StandaloneEncyclopediaBootstrap:
            raise TypeError('Original held standalone backend required')
        self.hold,self.backend=hold,None if owner_only else hold.backend
        self.original_owner=hold.owner if owner_only else self.backend.owner
        self.startup_only = owner_only or hold.control is None
        self.request = getattr(self.backend, 'request', None)
        self.can_retry = self.can_recover = self.can_close = self.can_run_reader = False
        self.recovery_entered = False
        self.reader_entered = False
        self.reader_attempt_control = None
        self.failure_details = type(hold.error).__name__ + ': ' + str(hold.error)[:512]

    def _owned(self):
        self.backend.owner._standalone_encyclopedia_owned(self.backend.subject)
        if self.backend.request is not self.request and not (
                self.backend.request is None and not self.backend.owner.record.get('standalone_update')):
            raise RuntimeError('Original recovery request changed; retain this root')

    def reconcile_launch(self):
        """Worker-only readback; never a health assertion or new reader launch."""
        if self.startup_only:
            return RootRecoveryStatus(False,
                'Startup stopped before the reader could be admitted. The original root remains supervised. Recovery and Close are unavailable here until qualified original-root cleanup completes. Retain the failure details.')
        self._owned()
        self.can_retry = self.can_recover = self.can_close = self.can_run_reader = False
        channel = self.hold.control.actor.channel
        if (not self.hold.control.close_intent or not self.hold.close_acknowledged
                or channel.read_fault is not None or getattr(channel, 'write_fault', None) is not None):
            return RootRecoveryStatus(False,
                'The original reader connection could not close safely. Recovery is unavailable here. Keep this window open and retain the failure details for qualified recovery.')
        phase = self.backend.owner.record.get('standalone_update', {}).get('phase')
        pending = False
        if self.backend.request is not None:
            from suite_core.discovery.component_staging import guard
            from core.encyclopedia_connection import COMPONENT
            path = self.backend.binding.root / 'State' / COMPONENT.value / 'connection-pending.json'
            guard(path)
            pending = path.exists()
            self.can_retry = pending and phase in ('applying', 'recovering')
            self.can_recover = phase == 'applying' and not self.recovery_entered
        self.can_close = not self.backend.close_held
        current = self.backend.binding.current()
        self.can_run_reader = (not pending and not self.can_close and not self.reader_entered
            and callable(getattr(self.hold, 'resume_original_reader', None))
            and (phase in ('applying', 'recovering') or
                 self.backend.request is None and current.phase == 'activated'))
        if self.can_close:
            reason = 'Original update and recovery are settled. You can close Tag Encyclopedia.'
        elif pending:
            reason = 'The package transaction is retained, but its connection was not saved. Choose Retry save connection; do not install again.'
        elif self.can_run_reader:
            reason = 'The saved reader needs an ordinary run to finish setup. Choose Run reader to finish setup, then close that reader normally. This window retains the update until original health is confirmed.'
        elif self.recovery_entered or self.reader_entered:
            reason = 'Recovery was requested. The original reader health and final settlement still need qualified completion. Keep this window open.'
        else:
            reason = 'The original update is held. Review the details, or confirm Recover installation when available. Reader health is still required before closing.'
        return RootRecoveryStatus(pending, reason)

    def retry_connection(self):
        self.reconcile_launch()
        if not self.can_retry:
            raise RuntimeError('Original pending connection publication required')
        self.backend.retry_connection(self.request)
        return self.reconcile_launch()

    def recover(self, *, confirmed=False):
        self.reconcile_launch()
        if confirmed is not True or not self.can_recover:
            raise RuntimeError('Explicit original recovery is unavailable')
        self.recovery_entered = True  # Ambiguous failure never triggers replay.
        self.can_recover = False
        self.backend.recover_update(self.request, confirmed=True)
        # A successfully recovered selection is a distinct permitted attempt,
        # not a retry of an ambiguous reader birth. The original backend's
        # recovery validates physical drain; require the same positive prior
        # control receipt and independently recheck that original drain.
        if (self.reader_entered and self.reader_attempt_control is self.hold.control
                and self.hold.close_acknowledged and self.backend.poll_reader()
                and self.backend.owner.record.get('standalone_update', {}).get('phase') == 'recovering'):
            self.reader_entered = False
            self.reader_attempt_control = None
        return self.reconcile_launch()

    def run_reader(self):
        self.reconcile_launch()
        if not self.can_run_reader:
            raise RuntimeError('Original saved reader continuation is unavailable')
        self.reader_entered = True  # A failed/ambiguous birth is never replayed.
        self.reader_attempt_control = self.hold.control
        self.can_run_reader = False
        try:
            self.hold.resume_original_reader()
        finally:
            self.reader_attempt_control = self.hold.control
        return self.reconcile_launch()


def present_original_root_hold(hold,*,dialog_ready=None):
    """Original root main-thread presentation; one original recovery worker."""
    if dialog_ready is not None and not callable(dialog_ready):raise TypeError('dialog_ready_callback_required')
    if not getattr(hold,'diagnostic_recorded',False):persist_root_failure(hold,phase='recovery_presentation')
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QApplication, QDialog, QVBoxLayout, QPushButton
    from suite_core.manager.encyclopedia_acquisition_ui import EncyclopediaAcquisitionPanel
    service = OriginalRootRecoveryService(hold)
    app = QApplication.instance() or QApplication([])

    class RecoveryDialog(QDialog):
        def reject(self):
            self.close()  # Escape uses the same original-owner Close guard.

        def accept(self):
            self.close()

        def done(self, result):
            if hold.released.is_set():
                QDialog.done(self, result)
            else:
                self.close()

        def closeEvent(self, event):
            if panel.close_held or not service.can_close:
                event.ignore()
                panel.status.setText('Close is waiting for original recovery and worker drain. Check app status for the next available action.')
                return
            try:
                hold.release_after_recovery(confirmed=True)
            except Exception as error:
                service.can_close = False
                panel.status.setText('Original Close remains held. Check app status and retain the details.')
                panel.details.setPlainText(type(error).__name__ + ': ' + str(error)[:512])
                event.ignore()
                return
            event.accept()
            QDialog.done(self, 0)

    dialog = RecoveryDialog()
    dialog.setWindowTitle('Tag Encyclopedia needs attention')
    dialog.resize(1000, 740)
    layout = QVBoxLayout(dialog)
    panel = EncyclopediaAcquisitionPanel(service, hold.executor, parent=dialog, recovery_only=True)
    layout.addWidget(panel)
    close = QPushButton('Close Tag Encyclopedia'); close.setObjectName('encyclopediaRootRecoveryClose')
    close.setEnabled(False); close.clicked.connect(dialog.close); layout.addWidget(close)
    timer = QTimer(dialog); timer.setInterval(100)
    timer.timeout.connect(lambda: close.setEnabled(service.can_close and not panel.close_held))
    timer.start()
    hold.dialog, hold.panel, hold.app = dialog, panel, app
    QTimer.singleShot(0, panel.reconcile_launch)
    _schedule_dialog_ready(dialog,panel,dialog_ready)
    dialog.exec()
    result=0
    if not hold.released.is_set():
        result=hold.wait()
    if hold.released.is_set():
        timer.stop(); panel.timer.stop()
        hold.dialog = hold.panel = hold.app = None
    if getattr(dialog,'dialog_ready_error',None) is not None:raise dialog.dialog_ready_error
    return result
