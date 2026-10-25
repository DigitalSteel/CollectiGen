"""Connected original editor commands in the synthetic standalone reader."""
import time
import pytest

from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog,QDialogButtonBox,QPushButton

from tests.test_encyclopedia_editing import editing_catalog
from suite_core.tests.test_encyclopedia_catalog_ui import click
from suite_core.tests.test_encyclopedia_editing_ui import modal_driver


@pytest.fixture
def shell(tmp_path,qt_test_scope,monkeypatch):
    from suite_core.encyclopedia.__main__ import create_window
    adapter=editing_catalog(tmp_path)
    monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *args:str(adapter.root))
    window=create_window(settings_path=tmp_path/'settings.json')
    window.resize(1440,900);qt_test_scope.show(window)
    host=window.catalog_host
    def settle():
        deadline=time.monotonic()+15
        while host.busy and time.monotonic()<deadline:
            host.poll();qt_test_scope.settle(1);time.sleep(.005)
        qt_test_scope.settle(1)
        assert not host.busy,window.status.text()+window.status.toolTip()
    click(window,'Open Tag Database');settle()
    yield window,host,adapter,settle
    if host.category_dialog is not None:host.category_dialog.close()
    if host.dialog is not None:host.dialog.close()
    window.close();qt_test_scope.settle()


@pytest.mark.parametrize('replacement',['identity','directory'])
def test_editor_preparation_refuses_replacement_under_old_reader(shell,tmp_path,qt_test_scope,replacement):
    from suite_core.catalog_identity_candidate import CatalogIdentity,encode_identity
    from uuid import uuid4
    window,host,adapter,settle=shell
    accepted=window.publication
    if replacement=='identity':
        (adapter.root/'data/user/catalog_identity.json').write_bytes(encode_identity(CatalogIdentity(str(uuid4()),str(uuid4()))))
    else:
        adapter.root.rename(adapter.root.with_name('old-accepted-catalog'))
        replacement_root=editing_catalog(tmp_path/'replacement').root
        replacement_root.rename(adapter.root)
    click(window,'Edit Tags and Categories');settle()
    assert host.editing_bridge is None and host.editor_session is None and host.knowledge is None
    assert window.publication is accepted and 'Editing is held' in window.status.text()


def test_editor_adoption_rechecks_binding_and_closes_returned_session(shell,monkeypatch,qt_test_scope):
    from core import encyclopedia_catalog
    from suite_core.catalog_identity_candidate import CatalogIdentity,encode_identity
    from uuid import uuid4
    window,host,adapter,settle=shell
    original=encyclopedia_catalog.prepare_selected_editor;prepared=[]
    def prepare(*args,**kwargs):
        result=original(*args,**kwargs);prepared.append(result[0]);return result
    monkeypatch.setattr(encyclopedia_catalog,'prepare_selected_editor',prepare)
    host.timer.stop()
    click(window,'Edit Tags and Categories')
    deadline=time.monotonic()+15
    while (not host.editor_future.done() or host.executor.close_held) and time.monotonic()<deadline:
        qt_test_scope.settle(1);time.sleep(.005)
    assert prepared and host.knowledge is None
    (adapter.root/'data/user/catalog_identity.json').write_bytes(encode_identity(CatalogIdentity(str(uuid4()),str(uuid4()))))
    settle()
    assert host.editing_bridge is None and host.knowledge is None and prepared[0].closed


def test_editor_retirement_disposes_exact_tag_host(shell,qt_test_scope):
    from shiboken6 import isValid
    from PySide6.QtCore import QCoreApplication,QEvent
    window,host,adapter,settle=shell
    click(window,'Edit Tags and Categories');settle()
    tag_host=host.editing_bridge.tag_editor;session=host.editor_session
    click(window,'Use optional default tags');settle()
    QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);qt_test_scope.settle()
    assert session.closed and host.editing_bridge is None and host.knowledge is None
    assert not isValid(tag_host)


@pytest.mark.parametrize('failure',['workspace','tag-host','bridge'])
def test_editor_partial_composition_closes_session_and_removes_widgets(shell,monkeypatch,qt_test_scope,failure):
    from core import encyclopedia_catalog
    from suite_core.encyclopedia import editing_ui
    from qt_app import tag_details_host
    from PySide6.QtCore import QCoreApplication,QEvent
    from shiboken6 import isValid
    window,host,adapter,settle=shell
    original=encyclopedia_catalog.prepare_selected_editor;prepared=[];partials=[]
    def prepare(*args,**kwargs):
        result=original(*args,**kwargs);prepared.append(result[0]);return result
    monkeypatch.setattr(encyclopedia_catalog,'prepare_selected_editor',prepare)
    target,attribute={'workspace':(editing_ui,'create_category_workspace'),
        'tag-host':(tag_details_host,'TagDetailsEditorHost'),'bridge':(editing_ui,'EncyclopediaEditingBridge')}[failure]
    stock=getattr(target,attribute)
    def broken(*args,**kwargs):
        partials.append(stock(*args,**kwargs))
        raise RuntimeError('Synthetic failure after original composition')
    monkeypatch.setattr(target,attribute,broken)
    click(window,'Edit Tags and Categories');settle()
    QCoreApplication.sendPostedEvents(None,QEvent.DeferredDelete);qt_test_scope.settle()
    assert prepared[0].closed and all(not isValid(obj) for obj in partials)
    assert host.editing_bridge is None and host.editor_session is None and host.category_dialog is None
    assert host.knowledge is None and host.settings is None and not host._operation_jobs
    assert 'Editing is held' in window.status.text()


def test_reader_original_new_tag_save_refresh_and_close(tmp_path,qt_test_scope,monkeypatch):
    from suite_core.encyclopedia.__main__ import create_window
    from qt_app.knowledge_page import _TagDetailsDialog
    from core.tag_database import TagDatabase
    adapter=editing_catalog(tmp_path)
    monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *args:str(adapter.root))
    window=create_window(settings_path=tmp_path/'settings.json')
    window.resize(1440,900);qt_test_scope.show(window)
    host=window.catalog_host
    def settle():
        deadline=time.monotonic()+20
        while host.busy and time.monotonic()<deadline:
            qt_test_scope.settle(1);time.sleep(.005)
        qt_test_scope.settle(1)
        assert not host.busy,window.status.text()+window.status.toolTip()
    click(window,'Open Tag Database');settle()
    click(window,'Edit Tags and Categories');settle()
    assert host.editing_bridge is not None,window.status.text()+window.status.toolTip()
    session=host.editor_session;bridge=host.editing_bridge
    assert host.knowledge is session.tags and bridge.workspace.session is session.category_session
    phase=['new']
    def drive(dialog):
        assert not host.close_ready()
        assert not host.open_database(adapter.root)
        assert not host.show_defaults()
        page=bridge.tag_editor.page
        if phase[0]=='new':
            page.tag_name.setText('synthetic shell wolf')
            page.tag_category.setCurrentIndex(page.tag_category.findData('animals'))
            phase[0]='wait'
            button=next(b for b in dialog.findChildren(QPushButton) if b.text()=='Create SDXL Tag')
            QTest.mouseClick(button,Qt.LeftButton)
        elif session.tags.find('synthetic shell wolf') is not None and not bridge.tag_editor.busy:
            QTest.mouseClick(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Cancel),Qt.LeftButton)
    timer,errors=modal_driver(qt_test_scope,_TagDetailsDialog,drive)
    click(window,'New SDXL Tag');timer.stop()
    assert not errors,errors
    settle()
    # The shell refreshes the reader after the original verified save, retaining
    # this selected native editor session rather than replacing its authority.
    deadline=time.monotonic()+10
    while (host._editor_refresh_needed or host.busy) and time.monotonic()<deadline:
        qt_test_scope.settle(1);time.sleep(.005)
    record=TagDatabase(adapter.root/'data/user/tags.json').find('synthetic shell wolf')
    assert record is not None and window.lookup.resolve('synthetic shell wolf').tag.id==record.id
    assert host.editor_session is session and not session.reload_required
    from qt_app.category_workspace import CategoryCreationDialog
    phase[0]='category'
    def create_category(dialog):
        assert not host.close_ready()
        if phase[0]=='category':
            dialog.name.setText('Synthetic shell category')
            phase[0]='wait-category'
            QTest.mouseClick(dialog.create_button,Qt.LeftButton)
    timer,errors=modal_driver(qt_test_scope,CategoryCreationDialog,create_category)
    click(window,'New Category');timer.stop()
    assert not errors,errors
    settle()
    deadline=time.monotonic()+10
    while (host._editor_refresh_needed or host.busy) and time.monotonic()<deadline:
        qt_test_scope.settle(1);time.sleep(.005)
    assert any(c.name=='Synthetic shell category' for c in session.categories.categories.values())
    assert any(window.category.itemText(i)=='Synthetic shell category' for i in range(window.category.count()))
    host.category_dialog.close();qt_test_scope.settle()
    window.close();qt_test_scope.settle()
    assert not window.isVisible() and session.closed
