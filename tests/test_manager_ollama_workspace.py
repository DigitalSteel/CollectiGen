"""Execute actual candidate methods against UI doubles; never import or launch Qt."""
import ast
from pathlib import Path
from types import SimpleNamespace
import pytest

BASE = Path(__file__).resolve().parents[1] / "suite_core/manager"

def methods(filename, names, namespace=None):
    tree = ast.parse((BASE / filename).read_text(encoding="utf-8"))
    nodes = [node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name in names]
    assert len(nodes) == len(names)
    cls = ast.ClassDef(name="Surface", bases=[], keywords=[], body=nodes, decorator_list=[])
    module = ast.fix_missing_locations(ast.Module(body=[cls], type_ignores=[]))
    scope = {} if namespace is None else namespace
    exec(compile(module, "<owned-ui-methods>", "exec"), scope)
    return scope["Surface"]

class Control:
    def __init__(self): self.visible = True; self.enabled = True; self.value = ""; self.checked = True
    def findChild(self, kind): return self
    def isVisible(self): return self.visible
    def show(self): self.visible = True
    def hide(self): self.visible = False
    def setVisible(self, value): self.visible = value
    def setEnabled(self, value): self.enabled = value
    def setText(self, value): self.value = value
    def text(self): return self.value
    def setChecked(self, value): self.checked = value
    def isChecked(self): return self.checked
    def blockSignals(self, value): pass

def controller(**changes):
    values = dict(draft=SimpleNamespace(endpoint="http://127.0.0.1:11434", allowed=False),
        message="Connection saved. Metadata access is off.", closed=False, can_save=False,
        hold=False, dirty=False, state="saved", opened=True, activation_pending=False,
        observe_refresh=lambda: None)
    values.update(changes)
    return SimpleNamespace(**values)

@pytest.mark.parametrize("state,hold,dirty,pending,expected", [
    ("saved",False,False,False,(True,False,False)),
    ("held",True,True,False,(True,False,False)),
    ("reloaded",False,True,False,(True,True,False)),
    ("waiting",False,False,True,(True,False,True)),
])
def test_idle_and_recovery_controls_are_contextual(state,hold,dirty,pending,expected):
    c = controller(state=state, hold=hold, dirty=dirty, activation_pending=pending)
    obj = methods("backend_setup_ui.py", {"render"}, {"controller":c,"QToolButton":object})()
    for name in ("endpoint","permission","status","save","reload","use_saved","retry","saved_options"): setattr(obj,name,Control())
    obj.saved_options.checked = False
    obj.render()
    assert obj.saved_options.checked is bool(hold or pending)
    obj.saved_options.checked = True  # user deliberately expands saved options
    obj.render()
    assert obj.saved_options.checked  # idle projection must not hide explicit Reload
    assert tuple(getattr(obj,name).visible for name in ("reload","use_saved","retry")) == expected
    assert obj.status.value == c.message
    assert not obj.save.enabled

@pytest.mark.parametrize("success", [True,False])
def test_dialog_closes_only_after_verified_persistence(success):
    c = controller(save=lambda: success)
    obj = methods("backend_setup_ui.py", {"save_and_close"}, {"controller":c,"QToolButton":object})()
    calls = []
    obj.timer = SimpleNamespace(stop=lambda:calls.append("stop"))
    obj.accept = lambda:calls.append("accept")
    obj.render = lambda:calls.append("render")
    obj.save_and_close()
    assert calls == (["stop","accept"] if success else ["render"])

@pytest.mark.parametrize("message,expected", [("Read-only Ollama metadata; see dated route status in Applications", ""), ("Synthetic real failure", "Synthetic real failure")])
def test_applications_route_and_return_only_change_view(message, expected):
    obj = methods("window.py", {"open_ollama_workspace","close_ollama_workspace"})()
    rows, rendered = [], []
    status = [message]
    obj.statusBar = lambda: SimpleNamespace(currentMessage=lambda: status[0], clearMessage=lambda: status.__setitem__(0, ""))
    obj.navigation = SimpleNamespace(setCurrentRow=rows.append)
    obj.page_rows = {"Applications":2}
    obj.dependency_route_settled = lambda: None
    for name in ("application_categories","categories_toggle","app_observation_heading","applications","ollama_workspace"): setattr(obj,name,Control())
    obj.ollama_workspace.visible = False
    obj.app_observation_heading.visible = False
    obj.render_ollama = lambda:rendered.append("inventory")
    obj.render_library = lambda:rendered.append("catalog")
    obj.open_ollama_workspace()
    assert rows == [2] and obj.ollama_workspace.visible and not obj.application_categories.visible
    obj.open_ollama_workspace()  # repeated shared-editor entry must retain original return state
    obj.close_ollama_workspace()
    assert not obj.ollama_workspace.visible and obj.application_categories.visible
    assert obj.applications.visible and not obj.app_observation_heading.visible
    assert rendered == ["inventory","inventory","catalog"]
    assert status[0] == expected

def test_shared_editor_is_singleton_and_both_routes_reference_it():
    obj = methods("window.py", {"open_backend_setup"})()
    obj.close_policy = SimpleNamespace(pending=False)
    obj.backend_setup_controller = object()
    calls=[]
    obj.open_ollama_workspace=lambda:calls.append("Applications")
    obj.backend_setup_dialog=SimpleNamespace(raise_=lambda:calls.append("raise"),activateWindow=lambda:calls.append("activate"))
    obj.open_backend_setup()
    assert calls == ["Applications","raise","activate"]
    obj.close_policy.pending=True
    obj.open_backend_setup()
    assert calls == ["Applications","raise","activate"]
    window=ast.parse((BASE/"window.py").read_text(encoding="utf-8"))
    bindings=[n for n in ast.walk(window) if isinstance(n,ast.keyword) and n.arg=="configure"]
    binding = next(n.value for n in bindings if any(isinstance(v,ast.Attribute) and v.attr=="open_backend_setup" for v in ast.walk(n.value)))
    expression = compile(ast.fix_missing_locations(ast.Expression(body=binding)), "<shared-editor-binding>", "eval")
    state = SimpleNamespace(backend_setup_controller=None, open_backend_setup=lambda:None)
    assert eval(expression, {"self":state}) is None
    state.backend_setup_controller = object()
    assert eval(expression, {"self":state}) is state.open_backend_setup
    bindings=[n for n in ast.walk(window) if isinstance(n,ast.keyword) and n.arg=="open_metadata"]
    assert any(isinstance(n.value,ast.Attribute) and n.value.attr=="open_ollama_workspace" for n in bindings)

def test_install_new_dispatch_is_gated_by_qualified_owned_panel():
    tree=ast.parse((BASE/"window.py").read_text(encoding="utf-8"))
    tile=next(n for n in ast.walk(tree) if isinstance(n,ast.FunctionDef) and n.name=="create_ollama_tile")
    calls=[n for n in ast.walk(tile) if isinstance(n,ast.Call)]
    enabled=next(n.args[0] for n in calls if isinstance(n.func,ast.Attribute)
        and isinstance(n.func.value,ast.Name) and n.func.value.id=="install" and n.func.attr=="setEnabled")
    expression=compile(ast.fix_missing_locations(ast.Expression(body=enabled)),"<qualified-install>","eval")
    for unavailable in ('Supply a qualified package.', ''):
        panel=SimpleNamespace(install=SimpleNamespace(isEnabled=lambda: not bool(unavailable)))
        assert bool(eval(expression,{"panel":panel})) is (not bool(unavailable))
    assert any(isinstance(n.func,ast.Attribute) and n.func.attr=="connect"
        and isinstance(n.func.value,ast.Attribute) and isinstance(n.func.value.value,ast.Name)
        and n.func.value.value.id=="install" for n in calls)

@pytest.mark.parametrize("selected,category,query,visible", [
    ("All entries","Model Runners","",True),
    ("All entries","All categories","ollama",True),
    ("Installed","Model Runners","",False),
    ("All entries","Games","",False),
    ("All entries","Model Runners","everday",False),
])
def test_connection_tile_respects_scope_and_does_not_claim_installed(selected,category,query,visible):
    tree=ast.parse((BASE/"window.py").read_text(encoding="utf-8"))
    assignment=next(n for n in ast.walk(tree) if isinstance(n,ast.Assign) and any(isinstance(t,ast.Name) and t.id=="show_ollama" for t in n.targets))
    expression=compile(ast.fix_missing_locations(ast.Expression(body=assignment.value)), "<connection-tile-filter>", "eval")
    assert bool(eval(expression, {"selected":selected,"category":category,"query":query,"self":SimpleNamespace(owned_runtime_panel=SimpleNamespace(installed=False))})) is visible
