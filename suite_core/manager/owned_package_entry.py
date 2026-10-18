"""Normal host package-selection caller and retained GUI failure supervision.

Default entry never selects a package. Frozen SDK hook ordering stays in its
existing entry; this caller runs only after normal-hook/path admission.
"""
from core.manager_ollama_runtime import ManagerOllamaRuntimeBinding
from core.manager_ollama_selection import SelectedOwnedPackage, select_owned_package, pinned_json
from .__main__ import admit_owned_runtime, main


_HELD_HOST_ENTRIES = []


class HeldManagerEntry:
    """Original exception/owner; host must supervise rather than exit with a hold."""
    def __init__(self,error,admission):
        self.error=error
        self.owner=error.manager_entry_owner
        self.admission=admission
        self.dialog=None;self.app=None
        _HELD_HOST_ENTRIES.append(self)

    @property
    def close_held(self):return not self.owner.closed

    def supervise(self):
        """Never return to frozen SystemExit while exact cleanup remains held."""
        import sys,time
        try:
            return self.run_recovery()
        except BaseException as failure:
            self.recovery_error=failure
            # Qt recovery itself failed: retain this host/owner instead of exiting.
            # No native action/restart is guessed; a separately retained host may
            # explicitly recover through this exact binding. Emit no paths/prompts.
            try:
                print('Manager recovery UI unavailable; process retained until original ownership is safely released.',file=sys.stderr)
            except BaseException as reporting_error:
                self.reporting_error=reporting_error
            while not self.owner.closed:
                try:
                    self.owner.poll_cleanup()
                except BaseException as cleanup_error:
                    self.cleanup_error=cleanup_error
                if self.owner.closed:break
                try:
                    time.sleep(.05)
                except BaseException as wait_error:
                    self.wait_error=wait_error
            return 1
        finally:
            if self.owner.closed and self in _HELD_HOST_ENTRIES:_HELD_HOST_ENTRIES.remove(self)

    def run_recovery(self):
        models = getattr(self.owner, 'models_workspace_host', None)
        if models is not None and models.close_held:
            from .model_workspace_host import supervise_models_recovery
            return supervise_models_recovery(self)

        if (getattr(self.owner, 'acquisition_panel', None) is not None and self.owner.acquisition_panel.close_held
                or getattr(self.owner, 'acquisition_executor', None) is not None and self.owner.acquisition_executor.close_held
                or getattr(self.owner, 'reader_owner', None) is not None and self.owner.reader_owner.close_held):
            from .encyclopedia_connection_ui import supervise_reader_recovery
            return supervise_reader_recovery(self)
        if self.owner.direct_model_host is not None and self.owner.direct_model_host.close_held:
            from .direct_model_ui import supervise_direct_model_recovery
            return supervise_direct_model_recovery(self)
        if self.owner.chat_host is not None and self.owner.chat_host.close_held:
            from .model_chat_binding import supervise_chat_recovery
            return supervise_chat_recovery(self)
        if getattr(self.owner, 'tag_controller', None) is not None:
            from .tag_entry_recovery import run_tag_recovery
            return run_tag_recovery(self)
        # Reuse actual runtime controls for explicit Stop/reconciliation. No implicit
        # Stop, retry of an action, PID adoption, or replacement of the original owner.
        from PySide6.QtCore import QTimer,Qt
        from PySide6.QtWidgets import QApplication,QDialog,QVBoxLayout,QLabel,QPushButton
        from .owned_runtime_ui import OwnedRuntimePanel
        app=QApplication.instance() or QApplication([])
        held=self
        class RecoveryDialog(QDialog):
            def closeEvent(self,event):
                event.ignore()
                held.close_requested=True
                try:
                    if held.admission.binding.close_held:return
                    original=held.owner.window
                    if original is not None and hasattr(original,'close') and original.close() is False:return
                    if not held.owner.poll_cleanup():return
                    event.accept()
                except BaseException as close_error:
                    held.close_error=close_error
            def reject(self):self.close()
        self.close_requested=False
        dialog=RecoveryDialog();dialog.setWindowTitle('CollectiGen needs attention');dialog.resize(660,360)
        layout=QVBoxLayout(dialog)
        label=QLabel('Manager could not finish starting. Review the background server below to finish cleanup.')
        label.setTextFormat(Qt.PlainText)
        label.setWordWrap(True);layout.addWidget(label)
        original=self.owner.window
        panel=None if original is None else getattr(original,'owned_runtime_panel',None)
        if panel is not None:
            if type(panel) is not OwnedRuntimePanel or panel.binding is not self.admission.binding:
                raise ValueError('exact_original_runtime_panel_required')
            # Transfer the sole existing polling/action owner into recovery. Its
            # pending ticket, review, timer and result admission remain the same.
            original.setEnabled(False)
            original.hide()
            panel.setParent(dialog)
            panel.setEnabled(True)
            panel.show()
            self.original_panel=panel
        else:
            panel=OwnedRuntimePanel(self.admission.binding,self.admission.descriptor,True,dialog)
        panel.enter_recovery(type(self.error).__name__+': '+str(self.error)[:256],finished=dialog.close)
        layout.addWidget(panel)
        close=QPushButton('Close');close.clicked.connect(dialog.close);layout.addWidget(close)
        close.setObjectName('managerRecoveryClose')
        def refresh():
            close.setEnabled(not self.admission.binding.close_held)
            if self.close_requested and not self.admission.binding.close_held:dialog.close()
        timer=QTimer(dialog);timer.setInterval(250);timer.timeout.connect(refresh);timer.start();refresh()
        session=getattr(self.owner,'normal_entry_session',None)
        if session is not None:
            from uuid import uuid4
            recovery_id=uuid4().hex
            def announce_visible():
                if dialog.isVisible():panel.recovery_claim=(session,recovery_id)
            QTimer.singleShot(0,announce_visible)
        # Keep both exact owner and dialog reachable if Qt recovery itself fails.
        self.dialog=dialog;self.app=app
        dialog.exec()
        if self.close_held:raise RuntimeError('manager_host_recovery_still_held')
        timer.stop();panel.timer.stop();dialog.deleteLater()
        self.dialog=None;self.app=None
        return 1


def launch_selected_package(selection, *, allowed, manager_arguments=(), settings_path=None,
                            diagnostics_root=None, normal_entry_session=None, tag_database_controller=None, tag_encyclopedia=None):
    if type(selection) is not SelectedOwnedPackage or allowed is not True:
        raise ValueError('explicit_qualified_package_permission_required')
    fresh=select_owned_package(selection.selection_path,selection.selection_pin,layout=selection.layout,
        system_root=dict(selection.environment)['SYSTEMROOT'])
    if fresh!=selection:raise ValueError('owned_package_selection_changed')
    def native_factory():
        from core.ollama_owned_windows import WindowsOwnedProcess
        native=WindowsOwnedProcess()
        if normal_entry_session is not None:
            import hashlib
            from core.ollama_owned_executor import OwnedOllamaExecutor
            from suite_core.discovery.component_staging import canonical
            reader=OwnedOllamaExecutor.__new__(OwnedOllamaExecutor)
            reader.layout=selection.layout;reader.plan=selection.plan
            pin=hashlib.sha256((selection.plan.source_payload/'ollama.exe').read_bytes()).hexdigest()
            lifecycle=hashlib.sha256(canonical({'publication':reader.plan_identity,
                'environment':dict(selection.environment),'program_sha256':pin})).hexdigest()
            context={'installation_id':selection.plan.registration.installation_id.value,
                'lifecycle_identity':lifecycle,
                'program':str((selection.plan.destination/'ollama.exe').resolve()),
                'publication_manifest_sha256':hashlib.sha256(selection.plan.manifest_bytes).hexdigest(),
                'prepared_inventory_sha256':selection.plan.prepared_inventory_sha256}
            # Pin prepared bytes before Install; owner rehashes published bytes on
            # the first reserve after publication/registration has completed.
            normal_entry_session.prepare_owned_helper_capability(native,context,pin)
        return native
    binding=ManagerOllamaRuntimeBinding(selection.layout,selection.plan,dict(selection.environment),
        configured_registry=selection.registry_reader.read,native_factory=native_factory,helper_capability=None)
    admission=admit_owned_runtime(selection.descriptor,selection.plan,dict(selection.environment),binding,allowed=True)
    try:
        return main(list(manager_arguments),settings_path=settings_path,
                    diagnostics_root=diagnostics_root,owned_runtime_admission=admission,
                    normal_entry_session=normal_entry_session,
                    **({} if tag_database_controller is None else dict(tag_database_controller=tag_database_controller,tag_encyclopedia=tag_encyclopedia)))
    except BaseException as error:
        owner=getattr(error,'manager_entry_owner',None)
        if owner is None or owner.closed:raise
        return HeldManagerEntry(error,admission)


def launch_selected_path(path,digest, *, layout,system_root,allowed,manager_arguments=(),
                         settings_path=None,diagnostics_root=None,normal_entry_session=None):
    selection=select_owned_package(path,digest,layout=layout,system_root=system_root)
    result=launch_selected_package(selection,allowed=allowed,manager_arguments=manager_arguments,
        settings_path=settings_path,diagnostics_root=diagnostics_root,normal_entry_session=normal_entry_session)
    if type(result) is HeldManagerEntry:
        return result.supervise()  # Normal launcher does not return/exit with an unclosed owner.
    return result
