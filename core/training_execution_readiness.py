"""Fresh native runtime/device/tokenizer observations for the SDXL execution path.

Preparation and inspection run in coordinated workers. They never download,
load selected model weights, train, activate software or register a model.
The reviewed execution profile remains the separate application launch gate.
"""
from __future__ import annotations

from copy import deepcopy
from dataclasses import asdict, dataclass
import hashlib
import json
import os
from pathlib import Path
import re
from uuid import uuid4


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / 'tools/training_execution_readiness_worker.py'
MAX_RECEIPT = 4 * 1024**2
KIND = 'sdxl_execution_readiness_request'
RESULT_KIND = 'sdxl_execution_native_readiness'
RECEIPT_KIND = 'sdxl_execution_readiness_evidence'
FLAGS = ('-I', '-S', '-B', '-X', 'utf8')
SHA = re.compile(r'[0-9a-f]{64}\Z')


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict(raw):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('training_readiness_duplicate_field')
            result[key] = value
        return result
    value = json.loads(raw, object_pairs_hook=pairs)
    if len(raw) > MAX_RECEIPT or canonical(value) != raw:
        raise ValueError('training_readiness_noncanonical_record')
    return value


def code_paths():
    from .training_execution_plan import OWNED_CODE
    return {**OWNED_CODE, 'core/training_execution_readiness.py': Path(__file__).absolute(),
        'tools/training_execution_worker.py': ROOT / 'tools/training_execution_worker.py'}


def validate_request(request):
    from .training_runtime_contract import SOURCE_REVISION, validate_configuration
    from .training_tokenizer_files import TOKENIZER_FILES
    if (type(request) is not dict or set(request) != {'schema_version', 'kind', 'workspace', 'process',
            'selection', 'expected_packages', 'source_revision'}
            or type(request['schema_version']) is not int or request['schema_version'] != 1
            or request['kind'] != KIND or request['source_revision'] != SOURCE_REVISION):
        raise ValueError('training_readiness_request_invalid')
    selection = request['selection']
    if type(selection) is not dict or set(selection) != {'qualification', 'base_id', 'tokenizers_root',
            'tokenizer_identities', 'configuration'}:
        raise ValueError('training_readiness_selection_invalid')
    qualification = selection['qualification']
    if (type(qualification) is not dict or set(qualification) != {'profile', 'evidence_sha256', 'runtime_profile'}
            or any(type(value) is not str or not value for value in qualification.values())
            or not SHA.fullmatch(qualification['evidence_sha256'])
            or type(selection['base_id']) is not str or not re.fullmatch(r'sha256:[0-9a-f]{64}', selection['base_id'])
            or type(selection['tokenizer_identities']) is not list or len(selection['tokenizer_identities']) != 2
            or any(type(value) is not str or not re.fullmatch(r'clip-files-v1:[0-9a-f]{64}', value)
                   for value in selection['tokenizer_identities'])):
        raise ValueError('training_readiness_selection_invalid')
    validate_configuration(selection['configuration'])
    token_root = Path(selection['tokenizers_root'])
    if not token_root.is_absolute() or token_root != token_root.absolute():
        raise ValueError('training_readiness_tokenizer_location_invalid')
    packages = request['expected_packages']
    if (type(packages) is not dict or not 1 <= len(packages) <= 100
            or any(type(key) is not str or not re.fullmatch(r'[a-z0-9-]+', key)
                   or (value is not None and (type(value) is not str or not value)) for key, value in packages.items())):
        raise ValueError('training_readiness_packages_invalid')
    process = request['process']
    if (type(process) is not dict or set(process) != {'command', 'inputs', 'contract_input_index', 'inventories', 'environment_profile'}
            or process['environment_profile'] != 'local-processing-v1'
            or type(process['inputs']) is not list or not 1 <= len(process['inputs']) < 128
            or process['contract_input_index'] != len(process['inputs'])
            or len(process['inventories']) != 3):
        raise ValueError('training_readiness_process_invalid')
    names = [row['purpose'] for row in process['inventories']]
    if names != ['source_inventory', 'environment_inventory', 'python_base_inventory']:
        raise ValueError('training_readiness_inventory_roles_invalid')
    roots = [Path(row['root']) for row in process['inventories']]
    if any(not root.is_absolute() for root in roots) or any(
            left.is_relative_to(right) for index, left in enumerate(roots) for right in roots[index + 1:]
            ) or any(right.is_relative_to(left) for index, left in enumerate(roots) for right in roots[index + 1:]):
        raise ValueError('training_readiness_roots_overlap')
    rows = process['inputs']
    if [row['index'] for row in rows] != list(range(len(rows))):
        raise ValueError('training_readiness_input_order_invalid')
    token_paths = [str(token_root / slot / name) for slot in ('tokenizer', 'tokenizer_2') for name, _ in TOKENIZER_FILES]
    if [row['path'] for row in rows if row['purpose'] == 'tokenizer'] != token_paths:
        raise ValueError('training_readiness_tokenizer_inputs_invalid')
    return request


def validate_native_result(value, request, fingerprint):
    from .training_reference_tokenizers import PROFILE, VOCABULARY_SIZE, MERGE_COUNT
    from .training_tokenizer_contract import BOS, EOS, WIDTH
    if (type(value) is not dict or set(value) != {'schema_version', 'kind', 'process_fingerprint',
            'request_sha256', 'device_observation', 'runtime', 'tokenizers', 'configuration_sha256', 'denials'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['kind'] != RESULT_KIND or value['process_fingerprint'] != fingerprint
            or value['request_sha256'] != digest(request) or value['denials'] != []
            or value['configuration_sha256'] != digest(request['selection']['configuration'])):
        raise ValueError('training_readiness_native_result_invalid')
    runtime = value['runtime']
    if (type(runtime) is not dict or set(runtime) != {'python_version', 'packages'}
            or runtime['python_version'] != '3.11.14' or type(runtime['packages']) is not dict
            or runtime['packages'].keys() != request['expected_packages'].keys()
            or any(type(version) is not str or not version for version in runtime['packages'].values())
            or any(expected is not None and runtime['packages'][name] != expected
                   for name, expected in request['expected_packages'].items())):
        raise ValueError('training_readiness_runtime_changed')
    device = value['device_observation']
    if (type(device) is not dict or set(device) != {'device_type', 'cuda_index', 'name', 'compute_capability',
            'total_memory_bytes', 'torch_version', 'cuda_version'}
            or device['device_type'] != 'cuda' or type(device['cuda_index']) is not int or device['cuda_index'] != 0
            or type(device['name']) is not str or not device['name']
            or type(device['compute_capability']) is not list or len(device['compute_capability']) != 2
            or any(type(part) is not int or part < 0 for part in device['compute_capability'])
            or device['compute_capability'][0] < 8
            or type(device['total_memory_bytes']) is not int or device['total_memory_bytes'] < 1024**3
            or device['torch_version'] != runtime['packages']['torch']
            or type(device['cuda_version']) is not str or not device['cuda_version']):
        raise ValueError('training_readiness_device_unsupported')
    report = value['tokenizers']
    if (type(report) is not dict or set(report) != {'schema_version', 'kind', 'profile', 'slots'}
            or report['schema_version'] != 1 or report['kind'] != 'sdxl_reference_tokenizer_construction'
            or report['profile'] != PROFILE or type(report['slots']) is not list or len(report['slots']) != 2):
        raise ValueError('training_readiness_tokenizer_result_invalid')
    inputs = {row['path']: row for row in request['process']['inputs']}
    from .training_tokenizer_files import TOKENIZER_FILES
    for role, row in enumerate(report['slots'], 1):
        root = Path(request['selection']['tokenizers_root']) / ('tokenizer' if role == 1 else 'tokenizer_2')
        files = [{'name': name, 'bytes': inputs[str(root / name)]['state'][2], 'sha256': inputs[str(root / name)]['sha256']}
                 for name, _ in TOKENIZER_FILES]
        expected = {'role': role, 'identity': request['selection']['tokenizer_identities'][role - 1],
            'files': files, 'vocabulary_size': VOCABULARY_SIZE, 'merge_count': MERGE_COUNT,
            'model_max_length': WIDTH, 'bos_token_id': BOS, 'eos_token_id': EOS, 'unk_token_id': EOS,
            'pad_token_id': EOS if role == 1 else 0}
        if canonical(row) != canonical(expected):
            raise ValueError('training_readiness_tokenizer_result_invalid')
    return value


@dataclass(frozen=True)
class NativeReadinessPreparation:
    plan: object
    request: dict


class TrainingExecutionReadinessService:
    def __init__(self, *, process=None, run_index=None):
        from .training_process import TrainingProcessService
        self.process = process or TrainingProcessService()
        self.run_index = run_index
        self.index_recovery_required = False
        self.process_invoked = False

    def prepare(self, source, environment, python_base, python, tokenizers_root, configuration,
                qualification, base_id, preparation_root, run_root, run_name, *, should_cancel=None, progress=None):
        from .training_file_inventory import SoftwareInventoryService
        from .training_process import ProcessInput, ProcessInventory, TrainingProcessPlan, _directory, _write_new
        from .training_runtime_qualification import PROFILES
        from .training_runtime_contract import SOURCE_REVISION
        from .training_reference_tokenizers import inspect_reference_slot
        from .training_tokenizer_files import TOKENIZER_FILES, read_slot, files_identity
        from .training_source_io import _assert_unredirected
        roots = [Path(path).absolute() for path in (source, environment, python_base)]
        preparation_root, run_root = Path(preparation_root).absolute(), Path(run_root).absolute()
        for root in (*roots, preparation_root, run_root): _assert_unredirected(root)
        if any(preparation_root.is_relative_to(root) or run_root.is_relative_to(root) for root in roots):
            raise ValueError('training_readiness_output_overlap')
        if should_cancel and should_cancel(): raise InterruptedError('Training readiness cancelled.')
        preparation_root.mkdir(exist_ok=False)
        _directory(run_root)
        bindings, inventory_rows, indexed = [], [], [(Path(python).absolute(), 'python', '0'), (WORKER, 'worker', '0')]
        for purpose, root in zip(('source', 'environment', 'python_base'), roots):
            inventory = SoftwareInventoryService().capture(root, should_cancel=should_cancel, progress=progress)
            path = preparation_root / (purpose + '-inventory.json')
            SoftwareInventoryService.save(inventory, path)
            captured = ProcessInput.capture(path, should_cancel)
            item = ProcessInventory(str(root), str(path), captured.sha256, len(inventory.files), inventory.byte_count)
            bindings.append(item)
            inventory_rows.append({'purpose': purpose + '_inventory', **asdict(item),
                'inventory_sha256': inventory.fingerprint})
            indexed.append((path, purpose + '_inventory', '0'))
        tokenizers_root = Path(tokenizers_root).absolute()
        identities = []
        for role, slot in enumerate(('tokenizer', 'tokenizer_2'), 1):
            blobs = read_slot(tokenizers_root / slot)
            inspect_reference_slot(blobs, role)
            identities.append(files_identity(blobs))
            indexed.extend((tokenizers_root / slot / name, 'tokenizer', str(role) + '/' + name) for name, _ in TOKENIZER_FILES)
        indexed.extend((path, 'owned_code', name) for name, path in code_paths().items())
        inputs, rows = [], []
        for index, (path, purpose, slot) in enumerate(indexed):
            captured = ProcessInput.capture(path, should_cancel)
            inputs.append(captured)
            rows.append({'index': index, 'purpose': purpose, 'slot': slot, 'path': captured.path,
                         'state': list(captured.state), 'sha256': captured.sha256})
        request_path = preparation_root / 'readiness-request.json'
        command = (str(Path(python).absolute()), *FLAGS, str(WORKER), str(request_path))
        request = {'schema_version': 1, 'kind': KIND, 'source_revision': SOURCE_REVISION,
            'workspace': {'root': str(run_root), 'root_identity': list(_directory(run_root)), 'name': run_name},
            'process': {'command': list(command), 'inputs': rows, 'contract_input_index': len(rows),
                'inventories': inventory_rows, 'environment_profile': 'local-processing-v1'},
            'selection': {'qualification': deepcopy(qualification), 'base_id': base_id,
                'tokenizers_root': str(tokenizers_root), 'tokenizer_identities': identities,
                'configuration': deepcopy(configuration)},
            'expected_packages': deepcopy(PROFILES[qualification['runtime_profile']])}
        validate_request(request)
        _write_new(request_path, canonical(request))
        inputs.append(ProcessInput.capture(request_path, should_cancel))
        plan = TrainingProcessPlan(uuid4().hex, str(run_root), _directory(run_root), run_name, command,
            tuple(inputs), digest(request), 600, 8 * 1024**2, 3, tuple(bindings), 'local-processing-v1')
        plan.validate()
        if os.path.lexists(plan.destination): raise ValueError('training_readiness_run_occupied')
        return NativeReadinessPreparation(plan, request)

    def execute(self, prepared, confirmed_fingerprint, jobs, *, should_cancel=None):
        self.process_invoked = False
        if self.run_index is None:
            return self._execute(prepared, confirmed_fingerprint, jobs, should_cancel=should_cancel)
        if confirmed_fingerprint != prepared.plan.fingerprint:
            raise ValueError('training_readiness_confirmation_changed')
        from .training_run_recovery import retain_readiness, register_review, note_outcome
        path, sha256 = retain_readiness(prepared)
        register_review(self.run_index, prepared.plan, 'readiness', path, sha256)
        self.index_recovery_required = False
        try:
            outcome, receipt = self._execute(prepared, confirmed_fingerprint, jobs, should_cancel=should_cancel)
        except BaseException:
            self.index_recovery_required = not note_outcome(self.run_index, prepared.plan,
                'recovery_required' if self.process_invoked else 'failed')
            raise
        phase = ('ready' if receipt is not None else 'held' if outcome.status == 'held'
                 else 'recovery_required' if not outcome.tree_stopped or outcome.status == 'recovery_required' else
                 outcome.status if outcome.status in {'failed', 'cancelled', 'interrupted'} else 'failed')
        self.index_recovery_required = not note_outcome(self.run_index, prepared.plan,
            phase, getattr(outcome, 'journal_tip', ''))
        return outcome, receipt

    def _execute(self, prepared, confirmed_fingerprint, jobs, *, should_cancel=None):
        from .training_process import _read, _write_new
        from .training_verification import verified_receipt_hashes
        plan, request = prepared.plan, prepared.request
        if confirmed_fingerprint != plan.fingerprint or digest(request) != plan.preview_fingerprint:
            raise ValueError('training_readiness_confirmation_changed')
        def entering_owner():
            self.process_invoked = True
        options = {}
        if getattr(self.process, "reports_process_invocation", False):
            options["invocation_observer"] = entering_owner
        else:
            entering_owner()
        outcome = self.process.execute(plan, confirmed_fingerprint, jobs, should_cancel=should_cancel, **options)
        if outcome.status != 'exited_unverified' or outcome.exit_code != 0 or not outcome.tree_stopped:
            return outcome, None
        native = strict(_read(plan.destination / 'work/readiness-result.json', limit=MAX_RECEIPT))
        validate_native_result(native, request, plan.fingerprint)
        value = {'schema_version': 1, 'kind': RECEIPT_KIND, 'plan': asdict(plan), 'request': request,
            'native': native, 'native_sha256': digest(native), 'process_journal_tip': outcome.journal_tip,
            'input_verification_sha256': verified_receipt_hashes(plan)}
        path = plan.destination / 'native-readiness-evidence.json'
        _write_new(path, canonical(value))
        if _read(path, limit=MAX_RECEIPT) != canonical(value): raise ValueError('training_readiness_evidence_readback')
        return outcome, {'path': str(path), 'sha256': digest(value)}


def validate_reference_admission(reference_admission, native_receipt_bytes):
    """Recheck a selected stopped native measurement before joining its facts."""
    from .training_process import (ProcessInput, ProcessInventory, TrainingProcessPlan, TrainingProcessService,
                                  TrainingRunJournal, _read, _bytes)
    from .training_verification import verified_receipt_hashes
    value = strict(native_receipt_bytes)
    if (type(value) is not dict or set(value) != {'schema_version', 'kind', 'plan', 'request', 'native',
            'native_sha256', 'process_journal_tip', 'input_verification_sha256'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1 or value['kind'] != RECEIPT_KIND
            or hashlib.sha256(native_receipt_bytes).hexdigest() != reference_admission['native_readiness_receipt']['sha256']):
        raise ValueError('training_readiness_evidence_invalid')
    fields = deepcopy(value['plan'])
    fields['inputs'] = tuple(ProcessInput(**{**row, 'state': tuple(row['state'])}) for row in fields['inputs'])
    fields['inventories'] = tuple(ProcessInventory(**row) for row in fields['inventories'])
    fields['command'], fields['root_identity'] = tuple(fields['command']), tuple(fields['root_identity'])
    plan = TrainingProcessPlan(**fields)
    plan.validate()
    request = validate_request(value['request'])
    if (plan.command[6] != str(WORKER) or plan.preview_fingerprint != digest(request)
            or list(plan.command) != request['process']['command'] or plan.command[1:6] != FLAGS):
        raise ValueError('training_readiness_plan_changed')
    rows, tip = TrainingRunJournal(plan.destination).read()
    if (not rows or tip != value['process_journal_tip'] or rows[-1]['state'] != 'exited_unverified'
            or rows[-1]['details'].get('exit_code') != 0 or rows[-1]['details'].get('tree_stopped') is not True
            or _bytes(rows[0]['details'].get('plan')) != _bytes(TrainingProcessService._receipt(plan))
            or rows[0]['details'].get('fingerprint') != digest(TrainingProcessService._receipt(plan))
            or TrainingProcessService().backend.active_named(plan.owner_name)
            or verified_receipt_hashes(plan) != value['input_verification_sha256']):
        raise ValueError('training_readiness_process_evidence_changed')
    native_raw = _read(plan.destination / 'work/readiness-result.json', limit=MAX_RECEIPT)
    if native_raw != canonical(value['native']) or hashlib.sha256(native_raw).hexdigest() != value['native_sha256']:
        raise ValueError('training_readiness_native_result_changed')
    native = validate_native_result(value['native'], request, plan.fingerprint)
    selection, qualification = request['selection'], request['selection']['qualification']
    expected = {'qualification_profile': qualification['profile'], 'qualification_evidence_sha256': qualification['evidence_sha256'],
        'runtime_profile': qualification['runtime_profile'], 'source_revision': request['source_revision'],
        'base_id': selection['base_id'], 'tokenizer_identities': [
            {'role': role, 'identity': identity} for role, identity in enumerate(selection['tokenizer_identities'], 1)],
        'device_observation_sha256': digest(native['device_observation'])}
    if any(reference_admission[key] != item for key, item in expected.items()):
        raise ValueError('training_readiness_admission_changed')
    inventories = {row['purpose'].removesuffix('_inventory'): row['inventory_sha256'] for row in request['process']['inventories']}
    if reference_admission['software_inventories'] != inventories:
        raise ValueError('training_readiness_inventory_evidence_changed')
    TrainingProcessService._verify_inputs(plan)
    return value
