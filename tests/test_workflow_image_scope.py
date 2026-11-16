"""Workflow image scopes: generated-file / synthetic-native component evidence.
No app launch, packaged qualification, private data or global image cache.
"""
from copy import deepcopy
import ctypes
from hashlib import sha256
import os
from threading import RLock
from types import SimpleNamespace

import pytest

from core import manager_launch_owner as native
from core.workflow_native_channel import WorkflowImageScope
from core.workflow_root_selection import WorkflowHelperWireAdmission
from suite_core.manager import bootstrap_entry as entry


@pytest.fixture
def channel():
    api = SimpleNamespace(GetCurrentProcess=lambda: 11)
    value = native.WindowsControlChannel(api, 10001, 10002, 12, epoch='2' * 32)
    return value


def test_overlap_and_unscoped_disposal_cannot_steal_workflow(channel):
    scope = WorkflowImageScope(channel, 'collectigen.generate', custodian=object())
    with pytest.raises(RuntimeError, match='workflow_image_scope_overlap'):
        WorkflowImageScope(channel, 'collectigen.train', custodian=object())
    with pytest.raises(native.LaunchHold, match='fresh_original_organizer_image_scope_required'):
        channel._begin_image_scope('3' * 32)
    with pytest.raises(native.LaunchHold, match='original_workflow_image_owner_required'):
        channel._end_image_scope()
    with pytest.raises(native.LaunchHold, match='original_workflow_image_owner_required'):
        channel._end_image_scope(scope.epoch)
    assert channel._workflow_image_owner is scope
    scope.close()
    channel._begin_image_scope('3' * 32)  # Original staging becomes available again.
    channel._end_image_scope('3' * 32)


@pytest.mark.parametrize('position', range(3))
def test_correlation_cannot_rebind_an_enrolled_lifetime(channel, position):
    scope = WorkflowImageScope(channel, 'collectigen.generate', custodian=object())
    identity = ['1' * 32, '4' * 32, '5' * 32]
    scope.bind(*identity)
    identity[position] = '9' * 32
    with pytest.raises(RuntimeError, match='workflow_image_lifetime_changed'):
        scope.bind(*identity)
    assert not scope.closed
    scope.close()


def test_close_failure_retains_exact_owner_and_original_scope(channel):
    scope = WorkflowImageScope(channel, 'collectigen.train', custodian=object())
    def fail():
        raise OSError('synthetic file close failure')
    channel._image_witnesses['peer'] = SimpleNamespace(close=fail)
    with pytest.raises(OSError, match='synthetic file close failure'):
        scope.close()
    assert not scope.closed and channel._workflow_image_owner is scope
    assert channel._image_scope == scope.epoch
    channel._image_witnesses['peer'].close = lambda: None
    scope.close()
    assert scope.closed and not channel._image_witnesses


@pytest.fixture
def root_route(channel):
    owner = native.DeploymentOwner.__new__(native.DeploymentOwner)
    owner.dispatch_lock = RLock()
    owner._helper_wire_peer = lambda route: None  # Explicit synthetic native admission boundary.
    owner._helper_wire_sequence = 0
    owner._helper_wire_binding = {'module': entry, 'helper_generation': 1}
    owner._retire_drained_helper_queries = lambda: None
    owner._fresh_helper_admission = lambda admission: admission
    owner._persist = lambda record: setattr(owner, 'record', deepcopy(record))
    owner._helper_wire_reply = lambda row: deepcopy(row)
    owner._bound_native_exit = lambda role: (None, 0)
    route = SimpleNamespace(channel=channel, state='ready', _live_peer=lambda: True,
        wire=SimpleNamespace(incoming=1, proof=SimpleNamespace(channel_epoch=channel.epoch,
                                                              binding_sha256='f' * 64)))
    attempt, reservation = '4' * 32, '5' * 32
    role = 'owned-generate-' + attempt
    context = {'component': 'collectigen.generate'}
    admission = WorkflowHelperWireAdmission(context, 'a' * 64,
                                            SimpleNamespace(component_id='collectigen.generate'))
    owner._helper_wire_admissions = {role: (admission, context, 'a' * 64)}
    owner.record = {'owner': '1' * 32, 'helper_generation': 1,
                    'helper_grants_open': True, 'helper_attempts': {}}
    owner.children = {role: object()}
    digest = sha256(native._bytes(dict(context, helper_attempt_id=attempt))).hexdigest()
    def frame(kind, **extras):
        return dict(protocol=1, kind=kind, sequence=route.wire.incoming, binding_sha256='f' * 64,
            payload=dict(attempt_id=attempt, context_sha256=digest, role=role,
                         program_sha256='a' * 64, reservation=None if kind == 'helper-reserve' else reservation,
                         **extras))
    def reserve(*args, **kwargs):
        owner.record['helper_attempts'][attempt].update(phase='granted', reservation=reservation)
    owner.reserve_helper = reserve
    return SimpleNamespace(owner=owner, route=route, frame=frame, attempt=attempt,
                           reservation=reservation, role=role, context=context)


def grant(f):
    f.owner.dispatch_helper_wire(f.route, f.frame('helper-reserve',
        context=dict(f.context, helper_attempt_id=f.attempt), creation='not_entered'))
    f.route.wire.incoming += 1
    return f.route.channel._workflow_image_owner


def test_root_scope_retained_after_grant_and_refused_drain(root_route):
    f = root_route
    scope = grant(f)
    assert (scope.owner_generation, scope.attempt_id, scope.reservation) == ('1' * 32, f.attempt, f.reservation)
    f.owner.record['helper_attempts'][f.attempt]['phase'] = 'transferred'
    with pytest.raises(native.LaunchHold, match='bound_original_helper_drain_required'):
        f.owner.dispatch_helper_wire(f.route, f.frame('helper-drained',
            root_exited=True, private_job_zero=False, output_drained=True))
    assert not scope.closed
    f.route.wire.incoming += 1
    f.owner.dispatch_helper_wire(f.route, f.frame('helper-drained',
        root_exited=True, private_job_zero=True, output_drained=True))
    assert scope.closed
    assert f.owner.record['helper_attempts'][f.attempt]['phase'] == 'drained'


def test_root_scope_retained_when_grant_publication_is_uncertain(root_route):
    f = root_route
    def uncertain(*args, **kwargs):
        raise OSError('synthetic grant persistence failure')
    f.owner.reserve_helper = uncertain
    with pytest.raises(OSError, match='synthetic grant persistence failure'):
        grant(f)
    scope = f.route.channel._workflow_image_owner
    assert scope.attempt_id == f.attempt and scope.reservation is None
    assert not scope.closed


@pytest.mark.skipif(os.name != 'nt', reason='Stock Windows retained image sharing')
def test_stock_workflow_witnesses_hash_each_generated_image_once(tmp_path):
    image = tmp_path / 'generated-image.bin'
    image.write_bytes(b'generated workflow image bytes' * 100)
    class ImageApi:
        def __init__(self):
            self.real = native._normal_native_api()
            self.queries = 0
        def __getattr__(self, name):
            return getattr(self.real, name)
        def QueryFullProcessImageNameW(self, handle, flags, buffer, length):
            self.queries += 1
            buffer.value = str(image)
            return True
    api = ImageApi()
    reader, writer = os.pipe()
    channel = native.WindowsControlChannel(api, reader, writer, api.GetCurrentProcess(), epoch='2' * 32)
    scope = WorkflowImageScope(channel, 'collectigen.generate', custodian=object())
    scope.bind('1' * 32, '4' * 32, '5' * 32)
    try:
        identities = channel.local_witness(), channel.peer_witness()
        class NoReads:
            def __init__(self, stream): self.stream = stream
            def __getattr__(self, name): return getattr(self.stream, name)
            def read(self, count): raise AssertionError('Repeated workflow image hash read')
        leases = tuple(channel._image_witnesses.values())
        assert len(leases) == 2
        for lease in leases:
            lease.stream = NoReads(lease.stream)
        before = api.queries
        for _ in range(100):
            assert (channel.local_witness(), channel.peer_witness()) == identities
        assert api.queries == before + 200
        with pytest.raises(PermissionError):
            image.write_bytes(b'write denied while original image held')
        scope.close()
        assert all(lease.stream.closed for lease in leases)
        image.write_bytes(b'allowed after positive owner scope disposal')
    finally:
        channel.close()
