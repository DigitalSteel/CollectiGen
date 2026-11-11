"""Result review, Request and active-job guidance remain distinct."""
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QLabel
from core.background_jobs import BackgroundJobManager
from qt_app.generation_dialog import GenerationDialog
from test_manager_generator_setup_ui import application


def test_result_context_and_existing_active_job_guidance(tmp_path, monkeypatch):
    app = application()
    jobs = BackgroundJobManager()
    dialog = GenerationDialog(jobs, storage_root=tmp_path / "generation",
        runtime_allowed=False, review_verified_results=True, checked_image_submission=True,
        checked_connection=("http://127.0.0.1:7801", "generated-server"),
        checked_connection_provider=lambda: ("http://127.0.0.1:7801", "generated-server"))
    dialog.show()
    try:
        QTest.mouseClick(dialog.tabs.tabBar(), Qt.LeftButton, pos=dialog.tabs.tabBar().tabRect(3).center())
        assert dialog.workflow_heading.text() == "Review a verified image for Organizer"
        text = " ".join(label.text() for label in dialog.workflow_context.findChildren(QLabel))
        assert "Refresh Recent Jobs" in text and "Enter an image prompt" not in text
        with monkeypatch.context() as scope:
            scope.setattr(dialog, "_execution_active", lambda: True)
            dialog._update_workflow_context(prepared_valid=False, active=True)
            text = " ".join(label.text() for label in dialog.workflow_context.findChildren(QLabel))
            assert "Image job in progress" in text
            assert dialog.workflow_heading.text() == "Track image generation work"
        QTest.mouseClick(dialog.tabs.tabBar(), Qt.LeftButton, pos=dialog.tabs.tabBar().tabRect(0).center())
        assert "Plan an image" in dialog.workflow_heading.text()
    finally:
        dialog.close(); jobs.shutdown(); app.processEvents()
