"""RPC dispatch state doubles; no native admission or executable is created."""
from threading import RLock
from types import SimpleNamespace
import pytest
from core.encyclopedia_legacy_handoff import LegacyReaderRefusal
from suite_core.manager.bootstrap_entry import NormalOwnerRoute, NormalEntrySession


def route_for(handler):
    request=dict(schema=1,request_id='a'*32,operation='prepare',profile_sha256='b'*64,handoff_id=None)
    replies=[]
    owner=SimpleNamespace(dispatch_lock=RLock(),record={},_owned=lambda:None)
    host=SimpleNamespace(profile=SimpleNamespace(identity='b'*64),plan=None,handle=handler)
    route=object.__new__(NormalOwnerRoute)
    route.owner=owner;route.state='ready';route.legacy_reader_host=host
    route.legacy_reader_request_ids=set();route._live_peer=lambda:True
    route.wire=SimpleNamespace(lock=RLock(),receive=lambda:dict(kind='legacy-reader-request',payload=request),
        send=lambda kind,payload:replies.append((kind,payload)))
    return route,request,replies


def test_clean_settled_refusal_is_correlated_and_does_not_hold_normal_route():
    calls=[]
    def refuse(route,request):
        calls.append(request['request_id']);raise LegacyReaderRefusal('synthetic selection conflict')
    route,request,replies=route_for(refuse)
    assert route.handle_next()=='legacy-reader-request'
    assert route.state=='ready' and calls==['a'*32]
    kind,reply=replies[0]
    assert kind=='legacy-reader-reply' and all(reply[key]==value for key,value in request.items())
    assert reply['result']==dict(status='refused',handoff_id=None,committed=False,consumed=True,reason='synthetic selection conflict')
    with pytest.raises(Exception):route.handle_next()
    assert calls==['a'*32] and route.state=='held'


def test_unreleased_stage_cannot_be_reported_as_clean_refusal():
    def refuse(route,request):
        route.owner.record['component_stage']={'original':'unresolved'}
        raise LegacyReaderRefusal('synthetic refusal with unresolved scope')
    route,request,replies=route_for(refuse)
    with pytest.raises(Exception):route.handle_next()
    assert replies==[] and route.state=='held' and 'component_stage' in route.owner.record


def test_foreign_profile_does_not_dispatch_a_request():
    route,request,replies=route_for(lambda *args:pytest.fail('foreign request must not execute'))
    request['profile_sha256']='d'*64
    with pytest.raises(Exception):route.handle_next()
    assert replies==[] and route.state=='held'


def test_uncertain_write_error_is_never_downgraded_to_a_refusal():
    def fail(*args):raise OSError('injected ambiguous settings writer failure')
    route,request,replies=route_for(fail)
    with pytest.raises(OSError):route.handle_next()
    assert replies==[] and route.state=='held'


def child_sequence():
    """Real child RPC state machine, inert wire boundary; no native authority."""
    from copy import deepcopy
    subject=object.__new__(NormalEntrySession)
    subject.state='offered';subject.error=None;subject.helper_outstanding=None
    subject.component_stage_outstanding=None;subject.legacy_reader_outstanding=None
    subject.legacy_reader_completed=None;subject.legacy_reader_profile_sha256='b'*64
    subject.scope=dict(generation=1,roles=['manager'],roles_sha256='c'*64)
    subject.helpers={};subject.reservations={};subject.pending_helpers=[];subject.helper_capabilities=[]
    subject.binding=SimpleNamespace(owner_generation='a'*32)
    subject._peer=lambda:None
    state=dict(sent=[],replies=[],malformed=False,fault=None,idle=False)
    def send(kind,payload):
        state['sent'].append((kind,deepcopy(payload)))
        if kind=='ready':reply=dict(kind='ready',payload={'accepted':True})
        elif kind=='legacy-reader-request':
            result=dict(status='ready',handoff_id='d'*64,committed=False,
                consumed=payload['operation']=='cancel',reason=None)
            value={**deepcopy(payload),'result':result}
            if state['malformed']:value['request_id']='e'*32
            reply=dict(kind='legacy-reader-reply',payload=value)
        elif kind=='seal-request':reply=dict(kind='sealed',payload=deepcopy(payload))
        elif kind=='local-drained':reply=dict(kind='local-report-accepted',payload=dict(
            status='local_scope_accepted',generation=1,roles_sha256='c'*64))
        else:raise AssertionError(kind)
        state['replies'].append(reply)
    def receive(**kwargs):
        if state['fault'] is not None:raise state['fault']
        if state['idle']:
            state['idle']=False
            from core.manager_launch_owner import LaunchHold
            raise LaunchHold('control_channel_idle_timeout')
        return state['replies'].pop(0)
    subject.wire=SimpleNamespace(lock=RLock(),send=send,receive=receive,
        channel=SimpleNamespace(local_witness=lambda:{'original':'entry'},read_fault=None),received_pending=None)
    return subject,state


def test_actual_ready_then_legacy_prepare_cancel_and_ordinary_cleanup():
    subject,state=child_sequence();subject.ready()
    original=subject.readiness_outstanding
    result=subject.request_legacy_reader('prepare')
    assert not result['consumed'] and subject.readiness_outstanding is original
    assert subject.legacy_reader_outstanding is None
    completed=subject.legacy_reader_completed
    result=subject.request_legacy_reader('cancel',result['handoff_id'])
    assert result['consumed'] and subject.legacy_reader_completed is not completed
    assert subject.readiness_outstanding is original and original['reply']['kind']=='ready'
    assert subject.begin_cleanup() and subject.local_drained()
    assert subject.state=='local_accepted'
    assert [k for k,_ in state['sent']]==['ready','legacy-reader-request','legacy-reader-request','seal-request','local-drained']


def test_legacy_idle_waits_for_same_reply_without_reusing_startup_receipt():
    subject,state=child_sequence();subject.ready();original=subject.readiness_outstanding
    state['idle']=True
    assert subject.request_legacy_reader('prepare')['status']=='ready'
    assert subject.readiness_outstanding is original
    assert [k for k,_ in state['sent']].count('legacy-reader-request')==1


@pytest.mark.parametrize('fault',[False,True])
def test_bad_legacy_reply_or_channel_retains_only_its_original_pending_request(fault):
    subject,state=child_sequence();subject.ready();original=subject.readiness_outstanding
    if fault:state['fault']=OSError('injected definitive original pipe failure')
    else:state['malformed']=True
    with pytest.raises((ValueError,OSError)):subject.request_legacy_reader('prepare')
    assert subject.state=='held' and subject.readiness_outstanding is original
    assert subject.legacy_reader_outstanding['send_entered']
    pending=subject.legacy_reader_outstanding
    with pytest.raises(ValueError):subject.request_legacy_reader('prepare')
    with pytest.raises(ValueError):subject.begin_cleanup()
    assert subject.legacy_reader_outstanding is pending
    assert [k for k,_ in state['sent']].count('legacy-reader-request')==1


def test_unsent_first_prepare_is_dismissible_but_original_helper_hold_remains():
    from suite_core.manager.bootstrap_entry import LegacyReaderPrepareNotSent
    subject,state=child_sequence();subject.ready();original=subject.readiness_outstanding
    subject.helper_outstanding={'original':'unsettled helper request'}
    with pytest.raises(LegacyReaderPrepareNotSent):subject.request_legacy_reader('prepare')
    assert subject.state=='ready' and subject.readiness_outstanding is original
    assert subject.legacy_reader_outstanding is None and [k for k,_ in state['sent']]==['ready']
    with pytest.raises(ValueError):subject.begin_cleanup()


def test_unsent_prepare_cannot_dismiss_an_existing_prepared_plan():
    from suite_core.manager.bootstrap_entry import LegacyReaderPrepareNotSent
    subject,state=child_sequence();subject.ready();subject.request_legacy_reader('prepare')
    subject.helper_outstanding={'original':'unsettled helper request'}
    with pytest.raises(ValueError) as raised:subject.request_legacy_reader('prepare')
    assert not isinstance(raised.value,LegacyReaderPrepareNotSent)
    assert len(state['sent'])==2
