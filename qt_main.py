from __future__ import annotations

import ctypes
import hashlib
import importlib
import importlib.util
import json
import os
import subprocess
import sys
import tempfile
import traceback
import urllib.request
from dataclasses import dataclass

from runtime_contract import install_pip_wheel,load_expected_runtime_contract,validate_runtime
from version import PRODUCT_NAME,VERSION


CORE_PACKAGES=(
    ("PySide6","PySide6-Essentials>=6.10,<7"),
    ("PIL","Pillow>=10.0"),
)
_NO_CONSOLE={"creationflags":getattr(subprocess,"CREATE_NO_WINDOW",0)}
_native_crash_trace=None


@dataclass(frozen=True)
class StartupFailure:
    message: str
    reason_code: str = ""
    recovery_location: str = ""


def _startup_failure(exc: Exception) -> StartupFailure:
    from core.knowledge_pack import KnowledgePackRecoveryHold
    if isinstance(exc, KnowledgePackRecoveryHold):
        return StartupFailure(str(exc), exc.reason_code, str(exc.recovery_location))
    return StartupFailure(f"{type(exc).__name__}: {exc}")


def _present_startup_failure(failure: StartupFailure) -> None:
    if failure.reason_code == "knowledge-pack-recovery-held":
        try:
            from qt_app.startup_recovery import StartupRecoveryDialog
            StartupRecoveryDialog(failure.message, failure.recovery_location,
                                  reason_code=failure.reason_code).exec()
            return
        except Exception:
            # Recovery guidance must remain available if the Qt surface fails.
            detail=(
                "Knowledge Pack recovery needs attention.\n\n"
                f"{failure.message}\n\nRecovery records: {failure.recovery_location}\n\n"
                "Keep backups and receipts unchanged. Restarting alone may not resolve this hold."
            )
    else:
        detail=(
            f"{PRODUCT_NAME} could not open its local application state.\n\n"
            f"{failure.message}\n\nNo image, TXT, or Metadata files were changed."
        )
    if not _native_error(f"{PRODUCT_NAME} could not start",detail):
        print(detail)


def _enable_native_crash_trace(app_root,logger) -> None:
    """Retain code-only fault stacks for windowless native Qt failures."""
    import faulthandler
    from pathlib import Path

    global _native_crash_trace
    # Preserve diagnostics explicitly installed by a caller or test runner.
    if faulthandler.is_enabled():return
    stream=None
    try:
        path=Path(app_root)/"logs"/"native-crash.log"
        path.parent.mkdir(parents=True,exist_ok=True)
        if path.exists() and path.stat().st_size>512*1024:
            path.replace(path.with_name("native-crash.previous.log"))
        stream=path.open("ab",buffering=0)
        stream.write(f"\n{PRODUCT_NAME} {VERSION} native fault trace\n".encode("utf-8"))
        # Keep the stream alive for the process lifetime, including Qt teardown.
        # faulthandler writes code locations, not image bytes or local values.
        faulthandler.enable(file=stream,all_threads=True)
    except (OSError,RuntimeError,ValueError) as error:
        if stream is not None:stream.close()
        logger.warning("Native fault tracing unavailable: %s",type(error).__name__)
    else:
        _native_crash_trace=stream


def _set_windows_app_identity() -> None:
    if sys.platform!="win32": return
    try: ctypes.windll.shell32.SetCurrentProcessExplicitAppUserModelID("DigitalSteel7.CollectiGen")
    except (AttributeError,OSError): pass


def _native_confirmation(title: str, message: str) -> bool | None:
    """Ask a startup question without relying on Qt or Tkinter being usable."""
    if sys.platform != "win32":
        return None
    try:
        # MB_YESNO | MB_ICONWARNING | MB_SETFOREGROUND
        result=ctypes.windll.user32.MessageBoxW(
            None,message,title,0x00000004 | 0x00000030 | 0x00010000,
        )
        return result == 6  # IDYES
    except (AttributeError,OSError):
        return None


def _native_error(title: str,message: str) -> bool:
    """Show startup failures even when CollectiGen was launched with pythonw."""
    if sys.platform != "win32": return False
    try:
        # MB_OK | MB_ICONERROR | MB_SETFOREGROUND
        ctypes.windll.user32.MessageBoxW(None,message,title,0x00000000 | 0x00000010 | 0x00010000)
        return True
    except (AttributeError,OSError):
        return False


def _confirm_core_install(packages: list[str]) -> bool:
    names="\n".join(f"• {package}" for package in packages)
    message=(
        f"{PRODUCT_NAME}'s bundled application components are incomplete:\n\n"
        f"{names}\n\nRepair this Python environment now?"
    )
    accepted=_native_confirmation(f"Finish {PRODUCT_NAME} setup",message)
    if accepted is not None:
        return accepted
    print(message)
    try: return input("Install now? [y/N] ").strip().casefold() in {"y","yes"}
    except EOFError: return False


def _install_core_packages(packages: list[str], *, force: bool=False) -> bool:
    if not _ensure_pip(): return False
    command=[sys.executable,"-m","pip","install"]
    if force: command.extend(("--upgrade","--force-reinstall"))
    command.extend(packages)
    try:
        subprocess.run(command,check=True,capture_output=True,text=True,**_NO_CONSOLE)
    except subprocess.CalledProcessError as exc:
        detail=(exc.stderr or exc.stdout or str(exc)).strip()
        message="Core component repair failed:\n\n"+detail
        if not _native_error(f"{PRODUCT_NAME} repair failed",message): print(message)
        return False
    importlib.invalidate_caches()
    return True


def _ensure_pip() -> bool:
    try:
        subprocess.run(
            [sys.executable,"-m","pip","--version"],check=True,
            capture_output=True,text=True,**_NO_CONSOLE,
        )
        return True
    except subprocess.CalledProcessError:
        pass
    try:
        with tempfile.TemporaryDirectory() as folder:
            wheel=os.path.join(folder,"pip-wheel.whl")
            pins_path=os.path.join(os.path.dirname(os.path.abspath(__file__)),"tools","runtime-pins.json")
            with open(pins_path,encoding="utf-8") as stream: pins=json.load(stream)
            source=pins["pip_wheel"]
            urllib.request.urlretrieve(source["url"],wheel)
            with open(wheel,"rb") as stream:
                actual=hashlib.sha256(stream.read()).hexdigest()
            if actual.casefold()!=str(source["sha256"]).casefold():
                raise RuntimeError("The pip recovery download failed checksum verification.")
            install_pip_wheel(
                wheel,expected_version=str(source["version"]),
                expected_sha256=str(source["sha256"]),
            )
            result=subprocess.run(
                [sys.executable,"-c","import pip; print(pip.__version__)"],
                check=True,capture_output=True,text=True,**_NO_CONSOLE,
            )
            if result.stdout.strip()!=str(source["version"]):
                raise RuntimeError("The installed pip version does not match the reviewed wheel.")
        importlib.invalidate_caches()
        return True
    except (KeyError,ValueError,OSError,RuntimeError,subprocess.SubprocessError) as exc:
        message=f"pip recovery failed:\n\n{exc}"
        if not _native_error(f"{PRODUCT_NAME} repair failed",message): print(message)
        return False


def _bootstrap_core() -> bool:
    packages=[package for module,package in CORE_PACKAGES if importlib.util.find_spec(module) is None]
    if not packages: return True
    if not _confirm_core_install(packages):
        print("The required application components are not available.")
        return False
    if not _install_core_packages(packages): return False
    return all(importlib.util.find_spec(module) is not None for module,_ in CORE_PACKAGES)


def _validate_bundled_runtime(*, verify_hashes: bool = True) -> tuple[bool, list[str]]:
    root=os.path.dirname(os.path.abspath(__file__))
    runtime_root=os.path.join(root,"data","runtime")
    manifest=os.path.join(runtime_root,"python","collectigen-runtime.json")
    try:
        expected=load_expected_runtime_contract(
            os.path.join(root,"tools","runtime-pins.json"),
            os.path.join(root,"requirements-runtime.lock"),
        )
    except (OSError,ValueError,json.JSONDecodeError) as exc:
        return False,[f"The checked-in runtime dependency contract could not be read: {exc}"]
    result=validate_runtime(
        manifest,application_version=VERSION,runtime_root=runtime_root,
        verify_hashes=verify_hashes,expected_contract=expected,
    )
    return result.ready,list(result.issues)


def _bootstrap_qt() -> bool:
    """Compatibility name retained for launcher-focused tests and tools."""
    return _bootstrap_core()


def _is_dependency_failure(exc: BaseException) -> bool:
    if isinstance(exc,ModuleNotFoundError):
        return (exc.name or "").partition(".")[0] in {"PySide6","PIL","shiboken6"}
    detail=f"{type(exc).__name__}: {exc}".casefold()
    markers=("pyside6","shiboken6","qt6","dll load failed","pillow")
    return isinstance(exc,(ImportError,OSError)) and any(marker in detail for marker in markers)


def _confirm_forced_repair(exc: BaseException) -> bool:
    message=(
        f"{PRODUCT_NAME} could not load a required application component.\n\n"
        f"{type(exc).__name__}: {exc}\n\n"
        "Repair the bundled Qt and image packages, then retry startup?"
    )
    accepted=_native_confirmation(f"Repair {PRODUCT_NAME}",message)
    if accepted is not None:
        return accepted
    print(message)
    try: return input("Repair and retry? [y/N] ").strip().casefold() in {"y","yes"}
    except EOFError: return False


def _recover_previous_ai_work() -> tuple[bool,str]:
    from core.ai_runtime_recovery import recover_ai_work
    from core.paths import AI_WORK_LEASE_PATH,APP_ROOT

    from core.startup_profile import startup_write_root
    recovery_root=startup_write_root(APP_ROOT)
    recovery=recover_ai_work(AI_WORK_LEASE_PATH,recovery_root)
    if recovery.status=="none":
        return True,""
    if recovery.status=="stale_cleared":
        return True,recovery.message
    if recovery.status=="cleanup_failed":
        _native_error(
            f"{PRODUCT_NAME} could not clear interrupted AI work",
            recovery.message+"\n\nRestart Windows before starting another AI session.",
        )
        return False,""
    accepted=_native_confirmation(
        f"Stop prior {PRODUCT_NAME} AI work?",
        (
            f"{recovery.message}\n\n"
            f"Task: {recovery.label}\nProcess: {recovery.pid}\n\n"
            "Stop this verified prior CollectiGen process and clear its interrupted "
            "AI state before starting a new session?"
        ),
    )
    if accepted is not True:
        return False,""
    stopped=recover_ai_work(AI_WORK_LEASE_PATH,recovery_root,terminate=True)
    if stopped.status!="terminated":
        _native_error(
            f"{PRODUCT_NAME} could not stop prior AI work",
            stopped.message+"\n\nRestart Windows before starting another AI session.",
        )
        return False,""
    return True,stopped.message


def _start_application(*,startup_notice: str="",dependency_service_factory=None) -> int:
    from PySide6.QtCore import QObject,QThread,QThreadPool,QTimer,Qt,Signal,Slot
    from PySide6.QtWidgets import QApplication,QDialog,QFrame,QLabel,QProgressBar,QVBoxLayout
    from qt_app.brand import application_icon

    _set_windows_app_identity()
    app=QApplication(sys.argv)
    app.setApplicationName(PRODUCT_NAME)
    app.setWindowIcon(application_icon())
    # Compatibility identity: changing this can redirect platform QSettings.
    app.setOrganizationName(PRODUCT_NAME)
    app.setStyle("Fusion")
    # Leave capacity for painting/input and avoid an unbounded mix of thumbnail,
    # rating, inspection, and page workers saturating the host after startup.
    ideal_threads=max(1,int(QThread.idealThreadCount()))
    QThreadPool.globalInstance().setMaxThreadCount(
        max(1,min(4,ideal_threads-1 if ideal_threads>1 else 1)),
    )

    startup=QDialog()
    startup.setWindowTitle(f"Opening {PRODUCT_NAME}")
    startup.setWindowIcon(application_icon())
    startup.setWindowFlags(
        Qt.Dialog|Qt.CustomizeWindowHint|Qt.WindowTitleHint,
    )
    startup.setMinimumWidth(420)
    startup_layout=QVBoxLayout(startup)
    # Keep the pre-core shell lightweight while using the same footer role.
    startup_footer=QFrame(); startup_footer.setObjectName("statusFooter")
    startup_footer.setAccessibleName("Window status and activity")
    startup_footer_layout=QVBoxLayout(startup_footer)
    startup_label=QLabel(
        "Opening your local workspace…\nLarge knowledge updates are applied safely in the background."
    )
    startup_label.setWordWrap(True)
    startup_progress=QProgressBar()
    startup_progress.setRange(0,0)
    startup_progress.setAccessibleName("CollectiGen startup progress")
    startup_footer_layout.addWidget(startup_label)
    startup_footer_layout.addWidget(startup_progress)
    startup_layout.addWidget(startup_footer)
    startup.show()
    app.processEvents()

    from qt_app import TagManagerWindow
    from qt_app.main_window import run_settings_recovery_preflight
    from core import (
        APP_ROOT,SETTINGS_PATH,USER_DATA_DIR,AppSettings,AppState,
        configure_logging,
    )
    from core.onboarding import is_genuinely_new_profile
    from qt_app.theme import apply_theme
    from qt_app.tooltips import apply_tooltip_policy
    from qt_app.notification_trace import install_notification_trace

    # Capture the clean-profile decision before migrations, default sync, or
    # AppState create ordinary application-owned files under data/user.
    genuinely_new_profile=is_genuinely_new_profile(
        USER_DATA_DIR,SETTINGS_PATH,
    )
    from core.startup_profile import startup_write_root
    write_root=startup_write_root(APP_ROOT)
    logger=configure_logging(write_root)
    _enable_native_crash_trace(write_root,logger)
    previous_hook=sys.excepthook
    def log_unhandled(error_type,error,traceback_value):
        logger.error(
            "Unhandled application exception: %s",
            error_type.__name__,
            exc_info=(error_type,error,traceback_value),
        )
        previous_hook(error_type,error,traceback_value)
    sys.excepthook=log_unhandled
    logger.info("%s v%s starting",PRODUCT_NAME,VERSION)
    app._collectigen_notification_trace=install_notification_trace(app,logger)
    settings=AppSettings.load(); apply_theme(app,settings.ui_theme,settings.color_blind_mode)
    apply_tooltip_policy(app,settings.show_tooltips)
    if not run_settings_recovery_preflight(settings,None):
        logger.warning("Application startup stopped at the settings recovery boundary")
        return 5
    try:
        from core.application_migrations import run_application_migrations
        run_application_migrations(write_root,VERSION)
    except Exception as exc:
        message=(
            f"{PRODUCT_NAME} could not safely upgrade its local data. No partial migration was kept.\n\n"
            f"{type(exc).__name__}: {exc}\n\n"
            f"If this followed an application update, start {PRODUCT_NAME} again to restore the previous verified version and data snapshot."
        )
        if not _native_error(f"{PRODUCT_NAME} data upgrade could not finish",message): print(message)
        return 3
    # Migration may have updated the settings schema. Reopen the exact current
    # file before constructing any other persistent application state.
    settings=AppSettings.load()
    if not run_settings_recovery_preflight(settings,None):
        logger.warning("Post-migration settings validation stopped application startup")
        return 5
    settings=AppSettings.load()
    apply_theme(app,settings.ui_theme,settings.color_blind_mode)
    apply_tooltip_policy(app,settings.show_tooltips)
    startup_label.setText(
        "Preparing CollectiGen for first use…"
        if genuinely_new_profile else
        "Loading settings and System Knowledge…"
    )
    startup.adjustSize()

    class StartupStateWorker(QObject):
        complete=Signal(object)
        failed=Signal(object)

        def __init__(self,startup_settings):
            super().__init__(); self.settings=startup_settings

        @Slot()
        def run(self):
            try:
                state=(AppState(settings=self.settings) if dependency_service_factory is None else
                       AppState(settings=self.settings,dependency_service_factory=dependency_service_factory))
                self.complete.emit(state)
            except Exception as exc:
                self.failed.emit(_startup_failure(exc))

    thread=QThread()
    worker=StartupStateWorker(settings)
    worker.moveToThread(thread)
    app._collectigen_startup_thread=thread
    app._collectigen_startup_worker=worker

    def startup_failed(failure: StartupFailure) -> None:
        logger.error("Application state startup failed: %s",failure.message)
        # Closing the startup window must not terminate the event loop before
        # the recovery assistance modal can receive input.
        app.setQuitOnLastWindowClosed(False)
        startup.close()
        thread.quit()
        _present_startup_failure(failure)
        app.exit(1)

    def startup_complete(state) -> None:
        try:
            startup_label.setText("Opening the interface…")
            window=TagManagerWindow(
                settings=settings,state=state,
                genuinely_new_profile=genuinely_new_profile,
            )
            if window.startup_cancelled:
                logger.warning("Application startup stopped at the settings recovery boundary")
                window.deleteLater()
                startup.close()
                thread.quit()
                app.exit(5)
                return
            app._collectigen_main_window=window
            window.show()
            startup.close()
            if startup_notice:
                QTimer.singleShot(
                    0,lambda:window.statusBar().showMessage(startup_notice,15000),
                )

            def mark_responsive_startup() -> None:
                from core.application_update import mark_active_update_healthy
                mark_active_update_healthy(
                    write_root,VERSION,
                )

            # Health is earned only after the event loop has displayed the
            # usable main window, not merely after constructing it.
            QTimer.singleShot(0,mark_responsive_startup)
            thread.quit()
        except Exception as exc:
            startup_failed(_startup_failure(exc))

    class StartupController(QObject):
        """Receive worker results with explicit main-thread Qt affinity."""

        @Slot(object)
        def complete(self,state):
            startup_complete(state)

        @Slot(object)
        def failed(self,failure):
            startup_failed(failure)

    controller=StartupController()
    app._collectigen_startup_controller=controller
    worker.complete.connect(controller.complete,Qt.QueuedConnection)
    worker.failed.connect(controller.failed,Qt.QueuedConnection)
    thread.started.connect(worker.run)
    thread.finished.connect(worker.deleteLater)
    thread.finished.connect(thread.deleteLater)
    thread.start()
    return app.exec()


def main() -> int:
    if "--validate-runtime" in sys.argv:
        ready,issues=_validate_bundled_runtime()
        if not ready:
            print("\n".join(issues))
            return 2
        print(f"{PRODUCT_NAME} portable runtime is ready.")
        return 0
    if not _bootstrap_core(): return 1
    proceed,startup_notice=_recover_previous_ai_work()
    if not proceed: return 4
    from core.application_update import mark_active_update_starting
    from core.startup_profile import startup_write_root
    mark_active_update_starting(startup_write_root(os.path.dirname(os.path.abspath(__file__))),VERSION)
    try:
        return _start_application(startup_notice=startup_notice)
    except Exception as exc:
        already_retried=os.environ.get("COLLECTIGEN_RECOVERY_ATTEMPT")=="1"
        if not already_retried and _is_dependency_failure(exc) and _confirm_forced_repair(exc):
            packages=[package for _,package in CORE_PACKAGES]
            if _install_core_packages(packages,force=True):
                environment=os.environ.copy(); environment["COLLECTIGEN_RECOVERY_ATTEMPT"]="1"
                result=subprocess.run([sys.executable,str(__file__)],env=environment,**_NO_CONSOLE)
                return result.returncode
        message=f"{PRODUCT_NAME} could not start.\n\n{type(exc).__name__}: {exc}\n\nTry Start CollectiGen again to use the built-in repair path."
        if not _native_error(f"{PRODUCT_NAME} could not start",message): print(message)
        traceback.print_exc()
        return 1


if __name__=="__main__":
    raise SystemExit(main())
