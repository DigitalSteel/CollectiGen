"""Controlled qualification startup with doubles only; no native process/Qt/network."""
import json,sys
from pathlib import Path
from types import ModuleType
import pytest
from suite_core.encyclopedia import __main__ as application
from suite_core.encyclopedia import bootstrap_entry
from suite_core.encyclopedia.root_recovery_ui import preowner_failure_diagnostic
from tools.encyclopedia_build.profile_renderer import render_entry


@pytest.fixture(autouse=True)
def no_external_actions(monkeypatch):
    import subprocess,socket,urllib.request
    def deny(*a,**kw):pytest.fail('No process/network/native action allowed')
    monkeypatch.setattr(subprocess,'Popen',deny);monkeypatch.setattr(subprocess,'run',deny)
    monkeypatch.setattr(socket,'socket',deny);monkeypatch.setattr(urllib.request,'urlopen',deny)


def test_preowner_diagnostic_omits_prose_and_sensitive_cause():
    try:
        try:raise ValueError(('S' + 'Y' + 'N' + 'T' + 'H' + 'E' + 'T' + 'I' + 'C' + '_' + 'S' + 'E' + 'C' + 'R' + 'E' + 'T' + ' ' + 'C' + ':' + '/' + 'U' + 's' + 'e' + 'r' + 's' + '/' + 'T' + 'e' + 's' + 't' + '/' + 'p' + 'r' + 'i' + 'v' + 'a' + 't' + 'e' + '.' + 'p' + 'n' + 'g' + ' ' + 'p' + 'e' + 'r' + 's' + 'o' + 'n' + '@' + 'e' + 'x' + 'a' + 'm' + 'p' + 'l' + 'e' + '.' + 'c' + 'o' + 'm'))
        except ValueError as cause:raise ValueError('empty_owned_cache_required') from cause
    except ValueError as error:text=preowner_failure_diagnostic(error)
    assert len(text.encode())<=4096
    assert all(value not in text for value in (('S' + 'Y' + 'N' + 'T' + 'H' + 'E' + 'T' + 'I' + 'C' + '_' + 'S' + 'E' + 'C' + 'R' + 'E' + 'T'),('p' + 'r' + 'i' + 'v' + 'a' + 't' + 'e' + '.' + 'p' + 'n' + 'g'),('p' + 'e' + 'r' + 's' + 'o' + 'n' + '@' + 'e' + 'x' + 'a' + 'm' + 'p' + 'l' + 'e' + '.' + 'c' + 'o' + 'm'),('C' + ':' + '/' + 'U' + 's' + 'e' + 'r' + 's')))
    record=json.loads(text)
    assert record['code']=='empty_owned_cache_required' and record['status']=='failed'
    assert len(record['errors'])==2 and record['phase']=='profile_initialization'


@pytest.mark.parametrize('presentation_fails',(False,True))
def test_controlled_failure_reports_stderr_without_owner_or_disk_writes(tmp_path,monkeypatch,capsys,presentation_fails):
    root=tmp_path/'fixture';root.mkdir();image=root/'TagEncyclopedia.exe';image.write_bytes(b'INERT')
    monkeypatch.setattr(sys,'frozen',True,raising=False);monkeypatch.setattr(sys,'executable',str(image))
    seen=[]
    def present(title,message):
        seen.append((title,message))
        if presentation_fails:raise RuntimeError('SYNTHETIC_SECRET')
    assert application.report_preowner_startup_failure(ValueError('empty_owned_cache_required'),
        root=root,present=present)==2
    captured=capsys.readouterr();assert not captured.out
    assert json.loads(captured.err)['event']=='encyclopedia_preowner_startup_failed'
    assert len(seen)==1 and 'Do not delete' in seen[0][1]
    assert list(root.iterdir())==[image] and not (root/'Data').exists()


def test_owned_or_wrong_layout_failure_cannot_be_reclassified_preowner(tmp_path,monkeypatch,capsys):
    root=tmp_path/'fixture';root.mkdir();image=root/'TagEncyclopedia.exe';image.write_bytes(b'INERT')
    monkeypatch.setattr(sys,'frozen',True,raising=False);monkeypatch.setattr(sys,'executable',str(image))
    error=ValueError('empty_owned_cache_required');error.encyclopedia_root_owner=object()
    with pytest.raises(ValueError,match='has_no_owner'):application.report_preowner_startup_failure(error,root=root)
    monkeypatch.setattr(sys,'executable',str(tmp_path/'other.exe'))
    with pytest.raises(ValueError,match='layout_required'):application.report_preowner_startup_failure(ValueError('empty_owned_cache_required'),root=root)
    assert capsys.readouterr().err==''


def test_exact_generated_root_factory_failure_exits_failed_before_dispatch(tmp_path,monkeypatch,capsys):
    root=tmp_path/'fixture';root.mkdir();image=root/'TagEncyclopedia.exe';image.write_bytes(b'INERT')
    profile=ModuleType('encyclopedia_fixture_profile');profile.ROOT=root
    calls=[]
    def make(role):calls.append(role);raise ValueError('empty_owned_cache_required')
    profile.make_profile=make
    monkeypatch.setitem(sys.modules,'encyclopedia_fixture_profile',profile)
    monkeypatch.setattr(sys,'frozen',True,raising=False);monkeypatch.setattr(sys,'executable',str(image))
    monkeypatch.setattr(sys,'argv',[str(image)])
    monkeypatch.setattr(bootstrap_entry,'PROFILE',None)
    original=application.report_preowner_startup_failure
    monkeypatch.setattr(application,'report_preowner_startup_failure',lambda error,root:
        original(error,root=root,present=lambda *a:None))
    monkeypatch.setattr(application,'run',lambda *a:pytest.fail('lifecycle must not run'))
    with pytest.raises(SystemExit) as stopped:
        exec(compile(render_entry('root',bound=True),'encyclopedia_root_entry','exec'),{'__name__':'__main__'})
    assert stopped.value.code==2 and calls==['root'] and bootstrap_entry.PROFILE is None
    assert json.loads(capsys.readouterr().err)['status']=='failed'
    assert list(root.iterdir())==[image]


@pytest.mark.parametrize('role,arguments',(('root',['--encyclopedia-custody']),('reader',['--entry-token=INERT'])))
def test_custody_and_reader_factory_failures_never_use_root_failure_handler(tmp_path,monkeypatch,role,arguments):
    from suite_core.encyclopedia.entry_binding import HOST
    if role=='reader':arguments=[next(iter(HOST))+'=INERT']
    image=tmp_path/'TagEncyclopedia.exe'
    profile=ModuleType('encyclopedia_fixture_profile');profile.ROOT=tmp_path
    def failed(value):raise ValueError('synthetic role refusal')
    profile.make_profile=failed;monkeypatch.setitem(sys.modules,'encyclopedia_fixture_profile',profile)
    monkeypatch.setattr(sys,'frozen',True,raising=False);monkeypatch.setattr(sys,'executable',str(image))
    monkeypatch.setattr(sys,'argv',[str(image),*arguments])
    monkeypatch.setattr(application,'report_preowner_startup_failure',lambda *a,**kw:pytest.fail('root handler forbidden'))
    with pytest.raises(ValueError,match='synthetic role refusal'):
        exec(compile(render_entry(role,bound=True),'synthetic-entry','exec'),{'__name__':'__main__'})
