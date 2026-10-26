"""Generated enrolled catalog + retained host bridge qualification; no Qt/live data."""
from dataclasses import replace
import importlib.util
import json
from pathlib import Path
from threading import Event, get_ident
from time import monotonic, sleep
from concurrent.futures import Future
import tempfile

import pytest
import core.knowledge_pack as native
from core import tag_catalog_connection as connection
from suite_core.manager.tag_database_controller import TagDatabaseController, _observe
from suite_core.manager.resource_executor import ResourceExecutorOwner
from suite_core.encyclopedia.publication import read
from tests.test_tag_catalog_connection import selected
from tests.test_tag_database_pack_producer import reviewed_local_input


@pytest.fixture(autouse=True)
def contained_temp(tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, "tempdir", str(tmp_path))


def data(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in (root/'data').rglob('*.json')}


def evidence(root):
    return {p.relative_to(root).as_posix():p.read_bytes() for p in root.rglob('*')
            if p.is_file() and p.name!='.lock' and (p.suffix=='.json' or 'knowledge-pack-import' in p.parts)}


@pytest.fixture
def connected(selected, tmp_path, monkeypatch):
    monkeypatch.setattr(tempfile, 'tempdir', str(tmp_path))
    settings, _source, root = selected
    review=connection.prepare_connection(*selected,True)
    choice=connection.confirm_connection(review,review.token).choice
    spec=importlib.util.spec_from_file_location('host_pack_helpers',Path(__file__).with_name('test_knowledge_pack_records.py'))
    helper=importlib.util.module_from_spec(spec);spec.loader.exec_module(helper)
    source=helper._root(tmp_path/'personal-source')
    helper._write(source/'data/user/categories.json',{'schema_version':6,'categories':[{'id':'new-category','name':'New personal category'}]})
    helper._write(source/'data/user/tags.json',{'schema_version':10,'tags':[{'id':'host-added','display':'Host added','category':'new-category'}]})
    helper._write(root/'data/user/categories.json',{'schema_version':6,'categories':[{'id':'personal','name':'Retained personal'}],'future':{'keep':1}})
    helper._write(root/'data/user/tags.json',{'schema_version':10,'tags':[{'id':'keep','display':'Keep','category':'personal'}],'future':{'keep':[1,2]}})
    before=data(root)
    pack=native.export_knowledge_pack(source,source/'user.zip')
    plan=native.plan_knowledge_pack_import(root,pack)
    result=native.import_knowledge_pack(root,pack,plan=plan)
    receipt=Path(result['backup']['path'])/'recovery.json'
    choice.transactions.reload()
    choice=_observe(choice,include_capabilities=False)
    executor=ResourceExecutorOwner()
    c=TagDatabaseController(choices=(choice,),executor=executor,apply_authorized=True,settings_path=settings)
    yield c,root,receipt,before,settings
    c.begin_close()
    end=monotonic()+120
    while executor.close_held and monotonic()<end:sleep(.002)
    assert not executor.close_held
    if c.pending:c.poll()
    executor.begin_close()


def drain(c):
    end=monotonic()+120
    while c.pending and (not c._future.done() or c.executor.close_held) and monotonic()<end:sleep(.002)
    assert c.pending and c._future.done() and not c.executor.close_held
    assert c.poll()


def review(c,receipt):
    assert c.request_completed('review',receipt=receipt)
    drain(c)
    assert not c.error,c.error
    assert c.completed_review is not None,c.reason
    return c.completed_review


def undo(c,receipt):
    r=review(c,receipt)
    assert c.request_completed('undo',review=r,confirmed=True)
    drain(c)


def test_connected_review_confirm_saved_publication_reopen_and_noop(connected,monkeypatch):
    c,root,receipt,before,settings=connected
    raw=receipt.read_bytes();original=evidence(root);owner=get_ident();calls=[]
    real=native.review_completed_knowledge_pack_import
    def inspect(*a,**k):
        calls.append(get_ident());return real(*a,**k)
    monkeypatch.setattr(native,'review_completed_knowledge_pack_import',inspect)
    r=review(c,receipt)
    assert evidence(root)==original
    assert not c.request_completed('undo',review=r,confirmed=False)
    assert evidence(root)==original
    generation=read(root/'tag_catalog_snapshot.json').generation
    assert c.request_completed('undo',review=r,confirmed=True);drain(c)
    assert not c.error,c.error
    result=c.result
    assert result.native_saved and not result.publication_error and result.publication is not None
    assert data(root)==before and receipt.read_bytes()==raw
    assert result.publication.generation>generation
    assert read(root/'tag_catalog_snapshot.json')==result.publication
    reopened=connection.open_saved_connection(settings).choice
    assert reopened.native_adapter.identity_binding==c.choice.native_adapter.identity_binding
    saved=evidence(root)
    assert c.request_completed('recover',receipt=result.receipt);drain(c)
    assert c.result.native_saved and evidence(root)==saved
    assert calls and all(t!=owner for t in calls)
    assert len(list((root/'backups/knowledge-pack-import').glob('completed-pack-undo-*/recovery.json')))==1


@pytest.mark.parametrize('drift',['participant','receipt','identity','session','selection'])
def test_stale_confirmation_never_allocates_inverse(connected,drift):
    c,root,receipt,_before,_settings=connected
    r=review(c,receipt)
    if drift=='participant':
        path=root/'data/user/tags.json';value=json.loads(path.read_bytes());value['later']='keep';path.write_text(json.dumps(value),encoding='utf-8')
    elif drift=='receipt':receipt.write_bytes(receipt.read_bytes()+b' ')
    elif drift=='identity':
        path=root/'data/user/catalog_identity.json';value=json.loads(path.read_bytes());value['future_extension']='changed';path.write_text(json.dumps(value),encoding='utf-8')
    elif drift=='session':c.choice.transactions.session.select(read(root/'tag_catalog_snapshot.json'))
    else:c.select(0)
    frozen=evidence(root)
    accepted=c.request_completed('undo',review=r,confirmed=True)
    if accepted:drain(c)
    assert c.error or not accepted
    assert evidence(root)==frozen
    assert not list((root/'backups/knowledge-pack-import').glob('completed-pack-undo-*'))


def test_publication_failure_retains_saved_result_without_replay(connected,monkeypatch):
    c,root,receipt,before,_settings=connected
    previous=(root/'tag_catalog_snapshot.json').read_bytes()
    with monkeypatch.context() as patch:
        patch.setattr(c.choice.transactions,'reload',lambda: (_ for _ in ()).throw(OSError('publication fault')))
        undo(c,receipt)
    assert c.result.native_saved and c.result.publication is None
    assert 'publication fault' in c.error and 'do not undo again' in c.reason
    assert data(root)==before and (root/'tag_catalog_snapshot.json').read_bytes()==previous
    exact=c.result.receipt
    assert c.request('reload');drain(c)
    assert not c.error
    assert list((root/'backups/knowledge-pack-import').glob('completed-pack-undo-*/recovery.json'))==[exact]


def interrupted(c,root,receipt,monkeypatch):
    r=review(c,receipt);value=json.loads(receipt.read_bytes());real=native._atomic_bytes;fired=[]
    targets={Path(row['target']) for row in value['files'] if row['expected_before']!=row['expected_after']}
    assert len(targets)>=2
    def fail(path,payload):
        real(path,payload)
        if Path(path) in targets and not fired:
            fired.append(True);raise OSError('host interrupted after first participant')
    with monkeypatch.context() as patch:
        patch.setattr(native,'_atomic_bytes',fail)
        assert c.request_completed('undo',review=r,confirmed=True);drain(c)
    assert fired and c.result is not None and not c.result.native_saved
    assert c.choice.observation_status=='completed_inverse_pending',c.error
    assert c.choice.recovery_receipts==() and c.result.receipt in c.choice.completed_inverse_receipts
    return c.result.receipt


def test_pending_exact_reopen_and_explicit_host_recovery(connected,monkeypatch):
    c,root,receipt,before,settings=connected
    inverse=interrupted(c,root,receipt,monkeypatch)
    frozen=evidence(root);publication=(root/'tag_catalog_snapshot.json').read_bytes()
    reopened=connection.open_saved_connection(settings).choice
    assert reopened.completed_inverse_receipts==(inverse,)
    assert reopened.recovery_receipts==() and evidence(root)==frozen
    assert (root/'tag_catalog_snapshot.json').read_bytes()==publication
    c.replace_choice(0,reopened)
    assert not c.can_apply
    assert c.request_completed('recover',receipt=inverse);drain(c)
    assert c.result.native_saved and not c.error,c.error
    assert data(root)==before and json.loads(inverse.read_bytes())['status']=='restored'
    assert connection.open_saved_connection(settings).choice.observation_status=='current'


@pytest.mark.parametrize('drift',['participant','receipt','backup'])
def test_pending_later_changes_refuse_without_writes(connected,monkeypatch,drift):
    c,root,receipt,_before,settings=connected
    inverse=interrupted(c,root,receipt,monkeypatch)
    if drift=='participant':
        p=root/'data/user/tags.json';value=json.loads(p.read_bytes());value['later']='retain';p.write_text(json.dumps(value),encoding='utf-8')
    elif drift=='receipt':inverse.write_bytes(inverse.read_bytes()+b' ')
    else:
        row=next(row for row in json.loads(inverse.read_bytes())['files'] if row['existed'])
        p=inverse.parent/row['backup'];p.write_bytes(b'changed backup')
    frozen=evidence(root)
    assert c.request_completed('recover',receipt=inverse);drain(c)
    assert c.error and evidence(root)==frozen
    if drift!='receipt':
        with pytest.raises(RuntimeError):connection.open_saved_connection(settings)


def test_cancel_and_close_after_admission_retains_saved_outcome(connected,monkeypatch):
    c,root,receipt,before,_settings=connected
    r=review(c,receipt);entered=Event();release=Event();real=native.undo_completed_knowledge_pack_import
    def delayed(*a,**k):
        entered.set();assert release.wait(5);return real(*a,**k)
    monkeypatch.setattr(native,'undo_completed_knowledge_pack_import',delayed)
    try:
        assert c.request_completed('undo',review=r,confirmed=True) and entered.wait(2)
        assert c.close_held and not c.request_completed('undo',review=r,confirmed=True)
        c.begin_close();assert c.close_held
    finally:release.set()
    drain(c)
    assert c.result.native_saved and data(root)==before and not c.close_held
    assert 'Cancellation arrived after admission' in c.reason


def test_cancel_before_admission_and_failed_submission(connected):
    c,root,receipt,_before,_settings=connected
    r=review(c,receipt);saved=evidence(root);real=c.executor
    class Queued:
        close_held=False
        def submit(self,fn,*args):
            self.fn,self.args=fn,args;self.future=Future();return self.future
    queued=Queued();c.executor=queued
    assert c.request_completed('undo',review=r,confirmed=True)
    c.cancel()
    # Execute on the actual retained background worker after cancellation.
    future=real.submit(queued.fn,*queued.args);queued.future.set_result(future.result(timeout=5))
    c.executor=real;drain(c)
    assert evidence(root)==saved and c.result is None
    class Failed:
        def submit(self,*args):raise OSError('submission unknown')
    r=review(c,receipt);c.executor=Failed()
    assert not c.request_completed('undo',review=r,confirmed=True)
    assert c.close_held and c.faulted and evidence(root)==saved
    # No operation was launched by this exact test double; release test-only hold.
    c.executor=real;c._unconfirmed=False


def test_missing_adapter_or_write_authority_refuses(connected):
    c,root,receipt,_before,_settings=connected
    r=review(c,receipt);c.apply_authorized=False;frozen=evidence(root)
    assert not c.request_completed('undo',review=r,confirmed=True)
    c.choices=(replace(c.choice,native_adapter=None),)
    assert not c.request_completed('review',receipt=receipt)
    assert evidence(root)==frozen


@pytest.mark.parametrize('delivery',['exception','missing','malformed'])
def test_unknown_mutation_delivery_retains_close_and_new_work_hold(connected,delivery):
    c,root,receipt,_before,_settings=connected
    r=review(c,receipt);real=c.executor;frozen=evidence(root)
    class Lost:
        close_held=False
        def submit(self,*args):self.future=Future();return self.future
    lost=Lost();c.executor=lost
    assert c.request_completed('undo',review=r,confirmed=True)
    c._operation.admitted=True
    if delivery=='exception':lost.future.set_exception(RuntimeError('lost delivery'))
    else:lost.future.set_result(None if delivery=='missing' else object())
    assert c.poll() and c.close_held and c.faulted
    assert not c.request_completed('review',receipt=receipt)
    assert evidence(root)==frozen
    # Exact double launched nothing. Production unknown ownership stays held.
    c.executor=real;c._unconfirmed=False


def test_queued_recovery_revalidates_session_after_waiting(connected,monkeypatch):
    c,root,receipt,_before,_settings=connected
    inverse=interrupted(c,root,receipt,monkeypatch);frozen=evidence(root);real=c.executor
    class Queued:
        close_held=False
        def submit(self,fn,*args):self.fn,self.args=fn,args;self.future=Future();return self.future
    queued=Queued();c.executor=queued
    assert c.request_completed('recover',receipt=inverse)
    c.choice.transactions.session.select(read(root/'tag_catalog_snapshot.json'))
    future=real.submit(queued.fn,*queued.args)
    queued.future.set_result(future.result(timeout=120))
    c.executor=real;drain(c)
    assert 'session changed' in c.error and evidence(root)==frozen


@pytest.mark.parametrize('scope',['foreign_receipt','foundation_receipt'])
def test_completed_host_review_preserves_scope_and_foundation_refusal(connected,scope,tmp_path):
    c,root,receipt,_before,_settings=connected
    if scope=='foreign_receipt':
        foreign=tmp_path/'foreign'/'backups'/'knowledge-pack-import'/receipt.parent.name/'recovery.json'
        foreign.parent.mkdir(parents=True);foreign.write_bytes(receipt.read_bytes());receipt=foreign
    else:
        value=json.loads(receipt.read_bytes());value['foundation_transition']={}
        receipt.write_text(json.dumps(value),encoding='utf-8')
    frozen=evidence(root)
    assert c.request_completed('review',receipt=receipt);drain(c)
    assert c.completed_review is None and c.error
    assert evidence(root)==frozen and not list((root/'backups/knowledge-pack-import').glob('completed-pack-undo-*'))



def test_other_retained_host_work_refuses_before_submission(connected):
    c,root,receipt,_before,_settings=connected
    entered=Event();release=Event();frozen=evidence(root)
    def other():entered.set();assert release.wait(5)
    future=c.executor.submit(other)
    try:
        assert entered.wait(2)
        assert not c.request_completed('review',receipt=receipt)
        assert not c.faulted and not c.pending and evidence(root)==frozen
    finally:release.set()
    future.result(timeout=5)



def test_post_save_observation_failure_keeps_verified_publication(connected,monkeypatch):
    from suite_core.manager import tag_database_controller as module
    c,root,receipt,before,_settings=connected
    r=review(c,receipt)
    with monkeypatch.context() as patch:
        patch.setattr(module,'_observe',lambda *a,**k: (_ for _ in ()).throw(OSError('observation fault')))
        assert c.request_completed('undo',review=r,confirmed=True);drain(c)
    assert c.result.native_saved and c.result.publication is not None
    assert not c.result.publication_error and 'observation fault' in c.error
    assert 'host observation held' in c.reason and 'snapshot refresh held' not in c.reason
    assert data(root)==before and read(root/'tag_catalog_snapshot.json')==c.result.publication
