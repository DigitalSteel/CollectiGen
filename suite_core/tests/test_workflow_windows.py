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
