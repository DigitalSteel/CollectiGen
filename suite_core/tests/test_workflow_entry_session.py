"""Synthetic component evidence only; no native process/package qualification.

The real WindowsControlChannel.qualify_binding and stock framing run against
explicit inert witness fixtures. Package review is stubbed only in the session
fixture; these tests do not claim publisher or installed-package acceptance.
"""
from dataclasses import replace
from hashlib import sha256
from pathlib import Path
from types import SimpleNamespace
import copy
import sys

import pytest

from core.manager_launch_owner import WindowsControlChannel
from core.workflow_connection import WorkflowConnection
from suite_core.installation import InstallMode, SuiteLayout
from suite_core.manager.bootstrap_entry import _wire_bytes
from suite_core.workflow_modules.contracts import WorkflowWorkspaceOffer
from suite_core.workflow_modules.entry_binding import ModuleEntryBinding
from suite_core.workflow_modules.session import ModuleEntrySession


def framed(kind, payload, sequence, binding_pin):
    raw = _wire_bytes({'protocol': 1, 'sequence': sequence, 'kind': kind,
                      'binding_sha256': binding_pin, 'payload': payload})
    return len(raw).to_bytes(4, 'little') + raw


@pytest.fixture
def native_fixture(tmp_path, monkeypatch):
    """Explicit synthetic native handles; no processes or actual channel I/O."""
    root = tmp_path / 'suite'
    package = root / 'Apps/collectigen.generate/0.1.0-alpha.1'
    selected = WorkflowConnection('collectigen.generate', package, '0.1.0-alpha.1',
                                  'a' * 64, 'b' * 64, 'synthetic-selected-revision')
    admitted = ModuleEntryBinding(SuiteLayout(root, root / 'Data', InstallMode.PORTABLE),
        selected, b'manifest', b'signature', b'trust', sha256(b'trust').hexdigest(),
        'c' * 64, (), package / 'Generate.exe')
    monkeypatch.setattr(ModuleEntryBinding, 'validate', lambda self: 'd' * 64)
    local = {'pid': 11, 'creation_filetime': 111, 'executable_sha256': 'd' * 64}
    peer = {'pid': 12, 'creation_filetime': 222, 'executable_sha256': 'c' * 64}
    api = SimpleNamespace(wait=258)
    api.WaitForSingleObject = lambda handle, timeout: api.wait
    channel = WindowsControlChannel(api, 1, 2, 3, epoch='2' * 32)
    document = {'protocol': 1, 'owner_generation': '1' * 32, 'channel_epoch': '2' * 32,
                'deployment_root': str(root), 'data_root': str(root / 'Data'),
                'settings_path': str(admitted.settings_path), 'mode': 'portable',
                'argv_sha256': sha256(b'[]').hexdigest(), 'entry': local.copy(), 'launcher': peer.copy()}
    offer = WorkflowWorkspaceOffer(1, 1, selected.component_id, selected.version, root, root / 'Data',
        package, root / 'Data/Manager', selected.inventory_sha256, selected.manifest_sha256,
        '1' * 32, '2' * 32, '3' * 32, '4' * 32, sha256(_wire_bytes(local)).hexdigest(), 'exclusive-writer')
    incoming, sent = [], []
    monkeypatch.setattr(WindowsControlChannel, 'local_witness', lambda self: local.copy())
    monkeypatch.setattr(WindowsControlChannel, 'peer_witness', lambda self: peer.copy())
    monkeypatch.setattr(WindowsControlChannel, 'read_frame', lambda self, timeout=1: incoming.pop(0))
    monkeypatch.setattr(WindowsControlChannel, 'write_frame', lambda self, raw, timeout=1: sent.append(raw))
    pin = sha256(_wire_bytes(document)).hexdigest()
    scope = {'component_id': offer.component_id, 'attempt_id': offer.attempt_id, 'grant_id': offer.grant_id}
    def prepare(*, changed_offer=None, changed_document=None, frame_pin=None):
        doc = document if changed_document is None else changed_document
        digest = sha256(_wire_bytes(doc)).hexdigest()
        incoming.append(framed('workflow-offer', {'binding': doc,
            'workspace': offer.mapping() if changed_offer is None else changed_offer}, 1, frame_pin or digest))
        incoming.append(framed('workflow-ready-ack', {**scope, 'state': 'ready'}, 2, digest))
    def accept(**changes):
        kwargs = dict(expected_owner_generation='1' * 32, expected_attempt_id='3' * 32, expected_grant_id='4' * 32)
        kwargs.update(changes)
        return ModuleEntrySession.accept(channel, admitted, **kwargs)
    return SimpleNamespace(admitted=admitted, channel=channel, local=local, peer=peer, api=api,
        document=document, offer=offer, incoming=incoming, sent=sent, pin=pin, scope=scope,
        prepare=prepare, accept=accept)


def test_authenticated_child_and_explicit_ready_check(native_fixture):
    f = native_fixture
    f.prepare()
    session = f.accept()
    assert session.workspace_root == f.admitted.layout.data_root / 'Manager'
    f.incoming.append(framed('workflow-check-ack', {**f.scope, 'state': 'ready'}, 3, f.pin))
    assert session.assert_ready() is session
    assert session.lease_release_authorized is False


@pytest.mark.parametrize('defect', [None, 'outside_recovery', 'foreign_trust', 'foreign_parent'])
def test_optional_worker_baseline_is_peer_bound_before_any_worker_access(native_fixture, defect):
    f = native_fixture
    reference = dict(version='0.8.0-alpha.1', channel='beta',
        platform={'os': 'windows', 'architecture': 'x86_64'}, files={
            name: dict(path='Recovery/generated/' + name, size=32, sha256='a' * 64)
            for name in ('archive', 'manifest', 'signature', 'trust', 'proof', 'inventory', 'executable')})
    reference['files']['trust']['sha256'] = f.admitted.expected_trust_sha256
    reference['files']['executable']['sha256'] = f.peer['executable_sha256']
    if defect == 'outside_recovery':
        reference['files']['archive']['path'] = 'Data/other/archive'
    elif defect == 'foreign_trust':
        reference['files']['trust']['sha256'] = '0' * 64
    elif defect == 'foreign_parent':
        reference['files']['executable']['sha256'] = '0' * 64
    f.prepare()
    f.incoming[0] = framed('workflow-offer', {'binding': f.document,
        'workspace': f.offer.mapping(), 'worker_baseline': reference}, 1, f.pin)
    if defect is not None:
        with pytest.raises(ValueError):
            f.accept()
        assert not f.sent
    else:
        session = f.accept()
        assert session.worker_baseline == reference
        assert session.worker_baseline is not reference


@pytest.mark.parametrize('path', ['generation/drafts/' + 'a' * 32 + '.json', '../other.json'])
def test_saved_handoff_locator_requires_canonical_draft_after_native_proof(native_fixture, path):
    f = native_fixture
    f.prepare()
    draft = {'relative_path': path, 'sha256': 'b' * 64}
    f.incoming[0] = framed('workflow-offer', {'binding': f.document,
        'workspace': f.offer.mapping(), 'initial_draft': draft}, 1, f.pin)
    if path.startswith('../'):
        with pytest.raises(ValueError, match='exact_saved_generation_handoff_required'):
            f.accept()
        assert not f.sent
    else:
        session = f.accept()
        assert session.initial_draft == draft
        assert not f.admitted.workspace_root.exists()  # Session construction never reads/creates a draft.


@pytest.mark.parametrize('field', ['expected_owner_generation', 'expected_attempt_id', 'expected_grant_id'])
def test_foreign_lifetime_refused(native_fixture, field):
    f = native_fixture
    f.prepare()
    with pytest.raises(ValueError, match='original_workflow_grant_required'):
        f.accept(**{field: '9' * 32})
    assert not f.sent


@pytest.mark.parametrize('field,value', [
    ('inventory_sha256', 'e' * 64), ('manifest_sha256', 'e' * 64),
    ('native_identity_sha256', 'e' * 64), ('workspace_contract_version', 2),
    ('access_mode', 'shared-writer'),
])
def test_forged_or_incompatible_offer_refused(native_fixture, field, value):
    f = native_fixture
    changed = {**f.offer.mapping(), field: value}
    f.prepare(changed_offer=changed)
    with pytest.raises(ValueError):
        f.accept()
    assert not f.sent


def test_foreign_component_or_version_refused(native_fixture):
    f = native_fixture
    changed = f.offer.mapping()
    changed.update(component_id='collectigen.train', software_version='0.2.0-alpha.1',
        package_root=str(f.admitted.layout.code_root / 'Apps/collectigen.train/0.2.0-alpha.1'))
    f.prepare(changed_offer=changed)
    with pytest.raises(ValueError, match='signed_workflow_workspace_binding_changed'):
        f.accept()


def test_offer_cannot_supply_its_own_trusted_peer_pin(native_fixture):
    f = native_fixture
    f.peer['executable_sha256'] = 'e' * 64
    changed = copy.deepcopy(f.document)
    changed['launcher'] = f.peer.copy()
    f.prepare(changed_document=changed)
    with pytest.raises(RuntimeError, match='native_entry_offer_mismatch'):
        f.accept()


def test_changed_birth_is_not_same_child(native_fixture):
    f = native_fixture
    f.local['creation_filetime'] += 1
    f.prepare()
    with pytest.raises(RuntimeError, match='native_entry_offer_mismatch'):
        f.accept()


def test_foreign_offer_frame_digest_refused(native_fixture):
    f = native_fixture
    f.prepare(frame_pin='e' * 64)
    with pytest.raises(ValueError, match='workflow_offer_binding_digest_mismatch'):
        f.accept()


@pytest.mark.parametrize('sequence,pin', [(2, None), (3, 'e' * 64)])
def test_stale_or_foreign_reply_holds_session(native_fixture, sequence, pin):
    f = native_fixture
    f.prepare()
    session = f.accept()
    f.incoming.append(framed('workflow-check-ack', {**f.scope, 'state': 'ready'}, sequence, pin or f.pin))
    with pytest.raises(ValueError, match='stale_or_foreign_channel_frame'):
        session.assert_ready()
    assert session.state == 'held'
    assert not session.lease_release_authorized


def test_peer_loss_forbids_new_work_and_retains_cleanup(native_fixture):
    f = native_fixture
    f.prepare()
    session = f.accept()
    owner = object()
    f.incoming.append(framed('workflow-check-ack', {**f.scope, 'state': 'ready'}, 3, f.pin))
    session.retain_cleanup_owner(owner, is_drained=lambda: False)
    f.api.wait = 0
    with pytest.raises(ValueError, match='original_workflow_peer_lost_or_changed'):
        session.assert_ready()
    session.begin_close()
    assert session.state == 'held'
    assert session._cleanup_owners[0][0] is owner
    assert not session.lease_release_authorized


def test_drain_report_never_releases_original_root_lease(native_fixture):
    f = native_fixture
    f.prepare()
    session = f.accept()
    done = [False]
    owner = object()
    f.incoming.append(framed('workflow-check-ack', {**f.scope, 'state': 'ready'}, 3, f.pin))
    session.retain_cleanup_owner(owner, is_drained=lambda: done[0])
    f.incoming.append(framed('workflow-closing-ack', {**f.scope, 'state': 'closing'}, 4, f.pin))
    session.begin_close()
    with pytest.raises(ValueError, match='workflow_session_not_ready'):
        session.assert_ready()
    with pytest.raises(ValueError, match='workflow_original_cleanup_pending'):
        session.report_local_drained()
    done[0] = True
    f.incoming.append(framed('workflow-drained-ack', {**f.scope, 'state': 'recorded'}, 5, f.pin))
    session.report_local_drained()
    assert session.state == 'drain-reported'
    assert session._cleanup_owners[0][0] is owner
    assert session.lease_release_authorized is False


def test_return_target_is_finite_and_not_execution(native_fixture):
    f = native_fixture
    f.prepare()
    session = f.accept()
    with pytest.raises(ValueError, match='finite_workflow_return_target_required'):
        session.request_return('run-trainer')
    f.incoming.append(framed('workflow-check-ack', {**f.scope, 'state': 'ready'}, 3, f.pin))
    f.incoming.append(framed('workflow-return-ack', {**f.scope, 'target': 'models', 'state': 'recorded'}, 4, f.pin))
    assert session.request_return('models')['target'] == 'models'
    assert session.state == 'ready'


def test_dto_or_foreign_channel_is_not_session_authority(native_fixture):
    f = native_fixture
    with pytest.raises(ValueError, match='original_child_native_channel_required'):
        ModuleEntrySession.accept(object(), f.admitted, expected_owner_generation='1' * 32,
                                 expected_attempt_id='3' * 32, expected_grant_id='4' * 32)


def test_missing_independent_trust_pin_refuses_before_package_or_workspace_io(tmp_path, monkeypatch):
    root = tmp_path / 'suite'
    package = root / 'Apps/collectigen.generate/0.1.0-alpha.1'
    executable = package / 'Generate.exe'
    selected = WorkflowConnection('collectigen.generate', package, '0.1.0-alpha.1', 'a' * 64, 'b' * 64, 'fixture')
    binding = ModuleEntryBinding(SuiteLayout(root, root / 'Data', InstallMode.PORTABLE),
        selected, b'manifest', b'signature', b'trust', '', 'c' * 64, (), executable)
    monkeypatch.setattr(sys, 'frozen', True, raising=False)
    monkeypatch.setattr(sys, 'executable', str(executable))
    monkeypatch.setattr(sys, 'argv', [str(executable)])
    with pytest.raises(ValueError, match='independently_admitted_workflow_pin_required'):
        binding.validate()
    assert not root.exists()
