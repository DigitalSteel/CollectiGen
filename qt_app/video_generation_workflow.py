"""Canonical retained Generate entry point for native video requests."""
from __future__ import annotations

from copy import deepcopy
from pathlib import Path
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QFileDialog

from core.model_resource_registry import ModelResourceRegistry
from core.paths import MODEL_RESOURCES_PATH, USER_DATA_DIR
from core.video_generation_draft import load_video_draft

from .generation_workflow import _host, _setup_window
from .status_notifications import FooterMessageBox as QMessageBox
from .video_generation_dialog import VideoGenerationDialog
from .workers import CoordinatedCallTask
from shiboken6 import isValid


def choose_video_generation_draft(parent):
    """Select and read one explicit saved file before adding a video workspace."""
    path, _ = QFileDialog.getOpenFileName(
        parent, "Open video draft", str(USER_DATA_DIR / "video_generation"),
        "Video draft (*.json)",
    )
    if not path:
        return None
    try:
        request = load_video_draft(Path(path).absolute())
        dialog = open_video_generation_workspace(
            parent, initial_request=request, draft_label=Path(path).name,
        )
    except (OSError, ValueError, TypeError, KeyError, OverflowError) as exc:
        QMessageBox.information(
            parent, "Video draft needs attention",
            "The selected video draft could not be opened. Existing work was not changed. "
            f"Choose Open Video Draft again after checking the file.\n\n{exc}",
        )
        return None
    window = _setup_window(_host(parent))
    status_bar = window.statusBar() if window is not None and callable(
        getattr(window, "statusBar", None)) else None
    footer = getattr(status_bar, "footer", None)
    if footer is not None and footer.details.toPlainText().startswith(
        "Video draft needs attention\n"
    ):
        footer.show_message("")
    return dialog


def open_video_generation_workspace(parent, *, source_path=None, on_opened=None,
                                    frame_reference_provider=None, initial_request=None,
                                    draft_label=None):
    """Default opens immediately; explicit runtime context is admitted off-UI."""
    host=_host(parent);state=host.state
    factory=getattr(state,"dependency_service_factory",None)
    lifetime=getattr(state,"video_decoder_lifetime",None)
    options=dict(source_path=source_path,on_opened=on_opened,
                 frame_reference_provider=frame_reference_provider,
                 initial_request=initial_request,draft_label=draft_label)
    if factory is None:
        return _open_prepared_video_generation_workspace(parent,decoder_lifetime=lifetime,**options)
    if getattr(host,"_video_runtime_admission_task",None) is not None:
        return None
    options["initial_request"]=deepcopy(initial_request)
    admit=getattr(state,"admit_video_runtime",None)
    if not callable(admit) or lifetime is None:
        QMessageBox.information(host,"Video components need attention","This host has no admitted Video runtime/lifetime. Review Components in Setup.")
        return None
    was_visible=host.isVisible()
    def prepare(job):
        def check_interrupt():
            if not job.checkpoint():raise InterruptedError("Video opening cancelled")
        check_interrupt()
        if state.dependency_service_factory is not factory or state.video_decoder_lifetime is not lifetime:
            raise RuntimeError("Video runtime context changed. Open the request again.")
        binding=admit(check_interrupt=check_interrupt)
        check_interrupt()
        return binding
    task=CoordinatedCallTask(state.jobs,"Check Video runtime before opening",prepare,resource="io")
    host._video_runtime_admission_task=task
    def current():
        return (isValid(host) and host.state is state and not state.jobs.shutting_down
                and (not was_visible or host.isVisible())
                and getattr(host,"_video_runtime_admission_task",None) is task)
    def clear():
        if isValid(host) and getattr(host,"_video_runtime_admission_task",None) is task:
            host._video_runtime_admission_task=None
    def complete(binding):
        accepted=current();clear()
        if not accepted:return
        if state.dependency_service_factory is not factory or state.video_decoder_lifetime is not lifetime:
            failed("Video runtime context changed. Open the request again.");return
        try:
            _open_prepared_video_generation_workspace(parent,runtime_binding=binding,decoder_lifetime=lifetime,**options)
        except Exception as exc:
            failed(str(exc))
    def failed(message):
        visible=isValid(host) and (not was_visible or host.isVisible());clear()
        if visible:
            QMessageBox.information(host,"Video components need attention",str(message)+"\nReview Video Components in Setup; the request and source remain unchanged.")
    task.signals.complete.connect(complete)
    task.signals.failed.connect(failed)
    task.signals.cancelled.connect(lambda:clear())
    host.destroyed.connect(lambda *_:task.cancel())
    try:task.start()
    except Exception as exc:failed(str(exc))
    return None


def _open_prepared_video_generation_workspace(parent, *, source_path=None, on_opened=None,
                                    frame_reference_provider=None, initial_request=None,
                                    draft_label=None,runtime_binding=None,decoder_lifetime=None):
    """Open a fresh retained draft; an explicit still attaches asynchronously."""
    host = _host(parent)
    window = _setup_window(host)
    state = host.state
    runtime_options={}
    if runtime_binding is not None:runtime_options["runtime_binding"]=runtime_binding
    if decoder_lifetime is not None:runtime_options["decoder_lifetime"]=decoder_lifetime
    dialog = VideoGenerationDialog(state.jobs, ModelResourceRegistry(MODEL_RESOURCES_PATH),
        state.settings, USER_DATA_DIR / "video_generation", parent=host,**runtime_options)
    if initial_request is not None:
        if source_path is not None:
            dialog.deleteLater()
            raise ValueError("Choose either a saved video draft or a new source image.")
        try:
            dialog._restore_draft(initial_request)
        except Exception:
            dialog.deleteLater()
            raise

    ingest_pending = False

    def open_ingest(admit, *, retained, completed=None):
        """Activate the lazy page, then admit only while that navigation survives."""
        nonlocal ingest_pending
        if ingest_pending or dialog._closed or dialog._shutdown_requested:
            return
        if window is None or not hasattr(window, "navigation"):
            dialog.status.setText(f"{retained} Ingest is unavailable; retry when it is available.")
            return
        previous = window.navigation.currentRow()
        ingest_pending = True
        try:
            window.navigation.setCurrentRow(1)
        except Exception as exc:
            ingest_pending = False
            dialog.status.setText(f"{retained} Ingest could not open: {exc}")
            return

        def start():
            nonlocal ingest_pending
            try:
                if dialog._closed or dialog._shutdown_requested:
                    return
                if window.navigation.currentRow() != 1:
                    raise ValueError("Ingest did not open or navigation changed. Retry the handoff when ready.")
                page = getattr(window, "ingest_page", None)
                if page is None:
                    raise ValueError("Ingest is still opening. Retry the handoff when ready.")
                if getattr(page, "_batch_task", None) is not None:
                    raise ValueError("Ingest is busy. Finish its current action, then retry.")
                if not admit(page):
                    raise ValueError("Ingest could not start this preview. Check its folder or finish its current action, then retry.")
                if completed is not None:
                    completed()
            except Exception as exc:
                if window.navigation.currentRow() == 1 and previous >= 0:
                    window.navigation.setCurrentRow(previous)
                dialog.status.setText(f"{retained} {exc}")
            finally:
                ingest_pending = False
        QTimer.singleShot(0, start)

    def ingest(payload):
        if ingest_pending or dialog._busy():
            return
        receipt_path = Path(payload["receipt_path"])
        source_path = Path(payload["source_path"])
        expected_path = receipt_path.absolute()
        supplied_verifier = payload.get("verified_outputs")
        expected_receipt = deepcopy(payload.get("receipt"))
        service = payload.get("service")

        def verified_outputs(check_path):
            if Path(check_path) != expected_path:
                raise ValueError("The selected video receipt differs from the verified handoff.")
            if callable(supplied_verifier):
                return supplied_verifier(check_path)
            return service.verified_video_outputs(deepcopy(expected_receipt))

        def admit(page):
            method = getattr(page, "_start_video_ingest_preview", None)
            if not callable(method):
                raise ValueError("The Ingest video handoff is unavailable.")
            return method(receipt_path, source_path, verified_outputs)
        open_ingest(admit, retained="Verified video remains retained.")

    def detached():
        if getattr(host, "_video_generation_dialog", None) is dialog:
            host._video_generation_dialog = None

    dialog.ingest_requested.connect(ingest)
    def ingest_frame(payload):
        if ingest_pending or dialog._busy() or dialog._closed or dialog._shutdown_requested:
            return
        origin = window.navigation.currentRow() if window is not None else None
        path = Path(payload["path"]).absolute()
        verify_handoff = payload["verify_handoff"]
        def check():
            verify_handoff()
            return path
        def checked(verified_path):
            if window is not None and window.navigation.currentRow() != origin:
                dialog.status.setText("The extracted frame remains saved. Navigation changed while verifying it; retry Keep Frame in Ingest when ready.")
                return
            def admit(page):
                method = getattr(page, "_start_ingest_import_preview", None)
                if not callable(method):
                    raise ValueError("The Ingest image handoff is unavailable.")
                return method("files", [verified_path], preserve_source=True)
            def completed():
                dialog.status.setText("Frame and exact video lineage opened in Ingest for review. The original video and extracted PNG remain saved.")
                if dialog._finishing_dialog is not None:
                    dialog._finishing_dialog.close()
            open_ingest(admit, retained="The extracted frame remains saved.", completed=completed)
        dialog._run("Verify extracted frame for Ingest", check, checked)
    dialog.frame_ingest_requested.connect(ingest_frame)
    dialog.workspace_closed.connect(detached)
    host._video_generation_dialog = dialog
    if window is not None and hasattr(window, "generation_page"):
        window.navigation.setCurrentRow(7)
        window.generation_page.add_workspace(dialog,
                                             label=(draft_label or "Video Draft") if initial_request is not None
                                             else "Animate Image" if source_path is not None else "New Video",
                                             task="video",
                                             media_type="video")
    else:
        dialog.show()
    if source_path is not None:
        dialog.task_choice.setCurrentIndex(dialog.task_choice.findData("i2v"))
        dialog.attach_source(Path(source_path), on_attached=on_opened,
                             frame_reference_provider=frame_reference_provider)
    elif callable(on_opened):
        on_opened(dialog)
    return dialog
