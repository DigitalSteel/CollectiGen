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
    from PySide6.QtCore import Qt
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
        QTest.mouseClick(dialog.details_button, Qt.LeftButton)
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


@pytest.mark.parametrize('cancelled', [False, True])
def test_module_preparation_retains_canonical_profile_and_original_lifetime(tmp_path, monkeypatch, cancelled):
    """Real module/reviewed composition; fake terminal preflight, no native process."""
    from threading import RLock
    from PIL import Image
    from core import ImageRecord, LibraryImageIdentity, TagDatabase, TrainingCandidateSelectionService, TrainingGoal, TrainingPermissionDisposition
    from core.training_package import TrainingPackageService
    from core.training_process import ProcessInventory
    from core import training_dataset_binding as binding
    from core import training_dataset_preparation as selected
    from core.training_preflight import ContainedRuntimeQualificationService
    image = tmp_path / 'generated.png'
    Image.new('RGB', (512, 768), (30, 60, 90)).save(image)
    record = ImageRecord(image)
    record.annotation.sdxl_tags = ['synthetic']
    record.modified = True
    record.save_sidecar(force=True)
    knowledge = TagDatabase(tmp_path / 'tags.json')
    selector = TrainingCandidateSelectionService(knowledge)
    plan = selector.plan((image,), TrainingGoal('Synthetic', kind='character', base_model_id='sha256:'+'a'*64),
        identities={image: LibraryImageIdentity('synthetic', image.name)})
    plan = selector.apply_permission_disposition(plan, TrainingPermissionDisposition('proceed', 'Generated fixture only.', True))
    packages = tmp_path / 'packages'
    packages.mkdir()
    service = TrainingPackageService(knowledge)
    preview = service.preview(plan, packages / 'package')
    assert preview.ready, preview.issues
    assert service.apply(preview, preview.decision.execution()).status == 'created'
    package = service.verify(packages / 'package')
    assert package.valid and not selected.assess_manifest(package.manifest)['holds']
    work = tmp_path / 'reviewed-work'
    work.mkdir()
    inventory = ProcessInventory(str(tmp_path / 'worker'), str(tmp_path / 'inventory.json'), 'a'*64, 0, 0)
    Path(inventory.root).mkdir()
    original = binding.ReviewedDatasetVerifier(inventory, 'python.exe', work)
    owner = SimpleNamespace(lock=RLock(), dataset_calls=0, dataset_jobs=[], adapter=SimpleNamespace(check=lambda: None))
    wrapped = runtime.ModuleDatasetVerifier(original, owner)
    state = SimpleNamespace(calls=0, memory=None)
    canonical_profile = binding.ContainedDatasetPreparationService.prepare_package.__kwdefaults__['profile']
    assert canonical_profile != ContainedRuntimeQualificationService.preflight.__kwdefaults__['profile']
    class Memory:
        def __init__(self, selection):
            selected.validate_selection(selection)
            self.descriptor = {'mapping': 'Local\\CollectiGen.Dataset.'+'b'*32, 'length': 1, 'sha256': 'c'*64}
            self.closed = False
            state.memory = self
        def close(self): self.closed = True
    result = (None, None, None, {'nonce': 'd'*32})
    jobs = object()
    roots = [tmp_path / name for name in ('source', 'environment', 'base-python')]
    def preflight(source, environment, root, actual_jobs, **kwargs):
        state.calls += 1
        assert (source, environment, actual_jobs) == (*roots[:2], jobs)
        assert root.is_relative_to(work) and kwargs['python_base'] == roots[2]
        assert kwargs['profile'] == canonical_profile and kwargs['prepare'] is True
        assert kwargs['selected_dataset']['memory'] is state.memory.descriptor
        assert owner.dataset_calls == 1 and owner.dataset_jobs == [jobs]
        if kwargs['should_cancel'](): raise InterruptedError('Generated cancellation')
        return result
    def factory(actual_inventory, python_relative, **kwargs):
        assert actual_inventory is inventory and python_relative == 'python.exe'
        return SimpleNamespace(preflight=preflight)
    monkeypatch.setattr(binding.selected, 'SelectionMemory', Memory)
    monkeypatch.setattr(binding.ContainedRuntimeQualificationService, 'from_reviewed_worker_inventory', factory)
    args = dict(source=roots[0], environment=roots[1], python_base=roots[2], jobs=jobs, should_cancel=lambda: cancelled)
    before = (package.destination / 'collectigen-training-manifest.json').read_bytes()
    if cancelled:
        with pytest.raises(InterruptedError, match='Generated cancellation'): wrapped.prepare(package, **args)
    else:
        assert wrapped.prepare(package, **args) is result
    assert state.calls == 1 and state.memory.closed and owner.dataset_calls == 0
    assert owner.dataset_jobs == [jobs]  # Original owner retains the job for physical drain.
    assert (package.destination / 'collectigen-training-manifest.json').read_bytes() == before


@pytest.fixture
def complete_dataset_parser(tmp_path, monkeypatch):
    """Full canonical source shape; generated bytes only, never executed."""
    import hashlib
    import json
    from dataclasses import asdict
    from PIL import Image
    from core import ImageRecord, LibraryImageIdentity, TagDatabase, TrainingCandidateSelectionService, TrainingGoal, TrainingPermissionDisposition
    from core.training_package import TrainingPackageService
    from core.training_adapter_preview import SdxlLoraRecipe
    from core import training_runtime_qualification as qualification
    from core import training_dataset_preparation as selected
    from core import training_preflight as preflight
    from core.training_file_inventory import SoftwareInventoryService
    from core.training_process import TrainingProcessService
    image = tmp_path / 'generated.png'
    Image.new('RGB', (512, 768), (30, 60, 90)).save(image)
    record = ImageRecord(image)
    record.annotation.sdxl_tags = ['synthetic']
    record.modified = True
    record.save_sidecar(force=True)
    knowledge = TagDatabase(tmp_path / 'tags.json')
    selector = TrainingCandidateSelectionService(knowledge)
    plan = selector.plan((image,), TrainingGoal('Synthetic', kind='character', base_model_id='sha256:'+'a'*64),
        identities={image: LibraryImageIdentity('synthetic', image.name)})
    plan = selector.apply_permission_disposition(plan, TrainingPermissionDisposition('proceed', 'Generated fixture only.', True))
    packages = tmp_path / 'packages'
    packages.mkdir()
    service = TrainingPackageService(knowledge)
    preview = service.preview(plan, packages / 'package')
    assert preview.ready, preview.issues
    assert service.apply(preview, preview.decision.execution()).status == 'created'
    package = service.verify(packages / 'package')
    assert package.valid and not selected.assess_manifest(package.manifest)['holds']

    source = tmp_path / 'trainer-source'
    source.mkdir()
    catalog = json.loads(qualification.CATALOG.read_bytes())
    assert len(catalog['files']) == 111
    for relative in catalog['files']:
        path = source / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        raw = ('# generated source identity: '+relative+'\n').encode()
        path.write_bytes(raw)
        catalog['files'][relative] = [hashlib.sha256(raw).hexdigest()]
    catalog_path = tmp_path / 'generated-catalog.json'
    catalog_path.write_text(json.dumps(catalog), encoding='utf-8')
    monkeypatch.setattr(qualification, 'CATALOG', catalog_path)
    environment = tmp_path / 'environment'
    (environment / 'Scripts').mkdir(parents=True)
    (environment / 'Scripts/python.exe').write_bytes(b'identity fixture; never executable')
    site = environment / 'Lib/site-packages'
    site.mkdir(parents=True)
    profile = 'kohya-uv-45088f04'
    for name, version in qualification.PROFILES[profile].items():
        metadata = site / (name.replace('-', '_')+'-'+(version or '1.0')+'.dist-info/METADATA')
        metadata.parent.mkdir()
        metadata.write_text(f'Name: {name}\nVersion: {version or "1.0"}\n', encoding='utf-8')
    base = tmp_path / 'base-python'
    base.mkdir()
    (base / 'python.exe').write_bytes(b'base identity fixture; never executable')
    software = tuple((role, SoftwareInventoryService().capture(path)) for role,path in (('environment',environment),('python_base',base)))
    service = qualification.TrainingRuntimeQualificationService()
    inspection = service.inspect(source, environment, profile=profile)
    assert inspection.probe_ready, inspection.blockers
    selection = {'root': str(Path(package.destination).resolve()), 'package_fingerprint': package.package_fingerprint,
        'manifest_sha256': selected.digest(package.manifest), 'recipe': asdict(SdxlLoraRecipe())}
    memory = selected.SelectionMemory(selection)
    chosen = {'memory': memory.descriptor, 'binding': selected.binding(selection), 'summary': selected.assess_manifest(package.manifest)}
    helper = SimpleNamespace(destination=tmp_path / 'helper-run')
    (helper.destination / 'work/prepared').mkdir(parents=True)
    monkeypatch.setattr(TrainingProcessService, 'execute', lambda *a,**k: pytest.fail('No native process is authorized by this component test'))
    try:
        plan, parser = service.prepare_parser(inspection, helper.destination / 'work/prepared', software=software, selected_dataset=chosen)
        value = json.loads(json.dumps({'inspection':asdict(inspection), 'plan':asdict(plan), 'request':parser}))
        request = dict(source=str(source), environment=str(environment), profile=profile, python_base=str(base),
            catalog=str(catalog_path), catalog_sha256=hashlib.sha256(catalog_path.read_bytes()).hexdigest(),
            operation='prepare', selected_dataset=chosen)
        yield SimpleNamespace(qualification=qualification, preflight=preflight, inspection=inspection,
            plan=plan, parser=parser, value=value, request=request, helper=helper, source=Path(parser['source']), package=package)
    finally:
        memory.close()


def test_complete_dataset_source_fits_unchanged_bound_and_live_decoder(complete_dataset_parser):
    f = complete_dataset_parser
    assert len(f.inspection.source_files) == 111
    assert len(f.plan.inputs) == 19
    assert len(f.plan.inventories) == 4
    assert Path(f.plan.inventories[2].root).name == 'dataset-package'
    assert f.plan.inventories[3].root == str(f.source)
    assert not any(Path(item.path).is_relative_to(f.source) for item in f.plan.inputs)
    assert f.preflight.decode_result(f.value, f.request, f.helper) == (f.inspection, f.plan, f.parser)


@pytest.mark.parametrize('change', ['missing', 'extra', 'changed', 'count', 'roles', 'fingerprint', 'manifest', 'file-count', 'byte-count', 'source-root'])
def test_complete_dataset_source_and_inventory_tampering_refused(complete_dataset_parser, change):
    f = complete_dataset_parser
    if change in {'missing','extra','changed'}:
        path = f.source / f.inspection.source_files[0]
        if change == 'missing': path.unlink()
        elif change == 'extra': (f.source / 'unselected.py').write_bytes(b'not selected')
        else: path.write_bytes(b'changed')
    elif change == 'count': f.value['plan']['inventories'].pop()
    elif change == 'roles': f.value['plan']['inventories'][2:4] = reversed(f.value['plan']['inventories'][2:4])
    elif change == 'fingerprint': f.value['plan']['inventories'][3]['manifest_sha256'] = '0'*64
    elif change == 'file-count': f.value['plan']['inventories'][3]['file_count'] -= 1
    elif change == 'byte-count': f.value['plan']['inventories'][3]['byte_count'] += 1
    elif change == 'source-root': f.value['plan']['inventories'][3]['root'] = str(f.source.parent)
    else: Path(f.plan.inventories[3].manifest_path).write_bytes(b'changed')
    with pytest.raises((ValueError, f.qualification.TrainingProcessError)):
        f.preflight.decode_result(f.value, f.request, f.helper)


def test_historical_preparation_review_does_not_redecode_old_parser_inputs(tmp_path, monkeypatch):
    from core import training_preflight as preflight
    from core.training_process import TrainingRunJournal, _bytes, _sha
    from core.training_run_index import ScopedPreparationHistory
    nonce = 'e'*32
    root = tmp_path / 'history'
    directory = root / ('preflight-'+nonce)
    (directory / 'events').mkdir(parents=True)
    receipt = dict(schema_version=1, nonce=nonce, operation='prepare', status='complete',
        process_fingerprint='a'*64, request_sha256='b'*64, result_sha256='c'*64)
    raw = _bytes(receipt)
    (directory / 'preflight-result.json').write_bytes(raw)
    journal = TrainingRunJournal(directory)
    tip = journal.append('reserved', {'plan':{'process_fingerprint':receipt['process_fingerprint']}}, '')
    tip = journal.append('starting', {}, tip)
    tip = journal.append('running', {}, tip)
    tip = journal.append('exited_unverified', dict(exit_code=0, tree_stopped=True, preflight_receipt_sha256=_sha(raw)), tip)
    monkeypatch.setattr(preflight, 'decode_result', lambda *a,**k: pytest.fail('Historical metadata must not decode a live parser plan'))
    monkeypatch.setattr(preflight.runtime, 'verify_selected_tokenizer_source_inventory', lambda *a,**k: pytest.fail('Historical metadata must not revalidate old source inventories'))
    history = ScopedPreparationHistory(root)
    history.retain(directory, receipt_sha256=_sha(raw), journal_tip=tip)
    records, revision = history.snapshot()
    assert history.review_preparation(records[0], revision) == receipt
    assert (directory / 'preflight-result.json').read_bytes() == raw
    assert journal.read()[1] == tip


@pytest.mark.parametrize('schema', [5, 11, 12, 13, 15])
def test_source_inventory_preserves_tokenizer_and_dataset_schema_roles(complete_dataset_parser, schema):
    f = complete_dataset_parser
    plan = f.plan if schema in {11,12,13} else replace(f.plan, inventories=(*f.plan.inventories[:2], f.plan.inventories[3]))
    f.qualification.verify_selected_tokenizer_source_inventory(f.inspection, plan, {**f.parser, 'schema_version':schema})
    with pytest.raises(f.qualification.TrainingProcessError, match='inventory_required'):
        f.qualification.verify_selected_tokenizer_source_inventory(f.inspection, replace(plan, inventories=plan.inventories[:-1]), {**f.parser, 'schema_version':schema})


@pytest.fixture
def admitted_preparation_owners(tmp_path):
    """Real stock worker provisioning with generated, already-admitted file rows."""
    from suite_core.discovery.manager_runtime_detached_admission import DetachedAdmissionPlan
    code = tmp_path / 'installed-code'
    data = tmp_path / 'owned-data'
    worker = code / 'training-worker/python'
    worker.mkdir(parents=True)
    raw = b'generated worker identity; never executable'
    (worker / 'python.exe').write_bytes(raw)
    stable = data / 'Manager/TrainingReview'
    stable.mkdir(parents=True)
    rows = {'python.exe': {'bytes':len(raw), 'sha256':sha256(raw).hexdigest()}}
    owner = SimpleNamespace(training_worker_file_rows=lambda proof: rows if proof == b'already-admitted-fixture' else None)
    first = DetachedAdmissionPlan.provision_training_worker(owner, b'already-admitted-fixture', code_root=code, data_root=data)
    second = DetachedAdmissionPlan.provision_training_worker(owner, b'already-admitted-fixture', code_root=code, data_root=data)
    assert first.work_root != second.work_root
    assert first.work_root.parent == second.work_root.parent == stable
    assert Path(first.inventory.manifest_path).parent == first.work_root
    assert Path(second.inventory.manifest_path).parent == second.work_root
    return SimpleNamespace(first=first, second=second, stable=stable)


@pytest.mark.parametrize('cancelled', [False, True])
@pytest.mark.parametrize('stable_history', [False, True])
def test_module_preparation_history_survives_owner_reopen_and_legacy_scope(complete_dataset_parser, admitted_preparation_owners, monkeypatch, cancelled, stable_history):
    from threading import RLock
    from core import training_dataset_binding as binding
    from core import training_dataset_preparation as selected
    from core.training_preflight import ContainedRuntimeQualificationService
    from core.training_process import TrainingRunJournal, _bytes, _sha
    from core.training_run_index import ScopedPreparationHistory
    f, owners = complete_dataset_parser, admitted_preparation_owners
    assert owners.first.history_root == owners.second.history_root == owners.stable
    original = (owners.first if stable_history else
        binding.ReviewedDatasetVerifier(owners.first.inventory, 'python.exe', owners.first.work_root))
    expected_root = owners.stable if stable_history else original.work_root
    assert original.history_root == expected_root
    calls = []
    def terminal_preflight(source, environment, root, jobs, **kwargs):
        calls.append(root)
        assert root.is_relative_to(original.work_root)
        assert kwargs['profile'] == binding.ContainedDatasetPreparationService.prepare_package.__kwdefaults__['profile']
        selection = selected.read_selection(kwargs['selected_dataset']['memory'])
        assert Path(selection['root']) == f.package.destination
        assert selected.binding(selection) == kwargs['selected_dataset']['binding']
        if kwargs['should_cancel'](): raise InterruptedError('generated cancellation')
        nonce = '9'*32
        directory = root / ('preflight-'+nonce)
        (directory / 'events').mkdir(parents=True)
        receipt = dict(schema_version=1, nonce=nonce, operation='prepare', status='complete',
            process_fingerprint='a'*64, request_sha256='b'*64, result_sha256='c'*64)
        raw = _bytes(receipt)
        (directory / 'preflight-result.json').write_bytes(raw)
        journal = TrainingRunJournal(directory)
        tip = journal.append('reserved', {'plan':{'process_fingerprint':receipt['process_fingerprint']}}, '')
        tip = journal.append('starting', {}, tip)
        tip = journal.append('running', {}, tip)
        journal.append('exited_unverified', dict(exit_code=0, tree_stopped=True, preflight_receipt_sha256=_sha(raw)), tip)
        return None, SimpleNamespace(root=str(directory / 'work/prepared')), None, receipt
    monkeypatch.setattr(ContainedRuntimeQualificationService, 'from_reviewed_worker_inventory',
        classmethod(lambda cls, inventory, relative, **kwargs: SimpleNamespace(preflight=terminal_preflight)))
    owner = SimpleNamespace(lock=RLock(), dataset_calls=0, dataset_jobs=[], adapter=SimpleNamespace(check=lambda: None))
    wrapped = runtime.ModuleDatasetVerifier(original, owner)
    jobs = object()
    manifest = f.package.destination / 'collectigen-training-manifest.json'
    before = manifest.read_bytes()
    args = dict(source=Path(f.inspection.source), environment=Path(f.request['environment']),
        python_base=Path(f.request['python_base']), jobs=jobs, should_cancel=lambda: cancelled)
    if cancelled:
        with pytest.raises(InterruptedError): wrapped.prepare(f.package, **args)
        assert not ScopedPreparationHistory(expected_root).snapshot()[0]
        assert not (expected_root / 'sdxl-preflight-runs.json').exists()
    else:
        result = wrapped.prepare(f.package, **args)
        # A separately provisioned owner reads through the same root used by
        # WorkflowMethods.open_training_history(preparations=True).
        reopened = ScopedPreparationHistory(owners.second.history_root if stable_history else expected_root)
        records, revision = reopened.snapshot()
        assert len(records) == 1
        assert reopened.review_preparation(records[0], revision) == result[3]
        assert Path(records[0].destination).is_relative_to(original.work_root)
        if stable_history: assert not (original.work_root / 'sdxl-preflight-runs.json').exists()
        receipt = Path(records[0].review_path)
        receipt.write_bytes(receipt.read_bytes()+b' ')
        with pytest.raises(binding.TrainingProcessError): reopened.review_preparation(records[0], revision)
    assert len(calls) == 1 and owner.dataset_calls == 0 and owner.dataset_jobs == [jobs]
    assert manifest.read_bytes() == before


@pytest.mark.parametrize('wrong', ['relative', 'sibling', 'ancestor', 'noncanonical'])
def test_preparation_history_root_refuses_unowned_scope(admitted_preparation_owners, wrong):
    from core.training_dataset_binding import ReviewedDatasetVerifier
    original = admitted_preparation_owners.first
    root = original.work_root
    selected = {'relative':Path('relative'), 'sibling':root.parent / 'other',
        'ancestor':root.parent.parent, 'noncanonical':root / '..'}[wrong]
    with pytest.raises(ValueError, match='history parent'):
        ReviewedDatasetVerifier(original.inventory, original.python_relative, root, history_root=selected)
