"""Retained journal/input proofs survive app updates; no trainer is launched."""
from dataclasses import asdict, replace
import hashlib
import pytest
from core.training_process import (ProcessInput, TrainingProcessPlan,
    TrainingProcessResult, TrainingProcessService, TrainingRunJournal, _bytes, _directory)
from core.training_result import TrainingResultError, _process_evidence
from core.training_retained_result import _retained_process_evidence
from core.training_verification import PHASES, verified_receipt_hashes


def saved_process(tmp_path):
    root = tmp_path / 'runs'; root.mkdir()
    worker = tmp_path / 'old-worker.py'; worker.write_bytes(b'old reviewed application code')
    plan = TrainingProcessPlan('1' * 32, str(root), _directory(root), 'saved',
        (str(worker),), (ProcessInput.capture(worker),), 'a' * 64)
    plan.destination.mkdir(); (plan.destination / 'events').mkdir()
    (plan.destination / 'process.log').write_bytes(b'')
    journal = TrainingRunJournal(plan.destination)
    recorded = TrainingProcessService._receipt(plan)
    sha = lambda raw: hashlib.sha256(raw).hexdigest()
    tip = journal.append('reserved', {'plan': recorded, 'fingerprint': sha(_bytes(recorded))}, '')
    tip = journal.append('starting', {'owner': 'synthetic'}, tip)
    tip = journal.append('running', {'pid': 1, 'owner': 'synthetic', 'pause_supported': False}, tip)
    for index, phase in enumerate(PHASES):
        nonce = f'{index + 1:032x}'
        request = {'schema_version': 1, 'nonce': nonce, 'phase': phase,
            'process_fingerprint': plan.fingerprint,
            'inputs': [asdict(item) for item in plan.inputs], 'inventories': []}
        receipt = {key: request[key] for key in ('schema_version', 'nonce', 'phase', 'process_fingerprint')}
        receipt.update(request_sha256=sha(_bytes(request)), status='verified')
        (plan.destination / ('verification-' + phase + '.json')).write_bytes(_bytes(receipt))
    details = {'exit_code': 0, 'tree_stopped': True, 'message': 'Stopped synthetic process',
        'log_sha256': sha(b''), 'outputs_verified': False,
        'input_verification_sha256': verified_receipt_hashes(plan)}
    tip = journal.append('exited_unverified', details, tip)
    outcome = TrainingProcessResult('exited_unverified', plan.destination,
        details['message'], 0, True, tip)
    return plan, outcome, worker


@pytest.mark.parametrize('change', ['contents', 'metadata'])
def test_retained_input_receipts_survive_application_update(tmp_path, change):
    plan, outcome, worker = saved_process(tmp_path)
    expected = _retained_process_evidence(plan, outcome)
    if change == 'contents':
        worker.write_bytes(b'updated application code')
    else:
        import os
        value = worker.stat()
        os.utime(worker, ns=(value.st_atime_ns, value.st_mtime_ns + 1_000_000_000))
    assert _retained_process_evidence(plan, outcome) == expected
    with pytest.raises(TrainingResultError):
        _process_evidence(plan, outcome)


@pytest.mark.parametrize('target', ['receipt', 'log', 'journal'])
def test_changed_retained_process_proof_is_rejected(tmp_path, target):
    plan, outcome, _ = saved_process(tmp_path)
    name = {'receipt': 'verification-after-exit.json', 'log': 'process.log',
            'journal': 'events/003.json'}[target]
    (plan.destination / name).write_bytes(b'changed')
    with pytest.raises((TrainingResultError, RuntimeError)):
        _retained_process_evidence(plan, outcome)


@pytest.mark.parametrize('change', [{'exit_code': 2}, {'tree_stopped': False}])
def test_retained_process_must_have_successfully_stopped(tmp_path, change):
    plan, outcome, _ = saved_process(tmp_path)
    with pytest.raises(TrainingResultError):
        _retained_process_evidence(plan, replace(outcome, **change))
