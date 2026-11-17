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



@pytest.mark.parametrize('kind', ['original', 'module', 'unreviewed', 'wrapped_unreviewed', 'subclass'])
def test_training_review_exposes_prepare_only_for_reviewed_original_or_exact_module_adapter(tmp_path, monkeypatch, kind):
    monkeypatch.setenv('QT_QPA_PLATFORM', 'offscreen')
    from time import monotonic, sleep
    from PIL import Image
    from PySide6.QtWidgets import QApplication, QWidget
    from PySide6.QtTest import QTest
    from core import ImageRecord, LibraryImageIdentity, TagDatabase, TrainingCandidateSelectionService, TrainingGoal, TrainingPermissionDisposition
    from core.training_package import TrainingPackageService
    from core.training_process import ProcessInventory
    from core.training_dataset_binding import ReviewedDatasetVerifier
    from core.background_jobs import BackgroundJobManager
    from suite_core.manager.krea_training_review import KreaTrainingReviewDialog
    app = QApplication.instance() or QApplication([])
    image = tmp_path / 'synthetic.png'
    Image.new('RGB', (768, 896), (30, 60, 90)).save(image)
    record = ImageRecord(image)
    record.annotation.sdxl_tags = ['synthetic']
    record.modified = True
    record.save_sidecar(force=True)
    knowledge = TagDatabase(tmp_path / 'tags.json')
    selector = TrainingCandidateSelectionService(knowledge)
    selected = selector.plan((image,), TrainingGoal('Synthetic', kind='character', base_model_id='sha256:'+'a'*64),
        identities={image: LibraryImageIdentity('synthetic', image.name)})
    selected = selector.apply_permission_disposition(selected, TrainingPermissionDisposition('proceed', 'Generated unit fixture only.', True))
    service = TrainingPackageService(knowledge)
    parent_folder = tmp_path / 'packages'
    parent_folder.mkdir()
    preview = service.preview(selected, parent_folder / 'package')
    assert preview.ready, preview.issues
    result = service.apply(preview, preview.decision.execution())
    assert result.status == 'created', result.issues
    inventory = ProcessInventory(str(tmp_path / 'worker'), str(tmp_path / 'inventory.json'), 'a'*64, 0, 0)
    original = ReviewedDatasetVerifier(inventory, 'python.exe', tmp_path / 'review-work')
    class ForeignVerifier(ReviewedDatasetVerifier):
        pass
    verifier = (original if kind == 'original' else
        runtime.ModuleDatasetVerifier(original, object()) if kind == 'module' else
        runtime.ModuleDatasetVerifier(lambda *_a, **_k: None, object()) if kind == 'wrapped_unreviewed' else
        ForeignVerifier(inventory, 'python.exe', tmp_path / 'review-work') if kind == 'subclass' else
        lambda *_a, **_k: None)
    # Presentation only: no native owner/grant or worker execution is fabricated.
    parent = QWidget()
    jobs = BackgroundJobManager()
    dialog = KreaTrainingReviewDialog(parent_folder / 'package', tmp_path / 'models.json', parent,
        dataset_verifier=verifier, preview_jobs=jobs)
    dialog.show()
    deadline = monotonic() + 5
    try:
        while (not dialog.tabs.isEnabled() or dialog._executor.close_held) and monotonic() < deadline:
            app.processEvents()
            QTest.qWait(10)
            sleep(.002)  # Let the retained Python worker physically retire before Qt settlement.
        assert dialog.tabs.isEnabled() and not dialog._executor.close_held, (dialog.status.text(), dialog._future.done(), dialog._executor.close_held, dialog._timer.isActive(), dialog._timer.remainingTime())
        assert dialog.dataset_button.isVisible()
        assert dialog.prepare_button.isVisible() == (kind in {'original', 'module'})
        assert dialog._dataset_verifier is verifier  # Never unwrap away authenticated lifetime/cancel hooks.
    finally:
        dialog.reject()
        dialog._timer.stop()
        jobs.shutdown(wait=True)
        parent.close()
        app.processEvents()


def _nested_verifier_fixture(tmp_path, monkeypatch, reply_mode='verified'):
    """Real stock composition/framing, synthetic memory and borrowed member only."""
    import core.training_preflight as preflight
    import core.training_process as process
    import core.training_verification as verifier
    import time
    worker = tmp_path / 'admitted-worker'
    worker.mkdir()
    python = worker / 'python.exe'
    python.write_bytes(b'generated nonexecutable reviewed interpreter')
    pin = process.ProcessInput.capture(python)
    manifest = tmp_path / 'reviewed-worker-inventory.json'
    manifest.write_bytes(b'generated inventory identity')
    inventory = process.ProcessInventory(str(worker), str(manifest), sha256(manifest.read_bytes()).hexdigest(), 1, python.stat().st_size)
    service = preflight.ContainedRuntimeQualificationService(worker_python=pin, worker_inventory=inventory)
    destination = tmp_path / 'generated-owned-run'
    destination.mkdir()
    plan = SimpleNamespace(inputs=(pin,), inventories=(inventory,), fingerprint='a'*64,
                           destination=destination, environment_profile=process.LEGACY_ENVIRONMENT)
    state = SimpleNamespace(mapping=None, command=None, member_closed=False, resumed=False,
                            member_requested=False, parent_closed=False, cancel=False)
    class Mapping:
        def __init__(self, size):
            self.data, self.position = bytearray(size), 0
        def __enter__(self): return self
        def __exit__(self, *_args): return False
        def write(self, data):
            self.data[self.position:self.position + len(data)] = data
            self.position += len(data)
        def __getitem__(self, key): return self.data[key]
        def __setitem__(self, key, value): self.data[key] = value
    def memory(_fd, size, **_kwargs):
        state.mapping = Mapping(size)
        return state.mapping
    class Member:
        def spawn_suspended(self, command, _cwd, _environment):
            state.command = command
            if reply_mode == 'cancel-before-resume': state.cancel = True
        def resume(self):
            state.resumed = True
            length = int(state.command[-2])
            request = json.loads(bytes(state.mapping[:length]))
            if reply_mode == 'zero': return
            response = {key: request[key] for key in ('schema_version', 'nonce', 'phase', 'process_fingerprint')}
            response.update(request_sha256=state.command[-1], status='verified')
            if reply_mode == 'stale': response['nonce'] = '0'*32
            data = process._bytes(response)
            state.mapping[length:length+4] = len(data).to_bytes(4, 'little')
            state.mapping[length+4:length+4+len(data)] = data
        def poll(self): return 0
        def close(self): state.member_closed = True
    member = Member()
    class Owner:
        def member(self):
            state.member_requested = True
            return member
        def active(self): return 0
        def close(self): state.parent_closed = True
    monkeypatch.setattr(verifier.mmap, 'mmap', memory)
    return SimpleNamespace(service=service, plan=plan, owner=Owner(), state=state,
                           pin=pin, inventory=inventory, process=process, verifier=verifier,
                           deadline=time.monotonic()+20)


@pytest.mark.parametrize('gui', ('Train.exe', 'Generate.exe'))
@pytest.mark.parametrize('phase', ('before-create', 'before-resume', 'after-exit'))
def test_frozen_nested_verifier_uses_original_reviewed_worker_python(tmp_path, monkeypatch, gui, phase):
    f = _nested_verifier_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(f.verifier.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(f.verifier.sys, 'executable', str(tmp_path / gui))
    f.service.process._verify_owned_inputs(f.plan, f.owner, phase, lambda: False, f.deadline)
    command = f.state.command
    assert command[:5] == (f.pin.path, '-I', '-S', '-B', str(f.verifier.WORKER))
    assert command[0] != f.verifier.sys.executable
    assert json.loads((f.plan.destination / ('verification-' + phase + '.json')).read_bytes())['phase'] == phase
    assert f.state.resumed and f.state.member_closed and not f.state.parent_closed


@pytest.mark.parametrize('fault', ('absent', 'inventory-absent', 'python-pin', 'inventory-pin', 'target-executable', 'inventory-root'))
def test_frozen_nested_verifier_refuses_unreviewed_runtime_before_spawn(tmp_path, monkeypatch, fault):
    f = _nested_verifier_fixture(tmp_path, monkeypatch)
    monkeypatch.setattr(f.verifier.sys, 'frozen', True, raising=False)
    monkeypatch.setattr(f.verifier.sys, 'executable', str(tmp_path / 'Train.exe'))
    if fault == 'absent':
        f.service.process = f.process.TrainingProcessService()
    elif fault == 'inventory-absent':
        f.service.process._verifier_inventory = None
    elif fault == 'python-pin':
        f.plan.inputs = (replace(f.pin, sha256='b'*64),)
    elif fault == 'inventory-pin':
        f.plan.inventories = (replace(f.inventory, manifest_sha256='b'*64),)
    elif fault == 'target-executable':
        f.service.process._verifier_python = replace(f.pin, path=str(tmp_path / 'trainer.exe'))
        f.plan.inputs = (f.service.process._verifier_python,)
    else:
        changed = replace(f.inventory, root=str(tmp_path / 'different-runtime'))
        f.service.process._verifier_inventory = changed
        f.plan.inventories = (changed,)
    with pytest.raises(f.process.TrainingProcessError, match='input.verification|input-verification|input-verifier'):
        f.service.process._verify_owned_inputs(f.plan, f.owner, 'before-create', lambda: False, f.deadline)
    assert not f.state.member_requested and f.state.command is None and f.state.mapping is None


@pytest.mark.parametrize('reply', ('zero', 'stale', 'cancel-before-resume'))
def test_nested_verifier_failure_closes_only_borrowed_member(tmp_path, monkeypatch, reply):
    f = _nested_verifier_fixture(tmp_path, monkeypatch, reply)
    monkeypatch.setattr(f.verifier.sys, 'frozen', True, raising=False)
    expected = InterruptedError if reply == 'cancel-before-resume' else f.process.TrainingProcessError
    with pytest.raises(expected):
        f.service.process._verify_owned_inputs(f.plan, f.owner, 'before-create', lambda: f.state.cancel, f.deadline)
    assert f.state.member_closed and not f.state.parent_closed
    assert not (f.plan.destination / 'verification-before-create.json').exists()
    if reply == 'cancel-before-resume': assert not f.state.resumed


def test_nested_verifier_source_default_and_expired_deadline_preserve_contract(tmp_path, monkeypatch):
    f = _nested_verifier_fixture(tmp_path, monkeypatch)
    monkeypatch.delattr(f.verifier.sys, 'frozen', raising=False)
    source_python = str(tmp_path / 'source-python.exe')
    monkeypatch.setattr(f.verifier.sys, 'executable', source_python)
    f.service.process = f.process.TrainingProcessService()
    f.service.process._verify_owned_inputs(f.plan, f.owner, 'before-create', lambda: False, f.deadline)
    assert f.state.command[0] == source_python
    f.state.member_requested = False
    with pytest.raises(f.process.TrainingProcessError, match='deadline expired'):
        f.service.process._verify_owned_inputs(f.plan, f.owner, 'after-exit', lambda: False, 0)
    assert not f.state.member_requested
