"""Existing acquisition recovery controls over one original held root."""
from dataclasses import dataclass


def preowner_failure_diagnostic(error):
    """Bounded stderr evidence; no exception prose, local variables or owner fiction."""
    import json,re
    from core.support_bundle import redact_diagnostic_text
    codes={'empty_owned_cache_required','qualification_cache_parent_required'}
    code=error.args[0] if len(error.args)==1 and type(error.args[0]) is str and error.args[0] in codes else 'not_recorded'
    errors=[];seen=set();current=error
    while current is not None and id(current) not in seen and len(errors)<4:
        seen.add(id(current));name=type(current).__name__
        name=name if re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,63}',name) else '<unknown>'
        frames=[];tb=current.__traceback__
        while tb is not None and len(frames)<4:
            module=tb.tb_frame.f_globals.get('__name__','');function=tb.tb_frame.f_code.co_name
            internal=(type(module) is str and len(module)<=128 and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9.]*',module)
                and (module.startswith(('core.','suite_core.','qt_app.','tools.'))
                    or module in ('encyclopedia_root_entry','encyclopedia_fixture_profile')))
            frames.append(dict(module=module if internal else '<external>',
                function=function if internal and re.fullmatch(r'[A-Za-z_][A-Za-z_0-9]{0,63}',function) else '<external>',
                line=tb.tb_lineno));tb=tb.tb_next
        errors.append(dict(type=name,frames=frames))
        current=current.__cause__ or (None if current.__suppress_context__ else current.__context__)
    payload=dict(schema=1,event='encyclopedia_preowner_startup_failed',phase='profile_initialization',
        status='failed',code=code,errors=errors)
    text=redact_diagnostic_text(json.dumps(payload,separators=(',',':'),ensure_ascii=True))
    if len(text.encode('utf-8'))>4096:
        payload['errors']=[];text=json.dumps(payload,separators=(',',':'))
    return text


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
    from suite_core.manager.encyclopedia_acquisition_ui import EncyclopediaAcquisitionPanel,_primary_action
    from PySide6.QtCore import QTimer
    from PySide6.QtWidgets import QDialog,QVBoxLayout,QPushButton
    from suite_core.manager.ui_cards import compact_action
    if type(adapter) is not StandaloneEncyclopediaEnrollment or type(executor) is not ResourceExecutorOwner:
        raise TypeError('original_enrollment_and_executor_required')
    if adapter.channel not in ('stable','beta'):raise ValueError('explicit_enrollment_channel_required')

    class EnrollmentPanel(EncyclopediaAcquisitionPanel):
        def __init__(self, *args, **kwargs):
            super().__init__(*args, **kwargs)
            from qt_app.components import IndeterminateActivityBar
            from PySide6.QtWidgets import QProgressBar, QLabel
            self.indeterminate = IndeterminateActivityBar(self)
            # Reuse the shared Manager owner's measured/elapsed widgets when present.
            # Older accepted panels have neither; this host supplies the same one area.
            shared_activity = getattr(self, 'activity', None)
            if isinstance(shared_activity, QProgressBar):
                self.measured = shared_activity
                self.layout().insertWidget(self.layout().indexOf(shared_activity), self.indeterminate)
            else:
                self.measured = QProgressBar(self)
                self.elapsed = QLabel(self)
                self.elapsed.setWordWrap(True)
                for offset, widget in enumerate((self.indeterminate, self.measured, self.elapsed)):
                    self.layout().insertWidget(3 + offset, widget)
            for widget in (self.indeterminate, self.measured, self.elapsed):
                widget.hide()
            self._activity_started = None
            self._activity_phase = ''
            self._activity_bytes = None
            self._external_activity = False
            self._purpose_label = self.layout().itemAt(1).widget()
            self._failure_details = None
            self._refresh_failure = None
            if getattr(self.service, 'local_reader', None) is not None:
                QTimer.singleShot(0, self.refresh_releases)
            self.render()

        def begin_activity(self, label, *, external=False):
            from time import monotonic
            self._activity_started = monotonic()
            self._activity_phase = label
            self._activity_bytes = None
            self._external_activity = external
            self._sync_activity()

        def end_external_activity(self):
            self._external_activity = False
            self._sync_activity()

        def _sync_activity(self):
            if not hasattr(self, '_activity_started'):
                return
            from time import monotonic
            active = self.future is not None or self._external_activity
            self.elapsed.setVisible(active)
            measured = active and self._activity_bytes is not None
            self.measured.setVisible(measured)
            self.indeterminate.setVisible(active and not measured)
            self.indeterminate.set_active(active and not measured, self._activity_phase)
            if active:
                seconds = max(0, int(monotonic() - self._activity_started)) if self._activity_started is not None else 0
                self.elapsed.setText(f'{seconds}s elapsed')
                if measured:
                    copied, total = self._activity_bytes
                    percent = round(copied * 100 / total)
                    self.measured.setRange(0, 1000)
                    self.measured.setValue(round(copied * 1000 / total))
                    self.measured.setFormat(f'{percent}%')
                    self.elapsed.setText(f'{copied / 1000000:.1f} MB of {total / 1000000:.1f} MB · {seconds}s elapsed')

        def _start(self, operation, work):
            if self.close_held or self.closing:
                return
            label = {'Refreshing releases': 'Checking the app package' if getattr(self.service, 'local_reader', None) is not None else 'Looking for the reader app',
                'Reviewing installation': 'Preparing your installation review',
                'Installing reviewed package': 'Preparing the reader app',
                'Checking app status': 'Checking installation status',
                'Recovering installation': 'Recovering the retained installation',
                'Saving installed connection': 'Saving the app connection'}.get(operation, operation)
            self.begin_activity(label)
            super()._start(operation, work)
            if self.future is not None:
                self.status.setText(label + '. You can cancel while it is safe to stop.')
            self._sync_activity()

        def poll(self):
            from queue import Empty
            while True:
                try:
                    values = self.progress.get_nowait()
                except Empty:
                    break
                if self.cancel.is_set():
                    continue
                if len(values) == 2 and values[0] == 'phase':
                    self._activity_phase = values[1]
                    self._activity_bytes = None
                elif len(values) == 4 and values[0] == 'bytes' and type(values[2]) is int and type(values[3]) is int and 0 <= values[2] <= values[3] and values[3] > 0:
                    self._activity_phase = values[1]
                    self._activity_bytes = (values[2], values[3])
                elif len(values) >= 2 and type(values[0]) is int and type(values[1]) is int and 0 <= values[0] <= values[1] and values[1] > 0:
                    self._activity_phase = 'Downloading the reader app'
                    self._activity_bytes = (values[0], values[1])
                if self.future is not None:
                    self.status.setText(self._activity_phase + '. Installation is not finished yet.')
            future, operation = self.future, self.operation
            error = future.exception() if future is not None and future.done() else None
            super().poll()
            if future is not None and future.done() and error is not None:
                cancelled = isinstance(error, InterruptedError)
                self._failure_details = self.details.toPlainText()
                self.status.setText(('Check cancelled. Choose Check package when you are ready.' if cancelled else
                    'The package check could not finish. Choose Check package to retry; Details explains the error.')
                    if operation == 'Refreshing releases' else
                    'Installation stopped. Checking whether it needs recovery.' if operation == 'Installing reviewed package' else
                    'This step could not finish. Check the package again or use the displayed recovery action. Details explains the error.')
                if operation == 'Installing reviewed package' and not self.closing:
                    self._refresh_failure = cancelled
                    self._start('Checking app status', self.service.reconcile_launch)
            elif future is not None and future.done() and operation == 'Checking app status' and self._refresh_failure is not None:
                if self.installed is not None and (self.installed.pending or self.installed.connection_pending):
                    self.status.setText('Setup was interrupted. Use Recover installation or Retry Save below; your retained files stay available.')
                else:
                    self.status.setText('Installation was cancelled. Check the package again when ready.' if self._refresh_failure else
                        'Installation could not finish. Check the package again to retry. Details explains the error.')
                if self._failure_details:
                    self.details.appendPlainText('\n' + self._failure_details)
                self._refresh_failure = None
            elif future is not None and future.done() and operation == 'Refreshing releases' and self.installed is not None and self.installed.pending:
                self.status.setText('Setup needs recovery. Choose Recover installation below before installing again.')
            self._sync_activity()

        def scope_text(self):
            return 'Tag Encyclopedia lets you browse and search tag databases. First, install its reader app. Your databases stay separate, and the app opens only when you choose Open.'

        def initial_status_text(self):
            return ('Checking your selected app package. Nothing will be installed until you confirm.'
                if getattr(self.service, 'local_reader', None) is not None else 'Choose Check for app to find the reader. Nothing will be installed until you confirm.')

        def observation_text(self):
            selected=None if self.installed is None else self.installed.selected
            return ('The reader app is not set up yet.' if self.installed is None else
                'Tag Encyclopedia is not installed yet.' if selected is None else
                'Installed version: '+selected.version+(' — open the app and close it normally to finish setup.'
                    if self.installed.phase=='activated' else ''))

        def available_version_label(self):
            return 'Selected local version: ' if getattr(self.service, 'local_reader', None) is not None else 'Available version: '

        def available_status_text(self,offer):
            action='Install' if self.installed.selected is None else 'Update'
            return ('Your selected app package is ready for review. Choose Install.'
                if getattr(self.service, 'local_reader', None) is not None else
                f'The reader app is available. Choose {action} to review it.')

        def empty_release_status_text(self):
            return ('No available version was found on this channel. Check Again later.' if self.installed.selected is None else
                'No newer version was found on this channel.')

        def pending_setup_guidance(self):
            return 'Tag Encyclopedia is installed. Choose Open to start using it; close the app normally when you are done.'

        def installation_status_reason(self,result):
            installed=result.installed
            if (result.connection_saved and installed.selected is not None
                    and installed.phase=='activated' and not installed.pending
                    and not installed.connection_pending):
                return 'Tag Encyclopedia is installed. Choose Open to start using it; close the app normally when you are done.'
            if not result.connection_saved:
                return 'The app files are installed, but setup could not be saved. Choose Finish setup to retry.'
            return super().installation_status_reason(result)

        def confirm_installation(self, reviewed):
            from PySide6.QtWidgets import QMessageBox
            from PySide6.QtCore import Qt
            offer = reviewed.offer
            local = getattr(self.service, 'local_reader', None)
            box = QMessageBox(self)
            box.setWindowTitle('Install Tag Encyclopedia')
            box.setIcon(QMessageBox.Question)
            box.setTextFormat(Qt.PlainText)
            box.setText('Install the Tag Encyclopedia reader app?')
            source = 'The selected local package will be copied and checked.' if local is not None else 'The app package will be downloaded and checked.'
            box.setInformativeText(f'This installs the app used to browse and search tag databases. Your databases stay separate.\n\nVersion {offer.release.software_version} · {offer.release.artifact.size_bytes / 1000000:.1f} MB\n{source} Compatibility is checked before installation.\n\nChoose Open after installation when you are ready to use the app.')
            box.setDetailedText(f'Destination: {reviewed.destination}\nPublisher key: {offer.release.key_id}\nPackage SHA256: {offer.release.artifact.sha256}' +
                (f'\nSource: {local.inputs.package}' if local is not None else ''))
            box.setStandardButtons(QMessageBox.Yes | QMessageBox.No)
            box.setDefaultButton(QMessageBox.No)
            box.button(QMessageBox.Yes).setText('Install')
            box.button(QMessageBox.No).setText('Cancel')
            return box.exec() == QMessageBox.Yes

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
            self.observation_stale=self.installed is not None
            def check_package():
                installed = self.service.observe()
                if installed.pending or installed.connection_pending:
                    return (), installed
                return self.service.discover(channel=adapter.channel, should_cancel=self.cancel.is_set), self.service.observe()
            self._start('Refreshing releases', check_package)

        def show_installed_details(self):
            selected=self.installed.selected
            self.details.setPlainText('No managed reader is active.' if selected is None else
                f'Installed reader {selected.version}\nDirectory: {selected.directory}'+
                '\nOpen the installed reader explicitly. The original root still verifies its actual startup and normal Close.')

        def render(self):
            super().render()
            self.refresh.setText("Check package" if getattr(self.service, "local_reader", None) is not None else "Check for app")
            self.stop.setText('Cancel check' if self.operation == 'Refreshing releases' else 'Cancel installation' if self.operation == 'Installing reviewed package' else 'Cancel review')
            self.stop.setToolTip('Stop at the next safe boundary. Retained installation files are preserved if recovery is needed.')
            self.destination.hide()
            if self.installed is not None and self.installed.selected is not None and self.offer is None:
                self.version_summary.setText("Installed version: " + self.installed.selected.version)
            if hasattr(self, '_purpose_label') and self.installed is not None:
                if self.installed.selected is not None:
                    self._purpose_label.setText('Tag Encyclopedia lets you browse and search tag databases. Your databases stay separate.')
                if self.installed.pending and self.future is None and not self.closing:
                    self.status.setText('Setup was interrupted. Select the recovery confirmation below, then choose Recover installation to undo it. Your databases are kept.')
            self.refresh.setVisible(self.refresh.isVisible() and self.future is None and (self.installed is None or self.installed.selected is None))
            self.retry_save.setText('Finish setup')
            self.recovery_confirm.setText('Undo the interrupted installation and keep my databases.')
            _primary_action(self.retry_save, self.retry_save.isVisible())
            _primary_action(self.recover, self.recover.isVisible() and self.recovery_confirm.isChecked())
            self.install.setDefault(self.install.isVisible() and self.install.isEnabled())
            self._sync_activity()
            self.retry_save.setVisible(self.publication_pending and not self.close_held)
            pending=self.installed is not None and self.installed.pending
            recovery_visible=pending or self.installed is not None and self.installed.selected is not None and self.technical.isChecked()
            self.recover.setVisible(recovery_visible and not self.close_held)
            self.recovery_confirm.setVisible(recovery_visible and not self.close_held)
            self.health_accept.setVisible(not self.close_held and self.installed is not None and
                (self.installed.pending or self.installed.selected is not None and self.technical.isChecked()))

    class EnrollmentDialog(QDialog):
        def __init__(self):
            super().__init__()
            self.adapter,self.executor=adapter,executor
            self.backend=None;self.handoff_future=self.cleanup_future=None
            self.close_requested=self.cleanup_entered=self.released=False
            self.setWindowTitle('Tag Encyclopedia')
            self.panel=EnrollmentPanel(adapter.installation,executor,parent=self)
            self.panel.details.setPlainText('Review a signed reader package before installing. User data stays separate. After its exact connection is saved, choose Open installed Tag Encyclopedia; no reader starts automatically.')
            layout=QVBoxLayout(self);layout.addWidget(self.panel)
            self.open_reader=compact_action(QPushButton('Open'));self.open_reader.setEnabled(False);self.open_reader.hide()
            self.open_reader.clicked.connect(self.open_installed);self.panel.footer.insertWidget(0,self.open_reader)
            self.cancel_close=compact_action(QPushButton('Cancel Close'));self.cancel_close.hide()
            self.cancel_close.clicked.connect(self.resume_installation);self.panel.footer.addWidget(self.cancel_close)
            self.close_button=compact_action(QPushButton('Close'));self.close_button.clicked.connect(self.close);self.panel.footer.addWidget(self.close_button)
            self.panel.notes_toggle.toggled.connect(self.fit_contents)
            self.panel.technical.toggled.connect(self.fit_contents)
            self.timer=QTimer(self);self.timer.setInterval(40);self.timer.timeout.connect(self.poll);self.timer.start()
            self.fit_contents()

        def fit_contents(self):
            """Qt logical work-area sizing; leave user enlargement and text scrolling available."""
            from PySide6.QtCore import QSize
            area=self.screen().availableGeometry().size()-QSize(24,64)
            self.setMaximumSize(area)
            self.setMinimumWidth(min(560,area.width()))
            self.layout().activate()
            wanted=self.sizeHint().expandedTo(self.minimumSizeHint())
            width=min(max(560,wanted.width()),680,area.width())
            height=min(wanted.height(),area.height())
            if not self.isVisible():
                self.resize(width,height)
            else:
                self.resize(max(self.width(),width),max(self.height(),height))

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
            self.panel.begin_activity('Opening Tag Encyclopedia', external=True)
            self.handoff_future=executor.submit(adapter.handoff_reader)
            self.panel.status.setText('Preparing to open the reader. It has not started yet.')
            self.open_reader.setEnabled(False)
            return True

        def resume_installation(self):
            if self.cleanup_entered or self.handoff_future is not None:return False
            self.close_requested=False;self.cancel_close.hide()
            if self.backend is None:self.panel.resume_after_cancelled_close()
            self.panel.status.setText('Close cancelled. Setup stays open.')
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
                self.panel.status.setText('Close is waiting for setup to finish. The reader will stay closed.')
                return
            if self.cleanup_future is not None:return
            if not self.panel.close_ready() or executor.close_held:
                self.cancel_close.setVisible(not self.cleanup_entered)
                self.panel.status.setText('Close is waiting for installation to finish. Choose Cancel Close to return to setup.')
                return
            self.cleanup_entered=True;self.cancel_close.hide()
            self.panel.begin_activity('Closing setup safely', external=True)
            self.cleanup_future=executor.submit(adapter.close)
            self.panel.status.setText('Closing setup. Keep this window open until it finishes.')

        def poll(self):
            self.panel.poll()
            if self.handoff_future is not None and self.handoff_future.done() and not executor.close_held:
                future,self.handoff_future=self.handoff_future,None
                self.panel.end_external_activity()
                try:
                    backend=future.result()
                    from .bootstrap_entry import StandaloneEncyclopediaBootstrap
                    if type(backend) is not StandaloneEncyclopediaBootstrap:raise TypeError('original_enrollment_backend_required')
                    self.backend=backend
                    if self.close_requested:
                        self.close_requested=False
                        self.panel.status.setText('Ready. Choose Open when you want to start the reader.')
                    else:self.finish();return
                except Exception as error:
                    self.panel.status.setText('The reader could not open. Keep setup open and check Technical Details.')
                    self.panel.details.setPlainText(type(error).__name__+': '+str(error)[:1024])
            if self.cleanup_future is not None and self.cleanup_future.done() and not executor.close_held:
                future,self.cleanup_future=self.cleanup_future,None
                self.panel.end_external_activity()
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
                        self.panel.status.setText('Open the reader and close it normally to finish setup before closing this window, or use Recover installation.')
                    else:
                        self.panel.status.setText('Setup could not close yet. Retry Close; the reader and its data are preserved.')
                    self.panel.details.setPlainText(type(error).__name__+': '+str(error)[:1024])
                else:self.released=True;self.finish();return
            if self.close_requested and self.backend is None and self.handoff_future is None and self.cleanup_future is None and not self.panel.close_held:
                self.close()
            self.open_reader.setEnabled(self.ready_to_open())
            self.open_reader.setDefault(self.ready_to_open())
            self.open_reader.setVisible(self.backend is not None or self.panel.installed is not None and self.panel.installed.selected is not None)
            _primary_action(self.open_reader,self.ready_to_open() and not self.panel.install.isVisible()
                and not self.panel.retry_save.isVisible())

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
