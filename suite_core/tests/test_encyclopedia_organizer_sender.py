"""Visible sender, stock receiver/native admission; Dataset endpoints are doubles."""
import json,time
import pytest
from PySide6.QtCore import Qt
from PySide6.QtTest import QTest
from PySide6.QtWidgets import QFileDialog,QInputDialog
from tests.test_encyclopedia_editing import editing_catalog
from suite_core.tests.test_encyclopedia_catalog_ui import click
from suite_core.tests.test_encyclopedia_downloaded_defaults import content_font


@pytest.fixture
def sender(tmp_path,qt_test_scope,monkeypatch):
    from core.encyclopedia_editing import EncyclopediaEditingSession
    from core.organizer_encyclopedia_admission import OrganizerCatalogAdmissionReader
    from suite_core.tests.test_organizer_encyclopedia_handoffs import Host
    from suite_core.media_organizer.facade import MediaOrganizer
    from suite_core.media_organizer.contracts import ExistingContext
    from suite_core.media_organizer.encyclopedia_handoff_entry import OrganizerEncyclopediaHandoffReceiver
    from core.organizer_handoff_scope import HostedRecipientScope, HostedLibraryParticipant
    from suite_core.encyclopedia.__main__ import create_window
    adapter=editing_catalog(tmp_path)
    path=adapter.root/'data/user/tags.json'
    payload=json.loads(path.read_bytes());payload['tags'][0]['aliases']=['old synthetic fox']
    path.write_text(json.dumps(payload),encoding='utf-8')
    session=EncyclopediaEditingSession.open(adapter)
    recipient=Host();recipient.state.knowledge=session.tags
    recipient.organizer=MediaOrganizer(ExistingContext(recipient.settings,recipient.state),recipient)
    recipient.ready()
    monkeypatch.setattr(QFileDialog,'getExistingDirectory',lambda *args:str(adapter.root))
    window=create_window(settings_path=tmp_path/'settings.json');window.resize(1440,900)
    qt_test_scope.show(window);host=window.catalog_host
    def settle():
        deadline=time.monotonic()+15
        while host.busy and time.monotonic()<deadline:
            host.poll();qt_test_scope.settle(1);time.sleep(.005)
        qt_test_scope.settle(1);assert not host.busy,window.status.text()+window.status.toolTip()
    click(window,'Open Tag Database');settle()
    index=window.model.index(0,0)
    QTest.mouseClick(window.table.viewport(),Qt.LeftButton,pos=window.table.visualRect(index).center())
    assert window._selected_id=='fox'
    adapter=host.accepted_native_adapter
    reader=OrganizerCatalogAdmissionReader(recipient.state,adapter=adapter)
    recipient.handoff_scope=HostedRecipientScope('synthetic-profile',('synthetic-library',),
        (HostedLibraryParticipant('synthetic-library','local_only','reviewed_only',True,'synthetic-root',object()),),
        0,recipient.state,recipient.settings,recipient.state.dataset,object())
    receiver=OrganizerEncyclopediaHandoffReceiver(recipient,read_admitted_catalog=reader,
        read_recipient_scope=lambda:recipient.handoff_scope)
    host.configure_organizer_handoffs(receiver=receiver,adapter=adapter)
    yield window,host,recipient,adapter,settle
    window.close();qt_test_scope.settle();session.close()


@pytest.mark.parametrize('route',['search','review','decline','stale'])
def test_visible_sender_uses_exact_current_native_receiver_without_writes(sender,monkeypatch,qt_test_scope,route):
    window,host,recipient,adapter,settle=sender
    before={p.relative_to(adapter.root):p.read_bytes() for p in adapter.root.rglob('*.json')}
    if route=='review':
        monkeypatch.setattr(QInputDialog,'getItem',lambda *args:('old synthetic fox',True))
        click(window,'Review Image Tag Names')
    else:
        if route=='decline':recipient.decline=True
        if route=='stale':
            path=adapter.root/'data/user/tags.json';payload=json.loads(path.read_bytes())
            payload['tags'][0]['notes']='Independent synthetic saved change'
            path.write_text(json.dumps(payload),encoding='utf-8')
            before={p.relative_to(adapter.root):p.read_bytes() for p in adapter.root.rglob('*.json')}
        click(window,'Find Matching Images in Organizer')
    settle()
    assert before=={p.relative_to(adapter.root):p.read_bytes() for p in adapter.root.rglob('*.json')}
    if route=='search':assert recipient.calls==[('display_name_search','fox')]
    elif route=='review':
        function,payload=recipient.calls[0]
        assert function=='dataset.image_facts.update_matching'
        assert payload['stable_ids']==('fox',) and payload['source_names']==('old synthetic fox',)
        assert 'opened correction review' in window.status.text()
    else:
        assert not recipient.calls and 'held' in window.status.text()


def test_sender_visible_cancel_discards_before_receiver_delivery(sender,monkeypatch,qt_test_scope):
    from threading import Event
    from core import encyclopedia_catalog
    window,host,recipient,adapter,settle=sender
    entered,release=Event(),Event();stock=encyclopedia_catalog.prepare_organizer_handoff
    def delayed(*args,**kwargs):
        entered.set();assert release.wait(5);return stock(*args,**kwargs)
    monkeypatch.setattr(encyclopedia_catalog,'prepare_organizer_handoff',delayed)
    click(window,'Find Matching Images in Organizer');assert entered.wait(1)
    host.poll();qt_test_scope.settle(1)
    assert window.cancel_work_button.isVisible()
    QTest.mouseClick(window.cancel_work_button,Qt.LeftButton);release.set();settle()
    assert not recipient.calls and 'discarded' in window.status.text()


def test_sender_cannot_rebind_receiver_or_use_export_authority(sender):
    from types import SimpleNamespace
    window,host,recipient,adapter,settle=sender
    with pytest.raises(ValueError,match='original receiver'):
        host.configure_organizer_handoffs(receiver=SimpleNamespace(receive=lambda _:True),adapter=adapter)
    host.accepted_source=None
    assert not host.organizer_ready() and not host.send_organizer_handoff()


def test_standalone_without_enrollment_does_not_add_organizer_commands(tmp_path,qt_test_scope):
    from suite_core.encyclopedia.__main__ import create_window
    window=create_window(settings_path=tmp_path/'settings.json');qt_test_scope.show(window)
    assert not any(command.key.startswith('encyclopedia:organizer-') for command in window.commands)
    assert not window.catalog_host.organizer_ready() and not window.catalog_host.send_organizer_handoff()
    window.close();qt_test_scope.settle();assert not window.isVisible()


@pytest.mark.parametrize('theme',['light','dark'])
def test_visible_sender_real_alias_choice_1080p(sender,qt_test_scope,content_font,theme):
    import os,hashlib
    from pathlib import Path
    from PySide6.QtWidgets import QDialogButtonBox,QComboBox
    from qt_app.theme import apply_theme
    from suite_core.tests.test_encyclopedia_editing_ui import modal_driver
    destination=os.environ.get('COLLECTIGEN_SENDER_VISUAL_ROOT')
    if not destination:pytest.skip('Opt-in focused synthetic sender capture')
    output=Path(destination).resolve()/theme
    assert Path(__file__).resolve().parents[2]/'.tmp' in output.parents
    output.mkdir(parents=True,exist_ok=False)
    window,host,recipient,adapter,settle=sender
    apply_theme(qt_test_scope.app,theme,False);window.resize(1920,1080);qt_test_scope.settle()
    before={p.relative_to(adapter.root):p.read_bytes() for p in adapter.root.rglob('*.json')}
    assert window.grab().save(str(output/'selected-reader.png'))
    captures=[]
    def choose(dialog):
        dialog.resize(760,180);qt_test_scope.settle(1)
        combo=dialog.findChild(QComboBox)
        assert combo.currentText()=='old synthetic fox'
        assert dialog.grab().save(str(output/'alias-review-choice.png'))
        captures.append({'size':[dialog.width(),dialog.height()],'dpr':dialog.devicePixelRatioF()})
        QTest.mouseClick(dialog.findChild(QDialogButtonBox).button(QDialogButtonBox.Ok),Qt.LeftButton)
    timer,errors=modal_driver(qt_test_scope,QInputDialog,choose)
    click(window,'Review Image Tag Names');timer.stop();assert not errors,errors
    settle()
    assert recipient.calls[0][0]=='dataset.image_facts.update_matching'
    assert window.grab().save(str(output/'request-accepted.png'))
    assert before=={p.relative_to(adapter.root):p.read_bytes() for p in adapter.root.rglob('*.json')}
    (output/'report.json').write_text(json.dumps({'theme':theme,'window_size':[window.width(),window.height()],
        'dpr':window.devicePixelRatioF(),'chooser':captures,'platform':os.environ.get('QT_QPA_PLATFORM'),
        'runner_sha256':hashlib.sha256(Path(__file__).read_bytes()).hexdigest(),
        'source_sha256':{name:hashlib.sha256((Path(__file__).resolve().parents[2]/name).read_bytes()).hexdigest()
            for name in ('core/encyclopedia_catalog.py','suite_core/encyclopedia/catalog_ui.py','suite_core/encyclopedia/browser.py',
                'core/organizer_encyclopedia_admission.py','core/organizer_encyclopedia_handoffs.py',
                'suite_core/media_organizer/encyclopedia_handoff_entry.py')},
        'version':'0.2.0-alpha.1','snapshot_interface':2,
        'qualification':'Actual sender command and alias chooser, actual native observer/receiver; Dataset endpoints are doubles. No actual Organizer review UI/image Apply/native package qualification.'},indent=2)+'\n',encoding='utf-8')
