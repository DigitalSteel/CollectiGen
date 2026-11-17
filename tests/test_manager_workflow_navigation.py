"""Standalone workflows and Manager-only navigation; synthetic channel evidence."""
import os
os.environ.setdefault('QT_QPA_PLATFORM', 'offscreen')
from datetime import datetime, timezone
from types import SimpleNamespace as NS
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication
from suite_core.manager.window import create_window, unconfigured
from suite_core.tests.workflow_ui_fixtures import workflow_window


@pytest.fixture
def manager_window(tmp_path):
    app = QApplication.instance() or QApplication([])
    window = create_window(unconfigured(datetime.now(timezone.utc)))
    window.resize(1500, 960); window.show(); app.processEvents()
    yield app, window
    window.close(); app.processEvents()


@pytest.fixture
def generate_window(tmp_path):
    app, session, window = workflow_window(tmp_path, 'generate')
    yield app, window
    for draft in tuple(window.generation_page.retained_workspaces()): draft.close()
    window.close(); app.processEvents()


@pytest.fixture
def train_window(tmp_path):
    app, session, window = workflow_window(tmp_path, 'train')
    yield app, window
    window.close(); app.processEvents()


def test_visible_workflow_navigation_keeps_one_panel_and_retained_routes(manager_window, generate_window, train_window):
    app, manager = manager_window
    for index, name in enumerate(('Dashboard', 'Servers', 'Applications', 'Databases', 'Dependencies', 'Settings')):
        assert manager.page_rows[name] == index
    assert 'Generate' not in manager.page_rows and 'Training' not in manager.page_rows
    item = manager.navigation.item(manager.page_rows['Applications'])
    QTest.mouseClick(manager.navigation.viewport(), Qt.LeftButton, pos=manager.navigation.visualItemRect(item).center())
    app.processEvents()
    assert manager.stack.currentIndex() == manager.page_rows['Applications']
    for _app, module in (generate_window, train_window):
        assert module.centralWidget() is module.surface and module.surface.isVisible()
        assert not manager.isAncestorOf(module.surface)
        assert module.module_session.lease_release_authorized is False
        assert not module.state.jobs.active()
    assert not hasattr(manager, 'state')
    assert not hasattr(manager, 'generation_page') and not hasattr(manager, 'training_panel')
    assert not any(command.key == 'workflow-help' for command in manager.commands)


def test_unconfigured_task_homes_do_not_enable_runtime(manager_window):
    app, window = manager_window
    window.navigation.setCurrentRow(window.page_rows['Applications']); app.processEvents()
    for _component, (status, opening, manage, group) in window.workflow_applications.rows.items():
        assert not opening.isEnabled() and not manage.isEnabled()
        assert 'No installable release is available' in status.text()
    assert not hasattr(window, 'state')


def test_retained_drafts_switch_without_replacing_original_owners(generate_window):
    from PySide6.QtCore import Signal
    from PySide6.QtWidgets import QDialog
    app, window = generate_window
    class Draft(QDialog):
        workspace_closed = Signal()
        draft_picker_open = False
        def has_active_background_work(self): return False
        def closeEvent(self, event):
            self.workspace_closed.emit(); event.accept()
    first, second, video = Draft(), Draft(), Draft()
    page = window.generation_page
    page.add_workspace(first, label='New Image'); page.add_workspace(second, label='New Image')
    assert page.draft_choice.itemText(0) != page.draft_choice.itemText(1)
    page.add_workspace(video, label='Retained video', media_type='video', task='video')
    assert page.retained_workspaces() == (first, second, video)
    page.show_workspace(first)
    assert page.active_workspace() is first
    assert window._generation_dialog is first and window._video_generation_dialog is video
    page.show_workspace(video)
    assert page.active_workspace() is video and page.retained_workspaces() == (first, second, video)
    assert not page.begin_button.isVisible()
    assert not window.surface.new.isVisible() and window.surface.more.isVisible()
    assert not window.state.jobs.active()


def test_close_cannot_drop_an_inactive_busy_draft(generate_window):
    from PySide6.QtCore import Signal
    from PySide6.QtWidgets import QDialog
    app, window = generate_window
    closed = []
    class Draft(QDialog):
        workspace_closed = Signal()
        draft_picker_open = False
        def __init__(self, name, busy=False):
            super().__init__(); self.name, self.busy = name, busy
        def has_active_background_work(self): return self.busy
        def closeEvent(self, event):
            closed.append(self.name); self.workspace_closed.emit(); event.accept()
    busy, visible = Draft('original', True), Draft('newer')
    window.generation_page.add_workspace(busy, label='Original running request')
    window.generation_page.add_workspace(visible, label='Newer idle request')
    assert window.generation_page.active_workspace() is visible
    assert not window.close()
    assert window.isVisible() and not closed
    assert window.generation_page.active_workspace() is busy
    assert window.generation_page.retained_workspaces() == (busy, visible)
    busy.busy = False
    assert window.close()
    assert closed == ['original', 'newer']


def test_training_open_review_selection_retains_exact_controller_owner(train_window):
    from PySide6.QtWidgets import QDialog
    app, window = train_window
    first, second = QDialog(window), QDialog(window)
    first.setWindowTitle('Retained SDXL review'); second.setWindowTitle('Retained comparison')
    calls = []
    class Reviews:
        def __init__(self, *dialogs): self.dialogs = list(dialogs)
        def open_reviews(self): return tuple(self.dialogs)
        def show_review(self, dialog):
            if dialog not in self.dialogs: return False
            calls.append(dialog); return True
        def has_active_background_work(self): return False
    preparation, comparison = Reviews(first), Reviews(second)
    window._sdxl_preparation_controller = preparation; window._training_comparison_controller = comparison
    window.refresh_open_training_reviews()
    assert window.surface.reviews.count() == 2
    window.surface.reviews.setCurrentIndex(1); window.refresh_open_training_reviews()
    assert window._training_reviews[window.surface.reviews.currentData()] == (comparison, second)
    assert window.surface.continue_review.isVisible() and window.surface.continue_review.isEnabled()
    QTest.mouseClick(window.surface.continue_review, Qt.LeftButton)
    assert calls == [second]
    comparison.dialogs.clear(); QTest.mouseClick(window.surface.continue_review, Qt.LeftButton)
    assert calls == [second] and window.surface.reviews.count() == 1
    preparation.dialogs.clear(); window.refresh_open_training_reviews()
    assert not window.surface.continue_review.isEnabled()
    window._sdxl_preparation_controller = window._training_comparison_controller = None
    first.close(); second.close()


@pytest.mark.parametrize('theme', ('dark', 'light'))
@pytest.mark.parametrize('component,section,topic,required', (
    ('generate', 'Generate', 'generation', ('New Image', 'Review Saved Results', 'Media Organizer')),
    ('train', 'Train', 'training', ('Open Training Package', 'Open Saved Training Draft', 'Saved Runs')),
))
def test_workflow_help_ribbon_f1_and_close_keep_scope(tmp_path, theme, component, section, topic, required):
    from pathlib import Path
    from PySide6.QtWidgets import QAbstractButton, QTextBrowser
    from qt_app.help_content import manager_workspace_help
    from test_manager_comparison_queue import app as configured_app
    app = configured_app()
    app, session, window = workflow_window(tmp_path, component, theme=theme)
    from qt_app.components import WindowStatusBar
    assert isinstance(window.statusBar(), WindowStatusBar)
    assert window.statusBar().geometry().top() >= window.surface.geometry().bottom()
    assert not hasattr(window, '_window_status_footer'), 'No floating footer may cover the workflow heading'
    control = window.workflow_help
    assert control.help_text == manager_workspace_help(topic) and not control.isVisible()
    visible = [b for b in window.findChildren(QAbstractButton) if b.isVisible() and b.text() == 'Help']
    assert visible == [window.surface.help]
    out = Path(os.environ.get('CG_WORKFLOW_HELP_CAPTURES', str(tmp_path))); out.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(out / (component + '-' + theme + '-entry.png')))
    QTest.mouseClick(window.surface.help, Qt.LeftButton); app.processEvents()
    guide = control._help_dialog
    assert guide is not None and guide.isVisible() and guide.windowTitle() == section + ' Help'
    text = '\n'.join(b.toPlainText() for b in guide.findChildren(QTextBrowser))
    for expected in required: assert expected in text
    assert 'Dataset Collections' not in text
    assert window.navigation.currentRow() == 0
    assert not window.state.jobs.active() and not getattr(window, '_generation_dialog', None)
    assert guide.grab().save(str(out / (component + '-' + theme + '-guide.png')))
    assert guide.close(); app.processEvents()
    window.activateWindow(); window.setFocus(); app.processEvents()
    QTest.keyClick(window, Qt.Key_F1); app.processEvents()
    assert control._help_dialog is not None and control._help_dialog.isVisible()
    assert not window.state.jobs.active()
    assert control._help_dialog.close(); app.processEvents()
    assert window.grab().save(str(out / (component + '-' + theme + '-return.png')))
    window.activateWindow(); window.setFocus(); app.processEvents()
    QTest.keyClick(window, Qt.Key_F1); app.processEvents()
    assert control._help_dialog.isVisible()
    assert window.close(); app.processEvents()
    assert not window.isVisible()
    from shiboken6 import isValid
    remaining = control._help_dialog
    assert remaining is None or not isValid(remaining) or not remaining.isVisible()


@pytest.mark.parametrize("theme", ("dark", "light"))
@pytest.mark.parametrize("size", ((900, 600), (1200, 800), (1920, 1080)))
def test_responsive_workflow_actions_keep_scope_and_keyboard(tmp_path, monkeypatch, theme, size):
    """Generated component UI only; no release, model, native actor or network work."""
    from pathlib import Path
    from hashlib import sha256
    from PySide6.QtCore import QPoint
    from PySide6.QtWidgets import QScrollArea
    from qt_app.theme import apply_theme
    from core.component_acquisition import ComponentAcquisition
    from core.manager_update_service import ManagerUpdateService
    from suite_core.manager.bootstrap_entry import NormalEntrySession
    from suite_core.manager.version import VERSION
    from test_manager_comparison_queue import app as configured_app
    application = configured_app()
    apply_theme(application, theme)
    requests = []
    def no_transport(*args, **kwargs):
        requests.append((args, kwargs))
        raise AssertionError("No network or acquisition is allowed during layout/focus checks")
    trust = (Path(__file__).parents[1] / "suite_core/manager/public_release_trust.json").read_bytes()
    (tmp_path / "component-cache").mkdir()
    acquisition = ComponentAcquisition(cache_root=tmp_path / "component-cache", cache_owner="responsive-ui",
        opener=no_transport, host_version=VERSION, trust_bytes=trust, trust_sha256=sha256(trust).hexdigest())
    service = ManagerUpdateService(acquisition, current_version=VERSION, channel="beta")
    # Same typed inert presentation seam as the existing update-review tests.
    # This is not a native session/admission qualification.
    session = NormalEntrySession.__new__(NormalEntrySession)
    session.state = "ready"
    session.update_profile = {"profile_sha256": "a" * 64}
    session.admitted = NS(layout=NS(data_root=tmp_path / "manager-data"))
    window = create_window(unconfigured(datetime.now(timezone.utc)), normal_entry_session=session,
                           manager_update_service=service)
    window.resize(*size)
    window.show()
    def settle():
        for _ in range(8):
            application.processEvents(); QTest.qWait(15)
    def go(name):
        item = window.navigation.item(window.page_rows[name])
        QTest.mouseClick(window.navigation.viewport(), Qt.LeftButton,
                        pos=window.navigation.visualItemRect(item).center())
        settle()
    def inside(widget, viewport):
        rect = widget.rect().translated(widget.mapTo(viewport, QPoint(0, 0)))
        assert rect.left() >= 0 and rect.right() < viewport.width(), (widget.text(), rect, viewport.rect())
        assert rect.top() >= 0 and rect.bottom() < viewport.height(), (widget.text(), rect, viewport.rect())
        assert widget.width() >= widget.sizeHint().width(), widget.text()
    out = Path(os.environ.get("CG_RESPONSIVE_CAPTURES", str(tmp_path / "captures")))
    out.mkdir(parents=True, exist_ok=True)
    try:
        generated_app, generated_session, generated = workflow_window(tmp_path / 'generate-ui', 'generate', theme=theme)
        generated.resize(*size); settle()
        assert generated.surface.header.isVisible()
        assert generated.surface.header.y() < generated.surface.new.y()
        assert generated.surface.page.empty.isHidden()  # Module guidance replaces all-in-one routes.
        controls = (generated.surface.new, generated.surface.resume, generated.surface.more, generated.surface.help)
        assert controls[0].property("primary") is True
        assert len({id(button) for button in controls}) == 4
        expert_labels = [action.text() for action in generated.surface.more.menu().actions()]
        assert expert_labels.count('Create Generation Setup…') == 1
        assert expert_labels.count('Plan a Remix…') == 1
        assert expert_labels.count('Import Prompt Catalog…') == 1
        for control in controls:
            assert control.isVisible()
            inside(control, generated.surface)
        generated.surface.new.setFocus(); settle()
        QTest.keyClick(generated.surface.new, Qt.Key_Tab); settle()
        assert application.focusWidget() is generated.surface.resume
        assert not generated.generation_page.retained_workspaces() and not generated.state.jobs.active()
        assert not requests
        assert generated.grab().save(str(out / ("generate-%sx%s-%s.png" % (*size, theme))))
        generated.resize(size[0] + 28, size[1]); settle()
        generated.resize(*size); settle()
        assert controls == (generated.surface.new, generated.surface.resume, generated.surface.more, generated.surface.help)
        assert not generated.generation_page.retained_workspaces()
        assert generated.close(); settle()
        window.activateWindow(); window.setFocus(); settle()
        go("Settings")
        panel = window.manager_update_panel
        actions = (panel.check, panel.stage, panel.apply, panel.cancel)
        assert tuple(panel.action_grid.cards) == actions
        assert panel.apply.isVisible() and not panel.apply.isEnabled()
        scroll, = [s for s in window.stack.currentWidget().findChildren(QScrollArea)
                   if s.widget() is not None and s.widget().isAncestorOf(panel)]
        assert scroll.horizontalScrollBar().maximum() == 0
        # Use the visible standard scrollbar's End key; no private scrolling handler.
        if scroll.verticalScrollBar().maximum():
            assert scroll.verticalScrollBar().isVisible()
            QTest.keyClick(scroll.verticalScrollBar(), Qt.Key_End)
            settle()
        for action in actions:
            inside(action, scroll.viewport())
        panel.review.setFocus(); application.processEvents()
        QTest.keyClick(panel.review, Qt.Key_Tab); settle()
        assert application.focusWidget() is panel.check
        calls = []
        monkeypatch.setattr(panel, "request", lambda operation: calls.append(operation))
        QTest.mouseClick(panel.check, Qt.LeftButton); settle()
        assert calls == ["discover"]  # Exactly the original button dispatch; no service work.
        assert not requests and panel.future is None and panel.prepared is None
        assert window.grab().save(str(out / ("settings-%sx%s-%s.png" % (*size, theme))))
        window.resize(size[0] + 28, size[1]); settle()
        assert tuple(panel.action_grid.cards) == actions and calls == ["discover"]
    finally:
        window.close(); settle()
    assert not window.isVisible() and not requests


@pytest.mark.parametrize('theme', ('dark', 'light'))
def test_application_task_tabs_keep_workflows_clear_and_catalog_state(manager_window, theme):
    from pathlib import Path
    from qt_app.theme import apply_theme
    from test_manager_comparison_queue import app as configured_app
    app, manager = manager_window
    assert configured_app() is app
    apply_theme(app, theme)
    manager.resize(1920, 1080)
    out = Path('.tmp/workflow-dataset-boundary-20261116/application-captures-final')
    out.mkdir(parents=True, exist_ok=True)
    def capture(name):
        app.processEvents()
        QTest.qWait(30)
        assert manager.grab().save(str(out / (theme + "-" + name + ".png")))
    item = manager.navigation.item(manager.page_rows['Applications'])
    QTest.mouseClick(manager.navigation.viewport(), Qt.LeftButton,
                     pos=manager.navigation.visualItemRect(item).center())
    tabs = manager.application_sections
    assert [tabs.tabText(i) for i in range(tabs.count())] == [
        'Generate and Train', 'App catalog', 'Connections']
    assert tabs.currentIndex() == 0
    capture("workflows-unavailable")
    assert manager.workflow_applications.isVisible()
    assert not manager.application_categories.isVisible()
    assert not manager.workflow_management.isVisible()
    QTest.mouseClick(tabs.tabBar(), Qt.LeftButton, pos=tabs.tabBar().tabRect(1).center())
    manager.library_search.setText('synthetic catalog query')
    app.processEvents()
    assert manager.application_categories.isVisible()
    capture("catalog-filtered")
    assert not manager.workflow_applications.isVisible()
    QTest.mouseClick(tabs.tabBar(), Qt.LeftButton, pos=tabs.tabBar().tabRect(2).center())
    assert manager.workflow_management.isVisible()
    capture("connections-unavailable")
    assert manager.local_models_button.isVisible()
    assert not manager.application_categories.isVisible()
    QTest.mouseClick(tabs.tabBar(), Qt.LeftButton, pos=tabs.tabBar().tabRect(0).center())
    app.processEvents()
    assert manager.workflow_applications.isVisible()
    assert all(not row[3].isHidden() for row in manager.workflow_applications.rows.values())
    QTest.mouseClick(tabs.tabBar(), Qt.LeftButton, pos=tabs.tabBar().tabRect(1).center())
    assert manager.library_search.text() == 'synthetic catalog query'
    # Real tab-bar keyboard navigation must preserve the same catalog query.
    tabs.tabBar().setFocus(Qt.TabFocusReason)
    QTest.keyClick(tabs.tabBar(), Qt.Key_Right);app.processEvents()
    assert tabs.currentIndex() == 2 and manager.workflow_management.isVisible()
    QTest.keyClick(tabs.tabBar(), Qt.Key_Left);app.processEvents()
    assert tabs.currentIndex() == 1 and manager.library_search.text() == 'synthetic catalog query'
    QTest.keyClick(tabs.tabBar(), Qt.Key_Left);app.processEvents()
    assert tabs.currentIndex() == 0 and manager.workflow_applications.isVisible()
    QTest.keyClick(tabs.tabBar(), Qt.Key_Right);app.processEvents()
    assert tabs.currentIndex() == 1 and manager.library_search.text() == 'synthetic catalog query'
    manager.open_generate_plan()  # Existing routes return to the named task home.
    assert tabs.currentIndex() == 0

    # Component seam into the existing embedded connection; visible Back returns
    # to the exact tab. Inactive catalog ancestry must not become explicit hide.
    QTest.mouseClick(tabs.tabBar(), Qt.LeftButton, pos=tabs.tabBar().tabRect(2).center())
    manager.open_ollama_workspace()
    assert not tabs.isVisible() and manager.ollama_workspace.isVisible()
    from PySide6.QtWidgets import QPushButton
    back, = [button for button in manager.ollama_workspace.findChildren(QPushButton)
             if button.text() == 'Back to applications']
    QTest.mouseClick(back, Qt.LeftButton)
    app.processEvents()
    assert tabs.isVisible() and tabs.currentIndex() == 2
    QTest.mouseClick(tabs.tabBar(), Qt.LeftButton, pos=tabs.tabBar().tabRect(1).center())
    assert manager.application_categories.isVisible()
    assert manager.library_search.text() == 'synthetic catalog query'
    capture('catalog-after-connection-return')


@pytest.mark.parametrize('theme', ('dark', 'light'))
def test_saved_preparations_default_and_saved_runs_expert_route(train_window, monkeypatch, theme):
    from pathlib import Path
    from qt_app.theme import apply_theme
    from suite_core.tests.workflow_ui_fixtures import trigger_more
    app, window = train_window
    apply_theme(app, theme)
    window.resize(1920, 1080)
    calls = []
    monkeypatch.setattr(window, 'open_training_history', lambda **kw: calls.append(kw))
    assert window.surface.history.text() == 'Saved Preparations…'
    QTest.mouseClick(window.surface.history, Qt.LeftButton)
    assert calls == [{'preparations': True}]
    trigger_more(window, 'Saved Runs…')
    assert calls == [{'preparations': True}, {}]
    assert window.surface.open_package.property('primary')
    app.processEvents()
    out=Path('D:/CollectiGen-Dev/.tmp/workflow-portable-delivery-20261117/ui-captures')
    out.mkdir(parents=True, exist_ok=True)
    assert window.grab().save(str(out/(theme+'-train-entry.png')))
    window.surface.set_status('Preparing selected dataset', busy=True)
    assert not window.surface.history.isEnabled()
    window.surface.set_status('Preparation saved', busy=False)
    assert window.surface.history.isEnabled()


def test_preparation_progress_and_guidance_preserve_evidence_and_cancel(monkeypatch):
    from threading import Event
    from PySide6.QtWidgets import QLabel, QPlainTextEdit
    from suite_core.manager import krea_training_review as review
    app=QApplication.instance() or QApplication([])
    evidence='Readiness facts\nNext step\nDisabled old action\n\nExact evidence: synthetic-pinned'
    section=QPlainTextEdit();section.setPlainText(evidence)
    state=NS(sections=[section])
    review.KreaTrainingReviewDialog._preparation_next_step(state, 'Open Saved Preparations')
    assert section.toPlainText()=='Readiness facts\nNext step\nOpen Saved Preparations\n\nExact evidence: synthetic-pinned'
    state=NS(_future=NS(done=lambda:False), _executor=NS(close_held=True),
        _preparing_dataset=True, _cancel=Event(), _preparation_started=10.0,
        _preparation_status_second=0, status=QLabel())
    monkeypatch.setattr(review, 'monotonic', lambda:17.5)
    review.KreaTrainingReviewDialog._settle(state)
    assert 'Elapsed 7s' in state.status.text() and 'Cancel Check and Return' in state.status.text()
    state._cancel.set();state.status.setText('Stopping safely')
    monkeypatch.setattr(review, 'monotonic', lambda:25.0)
    review.KreaTrainingReviewDialog._settle(state)
    assert state.status.text()=='Stopping safely'
