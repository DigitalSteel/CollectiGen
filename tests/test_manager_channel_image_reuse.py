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
    with pytest.raises(native.LaunchHold, match='live_original_control_channel_required'):
        value.local_witness()


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
