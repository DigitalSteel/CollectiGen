"""Synthetic intake component evidence; no signing keys, processes or app runs.

Publisher-verifier stubs below isolate path/ordering/pin logic. They do not claim
cryptographic, installed-package, native or packaged acceptance qualification.
"""
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace
import sys

import pytest

from core.manager_launch_owner import WindowsControlChannel
from core.workflow_connection import WorkflowConnection
from suite_core.discovery.component_staging import component_origin
from suite_core.installation import ComponentId
from suite_core.workflow_modules import compiled_admission, entry
from suite_core.workflow_modules.compiled_admission import CompiledWorkflowAdmission
from suite_core.workflow_modules.session import ModuleEntrySession
from suite_core.tests.test_workflow_entry_session import native_fixture


def encode(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


@pytest.fixture
def profile(monkeypatch):
    # Explicit inert publisher-schema fixture; no real or generated private key.
    monkeypatch.setattr(compiled_admission, '_trust', lambda raw: {'synthetic': True})
    value = CompiledWorkflowAdmission('collectigen.generate', '0.1.0-alpha.1', b'synthetic-public-trust',
        sha256(b'synthetic-public-trust').hexdigest(), '0.7.8-alpha.1', '1.0.0', 'a' * 64)
    value.validate('collectigen.generate')
    return value


@pytest.fixture
def frozen(tmp_path, monkeypatch):
    root = tmp_path / 'suite'
    exe = root / 'Apps/collectigen.generate/0.1.0-alpha.1/Generate.exe'
    argv = ['--suite-code-root', str(root), '--suite-data-root', str(root / 'Data')]
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(exe))
    monkeypatch.setattr(sys, 'argv', [str(exe), *argv])
    return SimpleNamespace(root=root, exe=exe, argv=argv)


def test_missing_compiled_admission_stops_before_any_read_or_channel(monkeypatch):
    monkeypatch.setattr(compiled_admission, 'PROFILE', None)
    monkeypatch.setattr(entry, '_read', lambda *args: pytest.fail('No evidence read before compiled admission'))
    monkeypatch.setattr(WindowsControlChannel, 'from_inherited_environment', classmethod(
        lambda cls: pytest.fail('No native intake before compiled admission')))
    with pytest.raises(ValueError, match='compiled_workflow_admission_unavailable'):
        entry.entry_session('collectigen.generate')


def test_compiled_pin_and_qualification_are_required(profile):
    from dataclasses import replace
    for changed in (replace(profile, trust_sha256='b' * 64),
                    replace(profile, native_entry_qualification_sha256=''),
                    replace(profile, minimum_manager_version='2.0.0')):
        with pytest.raises(ValueError):
            changed.validate('collectigen.generate')


def test_cli_locators_cannot_change_frozen_root_or_enroll_peer_pin(frozen, monkeypatch):
    assert entry.entry_locations('collectigen.generate').code_root == frozen.root
    changed = [str(frozen.exe), *frozen.argv, '--expected-peer-sha256', 'f' * 64]
    monkeypatch.setattr(sys, 'argv', changed)
    with pytest.raises(ValueError, match='exact_workflow_entry_locators_required'):
        entry.entry_locations('collectigen.generate')
    monkeypatch.setattr(sys, 'argv', [str(frozen.exe), '--suite-code-root', str(frozen.root / 'foreign'),
                                    '--suite-data-root', str(frozen.root / 'foreign/Data')])
    with pytest.raises(ValueError, match='exact_frozen_workflow_roots_required'):
        entry.entry_locations('collectigen.generate')


def test_component_is_fixed_by_frozen_entry(frozen):
    with pytest.raises(ValueError, match='exact_frozen_workflow_roots_required'):
        entry.entry_locations('collectigen.train')


@pytest.fixture
def initial_parent(frozen, profile, monkeypatch):
    root = frozen.root
    base = root / entry.INITIAL_MANAGER_EVIDENCE
    base.mkdir(parents=True)
    rows = []
    from core.manager_successor_admission import installed_manager_payload_path
    for name in ('CollectiGenManager.exe', 'Squirrel.exe', 'sq.version', 'collectigen.suite-manager_ExecutionStub.exe'):
        path = installed_manager_payload_path(root, name, stock_applied=False)
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = ('synthetic:' + name).encode()
        path.write_bytes(raw)
        rows.append({'path': name, 'size_bytes': len(raw), 'sha256': sha256(raw).hexdigest()})
    inventory = {'schema_version': 1, 'component_id': 'collectigen.suite-manager',
        'software_version': '0.8.0-alpha.1', 'platform': {'os': 'windows', 'architecture': 'x86_64'},
        'runtime_kind': 'frozen-portable', 'files': rows,
        'inventory_profile': 'manager-windows-full-nupkg-detached-v1', 'archive_sha256': 'c' * 64,
        'archive_size_bytes': 123}
    proof = encode(inventory)
    (base / 'inventory.json').write_bytes(proof)
    (base / 'release.json').write_bytes(encode({'version': '0.8.0-alpha.1', 'channel': 'beta'}))
    (base / 'release.sig.json').write_bytes(b'synthetic-signature-envelope')
    artifact = SimpleNamespace(portable_manifest_sha256=sha256(proof).hexdigest(), sha256='c' * 64, size_bytes=123)
    calls = []
    def verifier(manifest, signature, trust, **expected):
        calls.append((manifest, signature, trust, expected))
        assert trust == profile.trust_bytes
        assert signature == b'synthetic-signature-envelope'
        assert expected['expected_component'].value == 'collectigen.suite-manager'
        return SimpleNamespace(artifact=artifact, software_version='0.8.0-alpha.1')
    monkeypatch.setattr(entry, 'verify_component_release', verifier)
    return SimpleNamespace(root=root, base=base, artifact=artifact, calls=calls, inventory=inventory,
        expected=next(row['sha256'] for row in rows if row['path'] == 'CollectiGenManager.exe'))


def test_initial_signed_inventory_derives_exact_installed_peer_pin(initial_parent, profile):
    f = initial_parent
    assert entry.manager_peer_pin(f.root, profile) == f.expected
    assert len(f.calls) == 1


def test_unsigned_or_changed_parent_inventory_cannot_supply_pin(initial_parent, profile):
    f = initial_parent
    (f.base / 'inventory.json').write_bytes(encode({**f.inventory, 'archive_sha256': 'e' * 64}))
    with pytest.raises(ValueError, match='signed_manager_parent_inventory_changed'):
        entry.manager_peer_pin(f.root, profile)


def test_changed_installed_manager_bytes_are_refused(initial_parent, profile):
    f = initial_parent
    (f.root / 'Apps/Manager/current/CollectiGenManager.exe').write_bytes(b'changed')
    with pytest.raises(ValueError, match='signed_installed_manager_parent_changed'):
        entry.manager_peer_pin(f.root, profile)


def test_invalid_successor_never_falls_back_to_valid_initial_evidence(initial_parent, profile):
    f = initial_parent
    (f.root / '_bootstrap/manager-successor.json').write_bytes(encode({'transaction': '../../foreign'}))
    with pytest.raises(ValueError):
        entry.manager_peer_pin(f.root, profile)
    assert not f.calls


def test_missing_initial_evidence_refuses_instead_of_using_unsigned_package(frozen, profile):
    frozen.root.mkdir(parents=True)
    (frozen.root / 'MANAGER_PACKAGE.json').write_bytes(encode({'manager_sha256': 'f' * 64}))
    with pytest.raises((OSError, RuntimeError, ValueError)):
        entry.manager_peer_pin(frozen.root, profile)


def test_active_module_origin_and_publisher_evidence_are_required(frozen, profile, monkeypatch):
    locations = entry.entry_locations('collectigen.generate')
    base = frozen.root / 'State/collectigen.generate'
    base.mkdir(parents=True)
    component = ComponentId('collectigen.generate')
    origin = component_origin(frozen.root, frozen.root / 'Data', component)
    active = {'component_id': component.value, 'version': profile.software_version,
              'directory': f'Apps/{component.value}/{profile.software_version}', 'inventory_sha256': 'a' * 64}
    (base / 'component-origin.json').write_bytes(encode(origin))
    (base / 'active.json').write_bytes(encode(active))
    import base64
    manifest = encode({'channel': 'beta'})
    journal = {'phase': 'activated', 'target': active, 'before': None,
               'manifest': base64.b64encode(manifest).decode(),
               'signature': base64.b64encode(b'synthetic-signature').decode()}
    (base / 'transaction.json').write_bytes(encode(journal))
    def validate(value, expected_component, owner, trust):
        assert expected_component == component and owner == origin['owner'] and trust == profile.trust_bytes
    monkeypatch.setattr(entry, 'validate_journal', validate)
    selected = WorkflowConnection(component.value, locations.package_root, profile.software_version,
                                  'a' * 64, 'b' * 64, 'synthetic')
    def review(component_id, directory, **evidence):
        assert evidence['signature_bytes'] == b'synthetic-signature'
        assert evidence['trust_bytes'] == profile.trust_bytes
        return selected
    monkeypatch.setattr(entry, 'review_existing_package', review)
    assert entry._module_admission(locations, profile)[0] is selected
    (base / 'component-origin.json').write_bytes(encode({**origin, 'owner': '0' * 32}))
    with pytest.raises(ValueError, match='original_workflow_component_origin_required'):
        entry._module_admission(locations, profile)


@pytest.mark.parametrize('refused', [False, True])
def test_real_entry_orders_package_and_peer_proof_before_returning_session(frozen, profile, monkeypatch, refused):
    events = []
    monkeypatch.setattr(compiled_admission, 'PROFILE', profile)
    monkeypatch.setattr(entry, '_module_admission', lambda *args: (events.append('module-proof') or object(), b'm', b's'))
    monkeypatch.setattr(entry, 'manager_peer_pin', lambda *args: events.append('manager-proof') or 'e' * 64)
    admitted = object()
    def admit(**kwargs):
        events.append('independent-binding')
        assert kwargs['expected_peer_image_sha256'] == 'e' * 64
        return admitted
    monkeypatch.setattr(entry, 'admit_module_entry', admit)
    # Typed inert endpoint: exercise real scope ownership without native handles,
    # image reads or IPC. Package/native proof calls remain ordering-only stubs.
    channel = WindowsControlChannel(SimpleNamespace(), None, None, None, epoch='2' * 32)
    close_channel = channel.close
    def close():
        events.append('channel-close')
        close_channel()
    monkeypatch.setattr(channel, 'close', close)
    monkeypatch.setattr(WindowsControlChannel, 'from_inherited_environment', classmethod(
        lambda cls: events.append('native-channel') or channel))
    monkeypatch.setattr(entry, '_exact_parent_path', lambda *args: events.append('exact-native-parent-path'))
    session = SimpleNamespace(offer=SimpleNamespace(owner_generation='1' * 32,
                              attempt_id='3' * 32, grant_id='4' * 32))
    def accept(cls, actual_channel, binding, **kwargs):
        events.append('authenticated-session')
        assert actual_channel is channel and binding is admitted
        assert set(kwargs) == {'timeout'}  # Lifetimes come from the authenticated grant.
        assert channel._workflow_image_owner.attempt_id is None
        assert channel._workflow_image_owner.reservation is None
        if refused:
            raise RuntimeError('synthetic peer refusal')
        return session
    monkeypatch.setattr(ModuleEntrySession, 'accept', classmethod(accept))
    expected = ['module-proof', 'manager-proof', 'independent-binding', 'native-channel',
                'exact-native-parent-path', 'authenticated-session']
    if refused:
        with pytest.raises(RuntimeError, match='synthetic peer refusal'):
            entry.entry_session(profile.component_id)
        assert events == expected + ['channel-close']
        assert channel._workflow_image_owner is None and channel._image_scope is None
    else:
        assert entry.entry_session(profile.component_id) is session
        assert events == expected + ['exact-native-parent-path']
        scope = channel._workflow_image_owner
        assert (scope.owner_generation, scope.attempt_id, scope.reservation) == ('1' * 32, '3' * 32, '4' * 32)
        assert not scope.closed
    assert channel._image_witnesses == {}  # No native/image boundary executed.


def test_authenticated_lifetime_intake_consumes_initial_frame_once(native_fixture):
    f = native_fixture
    f.prepare()
    session = ModuleEntrySession.accept(f.channel, f.admitted)
    assert session.offer.grant_id == f.offer.grant_id
    assert session.state == 'ready'
    assert f.incoming == []


def test_partial_expected_lifetime_never_becomes_fallback(native_fixture):
    f = native_fixture
    f.prepare()
    with pytest.raises(ValueError, match='complete_expected_workflow_lifetime_required'):
        ModuleEntrySession.accept(f.channel, f.admitted, expected_owner_generation='1' * 32)
    assert len(f.incoming) == 2


@pytest.mark.parametrize('parent_pid,parent_birth,expected', [(7, 10, True), (8, 10, False), (7, 30, False)])
def test_parent_lineage_uses_original_query_handle_not_only_image_pin(monkeypatch, parent_pid, parent_birth, expected):
    import ctypes
    from ctypes import wintypes as W
    class InertQuery:
        def __call__(self, handle, information_class, output, length, returned):
            assert information_class == 0 and handle == -1
            value = ctypes.cast(output, ctypes.POINTER(entry._ProcessBasicInformation)).contents
            value.pid, value.parent_pid = 9, parent_pid
            ctypes.cast(returned, ctypes.POINTER(W.ULONG))[0] = ctypes.sizeof(value)
            return 0
    monkeypatch.setattr(ctypes, 'WinDLL', lambda *args, **kwargs: SimpleNamespace(NtQueryInformationProcess=InertQuery()))
    channel = SimpleNamespace(api=SimpleNamespace(GetCurrentProcess=lambda: -1,
        WaitForSingleObject=lambda handle, timeout: 258), peer_handle=22,
        local_witness=lambda: {'pid': 9, 'creation_filetime': 20},
        peer_witness=lambda: {'pid': 7, 'creation_filetime': parent_birth})
    if expected:
        entry._direct_native_parent(channel)
    else:
        with pytest.raises(ValueError, match='actual_live_manager_parent_required'):
            entry._direct_native_parent(channel)
