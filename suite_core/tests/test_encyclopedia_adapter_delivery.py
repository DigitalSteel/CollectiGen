"""Exact accepted native adapter delivery; export/memory remain read-only."""
import time
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog
from tests.test_encyclopedia_editing import editing_catalog
from suite_core.tests.test_encyclopedia_catalog_ui import click


@pytest.fixture
def native_reader(tmp_path,qt_test_scope,monkeypatch):
    from suite_core.encyclopedia.__main__ import create_window
    adapter=editing_catalog(tmp_path);window=create_window(settings_path=tmp_path/'settings.json')
    host=window.catalog_host;window.resize(1440,900);qt_test_scope.show(window)
    monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *args:str(adapter.root))
    def settle():
        deadline=time.monotonic()+15
        while host.busy and time.monotonic()<deadline:
            host.poll();qt_test_scope.settle(1);time.sleep(.005)
        qt_test_scope.settle(1);assert not host.busy,window.status.text()+window.status.toolTip()
    yield window,host,adapter.root,settle
    window.close();qt_test_scope.settle()


def test_native_success_signal_retains_exact_worker_adapter_and_refresh_identity(native_reader):
    window,host,root,settle=native_reader;accepted=[]
    def delivered(binding):
        assert window.publication is binding.publication
        assert host.accepted_native_adapter is binding.native_adapter
        assert binding.path==root and binding.native_adapter.directory_pin==binding.directory_pin
        accepted.append(binding)
    host.catalog_accepted.connect(delivered)
    click(window,'Open Tag Database');settle()
    original=host.service.view.adapter
    assert len(accepted)==1 and original is accepted[0].native_adapter
    assert host.editor_session is None
    click(window,'Refresh');settle()
    assert len(accepted)==2 and host.service.view.adapter is original and host.accepted_native_adapter is original


@pytest.mark.parametrize('ending',['cancel','refuse','export'])
def test_unaccepted_or_export_delivery_exposes_no_native_adapter(native_reader,tmp_path,monkeypatch,qt_test_scope,ending):
    from threading import Event
    from suite_core.encyclopedia import publication as codec
    window,host,root,settle=native_reader;accepted=[]
    host.catalog_accepted.connect(accepted.append)
    click(window,'Open Tag Database');settle()
    before=window.publication;original=host.accepted_native_adapter
    if ending=='cancel':
        foreign=editing_catalog(tmp_path/'foreign').root;entered,release=Event(),Event();stock=host.service.open_catalog
        def delayed(*args):entered.set();assert release.wait(5);return stock(*args)
        monkeypatch.setattr(host.service,'open_catalog',delayed)
        monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *args:str(foreign))
        click(window,'Open Tag Database');assert entered.wait(1)
        host.poll();qt_test_scope.settle(1)
        QTest.mouseClick(window.cancel_work_button,Qt.LeftButton);release.set();settle()
        assert len(accepted)==1 and window.publication is before
    elif ending=='refuse':
        monkeypatch.setattr(window,'open_publication',lambda *args,**kwargs:False)
        click(window,'Refresh');settle()
        assert len(accepted)==1 and host.accepted_source.native_adapter is original
    else:
        exported=tmp_path/'synthetic-export.json';exported.write_bytes(codec.encode(before))
        monkeypatch.setattr(QFileDialog,'getOpenFileName',lambda *args:(str(exported),''))
        click(window,'Open exported tag catalog');settle()
        assert len(accepted)==2 and accepted[-1].kind=='export' and accepted[-1].native_adapter is None
    assert host.accepted_native_adapter is None and host.editor_session is None


def test_foreign_adapter_is_refused_before_old_adapter_retention(native_reader,tmp_path):
    window,host,root,settle=native_reader;accepted=[]
    host.catalog_accepted.connect(accepted.append)
    click(window,'Open Tag Database');settle()
    old_source,old_publication=host.accepted_source,window.publication
    foreign=editing_catalog(tmp_path/'foreign-adapter');foreign.capture(1)
    assert not window.open_publication(old_publication,native_root=root,
        directory_pin=old_source.directory_pin,native_adapter=foreign)
    assert window.publication is old_publication and host.accepted_source is old_source
    assert len(accepted)==1 and host.accepted_native_adapter is None and window.stale
