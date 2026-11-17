"""Module entry refusal and original controller route composition; no native authority."""
from types import SimpleNamespace

import pytest


@pytest.mark.parametrize('component', ['generate', 'train'])
def test_module_cli_alone_cannot_create_workspace_or_application(component, monkeypatch, capsys):
    from importlib import import_module
    entry = import_module('suite_core.' + component + '.__main__')
    assert entry.main(['--profile-root', 'untrusted', '--runtime-profile', component]) == 2
    assert 'Manager Applications' in capsys.readouterr().err


@pytest.mark.parametrize('component', ['generate', 'train'])
def test_fake_normal_session_cannot_open_module_or_read_settings(component, monkeypatch):
    from importlib import import_module
    from core.settings import AppSettings
    from suite_core.workflow_modules.window import require_session
    calls = []
    monkeypatch.setattr(AppSettings, 'load', lambda *args, **kwargs: calls.append(args))
    forged = SimpleNamespace(component_id='collectigen.' + component, state='ready',
                             assert_ready=lambda: None)
    with pytest.raises(ValueError, match='genuine_matching_module_session_required'):
        require_session(forged, 'collectigen.' + component)
    with pytest.raises(ValueError, match='genuine_matching_module_session_required'):
        import_module('suite_core.' + component + '.__main__').main(session=forged)
    assert not calls


def test_cancelled_editor_close_keeps_original_controller_and_does_not_shutdown():
    from suite_core.workflow_modules.controllers import WorkflowController
    calls = []
    controller = WorkflowController.__new__(WorkflowController)
    original = SimpleNamespace(confirm_editor_close=lambda: False)
    controller.window = SimpleNamespace(_sdxl_preparation_controller=original)
    controller.modal_depth = 0
    controller.closing = False
    assert controller.confirm_close() is False
    assert controller.window._sdxl_preparation_controller is original
    assert controller.closing is False and calls == []


def test_local_drain_waits_for_original_training_owner_and_decoder():
    from suite_core.workflow_modules.controllers import WorkflowController
    controller = WorkflowController.__new__(WorkflowController)
    training = SimpleNamespace(has_active_background_work=lambda: False, dialogs={object()})
    controller.window = SimpleNamespace(_sdxl_preparation_controller=training)
    controller.closing = True
    controller.modal_depth = 0
    controller.session = SimpleNamespace(worker_capabilities=None)
    controller.state = SimpleNamespace(video_decoder_lifetime=SimpleNamespace(drained=True),
        jobs=SimpleNamespace(active=lambda: [], shutdown=lambda **kwargs: True))
    assert controller.drained() is False
    training.dialogs.clear()
    assert controller.drained() is True
    controller.state.video_decoder_lifetime.drained = False
    assert controller.drained() is False
    controller.state.video_decoder_lifetime.drained = True
    controller.session.worker_capabilities = SimpleNamespace(close_held=True)
    assert controller.drained() is False


def test_training_handlers_keep_mutating_history_recovery_and_dataset_recheck_routes():
    # These are the actual extracted callable routes bound by the module surface.
    from suite_core.workflow_modules.controllers import WorkflowMethods
    for name in ('open_training_history', 'review_interrupted_training_package',
                 '_review_training_package_receipt', 'open_training_draft',
                 'bind_training_draft_recheck', 'open_krea_training_review',
                 'open_sdxl_preparation', 'open_existing_trainer_setup',
                 'open_training_comparison', 'open_training_experiments'):
        assert callable(getattr(WorkflowMethods, name))


@pytest.mark.parametrize('recover,target', [(False, 'video-settings'), (True, 'video-recovery')])
def test_video_software_management_returns_to_manager(recover, target):
    from suite_core.workflow_modules.window import WorkflowWindow
    calls = []
    owner = SimpleNamespace(request_manager_return=lambda value: calls.append(value))
    WorkflowWindow.setup_video_components(owner, recover=recover)
    assert calls == [target]


def test_trainer_software_management_returns_to_manager():
    from suite_core.workflow_modules.window import WorkflowWindow
    calls = []
    owner = SimpleNamespace(request_manager_return=lambda value: calls.append(value))
    WorkflowWindow.open_existing_trainer_setup(owner)
    assert calls == ['trainer-settings']


@pytest.mark.parametrize('outcome', ['saved', 'save-failed', 'cancel-invalid'])
def test_ordinary_close_waits_for_draft_save_and_preserves_refusal(tmp_path, monkeypatch, outcome):
    """Real original UI/save/drain; one Close must resume only on saved success."""
    from threading import Event
    from PySide6.QtCore import Qt, QTimer
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication, QMessageBox
    from core.generation_planning import load_plan
    from suite_core.tests.workflow_ui_fixtures import workflow_window
    from test_manager_generator_setup_ui import wait_until

    app, session, window = workflow_window(tmp_path, 'generate')
    QTest.mouseClick(window.surface.new, Qt.LeftButton)
    wait_until(app, lambda: getattr(window, '_generation_dialog', None) is not None)
    owner = window._generation_dialog
    wait_until(app, lambda: not owner.has_active_background_work())
    owner._draft_timer.stop()  # Close, rather than periodic autosave, owns this save.
    owner.positive.setPlainText('Synthetic single Close retains the exact blue paper boat request.')
    original_save = owner._save_path
    expected = owner.plan.to_dict()
    entered, release = Event(), Event()
    def controlled_save(*args):
        entered.set()
        assert release.wait(10), 'Test must settle its original save worker'
        if outcome == 'save-failed':
            raise OSError('Synthetic guarded save failure')
        return original_save(*args)
    monkeypatch.setattr(owner, '_save_path', controlled_save)
    try:
        if outcome == 'cancel-invalid':
            owner.advanced_json.setPlainText('{')
            choices = []
            def cancel_original_warning():
                boxes = [x for x in QApplication.topLevelWidgets()
                         if isinstance(x, QMessageBox) and x.windowTitle() == 'Unsaved Image Draft']
                if boxes:
                    choices.append(True)
                    QTest.mouseClick(boxes[0].button(QMessageBox.Cancel), Qt.LeftButton)
            timer = QTimer(); timer.timeout.connect(cancel_original_warning); timer.start(10)
            try: window.close()
            finally: timer.stop()
            assert choices and not entered.is_set()
            assert owner.advanced_json.toPlainText() == '{'
        else:
            window.close()  # Exactly one ordinary Close; no helper retry.
            wait_until(app, entered.is_set)
            assert window.isVisible() and session.state == 'ready'
            release.set()
            wait_until(app, lambda: not owner.has_active_background_work())
            if outcome == 'saved':
                wait_until(app, lambda: not window.isVisible())
                assert load_plan(owner._draft_path).to_dict() == expected
                assert [kind for kind, _ in session.wire.sent].count('workflow-closing') == 1
                assert [kind for kind, _ in session.wire.sent].count('workflow-drained-report') == 1
                return
            assert owner._draft_save_error
        # Failed save and explicit Cancel must never acquire shutdown authority.
        assert window.isVisible() and session.state == 'ready'
        assert not any(kind == 'workflow-closing' for kind, _ in session.wire.sent)
    finally:
        release.set()
        monkeypatch.setattr(owner, '_save_path', original_save)
        if window.isVisible():
            wait_until(app, lambda: not owner.has_active_background_work())
            if outcome == 'cancel-invalid':
                import json
                owner.advanced_json.setPlainText(json.dumps(owner.plan.to_dict()))
            owner._draft_save_error = False
            window.close()
            wait_until(app, lambda: not window.isVisible())
