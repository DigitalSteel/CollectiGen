"""Manager exposes finite application management while embedded execution stays absent."""
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest
from PySide6.QtWidgets import QApplication

from suite_core.manager.workflow_applications_ui import WorkflowApplications


@pytest.fixture
def app():
    return QApplication.instance() or QApplication([])


def test_unqualified_modules_have_honest_cards_and_no_download_or_launch(app):
    widget = WorkflowApplications()
    try:
        assert set(widget.rows) == {'collectigen.generate', 'collectigen.train'}
        assert not widget.services and not widget.panels
        for component_id, (status, opening, manage, _group) in widget.rows.items():
            assert 'No installable release is available' in status.text()
            assert not opening.isEnabled() and not manage.isEnabled()
            widget.open_application(component_id)
            widget.manage(component_id)
        assert widget.close_ready()
    finally:
        widget.timer.stop()
        widget.deleteLater()


def test_application_filter_preserves_finite_categories_and_unknown_identity_refusal(app):
    widget = WorkflowApplications()
    try:
        widget.set_filter('train', 'In development', 'AI Training')
        assert widget.rows['collectigen.generate'][3].isHidden()
        assert not widget.rows['collectigen.train'][3].isHidden()
        widget.set_filter('', 'Installed', 'All categories')
        assert all(group.isHidden() for _status, _open, _manage, group in widget.rows.values())
        with pytest.raises(ValueError, match='finite_workflow'):
            widget.open_application('collectigen.unknown')
    finally:
        widget.timer.stop()
        widget.deleteLater()


def test_manager_navigation_and_old_commands_route_only_to_applications(app):
    from suite_core.manager.window import create_window, unconfigured
    window = create_window(unconfigured(datetime.now(timezone.utc)))
    try:
        assert 'Generate' not in window.page_rows and 'Training' not in window.page_rows
        assert not hasattr(window, 'generation_page')
        assert not hasattr(window, 'training_panel')
        assert not hasattr(window, '_sdxl_preparation_controller')
        for route in (window.open_generate_plan, window.open_video_plan,
                      window.open_training_history, window.open_krea_training_review):
            route()
            assert window.navigation.currentRow() == window.page_rows['Applications']
        assert not hasattr(window, 'state')
    finally:
        window.close()
        app.processEvents()


def test_entry_lifetime_retains_workflow_owner_until_its_exact_drain():
    from suite_core.manager.__main__ import EntryLifetimeOwner
    owner = EntryLifetimeOwner()
    pending = [True]
    original = SimpleNamespace(begin_close=lambda: not pending[0])
    owner.workflow_workspace_owner = original
    assert owner._drain_workflows() is False
    assert owner.workflow_workspace_owner is original
    pending[0] = False
    assert owner._drain_workflows() is True
    assert owner.workflow_workspace_owner is None


def test_entry_lifetime_does_not_drop_completed_but_undrained_executor():
    from suite_core.manager.__main__ import EntryLifetimeOwner
    owner = EntryLifetimeOwner()
    executor = SimpleNamespace(close_held=True, begin_close=lambda: None)
    owner.workflow_services = [('collectigen.generate', object(), executor)]
    assert owner._drain_workflows() is False
    assert owner.workflow_services[0][2] is executor
    executor.close_held = False
    assert owner._drain_workflows() is True


@pytest.mark.parametrize('available', [True, False])
def test_admitted_card_names_installed_version_and_preserves_restart_guidance(app, available):
    """Presentation-only injection; no service/admission/process is exercised."""
    from PySide6.QtWidgets import QLabel, QDialog
    widget = WorkflowApplications()
    widget.timer.stop()
    name = 'collectigen.generate'
    widget.owner = SimpleNamespace(close_held=False, pending_generate_draft=None, failure_reference=None,
        session=SimpleNamespace(workflow_selections={name:SimpleNamespace(
            selected=SimpleNamespace(version='0.1.0-alpha.1'))}))
    widget.services[name] = (SimpleNamespace(launch_available=available), None)
    widget.panels[name] = SimpleNamespace(close_held=False, closing=False,
        status=QLabel('Find Generate' if available else
            'App files are saved. Restart Manager to admit the saved app before opening it.'),
        initial_status_text=lambda: 'Find Generate')
    widget.dialogs[name] = QDialog(widget)
    try:
        widget.render()
        status, opening, _manage, _group = widget.rows[name]
        assert opening.isEnabled() is available
        assert ('Generate 0.1.0-alpha.1 is installed. Ready to open.' if available else
            'App files are saved. Restart Manager to admit the saved app before opening it.') == status.text()
        if available:
            widget.panels[name].status.setText('Setup needs attention. Review the available recovery action.')
            widget.panels[name].close_held = True
            widget.render()
            assert 'Setup needs attention.' in status.text() and 'Ready to open.' not in status.text()
            assert not opening.isEnabled()
        widget.owner.close_held = True
        widget.owner.reason = 'The workflow has started. Its window may take a moment to appear.'
        widget.render()
        assert status.text() == widget.owner.reason and not opening.isEnabled()
    finally:
        widget.deleteLater()
