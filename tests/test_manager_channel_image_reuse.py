"""Original-channel image reuse with real Windows sharing on generated files.
No Root/Reader/app launch, library, model or trainer is involved.
"""
import ctypes
import os
from pathlib import Path
import pytest
from core import manager_launch_owner as native

pytestmark = pytest.mark.skipif(os.name != 'nt', reason='Windows file sharing contract')

class ImageApi:
    def __init__(self, path):
        self.real = native._normal_native_api()
        self.path = path
        self.queries = 0
        self.pid_override = None
    def __getattr__(self, name):
        return getattr(self.real, name)
    def GetProcessId(self, handle):
        return self.pid_override or self.real.GetProcessId(handle)
    def QueryFullProcessImageNameW(self, handle, flags, buffer, length):
        self.queries += 1
        buffer.value = str(self.path)
        return True

@pytest.fixture
def channel(tmp_path):
    image = tmp_path / 'generated-channel-image.bin'
    image.write_bytes(b'synthetic executable bytes' * 2000)
    api = ImageApi(image)
    reader, writer = os.pipe()
    value = native.WindowsControlChannel(api, reader, writer, api.GetCurrentProcess(), epoch='original-epoch')
    value._begin_image_scope('a' * 32)
    try:
        yield value, api, image
    finally:
        value.close()


def test_thousands_of_original_stage_checks_do_not_reread_image(channel):
    value, api, image = channel
    expected = value.local_witness()
    retained = value._image_witnesses['local']
    class NoReads:
        def __init__(self, stream): self.stream = stream
        def __getattr__(self, name): return getattr(self.stream, name)
        def read(self, count): raise AssertionError('Repeated full executable read')
    retained.stream = NoReads(retained.stream)
    prior_queries = api.queries
    for _ in range(2000):
        assert value.local_witness() == expected
    assert api.queries == prior_queries + 2000


def test_channel_lease_denies_byte_writes_and_releases_after_close(channel):
    value, api, image = channel
    value.local_witness(); value.peer_witness()
    with pytest.raises(PermissionError):
        image.write_bytes(b'changed while retained')
    leases = list(value._image_witnesses.values())
    value.close()
    assert all(lease.stream.closed for lease in leases)
    image.write_bytes(b'allowed after original close')
    assert value._image_witnesses == {} and value._image_scope is None


def test_replacing_named_image_does_not_reuse_old_hash(channel, tmp_path):
    value, api, image = channel
    value.local_witness()
    replacement = tmp_path / 'replacement.bin'
    replacement.write_bytes(b'other signed or unsigned bytes')
    retired = image.with_name("retired-channel-image.bin")
    image.rename(retired)  # Stock activation retires the old name before replacement.
    replacement.rename(image)
    with pytest.raises(native.LaunchHold, match='original_control_image_file_changed'):
        value.local_witness()


@pytest.mark.parametrize('change', ['pid', 'path', 'epoch', 'api', 'handle'])
def test_original_channel_process_and_binding_changes_refuse(channel, change):
    value, api, image = channel
    value.peer_witness()
    if change == 'pid': api.pid_override = os.getpid() + 1
    if change == 'path': api.path = image.with_name('different.bin')
    if change == 'epoch': value.epoch = 'replacement-epoch'
    if change == 'api': value.api = ImageApi(image)
    if change == 'handle': value.peer_handle = 123456
    with pytest.raises(native.LaunchHold, match='original_control_image_process_changed'):
        value.peer_witness()


def test_initial_hash_failure_retains_no_lease(channel, monkeypatch):
    value, api, image = channel
    class BrokenDigest:
        def update(self, chunk): raise OSError('generated read failure')
    with monkeypatch.context() as patch:
        patch.setattr(native.hashlib, 'sha256', BrokenDigest)
        with pytest.raises(OSError, match='generated read failure'):
            value.local_witness()
    assert value._image_witnesses == {}
    image.write_bytes(b'lease was closed on failure')


def test_actual_current_process_identity_matches_existing_full_witness():
    api = native._normal_native_api()
    reader, writer = os.pipe()
    value = native.WindowsControlChannel(api, reader, writer, api.GetCurrentProcess(), epoch='actual-current-only')
    try:
        assert value.local_witness() == native._native_witness(api, api.GetCurrentProcess())
    finally:
        value.close()


def test_scope_release_allows_updater_overwrite_without_closing_channel(channel, tmp_path):
    value, api, image = channel
    value.local_witness(); value.peer_witness()
    leases = list(value._image_witnesses.values())
    value._end_image_scope('a' * 32)
    assert value._image_scope is None and value._image_witnesses == {}
    assert all(lease.stream.closed for lease in leases)
    os.fstat(value.reader); os.fstat(value.writer)  # Original channel remains retained.
    replacement = tmp_path / 'sdk-replacement.bin'
    replacement.write_bytes(b'generated SDK replacement')
    os.replace(replacement, image)  # The earlier channel-lifetime candidate denied this.
    assert value.local_witness()['executable_sha256'] != leases[0].digest
    assert value._image_witnesses == {}  # Ordinary full witnesses resumed.


def test_default_channels_do_not_retain_images(channel):
    value, api, image = channel
    value._end_image_scope()
    first = value.local_witness()
    image.write_bytes(b'generated changed file outside Organizer stage')
    assert value.local_witness()['executable_sha256'] != first['executable_sha256']
    assert value._image_witnesses == {}


def test_root_reader_never_enables_cache_and_organizer_release_precedes_maintenance(channel, tmp_path, monkeypatch):
    from copy import deepcopy
    from threading import RLock
    from types import SimpleNamespace
    from suite_core.manager.bootstrap_entry import NormalOwnerRoute
    value, api, image = channel
    value._end_image_scope()
    owner = native.DeploymentOwner.__new__(native.DeploymentOwner)
    owner.dispatch_lock = RLock(); owner._owned = lambda **kw: None
    owner._model_free_scope = None
    owner.root=tmp_path/'Code'; owner.data_root=tmp_path/'Data'
    owner.record={'phase':'normal','helper_grants_open':True,'owner':'generation','helper_attempts':{}}
    owner.children={'manager':SimpleNamespace(_handle=value.peer_handle,poll=lambda:None)}
    transport=SimpleNamespace(api=api,_creation_api=api,job=70,_creation_job=70,job_active_count=lambda:1)
    owner.native_transport=transport; value.transport=transport
    route=NormalOwnerRoute.__new__(NormalOwnerRoute)
    route.owner=owner; route.channel=value; route.state='ready'; route._live_peer=lambda:True
    route.wire=SimpleNamespace(proof=SimpleNamespace(channel_epoch=value.epoch))
    owner._persist=lambda record:setattr(owner,'record',deepcopy(record))
    reader=owner.component_stage_scope(route,'begin',None,component='collectigen.tag-encyclopedia')
    assert value._image_scope is None and value._image_witnesses == {}
    owner.component_stage_scope(route,'release',reader['scope_id'],component='collectigen.tag-encyclopedia')
    scope=owner.component_stage_scope(route,'begin',None,component='collectigen.media-organizer')
    value.local_witness(); value.peer_witness()
    retained=dict(owner.record['component_stage'])
    image_lease=value._image_witnesses['peer']
    with monkeypatch.context() as patch:
        patch.setattr(image_lease,'close',lambda:(_ for _ in ()).throw(OSError('generated close failure')))
        with pytest.raises(OSError,match='generated close failure'):
            owner.component_stage_scope(route,'release',scope['scope_id'],component='collectigen.media-organizer')
    assert owner.record['component_stage']==retained  # Failure grants no maintenance.
    owner.component_stage_scope(route,'release',scope['scope_id'],component='collectigen.media-organizer')
    assert 'component_stage' not in owner.record
    assert value._image_witnesses=={} and value._image_scope is None
    replacement=tmp_path/'sdk-after-release.bin';replacement.write_bytes(b'new program')
    os.replace(replacement,image)
    assert owner.children['manager']._handle==value.peer_handle and value.reader is not None


def test_entry_lost_release_ack_drops_only_cache_and_keeps_hold(channel, tmp_path):
    from threading import RLock
    from types import SimpleNamespace
    from suite_core.installation import ComponentId
    from suite_core.manager.bootstrap_entry import NormalEntrySession
    value, api, image=channel;value._end_image_scope()
    session=NormalEntrySession.__new__(NormalEntrySession);session.state='ready'
    session.binding=SimpleNamespace(owner_generation='original-owner')
    session.admitted=SimpleNamespace(layout=SimpleNamespace(code_root=tmp_path/'Code',data_root=tmp_path/'Data'))
    session.wire=SimpleNamespace(lock=RLock(),channel=value)
    scope={'scope_id':'b'*32,'component':'collectigen.media-organizer','owner_generation':'original-owner','code_root':str(tmp_path/'Code'),'data_root':str(tmp_path/'Data')}
    session._exchange=lambda kind,payload,expected:{'payload':{'schema':1,'operation':payload['operation'],'scope':scope}}
    component=ComponentId('collectigen.media-organizer')
    session.request_component_stage('begin',component=component)
    value.local_witness();assert value._image_witnesses
    pending={'operation':'release','scope_id':scope['scope_id']};session.component_stage_outstanding=pending
    session._exchange=lambda *args:(_ for _ in ()).throw(TimeoutError('generated lost ACK'))
    with pytest.raises(TimeoutError,match='generated lost ACK'):
        session.request_component_stage('release',scope['scope_id'],component=component)
    assert session.state=='held' and session.component_stage_outstanding is pending
    assert value._image_scope is None and value._image_witnesses=={}
    assert value.reader is not None and value.writer is not None


def test_late_checkpoint_cannot_recreate_image_lease_after_release(channel, monkeypatch):
    value, api, image=channel
    value.local_witness()
    late_check=lambda:value._image_witness('local',api.GetCurrentProcess())
    value._end_image_scope('a'*32)
    def forbidden(*args):raise AssertionError('Late checkpoint recreated a released lease')
    monkeypatch.setattr(native,'_RetainedChannelImage',forbidden)
    assert late_check()==native._native_witness(api,api.GetCurrentProcess())
    assert value._image_witnesses=={}
    image.write_bytes(b'no retained write-denial after release')
