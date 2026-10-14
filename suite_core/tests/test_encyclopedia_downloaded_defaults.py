"""Signed synthetic data download -> existing local review/import controls."""
from dataclasses import replace
from time import monotonic,sleep
from threading import get_ident
import pytest
from PySide6.QtCore import Qt,QPoint
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog,QScrollArea,QCheckBox
from tests.test_tag_catalog_connection import selected,connect
from tests.test_tag_database_pack_producer import reviewed_local_input
from tests.test_tag_default_package import no_external


@pytest.fixture
def content_font(qt_test_scope):
    from PySide6.QtGui import QFontDatabase,QFont
    from PySide6.QtWidgets import QApplication
    from qt_app.theme import apply_theme
    font_id=QFontDatabase.addApplicationFont('C:/Windows/Fonts/segoeui.ttf')
    assert font_id>=0
    apply_theme(QApplication.instance(),'light',False)
    QApplication.instance().setFont(QFont(QFontDatabase.applicationFontFamilies(font_id)[0],10))
    try:
        yield
    finally:
        QFontDatabase.removeApplicationFont(font_id)


def test_preparation_refuses_catalog_cache_overlap_before_any_output(tmp_path):
    from core.encyclopedia_catalog import prepare_downloaded_default_review
    from core.tag_catalog_connection import TagConnectionHold
    catalog=tmp_path/'catalog';catalog.mkdir()
    destination=catalog/'unsafe-source-cache'
    with pytest.raises(TagConnectionHold,match='separate'):
        prepare_downloaded_default_review(None,destination=destination,trust_bytes=None,
            component=None,acquisition_cache_root=None,settings_path=tmp_path/'settings.json',catalog_root=catalog)
    assert not destination.exists() and list(catalog.iterdir())==[]


@pytest.mark.parametrize('ending',['apply','apply_repeated','apply_close','stale','cancel','download_failure'])
def test_visible_signed_download_review_connect_preview_apply_preserves_local_tag(qt_test_scope,tmp_path,selected,monkeypatch,ending,content_font):
    from pathlib import Path
    from qt_app.theme import apply_theme
    from PySide6.QtWidgets import QApplication
    captures=Path('.tmp/encyclopedia-root-replacement-20261012/package-captures08')
    captures.mkdir(parents=True,exist_ok=True)
    from tests import test_tag_default_package as fixtures
    from core.tag_database import TagDatabase
    from core.tag_default_acquisition import TagDefaultAcquisition,TagDefaultBaseline,TAG_DATABASE_COMPONENT
    from suite_core.manager.tag_database_controller import empty_default_layer
    from suite_core.encyclopedia.__main__ import create_window
    from suite_core.tests.test_encyclopedia_catalog_ui import click
    original_service=fixtures.service;retained=[]
    def record_service(*args,**kwargs):
        stock=original_service(*args,**kwargs);retained.append(stock);return stock
    monkeypatch.setattr(fixtures,'service',record_service)
    package_root=tmp_path/'signed';package_root.mkdir()
    _acquired,trust,component,_publication,_recipient=fixtures.acquired_fixture(package_root)
    stock=retained[-1]
    choice=connect(selected);settings,_source,catalog=selected
    assert empty_default_layer(choice.native_adapter)
    from core.category_registry import CategoryRegistry
    categories=CategoryRegistry(catalog/'data/defaults/categories.json',catalog/'data/user/categories.json')
    local_category=categories.add('Local corrections');categories.save_user()
    tags=TagDatabase(catalog/'data/user/tags.json')
    local=tags.add_known_tag('local_custom',category=local_category.id,properties={'user_note':'keep this correction'})
    tags.save_verified()
    baseline=TagDefaultBaseline(choice.native_adapter.catalog_id,choice.native_adapter.revision(),
        _acquired.baseline.pack_id,observation_status='verified_empty')
    gui=get_ident()
    def observe():
        assert get_ident()!=gui
        return replace(baseline,catalog_revision=choice.native_adapter.revision())
    window=create_window(settings_path=settings);host=window.catalog_host
    original_executor=host.executor
    service=TagDefaultAcquisition(acquisition=stock,component_id=TAG_DATABASE_COMPONENT)
    host.configure_default_acquisition(service=service,observe_baseline=observe,
        package_component=component,preparation_directory=tmp_path/'prepared')
    monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *args:str(catalog))
    window.resize(1920,1080);qt_test_scope.show(window)
    def settle():
        deadline=monotonic()+10
        while host.busy and monotonic()<deadline:
            qt_test_scope.settle(1);sleep(.005)
        assert not host.busy,window.status.text()
        host.poll();qt_test_scope.settle(1)
    def press(widget):
        parent=widget.parentWidget()
        while parent is not None:
            if isinstance(parent,QScrollArea):parent.ensureWidgetVisible(widget)
            parent=parent.parentWidget()
        qt_test_scope.settle(1)
        assert widget.isVisible() and widget.isEnabled()
        if isinstance(widget,QCheckBox):
            QTest.mouseClick(widget,Qt.LeftButton,pos=QPoint(10,widget.height()//2))
        else:
            QTest.mouseClick(widget,Qt.LeftButton)
        qt_test_scope.settle(1)
    click(window,'Open Tag Database');settle()
    assert host.accepted_source is not None,window.status.text()+window.status.toolTip()
    click(window,'Use optional default tags')
    host.poll();pane=host.pane
    before_tags=(catalog/'data/user/tags.json').read_bytes()
    press(host.content_refresh);settle()
    assert host.content_offers.count()==1
    press(host.content_confirm);host.poll()
    if ending=='download_failure':
        _acquired.acquired.artifact.write_bytes(b'Synthetic corrupt cached payload')
        def unavailable(*args,**kwargs):
            raise OSError('Synthetic transport interrupted')
        monkeypatch.setattr(stock.transport,'download_artifact',unavailable)
    press(host.content_download);settle()
    assert (catalog/'data/user/tags.json').read_bytes()==before_tags
    if ending=='download_failure':
        assert host.default_acquisition.error and host.default_acquisition.result is None
        assert 'failed' in window.status.text() and 'Refresh content releases' in window.status.text()
        assert 'Verified Tag data offer available' not in window.status.text()
        assert not host.content_review.isEnabled() and host.content_refresh.isEnabled()
        host.dialog.close();window.close();qt_test_scope.settle();return
    if ending=='apply':
        assert host.dialog.grab().save(str(captures/'light-downloaded.png'))
    if ending=='stale':
        host.invalidate_default_baseline();host.poll()
        assert not host.content_review.isEnabled() and not host.review_downloaded_content()
        assert host.prepared_default_review is None and not list(tmp_path.glob('prepared-*'))
        assert (catalog/'data/user/tags.json').read_bytes()==before_tags
        host.dialog.close();window.close();qt_test_scope.settle();return
    if ending=='cancel':
        from threading import Event
        from core import encyclopedia_catalog
        entered=Event();release=Event();original_prepare=encyclopedia_catalog.prepare_downloaded_default_review
        def delayed(*args,**kwargs):
            entered.set();assert release.wait(5)
            return original_prepare(*args,**kwargs)
        monkeypatch.setattr(encyclopedia_catalog,'prepare_downloaded_default_review',delayed)
        host.poll();press(host.content_review)
        assert entered.wait(1)
        host.poll();press(window.cancel_work_button)
        release.set();settle()
        assert host.prepared_default_review is None and not list(tmp_path.glob('prepared-*'))
        assert (catalog/'data/user/tags.json').read_bytes()==before_tags
        host.dialog.close();window.close();qt_test_scope.settle();return
    host.poll();press(host.content_review);settle()
    assert host.prepared_default_review is not None,window.status.text()+window.status.toolTip()
    assert host.prepared_default_review.package.component is component
    assert (catalog/'data/user/tags.json').read_bytes()==before_tags
    assert host.controller.connection_review is host.prepared_default_review.connection_review
    assert host.dialog.grab().save(str(captures/'light-source-review.png'))
    press(pane.confirm_connection);press(pane.connect_catalog);settle()
    assert host.controller.choice is not None,host.controller.reason+' '+host.controller.error
    assert host.controller.choice.observation_status=='current',host.controller.reason+' '+host.controller.error
    press(pane.preview);settle()
    assert host.controller.review.can_apply,host.controller.review.holds
    assert (catalog/'data/user/tags.json').read_bytes()==before_tags
    assert host.dialog.grab().save(str(captures/'light-import-review.png'))
    retained_apply=None
    if ending in {'apply_repeated','apply_close'}:
        from threading import Event
        entered=Event();release_apply=Event();apply_calls=[]
        service=host.controller.choice.transactions;original_apply=service.apply
        def held_apply(*args,**kwargs):
            apply_calls.append(1);entered.set();assert release_apply.wait(5)
            return original_apply(*args,**kwargs)
        monkeypatch.setattr(service,'apply',held_apply)
        retained_apply=host.executor
    press(pane.confirm);press(pane.apply)
    if ending in {'apply_repeated','apply_close'}:
        assert entered.wait(1)
        original_future=host.controller._future
        QTest.mouseClick(pane.apply,Qt.LeftButton)
        qt_test_scope.settle(1)
        assert host.controller._future is original_future and len(apply_calls)==1
        assert not pane.apply.isEnabled()
        if ending=='apply_close':
            assert not window.close()
            assert host._closing and host.executor is retained_apply and host.executor.close_held
        release_apply.set()
    settle()
    if ending in {'apply_repeated','apply_close'}:
        assert len(apply_calls)==1 and host.controller.choice.capability_report is None
        assert host.controller.choice.capability_inventory==() and host.controller.choice.capability_revision==''
        if ending=='apply_close':
            assert not window.isVisible() and not host.executor.close_held and host.executor.closed
            saved=TagDatabase(catalog/'data/user/tags.json').find('local_custom')
            assert saved.id==local.id and saved.properties['user_note']=='keep this correction'
            return
    saved=TagDatabase(catalog/'data/user/tags.json').find('local_custom')
    assert saved.id==local.id and saved.properties['user_note']=='keep this correction'
    assert host.executor is original_executor and host.default_acquisition.service.acquisition is stock
    assert not host.prepared_default_review.package.production_install_authorized
    assert window.publication is not None and window.model.total_count>1
    assert host.dialog.grab().save(str(captures/'light-saved.png'))
    apply_theme(QApplication.instance(),'dark',False);qt_test_scope.settle()
    assert host.dialog.grab().save(str(captures/'dark-saved.png'))
    host.dialog.close()
    click(window,'Refresh');settle()
    assert window.lookup.resolve('local_custom').tag.id==local.id
    assert window.grab().save(str(captures/'dark-reopened-reader.png'))
    window.close();qt_test_scope.settle()
    reopened=create_window(settings_path=settings);new_host=reopened.catalog_host
    qt_test_scope.show(reopened)
    click(reopened,'Open Tag Database')
    deadline=monotonic()+10
    while new_host.busy and monotonic()<deadline:
        qt_test_scope.settle(1);sleep(.005)
    assert not new_host.busy and new_host.accepted_source.path==catalog
    assert reopened.lookup.resolve('local_custom').tag.id==local.id
    assert TagDatabase(catalog/'data/user/tags.json').find('local_custom').properties['user_note']=='keep this correction'
    reopened.close();qt_test_scope.settle()
