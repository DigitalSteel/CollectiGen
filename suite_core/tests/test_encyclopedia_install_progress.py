"""Delayed source UI evidence; synthetic original staging, not native qualification."""
from pathlib import Path
from threading import Event
from concurrent.futures import Future
import time
import pytest
from PySide6.QtCore import QTimer
from PySide6.QtWidgets import QLabel
from PySide6.QtWidgets import QApplication
from tools.qt_test_support import QtTestScope
from suite_core.tests.test_encyclopedia_acquisition_ui import click, wait, queue_confirmation
from tests.test_encyclopedia_installation import Fixture
from suite_core.manager.encyclopedia_acquisition_ui import EncyclopediaAcquisitionPanel
from suite_core.manager.resource_executor import ResourceExecutorOwner


def spin(scope, predicate, timeout=5):
    deadline=time.monotonic()+timeout
    while not predicate() and time.monotonic()<deadline:
        time.sleep(.002);scope.settle()
    assert predicate()


def test_slow_install_exposes_real_phases_before_completion(tmp_path,monkeypatch):
    fixture=Fixture(tmp_path,monkeypatch);fixture.add()
    owner=ResourceExecutorOwner();entered=Event();release=Event();phases=[]
    original=fixture.service.install
    def delayed(reviewed,**kwargs):
        notify=kwargs.pop('phase_progress')
        def phase(value):
            phases.append(value);notify(value)
            if value=='Preparing reader files':
                entered.set();assert release.wait(5)
        return original(reviewed,phase_progress=phase,**kwargs)
    monkeypatch.setattr(fixture.service,'install',delayed)
    with QtTestScope() as scope:
        returned=[]
        panel=EncyclopediaAcquisitionPanel(fixture.service,owner,return_to_applications=lambda:returned.append(True))
        panel.resize(1000,740);scope.show(panel)
        try:
            click(panel.refresh);wait(panel)
            queue_confirmation();click(panel.install)
            spin(scope,entered.is_set)
            spin(scope,lambda:'Preparing reader files' in panel.status.text())
            assert panel.future is not None and not panel.future.done()
            assert panel.activity.isVisible() and panel.activity.maximum()==0
            assert panel.elapsed.isVisible() and 'seconds' in panel.elapsed.text()
            assert not panel.install.isEnabled() and not panel.refresh.isEnabled()
            ticks=[];timer=QTimer(panel);timer.timeout.connect(lambda:ticks.append(True));timer.start(10)
            spin(scope,lambda:len(ticks)>=2);timer.stop()
            release.set();wait(panel)
            assert 'Verifying the downloaded reader' in phases
            assert 'Installing reader files' in phases and 'Saving the reader connection' in phases
            assert not panel.activity.isVisible() and not panel.elapsed.isVisible()
            assert 'Open the reader' in panel.status.text()
            assert fixture.store.selected(fixture.store.observe()) is not None
            assert panel.return_action.isVisible() and panel.return_action.property('primary') is True
            assert panel.refresh.property('primary') is not True
            click(panel.return_action);assert returned==[True]
        finally:
            release.set()
            if panel.close_held:wait(panel)
            panel.timer.stop();panel.close();owner.begin_close()


@pytest.mark.parametrize('failure',[False,True])
def test_cancel_or_failure_remains_responsive_and_actionable(tmp_path,monkeypatch,failure):
    fixture=Fixture(tmp_path,monkeypatch);fixture.add()
    owner=ResourceExecutorOwner();entered=Event();release=Event()
    original=fixture.service.discover
    def delayed(**kwargs):
        entered.set();assert release.wait(5)
        if failure:raise OSError('synthetic private-path diagnostic')
        return original(**kwargs)
    monkeypatch.setattr(fixture.service,'discover',delayed)
    with QtTestScope() as scope:
        panel=EncyclopediaAcquisitionPanel(fixture.service,owner);scope.show(panel)
        try:
            click(panel.refresh);spin(scope,entered.is_set)
            assert panel.activity.isVisible() and panel.activity.maximum()==0
            if not failure:
                click(panel.stop)
                assert 'Cancellation requested' in panel.status.text()
                assert panel.close_held and not panel.stop.isEnabled()
            release.set();wait(panel)
            assert panel.refresh.isEnabled() and not panel.activity.isVisible()
            assert 'synthetic private-path' not in panel.status.text()
            assert not panel.details.isVisible()
        finally:
            release.set()
            if panel.close_held:wait(panel)
            panel.timer.stop();panel.close();owner.begin_close()


def test_novice_initial_1080p_capture_and_honest_progress(tmp_path,monkeypatch):
    fixture=Fixture(tmp_path,monkeypatch);fixture.add();owner=ResourceExecutorOwner()
    with QtTestScope() as scope:
        from qt_app.theme import apply_theme
        apply_theme(QApplication.instance(), 'light')
        panel=EncyclopediaAcquisitionPanel(fixture.service,owner);panel.resize(1920,1080);scope.show(panel)
        try:
            assert any('lets you browse your tags and categories' in label.text() for label in panel.findChildren(QLabel))
            assert not panel.details.isVisible() and not panel.activity.isVisible()
            assert panel.refresh.text()=='Find reader'
            assert panel.refresh.property('primary') is True
            assert panel.refresh.geometry().top()<260
            assert not panel.notes_container.isVisible() and not panel.details_container.isVisible()
            out=tmp_path/'install-ux'
            out.mkdir(parents=True,exist_ok=True)
            assert panel.grab().save(str(out/'novice-initial-source.png'))
            panel.future=Future();panel.operation='Installing reviewed package';panel.started_at=time.monotonic()
            panel.progress.put((50,100));panel.poll()
            assert panel.activity.value()==50
            panel.progress.put(('phase','Verifying the downloaded reader'));panel.poll()
            assert panel.activity.maximum()==0 and '%' not in panel.status.text()
            panel.progress.put((20,None));panel.poll()
            assert panel.activity.maximum()==0
        finally:
            panel.future=None;panel.operation=None;panel.timer.stop();panel.close();owner.begin_close()
