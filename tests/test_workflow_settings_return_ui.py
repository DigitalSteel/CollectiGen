"""Logical app/settings/app return over saved drafts; synthetic IPC, never native qualification."""
from datetime import datetime, timezone
from pathlib import Path
from time import monotonic
from types import SimpleNamespace as NS

from PySide6.QtCore import Qt, QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QPushButton

from core.generation_module_handoff import read_reviewed_handoff
from suite_core.tests.workflow_ui_fixtures import workflow_window
from suite_core.manager.window import create_window, unconfigured
from suite_core.manager.description_setup_ui import DescriptionSetupDialog
from suite_core.manager.workflow_applications_ui import WorkflowReturnBar
from test_manager_generator_setup_ui import application, wait_until


def request_saved_return(app, session, window, button, target):
    """Actual visible request; wait for local owners and inspect the exact frame."""
    QTest.mouseClick(button, Qt.LeftButton)
    try:
        wait_until(app, lambda: not window.isVisible(), timeout=30)
    except AssertionError as error:
        owner = getattr(window, '_generation_dialog', None)
        raise AssertionError(dict(surface=window.surface.status.text(),
            draft_status=None if owner is None else owner.status.text(),
            return_pending=window._return_save_pending,
            target=window._return_target)) from error
    assert session.local_drain_reported
    returns = [payload for kind, payload in session.wire.sent if kind == 'workflow-return']
    assert len(returns) == 1 and returns[0]['target'] == target
    descriptor = returns[0]['resume_draft']
    saved, path, token = read_reviewed_handoff(session.workspace_root, descriptor)
    assert token == descriptor['sha256'] and path.is_file()
    # The fake channel tests UI/state composition only. Root's actual native
    # process/Job/output drain and transfer authority have separate tests.
    assert session.lease_release_authorized is False
    return descriptor, saved, path


def resumed_generate(root, descriptor, *, configure_session=None):
    app, session, window = workflow_window(root, 'generate', initial_draft=descriptor,
        configure_session=configure_session)
    wait_until(app, lambda: getattr(window, '_generation_dialog', None) is not None, timeout=30)
    owner = window._generation_dialog
    wait_until(app, lambda: not owner._pending, timeout=30)
    return app, session, window, owner


def wait_restored_review(app, owner, expected_name):
    def restored():
        review = getattr(owner, '_image_setup_dialog', None)
        return (review is not None and review._source_preview_task is None
                and review.name.text() == expected_name)
    wait_until(app, restored, timeout=30)
    return owner._image_setup_dialog


def manager_for_return(root, *, caption_runtime=None):
    app = application()
    session = NS(state='ready', admitted=NS(layout=NS(data_root=Path(root) / 'Suite/Data')),
                 caption_runtime=caption_runtime)
    window = create_window(unconfigured(datetime.now(timezone.utc)), normal_entry_session=session)
    window.resize(1920, 1040); window.show()
    item = window.navigation.item(window.page_rows['Applications'])
    QTest.mouseClick(window.navigation.viewport(), Qt.LeftButton,
                    pos=window.navigation.visualItemRect(item).center())
    app.processEvents()
    return app, session, window


def management_button(window, text):
    button, = [button for button in window.workflow_management.findChildren(QPushButton)
               if button.text().casefold().startswith(text.casefold())]
    assert button.isVisible() and button.isEnabled()
    return button


def description_settings(manager, manager_session, *, outcome='save', directory=None,
                         on_open=None):
    app = application()
    seen = []; errors = []; deadline = monotonic() + 20
    timer = QTimer(); timer.setInterval(20)
    def drive():
        child = manager._generation_setup_dialog
        if not isinstance(child, DescriptionSetupDialog):
            if monotonic() >= deadline:
                errors.append(AssertionError('The canonical Manager Description settings did not open.'))
                timer.stop()
            return
        timer.stop()
        try:
            assert child.isVisible()
            if on_open is not None:
                on_open(child)
            if directory is not None:
                child.directory.setText(str(directory))
            child.controls['joycaption_max_new_tokens'].setValue(512)
            # Manager's own unfinished settings review still holds Close.
            assert manager.close() is False and manager.isVisible() and child.isVisible()
            if outcome == 'ended_session':
                manager_session.state = 'ended'
                QTest.mouseClick(child.save_button, Qt.LeftButton)
                assert child.isVisible() and child.saved_snapshot is None
                QTest.mouseClick(child.cancel_button, Qt.LeftButton)
            elif outcome == 'cancel':
                QTest.mouseClick(child.cancel_button, Qt.LeftButton)
            else:
                QTest.mouseClick(child.save_button, Qt.LeftButton)
                assert not child.isVisible(), child.status.text()
            seen.append(child.saved_snapshot)
        except BaseException as error:
            errors.append(error); child.reject()
    timer.timeout.connect(drive); timer.start()
    try:
        QTest.mouseClick(management_button(manager, 'Description settings'), Qt.LeftButton)
    finally:
        timer.stop()
        # Only injected test conditions are reset; no real native grant resumes.
        manager_session.state = 'ready'
    assert not errors, errors
    assert len(seen) == 1
    return seen[0]


def close_component_window(app, window):
    review = getattr(getattr(window, '_generation_dialog', None), '_image_setup_dialog', None)
    if review is not None:
        review.reject()
    state = getattr(window, 'state', None)
    if state is not None:
        wait_until(app, lambda: not state.jobs.active(), timeout=30)
    window.close(); app.processEvents()
    if window.isVisible():
        window.close(); app.processEvents()
    wait_until(app, lambda: not window.isVisible(), timeout=30)


def test_return_bar_uses_visible_applications_navigation_without_launching():
    app = application(); requested = []
    bar = WorkflowReturnBar(lambda: requested.append('applications'))
    bar.show(); app.processEvents()
    button, = bar.findChildren(QPushButton)
    QTest.mouseClick(button, Qt.LeftButton)
    assert requested == ['applications']
    bar.close()


def test_failed_return_save_keeps_dirty_draft_and_sends_no_navigation(tmp_path, monkeypatch):
    app, session, window = workflow_window(tmp_path, 'generate')
    QTest.mouseClick(window.surface.new, Qt.LeftButton)
    wait_until(app, lambda: getattr(window, '_generation_dialog', None) is not None)
    owner = window._generation_dialog
    wait_until(app, lambda: not owner._pending)
    QTest.keyClicks(owner.positive, 'A retained draft before failed settings return')
    wait_until(app, lambda: not owner._pending and owner._draft_path.exists(), timeout=30)
    before = owner._draft_path.read_bytes()
    def fail(*args, **kwargs):
        raise OSError('Synthetic guarded save failure')
    monkeypatch.setattr(owner, '_save_path', fail)
    QTest.mouseClick(owner.review_connection_button, Qt.LeftButton)
    wait_until(app, lambda: not window._return_save_pending and not owner._pending)
    assert window.isVisible() and not owner._closed
    assert owner.positive.toPlainText() == 'A retained draft before failed settings return'
    assert owner._draft_path.read_bytes() == before
    assert not any(kind == 'workflow-return' for kind, _payload in session.wire.sent)
    monkeypatch.undo()
    close_component_window(app, window)


def test_imported_review_cancel_reads_no_saved_source(tmp_path, monkeypatch):
    from PIL import Image
    from suite_core.tests.workflow_ui_fixtures import trigger_more
    from test_manager_description_setup_return_ui import open_image_setup
    app, session, window = workflow_window(tmp_path, 'generate')
    source = tmp_path / 'synthetic-original.png'
    Image.new('RGB', (20, 20), 'blue').save(source)
    owner, review = open_image_setup(window, app, source, monkeypatch)
    window._description_test_watchdog.stop()
    review.name.setText('Keep imported unfinished choices')
    descriptor, saved, path = request_saved_return(app, session, window,
        review.review_description_setup_button, 'description-settings')
    before = path.read_bytes()
    # An imported/manual draft locator carries no authenticated live-review
    # source read authority. Reconnecting its image remains a separate choice.
    manual_descriptor = {key: value for key, value in descriptor.items()
                         if key != 'resume_image_review'}
    reads = []; selections = []
    def unexpected_read(*args, **kwargs):
        reads.append((args, kwargs))
        raise AssertionError('Canceled reconnect must not inspect saved source pixels')
    def cancel_selection(*args, **kwargs):
        selections.append((args, kwargs))
        return '', ''
    monkeypatch.setattr('qt_app.generation_workflow._source_identity', unexpected_read)
    monkeypatch.setattr('PySide6.QtWidgets.QFileDialog.getOpenFileName', cancel_selection)
    app, resumed_session, resumed, loaded = resumed_generate(tmp_path, manual_descriptor)
    assert getattr(loaded, '_image_setup_dialog', None) is None and reads == []
    trigger_more(resumed, 'Resume Image Setup Review…')
    assert len(selections) == 1 and selections[0][0][2] == ''
    assert reads == [] and getattr(loaded, '_image_setup_dialog', None) is None
    assert loaded.plan.target_context['unfinished_image_setup_review'] == saved.target_context['unfinished_image_setup_review']
    assert path.read_bytes() == before
    close_component_window(app, resumed)
