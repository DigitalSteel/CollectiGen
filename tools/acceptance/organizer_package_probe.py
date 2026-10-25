"""Explicit packaged-entry probe using the existing observed-QApplication pattern.

Run with the selected package's isolated bundled interpreter, never the project
venv. No native loader, AppState, startup completion or close guard is replaced.
"""
import argparse
import hashlib
import json
import os
from pathlib import Path
import runpy
import re
import shlex
import shutil
import stat
import sys
import time
import traceback


def fresh_branch_observed(record):
    return ('visible_skip_for_now' in record.get('events',[])
            and record.get('startup_outcome')=='setup_skipped'
            and record.get('onboarding_status')=='skipped')


def acceptance_passed(record):
    """Require execution, persistence and cleanup; never hide guarded failures."""
    return bool(record.get('exit_code') == 0 and record.get('ready')
        and record.get('ordinary_close') and not record.get('errors')
        and not record.get('held') and record.get('pool_drained')
        and record.get('pool_active') == 0 and record.get('jobs_drained')
        and record.get('jobs_active') == 0 and record.get('settings_saved')
        and record.get('checkout_not_on_path') and record.get('source_markers_removed')
        and not record.get('blocked_events') and not record.get('unhandled_errors')
        and (record.get('mode')!='fresh' or fresh_branch_observed(record))
        and (record.get('mode')!='reopen' or (record.get('settings_reopened')
            and record.get('saved_fields_preserved') and not record.get('onboarding_repeated'))))


def workspace_path(repo, value):
    """Stdlib-only pre-entry equivalent of the existing unredirected-path guard."""
    path=Path(value).absolute()
    if (not path.is_relative_to(repo/'.tmp') or '..' in path.parts
            or any(part.casefold() in ('local only','local_only') for part in path.parts)):
        raise ValueError('Probe path is outside the admitted synthetic workspace')
    current=Path(path.anchor)
    for part in path.parts[1:]:
        current=current/part
        try: info=current.lstat()
        except FileNotFoundError: break
        if stat.S_ISLNK(info.st_mode) or getattr(info,'st_file_attributes',0)&0x400:
            raise ValueError('Redirected probe path')
    return path


def admit_reopen(repo, profile, receipt_path, receipt_sha256):
    """Read only a pinned prior successful generated-profile receipt and settings."""
    profile=workspace_path(repo,profile);receipt_path=workspace_path(repo,receipt_path)
    if not re.fullmatch(r'[0-9a-f]{64}',receipt_sha256 or ''):
        raise ValueError('Pinned prior receipt SHA256 required')
    if receipt_path.stat().st_size>1024*1024:
        raise ValueError('Prior receipt exceeds evidence bound')
    raw=receipt_path.read_bytes()
    if hashlib.sha256(raw).hexdigest()!=receipt_sha256:
        raise ValueError('Prior receipt changed')
    previous=json.loads(raw)
    if previous.get('passed') is not True or not acceptance_passed(previous):
        raise ValueError('Prior receipt is not qualified')
    if not isinstance(previous.get('profile'),str) or workspace_path(repo,previous['profile'])!=profile:
        raise ValueError('Prior profile differs')
    settings=workspace_path(repo,profile/'data/user/settings.json')
    if settings.stat().st_size>4*1024*1024:
        raise ValueError('Generated settings exceed evidence bound')
    data=settings.read_bytes();digest=hashlib.sha256(data).hexdigest()
    if digest!=previous.get('settings_sha256'):
        raise ValueError('Generated settings changed since qualification')
    saved=json.loads(data)
    if saved.get('onboarding_status')!='skipped' or previous.get('onboarding_status')!='skipped':
        raise ValueError('Reopen requires the qualified skipped-onboarding fixture')
    fields={key:saved[key] for key in ('onboarding_status','onboarding_version','ui_theme',
                                     'active_library_id','library_profiles') if key in saved}
    return dict(receipt_sha256=receipt_sha256,settings_pre_sha256=digest,settings_fields=fields)


def main():
    parser=argparse.ArgumentParser(description=__doc__)
    for name in ('package','profile','output'):parser.add_argument('--'+name,type=Path,required=True)
    parser.add_argument('--reopen-from',type=Path)
    parser.add_argument('--reopen-receipt-sha256')
    args=parser.parse_args()
    repo=Path(__file__).resolve().parents[2]
    package,profile,output=(workspace_path(repo,p) for p in (args.package,args.profile,args.output))
    assert not output.exists()
    for left,right in ((package,profile),(package,output),(profile,output)):
        assert not left.is_relative_to(right) and not right.is_relative_to(left)
    if bool(args.reopen_from)!=bool(args.reopen_receipt_sha256):
        parser.error('Reopen requires both a prior receipt and its SHA256')
    reopened=admit_reopen(repo,profile,args.reopen_from,args.reopen_receipt_sha256) if args.reopen_from else None
    if reopened is None:assert not profile.exists()
    assert Path(sys.executable).resolve()==(package/'Runtime/python/python.exe').resolve()
    assert sys.flags.isolated and sys.dont_write_bytecode
    assert not any(os.environ.get(n)=='1' for n in ('TAG_MANAGER_UI_TEST','TAG_MANAGER_PYTEST'))
    assert str(repo) not in sys.path and not any('.venv' in Path(p).parts for p in sys.path)
    presentation=json.loads((package/'suite_core/media_organizer/RELEASE_PRESENTATION.json').read_bytes())
    expected_title='Media Organizer '+presentation['display_version']
    profile.parent.mkdir(parents=True,exist_ok=True);output.mkdir(parents=True)
    record={'pid':os.getpid(),'interpreter':sys.executable,'driver_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
            'package':str(package),'profile':str(profile),'events':[],'frames':[],'errors':[],
            'entry':'real bundled module with explicit organizer-image runtime/profile','ready':False,
            'blocked_events':[],'blocked_command_classes':[],'unhandled_errors':[],
            'read_only_gpu_queries':0,'read_only_exiftool_queries':0,
            'mode':'reopen' if reopened else 'fresh','reopen_admission':reopened,'onboarding_repeated':False,
            'startup_outcome':'awaiting_first_run' if not reopened else 'saved_profile_reopen',
            'required_component_readiness':'not_assessed',
            'process_audit_counts_meaning':'permitted_attempts_not_execution_results'}
    def retain(): (output/'RESULT.json').write_text(json.dumps(record,indent=2),encoding='utf-8')
    guard={'active':True}
    gpu=shutil.which('nvidia-smi')
    def audit(event,values):
        if guard['active'] and event in ('socket.connect','subprocess.Popen','os.system'):
            if event=='subprocess.Popen':
                command=values[1]
                tokens=shlex.split(command,posix=False) if isinstance(command,str) else list(command)
                if (gpu and len(tokens)==4 and os.path.normcase(tokens[0].strip('"'))==os.path.normcase(gpu)
                        and re.fullmatch(r'--id=\d+',tokens[1])
                        and tokens[2:]==['--query-gpu=index,name,memory.total','--format=csv,noheader,nounits']):
                    record['read_only_gpu_queries']+=1
                    return
                if (len(tokens)==2 and os.path.normcase(tokens[0].strip('"'))==os.path.normcase(str(package/'Runtime/exiftool/exiftool.exe'))
                        and tokens[1]=='-ver'):
                    record['read_only_exiftool_queries']+=1
                    return
                executable=Path(str(tokens[0]).strip('"')).name.casefold() if tokens else ''
                command_path=Path(str(tokens[0]).strip('"')).absolute() if tokens else Path()
                record.setdefault('blocked_command_shapes',[]).append({
                    'path':str(command_path.relative_to(repo/'.tmp')) if command_path.is_relative_to(repo/'.tmp') else 'outside-generated-workspace',
                    'argument_count':len(tokens)-1,'version_only':tokens[1:]==['-ver']})
                record['blocked_command_classes'].append(executable if executable in
                    ('nvidia-smi.exe','nvidia-smi','exiftool.exe','cmd.exe','python.exe') else 'other')
                record.setdefault('blocked_callers',[]).append([
                    {'source':str(Path(frame.filename).relative_to(package)), 'line':frame.lineno, 'function':frame.name}
                    for frame in traceback.extract_stack() if Path(frame.filename).is_relative_to(package)])
            record['blocked_events'].append(event);retain()
            raise RuntimeError('External execution/network outside package startup probe')
        if guard['active'] and event in ('open','os.listdir','os.scandir') and values and isinstance(values[0],(str,bytes)):
            name=os.fsdecode(values[0]);absolute=os.path.normcase(os.path.abspath(name))
            private=os.path.normcase(str(repo/'data'))
            if absolute==private or absolute.startswith(private+os.sep) or any(
                    p.casefold() in ('local only','local_only') for p in Path(name).parts):
                record['blocked_events'].append('private_zone');retain()
                raise RuntimeError('Unadmitted private zone')
    sys.addaudithook(audit)
    from PySide6 import QtWidgets
    from PySide6.QtCore import QTimer,QThreadPool,Qt
    from PySide6.QtGui import QFont,QFontDatabase
    from PySide6.QtTest import QTest
    from shiboken6 import isValid
    original=QtWidgets.QApplication;window=[None];state=[None];closed_at=[None]
    started=time.monotonic();modal_actions=set();ready_at=[None]
    def capture(widget,name):
        path=output/(name+'.png');assert widget.grab().save(str(path))
        record['frames'].append({'state':name,'path':str(path),'sha256':hashlib.sha256(path.read_bytes()).hexdigest(),
                                'width':widget.width(),'height':widget.height(),'device_pixel_ratio':widget.devicePixelRatioF()})
    def tick(app):
        try:
            modal=app.activeModalWidget()
            if modal is not None:
                identity=id(modal)
                if identity not in modal_actions:
                    modal_actions.add(identity)
                    if type(modal).__name__=='OnboardingDialog' and reopened:
                        record['onboarding_repeated']=True
                        capture(modal,'unexpected-onboarding');record['errors'].append('Onboarding repeated on qualified reopen')
                        modal.reject()
                    elif type(modal).__name__=='OnboardingDialog':
                        capture(modal,'onboarding');button=modal.skip_button
                        assert button.isVisible() and button.isEnabled()
                        QTest.mouseClick(button,Qt.LeftButton)
                        record['events'].append('visible_skip_for_now')
                    else:
                        capture(modal,'unexpected-modal');record['errors'].append('Unexpected modal: '+type(modal).__name__)
                        modal.reject()  # Ordinary refusal; never accept an unknown change.
                    retain()
                return
            current=getattr(app,'_collectigen_main_window',None)
            if current is not None and window[0] is None:
                window[0]=current;state[0]=current.state
                assert current.isVisible() and expected_title==current.windowTitle()
                current.resize(1920,1080);record['ready']=True
                ready_at[0]=time.monotonic()
                if reopened:
                    record['settings_reopened']=all(getattr(state[0].settings,key,None)==value
                        for key,value in reopened['settings_fields'].items() if key!='library_profiles')
                record['events'].append('ready');retain()
            elif window[0] is not None:
                if not reopened:
                    record['onboarding_status']=getattr(state[0].settings,'onboarding_status',None)
                    if ('visible_skip_for_now' in record['events']
                            and record['onboarding_status']=='skipped'):
                        record['startup_outcome']='setup_skipped'
                if (not record.get('close_requested') and isValid(window[0])
                        and (reopened or fresh_branch_observed(record))
                        and (not reopened or time.monotonic()-ready_at[0]>.75)):
                    capture(window[0],'ready');window[0].close()
                    record['close_requested']=True;closed_at[0]=time.monotonic();retain()
                if (not isValid(window[0]) or not window[0].isVisible()) and closed_at[0] is not None:
                    if time.monotonic()-closed_at[0]>.5:
                        record['ordinary_close']=True;app.probe_timer.stop();app.quit()
            if time.monotonic()-started>120 and not record.get('held'):
                record['held']='original startup/Close not settled';retain()
                # Retain original work; no termination, forced quit or replay.
        except Exception as error:
            record['errors'].append(type(error).__name__+': '+str(error));retain()
            if window[0] is not None and isValid(window[0]) and not record.get('close_requested'):
                window[0].close();record['close_requested']=True;closed_at[0]=time.monotonic()
    class ObservedApplication(original):
        def __init__(self,*a,**kw):
            super().__init__(*a,**kw)
            font=QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf');assert font>=0
            self.setFont(QFont(QFontDatabase.applicationFontFamilies(font)[0],10))
            self.setQuitOnLastWindowClosed(False)
            self.probe_timer=QTimer(self);self.probe_timer.setInterval(100)
            self.probe_timer.timeout.connect(lambda:tick(self));self.probe_timer.start()
    QtWidgets.QApplication=ObservedApplication
    prior_hook=sys.excepthook
    def observed_error(kind,error,traceback):
        record['unhandled_errors'].append(kind.__name__);retain()
        prior_hook(kind,error,traceback)
    sys.excepthook=observed_error
    sys.argv=['suite_core.media_organizer','--profile-root',str(profile),
              '--runtime-profile','organizer-image','--runtime-root',str(package/'Runtime')]
    retain();code=None
    try:
        runpy.run_module('suite_core.media_organizer',run_name='__main__')
        code=0
    except SystemExit as exit:
        code=exit.code or 0
    except Exception as error:
        record['errors'].append(type(error).__name__+': '+str(error))
        code=1
    finally:
        guard['active']=False
        sys.excepthook=prior_hook
    app=original.instance()
    if app is not None:app.probe_timer.stop()
    if window[0] is not None and record.get('close_requested'):
        record['ordinary_close']=not isValid(window[0]) or not window[0].isVisible()
    record['exit_code']=code
    record['pool_drained']=QThreadPool.globalInstance().waitForDone(5000)
    record['pool_active']=QThreadPool.globalInstance().activeThreadCount()
    if state[0] is not None:
        record['jobs_drained']=state[0].jobs.shutdown(wait=True,timeout=5)
        record['jobs_active']=len(state[0].jobs.active())
    settings=profile/'data/user/settings.json'
    record['settings_saved']=settings.is_file()
    if settings.is_file():
        saved=json.loads(settings.read_bytes());record['onboarding_status']=saved.get('onboarding_status')
        record['settings_sha256']=hashlib.sha256(settings.read_bytes()).hexdigest()
        if reopened:
            record['saved_fields_preserved']=all(saved.get(key)==value for key,value in reopened['settings_fields'].items())
    record['checkout_not_on_path']=str(repo) not in sys.path and not any('.venv' in Path(p).parts for p in sys.path)
    record['source_markers_removed']=not any(type(p).__name__=='_OwnedSourcePath' for p in sys.path)
    record['passed']=acceptance_passed(record)
    retain();print(json.dumps({k:record.get(k) for k in ('passed','exit_code','ready','ordinary_close','errors')},indent=2),flush=True)
    return 0 if record['passed'] else 1


if __name__=='__main__':raise SystemExit(main())
