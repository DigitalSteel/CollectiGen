"""Supplied context projection and draft delivery, never native acceptance."""
from dataclasses import replace
from pathlib import Path
from types import SimpleNamespace

import pytest

from suite_core.manager.database_presentation import project_connected_catalog
from suite_core.manager.tag_database_controller import TagDatabaseController, TagDatabaseChoice
from suite_core.manager.tag_database_ui import TagDatabasePane
from suite_core.knowledge.tag_transactions import TagCatalogTransactions
from suite_core.database_actions import CatalogRelease
from suite_core.knowledge import Source, Snapshot
from suite_core.knowledge.catalog_migration_preview import PackRevision


@pytest.mark.parametrize('size',[(1000,540),(1600,930)])
def test_keyboard_reader_viewport_is_fully_visible_and_state_is_preserved(tmp_path,qt_test_scope,request,size):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    executor=ResourceExecutorOwner();request.addfinalizer(executor.begin_close)
    controller=TagDatabaseController(executor=executor,choices=(supplied_choice(tmp_path),),settings_path=tmp_path/'settings.json')
    pane=TagDatabasePane(controller);pane.timer.stop();qt_test_scope.show(pane)
    pane.resize(*size);QApplication.instance().processEvents()
    assert (pane.width(),pane.height())==size
    pane.connection_options.setChecked(False);pane.detail_options.setChecked(True)
    pane.source_directory.setText('retained-source');pane.catalog_directory.setText('retained-catalog')
    pane.details.setPlainText('\n'.join('Synthetic review line '+str(i) for i in range(24)))
    expected=(pane.source_directory.text(),pane.catalog_directory.text(),pane.create_catalog.isChecked(),controller.choice)
    app=QApplication.instance();pane.sections.setFocus();app.processEvents()
    QTest.keyClick(pane.sections,Qt.Key_Tab)
    for _ in range(4):app.processEvents()
    assert pane.details.hasFocus()
    assert (pane.width(),pane.height())==size
    viewport=pane.details.viewport()
    facts={'size':(pane.width(),pane.height()),'minimum':(pane.minimumWidth(),pane.minimumHeight()),
        'outer':pane._review_scroll.viewport().height(),'reader_height':pane.details.height(),
        'reader_max':pane.details.maximumHeight(),'reader_min':pane.details.minimumHeight(),
        'visible':viewport.visibleRegion().boundingRect().height(),'viewport':viewport.height()}
    assert facts['visible']==facts['viewport'],facts
    assert pane.details.verticalScrollBar().maximum()>0
    QTest.keyClick(pane.details,Qt.Key_PageDown);app.processEvents()
    assert pane.details.verticalScrollBar().value()>0
    assert expected==(pane.source_directory.text(),pane.catalog_directory.text(),pane.create_catalog.isChecked(),controller.choice)
    assert not pane.confirm_connection.isChecked() and not (tmp_path/'settings.json').exists()
    for control in [pane.apply,pane.recover,pane.reload,pane.status]:
        assert control.visibleRegion().boundingRect().height()==control.height()


def supplied_choice(tmp_path):
    snapshot=Snapshot(Source('Synthetic', '1', 'a'*64, ()), (), (), ())
    release=CatalogRelease(PackRevision('synthetic','1',snapshot),'2026-10-25',('Synthetic',),'Synthetic')
    transaction=object.__new__(TagCatalogTransactions)
    transaction.controller=None
    transaction.host=SimpleNamespace(root=tmp_path/'accepted-catalog')
    transaction.session=SimpleNamespace(binding='synthetic')
    return TagDatabaseChoice('Synthetic choice',transaction,release,release,tmp_path/'source/TagDatabase.zip',False)


def test_context_projection_has_no_filesystem_or_settings_reads(tmp_path,monkeypatch):
    choice=supplied_choice(tmp_path)
    # The pure projector receives a supplied context, not a native authority grant.
    context=SimpleNamespace(native_adapter=SimpleNamespace(root=choice.transactions.host.root),
        transactions=choice.transactions,pack=choice.pack)
    def forbidden(*a,**k):raise AssertionError('Projection must not probe disk')
    for name in ['open','exists','is_file','is_dir','resolve','stat']:
        monkeypatch.setattr(Path,name,forbidden)
    assert 'No catalog connected' in project_connected_catalog(None)
    assert 'unavailable' in project_connected_catalog(choice)
    text=project_connected_catalog(context)
    assert str(context.transactions.host.root) in text and str(context.pack.parent) in text
    context.native_adapter.root=tmp_path/'other'
    assert 'unavailable' in project_connected_catalog(context)


def test_fixed_review_actions_and_keyboard_details_preserve_draft(qt_test_scope,tmp_path):
    from PySide6.QtCore import Qt, QPoint
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    controller=TagDatabaseController(choices=(supplied_choice(tmp_path),),settings_path=tmp_path/'settings.json')
    pane=TagDatabasePane(controller);pane.timer.stop();pane.resize(1300,900);qt_test_scope.show(pane)
    pane.connection_options.setChecked(True);pane.source_directory.setText('pending-source')
    pane.catalog_directory.setText('pending-catalog');pane.create_catalog.setChecked(True)
    QApplication.processEvents()
    for control in [pane.preview,pane.confirm,pane.apply,pane.receipts,pane.recover]:
        assert pane.rect().contains(control.mapTo(pane,QPoint(0,0)))
        assert pane.rect().contains(control.mapTo(pane,control.rect().bottomRight()))
    assert not pane.detail_options.isChecked() and not pane.detail_body.isVisible()
    pane.detail_options.setFocus();QTest.keyClick(pane.detail_options,Qt.Key_Space);QApplication.processEvents()
    assert pane.detail_body.isVisible()
    QTest.keyClick(pane.detail_options,Qt.Key_Tab);QApplication.processEvents();assert pane.sections.hasFocus()
    pane.detail_options.setFocus();QTest.keyClick(pane.detail_options,Qt.Key_Space);QApplication.processEvents()
    assert not pane.detail_body.isVisible() and pane.source_directory.text()=='pending-source'
    assert pane.catalog_directory.text()=='pending-catalog' and pane.create_catalog.isChecked()
    assert controller.review is None and not pane.confirm.isChecked()


def _pending_saved_host(kind='connection_open'):
    from concurrent.futures import Future
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    from suite_core.manager.tag_database_controller import _Operation
    from suite_core.encyclopedia.catalog_ui import EncyclopediaCatalogHost
    executor=ResourceExecutorOwner();controller=TagDatabaseController(executor=executor)
    controller._future=executor._future=Future();controller._operation=_Operation(kind)
    host=SimpleNamespace(controller=controller,executor=executor,_closing=False,
        future=None,_creation_confirming=False,help=None,entry_future=None,entry_close_fault=False,
        entry_action_future=None,entry_check=None,default_acquisition=None,package_future=None,
        editor_future=None,handoff_future=None,_handoff_waiting=False,
        editor_pool=SimpleNamespace(activeThreadCount=lambda:0),_operation_jobs={},editing_bridge=None)
    host._other_work_busy=lambda **kw:EncyclopediaCatalogHost._other_work_busy(host,**kw)
    return host,EncyclopediaCatalogHost


@pytest.mark.parametrize('hold',['none','future','help','entry_future','entry_close_fault','entry_action_future',
    'entry_check','default_acquisition','package_future','editor_future','handoff_future','_handoff_waiting',
    'editor_pool','_operation_jobs','editing_bridge','faulted','uncertain','foreign_future','closing','kind'])
def test_saved_open_feedback_preserves_foreign_and_uncertain_guards(hold):
    host,owner=_pending_saved_host('preview' if hold=='kind' else 'connection_open')
    if hold in ['help','entry_check','default_acquisition']:setattr(host,hold,SimpleNamespace(close_held=True))
    elif hold=='editing_bridge':host.editing_bridge=SimpleNamespace(busy=True)
    elif hold=='editor_pool':host.editor_pool=SimpleNamespace(activeThreadCount=lambda:1)
    elif hold=='_operation_jobs':host._operation_jobs={'foreign':'retained'}
    elif hold=='faulted':host.controller.faulted=True
    elif hold=='uncertain':host.executor._uncertain=True
    elif hold=='foreign_future':host.executor._future=object()
    elif hold=='closing':host._closing=True
    elif hold not in ['none','kind']:setattr(host,hold,True)
    host.other_work_busy=owner.other_work_busy.fget(host)
    assert owner.busy.fget(host) is True
    assert owner.saved_open_feedback_available(host) is (hold=='none')


def test_idle_shared_help_is_not_foreign_work_but_its_real_operation_is(qt_test_scope):
    from PySide6.QtCore import QObject
    from qt_app.encyclopedia_help_commands import EncyclopediaHelpCommands
    host,owner=_pending_saved_host()
    helper=EncyclopediaHelpCommands.__new__(EncyclopediaHelpCommands);QObject.__init__(helper)
    helper.executor=host.executor;helper.future=None;helper.checker=None;helper.closing=False
    host.help=helper;host.other_work_busy=owner.other_work_busy.fget(host)
    assert helper.close_held and host.other_work_busy and owner.saved_open_feedback_available(host)
    helper.future=object();assert not owner.saved_open_feedback_available(host)
    helper.future=None;helper.closing=True;assert not owner.saved_open_feedback_available(host)


def test_saved_open_elapsed_advances_without_semantic_rerender_and_resets(qt_test_scope,tmp_path):
    from concurrent.futures import Future
    from suite_core.manager.tag_database_controller import _Operation
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    executor=ResourceExecutorOwner();controller=TagDatabaseController(executor=executor,settings_path=tmp_path/'settings.json')
    clock=[10.0];pane=TagDatabasePane(controller,clock=lambda:clock[0]);pane.timer.stop();qt_test_scope.show(pane)
    first=_Operation('connection_open');controller._operation=first;controller._future=Future();pane.render()
    assert pane.elapsed.isVisible() and '0 seconds' in pane.elapsed.text()
    assert pane.status.text()=='Waiting to open your saved connection.'
    key=pane._render_key;clock[0]=13.2;pane.render()
    assert pane._render_key==key and '3 seconds' in pane.elapsed.text()
    controller.cancel();pane.render();assert pane.cancel.isEnabled() and controller.cancel_requested
    assert pane.status.text()=="Cancellation requested. Waiting for this connection check to finish."
    assert not any(w.isEnabled() for w in [pane.preview,pane.apply,pane.reload,pane.catalog,pane.source_directory])
    controller._operation=controller._future=None;pane.render();assert not pane.elapsed.isVisible()
    clock[0]=20;controller._operation=_Operation('connection_open');controller._future=executor._future=Future();pane.render()
    assert '0 seconds' in pane.elapsed.text() and pane._feedback_operation is not first
    alive=[True];executor._thread=SimpleNamespace(is_alive=lambda:alive[0])
    controller._future.set_result(None);controller.poll();pane.render()
    assert pane.elapsed.isVisible() and 'Settling saved connection' in pane.elapsed.text()
    alive[0]=False;pane.render();assert not pane.elapsed.isVisible()
    assert pane.status.text()=="Saved connection opening cancelled. No connection was changed."


@pytest.mark.parametrize('delivery',['external','pane'])
@pytest.mark.parametrize('outcome',['success','failure'])
def test_saved_open_preserves_connection_draft_and_disclosure(qt_test_scope,tmp_path,monkeypatch,delivery,outcome):
    choice=supplied_choice(tmp_path)
    controller=TagDatabaseController(choices=(choice,),settings_path=tmp_path/'settings.json')
    pane=TagDatabasePane(controller);pane.timer.stop();qt_test_scope.show(pane)
    pane.source_directory.setText('draft-source');pane.catalog_directory.setText('draft-catalog')
    pane.create_catalog.setChecked(True);pane.connection_options.setChecked(True)
    old_review=SimpleNamespace(source_directory=tmp_path/'old-source',catalog_root=tmp_path/'old-catalog',
        create_new=True,token='old',source_pin=('1','a'*64))
    controller.connection_review=old_review;pane.render();pane.confirm_connection.setChecked(True)
    def deliver():
        controller.connection_review=None;controller.last_operation='connection_open'
        controller.result=SimpleNamespace(source_directory=tmp_path/'accepted-source',choice=choice)
        controller.error='' if outcome=='success' else 'Synthetic saved open held'
        controller.reason='Catalog connected' if outcome=='success' else 'Operation held'
        return True
    if delivery=='external':deliver();pane.render()
    else:monkeypatch.setattr(controller,'poll',deliver);pane.poll()
    assert pane.source_directory.text()=='draft-source' and pane.catalog_directory.text()=='draft-catalog'
    assert pane.create_catalog.isChecked() and pane.connection_options.isChecked()
    assert not pane.confirm_connection.isChecked()
    original=pane.connected_context.text()
    for expanded in [False,True,True]:
        pane.connection_options.setChecked(expanded);pane.render()
        assert pane.connected_context.text()==original
        assert pane.source_directory.text()=='draft-source' and pane.catalog_directory.text()=='draft-catalog'


def test_confirmed_connection_still_consumes_its_review(qt_test_scope,tmp_path,monkeypatch):
    choice=supplied_choice(tmp_path);controller=TagDatabaseController(choices=(choice,),settings_path=tmp_path/'settings.json')
    pane=TagDatabasePane(controller);pane.timer.stop();qt_test_scope.show(pane)
    pane.source_directory.setText('old');pane.catalog_directory.setText('old');pane.create_catalog.setChecked(True)
    controller.result=SimpleNamespace(source_directory=tmp_path/'source',choice=choice)
    controller.last_operation='connection_confirm'
    monkeypatch.setattr(controller,'poll',lambda:True)
    pane.poll()
    assert pane.source_directory.text()==str(tmp_path/'source')
    assert pane.catalog_directory.text()==str(choice.transactions.host.root)
    assert not pane.create_catalog.isChecked() and not pane.connection_options.isChecked()


@pytest.mark.parametrize('state',['empty','connected','error','readonly'])
@pytest.mark.parametrize('theme',['dark','light'])
def test_connection_draft_disclosure_preserves_authority(qt_test_scope,tmp_path,state,theme,request):
    from PySide6.QtCore import Qt
    from PySide6.QtTest import QTest
    from PySide6.QtWidgets import QApplication
    from qt_app.theme import apply_theme
    import os,json,hashlib
    app=QApplication.instance();apply_theme(app,theme)
    from suite_core.manager.resource_executor import ResourceExecutorOwner
    executor=ResourceExecutorOwner();request.addfinalizer(executor.begin_close)
    controller=TagDatabaseController(executor=executor,choices=() if state=='empty' else (supplied_choice(tmp_path),),
        settings_path=None if state=='readonly' else tmp_path/'settings.json')
    pane=TagDatabasePane(controller);pane.timer.stop();pane.resize(1300,900);qt_test_scope.show(pane)
    parent=pane.connected_context.parentWidget();layout=parent.layout()
    assert layout.indexOf(pane.catalog)<layout.indexOf(pane.connected_context)<layout.indexOf(pane.connection_draft)
    assert pane.connection_options.text()=='Edit connection draft'
    assert pane.connection_options.isChecked()==(state=='empty')
    if state=='empty':assert 'Review connection' in pane.status.text() and 'Preview' not in pane.status.text()
    if state=='readonly':assert not pane.connection_draft.isVisible()
    else:
        pane.connection_options.setChecked(True)
        pane.source_directory.setText('pending-source');pane.catalog_directory.setText('pending-catalog');pane.create_catalog.setChecked(True)
        review=SimpleNamespace(source_directory=tmp_path/'source',catalog_root=tmp_path/'catalog',
            create_new=True,token='exact-reviewed-token',source_pin=('1','a'*64))
        controller.connection_review=review
        controller.review=SimpleNamespace(database=None,holds=(),can_apply=True,token='content-token')
        if state=='error':controller.error='Synthetic connection held; correct the draft.'
        pane.render();pane.confirm_connection.setChecked(True);pane.confirm.setChecked(True)
        original=(pane.source_directory.text(),pane.catalog_directory.text(),pane.create_catalog.isChecked(),
            controller.review,controller.connection_review,pane.confirm.isChecked(),pane.confirm_connection.isChecked(),pane.catalog.currentIndex())
        pane.connection_options.setFocus();app.setActiveWindow(pane);app.processEvents()
        QTest.keyClick(pane.connection_options,Qt.Key_Space);app.processEvents()
        assert not pane.connection_box.isVisible() and pane.connection_options.arrowType()==Qt.RightArrow
        QTest.keyClick(pane.connection_options,Qt.Key_Space);app.processEvents()
        assert pane.connection_box.isVisible() and pane.connection_options.arrowType()==Qt.DownArrow
        QTest.keyClick(pane.connection_options,Qt.Key_Tab);app.processEvents();assert pane.source_directory.hasFocus()
        assert original==(pane.source_directory.text(),pane.catalog_directory.text(),pane.create_catalog.isChecked(),
            controller.review,controller.connection_review,pane.confirm.isChecked(),pane.confirm_connection.isChecked(),pane.catalog.currentIndex())
        assert controller.connection_review.token=='exact-reviewed-token'
        assert not (tmp_path/'settings.json').exists()
    capture=os.environ.get('COLLECTIGEN_DRAFT_COMPONENT_CAPTURES')
    if capture:
        folder=Path(capture);folder.mkdir(parents=True,exist_ok=True);p=folder/(state+'-'+theme+'.png')
        assert pane.grab().save(str(p));p.with_suffix('.json').write_text(json.dumps({'state':state,'theme':theme,
            'level':'component_supplied','path':str(p),'sha256':hashlib.sha256(p.read_bytes()).hexdigest(),
            'limit':'Supplied controller/error/review fixtures; not native acceptance'},indent=2),encoding='utf-8')
