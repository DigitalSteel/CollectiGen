"""Standalone source entry candidate; native startup is loaded only by main().

No native activation is performed at import. The scoped bridge exists because
qt_main currently imports its window factory from qt_app inside startup.
"""
from dataclasses import dataclass, replace
from importlib import import_module
from pathlib import Path
import sys
from threading import Lock
from typing import Callable

from .contracts import (
    BoundaryError, EncyclopediaSession, ExistingContext, OptionalBackendUnavailable,
    TagLookupService,
)
from .facade import MediaOrganizer

APP_ID = "collectigen.media-organizer"
APP_NAME = "Media Organizer"
from .version import SOFTWARE_VERSION as APP_VERSION
from .version import DISPLAY_VERSION
INTERFACE_VERSION = 1
PRIMARY_PAGES = (2, 1, 6)
UNSUPPORTED_PAGES = (7, 8)
_startup_lock = Lock()
_ABSENT = object()


def make_window_type(base_window: type, *, current_application: Callable,
                     optional_backends=None, encyclopedia_session=None, settings_path=None) -> type:
    """Subclass the real canonical shell; retain its data and lifecycle wiring."""
    class MediaOrganizerWindow(base_window):
        NAVIGATION_PAGE_ORDER = PRIMARY_PAGES + (0, 3, 4, 5, 7, 8)
        WORKSPACE_COMMAND_GROUPS = ("File", "Home", "Import", "Organize", "Prepare",
            "Dataset Plans", "Review", "Resolution", "Knowledge", "Rules",
            "Settings", "View", "Advanced", "Help")

        def workspace_command_policy(self, command):
            if command.context in {"Generate", "Training"} or command.group == "Generate":
                return None
            if command.group == "Training":
                labels = {"Training Collections.": "Dataset Collections."}
                label = command.source.text() if command.key.endswith(":training_resume_button") else labels.get(command.label, command.label)
                return replace(command, group="Dataset Plans", label=label)
            return command

        def _build_ui(self):
            super()._build_ui()
            # Retain canonical indices/items for contextual repair and mode changes.
            # Primary product navigation exposes only its existing workspaces.
            for row, page_index in enumerate(self.NAVIGATION_PAGE_ORDER):
                self.navigation.item(row).setHidden(page_index not in PRIMARY_PAGES)

        def _build_menu(self):
            super()._build_menu()
            for menu in self._application_menus:
                for action in menu.actions():
                    if action.text().rstrip(".\u2026") == "Plan Image Generation":
                        action.setVisible(False)
                    elif action.text().startswith("About "):
                        action.setText(f"About {APP_NAME}")
                    elif action.text().startswith("What's New in "):
                        action.setText(f"What's New in {APP_NAME} {DISPLAY_VERSION}")

        def about_dialog(self):
            # Retain the canonical QMessageBox, icon and lifecycle, with this
            # product's supported scope rather than the full application's.
            from version import KNOWLEDGE_VERSION, SCHEMA_VERSION
            box = super().about_dialog()
            box.setWindowTitle(f"About {APP_NAME}")
            box.setText(
                f"{APP_NAME} {DISPLAY_VERSION}\n\n"
                "Local image libraries, Ingest, Dataset search and organization.\n"
                "Image Knowledge, optional local image analysis, Dataset planning "
                "and training-data export.\n"
                "Generation and Training execution are unavailable in Media Organizer.\n\n"
                f"Knowledge version {KNOWLEDGE_VERSION}\n"
                f"Application data schema {SCHEMA_VERSION}\n\n"
                "Original application code: AGPL-3.0-or-later\n"
                "Original documentation and compiled-knowledge contributions: CC-BY-SA-4.0\n"
                "Optional JoyCaption support is Built with Llama and uses separate Llama 3.1 terms.\n"
                "Optional Florence-2 Base FT support is MIT-licensed."
            )
            return box

        def release_notes_dialog(self):
            from core.release_notes import ReleaseNoteEntry
            from qt_app.main_window import ReleaseNotesDialog
            notes = Path(__file__).with_name("WHATS_NEW.md").read_text(encoding="utf-8-sig").strip()
            if not notes:
                raise ValueError(f"Release notes for {APP_NAME} {DISPLAY_VERSION} are empty.")
            # The component resource includes its own dates/history. Do not
            # invent a publication date or borrow the full-app changelog.
            dialog = ReleaseNotesDialog((ReleaseNoteEntry(APP_VERSION, "", notes),), self)
            dialog.setWindowTitle(f"What's New in {APP_NAME} {DISPLAY_VERSION}")
            dialog.release_chooser.setItemText(0, f"{APP_NAME} {DISPLAY_VERSION}")
            return dialog

        def __init__(self, settings=None, *, state=None, genuinely_new_profile=None):
            super().__init__(settings=settings, state=state,
                             genuinely_new_profile=genuinely_new_profile)
            if self.startup_cancelled:
                return
            self.organizer = MediaOrganizer(ExistingContext(self.settings, self.state),
                                            self, optional_backends)
            self.encyclopedia_session = encyclopedia_session
            install=getattr(self,"install_native_encyclopedia_route",None)
            if callable(install):
                if settings_path is None:
                    install()
                elif not getattr(getattr(self.settings, "load_report", None), "requires_decision", False):
                    install(settings_path=settings_path)
            self.setWindowTitle(f"{APP_NAME} {DISPLAY_VERSION}")
            app = current_application()
            if app is not None:
                # Display identity must not redirect canonical settings ownership.
                app.setApplicationDisplayName(APP_NAME)
            self.navigation.setCurrentRow(6 if genuinely_new_profile else 2)

        def change_page(self, index):
            if index in UNSUPPORTED_PAGES:
                self._optional_workflow_unavailable()
                self.navigation.setCurrentRow(self.stack.currentIndex())
                return
            if index!=1:
                self._organizer_ingest_wait_task=None
                self._organizer_ingest_attention_pending=False
            elif not hasattr(self,"ingest_page") and not hasattr(self,"dataset_page"):
                task=getattr(self,"_dataset_page_task",None)
                if task is not None:
                    if self.stack.currentIndex()==6 and hasattr(self,"setup_page") and not self.setup_page.confirm_leave():
                        from PySide6.QtCore import QSignalBlocker
                        blocker=QSignalBlocker(self.navigation)
                        self.navigation.setCurrentRow(6)
                        del blocker
                        return
                    # Organizer starts Dataset's stock cold reader immediately.
                    # Its Character Knowledge lock must drain before Ingest's
                    # original constructor opens that same guarded authority.
                    self.stack.setCurrentIndex(1)
                    placeholder=self.stack.widget(1)
                    progress=getattr(placeholder,"set_progress",None)
                    if callable(progress):
                        progress(0,0,"Opening Ingest after Dataset knowledge preparation finishes.")
                    self.statusBar().showMessage("Ingest is waiting for Dataset knowledge preparation.",12000)
                    if getattr(self,"_organizer_ingest_wait_task",None) is not task:
                        self._organizer_ingest_wait_task=task
                        task.signals.complete.connect(lambda *_args,owner=task:self._finish_ingest_dataset_wait(owner))
                        task.signals.failed.connect(lambda message,owner=task:self._finish_ingest_dataset_wait(owner,str(message)))
                        task.signals.cancelled.connect(lambda owner=task:self._finish_ingest_dataset_wait(owner,"Dataset preparation was cancelled."))
                        # Completion can already be queued for the stock Dataset
                        # owner when this later Ingest subscriber is connected.
                        # Signals do not replay; use the coordinator's retained
                        # terminal result after subscribing, covering both sides
                        # of that race without starting another reader.
                        job_id=getattr(task,"job_id","")
                        job=self.state.jobs.get(job_id) if job_id else None
                        if job is not None and job.state in {"complete","failed","cancelled"}:
                            from PySide6.QtCore import QTimer
                            error=(
                                "Dataset preparation was cancelled."
                                if job.state=="cancelled" else
                                (job.error or "Dataset preparation failed.")
                                if job.state=="failed" else ""
                            )
                            QTimer.singleShot(0,lambda owner=task,message=error:self._finish_ingest_dataset_wait(owner,message))
                    return
            # Hidden contextual review/Knowledge/automation remain native routes.
            return super().change_page(index)

        def _finish_ingest_dataset_wait(self,task,error=""):
            from shiboken6 import isValid
            if not isValid(self) or getattr(self,"_organizer_ingest_wait_task",None) is not task:
                return
            self._organizer_ingest_wait_task=None
            if getattr(self,"_dataset_preparation_shutdown",False) or self.navigation.currentRow()!=1:
                return
            if error:
                self._organizer_ingest_attention_pending=False
                placeholder=self.stack.widget(1)
                fail=getattr(placeholder,"set_error",None)
                if callable(fail):fail(error)
                self.statusBar().showMessage("Ingest could not finish opening; select it to retry after the preparation issue is resolved.",12000)
                return
            self.change_page(1)
            if getattr(self,"_organizer_ingest_attention_pending",False) and hasattr(self,"ingest_page"):
                self._organizer_ingest_attention_pending=False
                super()._open_ingest_attention()

        def _open_ingest_attention(self):
            self.navigation.setCurrentRow(1)
            if self.navigation.currentRow()!=1:
                return
            if getattr(self,"_organizer_ingest_wait_task",None) is not None:
                self._organizer_ingest_attention_pending=True
                return
            return super()._open_ingest_attention()

        def _optional_workflow_unavailable(self):
            self.statusBar().showMessage(
                "Generation and Training are unavailable in Media Organizer. "
                "Your library and local organization remain available.", 12000)
            return False

        def open_generation_workspace(self, *, force_new=False):
            return self._optional_workflow_unavailable()

        def open_training_workspace(self, *, group=None, draft=None, collection_reviewed=False):
            return self._optional_workflow_unavailable()

        def show_optional_backend_status(self, name):
            status = self.organizer.optional_backend_status(name)
            self.statusBar().showMessage(status.explanation, 12000)
            return status

        def lookup_encyclopedia_tag(self, identity, *, category_ids=None):
            if self.encyclopedia_session is None:
                self.statusBar().showMessage(
                    "Encyclopedia session unavailable; existing local Tag editing remains available.", 12000)
                return None
            try:
                return self.encyclopedia_session.lookup_tag(identity, category_ids=category_ids)
            except OptionalBackendUnavailable:
                self.statusBar().showMessage(
                    "Encyclopedia session unavailable; use existing local Tag editing.", 12000)
                return None

    MediaOrganizerWindow.__name__ = "MediaOrganizerWindow"
    return MediaOrganizerWindow


@dataclass(frozen=True)
class StartupRuntime:
    """Canonical startup functions supplied explicitly; fakeable without Qt."""
    window_exports: object
    base_window: type
    current_application: Callable
    recover_previous_ai_work: Callable
    mark_active_update_starting: Callable
    start_application: Callable
    settings_path: Path | None = None


def load_native_runtime(*, runtime_selection=None) -> StartupRuntime:
    """Explicit native-only loader; never invoked by pure tests/import inspection.

    Reuse admitted canonical startup, settings recovery/migrations, AppState worker
    and lifecycle. Required dependencies must already exist. Do not invoke the
    native main() repair/download/subprocess retry path from this product entry.
    """
    if runtime_selection is not None:
        runtime_selection.validate()
    startup = import_module("qt_main")
    start_application=startup._start_application
    if runtime_selection is not None and runtime_selection.capability_profile=="organizer-image":
        from inspect import signature
        from .runtime_startup import OrganizerRuntimeError
        if "dependency_service_factory" not in signature(start_application).parameters:
            raise OrganizerRuntimeError("Canonical startup must accept the selected dependency service factory before Organizer can use its image runtime.")
        original_start=start_application
        factory=runtime_selection.dependency_factory()
        def start_application(*,startup_notice=""):
            return original_start(startup_notice=startup_notice,dependency_service_factory=factory)
    exports = import_module("qt_app")
    base_window = import_module("qt_app.main_window").TagManagerWindow
    application = import_module("PySide6.QtWidgets").QApplication

    def mark_starting():
        updates = import_module("core.application_update")
        from core.startup_profile import startup_write_root
        from core.paths import APP_ROOT
        updates.mark_active_update_starting(
            startup_write_root(APP_ROOT), startup.VERSION)

    from core.settings import DEFAULT_SETTINGS_PATH
    return StartupRuntime(exports, base_window, application.instance,
                          startup._recover_previous_ai_work, mark_starting,
                          start_application, settings_path=DEFAULT_SETTINGS_PATH)


def run_startup(runtime: StartupRuntime, *, optional_backends=None,
                encyclopedia_session=None) -> int:
    """Exactly one scoped in-process native startup; restore lazy exports on exit."""
    if not _startup_lock.acquire(blocking=False):
        raise BoundaryError("A Media Organizer startup bridge is already active.")
    try:
        proceed, notice = runtime.recover_previous_ai_work()
        if not proceed:
            return 4
        window_type = make_window_type(runtime.base_window,
            current_application=runtime.current_application,
            optional_backends=optional_backends, encyclopedia_session=encyclopedia_session,
            settings_path=runtime.settings_path)
        exports = vars(runtime.window_exports)
        previous = exports.get("TagManagerWindow", _ABSENT)
        exports["TagManagerWindow"] = window_type
        try:
            runtime.mark_active_update_starting()
            return runtime.start_application(startup_notice=notice)
        finally:
            if exports.get("TagManagerWindow", _ABSENT) is not window_type:
                # Never clobber another owner's concurrent factory replacement.
                raise BoundaryError("Startup factory ownership changed; export left untouched.")
            if previous is _ABSENT:
                del exports["TagManagerWindow"]
            else:
                exports["TagManagerWindow"] = previous
    finally:
        _startup_lock.release()


def main(*, native_loader: Callable = load_native_runtime,runtime_selection=None) -> int:
    """Source startup entry. Activation/runtime behavior remains unqualified."""
    # Permanent source package placement: repo/suite_core/media_organizer/entry.py. Direct script
    # launch otherwise places only this directory on sys.path. Resolve source
    # code here, never profiles, catalogs or library data.
    source_root = str(Path(__file__).resolve().parents[2])
    source_bridge = not getattr(sys, "frozen", False)
    class _OwnedSourcePath(str):
        pass
    source_marker = _OwnedSourcePath(source_root)
    if source_bridge:
        sys.path.insert(0, source_marker)
    try:
        try:
            return run_startup(native_loader())
        except ModuleNotFoundError:
            if runtime_selection is None:
                print("Media Organizer cannot start: a required local runtime is unavailable. "
                      "Use the existing CollectiGen runtime repair workflow; no installer was started.")
            else:
                print("Media Organizer cannot start: a selected local runtime component is unavailable.\n"+
                    runtime_selection.repair_guidance())
            return 2
    finally:
        if source_bridge:
            slots = [i for i, value in enumerate(sys.path) if value is source_marker]
            if len(slots) != 1:
                raise BoundaryError("Startup source-path ownership changed; path left untouched.")
            del sys.path[slots[0]]


if __name__ == "__main__":
    raise SystemExit(main())
