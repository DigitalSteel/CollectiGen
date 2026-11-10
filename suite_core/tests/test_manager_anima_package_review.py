"""Synthetic Applications dispatch for Manager's saved Anima setup review."""
from datetime import datetime, timezone
from types import SimpleNamespace

from PySide6.QtWidgets import QFileDialog

from suite_core.manager.krea_training_review import KreaTrainingReviewDialog
from suite_core.manager.window import create_window, unconfigured


def test_training_package_review_binds_saved_anima_setup_to_ready_session(
        qt_test_scope, tmp_path, monkeypatch):
    settings = tmp_path / "settings.json"
    settings.write_bytes(b"{}")
    session = SimpleNamespace(
        state="ready", admitted=SimpleNamespace(layout=SimpleNamespace(data_root=tmp_path)))
    manager = create_window(
        unconfigured(datetime.now(timezone.utc)), normal_entry_session=session,
        training_draft_settings_path=settings)
    qt_test_scope.show(manager, size=(1920, 1080))
    manager.navigation.setCurrentRow(manager.page_rows["Applications"])
    qt_test_scope.settle()
    observed = []

    def review(dialog):
        ready = dialog._anima_review_allowed
        accepted = ready() if ready else None
        session.state = "closed"
        try:
            observed.append((dialog._anima_settings_path, accepted, ready() if ready else None))
        finally:
            session.state = "ready"
        return 0

    monkeypatch.setattr(QFileDialog, "getExistingDirectory", lambda *args: str(tmp_path))
    monkeypatch.setattr(KreaTrainingReviewDialog, "exec", review)
    assert manager.krea_training_review_button.isVisible()
    manager.krea_training_review_button.click()
    assert observed == [(settings, True, False)]
    assert settings.read_bytes() == b"{}"
    assert manager.close()
