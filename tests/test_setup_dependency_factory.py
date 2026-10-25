"""Pure factory routing checks: no AppState construction or local resource probes."""
from types import SimpleNamespace
import pytest


@pytest.mark.parametrize("owner",["state","queue","page","dialog"])
@pytest.mark.parametrize("explicit",[False,True])
def test_dependency_factory_defaults_and_exact_injected_context(monkeypatch,owner,explicit):
    from core import app_state,setup_operation_queue
    from qt_app import pages,dependency_dialog
    module,cls={"state":(app_state,app_state.AppState),
        "queue":(setup_operation_queue,setup_operation_queue.SetupOperationQueue),
        "page":(pages,pages.SetupPage),"dialog":(dependency_dialog,dependency_dialog.DependencyDialog)}[owner]
    settings=object();calls=[];marker=object()
    class FalseFactory:
        def __bool__(self):return False
        def __call__(self,actual):
            assert actual is settings;calls.append("explicit");return marker
    def default(actual):
        assert actual is settings;calls.append("default");return marker
    monkeypatch.setattr(module,"DependencyService",default)
    state=SimpleNamespace(settings=settings,dependency_service_factory=FalseFactory() if explicit else None)
    assert cls.dependency_service(state) is marker
    assert calls==["explicit" if explicit else "default"]


@pytest.mark.parametrize("explicit",[False,True])
def test_original_startup_readiness_uses_state_factory(monkeypatch,explicit):
    from qt_app import main_window
    settings=object();calls=[]
    def factory(actual):
        assert actual is settings;calls.append(True)
        return SimpleNamespace(components=lambda **kwargs:[])
    monkeypatch.setattr(main_window,"DependencyService",factory if not explicit else lambda _:pytest.fail("Lost admitted context"))
    window=SimpleNamespace(settings=settings,state=SimpleNamespace(dependency_service_factory=factory if explicit else None))
    main_window.TagManagerWindow.check_startup_dependencies(window)
    assert calls==[True]


@pytest.mark.parametrize("explicit",[False,True])
def test_missing_capability_modal_fallback_preserves_context_and_rechecks(monkeypatch,explicit):
    from qt_app import dependency_dialog as module
    settings=object();ready=[False];calls=[];dialogs=[]
    def factory(actual):
        assert actual is settings;calls.append(ready[0])
        return SimpleNamespace(settings=settings,components=lambda **kwargs:[SimpleNamespace(name="ExifTool",installed=ready[0])])
    context=factory if explicit else None
    parent=SimpleNamespace(state=SimpleNamespace(dependency_service_factory=context))
    monkeypatch.setattr(module,"DependencyService",factory if not explicit else lambda _:pytest.fail("Lost injected factory"))
    monkeypatch.setattr(module.QMessageBox,"question",lambda *args:module.QMessageBox.Yes)
    class Modal:
        def __init__(self,actual,owner,**kwargs):
            assert actual is settings and owner is parent
            assert kwargs["dependency_service_factory"] is context
            dialogs.append(kwargs)
        def exec(self):ready[0]=True
    monkeypatch.setattr(module,"DependencyDialog",Modal)
    assert module.ensure_capability(parent,settings,"metadata","Synthetic metadata task")
    assert len(dialogs)==1 and calls==[False,True]
