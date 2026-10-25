"""Join bounded run pointers to retained reviews; never discover or launch runs."""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass, replace
import hashlib
from pathlib import Path

from .training_process import ProcessInput, ProcessInventory, TrainingProcessPlan, _bytes, _read, _write_new
from .training_run_index import TrainingRunRecord, TrainingRunIndexError

MAX_REVIEW_BYTES = 4 * 1024**2


def _plan(value):
    fields = deepcopy(value)
    fields['inputs'] = tuple(ProcessInput(**{**row, 'state': tuple(row['state'])}) for row in fields['inputs'])
    fields['inventories'] = tuple(ProcessInventory(**row) for row in fields['inventories'])
    fields['command'], fields['root_identity'] = tuple(fields['command']), tuple(fields['root_identity'])
    plan = TrainingProcessPlan(**fields)
    plan.validate()
    return plan


def retain_readiness(prepared):
    from .training_execution_readiness import digest, validate_request
    plan, request = prepared.plan, prepared.request
    plan.validate(); validate_request(request)
    if digest(request) != plan.preview_fingerprint:
        raise ValueError('training_readiness_review_unbound')
    path = Path(plan.root) / (plan.name + '.readiness-review.json')
    raw = _bytes({'schema_version': 1, 'kind': 'sdxl_training_readiness_review',
        'plan': asdict(plan), 'request': request, 'fingerprint': plan.fingerprint})
    if len(raw) > MAX_REVIEW_BYTES:
        raise ValueError('training_readiness_review_size_bound')
    if path.exists():
        if _read(path, limit=MAX_REVIEW_BYTES) != raw:
            raise ValueError('training_readiness_review_changed')
    else:
        _write_new(path, raw)
    if _read(path, limit=MAX_REVIEW_BYTES) != raw:
        raise ValueError('training_readiness_review_readback_failed')
    return path, hashlib.sha256(raw).hexdigest()


def register_review(index, plan, kind, path, sha256, *, registry_path='', registry_revision=''):
    """Persist the exact pointer before entering the process owner."""
    record = TrainingRunRecord(plan.run_id, kind, str(path), sha256, plan.fingerprint,
        str(plan.destination), str(registry_path), registry_revision, 'starting')
    record.validate()
    records, revision = index.snapshot()
    prior = next((item for item in records if item.run_id == plan.run_id), None)
    if prior is None:
        index.register(record, revision)
        return
    fields = ('run_id', 'kind', 'review_path', 'review_sha256', 'plan_fingerprint',
              'destination', 'registry_path', 'registry_revision')
    if any(getattr(prior, key) != getattr(record, key) for key in fields):
        raise TrainingRunIndexError('The retained training run pointer changed.')
    if prior.phase not in {'prepared', 'ready', 'held', 'starting'}:
        raise TrainingRunIndexError('This retained run requires recovery before another launch review.')
    index.update_phase(plan.run_id, 'starting', revision)


def note_outcome(index, plan, phase, tip=''):
    """An index failure must not interrupt process ownership or hide outputs."""
    try:
        records, revision = index.snapshot()
        record = next((item for item in records if item.run_id == plan.run_id), None)
        if record is None or record.plan_fingerprint != plan.fingerprint:
            return False
        index.update_phase(plan.run_id, phase, revision, latest_journal_tip=tip or None)
        return True
    except (OSError, RuntimeError, ValueError):
        return False


@dataclass(frozen=True)
class IndexedTrainingReview:
    record: TrainingRunRecord
    plan: TrainingProcessPlan
    value: dict


def restore_indexed_review(index, run_id, expected_revision):
    """Open only the explicitly selected index entry; verify all pointer bindings."""
    records, revision = index.snapshot()
    if revision != expected_revision:
        raise TrainingRunIndexError('The training run list changed. Refresh it before opening this run.')
    record = next((item for item in records if item.run_id == run_id), None)
    if record is None:
        raise TrainingRunIndexError('This training run is no longer indexed.')
    if record.kind == 'execution':
        from .training_execution import restore_review
        plan, value = restore_review(record.review_path, record.review_sha256)
        if value['resources']['registry_revision'] != record.registry_revision:
            raise TrainingRunIndexError('The retained training registry review changed.')
    else:
        from .training_execution_readiness import strict, digest, validate_request
        raw = _read(Path(record.review_path), limit=MAX_REVIEW_BYTES)
        if hashlib.sha256(raw).hexdigest() != record.review_sha256:
            raise TrainingRunIndexError('The retained readiness review changed.')
        saved = strict(raw)
        if (set(saved) != {'schema_version', 'kind', 'plan', 'request', 'fingerprint'}
                or type(saved['schema_version']) is not int or saved['schema_version'] != 1
                or saved['kind'] != 'sdxl_training_readiness_review'):
            raise TrainingRunIndexError('The retained readiness review is invalid.')
        plan, value = _plan(saved['plan']), validate_request(saved['request'])
        if saved['fingerprint'] != plan.fingerprint or digest(value) != plan.preview_fingerprint:
            raise TrainingRunIndexError('The retained readiness review is unbound.')
    if (plan.run_id != record.run_id or plan.fingerprint != record.plan_fingerprint
            or str(plan.destination) != record.destination):
        raise TrainingRunIndexError('The retained training plan differs from its index pointer.')
    if index.snapshot()[1] != revision:
        raise TrainingRunIndexError('The training run list changed while opening this review.')
    return IndexedTrainingReview(record, plan, value)


def recover_plan_process(plan, process, expected_tip):
    """Reconcile one exact retained plan and preserve its observed terminal exit."""
    from .training_process import TrainingRunJournal, TrainingProcessService, TrainingProcessResult
    journal = TrainingRunJournal(plan.destination)
    before, tip = journal.read()
    receipt = TrainingProcessService._receipt(plan)
    expected = {'plan': receipt, 'fingerprint': hashlib.sha256(_bytes(receipt)).hexdigest()}
    if not before or tip != expected_tip or _bytes(before[0]['details']) != _bytes(expected):
        raise TrainingRunIndexError('The retained process journal differs from this reviewed training plan.')
    result = process.recover(plan.destination, expected_tip)
    if not isinstance(result, TrainingProcessResult) or result.destination != plan.destination:
        raise TrainingRunIndexError('Process recovery returned another run.')
    if not result.tree_stopped:
        return result
    after, tip = journal.read()
    if (after[:len(before)] != before or not after or tip != result.journal_tip
            or after[-1]['state'] != result.status):
        raise TrainingRunIndexError('The process journal changed during recovery.')
    code = after[-1]['details'].get('exit_code')
    if code is not None and type(code) is not int:
        raise TrainingRunIndexError('The retained process exit status is invalid.')
    if result.status == 'exited_unverified' and code is None:
        raise TrainingRunIndexError('The stopped trainer has no retained exit status.')
    return replace(result, exit_code=code)


@dataclass(frozen=True)
class RestoredTrainingContext:
    review: IndexedTrainingReview
    package: object = None
    registry: object = None
    recipe: object = None
    request: dict | None = None
    package_error: str = ''


def restore_training_context(index, run_id, expected_revision, *, should_cancel=None, allow_process_only=False):
    """Reopen one chosen review and its package without launching or registering."""
    def cancel():
        if should_cancel and should_cancel():
            raise InterruptedError('Opening the retained training review was cancelled.')
    cancel()
    review = restore_indexed_review(index, run_id, expected_revision)
    if review.record.kind == 'readiness':
        return RestoredTrainingContext(review)
    from .training_execution_plan import validate_contract_structure
    from .training_adapter_preview import SdxlLoraRecipe
    from .training_package import TrainingPackageService
    from .model_resource_registry import ModelResourceRegistry
    contract = validate_contract_structure(review.value)
    package = TrainingPackageService().verify(contract['package']['path'], should_cancel=should_cancel)
    if not package.valid or package.package_fingerprint != contract['package']['fingerprint']:
        message = 'The retained training package changed or is unavailable. Keep this run workspace and restore its exact package before continuing.'
        if allow_process_only and review.record.phase in {
                'starting', 'running', 'stopping', 'recovery_required', 'stopped',
                'cancelled', 'failed', 'interrupted', 'verification_required',
                'registration_required', 'completed'}:
            cancel()
            if index.snapshot()[1] != expected_revision:
                raise TrainingRunIndexError('The training run list changed while checking the package. Refresh it before opening this run.')
            return RestoredTrainingContext(review, package_error=message)
        raise TrainingRunIndexError(message)
    recipe = SdxlLoraRecipe.from_dict(contract['recipe']['proposal'])
    inventories = {row['purpose']: row['root'] for row in contract['process']['inventories']}
    request = {name + '_root': inventories[name + '_inventory'] for name in ('source', 'environment', 'python_base')}
    request.update(python=contract['process']['command'][0],
        tokenizers_root=str(Path(contract['resources']['tokenizers'][0]['path']).parent),
        output_root=review.plan.root, run_name=review.plan.name,
        checkpoint_interval=contract['continuation']['contract']['settings']['save_every_n_steps'])
    cancel()
    if index.snapshot()[1] != expected_revision:
        raise TrainingRunIndexError('The training run list changed while checking the package. Refresh it before opening this run.')
    return RestoredTrainingContext(review, package, ModelResourceRegistry(review.record.registry_path), recipe, request)
