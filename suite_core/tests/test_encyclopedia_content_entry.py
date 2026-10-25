"""Explicit standalone content enrollment over stock controller; synthetic only."""
import threading
from pathlib import Path
import pytest
from core.tag_default_acquisition import TagDefaultAcquisition,TagDefaultBaseline
from suite_core.tests.test_encyclopedia import app
from suite_core.tests.test_encyclopedia_catalog_ui import settle,click
from suite_core.tests.test_encyclopedia_downloaded_defaults import content_font
from suite_core.encyclopedia.__main__ import create_window


@pytest.mark.parametrize('theme',['light','dark'])
def test_entry_enrollment_reaches_original_refresh_without_implicit_io(app,tmp_path,monkeypatch,theme,content_font):
    from qt_app.theme import apply_theme
    from PySide6.QtGui import QFont
    apply_theme(app,theme)
    app.setFont(QFont('Segoe UI',10))
    service=TagDefaultAcquisition();events=[];owner=threading.get_ident()
    original=service.discover
    def discover(**kwargs):
        events.append((kwargs['channel'],threading.get_ident()))
        return original(**kwargs)
    monkeypatch.setattr(service,'discover',discover)
    def factory(host):
        assert host.default_acquisition is None
        events.append(('factory',threading.get_ident()))
        return dict(service=service,observe_baseline=lambda:TagDefaultBaseline(
            'synthetic-catalog','synthetic-revision','synthetic-pack',observation_status='verified_empty'),
            package_component=None,preparation_directory=None,channel='beta')
    window=create_window(settings_path=tmp_path/'settings.json',default_content_factory=factory)
    window.resize(1920,1080);window.show();app.processEvents()
    try:
        host=window.catalog_host
        assert host.default_acquisition.service is service
        assert events==[('factory',owner)] and not (tmp_path/'settings.json').exists()
        click(window,'Use optional default tags');app.processEvents()
        assert host.content_refresh.isEnabled()
        host.content_refresh.click();settle(app,host)
        assert events[1][0]=='beta' and events[1][1]!=owner
        assert host.default_acquisition.discovery.status=='unable_to_check'
        assert not host.content_download.isEnabled() and not host.content_review.isEnabled()
        assert not (tmp_path/'settings.json').exists()
        capture=Path('.tmp/encyclopedia-root-replacement-20261012/entry-successor41/visuals')
        capture.mkdir(parents=True,exist_ok=True)
        assert host.dialog.grab().save(str(capture/(theme+'.png')))
    finally:
        window.close();app.processEvents()


def test_entry_without_enrollment_keeps_local_review_available(app,tmp_path):
    window=create_window(settings_path=tmp_path/'settings.json')
    window.resize(1920,1080);window.show();app.processEvents()
    try:
        click(window,'Use optional default tags');app.processEvents()
        assert window.catalog_host.default_acquisition is None
        from PySide6.QtTest import QSignalSpy
        tick = QSignalSpy(window.catalog_host.timer.timeout)
        assert tick.wait(1000), "ordinary content state poll did not run"
        assert not window.catalog_host.content_refresh.isEnabled()
        assert window.catalog_host.pane is not None
        assert not (tmp_path/'settings.json').exists()
    finally:window.close();app.processEvents()


@pytest.mark.parametrize('change',['unknown_field','unsupported_channel'])
def test_invalid_entry_enrollment_retires_owner_without_settings(app,tmp_path,change):
    hosts=[]
    def factory(host):
        hosts.append(host)
        result=dict(service=TagDefaultAcquisition(),observe_baseline=lambda:None,
            package_component=None,preparation_directory=None,channel='stable')
        if change=='unknown_field':result['trust_override']=True
        else:result['channel']='invented'
        return result
    with pytest.raises(ValueError):create_window(settings_path=tmp_path/'settings.json',default_content_factory=factory)
    app.processEvents()
    assert hosts[0].executor.closed and hosts[0].default_acquisition is None
    assert not (tmp_path/'settings.json').exists()


def test_enrollment_failure_retains_original_window_if_close_is_held(app,tmp_path,monkeypatch):
    from suite_core.encyclopedia import __main__ as entry
    from suite_core.encyclopedia.catalog_ui import EncyclopediaCatalogHost
    original=EncyclopediaCatalogHost.close_ready
    before=len(entry._HELD_ENTRIES)
    monkeypatch.setattr(EncyclopediaCatalogHost,'close_ready',lambda host:False)
    with pytest.raises(ValueError):
        create_window(settings_path=tmp_path/'settings.json',default_content_factory=lambda host:{})
    window=entry._HELD_ENTRIES[-1]
    try:
        assert len(entry._HELD_ENTRIES)==before+1 and window.isVisible()
        assert not window.catalog_host.executor.closed
        assert not (tmp_path/'settings.json').exists()
    finally:
        monkeypatch.setattr(EncyclopediaCatalogHost,'close_ready',original)
        window.close();entry._HELD_ENTRIES.pop();app.processEvents()
