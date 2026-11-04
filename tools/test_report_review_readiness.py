"""Current privacy review, actual Qt controls, generated data and inert effects."""
from contextlib import contextmanager
from dataclasses import replace
from copy import deepcopy
from pathlib import Path
from types import SimpleNamespace as NS
import importlib.util,os,sys
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from core import BugReportDraft,prepare_bug_report
from qt_app import bug_report_dialog as candidate
from qt_app.theme import apply_theme
from duplicate_custody_test_support import qt_test_scope,capture

def prepared(summary='Generated previous review'):
    return prepare_bug_report(BugReportDraft(summary,'Generated context','Generated desired result','Generated steps'),
        {'application_version':'generated','page':'Generated review fixture','recent_events':[],'jobs':[]},
        report_id='CBR-0123456789ABCDEF',created_at='2026-11-04T09:22:00+00:00')

def module(variant):
    if variant=='candidate':return candidate
    name='qt_app._bug323_original17'
    if name not in sys.modules:
        path=Path(os.environ['CG_REPORT_BASELINE_ROOT'])/'qt_app/bug_report_dialog.py'
        spec=importlib.util.spec_from_file_location(name,path);m=importlib.util.module_from_spec(spec)
        sys.modules[name]=m;spec.loader.exec_module(m)
    return sys.modules[name]

@contextmanager
def actor(scope,monkeypatch,tmp_path,variant):
    m=module(variant);copies=[];writes=[]
    monkeypatch.setattr(m,'QApplication',NS(clipboard=lambda:NS(setText=copies.append)))
    def save(value,destination):
        writes.append((value,destination));return NS(directory=tmp_path/'inert-saved',duplicate_of_report_id='')
    monkeypatch.setattr(m,'save_bug_report',save)
    d=m.BugReportPrivacyDialog(prepared(),app_root=tmp_path/'generated-app',development_inbox=None)
    d.create_zip.setChecked(False);scope.show(d,size=(1920,1080));scope.settle(2)
    try:yield NS(d=d,copies=copies,writes=writes)
    finally:d.close();d.deleteLater();scope.settle(2)

def invalidate(d):
    report=deepcopy(d._base.report);report['report']['summary']=''
    d._base=replace(d._base,report=report)

def click(o,action):
    QTest.mouseClick(o.d.copy_button if action=='copy' else o.d.save_button,Qt.LeftButton)

@pytest.mark.parametrize('variant',['baseline','candidate'])
@pytest.mark.parametrize('action',['copy','save'])
@pytest.mark.parametrize('valid',[False,True])
def test_original_current_review_effect_boundary(qt_test_scope,monkeypatch,tmp_path,variant,action,valid):
    with actor(qt_test_scope,monkeypatch,tmp_path,variant) as o:
        prior=o.d._prepared
        if not valid:invalidate(o.d)
        click(o,action);qt_test_scope.settle(2)
        effects=o.copies if action=='copy' else o.writes
        assert len(effects)==int(valid or variant=='baseline')
        if not valid:
            assert o.d._prepared is prior and not o.d.save_button.isEnabled()
            if variant=='candidate':
                assert o.d.validation_status.isVisible() and 'Summary' in o.d.validation_status.text()
                assert o.d.isVisible() and not o.d.saved_directory
        elif action=='copy':assert effects==[o.d._prepared.human_readable()]
        else:assert effects[0][0] is o.d._prepared and o.d.saved_directory==tmp_path/'inert-saved'

@pytest.mark.parametrize('action',['copy','save'])
def test_deliberate_valid_correction_retries_once(qt_test_scope,monkeypatch,tmp_path,action):
    with actor(qt_test_scope,monkeypatch,tmp_path,'candidate') as o:
        invalidate(o.d);click(o,action);assert not o.copies and not o.writes
        o.d._base=prepared('Generated corrected review');o.d._refresh_previews()
        assert o.d.save_button.isEnabled() and not o.d.validation_status.isVisible()
        click(o,action);qt_test_scope.settle(2)
        assert len(o.copies if action=='copy' else o.writes)==1
        assert o.d._prepared.report['report']['summary']=='Generated corrected review'

@pytest.mark.parametrize('theme',['light','dark'])
@pytest.mark.parametrize('variant',['baseline','candidate'])
def test_visible_current_review_ready_invalid_corrected(qt_test_scope,monkeypatch,tmp_path,theme,variant):
    apply_theme(qt_test_scope.app,theme,False)
    with actor(qt_test_scope,monkeypatch,tmp_path,variant) as o:
        group=variant+'-'+theme
        capture(o.d,group,'ready')
        invalidate(o.d);click(o,'copy');qt_test_scope.settle(2)
        assert len(o.copies)==int(variant=='baseline')
        if variant=='candidate':assert o.d.validation_status.isVisible()
        capture(o.d,group,'invalid')
        o.d._base=prepared('Generated corrected review');o.d._refresh_previews();click(o,'copy');qt_test_scope.settle(2)
        assert o.copies[-1]==o.d._prepared.human_readable()
        capture(o.d,group,'corrected')
