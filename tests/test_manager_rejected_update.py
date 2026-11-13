"""Original-owner pre-start rejection controls with explicit boundary doubles.

No native actors or SDK execute. Durable journal integration is separate.
"""
from copy import deepcopy
from threading import RLock
from types import SimpleNamespace
import pytest
from core.manager_launch_owner import DeploymentOwner, LaunchHold


@pytest.fixture
def original():
    request = SimpleNamespace(transaction_id='retained', binding_sha256='a'*64)
    owner = DeploymentOwner.__new__(DeploymentOwner)
    owner.dispatch_lock = RLock()
    owner.pending = []
    owner.persistence_fault = None
    owner.record = dict(phase='applying', maintenance=dict(dispatch='created',
        roles=['updater'], replacement_dispatch='not_started'))
    owner.children = {role:SimpleNamespace(code=0) for role in ('manager','updater')}
    for child in owner.children.values():child.poll=lambda c=child:c.code
    owner._maintenance_context=lambda value, **kw:owner.record['maintenance']
    owner._scope=lambda *a, **k:None
    records={}
    def commit(kind, proof, transaction):
        assert transaction==request.transaction_id
        records[kind]=deepcopy(proof)
        return kind
    owner._commit_record=commit
    owner._read_record=lambda ref:deepcopy(records[ref])
    owner._persist=lambda value:setattr(owner,'record',deepcopy(value))
    def exit(role):
        child=owner.children[role]
        return SimpleNamespace(process=child,identity=lambda:dict(role=role)),child.code
    owner._bound_native_exit=exit
    return owner,request,records


def rejected(*args):raise ValueError('signed payload changed')


def test_rejection_keeps_original_context_and_no_target_is_invented(original):
    owner,request,records=original
    with pytest.raises(ValueError,match='signed payload changed'):
        owner.finish_apply(request,verify_payload=rejected)
    assert owner.record['phase']=='replacement_rejected'
    assert owner.record['maintenance']['dispatch']=='created'
    assert 'apply_verified' not in owner.record['maintenance']
    assert 'replacement' not in owner.children
    proof=owner.rejected_replacement_before_start(request)
    assert proof['children'].keys()==owner.children.keys()
    assert proof['binding_sha256']==request.binding_sha256


@pytest.mark.parametrize('condition',['live_updater','failed_updater','uncertain_birth'])
def test_unsettled_apply_is_not_recoverable_prestart_rejection(original,condition):
    owner,request,records=original
    if condition=='live_updater':owner.children['updater'].code=None
    elif condition=='failed_updater':owner.children['updater'].code=7
    else:owner.record['maintenance']['replacement_dispatch']='reserved'
    with pytest.raises(Exception):owner.finish_apply(request,verify_payload=rejected)
    assert 'replacement_rejected' not in records


@pytest.mark.parametrize('condition',['new_child','changed_exit','pending','missing_record'])
def test_rejection_evidence_cannot_survive_changed_actor_scope(original,condition):
    owner,request,records=original
    with pytest.raises(ValueError):owner.finish_apply(request,verify_payload=rejected)
    if condition=='new_child':owner.children['replacement']=SimpleNamespace(poll=lambda:0)
    elif condition=='changed_exit':owner.children['manager'].code=3
    elif condition=='pending':owner.pending.append(object())
    else:owner.record['maintenance'].pop('replacement_rejected')
    with pytest.raises(LaunchHold):owner.rejected_replacement_before_start(request)
