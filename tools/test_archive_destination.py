"""Bulk destination preservation using fresh generated report evidence only."""
import hashlib
import json
import os
from pathlib import Path
import zipfile

import pytest
from core import bug_report as reports

def test_published_baseline_collision_and_exact_generated_restore(tmp_path):
    """Discriminate the old service on disposable evidence, never real reports."""
    import importlib.util,sys
    path=Path(os.environ['CG_REPORT_BASELINE_ROOT'])/'core/bug_report.py'
    name='core._archive_destination_original18'
    spec=importlib.util.spec_from_file_location(name,path)
    baseline=importlib.util.module_from_spec(spec);sys.modules[name]=baseline;spec.loader.exec_module(baseline)
    draft=baseline.BugReportDraft('Generated baseline collision','Generated only','Keep original','Generated steps')
    saved=baseline.save_bug_report(baseline.prepare_bug_report(draft,{}),tmp_path/'baseline-feedback')
    original=snapshot(saved.directory);target=saved.directory/'report.md'
    try:
        baseline.export_feedback_archive([saved.directory],target)
        assert zipfile.is_zipfile(target) and target.read_bytes()!=original['report.md']
    finally:
        for name,content in original.items():(saved.directory/name).write_bytes(content)
    assert snapshot(saved.directory)==original

def test_tampered_selection_remains_rejected(saved,tmp_path):
    target=tmp_path/'tampered.zip';(saved.directory/'report.md').write_bytes(b'Generated tamper')
    original=snapshot(saved.directory)
    with pytest.raises(reports.BugReportError):reports.export_feedback_archive([saved.directory],target)
    assert not target.exists() and snapshot(saved.directory)==original


@pytest.fixture
def saved(tmp_path):
    draft=reports.BugReportDraft('Generated archive test','Generated observation',
        'Preserve generated evidence','Generated steps')
    attachment=reports.BugReportAttachment('generated.txt','text/plain',b'Generated attachment',
        'Generated destination fixture',True)
    return reports.save_bug_report(reports.prepare_bug_report(draft,{},(attachment,)),tmp_path/'feedback')


def snapshot(directory):
    return {str(p.relative_to(directory)):p.read_bytes() for p in directory.rglob('*') if p.is_file()}


@pytest.mark.parametrize('relative',['report.md','report.json','privacy-manifest.json',
    'tracking.json','attachments/generated.txt','new-child.zip','attachments/new-child.zip',
    'attachments/../report.md','.'])
def test_refuse_report_contained_destination_before_loader_or_writer(saved,relative,monkeypatch):
    before=snapshot(saved.directory)
    def forbidden(*a,**k):pytest.fail('Unsafe destination must refuse before report loading or writing')
    monkeypatch.setattr(reports,'load_bug_report',forbidden)
    monkeypatch.setattr(reports,'write_verified_zip',forbidden)
    target=saved.directory/relative
    with pytest.raises(reports.BugReportError,match='outside the selected report folders'):
        reports.export_feedback_archive([saved.directory],target)
    assert snapshot(saved.directory)==before


def test_every_selected_report_directory_is_protected(saved,tmp_path):
    second=reports.save_bug_report(reports.prepare_bug_report(reports.BugReportDraft(
        'Generated second report','Second observation','Keep both reports','Second steps'),{}),
        tmp_path/'feedback')
    before=(snapshot(saved.directory),snapshot(second.directory))
    with pytest.raises(reports.BugReportError,match='outside the selected report folders'):
        reports.export_feedback_archive([saved.directory,second.directory],second.directory/'new.zip')
    assert (snapshot(saved.directory),snapshot(second.directory))==before


def test_refuse_existing_outside_artifact_before_loading(saved,tmp_path,monkeypatch):
    target=tmp_path/'previous.zip';target.write_bytes(b'Generated previous artifact')
    before=snapshot(saved.directory)
    monkeypatch.setattr(reports,'load_bug_report',lambda *_:pytest.fail('No report loading for an existing output'))
    with pytest.raises(reports.BugReportError,match='already exists'):
        reports.export_feedback_archive([saved.directory],target)
    assert target.read_bytes()==b'Generated previous artifact' and snapshot(saved.directory)==before


@pytest.mark.parametrize('error',[OSError('Generated inaccessible path'),RuntimeError('Generated alias loop'),ValueError('Generated invalid path')])
def test_unverifiable_destination_fails_closed(saved,tmp_path,monkeypatch,error):
    before=snapshot(saved.directory)
    monkeypatch.setattr(Path,'resolve',lambda *_a,**_k:(_ for _ in ()).throw(error))
    with pytest.raises(reports.BugReportError,match='could not be verified'):
        reports.export_feedback_archive([saved.directory],tmp_path/'new.zip')
    assert not (tmp_path/'new.zip').exists() and snapshot(saved.directory)==before


def test_normal_archive_verifies_and_preserves_all_originals(saved,tmp_path):
    before=snapshot(saved.directory);target=tmp_path/'new.zip'
    result=reports.export_feedback_archive([saved.directory],target)
    assert result.path==target and hashlib.sha256(target.read_bytes()).hexdigest()==result.sha256
    with zipfile.ZipFile(target) as z:
        manifest=json.loads(z.read('feedback-archive-manifest.json'))
        assert manifest['report_count']==1 and manifest['archive_id']==result.archive_id
        for entry in manifest['reports'][0]['files']:
            payload=z.read(entry['name'])
            assert len(payload)==entry['bytes'] and hashlib.sha256(payload).hexdigest()==entry['sha256']
        assert not any(n.endswith('tracking.json') for n in z.namelist())
    assert snapshot(saved.directory)==before


def test_empty_and_duplicate_selection_still_refuse(saved,tmp_path):
    before=snapshot(saved.directory)
    with pytest.raises(reports.BugReportError,match='Select at least one'):
        reports.export_feedback_archive([],tmp_path/'empty.zip')
    with pytest.raises(reports.BugReportError,match='more than once'):
        reports.export_feedback_archive([saved.directory,saved.directory],tmp_path/'duplicate.zip')
    assert not (tmp_path/'empty.zip').exists() and not (tmp_path/'duplicate.zip').exists()
    assert snapshot(saved.directory)==before
