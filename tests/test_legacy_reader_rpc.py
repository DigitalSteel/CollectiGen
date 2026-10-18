"""RPC dispatch state doubles; no native admission or executable is created."""
from threading import RLock
from types import SimpleNamespace
import pytest
from core.encyclopedia_legacy_handoff import LegacyReaderRefusal
from suite_core.manager.bootstrap_entry import NormalOwnerRoute


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
