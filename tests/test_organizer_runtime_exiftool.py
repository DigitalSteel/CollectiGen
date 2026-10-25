"""Selected-runtime tool authority without native execution or global fallback."""
from types import SimpleNamespace

import pytest

from core import dependencies as dep
from suite_core.media_organizer.runtime_startup import RuntimeSelection


@pytest.fixture
def isolated(monkeypatch, tmp_path):
    for name in ('_recover_interrupted_optional_install', '_recover_interrupted_installs',
                 '_recover_interrupted_removals'):
        monkeypatch.setattr(dep.DependencyService, name, lambda self: ())
    monkeypatch.setattr(dep, 'OPTIONAL_COMPONENT_ROOT', tmp_path/'optional')
    monkeypatch.setattr(dep, 'PRIVATE_EXIFTOOL_DIR', tmp_path/'profile-tools')
    global_tool = tmp_path/'global/exiftool.exe'
    global_tool.parent.mkdir()
    global_tool.write_bytes(b'global fixture, never executable')
    monkeypatch.setattr(dep.shutil, 'which', lambda name: str(global_tool))
    return tmp_path, global_tool


def test_selected_factory_uses_runtime_tool_without_changing_settings(isolated):
    root, global_tool = isolated
    runtime = root/'Runtime'
    tool = runtime/'exiftool/exiftool.exe'
    tool.parent.mkdir(parents=True)
    tool.write_bytes(b'admitted fixture, never executable')
    settings = SimpleNamespace(exiftool_path='')
    service = RuntimeSelection('organizer-image', runtime, root).dependency_factory()(settings)
    assert service.exiftool_source_path() == str(tool)
    assert service.exiftool_path() == str(tool)
    assert service.exiftool_source_path() != str(global_tool)
    assert settings.exiftool_path == ''


def test_missing_selected_tool_never_borrows_global_or_profile_tool(isolated):
    root, _ = isolated
    dep.PRIVATE_EXIFTOOL_DIR.mkdir()
    (dep.PRIVATE_EXIFTOOL_DIR/'exiftool.exe').write_bytes(b'profile fixture')
    service = RuntimeSelection('organizer-image', root/'Runtime', root).dependency_factory()(
        SimpleNamespace(exiftool_path=''))
    assert service.exiftool_source_path() == service.exiftool_path() == ''


def test_explicit_configured_tool_keeps_authority(isolated):
    root, configured = isolated
    settings = SimpleNamespace(exiftool_path=str(configured))
    service = dep.DependencyService(settings, runtime_exiftool=root/'missing.exe')
    assert service.exiftool_source_path() == str(configured)
    assert settings.exiftool_path == str(configured)


def test_default_full_app_selection_unchanged(isolated):
    root, global_tool = isolated
    service = dep.DependencyService(SimpleNamespace(exiftool_path=''))
    assert service.exiftool_source_path() == str(global_tool)
    assert RuntimeSelection('collectigen-core', root/'Runtime', root).dependency_factory() is None


def test_runtime_tool_keeps_existing_unicode_launcher(monkeypatch, isolated):
    root, _ = isolated
    tool = root/'Runtime/exiftool/exiftool.exe'
    tool.parent.mkdir(parents=True)
    tool.write_bytes(b'fixture')
    service = dep.DependencyService(SimpleNamespace(exiftool_path=''), runtime_exiftool=tool)
    seen = []
    def launch(path):
        seen.append(path)
        return root/'safe-launcher/exiftool.exe'
    monkeypatch.setattr(dep, '_launchable_bundled_exiftool', launch)
    assert service.exiftool_path() == str(root/'safe-launcher/exiftool.exe')
    assert seen == [tool]


@pytest.mark.parametrize('explicit', [False, True])
def test_guided_setup_keeps_exact_factory_even_if_falsey(monkeypatch, explicit):
    from qt_app import onboarding_dialog as module
    settings = object()
    marker = object()
    class Factory:
        def __bool__(self): return False
        def __call__(self, actual):
            assert actual is settings
            return marker
    factory = Factory()
    def default(actual):
        assert actual is settings
        assert not explicit, 'Guided Setup lost selected runtime'
        return marker
    monkeypatch.setattr(module, 'DependencyService', default)
    dialog = SimpleNamespace(settings=settings, dependency_service_factory=factory if explicit else None)
    assert module.OnboardingDialog.dependency_service(dialog) is marker
