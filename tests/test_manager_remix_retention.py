"""Synthetic retained Remix routes; no generator, real library or desktop input."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from datetime import datetime, timezone
from types import SimpleNamespace as NS
from pathlib import Path
import pytest
from PIL import Image
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton
from test_manager_generator_setup_ui import application, wait_until, capture
from suite_core.manager.window import create_window, unconfigured
from qt_app.generation_workflow import open_generation_workspace, _source_identity
from core.generation_planning import load_plan_with_digest


@pytest.mark.parametrize('route', ['file', 'cancel', 'verified', 'changed'])
def test_remix_retains_original_saved_draft(tmp_path, monkeypatch, route):
    app = application()
    session = NS(state='ready', admitted=NS(layout=NS(data_root=tmp_path)))
    window = create_window(unconfigured(datetime.now(timezone.utc)), normal_entry_session=session)
    window.resize(1920, 1040); window.show()
    window.generation_state()
    open_generation_workspace(window, force_new=True)
    wait_until(app, lambda: getattr(window, '_generation_dialog', None) is not None)
    original = window._generation_dialog
    wait_until(app, lambda: not original._pending)
    QTest.keyClicks(original.positive, 'Original ceramic cup draft')
    wait_until(app, lambda: original._draft_path.exists() and not original._pending
        and load_plan_with_digest(original._draft_path)[0].request.positive_prompt == 'Original ceramic cup draft', timeout=30)
    before = original._draft_path.read_bytes()
    image = tmp_path / 'synthetic-result.png'
    Image.new('RGB', (64, 64), '#2288aa').save(image)
    image_before = image.read_bytes()
    window.show_generate_workspace()
    errors = []
    monkeypatch.setattr('qt_app.generation_workflow.QMessageBox.information', lambda *args: errors.append(str(args[-1])))
    try:
        if route in {'verified', 'changed'}:
            evidence = _source_identity((image,))['collection'][0]
            # A synthetic already-verified result supplies the existing authority;
            # source preparation and all stale checks below are production code.
            original.result_action_requested.emit({'action': 'remix', 'paths': (image,),
                'verified_source': lambda: (image,), 'verified_source_evidence': evidence})
            assert 'In Generate' in original.status.text()
            assert 'Close this draft' not in original.status.text()
            pending = window._verified_result_remix
            if route == 'changed':
                Image.new('RGB', (64, 64), '#cc1144').save(image)
            QTest.mouseClick(window.verified_result_remix_button, Qt.LeftButton)
        else:
            monkeypatch.setattr('PySide6.QtWidgets.QFileDialog.getOpenFileName',
                lambda *args, **kwargs: ('' if route == 'cancel' else str(image), 'Images'))
            button = next(b for b in window.findChildren(QPushButton) if b.text().startswith('Plan a Remix'))
            assert button.isVisible() and button.isEnabled()
            QTest.mouseClick(button, Qt.LeftButton)
        wait_until(app, lambda: getattr(window, '_generation_source_task', None) is None)
        if route in {'file', 'verified'}:
            wait_until(app, lambda: len(window.generation_page.retained_workspaces()) == 2)
            new = window.generation_page.active_workspace()
            wait_until(app, lambda: not new._pending and new._draft_path.exists(), timeout=30)
            assert new is not original and new.source_image_bytes == image_before
            assert new._draft_path != original._draft_path
            assert new.source_current_provider()['collection'][0]['image_sha256'] == _source_identity((image,))['collection'][0]['image_sha256']
            if route == 'verified':
                assert window._verified_result_remix is None
                assert not window.verified_result_remix_button.isVisible()
            capture(window, 'remix-retained-' + route)
            window.generation_page.show_workspace(original)
            assert original.positive.toPlainText() == 'Original ceramic cup draft'
        else:
            assert window.generation_page.retained_workspaces() == (original,)
            if route == 'changed':
                assert errors and 'changed' in errors[-1]
                assert window._verified_result_remix is pending
                assert window.verified_result_remix_button.isVisible()
            else:
                assert not errors
        assert original._draft_path.read_bytes() == before
        assert original.positive.toPlainText() == 'Original ceramic cup draft'
        if route != 'changed': assert image.read_bytes() == image_before
    finally:
        for draft in tuple(window.generation_page.retained_workspaces()): draft.close()
        app.processEvents(); window.close(); app.processEvents()
