"""Synthetic readiness evidence; no real trainer, model, network or GPU."""
from copy import deepcopy
from dataclasses import replace
import hashlib
from pathlib import Path
from types import SimpleNamespace

import pytest

from core import training_execution_readiness as readiness
from core import training_reference_tokenizers as tokenizers
from core.training_process import ProcessInput, TrainingProcessService, _bytes
from core.training_runtime_contract import synthetic_configurations
from core.training_tokenizer_files import TOKENIZER_FILES


def fixture(tmp_path, monkeypatch):
    roots = [tmp_path / name for name in ('source', 'environment', 'python', 'tokenizers', 'runs')]
    for root in roots:
        root.mkdir()
        (root / 'generated.txt').write_bytes(b'generated software')
    python = roots[2] / 'python.exe'
    python.write_bytes(b'generated nonexecutable interpreter')
    for slot in ('tokenizer', 'tokenizer_2'):
        folder = roots[3] / slot
        folder.mkdir()
        for name, _ in TOKENIZER_FILES:
            raw = b'{"!":0}\n' if name == 'vocab.json' else b'#version: 0.2\n' if name == 'merges.txt' else b'{}\n'
            (folder / name).write_bytes(raw)
    monkeypatch.setattr(tokenizers, 'inspect_reference_slot', lambda *_: None)
    profile = {'profile': 'generated-reference', 'evidence_sha256': 'a' * 64,
               'runtime_profile': 'kohya-uv-45088f04'}
    service = readiness.TrainingExecutionReadinessService()
    prepared = service.prepare(*roots[:3], python, roots[3], synthetic_configurations()[0],
        profile, 'sha256:' + 'b' * 64, tmp_path / 'prepared', roots[4], 'native-check')
    return service, prepared, roots


def native_result(prepared):
    request, plan = prepared.request, prepared.plan
    rows = {row['path']: row for row in request['process']['inputs']}
    slots = []
    for role, slot in enumerate(('tokenizer', 'tokenizer_2'), 1):
        folder = Path(request['selection']['tokenizers_root']) / slot
        files = [{'name': name, 'bytes': rows[str(folder / name)]['state'][2],
                  'sha256': rows[str(folder / name)]['sha256']} for name, _ in TOKENIZER_FILES]
        slots.append({'role': role, 'identity': request['selection']['tokenizer_identities'][role - 1],
            'files': files, 'vocabulary_size': 49408, 'merge_count': 48894, 'model_max_length': 77,
            'bos_token_id': 49406, 'eos_token_id': 49407, 'unk_token_id': 49407,
            'pad_token_id': 49407 if role == 1 else 0})
    packages = {key: value or 'generated-1.0' for key, value in request['expected_packages'].items()}
    return {'schema_version': 1, 'kind': readiness.RESULT_KIND, 'process_fingerprint': plan.fingerprint,
        'request_sha256': readiness.digest(request), 'configuration_sha256': readiness.digest(request['selection']['configuration']),
        'runtime': {'python_version': '3.11.14', 'packages': packages},
        'device_observation': {'device_type': 'cuda', 'cuda_index': 0, 'name': 'Generated GPU',
            'compute_capability': [12, 0], 'total_memory_bytes': 16 * 1024**3,
            'torch_version': packages['torch'], 'cuda_version': '12.8'},
        'tokenizers': {'schema_version': 1, 'kind': 'sdxl_reference_tokenizer_construction',
            'profile': tokenizers.PROFILE, 'slots': slots}, 'denials': []}


def test_preparation_binds_explicit_software_and_original_tokenizers_without_launch(tmp_path, monkeypatch):
    service, prepared, roots = fixture(tmp_path, monkeypatch)
    assert readiness.validate_request(prepared.request) is prepared.request
    assert not prepared.plan.destination.exists()
    assert prepared.plan.command[0] == str(roots[2] / 'python.exe')
    assert len(prepared.plan.inventories) == 3
    assert prepared.plan.preview_fingerprint == readiness.digest(prepared.request)
    assert ProcessInput.capture(prepared.plan.command[-1]) == prepared.plan.inputs[-1]
    TrainingProcessService._verify_inputs(prepared.plan)
    value = native_result(prepared)
    assert readiness.validate_native_result(value, prepared.request, prepared.plan.fingerprint) is value


@pytest.mark.parametrize('fault', ['process', 'request', 'configuration', 'denial', 'python', 'package',
    'torch', 'cpu', 'old_gpu', 'slot', 'tokenizer_hash', 'tokenizer_bytes', 'tokenizer_identity'])
def test_native_measurements_cannot_be_relabelled_or_substituted(tmp_path, monkeypatch, fault):
    _, prepared, _ = fixture(tmp_path, monkeypatch)
    value = native_result(prepared)
    if fault == 'process': value['process_fingerprint'] = 'f' * 64
    elif fault == 'request': value['request_sha256'] = 'f' * 64
    elif fault == 'configuration': value['configuration_sha256'] = 'f' * 64
    elif fault == 'denial': value['denials'] = ['generated denied read']
    elif fault == 'python': value['runtime']['python_version'] = '3.12.0'
    elif fault == 'package': value['runtime']['packages']['transformers'] = 'changed'
    elif fault == 'torch': value['device_observation']['torch_version'] = 'changed'
    elif fault == 'cpu': value['device_observation']['device_type'] = 'cpu'
    elif fault == 'old_gpu': value['device_observation']['compute_capability'] = [7, 5]
    elif fault == 'slot': value['tokenizers']['slots'].reverse()
    elif fault == 'tokenizer_hash': value['tokenizers']['slots'][0]['files'][0]['sha256'] = 'f' * 64
    elif fault == 'tokenizer_bytes': value['tokenizers']['slots'][0]['files'][0]['bytes'] += 1
    else: value['tokenizers']['slots'][0]['identity'] = 'clip-files-v1:' + 'f' * 64
    with pytest.raises(ValueError):
        readiness.validate_native_result(value, prepared.request, prepared.plan.fingerprint)


@pytest.mark.parametrize('fault', ['cancelled', 'failed', 'held', 'live'])
def test_unsuccessful_or_live_process_cannot_publish_readiness(tmp_path, monkeypatch, fault):
    service, prepared, _ = fixture(tmp_path, monkeypatch)
    outcome = SimpleNamespace(status='exited_unverified' if fault == 'live' else fault,
        tree_stopped=fault != 'live', exit_code=0 if fault == 'live' else 2)
    service.process = SimpleNamespace(execute=lambda *a, **k: outcome)
    observed, receipt = service.execute(prepared, prepared.plan.fingerprint, object())
    assert observed is outcome and receipt is None
    assert not (prepared.plan.destination / 'native-readiness-evidence.json').exists()


def test_confirmation_and_request_changes_do_not_launch(tmp_path, monkeypatch):
    service, prepared, _ = fixture(tmp_path, monkeypatch)
    service.process = SimpleNamespace(execute=lambda *a, **k: pytest.fail('Unexpected native launch'))
    with pytest.raises(ValueError, match='confirmation_changed'):
        service.execute(prepared, 'f' * 64, object())
    prepared.request['selection']['base_id'] = 'sha256:' + 'c' * 64
    with pytest.raises(ValueError, match='confirmation_changed'):
        service.execute(prepared, prepared.plan.fingerprint, object())


@pytest.mark.parametrize('raw', [b'{"a":1,"a":2}\n', b'{"a":NaN}\n', b'{"a":1} ', b'{}\n' * 2])
def test_ambiguous_native_evidence_is_rejected(raw):
    with pytest.raises(ValueError): readiness.strict(raw)


def test_reference_admission_requires_stopped_process_provenance(tmp_path, monkeypatch):
    _, prepared, _ = fixture(tmp_path, monkeypatch)
    native = native_result(prepared)
    value = {'schema_version': 1, 'kind': readiness.RECEIPT_KIND,
        'plan': __import__('dataclasses').asdict(prepared.plan), 'request': prepared.request,
        'native': native, 'native_sha256': readiness.digest(native),
        'process_journal_tip': 'd' * 64, 'input_verification_sha256': ['a' * 64] * 3}
    raw = readiness.canonical(value)
    admission = {'native_readiness_receipt': {'path': str(tmp_path / 'unpublished.json'),
        'sha256': hashlib.sha256(raw).hexdigest()}}
    with pytest.raises((ValueError, OSError, RuntimeError)):
        readiness.validate_reference_admission(admission, raw)


@pytest.mark.parametrize('reuse', [False, True])
def test_stopped_measurement_joins_role_bound_original_tokenizers(tmp_path, monkeypatch, reuse):
    from dataclasses import asdict
    from core import training_verification
    from core.training_process import TrainingRunJournal
    _, prepared, _ = fixture(tmp_path, monkeypatch)
    plan, request = prepared.plan, prepared.request
    plan.destination.mkdir(); (plan.destination/'events').mkdir(); (plan.destination/'work').mkdir()
    journal=TrainingRunJournal(plan.destination)
    receipt=TrainingProcessService._receipt(plan)
    tip=journal.append('reserved',{'plan':receipt,'fingerprint':readiness.digest(receipt)},'')
    tip=journal.append('starting',{},tip)
    tip=journal.append('running',{},tip)
    tip=journal.append('exited_unverified',{'exit_code':0,'tree_stopped':True},tip)
    native=native_result(prepared)
    (plan.destination/'work/readiness-result.json').write_bytes(readiness.canonical(native))
    verification_hashes=['a'*64,'b'*64,'c'*64]
    # Native supervisor receipts have separate tests; this isolates their admission projection.
    monkeypatch.setattr(training_verification,'verified_receipt_hashes',lambda checked:verification_hashes if checked==plan else [])
    value={'schema_version':1,'kind':readiness.RECEIPT_KIND,'plan':asdict(plan),'request':request,
        'native':native,'native_sha256':readiness.digest(native),'process_journal_tip':tip,
        'input_verification_sha256':verification_hashes}
    raw=readiness.canonical(value)
    qualification=request['selection']['qualification']
    admission={'native_readiness_receipt':{'path':str(plan.destination/'evidence.json'),'sha256':hashlib.sha256(raw).hexdigest()},
        'qualification_profile':qualification['profile'],'qualification_evidence_sha256':qualification['evidence_sha256'],
        'runtime_profile':qualification['runtime_profile'],'source_revision':request['source_revision'],
        'base_id':request['selection']['base_id'],
        'tokenizer_identities':[{'role':i,'identity':identity} for i,identity in enumerate(request['selection']['tokenizer_identities'],1)],
        'device_observation_sha256':readiness.digest(native['device_observation']),
        'software_inventories':{row['purpose'].removesuffix('_inventory'):row['inventory_sha256'] for row in request['process']['inventories']}}
    shared = {}
    if reuse:
        shared['software_snapshot'] = readiness._software_snapshot(request, admission['native_readiness_receipt']['sha256'])
        monkeypatch.setattr(TrainingProcessService, '_verify_inputs',
            staticmethod(lambda *a, **k: pytest.fail('Repeated whole-software verification during the same preparation')))
    assert readiness.validate_reference_admission(admission,raw,**shared)['native']==native
    if reuse:
        Path(plan.inputs[0].path).write_bytes(b'changed generated interpreter')
        with pytest.raises(ValueError,match='process_input_changed'):
            readiness.validate_reference_admission(admission,raw,**shared)
    admission['tokenizer_identities'].reverse()
    with pytest.raises(ValueError,match='admission_changed'):
        readiness.validate_reference_admission(admission,raw,**shared)


def test_readiness_cli_has_no_training_or_profile_override(monkeypatch):
    from tools import training_execution_readiness_worker as worker
    monkeypatch.setattr(worker.sys, 'argv', ['worker.py', 'request.json', '--force'])
    monkeypatch.setattr(worker, 'run', lambda *_: pytest.fail('Unexpected native run'))
    assert worker.main() == 2


def test_readiness_uses_exact_process_canonical_bytes():
    value = {'scope': 'generated Ω', 'values': [1, True, None]}
    assert readiness.canonical(value) == _bytes(value)


def test_readiness_reconstructs_actual_reserved_process_plan(tmp_path, monkeypatch):
    from core import training_process as process
    from tools import training_execution_worker as worker
    _, prepared, _ = fixture(tmp_path, monkeypatch)
    plan = prepared.plan
    plan.destination.mkdir()
    (plan.destination / 'events').mkdir()
    (plan.destination / 'work').mkdir()
    receipt = process.TrainingProcessService._receipt(plan)
    process.TrainingRunJournal(plan.destination).append('reserved',
        {'plan': receipt, 'fingerprint': hashlib.sha256(_bytes(receipt)).hexdigest()}, '')
    rebuilt = worker.reconstruct_plan(prepared.request, plan.command[-1], process,
        work=plan.destination / 'work', interpreter=plan.command[0], worker=readiness.WORKER)
    assert rebuilt == plan


def test_connected_preparation_reuses_bound_software_but_captures_its_package(tmp_path, monkeypatch):
    from core import training_execution_selection as selection
    from core import training_execution_preparation as preparation
    from core.training_file_inventory import SoftwareInventoryService
    _, prepared, roots = fixture(tmp_path, monkeypatch)
    receipt_sha = 'f' * 64
    snapshot = readiness._software_snapshot(prepared.request, receipt_sha)
    monkeypatch.setattr(SoftwareInventoryService, 'verify', lambda *a, **k: pytest.fail('Software rescan'))
    summaries = selection._inventories(prepared.request, None, None, snapshot, receipt_sha)
    assert set(summaries) == {'source', 'environment', 'python_base'}
    service = SoftwareInventoryService()
    capture = service.capture
    calls = []
    def counted(root, **kwargs):
        calls.append(Path(root))
        return capture(root, **kwargs)
    monkeypatch.setattr(service, 'capture', counted)
    copies = tmp_path / 'execution-inputs'; copies.mkdir()
    for purpose, root in zip(('source_inventory', 'environment_inventory', 'python_base_inventory'), roots):
        _, row = preparation._inventory(service, root, copies / (purpose + '.json'), purpose, None, None,
                                        software_snapshot=snapshot)
        assert row['inventory_sha256'] == summaries[purpose.removesuffix('_inventory')]['inventory_sha256']
    assert calls == []
    package = tmp_path / 'generated-package'; package.mkdir(); (package / 'image.txt').write_bytes(b'generated image')
    preparation._inventory(service, package, copies / 'package.json', 'package_inventory', None, None,
                           software_snapshot=snapshot)
    assert calls == [package]


@pytest.mark.parametrize('fault', ['receipt', 'request', 'root', 'purpose', 'manifest'])
def test_software_snapshot_cannot_cross_receipts_or_roots(tmp_path, monkeypatch, fault):
    _, prepared, roots = fixture(tmp_path, monkeypatch)
    request = deepcopy(prepared.request)
    receipt_sha = 'f' * 64
    snapshot = readiness._software_snapshot(request, receipt_sha)
    if fault == 'receipt': receipt_sha = 'e' * 64
    elif fault == 'request': request['selection']['base_id'] = 'sha256:' + 'e' * 64
    elif fault == 'root':
        with pytest.raises(ValueError, match='snapshot_binding_changed'):
            snapshot.inventory('environment_inventory', roots[0])
        return
    elif fault == 'purpose': snapshot = replace(snapshot, inventories=tuple(reversed(snapshot.inventories)))
    else:
        path = Path(request['process']['inventories'][0]['manifest_path'])
        path.write_bytes(path.read_bytes() + b' ')
    with pytest.raises(ValueError, match='snapshot_binding_changed'):
        snapshot.validate(request, receipt_sha)


def test_changed_software_still_fails_execution_after_snapshot_reuse(tmp_path, monkeypatch):
    import os
    _, prepared, roots = fixture(tmp_path, monkeypatch)
    snapshot = readiness._software_snapshot(prepared.request, 'f' * 64)
    path = roots[1] / 'generated.txt'
    before = path.stat()
    original = path.read_bytes()
    path.write_bytes(original.upper())
    os.utime(path, ns=(before.st_atime_ns, before.st_mtime_ns))
    assert path.stat().st_size == before.st_size
    # Review projects the completed observation; Start still checks actual bytes.
    snapshot.validate(prepared.request, 'f' * 64)
    with pytest.raises(RuntimeError):
        TrainingProcessService._verify_inputs(prepared.plan)
