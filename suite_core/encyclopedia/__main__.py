"""Developer entry point: python -B -m suite_core.encyclopedia [--snapshot FILE]."""
import argparse
from pathlib import Path
from .root_holds import OriginalRootHold,OriginalDeploymentHold

_HELD_ENTRIES = []


def report_preowner_startup_failure(error,*,root,present=None):
    """Controlled qualification profile refusal; never a normal owner/Close receipt."""
    import sys
    from suite_core.discovery.component_staging import guard,require
    root=Path(root).absolute()
    require(getattr(sys,'frozen',False) and Path(sys.executable).absolute()==root/'TagEncyclopedia.exe',
        'fixed_preowner_failure_layout_required')
    guard(root);guard(root/'Data');guard(root/'Data/Manager/LaunchOwner/owner.json')
    require(not (root/'Data/Manager/LaunchOwner/owner.json').exists()
        and getattr(error,'encyclopedia_root_owner',None) is None,'preowner_failure_has_no_owner_required')
    from .root_recovery_ui import preowner_failure_diagnostic
    # No local log writer can be admitted before an authentic owner exists.
    # Stderr is already captured by the original creator; no state is created here.
    try:
        if sys.stderr is not None:print(preowner_failure_diagnostic(error),file=sys.stderr,flush=True)
    except Exception:pass
    title='Tag Encyclopedia could not start'
    message=('Startup stopped before a reader was admitted. Close this message and report the startup diagnostic. '
        'Do not delete the installed reader, settings or update cache to retry.')
    try:
        if present is not None:present(title,message)
        else:
            from PySide6.QtWidgets import QApplication,QMessageBox
            app=QApplication.instance() or QApplication([])
            QMessageBox.critical(None,title,message,QMessageBox.StandardButton.Ok)
    except Exception:pass  # Diagnostic/UI refusal still exits failed, without a bootloader traceback.
    return 2


def _present_root_failure(hold,*,phase):
    from .root_recovery_ui import persist_root_failure
    persist_root_failure(hold,phase=phase)
    return hold.present()


def _retained_startup_control(error,backend):
    from .control_entry import EncyclopediaRootControl
    control=getattr(error,'encyclopedia_root_control',None)
    if control is not None and type(control) is EncyclopediaRootControl and control.backend is backend:
        return control
    return None  # Unqualified evidence stays on the original error, never acted on.


def create_window(*, settings_path, update_check=None, executor=None, entry_control=None,
        default_content_factory=None):
    """Compose one standalone owner; importing the module remains inert."""
    from .browser import EncyclopediaWindow
    from .catalog_ui import EncyclopediaCatalogHost
    from core.encyclopedia_help_content import load_encyclopedia_help
    from qt_app.encyclopedia_help_commands import EncyclopediaHelpCommands
    from PySide6.QtWidgets import QMessageBox
    if entry_control is not None:
        if update_check is not None:
            raise ValueError('one_original_update_check_owner_required')
        from core.encyclopedia_entry_adapter import EncyclopediaEntryUpdateCheck
        update_check = EncyclopediaEntryUpdateCheck(entry_control)
    if default_content_factory is not None and not callable(default_content_factory):
        raise TypeError('explicit_default_content_factory_required')
    window = EncyclopediaWindow()
    host = EncyclopediaCatalogHost(window, settings_path=settings_path, executor=executor)
    if entry_control is not None:
        host.entry_check = update_check
    window.catalog_host = host
    if default_content_factory is not None:
        try:
            enrollment = default_content_factory(host)
            required = {'service', 'observe_baseline', 'package_component',
                'preparation_directory', 'channel'}
            if type(enrollment) is not dict or set(enrollment) != required:
                raise ValueError('exact_default_content_enrollment_required')
            host.configure_default_acquisition(**enrollment)
        except Exception:
            if not window.close():
                _HELD_ENTRIES.append(window)
                window.show()
            raise
    component = Path(__file__).resolve().parent
    notes_path = component / "WHATS_NEW.md"
    public_notes = ""
    if notes_path.is_file() and notes_path.stat().st_size <= 256 * 1024:
        # The current section is editorially reviewed public prose; the older
        # developer archive is not projected into the beginner Help surface.
        public_notes = notes_path.read_text(encoding="utf-8").split("\n# ", 1)[0].strip()
    content = load_encyclopedia_help(component, public_notes=public_notes)
    def notes(value):
        history = "\n".join(entry.label for entry in value.history)
        QMessageBox.information(window, "What's New", value.notes_text + "\n\nDated version history\n" + history)
    def about(value):
        QMessageBox.information(window, "About Tag Encyclopedia", value)
    def update(text, notice):
        window.status.setText(text)
        host.accept_entry_notice(notice)
    helper = EncyclopediaHelpCommands(window, content, present_notes=notes,
        present_about=about, present_update=update, update_check=update_check, executor=host.executor)
    host.help = helper
    window.help_commands = helper
    window._set_commands()  # One bar; never call helper.create_bar().
    return window


def main(argv=None, *, settings_path=None, update_check=None, executor=None, entry_probe=None,
        entry_control=None, default_content_factory=None):
    parser = argparse.ArgumentParser(description="Tag Encyclopedia: browse and review an explicitly selected database")
    parser.add_argument("--snapshot", help="Explicit approved published snapshot")
    parser.add_argument("--theme", choices=("system", "light", "dark"), default="system")
    arguments = parser.parse_args(argv)
    from PySide6.QtWidgets import QApplication
    from PySide6.QtCore import QStandardPaths
    from qt_app.theme import apply_theme
    app = QApplication.instance() or QApplication([])
    apply_theme(app, arguments.theme, False)
    if settings_path is None:
        settings_path = Path(QStandardPaths.writableLocation(QStandardPaths.AppLocalDataLocation)) / "CollectiGenEncyclopedia/settings.json"
    window = create_window(settings_path=Path(settings_path), update_check=update_check,
        executor=executor, entry_control=entry_control, default_content_factory=default_content_factory)
    try:
        if arguments.snapshot:
            window.open_snapshot(arguments.snapshot)
        window.show()
        if entry_probe is not None:
            entry_probe(window, app)
        result = app.exec()
        host = window.catalog_host
        if host.busy or entry_control is not None and not (
                host.entry_check is not None and host.entry_check.close_admitted
                and host.executor.closed and not host.executor.close_held):
            raise RuntimeError("Encyclopedia work remains held by the original entry")
        return result
    except BaseException as error:
        if window.catalog_host.busy or entry_control is not None:
            window.catalog_host.cancel()
            _HELD_ENTRIES.append((window, window.catalog_host, app))
            error.encyclopedia_entry_owner = window.catalog_host
            if entry_control is not None:
                error.encyclopedia_child_control = entry_control
            error.add_note("The original Encyclopedia window and worker are retained for checked cleanup.")
        else:
            window.close()
        raise


def _run_original_root(backend, control):
    """Service the original root on one retained stock executor."""
    import time
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    from core.manager_launch_owner import LaunchHold
    executor = ResourceExecutorOwner()
    current_control = [control]
    acknowledged_control = [None]
    held_root = [None]

    def service():
        current = current_control[0]
        while True:
            while not current.close_intent:
                try:
                    # Observe the exact original reader while it can still be
                    # visible, before blocking for commands or acknowledging Close.
                    backend.poll_reader()
                    reply = current.handle_next()
                    if reply == {'status': 'close_requested'}:
                        acknowledged_control[0] = current
                        if held_root[0] is not None:
                            held_root[0].close_acknowledged = True
                except LaunchHold as error:
                    if type(error) is LaunchHold and str(error) == 'control_channel_idle_timeout':
                        continue  # Stock native read established zero-byte idle.
                    raise
            if acknowledged_control[0] is not current:
                raise RuntimeError('Original Close reply was not acknowledged')
            while not backend.poll_reader():
                time.sleep(.05)
            settlement = current.after_reader_close()
            from core.encyclopedia_installation import InstallationResult
            if isinstance(settlement, InstallationResult) and not settlement.connection_saved:
                raise RuntimeError('Original connection publication remains held')
            if backend.request is None:
                if backend.binding.current().phase == 'activated':
                    backend.finish_initial_health()
                if backend.close_held:
                    raise RuntimeError('Original root settlement remains held')
                return
            # Installation/recovery does not grant reader health. Reuse the
            # original backend for the next exact selected reader attempt.
            backend.launch_current()
            from .control_entry import EncyclopediaRootControl
            current = EncyclopediaRootControl(backend)
            current_control[0] = current
            current.accept_ready()

    try:
        future = executor.submit(service)
        future.result()
        while executor.close_held:
            time.sleep(.05)
        backend.close()
        executor.begin_close()
        return 0
    except BaseException as error:
        hold = OriginalRootHold(backend, current_control[0], executor, error)
        hold.close_acknowledged = acknowledged_control[0] is current_control[0]
        held_root[0] = hold
        def resume_original_reader():
            # Only an explicit recovery-panel action invokes this continuation.
            # Stock backend validates selection/custody; no new root or installer.
            try:
                hold.close_acknowledged = False
                backend.launch_current()
                from .control_entry import EncyclopediaRootControl
                current_control[0] = EncyclopediaRootControl(backend)
                hold.control = current_control[0]
                current_control[0].accept_ready()
                service()
            except BaseException as resumed_error:
                hold.error = resumed_error
                from .root_recovery_ui import persist_root_failure
                persist_root_failure(hold,phase='reader_resume')
                raise
        hold.resume_original_reader = resume_original_reader
        _HELD_ENTRIES.append(hold)
        error.encyclopedia_root_owner = backend
        error.encyclopedia_root_executor = executor
        return _present_root_failure(hold,phase='root_service')


def _run_original_enrollment(adapter):
    """Original first-install UI, then same-owner reader handoff; no fresh root."""
    import time,re
    from core.encyclopedia_enrollment import StandaloneEncyclopediaEnrollment
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    from .root_recovery_ui import present_original_enrollment
    from .entry_binding import start_original_reader
    if type(adapter) is not StandaloneEncyclopediaEnrollment:
        raise TypeError('original_enrollment_adapter_required')
    executor=ResourceExecutorOwner()
    _HELD_ENTRIES.append(adapter)
    try:
        backend=present_original_enrollment(adapter,executor)
    except BaseException as error:
        backend=getattr(adapter,'_reader_backend',None)
        if backend is not None:
            hold=OriginalRootHold(backend,None,executor,error)
        else:
            try:
                if executor.close_held:raise RuntimeError('original_enrollment_worker_still_owned')
                future=executor.submit(adapter.close)
                while not future.done() or executor.close_held:time.sleep(.005)
                terminal=future.result()
                if type(terminal) is not str or re.fullmatch('[0-9a-f]{64}',terminal) is None:
                    raise ValueError('original_enrollment_close_receipt_required')
                executor.begin_close()
            except BaseException as cleanup_error:
                error.add_note('Original setup cleanup remains held: '+type(cleanup_error).__name__)
                hold=OriginalDeploymentHold(adapter.owner,executor,error)
            else:raise
        _HELD_ENTRIES.append(hold)
        return _present_root_failure(hold,phase='enrollment_cleanup')
    if executor.close_held:raise RuntimeError('original_enrollment_worker_drain_required')
    executor.begin_close()
    if backend is None:return 0
    try:
        started=start_original_reader(backend)
    except BaseException as error:
        hold=OriginalRootHold(backend,_retained_startup_control(error,backend),ResourceExecutorOwner(),error)
        _HELD_ENTRIES.append(hold)
        return _present_root_failure(hold,phase='reader_startup')
    return _run_original_root(*started)


def run(argv=None):
    """Dispatch fixed compiled roles before importing Qt or catalog services."""
    import sys
    arguments = tuple(sys.argv[1:] if argv is None else argv)
    if not getattr(sys, 'frozen', False):
        from importlib.util import find_spec
        if find_spec(__package__ + '.bootstrap_entry') is None:
            # Finite source deliveries deliberately omit compiled native roles.
            # Ordinary argparse still refuses every inherited/custody argument.
            return main(argv=arguments)
    from . import bootstrap_entry
    from .entry_binding import HOST, render_compiled_child, start_compiled_control
    if arguments == ('--encyclopedia-custody',):
        profile = bootstrap_entry.PROFILE
        if profile is None:
            raise ValueError('compiled_standalone_profile_unavailable')
        from .custody_entry import CustodyObserver
        observer = CustodyObserver.from_inherited_entry(
            bootstrap_sha256=profile.bootstrap_sha256)
        return observer.run()
    if any(argument.split('=', 1)[0] in HOST for argument in arguments):
        return render_compiled_child(main, arguments)
    if bootstrap_entry.PROFILE is not None and arguments:
        raise ValueError('compiled_root_renderer_arguments_unqualified')
    try:
        started = start_compiled_control()
    except BaseException as error:
        original = getattr(error, 'encyclopedia_root_owner', None)
        if original is None:
            raise
        from suite_core.manager.resource_executor import ResourceExecutorOwner
        from core.manager_launch_owner import DeploymentOwner
        if type(original) is DeploymentOwner:
            hold = OriginalDeploymentHold(original,ResourceExecutorOwner(),error)
        else:
            hold = OriginalRootHold(original,_retained_startup_control(error,original),ResourceExecutorOwner(),error)
        _HELD_ENTRIES.append(hold)
        return _present_root_failure(hold,phase='compiled_startup')
    if started is None:
        return render_compiled_child(main, arguments)
    from core.encyclopedia_enrollment import StandaloneEncyclopediaEnrollment
    if type(started) is StandaloneEncyclopediaEnrollment:
        return _run_original_enrollment(started)
    return _run_original_root(*started)


if __name__ == "__main__":
    raise SystemExit(run())
