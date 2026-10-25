"""Stopped-run SDXL validation. Measurements are denoising MSE, never quality scores.

The evaluation worker has its own native qualification. The existing training
profile cannot authorize a different source closure or another GPU workload.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path, PurePosixPath
import re

from .training_package import TrainingPackageService
from .training_configuration import TrainingConfiguration
from .training_process import (_bytes, _read, _write_new, ProcessInput, ProcessInventory,
    TrainingProcessPlan, TrainingProcessResult, TrainingProcessService,
    TrainingRunJournal, LOCAL_ENVIRONMENT)
from .training_result import VerifiedTrainingResult, SdxlTrainingResultService
from .training_source_io import _assert_unredirected


PROFILE = 'collectigen.sdxl-heldout-denoising-mse-v1'
WORKER = Path(__file__).resolve().parents[1] / 'core' / 'training_validation_worker.py'
PROFILE_FILE = Path(__file__).resolve().parents[1] / 'tools' / 'training_validation_profile.json'
EVIDENCE_FILE = Path(__file__).resolve().parents[1] / 'tools' / 'training_validation_evidence.json'
REQUEST_NAME = 'training-validation-request.json'
RESULT_NAME = 'training-validation-result.json'
# The process owner admits at most 128 individually fingerprinted inputs.
# Leave room for its verifier closure, software manifests and tokenizers.
MAX_CASES = 40
MAX_REQUEST = 256 * 1024
MAX_RESULT = 256 * 1024
TIMESTEPS = (250, 750)
SHA = re.compile(r'[0-9a-f]{64}\Z')
_ROOT = Path(__file__).resolve().parents[1]
OWNED_NATIVE_CODE = {name: _ROOT / name for name in (
    'core/training_reference_loading.py', 'core/training_sdxl_loading.py',
    'core/training_reference_tokenizers.py', 'core/training_tokenizer_files.py',
    'core/training_tokenizer_contract.py', 'core/training_continuation_hooks.py',
    'core/training_unet_step_contract.py', 'core/training_model_contract.py',
    'core/model_resource_registry.py')}


class TrainingValidationError(ValueError):
    pass


def _digest(value):
    return hashlib.sha256(_bytes(value)).hexdigest()


def _cancel(should_cancel):
    if should_cancel and should_cancel():
        raise InterruptedError('Held-out validation was cancelled.')


def _relative(value):
    if type(value) is not str or not value or len(value) > 512 or '\\' in value or ':' in value:
        raise TrainingValidationError('validation_package_path_invalid')
    path = PurePosixPath(value)
    if path.is_absolute() or any(part in {'', '.', '..'} for part in value.split('/')):
        raise TrainingValidationError('validation_package_path_invalid')
    return value


def _cases(verification, execution_fingerprint):
    manifest = verification.manifest
    artifacts = {row['path']: row for row in manifest['artifacts']}
    rows = [item for item in manifest['package_plan']['items'] if item['role'] == 'validation']
    if not rows:
        return ()
    if len(rows) > MAX_CASES or len({row['stable_id'] for row in rows}) != len(rows):
        raise TrainingValidationError('validation_split_unsupported')
    cases = []
    for item in sorted(rows, key=lambda row: row['stable_id']):
        image = artifacts[_relative(item['image_path'])]
        caption = artifacts[_relative(item['caption_path'])]
        if image['kind'] != 'image' or caption['kind'] != 'caption' or any(
            not SHA.fullmatch(row['sha256']) for row in (image, caption)):
            raise TrainingValidationError('validation_artifact_invalid')
        width, height = image['transform']['output_dimensions']
        if (type(width) is not int or type(height) is not int or min(width, height) < 64
                or width % 64 or height % 64 or width * height > 1024 * 1024):
            raise TrainingValidationError('validation_geometry_unsupported')
        seed = int(hashlib.sha256(_bytes([PROFILE, execution_fingerprint, item['stable_id']])).hexdigest()[:16], 16)
        cases.append({'stable_id': item['stable_id'], 'image_path': item['image_path'],
            'image_sha256': image['sha256'], 'caption_path': item['caption_path'],
            'caption_sha256': caption['sha256'], 'width': width, 'height': height,
            'seed': seed, 'timesteps': list(TIMESTEPS)})
    return tuple(cases)


def available(verification) -> bool:
    """An empty validation role is visibly unavailable, never scored as zero."""
    return bool(verification.valid and _cases(verification, verification.package_fingerprint))


def request_for_verified(plan, contract, verified: VerifiedTrainingResult, *, should_cancel=None):
    """Bind a fresh stopped final result and verified validation-role package.

    This does not create a process or grant launch authority.
    """
    if type(verified) is not VerifiedTrainingResult or verified.plan != plan:
        raise TrainingValidationError('validation_result_unbound')
    fresh = SdxlTrainingResultService().verify(plan, verified.outcome, contract,
        verified.anchor_sha256, should_cancel=should_cancel)
    if fresh.receipt_json != verified.receipt_json:
        raise TrainingValidationError('validation_result_changed')
    receipt = fresh.receipt
    output_path = plan.destination / receipt['output']['relative_path']
    return _request_from_source(plan, contract, output_path,
        receipt['output']['resource_id'][7:], receipt['package_fingerprint'],
        {'kind': 'verified_final_result', 'receipt_sha256': fresh.receipt_sha256},
        should_cancel=should_cancel)


def request_for_checkpoint_export(plan, contract, previous, export, *, should_cancel=None):
    """Bind an exact anchored, stopped checkpoint's separately published FP32 LoRA."""
    from .training_checkpoint_export import CheckpointExport, export_checkpoint
    if type(export) is not CheckpointExport:
        raise TrainingValidationError('validation_checkpoint_export_unbound')
    fresh = export_checkpoint(plan, contract, previous, export.path,
        confirmed_selection=_digest(previous), should_cancel=should_cancel)
    if fresh != export:
        raise TrainingValidationError('validation_checkpoint_export_changed')
    return _request_from_source(plan, contract, export.path, export.sha256,
        contract['result_binding']['package_fingerprint'],
        {'kind': 'anchored_checkpoint_export', 'receipt_sha256': export.receipt_sha256,
         'step': export.step}, should_cancel=should_cancel)


def _request_from_source(plan, contract, output_path, expected_sha256,
                         expected_package_fingerprint, origin, *, should_cancel=None):
    binding = contract['result_binding']
    package_path = Path(binding['package_path']).absolute()
    verification = TrainingPackageService().verify(package_path, should_cancel=should_cancel)
    if not verification.valid or verification.package_fingerprint != expected_package_fingerprint:
        raise TrainingValidationError('validation_package_changed')
    cases = _cases(verification, plan.fingerprint)
    if not cases:
        raise TrainingValidationError('validation_split_empty')
    selected = ProcessInput.capture(output_path, should_cancel)
    if selected.sha256 != expected_sha256:
        raise TrainingValidationError('validation_model_changed')
    resources = contract['resources']
    base = resources['base']
    if 'sha256:' + ProcessInput.capture(base['path'], should_cancel).sha256 != base['id']:
        raise TrainingValidationError('validation_base_changed')
    tokenizers = []
    for item in resources['tokenizers']:
        rows = []
        for name, expected in sorted(item['files'].items()):
            captured = ProcessInput.capture(Path(item['path']) / name, should_cancel)
            if captured.sha256 != expected:
                raise TrainingValidationError('validation_tokenizer_changed')
            rows.append({'name': name, 'sha256': expected})
        tokenizers.append({'path': item['path'], 'files': rows, 'identity': item['identity']})
    _cancel(should_cancel)
    result = {'schema_version': 1, 'kind': 'sdxl_heldout_validation_request',
        'profile': PROFILE, 'training_process_fingerprint': plan.fingerprint,
        'training_contract_sha256': plan.preview_fingerprint,
        'model_origin': origin,
        'package_path': str(package_path), 'package_fingerprint': verification.package_fingerprint,
        'model_path': str(output_path), 'model_sha256': selected.sha256,
        'base_path': base['path'], 'base_sha256': base['id'][7:],
        'tokenizers': tokenizers, 'cases': list(cases), 'timesteps': list(TIMESTEPS),
        'metric': 'epsilon_denoising_mse', 'quality_claim': 'none'}
    if len(_bytes(result)) > MAX_REQUEST:
        raise TrainingValidationError('validation_request_too_large')
    return result


def qualified_profile():
    """A missing independently qualified profile keeps product launch closed."""
    if not PROFILE_FILE.is_file():
        return None
    raw = _read(PROFILE_FILE, limit=64 * 1024)
    value = json.loads(raw)
    expected = {'schema_version', 'kind', 'profile', 'worker_sha256',
        'evaluator_sha256', 'native_evidence_sha256'}
    if (type(value) is not dict or set(value) != expected or value['schema_version'] != 1
            or value['kind'] != 'sdxl_heldout_validation_qualification' or value['profile'] != PROFILE
            or any(type(value[key]) is not str or not SHA.fullmatch(value[key])
                   for key in ('worker_sha256', 'evaluator_sha256', 'native_evidence_sha256'))
            or _bytes(value) != raw or value['worker_sha256'] != ProcessInput.capture(WORKER).sha256
            or value['evaluator_sha256'] != ProcessInput.capture(__file__).sha256):
        raise TrainingValidationError('validation_qualification_changed')
    if not EVIDENCE_FILE.is_file():
        raise TrainingValidationError('validation_native_evidence_missing')
    evidence_raw = _read(EVIDENCE_FILE, limit=64 * 1024)
    if hashlib.sha256(evidence_raw).hexdigest() != value['native_evidence_sha256']:
        raise TrainingValidationError('validation_native_evidence_changed')
    evidence = json.loads(evidence_raw)
    required = {'schema_version', 'kind', 'profile', 'worker_sha256', 'evaluator_sha256',
        'source_revision', 'runtime_profile', 'source_inventory_sha256',
        'environment_inventory_sha256', 'python_base_inventory_sha256',
        'package_fingerprint', 'base_sha256', 'output_sha256', 'request_sha256',
        'process_fingerprint', 'process_journal_tip', 'input_verification_sha256',
        'report_sha256', 'candidate_status', 'qualification', 'tree_stopped',
        'sample_count', 'timesteps', 'mean_mse', 'elapsed_seconds',
        'peak_cuda_memory_bytes', 'negative_cases', 'owned_code_sha256'}
    negatives = {'empty_split', 'changed_model', 'changed_package', 'changed_tokenizer',
                 'nonfinite_measurement', 'corrupt_report', 'cancelled_tree_stopped'}
    hashes = {'worker_sha256', 'evaluator_sha256', 'source_inventory_sha256',
        'environment_inventory_sha256', 'python_base_inventory_sha256',
        'package_fingerprint', 'base_sha256', 'output_sha256', 'request_sha256',
        'process_fingerprint', 'process_journal_tip', 'report_sha256'}
    if (type(evidence) is not dict or set(evidence) != required or _bytes(evidence) != evidence_raw
            or evidence['schema_version'] != 1
            or evidence['kind'] != 'sdxl_heldout_validation_native_evidence'
            or evidence['profile'] != PROFILE
            or evidence['worker_sha256'] != value['worker_sha256']
            or evidence['evaluator_sha256'] != value['evaluator_sha256']
            or evidence['candidate_status'] != 'candidate_complete'
            or evidence['qualification'] != 'candidate_only'
            or evidence['tree_stopped'] is not True
            or type(evidence['source_revision']) is not str or not evidence['source_revision']
            or type(evidence['runtime_profile']) is not str or not evidence['runtime_profile']
            or any(type(evidence[key]) is not str or not SHA.fullmatch(evidence[key]) for key in hashes)
            or type(evidence['owned_code_sha256']) is not dict
            or set(evidence['owned_code_sha256']) != set(OWNED_NATIVE_CODE)
            or any(evidence['owned_code_sha256'][name] != ProcessInput.capture(path).sha256
                   for name, path in OWNED_NATIVE_CODE.items())
            or type(evidence['input_verification_sha256']) is not list
            or len(evidence['input_verification_sha256']) != 3
            or any(type(item) is not str or not SHA.fullmatch(item)
                   for item in evidence['input_verification_sha256'])
            or type(evidence['sample_count']) is not int or not 2 <= evidence['sample_count'] <= MAX_CASES
            or evidence['timesteps'] != list(TIMESTEPS)
            or type(evidence['mean_mse']) not in {int, float}
            or not math.isfinite(evidence['mean_mse']) or evidence['mean_mse'] < 0
            or type(evidence['elapsed_seconds']) not in {int, float}
            or not math.isfinite(evidence['elapsed_seconds']) or evidence['elapsed_seconds'] <= 0
            or type(evidence['peak_cuda_memory_bytes']) is not int or evidence['peak_cuda_memory_bytes'] <= 0
            or type(evidence['negative_cases']) is not dict
            or set(evidence['negative_cases']) != negatives
            or any(item is not True for item in evidence['negative_cases'].values())):
        raise TrainingValidationError('validation_native_evidence_invalid')
    return {**value, 'native_evidence': evidence}


def _qualification_matches_request(profile, request, contract):
    evidence = profile['native_evidence']
    inventories = {row['purpose']: row['manifest_sha256']
                   for row in contract['process']['inventories']}
    if (contract['recipe']['source_revision'] != evidence['source_revision']
            or contract['recipe']['runtime_profile'] != evidence['runtime_profile']
            or request['base_sha256'] != evidence['base_sha256']
            or any(inventories.get(purpose) != evidence[purpose + '_sha256']
                   for purpose in ('source_inventory', 'environment_inventory', 'python_base_inventory'))):
        raise TrainingValidationError('validation_native_qualification_not_for_selection')


@dataclass(frozen=True)
class PreparedValidation:
    plan: TrainingProcessPlan
    request_path: Path
    request_sha256: str
    request: dict
    candidate: bool = False
    review_path: Path | None = None
    review_sha256: str = ''


def retain_prepared(prepared: PreparedValidation):
    """No-clobber exact process review outside its not-yet-created workspace."""
    if type(prepared) is not PreparedValidation or prepared.review_path is not None:
        raise TrainingValidationError('validation_review_invalid')
    path = Path(prepared.plan.root) / (prepared.plan.name + '.validation-review.json')
    payload = {'schema_version': 1, 'kind': 'sdxl_heldout_validation_review',
        'plan': asdict(prepared.plan), 'request_path': str(prepared.request_path),
        'request_sha256': prepared.request_sha256, 'candidate': prepared.candidate}
    raw = _bytes(payload)
    if len(raw) > MAX_REQUEST:
        raise TrainingValidationError('validation_review_too_large')
    _write_new(path, raw)
    if _read(path, limit=MAX_REQUEST) != raw:
        raise TrainingValidationError('validation_review_readback_failed')
    return PreparedValidation(prepared.plan, prepared.request_path, prepared.request_sha256,
        prepared.request, prepared.candidate, path, hashlib.sha256(raw).hexdigest())


def restore_prepared(path, expected_sha256):
    """Restore an exact retained evaluation review; never start the process."""
    raw = _read(Path(path), limit=MAX_REQUEST)
    if not SHA.fullmatch(expected_sha256 or '') or hashlib.sha256(raw).hexdigest() != expected_sha256:
        raise TrainingValidationError('validation_review_changed')
    value = json.loads(raw)
    if (type(value) is not dict or set(value) != {'schema_version', 'kind', 'plan',
            'request_path', 'request_sha256', 'candidate'} or value['schema_version'] != 1
            or value['kind'] != 'sdxl_heldout_validation_review' or _bytes(value) != raw
            or type(value['candidate']) is not bool):
        raise TrainingValidationError('validation_review_invalid')
    fields = value['plan']
    fields['inputs'] = tuple(ProcessInput(**{**row, 'state': tuple(row['state'])})
                             for row in fields['inputs'])
    fields['inventories'] = tuple(ProcessInventory(**row) for row in fields['inventories'])
    fields['command'] = tuple(fields['command'])
    fields['root_identity'] = tuple(fields['root_identity'])
    plan = TrainingProcessPlan(**fields)
    plan.validate()
    request_path = Path(value['request_path']).absolute()
    if (request_path != Path(plan.root) / (plan.name + '.validation-request.json')
            or not SHA.fullmatch(value['request_sha256'])
            or ProcessInput.capture(request_path).sha256 != value['request_sha256']
            or not any(item.path == str(request_path) and item.sha256 == value['request_sha256']
                       for item in plan.inputs)):
        raise TrainingValidationError('validation_review_request_changed')
    request_raw = _read(request_path, limit=MAX_REQUEST)
    request = json.loads(request_raw)
    if (_bytes(request) != request_raw or _digest(request) != plan.preview_fingerprint
            or request.get('qualification') != ('candidate_only' if value['candidate'] else 'product')):
        raise TrainingValidationError('validation_review_request_changed')
    return PreparedValidation(plan, request_path, value['request_sha256'], request,
        value['candidate'], Path(path), expected_sha256)


@dataclass(frozen=True)
class TrainingValidationOutcome:
    status: str
    message: str
    process: TrainingProcessResult | None = None
    report: dict | None = None


class TrainingValidationService:
    """The only product process owner for this separate evaluation."""

    def __init__(self, *, process=None):
        self.process = process or TrainingProcessService()

    def prepare(self, training_plan, contract, verified, *, root, name, interpreter,
                source_root, source_inventory, environment_root, environment_inventory,
                python_base_root, python_base_inventory, package_inventory,
                should_cancel=None):
        return self._prepare(training_plan, contract, verified, root=root, name=name,
            interpreter=interpreter, source_root=source_root, source_inventory=source_inventory,
            environment_root=environment_root, environment_inventory=environment_inventory,
            python_base_root=python_base_root, python_base_inventory=python_base_inventory,
            package_inventory=package_inventory, should_cancel=should_cancel, candidate=False)

    def prepare_candidate(self, training_plan, contract, verified, **kwargs):
        """Qualifier-only candidate; its receipt can never become product readiness."""
        return self._prepare(training_plan, contract, verified, candidate=True, **kwargs)

    def _prepare(self, training_plan, contract, verified, *, root, name, interpreter,
                 source_root, source_inventory, environment_root, environment_inventory,
                 python_base_root, python_base_inventory, package_inventory,
                 should_cancel=None, candidate):
        profile = None if candidate else qualified_profile()
        if not candidate and profile is None:
            raise TrainingValidationError('validation_native_unqualified')
        if str(Path(interpreter).absolute()) != training_plan.command[0]:
            raise TrainingValidationError('validation_interpreter_changed')
        if type(verified) is VerifiedTrainingResult:
            request = request_for_verified(training_plan, contract, verified, should_cancel=should_cancel)
        elif type(verified) is tuple and len(verified) == 2:
            request = request_for_checkpoint_export(training_plan, contract,
                verified[0], verified[1], should_cancel=should_cancel)
        else:
            raise TrainingValidationError('validation_model_origin_invalid')
        request = deepcopy(request)
        request['qualification'] = 'candidate_only' if candidate else 'product'
        if not candidate:
            _qualification_matches_request(profile, request, contract)
        source_root = Path(source_root).absolute()
        environment_root = Path(environment_root).absolute()
        package = TrainingPackageService().verify(request['package_path'], should_cancel=should_cancel)
        if not package.valid or package.package_fingerprint != request['package_fingerprint']:
            raise TrainingValidationError('validation_package_changed')
        configuration = TrainingConfiguration.from_dict(package.manifest['training_configuration'])
        bound_inventories = {row['purpose']: row for row in contract['process']['inventories']}
        selected_inventories = {
            'source_inventory': (source_root, source_inventory),
            'environment_inventory': (environment_root, environment_inventory),
            'python_base_inventory': (python_base_root, python_base_inventory),
            'package_inventory': (request['package_path'], package_inventory),
        }
        if set(bound_inventories) != set(selected_inventories) or any(
            str(Path(root_path).absolute()) != bound_inventories[purpose]['root']
            or str(Path(manifest_path).absolute()) != bound_inventories[purpose]['manifest_path']
            or ProcessInput.capture(manifest_path, should_cancel).sha256 != bound_inventories[purpose]['manifest_sha256']
            for purpose, (root_path, manifest_path) in selected_inventories.items()):
            raise TrainingValidationError('validation_software_selection_changed')
        request['native'] = {
            'source_root': str(source_root), 'environment_root': str(environment_root),
            'model_catalog_path': str(Path(__file__).resolve().parents[1] / 'tools' / 'training_sdxl_layout.json'),
            'max_caption_tokens': configuration.max_caption_tokens,
            'weight_dtype': contract['review']['trainer_arguments']['mixed_precision'],
            'source_revision': contract['recipe']['source_revision'],
            'runtime_profile': contract['recipe']['runtime_profile'],
            'source_inventory_sha256': bound_inventories['source_inventory']['manifest_sha256'],
            'environment_inventory_sha256': bound_inventories['environment_inventory']['manifest_sha256'],
            'python_base_inventory_sha256': bound_inventories['python_base_inventory']['manifest_sha256'],
        }
        if len(_bytes(request)) > MAX_REQUEST:
            raise TrainingValidationError('validation_request_too_large')
        root = Path(root).absolute()
        path = root / (name + '.validation-request.json')
        raw = _bytes(request)
        _assert_unredirected(path)
        _write_new(path, raw)
        input_paths = [WORKER, __file__, *OWNED_NATIVE_CODE.values(),
            *((PROFILE_FILE, EVIDENCE_FILE) if not candidate else ()),
            path, request['model_path'], request['base_path'],
            request['native']['model_catalog_path'],
        ]
        for slot in request['tokenizers']:
            input_paths.extend(Path(slot['path']) / item['name'] for item in slot['files'])
        for case in request['cases']:
            input_paths.extend((Path(request['package_path']) / case['image_path'],
                                Path(request['package_path']) / case['caption_path']))
        command = (str(Path(interpreter).absolute()), '-I', '-S', '-B', '-X', 'utf8', str(WORKER), str(path))
        inventories = ((source_root, source_inventory), (environment_root, environment_inventory),
                       (python_base_root, python_base_inventory), (request['package_path'], package_inventory))
        try:
            plan = self.process.prepare(root, name, command, inputs=input_paths,
                inventories=inventories, preview_fingerprint=_digest(request),
                timeout_seconds=1200 if candidate else 3600, environment_profile=LOCAL_ENVIRONMENT,
                should_cancel=should_cancel)
        except BaseException:
            # Retain exact request for explicit recovery rather than silently reuse the name.
            raise
        return retain_prepared(PreparedValidation(plan, path, hashlib.sha256(raw).hexdigest(), request, candidate))

    def execute(self, prepared: PreparedValidation, jobs, *, confirmed_fingerprint, should_cancel=None):
        if type(prepared) is not PreparedValidation or prepared.candidate or prepared.request.get('qualification') != 'product':
            raise TrainingValidationError('validation_candidate_not_product')
        profile = qualified_profile()
        if profile is None:
            raise TrainingValidationError('validation_native_unqualified')
        evidence, native = profile['native_evidence'], prepared.request['native']
        if (prepared.request['base_sha256'] != evidence['base_sha256']
                or any(native[key] != evidence[key] for key in (
                    'source_revision', 'runtime_profile', 'source_inventory_sha256',
                    'environment_inventory_sha256', 'python_base_inventory_sha256'))):
            raise TrainingValidationError('validation_native_qualification_not_for_selection')
        return self._execute(prepared, jobs, confirmed_fingerprint=confirmed_fingerprint,
            should_cancel=should_cancel)

    def run_candidate(self, prepared: PreparedValidation, jobs, *, confirmed_fingerprint, should_cancel=None):
        """Execute a visibly unqualified native candidate for coordinator review."""
        if type(prepared) is not PreparedValidation or not prepared.candidate or prepared.request.get('qualification') != 'candidate_only':
            raise TrainingValidationError('validation_candidate_invalid')
        return self._execute(prepared, jobs, confirmed_fingerprint=confirmed_fingerprint,
            should_cancel=should_cancel)

    def _execute(self, prepared, jobs, *, confirmed_fingerprint, should_cancel):
        if confirmed_fingerprint != prepared.plan.fingerprint:
            raise TrainingValidationError('validation_confirmation_changed')
        raw = _read(prepared.request_path, limit=MAX_REQUEST)
        if raw != _bytes(prepared.request) or hashlib.sha256(raw).hexdigest() != prepared.request_sha256:
            raise TrainingValidationError('validation_request_changed')
        outcome = self.process.execute(prepared.plan, confirmed_fingerprint, jobs,
            should_cancel=should_cancel)
        if not outcome.tree_stopped:
            return TrainingValidationOutcome('recovery_required', 'Evaluation process ownership requires recovery.', outcome)
        if outcome.status == 'cancelled':
            return TrainingValidationOutcome('cancelled', 'Held-out evaluation was cancelled.', outcome)
        if outcome.status != 'exited_unverified' or outcome.exit_code != 0:
            return TrainingValidationOutcome('failed', 'Held-out evaluation failed; no score is available.', outcome)
        report = read_report(prepared, outcome)
        if prepared.candidate:
            return TrainingValidationOutcome('candidate_complete',
                'Native candidate measured; product validation remains unqualified.', outcome, report)
        return TrainingValidationOutcome('complete', 'Held-out denoising MSE measured.', outcome, report)

    def recover(self, prepared: PreparedValidation):
        """Reconcile only this process owner and reopen an exact complete receipt."""
        if prepared.review_path is None:
            raise TrainingValidationError('validation_review_missing')
        fresh = restore_prepared(prepared.review_path, prepared.review_sha256)
        if fresh.plan != prepared.plan or fresh.request != prepared.request:
            raise TrainingValidationError('validation_review_changed')
        rows, tip = TrainingRunJournal(fresh.plan.destination).read()
        if not rows:
            raise TrainingValidationError('validation_process_missing')
        stopped = self.process.recover(fresh.plan.destination, tip)
        rows, tip = TrainingRunJournal(fresh.plan.destination).read()
        if not stopped.tree_stopped or stopped.journal_tip != tip:
            return TrainingValidationOutcome('recovery_required',
                'The evaluator process still needs recovery.', stopped)
        if rows[-1]['state'] == 'exited_unverified' and rows[-1]['details'].get('exit_code') == 0:
            outcome = TrainingProcessResult('exited_unverified', fresh.plan.destination,
                rows[-1]['details'].get('message', ''), 0, True, tip)
            report = read_report(fresh, outcome)
            return TrainingValidationOutcome('candidate_complete' if fresh.candidate else 'complete',
                ('Retained native candidate rechecked; product validation remains unqualified.'
                 if fresh.candidate else 'Retained held-out measurement rechecked.'), outcome, report)
        return TrainingValidationOutcome(rows[-1]['state'],
            'The stopped evaluator has no complete validated measurement.', stopped)


def validate_measurements(request, values):
    if type(values) is not list or len(values) != len(request['cases']):
        raise TrainingValidationError('validation_measurements_incomplete')
    observed = []
    for case, row in zip(request['cases'], values):
        if (type(row) is not dict or set(row) != {'stable_id', 'mse_by_timestep'}
                or row['stable_id'] != case['stable_id'] or type(row['mse_by_timestep']) is not list
                or len(row['mse_by_timestep']) != len(TIMESTEPS)):
            raise TrainingValidationError('validation_measurements_changed')
        for value in row['mse_by_timestep']:
            if type(value) not in {int, float} or not math.isfinite(value) or value < 0:
                raise TrainingValidationError('validation_measurements_nonfinite')
            observed.append(float(value))
    mean = math.fsum(observed) / len(observed)
    if not math.isfinite(mean):
        raise TrainingValidationError('validation_measurements_nonfinite')
    return mean


def _current_inputs(request, should_cancel=None):
    verification = TrainingPackageService().verify(request['package_path'], should_cancel=should_cancel)
    if (not verification.valid or verification.package_fingerprint != request['package_fingerprint']
            or list(_cases(verification, request['training_process_fingerprint'])) != request['cases']):
        raise TrainingValidationError('validation_package_changed')
    for path, expected in ((request['model_path'], request['model_sha256']),
                           (request['base_path'], request['base_sha256'])):
        if ProcessInput.capture(path, should_cancel).sha256 != expected:
            raise TrainingValidationError('validation_resource_changed')
    for slot in request['tokenizers']:
        for row in slot['files']:
            if ProcessInput.capture(Path(slot['path']) / row['name'], should_cancel).sha256 != row['sha256']:
                raise TrainingValidationError('validation_tokenizer_changed')


def read_report(prepared: PreparedValidation, outcome: TrainingProcessResult):
    if not outcome.tree_stopped or outcome.status != 'exited_unverified' or outcome.exit_code != 0:
        raise TrainingValidationError('validation_process_unverified')
    rows, tip = TrainingRunJournal(prepared.plan.destination).read()
    if not rows or rows[-1]['state'] != 'exited_unverified' or tip != outcome.journal_tip:
        raise TrainingValidationError('validation_process_changed')
    raw = _read(prepared.plan.destination / 'work' / RESULT_NAME, limit=MAX_RESULT)
    value = json.loads(raw)
    required = {'schema_version', 'kind', 'profile', 'request_sha256', 'process_fingerprint',
        'metric', 'qualification', 'timesteps', 'values', 'mean_mse', 'elapsed_seconds', 'peak_cuda_memory_bytes'}
    if (type(value) is not dict or set(value) != required or _bytes(value) != raw
            or value['schema_version'] != 1 or value['kind'] != 'sdxl_heldout_validation_result'
            or value['profile'] != PROFILE or value['request_sha256'] != prepared.request_sha256
            or value['process_fingerprint'] != prepared.plan.fingerprint
            or value['qualification'] != prepared.request['qualification']
            or value['metric'] != 'epsilon_denoising_mse' or value['timesteps'] != list(TIMESTEPS)):
        raise TrainingValidationError('validation_receipt_changed')
    mean = validate_measurements(prepared.request, value['values'])
    if (type(value['mean_mse']) not in {int, float} or value['mean_mse'] != mean
            or type(value['elapsed_seconds']) not in {int, float}
            or not math.isfinite(value['elapsed_seconds']) or value['elapsed_seconds'] <= 0
            or type(value['peak_cuda_memory_bytes']) is not int or value['peak_cuda_memory_bytes'] <= 0):
        raise TrainingValidationError('validation_receipt_invalid')
    _current_inputs(prepared.request)
    return value
