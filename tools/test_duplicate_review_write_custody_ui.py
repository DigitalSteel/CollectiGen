"""Generated write adapters; actual dialog constructor/stock coordinator; no file IO."""
from pathlib import Path
from threading import Event
from types import SimpleNamespace as NS
import pytest
from PySide6.QtCore import Qt,QTimer
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QApplication,QDialog,QMessageBox
from core.background_jobs import BackgroundJobManager
from core.duplicate_finder import DuplicateFingerprint,DuplicateSearchReport
from qt_app.workers import CoordinatedCallTask
from duplicate_custody_test_support import wait_for,capture,qt_test_scope


def make(scope,monkeypatch,mode=''):
    import qt_app.duplicate_finder_dialog as ui
    monkeypatch.setattr(ui,'content_safety_policy',lambda:None)
    entered,release=Event(),Event();attempts=[];effects=[];warnings=[];saved=[];changed=[];finished=[]
    def record(name):
        return DuplicateFingerprint(name,'generated-scope',Path(name+'.png'),'generated',name+'.png','Generated no-IO scope','.','generated',1,0,0,'sample','byte','pixel',(0,),('identity',),80,60,'PNG','RGB','2026-10-30T12:00:00+00:00')
    first=record('generated-first');second=record('generated-second')
    report=DuplicateSearchReport('generated-scope','Generated comparison',88,100,{first.identity:first,second.identity:second},())
    o=NS(entered=entered,release=release,attempts=attempts,effects=effects,warnings=warnings,saved=saved,changed=changed,finished=finished,mode='success',report=report,first=first,second=second)
    def held(kind):
        attempts.append(kind);entered.set();assert release.wait(10),'generated adapter hold not released'
        effects.append(kind)
        if o.mode=='failure':raise OSError('Generated acknowledgement failure;outcome not confirmed')
    def mark(*a,**k):held('mark');return report
    def recycle(*a,**k):held('recycle')
    decisions=NS(snapshot_keys=lambda:(),is_distinct=lambda *a:False)
    duplicates=NS(decisions=decisions,has_session_reports=lambda:False,mark_distinct=mark,record_session_report=saved.append,regroup=lambda r:r,clear_session_reports=lambda:None,record_badge_results=lambda r:None)
    manager=BackgroundJobManager(limits={'cpu':1,'io':1});o.manager=manager
    settings=NS(gallery_blur_unknown=False,library_registry=lambda:NS(profiles=()))
    state=NS(settings=settings,jobs=manager,duplicates=duplicates,recycle=NS(apply_recycle=recycle))
    frozen=NS(id='generated-scope',label='Generated frozen scope') if mode else None
    d=ui.DuplicateFinderDialog(state,frozen_scope=frozen,frozen_mode=mode or 'training');o.d=d;o.scope=scope
    d.report=report
    for combo,record in [(d.left_choice,first),(d.right_choice,second),(d.reference,first)]:
        combo.blockSignals(True);combo.addItem(record.path.name,record.identity);combo.blockSignals(False)
    d._update_actions();d.changed.connect(changed.append);d.finished.connect(finished.append)
    monkeypatch.setattr(ui.QMessageBox,'warning',lambda *a,**k:warnings.append((a[1],a[2])))
    scope.show(d,size=(1600,960));return o


def start(o,kind='mark',group=None):
    if kind=='mark':
        def confirm():
            box=QApplication.activeModalWidget();assert isinstance(box,QMessageBox)
            assert box.defaultButton().text()=='Cancel' and box.escapeButton().text()=='Cancel'
            if group:capture(box,group,'01_confirmation')
            QTest.mouseClick(next(b for b in box.buttons() if b.text()=='Mark Distinct'),Qt.LeftButton)
        QTimer.singleShot(0,confirm);QTest.mouseClick(o.d.resolve,Qt.LeftButton)
    else:o.d._apply_recycle(((o.second,NS()),),False)  # Component submission;no preview/writer execution.
    wait_for(o.entered.is_set);assert o.d._retaining_write_result();return o.d._task


def finish(o):o.release.set();wait_for(lambda:o.d._task is None);QApplication.processEvents()


def cleanup(o):
    o.release.set();assert o.manager.shutdown(wait=True,timeout=2)
    QApplication.processEvents();o.d.close()


@pytest.mark.parametrize('mode',['','preflight'])
@pytest.mark.parametrize('route',['close','escape','reject','done'])
@pytest.mark.parametrize('outcome',['success','failure'])
def test_pending_mark_completion_retains_exact_result(qt_test_scope,monkeypatch,mode,route,outcome):
    o=make(qt_test_scope,monkeypatch,mode);o.mode=outcome
    try:
        task=start(o)
        if route=='escape':QTest.keyClick(o.d,Qt.Key_Escape)
        elif route=='done':o.d.done(QDialog.Rejected)
        else:getattr(o.d,route)()
        assert o.d.isVisible() and not o.finished and task._observer and not task._cancelled and not o.d.resolve.isEnabled()
        finish(o);assert o.d.isVisible() and not o.finished and o.attempts==['mark'] and o.effects==['mark']
        if outcome=='success':assert o.saved==[o.report] and 'decision saved' in o.d.search_status.text()
        else:assert not o.saved and 'not confirmed' in o.d.search_status.text() and o.warnings
        o.d.close();assert o.finished==[QDialog.Rejected]
    finally:cleanup(o)


@pytest.mark.parametrize('route',['close','escape','reject','done'])
@pytest.mark.parametrize('outcome',['success','failure'])
def test_pending_recycle_completion_retains_receipt(qt_test_scope,monkeypatch,route,outcome):
    o=make(qt_test_scope,monkeypatch);o.mode=outcome
    try:
        task=start(o,'recycle')
        if route=='escape':QTest.keyClick(o.d,Qt.Key_Escape)
        elif route=='done':o.d.done(QDialog.Rejected)
        else:getattr(o.d,route)()
        assert o.d.isVisible() and not o.finished and task._observer and not task._cancelled
        finish(o);assert o.attempts==o.effects==['recycle'] and not o.finished
        if outcome=='success':assert o.changed==[['generated-second.png']] and 'Confirmed moved: 1' in o.d.search_status.text()
        else:assert not o.changed and 'Confirmed moved: 0' in o.d.search_status.text() and 'Unconfirmed: 1' in o.d.search_status.text() and o.warnings
    finally:cleanup(o)


@pytest.mark.parametrize('kind',['mark','recycle'])
def test_write_cancel_control_does_not_detach_or_replay(qt_test_scope,monkeypatch,kind):
    o=make(qt_test_scope,monkeypatch)
    try:
        task=start(o,kind);o.d.cancel_work();assert not task._cancelled and task._observer and o.d._task is task and o.d.cancel.isHidden()
        finish(o);assert o.attempts==[kind] and not o.finished
    finally:cleanup(o)


@pytest.mark.parametrize('kind',['mark','recycle'])
def test_external_late_cancellation_retains_honest_uncertainty(qt_test_scope,monkeypatch,kind):
    o=make(qt_test_scope,monkeypatch)
    try:
        task=start(o,kind);o.manager.cancel(task.job_id);assert task._observer
        finish(o);assert o.effects==[kind] and o.attempts==[kind]
        if kind=='mark':assert 'not confirmed' in o.d.search_status.text()
        else:assert 'Confirmed moved: 1' in o.d.search_status.text() and 'Unconfirmed: 0' in o.d.search_status.text()
        assert 'stopped before' not in o.d.search_status.text() and not o.finished
    finally:cleanup(o)


@pytest.mark.parametrize('mode',['','preflight'])
def test_readonly_close_still_detaches_search(qt_test_scope,monkeypatch,mode):
    o=make(qt_test_scope,monkeypatch,mode);entered,release=Event(),Event()
    def work(job):entered.set();assert release.wait(10);return o.report
    task=CoordinatedCallTask(o.manager,'Generated read-only search',work,resource='cpu');o.d._task=task;task.start()
    try:
        wait_for(entered.is_set);o.d.close();assert not o.d.isVisible() and task._cancelled and not task._observer
    finally:release.set();cleanup(o)


def test_recycle_outer_authority_failure_reports_unknown_outcome(qt_test_scope,monkeypatch):
    o=make(qt_test_scope,monkeypatch,'preflight');o.d.frozen_authority_currentness=lambda:False
    try:
        o.d._apply_recycle(((o.second,NS()),),False);wait_for(lambda:o.d._task is None)
        assert not o.attempts and not o.changed and o.warnings and 'Not attempted: 1' in o.d.search_status.text()
        assert 'no image changed' not in o.d.search_status.text()
    finally:cleanup(o)


@pytest.mark.parametrize('theme',['dark','light'])
@pytest.mark.parametrize('kind',['mark','recycle'])
def test_visible_delayed_write_and_known_result(qt_test_scope,monkeypatch,theme,kind):
    from qt_app.theme import apply_theme
    o=make(qt_test_scope,monkeypatch);group=theme+'-duplicate-'+kind;apply_theme(o.scope.app,theme,False)
    try:
        start(o,kind,group);QTest.qWait(1100);p=o.d._window_job_progress
        assert p.activity.active and 'Elapsed 1' in p.bar.format() and not p.stop.isVisible()
        assert 'accepted result' in o.d.search_status.text() and 'Ready to search' not in o.d.search_status.text()
        capture(o.d,group,'02_pending_result');o.d.close();assert o.d.isVisible()
        finish(o);assert not o.finished;capture(o.d,group,'03_known_result')
    finally:cleanup(o)


@pytest.mark.parametrize('theme',['dark','light'])
@pytest.mark.parametrize('kind',['mark','recycle'])
def test_visible_external_cancellation_uncertainty(qt_test_scope,monkeypatch,theme,kind):
    from qt_app.theme import apply_theme
    o=make(qt_test_scope,monkeypatch);group=theme+'-duplicate-'+kind+'-uncertain';apply_theme(o.scope.app,theme,False)
    try:
        task=start(o,kind);QTest.qWait(1100);capture(o.d,group,'01_accepted_pending')
        o.manager.cancel(task.job_id);finish(o)
        assert o.effects==[kind] and o.attempts==[kind]
        if kind=='mark':assert 'not confirmed' in o.d.search_status.text()
        else:assert 'Confirmed moved: 1' in o.d.search_status.text() and 'Unconfirmed: 0' in o.d.search_status.text()
        capture(o.d,group,'02_uncertainty_retained');assert not o.finished
    finally:cleanup(o)
