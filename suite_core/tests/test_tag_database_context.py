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
