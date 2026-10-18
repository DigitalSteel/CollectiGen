"""Installation-only observations for an explicitly selected portable runtime.

This callback never establishes training readiness. All work is retained, uses
the caller's shared process coordinator, and requires stopped-process evidence
plus unchanged software before a healthy observation can be returned.
"""
from __future__ import annotations

from dataclasses import asdict, dataclass
from email.parser import BytesParser
import hashlib
import json
import math
from pathlib import Path
import re
import time
from uuid import uuid4

from .trainer_environment import TrainerHealth, TrainerProfile, _verify_software, _write_json
from .training_file_inventory import MAX_MANIFEST_BYTES, MAX_SECONDS, SoftwareInventoryService, _control
from .training_process import (ProcessInput, ProcessInventory, TrainingProcessPlan,
    TrainingProcessService, TrainingRunJournal, _directory, _read, _write_new)
from .training_verification import verified_receipt_hashes
from .training_source_loader import WORKER_MODULES


ROOT = Path(__file__).resolve().parents[1]
WORKER = ROOT / 'tools/trainer_runtime_health_worker.py'
FLAGS = ('-I', '-S', '-B', '-X', 'utf8')
KIND = 'trainer_installation_health_request'
RESULT_KIND = 'trainer_installation_health_observation'
MAX_RECORD = 4 * 1024**2
SHA = re.compile(r'[0-9a-f]{64}\Z')
MODULES = ('torch', 'torch._C', 'transformers', 'safetensors',
           'safetensors._safetensors_rust', 'sdxl_train_network')
LAYOUT = {'source': 'source', 'environment': 'environment', 'python_base': 'python'}


def canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def strict(raw, limit=MAX_RECORD):
    def pairs(items):
        result = {}
        for key, value in items:
            if key in result: raise ValueError('trainer_health_duplicate_field')
            result[key] = value
        return result
    if len(raw) > limit: raise ValueError('trainer_health_record_bound')
    value = json.loads(raw, object_pairs_hook=pairs)
    if canonical(value) != raw: raise ValueError('trainer_health_noncanonical_record')
    return value


def code_paths():
    # Complete stdlib-only bootstrap closure; no app controller, model or recipe.
    core = WORKER_MODULES['health']
    tools = ('training_execution_worker.py', 'training_runtime_probe.py', 'training_verification_worker.py')
    return {**{'core/' + name + '.py': ROOT / 'core' / (name + '.py') for name in core},
            **{'tools/' + name: ROOT / 'tools' / name for name in tools}}


def _keys(value, fields):
    if type(value) is not dict or set(value) != set(fields):
        raise ValueError('trainer_health_schema_invalid')


def _sha(value):
    if type(value) is not str or not SHA.fullmatch(value):
        raise ValueError('trainer_health_identity_invalid')


def packages_from_profile(root, profile):
    """Read only declared, hash-pinned METADATA files, never search installations."""
    packages = {}
    prefix = 'environment/Lib/site-packages/'
    for row in profile.files:
        name = row['path']
        if not name.startswith(prefix) or not name.endswith('.dist-info/METADATA'): continue
        relative = name.removeprefix(prefix)
        if len(Path(relative).parts) != 2 or row['size_bytes'] > MAX_RECORD:
            raise ValueError('trainer_health_metadata_layout_invalid')
        raw = _read(Path(root) / name, limit=MAX_RECORD)
        if len(raw) != row['size_bytes'] or hashlib.sha256(raw).hexdigest() != row['sha256']:
            raise ValueError('trainer_health_metadata_changed')
        metadata = BytesParser().parsebytes(raw, headersonly=True)
        if len(metadata.get_all('Name', [])) != 1 or len(metadata.get_all('Version', [])) != 1:
            raise ValueError('trainer_health_metadata_invalid')
        package = re.sub(r'[-_.]+', '-', str(metadata['Name'])).lower()
        version = str(metadata['Version'])
        if (not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', package) or package in packages
                or not re.fullmatch(r'[A-Za-z0-9.+!_-]{1,100}', version) or len(packages) >= 100):
            raise ValueError('trainer_health_metadata_invalid')
        packages[package] = {'version': version, 'metadata': name}
    if not {'torch', 'transformers', 'safetensors'} <= packages.keys():
        raise ValueError('trainer_health_required_packages_missing')
    return packages


def validate_request(request):
    _keys(request, ('schema_version', 'kind', 'workspace', 'process', 'software'))
    if type(request['schema_version']) is not int or request['schema_version'] != 1 or request['kind'] != KIND:
        raise ValueError('trainer_health_request_invalid')
    software = request['software']
    _keys(software, ('root', 'profile_sha256', 'inventory_sha256', 'python_version', 'packages', 'observe_cuda'))
    _sha(software['profile_sha256']); _sha(software['inventory_sha256'])
    root = Path(software['root'])
    if not root.is_absolute() or type(software['observe_cuda']) is not bool:
        raise ValueError('trainer_health_layout_invalid')
    if type(software['python_version']) is not str or not re.fullmatch(r'\d+\.\d+\.\d+', software['python_version']):
        raise ValueError('trainer_health_python_version_invalid')
    packages = software['packages']
    if type(packages) is not dict or not 3 <= len(packages) <= 100 or not {'torch', 'transformers', 'safetensors'} <= packages.keys():
        raise ValueError('trainer_health_packages_invalid')
    for name, row in packages.items():
        _keys(row, ('version', 'metadata'))
        if (not re.fullmatch(r'[a-z0-9]+(?:-[a-z0-9]+)*', name)
                or type(row['version']) is not str or not re.fullmatch(r'[A-Za-z0-9.+!_-]{1,100}', row['version'])
                or type(row['metadata']) is not str
                or not re.fullmatch(r'environment/Lib/site-packages/[^/\\:]+\.dist-info/METADATA', row['metadata'])):
            raise ValueError('trainer_health_packages_invalid')
    workspace = request['workspace']
    _keys(workspace, ('root', 'root_identity', 'name'))
    work_root = Path(workspace['root'])
    if (not work_root.is_absolute() or work_root.is_relative_to(root) or root.is_relative_to(work_root)
            or type(workspace['name']) is not str or not re.fullmatch(r'health-[0-9a-f]{32}', workspace['name'])):
        raise ValueError('trainer_health_workspace_invalid')
    process = request['process']
    _keys(process, ('command', 'inputs', 'contract_input_index', 'inventories', 'environment_profile'))
    rows = process['inputs']
    if (process['environment_profile'] != 'local-processing-v1' or type(rows) is not list
            or not 1 <= len(rows) < 128 or type(process['contract_input_index']) is not int
            or process['contract_input_index'] != len(rows)):
        raise ValueError('trainer_health_process_invalid')
    expected = [(str(root / 'python/python.exe'), 'python', '0'), (str(WORKER), 'worker', '0')]
    preparation = work_root / (workspace['name'] + '-preparation')
    expected += [(str(preparation / 'profile.json'), 'profile', '0')]
    expected += [(str(preparation / (name + '-inventory.json')), name + '_inventory', '0') for name in LAYOUT]
    expected += [(str(path), 'owned_code', name) for name, path in code_paths().items()]
    if len(rows) != len(expected): raise ValueError('trainer_health_owned_inputs_invalid')
    for index, (row, wanted) in enumerate(zip(rows, expected)):
        _keys(row, ('index', 'purpose', 'slot', 'path', 'state', 'sha256'))
        if (type(row['index']) is not int or row['index'] != index
                or (row['path'], row['purpose'], row['slot']) != wanted
                or type(row['state']) is not list or len(row['state']) != 5
                or any(type(part) is not int or part < 0 for part in row['state'])):
            raise ValueError('trainer_health_owned_inputs_invalid')
        _sha(row['sha256'])
    wanted_command = [str(root / 'python/python.exe'), *FLAGS, str(WORKER), str(preparation / 'health-request.json')]
    if process['command'] != wanted_command: raise ValueError('trainer_health_command_invalid')
    inventories = process['inventories']
    if type(inventories) is not list or len(inventories) != 3:
        raise ValueError('trainer_health_inventories_invalid')
    for index, (row, (name, folder)) in enumerate(zip(inventories, LAYOUT.items()), 3):
        _keys(row, ('purpose', 'root', 'manifest_path', 'manifest_sha256', 'file_count', 'byte_count', 'inventory_sha256'))
        if (row['purpose'] != name + '_inventory' or row['root'] != str(root / folder)
                or row['manifest_path'] != rows[index]['path'] or row['manifest_sha256'] != rows[index]['sha256']
                or type(row['file_count']) is not int or not 1 <= row['file_count'] <= 200000
                or type(row['byte_count']) is not int or not 0 <= row['byte_count'] <= 64 * 1024**3):
            raise ValueError('trainer_health_inventories_invalid')
        _sha(row['inventory_sha256'])
    return request


def validate_native_result(value, request, fingerprint, profile):
    _keys(value, ('schema_version', 'kind', 'process_fingerprint', 'request_sha256', 'profile_sha256',
                 'python_version', 'packages', 'module_origins', 'parser', 'cuda', 'denials'))
    software = request['software']
    if (type(value['schema_version']) is not int or value['schema_version'] != 1 or value['kind'] != RESULT_KIND
            or value['process_fingerprint'] != fingerprint or value['request_sha256'] != digest(request)
            or value['profile_sha256'] != profile.sha256 or profile.sha256 != software['profile_sha256']
            or value['python_version'] != software['python_version'] or value['denials'] != []
            or value['packages'] != {name: row['version'] for name, row in software['packages'].items()}
            or canonical(value['parser']) != canonical({'module': 'sdxl_train_network', 'arguments': [], 'parsed': True})):
        raise ValueError('trainer_health_native_result_invalid')
    origins = value['module_origins']
    if type(origins) is not dict or set(origins) != set(MODULES):
        raise ValueError('trainer_health_native_origin_invalid')
    files = {row['path'] for row in profile.files}
    for name, path in origins.items():
        prefix = 'source/' if name == 'sdxl_train_network' else 'environment/Lib/site-packages/' + name.split('.')[0] + '/'
        if type(path) is not str or path not in files or not path.startswith(prefix):
            raise ValueError('trainer_health_native_origin_invalid')
        if name in {'torch._C', 'safetensors._safetensors_rust'} and not path.endswith('.pyd'):
            raise ValueError('trainer_health_native_extension_invalid')
    cuda = value['cuda']
    if not software['observe_cuda']:
        if cuda is not None: raise ValueError('trainer_health_cuda_invalid')
    else:
        if type(cuda) is not dict or type(cuda.get('available')) is not bool:
            raise ValueError('trainer_health_cuda_invalid')
        if not cuda['available']:
            _keys(cuda, ('available',))
        else:
            _keys(cuda, ('available', 'index', 'name', 'compute_capability', 'total_memory_bytes', 'cuda_version'))
            if (type(cuda['index']) is not int or not 0 <= cuda['index'] <= 1024
                    or type(cuda['name']) is not str or not 1 <= len(cuda['name']) <= 256
                    or type(cuda['compute_capability']) is not list or len(cuda['compute_capability']) != 2
                    or any(type(n) is not int or not 0 <= n <= 1000 for n in cuda['compute_capability'])
                    or type(cuda['total_memory_bytes']) is not int or not 0 < cuda['total_memory_bytes'] <= 2**60
                    or type(cuda['cuda_version']) is not str or not re.fullmatch(r'\d+\.\d+(?:\.\d+)?', cuda['cuda_version'])):
                raise ValueError('trainer_health_cuda_invalid')
    return value


@dataclass(frozen=True)
class RuntimeHealthResult:
    health: TrainerHealth
    status: str
    plan: object = None
    outcome: object = None
    receipt_path: Path | None = None
    receipt_sha256: str = ''


class TrainerRuntimeHealthService:
    def __init__(self, jobs, work_root, *, process=None, should_cancel=None,
                 timeout_seconds=120, observe_cuda=True, retain_plan=None):
        if retain_plan is not None and not callable(retain_plan):
            raise ValueError("The trainer health recovery recorder must be callable.")
        self.retain_plan = retain_plan
        if (type(timeout_seconds) not in {int, float} or not math.isfinite(timeout_seconds)
                or not 1 <= timeout_seconds <= 600 or type(observe_cuda) is not bool):
            raise ValueError('trainer_health_controls_invalid')
        self.jobs, self.work_root = jobs, Path(work_root).absolute()
        self.process = process if process is not None else TrainingProcessService()
        self.should_cancel, self.timeout_seconds, self.observe_cuda = should_cancel, timeout_seconds, observe_cuda
        self.last_result = None

    def __call__(self, software_root, profile):
        return self.check(software_root, profile).health

    def _prepare(self, root, profile):
        deadline = time.monotonic() + MAX_SECONDS
        def cancel():
            _control(self.should_cancel, deadline)
            return False
        cancel()
        _directory(root); _directory(self.work_root)
        if root.is_relative_to(self.work_root) or self.work_root.is_relative_to(root):
            raise ValueError('trainer_health_workspace_invalid')
        profile = TrainerProfile.from_dict(profile.payload())
        if profile.payload()['python']['executable'] != 'python/python.exe':
            raise ValueError('trainer_health_layout_invalid')
        if any(Path(row['path']).parts[0] not in set(LAYOUT.values()) for row in profile.files):
            raise ValueError('trainer_health_layout_invalid')
        identity = _verify_software(root, profile, cancel, deadline)
        name = 'health-' + uuid4().hex
        preparation = self.work_root / (name + '-preparation')
        preparation.mkdir(exist_ok=False)
        _directory(preparation)
        profile_path = preparation / 'profile.json'
        # Profiles can list tens of thousands of files; use their manifest
        # writer's bound, not the small process-event record bound.
        _write_json(profile_path, profile.payload())
        indexed = [(root / 'python/python.exe', 'python', '0'), (WORKER, 'worker', '0'), (profile_path, 'profile', '0')]
        bindings, inventory_rows = [], []
        for purpose, folder in LAYOUT.items():
            _control(cancel, deadline)
            selected = root / folder
            inventory = SoftwareInventoryService().capture(selected, should_cancel=cancel)
            manifest = preparation / (purpose + '-inventory.json')
            SoftwareInventoryService.save(inventory, manifest)
            item = ProcessInventory(str(selected), str(manifest), ProcessInput.capture(manifest, cancel).sha256,
                                    len(inventory.files), inventory.byte_count)
            bindings.append(item)
            inventory_rows.append({'purpose': purpose + '_inventory', **asdict(item), 'inventory_sha256': inventory.fingerprint})
            indexed.append((manifest, purpose + '_inventory', '0'))
        indexed.extend((path, 'owned_code', name) for name, path in code_paths().items())
        inputs, rows = [], []
        for index, (path, purpose, slot) in enumerate(indexed):
            _control(cancel, deadline)
            selected = ProcessInput.capture(path, cancel)
            inputs.append(selected)
            rows.append({'index': index, 'purpose': purpose, 'slot': slot, 'path': selected.path,
                         'state': list(selected.state), 'sha256': selected.sha256})
        request_path = preparation / 'health-request.json'
        command = (str(root / 'python/python.exe'), *FLAGS, str(WORKER), str(request_path))
        request = {'schema_version': 1, 'kind': KIND,
            'workspace': {'root': str(self.work_root), 'root_identity': list(_directory(self.work_root)), 'name': name},
            'process': {'command': list(command), 'inputs': rows, 'contract_input_index': len(rows),
                        'inventories': inventory_rows, 'environment_profile': 'local-processing-v1'},
            'software': {'root': str(root), 'profile_sha256': profile.sha256, 'inventory_sha256': identity,
                         'python_version': profile.payload()['python']['version'],
                         'packages': packages_from_profile(root, profile), 'observe_cuda': self.observe_cuda}}
        validate_request(request)
        _write_new(request_path, canonical(request))
        inputs.append(ProcessInput.capture(request_path, cancel))
        plan = TrainingProcessPlan(uuid4().hex, str(self.work_root), _directory(self.work_root), name,
            command, tuple(inputs), digest(request), self.timeout_seconds, 1024**2, 3, tuple(bindings), 'local-processing-v1')
        plan.validate()
        if _verify_software(root, profile, cancel, deadline) != identity:
            raise ValueError('trainer_health_software_changed')
        return plan, request

    def check(self, software_root, profile):
        """Run afresh; failures and unknown process ownership never yield health."""
        root, plan, outcome = Path(software_root).absolute(), None, None
        invoked = False
        self.last_result = None
        try:
            plan, request = self._prepare(root, profile)
            # Retain the complete process ownership plan before any native
            # invocation. A queue restart must recover this exact owner.
            if self.retain_plan is not None:
                self.retain_plan(plan)
            _control(self.should_cancel, time.monotonic() + MAX_SECONDS)
            def entering_owner():
                nonlocal invoked
                invoked = True
            options = {}
            if getattr(self.process, "reports_process_invocation", False):
                options["invocation_observer"] = entering_owner
            else:
                entering_owner()
            outcome = self.process.execute(plan, plan.fingerprint, self.jobs,
                should_cancel=self.should_cancel, **options)
            if (outcome.status != 'exited_unverified' or type(outcome.exit_code) is not int
                    or outcome.exit_code != 0 or outcome.tree_stopped is not True):
                status = outcome.status if outcome.status in {'held', 'cancelled', 'failed', 'interrupted'} else 'recovery_required'
                if outcome.status != 'held' and outcome.tree_stopped is not True: status = 'recovery_required'
                result = RuntimeHealthResult(TrainerHealth(False, 'Installation check did not finish successfully. Retain its workspace and retry or recover.'), status, plan, outcome)
            else:
                result = self._finish(root, profile, plan, request, outcome)
        except Exception as exc:
            status = ('recovery_required' if invoked and (outcome is None or outcome.tree_stopped is not True)
                      else 'cancelled' if isinstance(exc, InterruptedError) else 'failed')
            result = RuntimeHealthResult(TrainerHealth(False,
                'Installation health could not be verified. Software and generated evidence were retained.'), status, plan, outcome)
        self.last_result = result
        return result

    def verify_retained(self, software_root, profile, plan, request, outcome, expected_sha256):
        """Read-only current verification of one exact retained health receipt."""
        validate_request(request); plan.validate()
        if (digest(request) != plan.preview_fingerprint
                or request['software']['root'] != str(Path(software_root).absolute())
                or request['software']['profile_sha256'] != profile.sha256):
            raise ValueError('trainer_health_retained_binding_changed')
        path = plan.destination / 'installation-health-receipt.json'
        if hashlib.sha256(_read(path, limit=MAX_RECORD)).hexdigest() != expected_sha256:
            raise ValueError('trainer_health_retained_receipt_changed')
        result = self._finish(Path(software_root).absolute(), profile, plan, request, outcome, publish=False)
        if result.receipt_sha256 != expected_sha256:
            raise ValueError('trainer_health_retained_receipt_changed')
        return result

    def _finish(self, root, profile, plan, request, outcome, *, publish=True):
        rows, tip = TrainingRunJournal(plan.destination).read()
        receipt = TrainingProcessService._receipt(plan)
        if (Path(outcome.destination) != plan.destination or tip != outcome.journal_tip or not rows
                or rows[-1]['state'] != 'exited_unverified' or type(rows[-1]['details'].get('exit_code')) is not int
                or rows[-1]['details'].get('exit_code') != 0
                or rows[-1]['details'].get('tree_stopped') is not True
                or canonical(rows[0]['details']) != canonical({'plan': receipt, 'fingerprint': digest(receipt)})
                or self.process.backend.active_named(plan.owner_name)):
            raise ValueError('trainer_health_stopped_receipt_invalid')
        native_path = plan.destination / 'work/health-result.json'
        native_raw = _read(native_path, limit=MAX_RECORD)
        native = validate_native_result(strict(native_raw), request, plan.fingerprint, profile)
        hashes = verified_receipt_hashes(plan)
        if hashes != rows[-1]['details'].get('input_verification_sha256'):
            raise ValueError('trainer_health_verification_receipt_changed')
        # Includes every directory/file identity and exact bytes, after imports.
        after = _verify_software(root, profile, self.should_cancel, time.monotonic() + MAX_SECONDS)
        if after != request['software']['inventory_sha256']:
            raise ValueError('trainer_health_software_changed')
        value = {'schema_version': 1, 'kind': 'trainer_installation_health_receipt',
            'plan': asdict(plan), 'request': request, 'native': native, 'native_sha256': digest(native),
            'process_journal_tip': tip, 'input_verification_sha256': hashes,
            'software_before_sha256': after, 'software_after_sha256': after}
        path = plan.destination / 'installation-health-receipt.json'
        raw = canonical(value)
        if len(raw) > MAX_RECORD: raise ValueError('trainer_health_record_bound')
        _control(self.should_cancel, time.monotonic() + MAX_SECONDS)
        if publish:
            _write_new(path, raw)
        if (_read(path, limit=MAX_RECORD) != raw or _read(native_path, limit=MAX_RECORD) != native_raw
                or TrainingRunJournal(plan.destination).read()[1] != tip):
            raise ValueError('trainer_health_readback_failed')
        return RuntimeHealthResult(TrainerHealth(True,
            'Pinned Python, package imports, native origins and trainer parser passed. Training compatibility requires separate qualification.'),
            'healthy', plan, outcome, path, hashlib.sha256(raw).hexdigest())
