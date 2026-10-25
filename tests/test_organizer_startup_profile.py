"""Finite explicit runtime admission and stock repair forwarding; no installation."""
from pathlib import Path
from types import SimpleNamespace
import pytest
from suite_core.media_organizer.runtime_startup import RuntimeSelection,OrganizerRuntimeError
from suite_core.media_organizer import entry


def test_explicit_image_lock_profile_root_and_interpreter_forwarded(tmp_path,monkeypatch):
    import runtime_contract
    from suite_core.media_organizer import runtime_startup
    selection=RuntimeSelection('organizer-image',tmp_path/'runtime',tmp_path/'program')
    monkeypatch.setattr(runtime_startup.sys,'executable',str(selection.interpreter))
    calls=[];expected={'fixture':'explicit authority'};checked=SimpleNamespace(ready=True,issues=[])
    def load(pins,lock,**kwargs):calls.append(('contract',pins,lock,kwargs));return expected
    def validate(manifest,**kwargs):calls.append(('validation',manifest,kwargs));return checked
    monkeypatch.setattr(runtime_contract,'load_expected_runtime_contract',load)
    monkeypatch.setattr(runtime_contract,'validate_runtime',validate)
    assert selection.validate() is checked
    assert calls[0][2]==selection.program_root/'requirements-organizer-image-runtime.lock'
    assert calls[0][3]=={'capability_profile':'organizer-image'}
    assert calls[1][1]==selection.runtime_root/'python/collectigen-runtime.json'
    assert calls[1][2]['expected_contract'] is expected
    assert calls[1][2]['capability_profile']=='organizer-image'
    assert calls[1][2]['runtime_root']==selection.runtime_root and calls[1][2]['verify_hashes'] is True


def test_manifest_cannot_choose_policy_or_borrow_interpreter(tmp_path):
    selection=RuntimeSelection('organizer-image',tmp_path/'runtime',tmp_path)
    with pytest.raises(OrganizerRuntimeError,match='selected private runtime interpreter'):selection.validate()
    with pytest.raises(ValueError,match='Unknown'):RuntimeSelection('manifest-auto',tmp_path,tmp_path)


def test_repair_forwards_explicit_profile_and_destination_without_manifest(tmp_path,monkeypatch):
    from suite_core.media_organizer import runtime_startup
    selection=RuntimeSelection('organizer-image',tmp_path/'runtime',tmp_path/'program')
    (selection.program_root/'tools').mkdir(parents=True)
    (selection.program_root/'tools/Repair-PortableRuntime.ps1').write_text('# synthetic; execution mocked')
    calls=[]
    def run(command,**kwargs):calls.append((command,kwargs));return SimpleNamespace(returncode=7)
    monkeypatch.setattr(runtime_startup.subprocess,'run',run)
    assert selection.repair()==7
    assert calls[0][0][-6:]==['-CapabilityProfile','organizer-image','-RuntimeRoot',str(selection.runtime_root),
        '-Destination',str(selection.interpreter.parent)]
    assert calls[0][1]['check'] is False


def test_default_full_runtime_factory_is_unchanged(tmp_path):
    assert RuntimeSelection('collectigen-core',tmp_path,tmp_path).dependency_factory() is None


def test_native_failure_guidance_preserves_selection_without_repair(tmp_path,capsys):
    selection=RuntimeSelection('organizer-image',tmp_path/'runtime',tmp_path/'program')
    (selection.program_root/'tools').mkdir(parents=True)
    (selection.program_root/'tools/Repair-PortableRuntime.ps1').write_text('# synthetic; never executed')
    def unavailable():raise ModuleNotFoundError('Missing fixture native component')
    assert entry.main(native_loader=unavailable,runtime_selection=selection)==2
    output=capsys.readouterr().out
    assert 'organizer-image' in output and str(selection.runtime_root) in output
    assert '-Destination' in output and str(selection.interpreter.parent) in output


def test_direct_launcher_scopes_only_program_source_path(monkeypatch):
    import sys
    from suite_core.media_organizer import launch
    from suite_core.media_organizer import __main__ as bootstrap
    before=list(sys.path);calls=[]
    def cli(argv):calls.append((argv,list(sys.path)));return 3
    monkeypatch.setattr(bootstrap,'cli_main',cli)
    assert launch.main(['--validate-runtime'])==3
    assert calls[0][0]==['--validate-runtime']
    assert calls[0][1][1:]==before and sys.path==before
    assert calls[0][1][0]==str(Path(launch.__file__).resolve().parents[2])


def test_native_loader_requires_explicit_canonical_factory_seam_before_state_import(monkeypatch):
    selection=SimpleNamespace(validate=lambda:None,capability_profile='organizer-image')
    startup=SimpleNamespace(_start_application=lambda *,startup_notice='':0)
    monkeypatch.setattr(entry,'import_module',lambda name:startup if name=='qt_main' else pytest.fail('Loaded state before seam admission'))
    with pytest.raises(OrganizerRuntimeError,match='selected dependency service factory'):
        entry.load_native_runtime(runtime_selection=selection)


def test_native_loader_forwards_same_factory_to_original_startup(tmp_path,monkeypatch):
    import sys
    factory=object();calls=[]
    selection=SimpleNamespace(validate=lambda:calls.append('admitted'),capability_profile='organizer-image',dependency_factory=lambda:factory)
    def start(*,startup_notice='',dependency_service_factory=None):
        calls.append((startup_notice,dependency_service_factory));return 9
    startup=SimpleNamespace(_start_application=start,_recover_previous_ai_work=lambda:(True,''),VERSION='fixture')
    exports=SimpleNamespace();app=SimpleNamespace(instance=lambda:None)
    modules={'qt_main':startup,'qt_app':exports,'qt_app.main_window':SimpleNamespace(TagManagerWindow=object),
        'PySide6.QtWidgets':SimpleNamespace(QApplication=app)}
    monkeypatch.setattr(entry,'import_module',lambda name:modules[name])
    monkeypatch.setitem(sys.modules,'core.settings',SimpleNamespace(DEFAULT_SETTINGS_PATH=tmp_path/'settings.json'))
    loaded=entry.load_native_runtime(runtime_selection=selection)
    assert loaded.start_application(startup_notice='same notice')==9
    assert calls==['admitted',('same notice',factory)]


@pytest.mark.parametrize('injected',[False,True])
def test_canonical_startup_worker_preserves_default_or_injected_factory(injected):
    import ast
    source=Path(__file__).resolve().parents[1]/'qt_main.py'
    module=ast.parse(source.read_text(encoding='utf-8-sig'))
    startup=next(n for n in module.body if isinstance(n,ast.FunctionDef) and n.name=='_start_application')
    worker=next(n for n in startup.body if isinstance(n,ast.ClassDef) and n.name=='StartupStateWorker')
    run=next(n for n in worker.body if isinstance(n,ast.FunctionDef) and n.name=='run')
    run.decorator_list=[]
    calls=[];factory=object() if injected else None;settings=object();state=object();emitted=[]
    def AppState(**kwargs):calls.append(kwargs);return state
    namespace={'AppState':AppState,'dependency_service_factory':factory}
    exec(compile(ast.Module(body=[run],type_ignores=[]),str(source),'exec'),namespace)
    namespace['run'](SimpleNamespace(settings=settings,complete=SimpleNamespace(emit=emitted.append),
        failed=SimpleNamespace(emit=lambda error:pytest.fail(str(error)))))
    expected={'settings':settings}
    if injected:expected['dependency_service_factory']=factory
    assert calls==[expected] and emitted==[state]
