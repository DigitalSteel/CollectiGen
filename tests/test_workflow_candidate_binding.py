"""Synthetic pending-offer/refusal checks; no software or native execution."""
import base64
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from urllib.request import Request

import pytest

from core.component_acquisition import ExactComponentOffer, ExactOfferAsset
from core.workflow_candidate_admission import CandidateWorkflowAdmission
from core.manager_update_host import SealedLocalQualificationOpener
from suite_core.discovery.github_component_releases import REPOSITORY
from core.workflow_connection import workflow_component
from suite_core.manager import compiled_workflow_config as config
from suite_core.manager import workflow_acquisition_binding as binding
from suite_core.workflow_modules.compiled_candidate import CompiledWorkflowCandidateAdmission
from suite_core.tests.test_workflow_entry_session import native_fixture, framed


def raw(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':')).encode()


@pytest.fixture
def profile(tmp_path):
    # Public synthetic modulus only; no signing key or signature-verifier bypass.
    from suite_core.discovery.component_release_signature import SIGNATURE_ALGORITHM
    trust = raw({'schema_version': 1, 'keys': [{'key_id': 'synthetic',
        'algorithm': SIGNATURE_ALGORITHM, 'exponent': 65537,
        'modulus': base64.urlsafe_b64encode(b'\x80' + b'\x01' * 255).decode().rstrip('=')}]})
    root = tmp_path / 'candidate'
    doc = raw({'schema': 1, 'purpose': 'workflow-candidate-evaluation',
        'candidate_id': 'a' * 32, 'status': 'pending', 'suite_root': str(root),
        'data_root': str(root / 'Data'), 'components': {'collectigen.generate': '0.1.0-alpha.1'},
        'manager_version': '0.8.0-alpha.1', 'bootstrap_version': '0.1.1',
        'workspace_contract_version': 1, 'trust_sha256': sha256(trust).hexdigest()})
    admission = CandidateWorkflowAdmission(doc, sha256(doc).hexdigest(), trust, sha256(trust).hexdigest())
    component = 'collectigen.generate'; version = admission.version_for(component)
    tag = component + '/v' + version
    def asset(name, identity):
        return ExactOfferAsset(name, 10, str(identity) * 64,
            f'https://github.com/{REPOSITORY}/releases/download/{component}%2Fv{version}/{name}', identity)
    offer = ExactComponentOffer(workflow_component(component), version, 'beta', 'windows', 'x86_64',
        'synthetic', admission.trust_sha256, 1, 2, tag,
        asset('release.json', 1), asset('release.sig.json', 2), asset('Generate.zip', 3))
    return binding.CompiledWorkflowCandidateAcquisitionProfile(component, admission, 'f' * 64, offer)


def test_pending_profile_binds_exact_offer_without_completed_evidence(profile):
    assert profile.trust_bytes == profile.admission.trust_bytes
    assert profile.trust_sha256 == profile.admission.trust_sha256
    assert not hasattr(profile, 'transport_qualification_sha256')
    compiled = CompiledWorkflowCandidateAdmission(profile.component_id, profile.admission)
    assert compiled.validate(profile.component_id) is compiled
    assert compiled.software_version == profile.expected_offer.software_version
    for changes in ({'component_id': 'collectigen.train'}, {'workspace_contract_version': True},
                    {'admission': object()}):
        with pytest.raises(ValueError): replace(compiled, **changes).validate(profile.component_id)


@pytest.mark.parametrize('change', ['wrong_version', 'wrong_trust', 'unknown_key', 'stable', 'oversize', 'mapping', 'untyped'])
def test_candidate_profile_refuses_mismatched_offer(profile, change):
    changes = {}
    if change == 'wrong_version':
        doc = profile.admission.document(); doc['components'][profile.component_id] = '0.2.0-alpha.1'
        encoded = raw(doc)
        changes['admission'] = replace(profile.admission, document_bytes=encoded, document_sha256=sha256(encoded).hexdigest())
    elif change == 'wrong_trust': changes['expected_offer'] = replace(profile.expected_offer, trust_sha256='0' * 64)
    elif change == 'unknown_key': changes['expected_offer'] = replace(profile.expected_offer, key_id='unknown')
    elif change == 'stable': changes['channel'] = 'stable'
    elif change == 'oversize': changes['max_artifact_bytes'] = 1
    elif change == 'mapping': changes['mapping_sha256'] = 'unknown'
    else: changes['admission'] = object()
    with pytest.raises(ValueError): replace(profile, **changes)


def test_no_default_or_malformed_profile_fallback(profile, monkeypatch):
    component = profile.component_id
    monkeypatch.setattr(config, 'PROFILES', {})
    monkeypatch.setattr(config, 'CANDIDATE_PROFILES', {})
    assert binding._profile(component) is None
    monkeypatch.setattr(config, 'CANDIDATE_PROFILES', {component: profile})
    assert binding._profile(component) is profile
    monkeypatch.setattr(config, 'PROFILES', {component: None})
    with pytest.raises(ValueError, match='mixed_workflow'): binding._profile(component)
    monkeypatch.setattr(config, 'CANDIDATE_PROFILES', {})
    with pytest.raises(ValueError, match='typed_compiled'): binding._profile(component)
    monkeypatch.setattr(config, 'PROFILES', {})
    monkeypatch.setattr(config, 'CANDIDATE_PROFILES', {component: None})
    with pytest.raises(ValueError, match='typed_compiled'): binding._profile(component)


def test_candidate_marker_rechecked_and_native_session_still_required(profile, monkeypatch):
    admission = profile.admission
    marker = admission.suite_root / '_bootstrap/workflow-candidate.json'
    marker.parent.mkdir(parents=True); marker.write_bytes(admission.document_bytes)
    binding._check_candidate(profile, admission.suite_root, admission.data_root)
    monkeypatch.setattr(config, 'PROFILES', {})
    monkeypatch.setattr(config, 'CANDIDATE_PROFILES', {profile.component_id: profile})
    with pytest.raises(ValueError, match='original_native_normal_session'):
        binding.factory_for_session(object(), profile.component_id)
    marker.write_bytes(b'changed')
    with pytest.raises(ValueError, match='candidate_marker_changed'):
        binding._check_candidate(profile, admission.suite_root, admission.data_root)


@pytest.mark.parametrize('component', ['collectigen.generate', 'collectigen.train'])
def test_sealed_transport_explicit_finite_components_only(tmp_path, component):
    root = tmp_path / 'sealed'; (root / '_bootstrap').mkdir(parents=True)
    payload = root / '_qualification/asset.bin'; payload.parent.mkdir(); payload.write_bytes(b'INERT ASSET')
    url = f'https://github.com/{REPOSITORY}/releases/download/{component}%2Fv0.1.0-alpha.1/candidate.zip'
    mapping = raw({'schema': 1, 'responses': {url: {'path': '_qualification/asset.bin',
        'size': 11, 'sha256': sha256(payload.read_bytes()).hexdigest()}}})
    (root / '_bootstrap/qualification-responses.json').write_bytes(mapping)
    pin = sha256(mapping).hexdigest()
    with pytest.raises(ValueError, match='unknown_qualification_url'):
        SealedLocalQualificationOpener(pin, root)(Request(url))
    opener = SealedLocalQualificationOpener(pin, root, (component,))
    with opener(Request(url)) as response: assert response.read() == b'INERT ASSET' and response.getcode() == 200
    with opener(Request(url, headers={'Range': 'bytes=6-'})) as response:
        assert response.read() == b'ASSET' and response.getcode() == 206
    assert opener.bind(root, opener.transport_id).component_ids == (component,)
    for request in (Request(url.replace('candidate.zip', 'unknown.zip')), Request(url, method='POST'),
                    Request(url.replace(component, 'collectigen.reader')), Request(url + '?download=1')):
        with pytest.raises(ValueError): opener(request)
    payload.write_bytes(b'CHANGED')
    with pytest.raises(ValueError): opener(Request(url))


@pytest.mark.parametrize('components', [(), [], ('collectigen.reader',), ('collectigen.generate',) * 2, (None,)])
def test_sealed_transport_refuses_unbounded_or_foreign_components(components):
    with pytest.raises(ValueError, match='finite_qualification_components'):
        SealedLocalQualificationOpener(component_ids=components)


def test_exact_offer_repository_identity_is_mapped_only_for_workflows(tmp_path):
    root = tmp_path / 'sealed'; (root / '_bootstrap').mkdir(parents=True)
    repository = root / '_qualification/repository.json'; repository.parent.mkdir()
    contents = raw({'id': 1301951104, 'full_name': REPOSITORY}); repository.write_bytes(contents)
    url = f'https://api.github.com/repos/{REPOSITORY}'
    mapping = raw({'schema': 1, 'responses': {url: {'path': '_qualification/repository.json',
        'size': len(contents), 'sha256': sha256(contents).hexdigest()}}})
    (root / '_bootstrap/qualification-responses.json').write_bytes(mapping)
    pin = sha256(mapping).hexdigest()
    with pytest.raises(ValueError, match='unknown_qualification_url'):
        SealedLocalQualificationOpener(pin, root)(Request(url))
    for component in ('collectigen.generate', 'collectigen.train'):
        with SealedLocalQualificationOpener(pin, root, (component,))(Request(url)) as response:
            assert response.read() == contents
        with pytest.raises(ValueError, match='unknown_qualification_url'):
            SealedLocalQualificationOpener(pin, root, (component,))(Request(url + '/contents'))


def test_mixed_entry_policy_refused_before_read_or_channel(profile, monkeypatch):
    from suite_core.workflow_modules import entry, compiled_admission, compiled_candidate
    monkeypatch.setattr(compiled_admission, 'PROFILE', object())
    monkeypatch.setattr(compiled_candidate, 'PROFILE',
        CompiledWorkflowCandidateAdmission(profile.component_id, profile.admission))
    monkeypatch.setattr(entry, 'entry_locations', lambda *args: pytest.fail('No root read for mixed policy'))
    with pytest.raises(ValueError, match='mixed_workflow_admission'):
        entry.entry_session(profile.component_id)
    monkeypatch.setattr(compiled_candidate, 'PROFILE', None)
    with pytest.raises(ValueError, match='compiled_workflow_admission_unavailable'):
        entry.entry_session(profile.component_id)


def test_marker_drift_holds_new_work_but_preserves_normal_close(profile, native_fixture):
    f = native_fixture
    doc = profile.admission.document()
    doc.update(suite_root=str(f.offer.code_root), data_root=str(f.offer.data_root))
    encoded = raw(doc)
    admission = replace(profile.admission, document_bytes=encoded, document_sha256=sha256(encoded).hexdigest())
    marker = admission.suite_root / '_bootstrap/workflow-candidate.json'
    marker.parent.mkdir(parents=True); marker.write_bytes(encoded)
    f.prepare(); session = f.accept(); session.candidate_admission = admission
    f.incoming.append(framed('workflow-check-ack', {**f.scope, 'state': 'ready'}, 3, f.pin))
    assert session.assert_ready() is session
    marker.write_bytes(b'changed')
    with pytest.raises(ValueError, match='candidate_marker_changed'): session.assert_ready()
    assert session.state == 'ready'
    f.incoming.append(framed('workflow-closing-ack', {**f.scope, 'state': 'closing'}, 4, f.pin))
    session.begin_close()
    f.incoming.append(framed('workflow-drained-ack', {**f.scope, 'state': 'recorded'}, 5, f.pin))
    session.report_local_drained()
    assert session.state == 'drain-reported' and not session.lease_release_authorized


def test_pending_entry_still_requires_signed_module_proof(profile, monkeypatch):
    from types import SimpleNamespace
    from suite_core.workflow_modules import entry, compiled_admission, compiled_candidate
    admission = profile.admission
    marker = admission.suite_root / '_bootstrap/workflow-candidate.json'
    marker.parent.mkdir(parents=True); marker.write_bytes(admission.document_bytes)
    monkeypatch.setattr(compiled_admission, 'PROFILE', None)
    monkeypatch.setattr(compiled_candidate, 'PROFILE',
        CompiledWorkflowCandidateAdmission(profile.component_id, admission))
    monkeypatch.setattr(entry, 'entry_locations', lambda *args: SimpleNamespace(
        code_root=admission.suite_root, data_root=admission.data_root))
    def module_proof(*args):
        raise ValueError('signed_module_proof_required')
    monkeypatch.setattr(entry, '_module_admission', module_proof)
    with pytest.raises(ValueError, match='signed_module_proof_required'):
        entry.entry_session(profile.component_id)
    marker.write_bytes(b'changed')
    with pytest.raises(ValueError, match='candidate_marker_changed'):
        entry.entry_session(profile.component_id)


def test_factory_passes_exact_offer_to_stock_acquisition_and_checks_stage(profile, monkeypatch):
    """Composition doubles only; original native/session classes remain production gates."""
    from types import SimpleNamespace
    from contextlib import contextmanager
    import core.component_acquisition as acquisition_module
    import core.workflow_installation as installation_module
    import core.workflow_native_owner as native_module
    from suite_core.manager import bootstrap_entry, resource_executor
    admission = profile.admission
    marker = admission.suite_root / '_bootstrap/workflow-candidate.json'
    marker.parent.mkdir(parents=True); marker.write_bytes(admission.document_bytes)
    class Session: pass
    class Workspace:
        def assert_idle(self): pass
    session = Session(); session.state = 'ready'; session.workflow_contract_version = 1
    settings = admission.data_root / 'Manager/settings.ini'
    session.admitted = SimpleNamespace(settings_path=settings,
        layout=SimpleNamespace(code_root=admission.suite_root, data_root=admission.data_root))
    workspace = Workspace(); workspace.session = session
    captured = {}
    class Stage:
        def __init__(self, *args, assert_workspace_idle): captured['idle'] = assert_workspace_idle
        @contextmanager
        def hold(self, *args): yield
    def acquisition(**kwargs):
        captured['acquisition'] = kwargs
        return SimpleNamespace()
    monkeypatch.setattr(bootstrap_entry, 'NormalEntrySession', Session)
    monkeypatch.setattr(native_module, 'WorkflowWorkspaceOwner', Workspace)
    monkeypatch.setattr(binding, 'WorkflowStageOwner', Stage)
    monkeypatch.setattr(acquisition_module, 'ComponentAcquisition', acquisition)
    monkeypatch.setattr(installation_module, 'WorkflowInstallation', lambda *args, **kwargs: SimpleNamespace())
    monkeypatch.setattr(resource_executor, 'ResourceExecutorOwner', lambda: object())
    monkeypatch.setattr(config, 'PROFILES', {})
    monkeypatch.setattr(config, 'CANDIDATE_PROFILES', {profile.component_id: profile})
    create = binding.factory_for_session(session, profile.component_id)
    with pytest.raises(ValueError, match='original_workflow_settings'):
        create(None, settings, workspace_owner=workspace)
    service, _executor = create(None, settings.with_suffix('.json'), workspace_owner=workspace)
    assert captured['acquisition']['expected_offer'] is profile.expected_offer
    assert captured['acquisition']['opener'].component_ids == (profile.component_id,)
    assert captured['acquisition']['opener'].root == admission.suite_root
    assert service.candidate_admission is admission
    captured['idle']()
    marker.write_bytes(b'changed')
    with pytest.raises(ValueError, match='candidate_marker_changed'): captured['idle']()
