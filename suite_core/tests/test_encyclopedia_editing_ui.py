"""Original editor component routes; standalone shell integration is separate."""
import json
import time

import pytest
from PySide6.QtCore import QThreadPool, QTimer, Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication, QDialogButtonBox, QPushButton, QVBoxLayout, QWidget

from core.encyclopedia_editing import EncyclopediaEditingSession
from qt_app.category_workspace import CategoryCreationDialog, CategoryDetailsDialog
from suite_core.encyclopedia.editing_ui import EncyclopediaEditingBridge, create_category_workspace
from tests.test_encyclopedia_editing import editing_catalog


@pytest.fixture
def editing_ui(tmp_path, qt_test_scope, monkeypatch):
    # Supplying the stock prepared session must never invoke startup recovery.
    from core.tag_order_workspace import CategoryWorkspaceSession
    from core.tag_identity_merge import TagIdentityMergeService
    def forbidden(*_args, **_kwargs):
        pytest.fail("Editor composition cannot perform implicit recovery")
    monkeypatch.setattr(CategoryWorkspaceSession, "recover_interrupted", forbidden)
    monkeypatch.setattr(TagIdentityMergeService, "recover_interrupted", forbidden)
    session = EncyclopediaEditingSession.open(editing_catalog(tmp_path))
    pool = QThreadPool()
    host = qt_test_scope.show(QWidget())
    layout = QVBoxLayout(host)
    workspace = create_category_workspace(session, parent=host, pool=pool)
    layout.addWidget(workspace)
    selected = {"category": "animals", "tag": "fox"}
    bridge = EncyclopediaEditingBridge(session, category_workspace=workspace,
        selected_category=lambda: selected["category"], selected_tag=lambda: selected["tag"], parent=host)
    buttons = {}
    for command in bridge.commands:
        button = QPushButton(command.label, host)
        button.clicked.connect(command.callback)
        layout.addWidget(button)
        buttons[command.label] = button
    host.resize(1000, 800)
    qt_test_scope.settle()
    yield session, workspace, bridge, buttons, selected
    assert pool.waitForDone(10000), "Original Category worker did not drain"


def modal_driver(qt_test_scope, dialog_type, drive):
    """Retain failures without leaving a test-owned modal event loop open."""
    errors = []
    deadline = time.monotonic() + 15
    timer = qt_test_scope.own_timer(QTimer())
    timer.setInterval(20)
    def tick():
        dialog = next((w for w in QApplication.topLevelWidgets()
                       if isinstance(w, dialog_type) and w.isVisible()), None)
        if dialog is None:
            return
        if errors:
            # The original close gate may hold while a verified save completes.
            # Keep the cleanup timer alive until that same gate can close.
            dialog.reject()
            if not dialog.isVisible():
                timer.stop()
            return
        try:
            assert time.monotonic() < deadline, "Synthetic original editor route timed out"
            drive(dialog)
        except Exception as error:
            errors.append(error)
            dialog.reject()
            if not dialog.isVisible():
                timer.stop()
    timer.timeout.connect(tick)
    timer.start()
    return timer, errors


def test_original_category_create_cancel_save_and_reopen(editing_ui, qt_test_scope):
    session, workspace, bridge, buttons, selected = editing_ui
    original = session.categories.user_path.read_bytes()
    def cancel(dialog):
        QTest.keyClicks(dialog.name, "Unsaved synthetic category")
        assert not bridge.close_ready()
        QTest.mouseClick(dialog.buttons.button(QDialogButtonBox.Cancel), Qt.LeftButton)
    timer, errors = modal_driver(qt_test_scope, CategoryCreationDialog, cancel)
    QTest.mouseClick(buttons["New Category"], Qt.LeftButton)
    timer.stop()
    assert not errors
    assert session.categories.user_path.read_bytes() == original
    assert bridge.close_ready()
    phase = ["create"]
    saved = []
    bridge.saved.connect(lambda: saved.append(True))
    def create(dialog):
        if phase[0] == "create":
            QTest.keyClicks(dialog.name, "Saved synthetic category")
            assert dialog.parent_choice.currentData() == "animals"
            phase[0] = "wait"
            QTest.mouseClick(dialog.create_button, Qt.LeftButton)
    timer, errors = modal_driver(qt_test_scope, CategoryCreationDialog, create)
    QTest.mouseClick(buttons["New Category"], Qt.LeftButton)
    timer.stop()
    assert not errors and saved
    assert bridge.close_ready() and not session.reload_required
    reopened = EncyclopediaEditingSession.open(session.adapter)
    record = next(r for r in reopened.categories.categories.values() if r.name == "Saved synthetic category")
    assert record.parent_id == "animals"
    assert json.loads(reopened.categories.user_path.read_bytes())["future"] == {"keep": 1}


def test_category_adoption_failure_disables_original_workspace(editing_ui, monkeypatch):
    session, workspace, bridge, *_ = editing_ui
    def fail(_workspace):
        raise RuntimeError("Synthetic delivery authority change")
    monkeypatch.setattr(session, "adopt_category_workspace_save", fail)
    bridge._category_saved({})
    assert session.reload_required
    assert not workspace.isEnabled()


def test_original_category_details_visible_edit_saved_completion(editing_ui, qt_test_scope):
    session, workspace, bridge, buttons, selected = editing_ui
    phase = ["edit"]
    saved = []
    bridge.saved.connect(lambda: saved.append(True))
    def edit(dialog):
        if phase[0] == "edit":
            assert not bridge.close_ready()
            assert workspace.category_advanced_group.isHidden()
            QTest.mouseClick(workspace.category_description, Qt.LeftButton)
            QTest.keyClicks(workspace.category_description, " Edited through original controls.")
            phase[0] = "wait"
        elif phase[0] == "wait" and saved and not workspace.save_in_progress:
            phase[0] = "close"
            QTest.mouseClick(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Close), Qt.LeftButton)
    timer, errors = modal_driver(qt_test_scope, CategoryDetailsDialog, edit)
    QTest.mouseClick(buttons["Edit Selected Category"], Qt.LeftButton)
    timer.stop()
    assert not errors and saved
    assert bridge.close_ready() and not session.reload_required
    reopened = EncyclopediaEditingSession.open(session.adapter)
    assert "Edited through original controls." in reopened.categories.get("animals").description


def test_tag_commands_explicitly_unavailable_without_original_host(editing_ui):
    _session, _workspace, bridge, _buttons, _selected = editing_ui
    messages = []
    bridge.unavailable.connect(messages.append)
    assert bridge.tag_editor is None
    assert not bridge.commands[0].enabled() and not bridge.commands[1].enabled()
    assert not bridge.new_tag() and not bridge.edit_tag()
    assert len(messages) == 2


def test_original_save_failure_holds_widget_until_selected_reopen(editing_ui, qt_test_scope, monkeypatch):
    session, workspace, bridge, buttons, _selected = editing_ui
    before = session.categories.user_path.read_bytes()
    def refuse(*_args, **_kwargs):
        raise OSError("Synthetic selected save refusal")
    monkeypatch.setattr(type(session.category_session), "save", refuse)
    phase = ["create"]
    def fail(dialog):
        if phase[0] == "create":
            QTest.keyClicks(dialog.name, "Cannot save synthetic category")
            phase[0] = "wait"
            QTest.mouseClick(dialog.create_button, Qt.LeftButton)
        elif phase[0] == "wait" and session.reload_required:
            assert not workspace.isEnabled() and not bridge.ready
            assert "Synthetic selected save refusal" in dialog.status.text()
            phase[0] = "close"
            dialog.reject()
    timer, errors = modal_driver(qt_test_scope, CategoryCreationDialog, fail)
    QTest.mouseClick(buttons["New Category"], Qt.LeftButton)
    timer.stop()
    assert not errors and session.reload_required
    assert session.categories.user_path.read_bytes() == before
    assert not bridge.new_category()
