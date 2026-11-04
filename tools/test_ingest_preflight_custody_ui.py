"""Original Ingest methods on generated QWidget shell, not full host entry."""
from types import SimpleNamespace as NS
from dataclasses import replace
import pytest
from PySide6.QtCore import Qt
from PySide6.QtWidgets import QApplication,QDialog,QWidget
from PySide6.QtTest import QTest
from qt_app.ingest_page import IngestPage
from qt_app.workers import CoordinatedCallTask
import test_duplicate_preflight_custody_ui as outer
from duplicate_custody_test_support import wait_for,qt_test_scope

def page_for(p):
    # Deliberately skip IngestPage.__init__: no normal profile/gallery/scan startup.
    page=IngestPage.__new__(IngestPage);QWidget.__init__(page)
    page._batch_task=None;page.state=NS();page._duplicate_preflight_dialog=p.d
    p.d.context=replace(p.d.context,owner=page)
    page.events=[]
    page.background_work_finished.connect(lambda:page.events.append(
        {'closed':p.d._closed,'registered':page._duplicate_preflight_dialog,'outer':tuple(p.finished),'child':tuple(p.o.finished)}))
    return page


@pytest.fixture
def owner(qt_test_scope,monkeypatch):
    p=outer.make(qt_test_scope,monkeypatch);page=page_for(p)
    try:yield p,page
    finally:outer.cleanup(p);page.close()


def test_stock_queued_finished_rechecks_after_result(qt_test_scope):
    page=IngestPage.__new__(IngestPage);QWidget.__init__(page);page._batch_task=None
    d=QDialog();events=[];page.background_work_finished.connect(lambda:events.append(d.result()))
    d.finished.connect(page._notify_background_work_finished,Qt.QueuedConnection)
    d.done(QDialog.Accepted);assert events==[]
    wait_for(lambda:events==[QDialog.Accepted]);page.close()


@pytest.mark.parametrize('purpose',['preparation','gallery'])
@pytest.mark.parametrize('kind',['mark','recycle'])
def test_pending_known_result_and_deliberate_completion(qt_test_scope,monkeypatch,purpose,kind):
    p=outer.make(qt_test_scope,monkeypatch,purpose);page=page_for(p);o=p.o
    try:
        task=outer.custody.start(o,kind)
        assert p.d._task is None and page.has_active_background_work()
        for _ in range(3):
            assert page.request_background_shutdown();QApplication.processEvents()
            assert not page.events and not p.finished and page._duplicate_preflight_dialog is p.d
        relay=p.d._ingest_shutdown_notification
        assert task._observer and not task._cancelled
        outer.custody.finish(o)
        assert page.has_active_background_work() and not page.events and not p.finished
        assert o.attempts==o.effects==[kind]
        if kind=='mark':assert o.saved==[o.report]
        else:assert o.changed==[['generated-second.png']]
        # A deliberate later outer action is still necessary after the known result.
        p.d.reject();assert not page.events and p.finished==[QDialog.Rejected]
        wait_for(lambda:len(page.events)==1)
        assert page.events==[{'closed':True,'registered':None,'outer':(QDialog.Rejected,),'child':(QDialog.Rejected,)}]
        assert not page.has_active_background_work() and p.d._ingest_shutdown_notification is relay
        assert not page.request_background_shutdown();QTest.qWait(20);assert len(page.events)==1
    finally:outer.cleanup(p);page.close()


def test_idle_owner_drains_with_one_notification(owner):
    p,page=owner
    assert page.has_active_background_work() and page.request_background_shutdown()
    assert p.finished==[QDialog.Rejected] and not page.events
    wait_for(lambda:len(page.events)==1);QTest.qWait(20)
    assert len(page.events)==1 and not page.has_active_background_work()


@pytest.mark.parametrize('location',['parent','review'])
def test_readonly_shutdown_detaches_and_physically_drains(owner,location):
    p,page=owner;o=p.o
    def work(job):o.entered.set();assert o.release.wait(10);return o.report
    task=CoordinatedCallTask(o.manager,'Generated Ingest read-only control',work,resource='cpu')
    target=p.d if location=='parent' else o.d;target._task=task
    if location=='parent':p.d._task_phase='scope'
    task.start();wait_for(o.entered.is_set)
    assert page.request_background_shutdown() and task._cancelled and not task._observer
    wait_for(lambda:len(page.events)==1);o.release.set();assert o.manager.shutdown(wait=True,timeout=2)
    assert len(page.events)==1 and not page.has_active_background_work()


def test_delayed_recheck_preserves_other_work_and_replacement_owner(owner):
    p,page=owner;o=p.o;outer.custody.start(o,'mark');assert page.request_background_shutdown()
    QApplication.processEvents();outer.custody.finish(o)
    replacement=NS(_closed=False,_task=None)
    page._duplicate_preflight_dialog=replacement;p.d.reject()
    QTest.qWait(20);assert not page.events and page.has_active_background_work()
    assert page._duplicate_preflight_dialog is replacement
    page._duplicate_preflight_dialog=None;page._conflict_task=object()
    page._notify_background_work_finished();assert not page.events
    page._conflict_task=None;page._notify_background_work_finished()
    assert len(page.events)==1 and not page.has_active_background_work()
