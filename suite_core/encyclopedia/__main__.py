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
        default_content_factory=None, optional_content_factory=None):
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
    if optional_content_factory is not None and (not callable(optional_content_factory)
            or default_content_factory is not None):
        raise ValueError('one_explicit_optional_content_owner_required')
    window = EncyclopediaWindow()
    host = EncyclopediaCatalogHost(window, settings_path=settings_path, executor=executor)
    if entry_control is not None:
        host.entry_check = update_check
    window.catalog_host = host
    if optional_content_factory is not None:
        host.optional_content_factory = optional_content_factory
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
        entry_control=None, default_content_factory=None, optional_content_factory=None):
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
    if optional_content_factory is None and default_content_factory is None:
        from .tag_data_feed_entry import standalone_tag_data_factory
        optional_content_factory = standalone_tag_data_factory(settings_path=Path(settings_path),
            trust_path=Path(__file__).resolve().parents[2] / 'data/defaults/release-trust.json')
    window = create_window(settings_path=Path(settings_path), update_check=update_check,
        executor=executor, entry_control=entry_control, default_content_factory=default_content_factory,
        optional_content_factory=optional_content_factory)
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


def _await_original_startup(factory, executor, progress):
    """Resolve one original startup Future while Qt paints; never resubmit it."""
    import sys
    future = executor.submit(factory)
    retained = dict(executor=executor, future=future, progress=progress)
    _HELD_ENTRIES.append(retained)
    presentation_error = None
    try:
        try:
            result = progress.wait(future, executor)
            if not future.done() or executor.close_held:
                raise RuntimeError('Startup presentation returned before original worker settlement')
        except BaseException as observed:
            # A failed presenter may return while the factory is still creating
            # its original owner. Resolve that exact outcome before selecting
            # recovery; no blocking result(), replacement factory or new worker.
            presentation_error = observed
            if not future.done() or executor.close_held:
                from PySide6.QtCore import QCoreApplication, QEventLoop, QTimer
                from PySide6.QtWidgets import QApplication
                app = QApplication.instance() or QApplication([])
                app.setQuitOnLastWindowClosed(False)
                loop = QEventLoop()
                timer = QTimer()
                timer.setInterval(50)
                outer_loop = [False]
                paint_fallback = (future.done() and not future.cancelled()
                    and future.exception() is observed)
                if paint_fallback:
                    progress.phase('Startup needs attention', 'Waiting for the original startup worker to settle. Keep this window open; do not start another copy.')
                def observe_startup():
                    nonlocal paint_fallback
                    if paint_fallback:
                        try:progress.poll()
                        except BaseException as secondary:
                            retained['presentation_error'] = secondary
                            paint_fallback = False
                    if future.done() and not executor.close_held:
                        if outer_loop[0]:
                            # Qt quit() can be vetoed by our unretired progress
                            # window. Exit only this temporary startup loop after
                            # the original Future AND physical worker settle.
                            QCoreApplication.exit(0)
                        else:
                            loop.quit()
                timer.timeout.connect(observe_startup)
                timer.start()
                try:
                    loop.exec()
                    if not future.done() or executor.close_held:
                        # A nested event loop may be interrupted independently.
                        # The root has not entered its application event loop yet.
                        # Keep observing the same Future; never restart startup.
                        retained['nested_loop_interrupted'] = True
                        outer_loop[0] = True
                        app.exec()
                finally:
                    timer.stop()
                if not future.done() or executor.close_held:
                    # Both Qt loops stopped: painting cannot be promised. Keep
                    # the real operation retained, and still resolve its eventual
                    # outcome rather than waiting on an event nobody can set.
                    from threading import Event
                    retained['supervision_error'] = RuntimeError('Original startup outcome remains unconfirmed')
                    if sys.stderr is not None:
                        print('Tag Encyclopedia startup supervision remains held; do not relaunch or discard its recovery files.', file=sys.stderr, flush=True)
                    interval = Event()
                    while not future.done() or executor.close_held:
                        interval.wait(.05)
            # This is now nonblocking. A factory exception carrying its actual
            # owner/control takes precedence over a presentation exception.
            try:
                result = future.result()
            except BaseException as original:
                if original is not observed:
                    original.add_note('Startup progress presentation also failed: ' + type(observed).__name__)
                raise
        return result, presentation_error
    finally:
        # Unresolved startup retains this record above. Settled outcomes are
        # handed to the caller's existing typed hold or ordinary root service.
        if future.done() and not executor.close_held:
            _HELD_ENTRIES.remove(retained)


def _present_original_startup_failure(original, control, executor, error, progress, *, phase):
    from core.manager_launch_owner import DeploymentOwner
    hold = (OriginalDeploymentHold(original, executor, error)
        if type(original) is DeploymentOwner else OriginalRootHold(original, control, executor, error))
    _HELD_ENTRIES.append(hold)
    error.encyclopedia_root_executor = executor
    if progress is not None:
        try:
            progress.finish()
        except BaseException as cleanup_error:
            hold.progress_cleanup_error = cleanup_error
    return _present_root_failure(hold, phase=phase)


def _run_original_root(backend, control, *, executor=None, progress=None):
    """Service the original root on one retained stock executor."""
    import time
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    from core.manager_launch_owner import LaunchHold
    executor = executor if executor is not None else ResourceExecutorOwner()
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
                        progress.phase('Finishing Reader Close', 'Waiting for the Reader and its background work to close safely.')
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
            phase = getattr(backend, 'owner', None)
            phase = getattr(phase, 'record', {}).get('standalone_update', {}).get('phase')
            if getattr(current, 'after_close_operation', None) == 'recover':
                progress.phase('Restoring the previous Reader', 'Restoring and checking the retained app version. Your catalogs and images are kept.')
            elif phase == 'waiting_for_close':
                progress.phase('Installing the Reader update', 'Downloading, verifying and preparing the update. The Reader will open when it is ready.')
            elif phase in ('applying', 'recovering'):
                progress.phase('Checking the Reader installation', 'Verifying the saved app selection and its normal Close before finishing.')
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
            progress.phase('Opening the Reader', 'Checking the selected Reader and preparing its window.')
            backend.launch_current()
            from .control_entry import EncyclopediaRootControl
            current = EncyclopediaRootControl(backend)
            current_control[0] = current
            current.accept_ready()
            progress.reader_ready()

    try:
        from .pending_task_ui import RootTaskProgress
        if progress is None:
            progress = RootTaskProgress()
        else:
            progress.reader_ready()
            progress.poll()
        future = executor.submit(service)
        progress.wait(future, executor)
        progress.phase('Finishing Reader Close', 'Finishing saved state and releasing the app’s background resources safely.')
        progress.wait(executor.submit(backend.close), executor)
        executor.begin_close()
        progress.finish()
        return 0
    except BaseException as error:
        hold = OriginalRootHold(backend, current_control[0], executor, error)
        hold.close_acknowledged = acknowledged_control[0] is current_control[0]
        held_root[0] = hold
        _HELD_ENTRIES.append(hold)
        error.encyclopedia_root_owner = backend
        error.encyclopedia_root_executor = executor
        if progress is not None:
            try:
                progress.finish()
            except BaseException as cleanup_error:
                hold.progress_cleanup_error = cleanup_error
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
        return _present_root_failure(hold,phase='root_service')


def _run_original_enrollment(adapter, *, executor=None, startup_error=None):
    """Original first-install UI, then same-owner reader handoff; no fresh root."""
    import re
    from core.encyclopedia_enrollment import StandaloneEncyclopediaEnrollment
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    from .root_recovery_ui import present_original_enrollment
    from .entry_binding import start_original_reader
    if type(adapter) is not StandaloneEncyclopediaEnrollment:
        raise TypeError('original_enrollment_adapter_required')
    executor=executor if executor is not None else ResourceExecutorOwner()
    progress=None
    _HELD_ENTRIES.append(adapter)
    try:
        if startup_error is not None:raise startup_error
        backend=present_original_enrollment(adapter,executor)
    except BaseException as error:
        backend=getattr(adapter,'_reader_backend',None)
        if backend is not None:
            hold=OriginalRootHold(backend,None,executor,error)
        else:
            try:
                if executor.close_held:raise RuntimeError('original_enrollment_worker_still_owned')
                from .pending_task_ui import RootTaskProgress
                progress=RootTaskProgress()
                progress.phase('Finishing Reader setup', 'Closing the original setup and its background resources safely.')
                terminal,presentation_error=_await_original_startup(adapter.close,executor,progress)
                if type(terminal) is not str or re.fullmatch('[0-9a-f]{64}',terminal) is None:
                    raise ValueError('original_enrollment_close_receipt_required')
                executor.begin_close()
            except BaseException as cleanup_error:
                error.add_note('Original setup cleanup remains held: '+type(cleanup_error).__name__)
                hold=OriginalDeploymentHold(adapter.owner,executor,error)
            else:
                # The original adapter produced its checked terminal receipt.
                # UI errors cannot turn completed cleanup into an unreleasable
                # live-owner hold or replace the original presentation failure.
                if presentation_error is not None:
                    error.add_note('Setup progress presentation also failed: '+type(presentation_error).__name__)
                try:progress.finish()
                except BaseException as secondary:
                    error.add_note('Setup progress cleanup also failed: '+type(secondary).__name__)
                raise
        _HELD_ENTRIES.append(hold)
        if progress is not None:
            try:progress.finish()
            except BaseException as cleanup_error:hold.progress_cleanup_error=cleanup_error
        return _present_root_failure(hold,phase='enrollment_cleanup')
    if executor.close_held:raise RuntimeError('original_enrollment_worker_drain_required')
    if backend is None:
        executor.begin_close()
        return 0
    try:
        # The stock enrollment dialog retires its worker when Open hands off.
        # A closed, physically drained UI worker is never reopened. The new
        # startup worker retains the same native backend and is then handed to
        # the ordinary root service, including on startup failure.
        if executor.closed:
            executor=ResourceExecutorOwner()
        from .pending_task_ui import RootTaskProgress
        progress=RootTaskProgress()
        progress.phase('Opening the Reader', 'Checking the installed app and preparing its window. Your databases stay separate.')
        started,presentation_error=_await_original_startup(lambda:start_original_reader(backend),executor,progress)
    except BaseException as error:
        return _present_original_startup_failure(backend,_retained_startup_control(error,backend),executor,error,progress,phase='reader_startup')
    if presentation_error is not None:
        return _present_original_startup_failure(backend,started[1],executor,presentation_error,progress,phase='reader_startup_progress')
    return _run_original_root(*started,executor=executor,progress=progress)


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
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    from .pending_task_ui import RootTaskProgress
    executor=ResourceExecutorOwner()
    progress=None
    try:
        progress=RootTaskProgress()
        progress.phase('Preparing Tag Encyclopedia', 'Checking the app installation and preparing its next window.')
        started,presentation_error=_await_original_startup(start_compiled_control,executor,progress)
    except BaseException as error:
        original = getattr(error, 'encyclopedia_root_owner', None)
        if original is None:
            executor.begin_close()
            if progress is not None:
                try:progress.finish()
                except BaseException as cleanup_error:error.add_note('Startup progress cleanup also failed: '+type(cleanup_error).__name__)
            raise
        return _present_original_startup_failure(original,_retained_startup_control(error,original),executor,error,progress,phase='compiled_startup')
    if started is None:
        executor.begin_close()
        progress.finish()
        if presentation_error is not None:raise presentation_error
        return render_compiled_child(main, arguments)
    from core.encyclopedia_enrollment import StandaloneEncyclopediaEnrollment
    if type(started) is StandaloneEncyclopediaEnrollment:
        try:progress.finish()
        except BaseException as cleanup_error:presentation_error=presentation_error or cleanup_error
        return _run_original_enrollment(started,executor=executor,startup_error=presentation_error)
    if presentation_error is not None:
        return _present_original_startup_failure(started[0],started[1],executor,presentation_error,progress,phase='compiled_startup_progress')
    return _run_original_root(*started,executor=executor,progress=progress)


if __name__ == "__main__":
    raise SystemExit(run())
