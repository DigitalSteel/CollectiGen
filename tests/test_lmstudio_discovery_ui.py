"""Connected synthetic discovery consumer; no server, model or native app."""
import json
from threading import Event

import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest

from core.lmstudio_connection_settings import LmStudioConnectionSettings
from core.lmstudio_discovery import DiscoveryError, parse_models
from qt_app.lmstudio_connection_dialog import LmStudioConnectionDialog
from test_lmstudio_connection_ui import (
    app, clean_owned_dialogs, settled, type_value, capture,
    test_original_setup_visible_entry_preserves_local_ai_draft as setup_route,
)


def inventory(*, empty=False, loaded=False, vision=None):
    return parse_models(json.dumps({"models": [] if empty else [{
        "key": "synthetic/exact-key", "type": "llm", "display_name": "Generated Model",
        "size_bytes": 1024, "loaded_instances": [{"id": "different-loaded-instance", "config": {"context_length": 2048}}] if loaded else [],
        "capabilities": {} if vision is None else {"vision": vision},
    }]}).encode())


def opened(tmp_path, app, discovery):
    store = LmStudioConnectionSettings(tmp_path / "settings.json")
    dialog = LmStudioConnectionDialog(store, discovery=discovery)
    app._connection_test_dialogs.append(dialog)
    dialog.show(); settled(dialog)
    type_value(dialog.endpoint, "http://127.0.0.1:1234")
    return store, dialog


def click_refresh(dialog):
    QTest.mouseClick(dialog.refresh_button, Qt.LeftButton)
    settled(dialog)


@pytest.mark.parametrize("theme", ["light", "dark"])
def test_original_setup_refresh_select_save_reopen(app, tmp_path, theme, monkeypatch):
    import test_lmstudio_connection_ui as original
    import qt_app.lmstudio_connection_dialog as ui
    calls = []
    monkeypatch.setattr(ui, "discover", lambda endpoint, **kw: calls.append(endpoint) or inventory(loaded=True, vision=True))
    original_edit = original.edit
    def refresh_and_choose(dialog, key="synthetic-model"):
        original_edit(dialog, "")
        assert not calls
        QTest.mouseClick(dialog.refresh_button, Qt.LeftButton)
        settled(dialog)
        assert len(calls) == 1 and dialog.models.count() == 2
        dialog.models.setFocus()
        QTest.keyClick(dialog.models, Qt.Key_Down)
        assert dialog.model_key.text() == "synthetic/exact-key"
        assert "inference readiness" in dialog.model_status.text()
        capture(dialog, "discovery-populated-" + theme)
    monkeypatch.setattr(original, "edit", refresh_and_choose)
    setup_route(app, tmp_path, theme)
    store = LmStudioConnectionSettings(tmp_path / "settings.json")
    assert store.observe().selection.preferred_model_key == "synthetic/exact-key"
    dialog = LmStudioConnectionDialog(store)
    app._connection_test_dialogs.append(dialog); dialog.show(); settled(dialog)
    assert len(calls) == 1 and dialog.models.count() == 1
    assert dialog.model_key.text() == "synthetic/exact-key" and "not been checked" in dialog.status.text()
    capture(dialog, "discovery-reopened-" + theme)
    QTest.mouseClick(dialog.close_button, Qt.LeftButton)


@pytest.mark.parametrize("empty,loaded,vision", [(True,False,None),(False,False,None),(False,True,False)])
def test_declared_metadata_is_not_readiness(app, tmp_path, empty, loaded, vision):
    store, dialog = opened(tmp_path, app, lambda *a, **kw: inventory(empty=empty,loaded=loaded,vision=vision))
    type_value(dialog.model_key, "unreported-preference")
    click_refresh(dialog)
    assert "preferred key was not reported" in dialog.model_status.text()
    assert store.observe().selection is None
    if empty:
        assert "no available models" in dialog.model_status.text()
        capture(dialog, "discovery-empty")
    else:
        assert ("Loaded" if loaded else "Not loaded") in dialog.models.itemText(1)
        assert ("Vision unknown" if vision is None else "No vision advertised") in dialog.models.itemData(1, Qt.ToolTipRole)
    assert dialog.save_button.isEnabled()
    type_value(dialog.endpoint, "http://127.0.0.1:1235")
    assert dialog.models.count()==1 and dialog._observed_endpoint is None
    assert dialog.model_key.text()=="unreported-preference"


@pytest.mark.parametrize("code,phrase", [("authentication_required_or_rejected","Enter a token"),("unsupported_route","supported model-list API"),("discovery_deadline","timed out"),("invalid_json","could not be read"),("private-sentinel-path","could not be read")])
def test_refusals_leave_manual_preferences_available(app,tmp_path,code,phrase):
    def refused(*a,**kw): raise DiscoveryError(code)
    store,dialog=opened(tmp_path,app,refused)
    click_refresh(dialog)
    assert phrase in dialog.model_status.text()
    assert "private-sentinel" not in dialog.model_status.text()
    assert not dialog._held and dialog.save_button.isEnabled()
    capture(dialog,"discovery-error-"+code if code!="private-sentinel-path" else "discovery-safe-error")
    assert store.observe().selection is None


@pytest.mark.parametrize("action", ["cancel","close","edit"])
def test_delayed_result_is_revoked_and_physically_settled(app,tmp_path,action):
    started,release=Event(),Event();calls=[]
    def delayed(endpoint,*,cancelled):
        calls.append((endpoint,cancelled));started.set();release.wait(4)
        return inventory()
    store,dialog=opened(tmp_path,app,delayed)
    # Establish a clean saved draft so Close needs no discard dialog.
    QTest.mouseClick(dialog.save_button,Qt.LeftButton);settled(dialog)
    QTest.mouseClick(dialog.refresh_button,Qt.LeftButton)
    try:
        for _ in range(100):
            QTest.qWait(10)
            if started.is_set():break
        assert started.is_set() and dialog._task is not None
        assert not dialog.refresh_button.isEnabled() and not dialog.save_button.isEnabled()
        if action=="close":
            QTest.mouseClick(dialog.close_button,Qt.LeftButton)
        elif action=="cancel":
            QTest.mouseClick(dialog.cancel_button,Qt.LeftButton)
        else:
            dialog.endpoint.setText("http://127.0.0.1:1235")
        if action!="edit":assert calls[0][1]()
        assert dialog.isVisible() and not dialog._task.is_finished()
        QTest.qWait(60)
        assert "Elapsed" in dialog.status.text()
        capture(dialog,"discovery-pending-"+action)
    finally:
        release.set();settled(dialog)
    assert dialog.models.count()==1 and dialog._observed_endpoint is None
    assert len(calls)==1
    assert store.observe().selection.endpoint=="http://127.0.0.1:1234"
    if action=="close":assert not dialog.isVisible()
    else:assert dialog.isVisible() and dialog.refresh_button.isEnabled()
