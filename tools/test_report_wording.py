"""Generated legacy/new report evidence; no real reports or native clipboard."""
from pathlib import Path
import importlib.util,sys,os,json,hashlib,zipfile
import pytest
from core import bug_report as reports
from qt_app.theme import apply_theme
from duplicate_custody_test_support import qt_test_scope,capture
from test_report_review_readiness import actor,invalidate,click

def old_module():
    name='core._report_wording_original19'
    if name not in sys.modules:
        spec=importlib.util.spec_from_file_location(name,Path(os.environ['CG_REPORT_BASELINE_ROOT'])/'core/bug_report.py')
        m=importlib.util.module_from_spec(spec);sys.modules[name]=m;spec.loader.exec_module(m)
    return sys.modules[name]

def prepared(m,kind='bug',attachment=False,identifier='CBR-1111111111111111'):
    draft=m.BugReportDraft('Generated wording review','Generated observed context','Generated desired result','Generated example',feedback_type=kind)
    attachments=(m.BugReportAttachment('generated.txt','text/plain',b'Generated attachment','Generated only',True),) if attachment else ()
    return m.prepare_bug_report(draft,{'application_version':'generated','page':'Generated review','jobs':[],'recent_events':[]},attachments,report_id=identifier,created_at='2026-11-04T11:19:00+00:00')

def snapshot(path):return {p.relative_to(path).as_posix():p.read_bytes() for p in path.rglob('*') if p.is_file()}
def sha(b):return hashlib.sha256(b).hexdigest()

@pytest.mark.parametrize('kind',['bug','workflow','feature'])
@pytest.mark.parametrize('attachment',[False,True])
def test_legacy_and_new_review_save_hashes_and_archive(tmp_path,kind,attachment):
    old=old_module();prior=prepared(old,kind,attachment);new=prepared(reports,kind,attachment)
    assert 'was saved locally' in prior.human_readable() and 'was saved locally' not in new.human_readable()
    assert 'was prepared locally' in new.human_readable() and 'is prepared locally' in new.privacy_manifest['notice']
    assert new.report['report']==prior.report['report'] and new.report['context']==prior.report['context']
    assert new.report['attachments']==prior.report['attachments'] and new.report['schema_version']==prior.report['schema_version']
    old_manifest=dict(prior.privacy_manifest);old_manifest['notice']=old_manifest['notice'].replace('is saved locally','is prepared locally');assert new.privacy_manifest==old_manifest
    expected=prior.human_readable().replace('was saved locally','was prepared locally').replace(prior.content_fingerprint,new.content_fingerprint);assert new.human_readable()==expected
    assert new.report['report_md_sha256']==sha(new.human_readable().encode())
    legacy=old.save_bug_report(prior,tmp_path/'legacy').directory;before=snapshot(legacy)
    assert reports.load_bug_report(legacy)==prior.report
    archive=reports.export_feedback_archive([legacy],tmp_path/'legacy.zip')
    with zipfile.ZipFile(archive.path) as z:assert z.read(prior.report_id+'/report.md')==before['report.md']
    assert snapshot(legacy)==before
    saved=reports.save_bug_report(new,tmp_path/'current').directory;loaded=reports.load_bug_report(saved)
    assert loaded==new.report and (saved/'report.md').read_bytes()==new.human_readable().encode()
    assert loaded['privacy_manifest_sha256']==sha((saved/'privacy-manifest.json').read_bytes())
    before_new=snapshot(saved);archive=reports.export_feedback_archive([saved],tmp_path/'current.zip')
    with zipfile.ZipFile(archive.path) as z:assert z.read(new.report_id+'/report.md')==before_new['report.md']
    assert snapshot(saved)==before_new and snapshot(legacy)==before

@pytest.mark.parametrize('variant',['legacy','new'])
def test_tampered_stored_reports_still_refuse(tmp_path,variant):
    m=old_module() if variant=='legacy' else reports;saved=m.save_bug_report(prepared(m),tmp_path/'generated').directory
    (saved/'report.md').write_bytes(b'Generated tamper');before=snapshot(saved)
    with pytest.raises(reports.BugReportError):reports.load_bug_report(saved)
    with pytest.raises(reports.BugReportError):reports.export_feedback_archive([saved],tmp_path/'refused.zip')
    assert not (tmp_path/'refused.zip').exists() and snapshot(saved)==before

def test_current_template_duplicate_tracking_remains_explicit(tmp_path):
    one=prepared(reports);two=prepared(reports,identifier='CBR-2222222222222222')
    assert one.content_fingerprint==two.content_fingerprint
    first=reports.save_bug_report(one,tmp_path/'generated');before=snapshot(first.directory)
    second=reports.save_bug_report(two,tmp_path/'generated');assert second.duplicate_of_report_id==one.report_id
    assert reports.load_bug_report(second.directory)==two.report and snapshot(first.directory)==before

@pytest.mark.parametrize('theme',['light','dark'])
def test_review_wording_tabs_copy_and_correction(qt_test_scope,monkeypatch,tmp_path,theme):
    apply_theme(qt_test_scope.app,theme,False)
    with actor(qt_test_scope,monkeypatch,tmp_path,'candidate') as o:
        assert 'was prepared locally' in o.d.report_preview.toPlainText() and 'was saved locally' not in o.d.report_text
        assert 'is prepared locally' in o.d.manifest_preview.toPlainText()
        capture(o.d,theme,'readable')
        o.d.preview_tabs.setCurrentIndex(2);o.d.manifest_preview.verticalScrollBar().setValue(o.d.manifest_preview.verticalScrollBar().maximum());qt_test_scope.settle(2);capture(o.d,theme,'privacy')
        o.d.preview_tabs.setCurrentIndex(0);click(o,'copy');assert o.copies==[o.d._prepared.human_readable()]
        o.copies.clear();invalidate(o.d);click(o,'copy');assert not o.copies and not o.d.save_button.isEnabled() and o.d.validation_status.isVisible();capture(o.d,theme,'invalid')
        o.d._base=prepared(reports);o.d._refresh_previews();click(o,'copy');assert o.copies==[o.d._prepared.human_readable()] and o.d.save_button.isEnabled();capture(o.d,theme,'corrected')
