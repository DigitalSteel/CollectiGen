"""Manager Models parity using disposable profiles only; no network or model runtime."""
import os
os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")
from datetime import datetime, timezone
from pathlib import Path
import time
from types import SimpleNamespace as NS
import pytest
from PySide6.QtCore import Qt, QTimer
from PySide6.QtGui import QFont, QFontDatabase
from PySide6.QtWidgets import QApplication, QDialog, QLabel
from PySide6.QtTest import QTest
from suite_core.manager.window import create_window, unconfigured
from suite_core.manager.model_workspace_host import ModelsWorkspaceHost


def settle(predicate):
    end = time.monotonic() + 20
    while time.monotonic() < end:
        QApplication.processEvents()
        if predicate(): return
        time.sleep(.005)
    assert predicate()


def manager(tmp_path, admitted=True):
    application = QApplication.instance() or QApplication([])
    for name in ("segoeui.ttf", "segoeuib.ttf"):
        assert QFontDatabase.addApplicationFont(str(Path(os.environ.get("WINDIR", "C:/Windows")) / "Fonts" / name)) >= 0
    application.setFont(QFont("Segoe UI", 10))
    application.setStyle("Fusion")
    session = NS(state="ready", admitted=NS(layout=NS(data_root=tmp_path))) if admitted else None
    host = ModelsWorkspaceHost(tmp_path / "Manager/generation-settings.json")
    window = create_window(unconfigured(datetime.now(timezone.utc)), normal_entry_session=session,
                           model_workspace_factory=host)
    window.resize(1500, 960)
    window.show()
    return application, window, host


def test_visible_local_models_entry_and_close(tmp_path):
    application, window, host = manager(tmp_path)
    before = set(tmp_path.rglob("*"))
    captures = os.environ.get("COLLECTIGEN_MODELS_CAPTURE_DIR")
    if captures: Path(captures).mkdir(parents=True, exist_ok=True)
    errors = []
    def review():
        try:
            dialog = host.dialog
            assert dialog is not None and dialog.isVisible()
            settle(lambda: dialog._task is None)
            assert not dialog.repository_review_only
            assert dialog.registry.path == tmp_path / "Manager/model_resources.json"
            assert dialog.windowTitle() == "Manage local models"
            assert not window.state.generation_runtime_allowed
            assert dialog._active_ribbon_group == "Local Models"
            if captures:
                Path(captures).mkdir(parents=True, exist_ok=True)
                dialog.resize(1500, 960)
                application.processEvents()
                dialog.grab().save(str(Path(captures) / "local-models.png"))
            QTest.keyClick(dialog, Qt.Key_Escape)
        except BaseException as exc:
            errors.append(exc)
            if host.dialog: host.dialog.reject()
    QTimer.singleShot(100, review)
    item = window.navigation.item(window.page_rows["Applications"])
    QTest.mouseClick(window.navigation.viewport(), Qt.LeftButton,
                    pos=window.navigation.visualItemRect(item).center())
    if captures:
        application.processEvents()
        window.grab().save(str(Path(captures) / "manager-models-entry.png"))
    QTest.mouseClick(window.local_models_button, Qt.LeftButton)
    assert not errors, errors
    assert host.dialog is None
    assert not (tmp_path / "Manager/model_resources.json").exists()
    assert set(tmp_path.rglob("*")) == before
    host.begin_close(); settle(host.poll_close)
    window.close(); application.processEvents()


def test_unadmitted_models_disabled(tmp_path):
    app, window, host = manager(tmp_path, admitted=False)
    assert not window.local_models_button.isEnabled()
    window.open_local_models()
    assert host.dialog is None and host.jobs is None
    window.close(); app.processEvents()


def test_repository_mode_stays_separate_and_retained_review_is_not_replaced(tmp_path):
    app, window, host = manager(tmp_path)
    dialog = host.open_local_models(window)
    settle(lambda: dialog._task is None)
    with pytest.raises(ValueError, match="retained Models"):
        host(window)
    assert host.open_local_models(window) is dialog
    dialog.reject(); host.dialog_returned()
    repository = host(window)
    settle(lambda: repository._task is None)
    assert repository.repository_review_only
    repository.reject(); host.dialog_returned()
    host.begin_close(); settle(host.poll_close)
    window.close(); app.processEvents()


@pytest.mark.parametrize("mode", ["success", "registry", "catalog", "busy", "late_catalog"])
def test_guidance_new_draft_recheck_and_retention(tmp_path, monkeypatch, mode):
    from qt_app import generation_workflow
    app, window, host = manager(tmp_path)
    dialog = host.open_local_models(window)
    settle(lambda: dialog._task is None)
    snapshot, revision = dialog.registry.snapshot()
    service = NS(catalog_write_token=lambda: "changed" if mode == "catalog" else "token")
    guidance, configuration = object(), object()
    dialog.generation_handoff = (guidance, configuration, "stale" if mode == "registry" else revision, service, "token")
    dialog._generation_handoff_ready = True
    calls = []
    target = NS(status=QLabel(), apply_model_guidance=lambda *a, **k: calls.append((a,k)) or True)
    def opened(parent, *, force_new, on_opened):
        assert parent is window and force_new is True
        if mode == "busy": return
        if mode == "late_catalog": service.catalog_write_token = lambda: "changed"
        on_opened(target)
    monkeypatch.setattr(generation_workflow, "open_generation_workspace", opened)
    result = window.continue_models_generation(dialog)
    if mode == "success":
        assert result and len(calls) == 1
        assert calls[0] == ((guidance, configuration), {"expected_registry_revision": revision})
        assert dialog.generation_handoff is None
    elif mode == "late_catalog":
        assert result and not calls and "not changed" in target.status.text()
    else:
        assert not result and not calls
        assert dialog.generation_handoff is not None
        assert not dialog._generation_handoff_ready
        assert "Return to Models" in window.statusBar().currentMessage()
    dialog._generation_handoff_ready = False
    dialog.reject(); host.dialog_returned()
    host.begin_close(); settle(host.poll_close)
    window.close(); app.processEvents()


@pytest.mark.parametrize("gate", ["ready", "unadmitted", "closing", "demo", "disposable"])
def test_entry_factory_requires_ready_ordinary_session_and_stays_lazy(tmp_path, gate):
    from suite_core.manager.model_workspace_host import factory_for_entry
    session = None if gate == "unadmitted" else NS(state="closing" if gate == "closing" else "ready",
                                                 admitted=NS(layout=NS(data_root=tmp_path)))
    host = factory_for_entry(None, session=session, demo=gate == "demo",
                             disposable_binding=object() if gate == "disposable" else None)
    if gate == "ready":
        assert type(host) is ModelsWorkspaceHost
        assert host.registry_path == tmp_path / "Manager/model_resources.json"
        assert host.jobs is None and host.dialog is None
    else: assert host is None
    assert not list(tmp_path.iterdir())
    explicit = object()
    assert factory_for_entry(explicit, session=session, demo=True) is explicit


def test_host_close_retains_physical_worker_until_drain(tmp_path):
    host = ModelsWorkspaceHost(tmp_path / "settings.json")
    class Jobs:
        done = False
        def active(self): return []
        def shutdown(self, *, wait):
            assert wait is False
            return self.done
    host.jobs = Jobs()
    host.begin_close()
    assert host.close_held
    host.cancel_close()
    assert host.reopen_after_drain
    host.jobs.done = True
    assert host.poll_close()
    host.cancel_close()
    assert host.jobs is None and not host.closing
