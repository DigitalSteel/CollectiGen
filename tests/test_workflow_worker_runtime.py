"""Synthetic worker evidence/adapter tests; no native worker or signing key runs."""
from dataclasses import replace
from hashlib import sha256
import json
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import manager_worker_evidence as evidence
from core.manager_update_host import CompiledManagerUpdateProfile
from core.workflow_connection import WorkflowConnection
from suite_core.installation import InstallMode, SuiteLayout
from suite_core.manager import entry_binding as manager_binding
from suite_core.manager.bootstrap_entry import NormalEntrySession, ProcessBinding
from suite_core.workflow_modules.entry_binding import ModuleEntryBinding
from suite_core.workflow_modules.session import ModuleEntrySession
from suite_core.workflow_modules import worker_runtime as runtime


def raw(value):
    return json.dumps(value, sort_keys=True).encode()


@pytest.fixture
def baseline(tmp_path, monkeypatch):
    root = tmp_path / 'suite'
    home = root / 'Recovery/worker-evidence'
    home.mkdir(parents=True)
    trust = b'synthetic public trust evidence'
    archive = b'synthetic archive bytes; verifier is explicitly stubbed'
    proof = raw({'archive_size_bytes': len(archive)})
    contents = {'archive': archive, 'manifest': b'synthetic manifest', 'signature': b'synthetic signature',
                'trust': trust, 'proof': proof, 'inventory': proof, 'executable': b'synthetic Manager executable'}
    files = {}
    for name, content in contents.items():
        path = home / (name + ('-full.nupkg' if name == 'archive' else '.bin'))
        path.write_bytes(content)
        files[name] = dict(path=path.relative_to(root).as_posix(), size=len(content), sha256=sha256(content).hexdigest())
    reference = dict(version='0.8.0-alpha.1', channel='beta',
        platform={'os': 'windows', 'architecture': 'x86_64'}, files=files)
    identity = ProcessBinding(10, 100, files['executable']['sha256'])
    calls = []
    def admit(archive_path, **kwargs):
        calls.append((archive_path, kwargs))
        kwargs['should_cancel']()
        return SimpleNamespace(artifact_sha256=files['archive']['sha256'])
    monkeypatch.setattr(evidence, 'admit_installed_detached', admit)
    args = dict(root=root, reference=reference, expected_version=reference['version'], expected_channel='beta',
                trust_bytes=trust, trust_sha256=sha256(trust).hexdigest(), manager_identity=identity, check=lambda: None)
    return SimpleNamespace(root=root, home=home, reference=reference, args=args, calls=calls, proof=proof)


def test_shared_verifier_reuses_exact_signed_archive_readmission(baseline):
    f = baseline
    plan, proof = evidence.verify_retained_manager_worker(**f.args)
    assert proof == f.proof
    assert len(f.calls) == 1
    kwargs = f.calls[0][1]
    assert kwargs['installed_executable_sha256'] == f.args['manager_identity'].executable_sha256
    assert kwargs['expected_trust_sha256'] == f.args['trust_sha256']
    assert kwargs['inventory_profile'] == 'manager-windows-full-nupkg-detached-v1'
    assert plan.artifact_sha256 == f.reference['files']['archive']['sha256']


def test_absent_baseline_does_not_discover_or_readmit(baseline):
    f = baseline
    assert evidence.verify_retained_manager_worker(**{**f.args, 'reference': None}) is None
    assert not f.calls


@pytest.mark.parametrize('mutation', ['peer', 'path', 'proof', 'trust'])
def test_changed_worker_reference_or_authority_refuses_before_readmission(baseline, mutation):
    f = baseline
    args = dict(f.args)
    reference = json.loads(json.dumps(f.reference))
    args['reference'] = reference
    if mutation == 'peer':
        args['manager_identity'] = ProcessBinding(10, 100, 'e' * 64)
    elif mutation == 'path':
        reference['files']['manifest']['path'] = 'Recovery/../../foreign.json'
    elif mutation == 'proof':
        reference['files']['proof']['sha256'] = 'e' * 64
    else:
        args['trust_bytes'] = b'changed trust'
    with pytest.raises(ValueError):
        evidence.verify_retained_manager_worker(**args)
    assert not f.calls


def test_manager_exact_normal_offered_gate_survives_extraction(monkeypatch):
    profile = CompiledManagerUpdateProfile.__new__(CompiledManagerUpdateProfile)
    monkeypatch.setattr(CompiledManagerUpdateProfile, 'check', lambda self: None)
    admitted = object()
    wrong = ModuleEntrySession.__new__(ModuleEntrySession)
    wrong.state = 'offered'
    with pytest.raises(ValueError, match='original_training_proof_session_required'):
        profile.training_proof_for_retained_baseline(session=wrong, admitted=admitted)
    normal = NormalEntrySession.__new__(NormalEntrySession)
    normal.state, normal.admitted = 'ready', admitted
    with pytest.raises(ValueError, match='original_training_proof_session_required'):
        profile.training_proof_for_retained_baseline(session=normal, admitted=admitted)


@pytest.fixture
def module_session(tmp_path, monkeypatch):
    layout = SuiteLayout(tmp_path / 'suite', tmp_path / 'suite/Data', InstallMode.PORTABLE)
    selected = WorkflowConnection('collectigen.generate', layout.code_root / 'Apps/collectigen.generate/0.1.0-alpha.1',
                                  '0.1.0-alpha.1', 'a' * 64, 'b' * 64, 'synthetic')
    admitted = ModuleEntryBinding(layout, selected, b'm', b's', b'trust', sha256(b'trust').hexdigest(),
                                 'c' * 64, (), selected.directory / 'Generate.exe')
    session = ModuleEntrySession.__new__(ModuleEntrySession)
    session.admitted_binding, session.state = admitted, 'ready'
    session.wire = SimpleNamespace(document={'launcher': {'pid': 10, 'creation_filetime': 100, 'executable_sha256': 'c' * 64}})
    session.worker_baseline = {'version': '0.8.0-alpha.1', 'channel': 'beta'}
    retained, checks = [], []
    def ready(self):
        checks.append(self.state)
        if self.state != 'ready':
            raise ValueError('synthetic peer/grant no longer ready')
        return self
    monkeypatch.setattr(ModuleEntrySession, 'assert_ready', ready)
    monkeypatch.setattr(ModuleEntrySession, 'retain_cleanup_owner', lambda self, owner, is_drained: retained.append((owner, is_drained)))
    return SimpleNamespace(session=session, retained=retained, checks=checks, layout=layout)


def test_module_without_delegated_baseline_remains_unavailable(module_session, monkeypatch):
    f = module_session
    f.session.worker_baseline = None
    monkeypatch.setattr(runtime, 'verify_retained_manager_worker', lambda **kwargs: pytest.fail('No baseline inferred'))
    assert runtime.bind_worker_capabilities(f.session) is None
    assert not f.retained


def test_module_reuses_worker_and_retains_original_runtime_owners(module_session, monkeypatch):
    f = module_session
    proof = b'proof'
    plan = SimpleNamespace(training_worker_file_rows=lambda value: {'python.exe': {'sha256': 'd' * 64}})
    def verify(**kwargs):
        assert kwargs['reference'] is f.session.worker_baseline
        assert kwargs['manager_identity'].executable_sha256 == 'c' * 64
        kwargs['check']()
        return plan, proof
    monkeypatch.setattr(runtime, 'verify_retained_manager_worker', verify)
    source = f.layout.code_root / 'Apps/Manager/current/_internal'
    monkeypatch.setattr(runtime, '_signed_source_version', lambda *args: (source, '3.13.7'))
    original = SimpleNamespace(inventory=object(), python_relative='python.exe', work_root=f.layout.data_root / 'Manager/TrainingReview')
    def provision(admitted, signed_worker, *, prepare_workspace):
        assert admitted.layout is f.layout and admitted.manager_arguments == () and prepare_workspace
        assert f.retained  # Lifetime retained before provisioning writes.
        return original
    monkeypatch.setattr(runtime, '_bind_training_worker', provision)
    caption = SimpleNamespace(close_held=False)
    def caption_bind(adapter, verifier, signed, **kwargs):
        assert type(adapter) is runtime.ModuleWorkerSessionAdapter
        assert not isinstance(adapter, NormalEntrySession)
        assert verifier is original and kwargs['source_root'] == source
        assert kwargs['worker_python_version'] == '3.13.7'
        kwargs['admission_check']()
        return caption
    monkeypatch.setattr(runtime, '_bind_manager_caption_runtime', caption_bind)
    callbacks = tuple(lambda: None for _ in range(4))
    monkeypatch.setattr(runtime, '_bind_manager_video_runtime', lambda *args, **kwargs: callbacks)
    result = runtime.bind_worker_capabilities(f.session)
    assert result.original is original
    assert f.session.caption_runtime is caption
    assert f.session.admit_manager_video_runtime is callbacks[0]
    owner = f.session.worker_capabilities
    assert owner.begin_close()
    with pytest.raises(InterruptedError, match='closing'):
        owner.adapter.check()
    assert f.retained[0][0] is owner


def test_dataset_adapter_checks_actual_module_grant_during_original_operation(module_session):
    f = module_session
    owner = runtime.ModuleWorkerCapabilities(f.session)
    class Original:
        def __call__(self, package, *, should_cancel):
            assert owner.dataset_calls == 1
            f.session.state = 'held'
            should_cancel()
    wrapper = runtime.ModuleDatasetVerifier(Original(), owner)
    with pytest.raises(ValueError, match='no longer ready'):
        wrapper(object(), should_cancel=lambda: False)
    assert owner.dataset_calls == 0


def test_signed_manager_config_root_version_and_local_video_copy_are_checked(tmp_path, monkeypatch):
    from core import paths
    layout = SuiteLayout(tmp_path / 'suite', tmp_path / 'suite/Data', InstallMode.PORTABLE)
    source = layout.code_root / 'Apps/Manager/current/_internal'
    local = tmp_path / 'module-source'
    values = {'tools/runtime-pins.json': raw({'python': {'version': '3.13.7'}}),
              'tools/optional-ai-profiles.json': raw({'target': {'python': '3.13.7'}})}
    rows = []
    for relative, content in values.items():
        for base in (source, local):
            path = base / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(content)
        rows.append({'path': '_internal/' + relative, 'size_bytes': len(content), 'sha256': sha256(content).hexdigest()})
    proof = raw({'files': rows})
    signed = (SimpleNamespace(inventory_sha256=sha256(proof).hexdigest()), proof)
    admitted = SimpleNamespace(layout=layout)
    monkeypatch.setattr(paths, 'PROGRAM_ROOT', local)
    assert manager_binding._checked_explicit_worker_configs(admitted, signed, source, '3.13.7', local_video_copy=True)[0] == source
    (local / 'tools/runtime-pins.json').write_bytes(b'changed')
    with pytest.raises(ValueError, match='module_video_config_differs'):
        manager_binding._checked_explicit_worker_configs(admitted, signed, source, '3.13.7', local_video_copy=True)
    with pytest.raises(ValueError, match='signed_manager_worker_source_required'):
        manager_binding._checked_explicit_worker_configs(admitted, signed, local, '3.13.7')
