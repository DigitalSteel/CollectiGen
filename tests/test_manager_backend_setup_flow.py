"""Synthetic candidate controller tests; no Qt, project imports or disk settings."""
import importlib.util
import sys
from pathlib import Path
from types import SimpleNamespace
import ast
import pytest

BASE = Path(__file__).resolve().parents[1]
spec = importlib.util.spec_from_file_location("owned_setup_controller", BASE / "suite_core/manager/backend_setup_controller.py")
module = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = module
spec.loader.exec_module(module)

class Setup:
    def __init__(self):
        self._observed = SimpleNamespace(selection=None)
        self.persisted = None
        self.fail = None
        self.cancelled = 0
    def open_setup(self):
        self._observed.selection = self.persisted
    def save_connection(self, endpoint, *, endpoint_network_allowed):
        if self.fail:
            raise self.fail
        self.persisted = SimpleNamespace(endpoint=endpoint.rstrip("/"), endpoint_network_allowed=endpoint_network_allowed)
        self._observed.selection = self.persisted
    def selected_binding(self):
        return self.persisted
    def cancel_setup(self):
        self.cancelled += 1

class Host:
    def __init__(self):
        self.ready = True
        self.can_refresh = True
        self.calls = 0
        self.installs = []
        self.bridge = SimpleNamespace(ollama_report=None, ollama_pending=False, worker_pending=False, ollama_error=None)
    def replace_ready(self): return self.ready
    def install(self, binding): self.installs.append(binding)
    def get_bridge(self): return self.bridge
    def refresh(self): self.calls += 1; return True
    def cancel_refresh(self): self.bridge.ollama_pending = False

def flow():
    setup, host = Setup(), Host()
    controller = module.SetupController(setup, host)
    controller.open()
    return controller, setup, host

def test_offline_save_is_complete_and_repeated_save_is_disabled():
    c, setup, host = flow()
    c.edit("http://127.0.0.1:11434/", False)
    assert c.save()
    assert setup.persisted.endpoint == "http://127.0.0.1:11434"
    assert c.message == "Connection saved. Metadata access is off."
    assert not c.dirty and not c.can_save and not c.can_refresh
    assert not c.save() and host.calls == 0
    c.edit(c.saved.endpoint, c.saved.allowed)
    assert c.state == "saved" and c.message == "Connection saved. Metadata access is off."

def test_consent_and_explicit_refresh_remain_separate():
    c, setup, host = flow()
    c.edit("http://127.0.0.1:11434", True)
    assert c.save() and host.calls == 0
    assert c.can_refresh and c.refresh() and host.calls == 1
    c.edit("http://127.0.0.1:11435", True)
    assert not c.can_refresh and not c.refresh() and host.calls == 1

def test_stale_write_reload_retains_draft_and_use_saved_is_explicit():
    c, setup, host = flow()
    c.edit("http://127.0.0.1:11434", False); assert c.save()
    c.edit("http://127.0.0.1:11435", True)
    error = RuntimeError("synthetic stale write")
    error.code = "settings_changed_reload_setup"
    setup.fail = error
    assert not c.save() and c.hold and c.draft.endpoint.endswith("11435")
    setup.fail = None
    setup.persisted = SimpleNamespace(endpoint="http://127.0.0.1:11436", endpoint_network_allowed=False)
    assert c.reload() and c.draft.endpoint.endswith("11435") and c.saved.endpoint.endswith("11436")
    assert c.state == "reloaded" and c.dirty
    assert c.use_saved() and c.draft == c.saved and not c.can_save and host.calls == 0

def test_saved_activation_hold_is_distinct_from_persistence_failure():
    c, setup, host = flow()
    host.ready = False
    c.edit("http://127.0.0.1:11434", False)
    assert c.save() and setup.persisted is not None
    assert c.activation_pending and c.state == "waiting"
    assert not c.can_refresh and not c.can_save and host.calls == 0
    host.ready = True
    assert c.activate() and c.state == "saved"

def test_cancel_discards_only_draft_and_close_retains_network_gate():
    c, setup, host = flow()
    c.edit("http://127.0.0.1:11434", False); assert c.save()
    saved = setup.persisted
    c.edit("http://127.0.0.1:11435", True); c.cancel()
    assert setup.persisted is saved and c.draft == c.saved and not c.can_refresh
    c.begin_close()
    c.edit("http://127.0.0.1:11436", True)
    assert c.draft == c.saved and not c.save() and not c.refresh() and host.calls == 0


def manager_poll(c):
    # Execute the actual Manager poll method with no dialog present and no Qt.
    tree=ast.parse((BASE/"suite_core/manager/window.py").read_text(encoding="utf-8"))
    node=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=="poll_backend_setup")
    scope={}
    exec(compile(ast.fix_missing_locations(ast.Module(body=[node],type_ignores=[])),"<owned-manager-poll>","exec"),scope)
    messages=[]
    owner=SimpleNamespace(backend_setup_controller=c, backend_setup_dialog=None,
        ollama_connection_status=SimpleNamespace(setText=messages.append))
    scope["poll_backend_setup"](owner)
    return messages[-1]

@pytest.mark.parametrize("outcome,expected", [
    ("success","metadata_ready"),("failure","refresh_failed"),("cancel","cancelled"),
])
def test_manager_observes_refresh_completion_after_editor_is_closed(outcome,expected):
    c, setup, host = flow()
    c.edit("http://127.0.0.1:11434",True); assert c.save()
    assert c.refresh()
    host.bridge.ollama_pending=True
    assert "Refreshing" in manager_poll(c)
    if outcome=="success": host.bridge.ollama_report=object()
    elif outcome=="failure": host.bridge.ollama_error="synthetic read failed"
    else: assert c.cancel_refresh()
    host.bridge.ollama_pending=False
    message=manager_poll(c)
    assert c.state==expected
    assert "Refreshing" not in message and "Waiting for its worker" not in message
    assert host.calls==1 and not c.dirty

def test_external_change_failed_refresh_reopen_explicit_reload_preserves_old_draft():
    c, setup, host = flow()
    c.edit("http://127.0.0.1:11434",True); assert c.save()
    original=c.saved
    setup.persisted=SimpleNamespace(endpoint="http://127.0.0.1:11436",endpoint_network_allowed=False)
    assert c.refresh()
    host.bridge.ollama_error="stale selection"; manager_poll(c)
    assert c.state=="refresh_failed"
    assert c.open() and c.saved==original and c.draft==original and not c.can_save
    assert c.reload()
    assert c.saved.endpoint.endswith("11436") and c.draft==original and c.dirty and c.state=="reloaded"
    assert c.use_saved() and not c.dirty and not c.can_save and not c.can_refresh
    assert c.message=="Connection saved. Metadata access is off." and host.calls==1
