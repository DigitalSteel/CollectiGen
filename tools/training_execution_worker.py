"""Fixed local trainer entry point; only a qualified, journal-bound contract runs.

The executable CLI has no candidate mode or authority override. Unit tests may
inject the owned-module loader and native runner into ``execute``. The parent
owns process cancellation, checkpoint recovery, and successful result anchoring.
"""
from __future__ import annotations

from dataclasses import dataclass
import errno
import hashlib
import importlib
import importlib.util
import json
from pathlib import Path
import re
import runpy
import sys
from types import ModuleType


ROOT = Path(__file__).resolve().parents[1]
CORE = ROOT / 'core'
MAX_CONTRACT_BYTES = 4 * 1024**2
MAX_RESULT_BYTES = 1024**2
MAX_STATE_FILES = 1000
FLAGS = ('-I', '-S', '-B', '-X', 'utf8')
PACKAGE = '_collectigen_training_execution'
SOURCE_LOADER_HASHES = ('b6c91ee1eaac1ea1a70800723e126452f5545efc8b6789ccc2f2a6a3f7bcc1c4', '15ebc6afbf81d5c11f8859d1f8358588f59d80e463c590e4d95373e26724fb0e')
_SOURCE_LOADER = None
_OWNED = None


class WorkerError(ValueError):
    """Fixed reason codes only; never selected paths or exception payloads."""


def source_loader():
    """Compile the initial helper from its reviewed bytes, never its cache."""
    global _SOURCE_LOADER
    if _SOURCE_LOADER is None:
        path = CORE / 'training_source_loader.py'
        with path.open('rb') as handle:
            raw = handle.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024 or hashlib.sha256(raw).hexdigest() not in SOURCE_LOADER_HASHES:
            raise ValueError('training_execution_source_loader_changed')
        name = '_collectigen_source_loader_' + hashlib.sha256(__name__.encode()).hexdigest()[:16]
        if name in sys.modules:
            raise ValueError('training_execution_source_loader_occupied')
        module = ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module
        try:
            exec(compile(raw, str(path), 'exec', dont_inherit=True), module.__dict__)
        except BaseException:
            del sys.modules[name]
            raise
        _SOURCE_LOADER = module
    return _SOURCE_LOADER


def prepare_owned(request_path, kind='execution', *, worker_path=None, work=None):
    global _OWNED
    if _OWNED is not None:
        raise WorkerError('training_execution_owned_package_occupied')
    loader = source_loader()
    command = (str(Path(sys.executable).absolute()), *FLAGS,
        str(Path(worker_path or __file__).absolute()), str(Path(request_path).absolute()))
    request, inputs = loader.reserved_request(request_path, command=command, work=work)
    _OWNED = loader.worker_package(PACKAGE, CORE, inputs, kind)
    return request


def owned(name):
    if _OWNED is None:
        raise WorkerError('training_execution_owned_package_unprepared')
    return _OWNED.import_module(name)


def strict_json(raw):
    def pairs(items):
        value = {}
        for key, item in items:
            if key in value:
                raise WorkerError('training_execution_duplicate_json_key')
            value[key] = item
        return value
    def constant(_):
        raise WorkerError('training_execution_nonfinite_json')
    return json.loads(raw, object_pairs_hook=pairs, parse_constant=constant)


def reconstruct_plan(contract, contract_path, process, *, work=None, interpreter=None, worker=None):
    """Rebuild the cycle-free plan and compare the entire reserved receipt."""
    work = Path.cwd() if work is None else Path(work)
    work = work.absolute()
    path = Path(contract_path).absolute()
    workspace, request = contract['workspace'], contract['process']
    command = tuple(request['command'])
    expected = (str(Path(interpreter or sys.executable).absolute()), *FLAGS,
                str(Path(worker or __file__).absolute()), str(path))
    if command != expected or work != Path(workspace['root']) / workspace['name'] / 'work':
        raise WorkerError('training_execution_worker_location_changed')
    if process._directory(workspace['root']) != tuple(workspace['root_identity']):
        raise WorkerError('training_execution_workspace_changed')
    process._directory(work)
    rows, _ = process.TrainingRunJournal(work.parent).read()
    if not rows or rows[0]['state'] != 'reserved' or set(rows[0]['details']) != {'plan', 'fingerprint'}:
        raise WorkerError('training_execution_reservation_missing')
    receipt = rows[0]['details']['plan']
    process.TrainingProcessService._validate_receipt(receipt)
    if hashlib.sha256(process._bytes(receipt)).hexdigest() != rows[0]['details']['fingerprint']:
        raise WorkerError('training_execution_reservation_changed')
    raw = process._bytes(contract)
    selected = process.ProcessInput.capture(path)
    if selected.sha256 != hashlib.sha256(raw).hexdigest() or process._read(path, limit=MAX_CONTRACT_BYTES) != raw:
        raise WorkerError('training_execution_contract_changed')
    size = len(request['inputs']) + 1
    index = request['contract_input_index']
    if not 1 <= size <= 128 or type(index) is not int or not 0 <= index < size:
        raise WorkerError('training_execution_input_order_changed')
    inputs = [None] * size
    inputs[index] = selected
    for row in request['inputs']:
        position = row['index']
        if type(position) is not int or not 0 <= position < size or inputs[position] is not None:
            raise WorkerError('training_execution_input_order_changed')
        inputs[position] = process.ProcessInput(row['path'], tuple(row['state']), row['sha256'])
    if any(item is None for item in inputs):
        raise WorkerError('training_execution_input_order_changed')
    inventories = tuple(process.ProcessInventory(row['root'], row['manifest_path'], row['manifest_sha256'],
        row['file_count'], row['byte_count']) for row in request['inventories'])
    plan = process.TrainingProcessPlan(receipt['run_id'], workspace['root'], tuple(workspace['root_identity']),
        workspace['name'], command, tuple(inputs), selected.sha256, receipt['timeout_seconds'],
        receipt['log_limit'], 3, inventories, request['environment_profile'])
    plan.validate()
    if process._bytes(process.TrainingProcessService._receipt(plan)) != process._bytes(receipt):
        raise WorkerError('training_execution_reserved_plan_changed')
    return plan


@dataclass(frozen=True)
class Context:
    contract: dict
    plan: object
    manifest: dict
    work: Path
    binding: dict
    source: Path
    site: Path
    python_base: Path
    package: Path
    patch: Path
    probe: Path
    source_catalog: Path
    model_catalog: Path
    tokenizers_root: Path
    inputs: dict
    inventories: dict


def locations(contract, plan, manifest, process, *, python_base=None):
    """Resolve only the four exact selected roots; never search for software."""
    rows = contract['process']['inputs']
    singles = {row['purpose']: row for row in rows if row['slot'] == '0' and row['purpose'] != 'owned_code'}
    code = {row['slot']: Path(row['path']) for row in rows if row['purpose'] == 'owned_code'}
    inventories = {row['purpose']: Path(row['root']) for row in contract['process']['inventories']}
    if set(inventories) != {'source_inventory', 'environment_inventory', 'python_base_inventory', 'package_inventory'}:
        raise WorkerError('training_execution_inventory_roots_changed')
    source, environment, base, package = (inventories[name + '_inventory'] for name in ('source', 'environment', 'python_base', 'package'))
    roots = tuple(inventories.values())
    for root in roots:
        process._directory(root)
        if root != root.resolve() or root.parent == root:
            raise WorkerError('training_execution_inventory_roots_changed')
    if any(left.is_relative_to(right) for left in roots for right in roots if left != right) or len(set(roots)) != 4:
        raise WorkerError('training_execution_inventory_roots_overlap')
    if (base != Path(python_base or sys.base_prefix).absolute()
            or not any(Path(plan.command[0]).is_relative_to(root) for root in (environment, base))
            or package != Path(contract['package']['path'])):
        raise WorkerError('training_execution_selected_environment_changed')
    site = environment / 'Lib' / 'site-packages'
    process._directory(site)
    slots = [Path(row['path']) for row in contract['resources']['tokenizers']]
    if len(slots) != 2 or slots != [slots[0].parent / 'tokenizer', slots[0].parent / 'tokenizer_2']:
        raise WorkerError('training_execution_tokenizer_layout_unsupported')
    if contract['resources']['vae'] is not None:
        raise WorkerError('training_execution_vae_override_unqualified')
    tools = {'training_runtime_probe.py', 'training_runtime_sources.json', 'training_sdxl_layout.json'}
    if any(code.get('tools/' + name) != ROOT / 'tools' / name for name in tools):
        raise WorkerError('training_execution_owned_tools_changed')
    work = plan.destination / 'work'
    if Path(contract['resources']['base']['path']).is_relative_to(plan.destination):
        raise WorkerError('training_execution_base_inside_output')
    binding = {'run_id': plan.run_id, 'process_sha256': plan.fingerprint, 'preview_sha256': plan.preview_fingerprint}
    return Context(contract, plan, manifest, work, binding, source, site, base, package,
        Path(singles['prepared_patch']['path']), code['tools/training_runtime_probe.py'], code['tools/training_runtime_sources.json'],
        code['tools/training_sdxl_layout.json'], slots[0].parent, singles, inventories)


def state_files(contract, work):
    settings = contract['continuation']['contract']['settings']
    final, interval = settings['max_train_steps'], settings['save_every_n_steps']
    if (type(final) is not int or type(interval) is not int or not 1 <= interval <= final
            or (final + interval - 1) // interval > MAX_STATE_FILES):
        raise WorkerError('training_execution_checkpoint_bound')
    steps = list(range(interval, final + 1, interval))
    if not steps or steps[-1] != final: steps.append(final)
    return tuple(Path(work) / f'training-step{step:08d}-state/model.safetensors' for step in steps)


def previous_checkpoint(contract, execution):
    previous = contract['continuation']['previous']
    if previous is None:
        return None
    selected = {key: previous[key] for key in ('destination', 'binding', 'anchor_sha256', 'sequence')}
    row = execution.anchored_checkpoint(**selected, contract=contract['continuation']['contract'])
    if any(previous[key] != row[key] for key in ('name', 'step', 'epoch')) or any(
            previous[key] != row['proof'][key] for key in ('checkpoint_receipt_sha256', 'continuation_receipt_sha256')):
        raise WorkerError('training_execution_previous_checkpoint_changed')
    return row


def tokenizer_binding(report, context, tokens, execution):
    """Bind constructor evidence to both explicit original slot inventories."""
    expected = []
    for role in (1, 2):
        rows = {row['slot']: row for row in context.contract['process']['inputs'] if row['purpose'] == f'tokenizer_{role}_file'}
        files = [{'name': name, 'bytes': rows[name]['state'][2], 'sha256': rows[name]['sha256']} for name, _ in tokens.TOKENIZER_FILES]
        identity = 'clip-files-v1:' + hashlib.sha256(tokens.canonical({'format': 'clip-files-v1', 'files': files})).hexdigest()
        expected.append((role, files, identity))
    slots = report.get('slots') if type(report) is dict else None
    if type(slots) is not list or len(slots) != 2 or any(type(row) is not dict or
            (row.get('role'), row.get('files'), row.get('identity')) != values for row, values in zip(slots or [], expected)):
        raise WorkerError('training_execution_tokenizer_binding_changed')
    component = execution.digest({'base': context.contract['resources']['base']['id'], 'tokenizers': [row[2] for row in expected]})
    if component != context.contract['resources']['component_binding_sha256'] or component != context.contract['continuation']['contract']['prerequisite']['selection']['component_binding_sha256']:
        raise WorkerError('training_execution_component_binding_changed')


def verify_device(torch, context, process):
    """Compare the selected machine observation with this worker's actual GPU."""
    receipt = strict_json(process._read(Path(context.inputs['device_receipt']['path']), limit=4096))
    if not torch.cuda.is_available() or torch.cuda.current_device() != 0:
        raise WorkerError('training_execution_selected_device_changed')
    properties = torch.cuda.get_device_properties(0)
    observed = {'device_type': 'cuda', 'cuda_index': 0, 'name': properties.name,
        'compute_capability': [properties.major, properties.minor],
        'total_memory_bytes': properties.total_memory, 'torch_version': str(torch.__version__),
        'cuda_version': str(torch.version.cuda)}
    if receipt['observation'] != observed:
        raise WorkerError('training_execution_selected_device_changed')
    return observed


def record_boundary_observation(context, guard, process):
    """Retain bounded public-code diagnostics, never denied targets or messages."""
    categories = {'external_write_denied', 'external_or_model_read_denied', 'external_directory_denied'}
    denials = sorted({item if type(item) is str and item in categories else 'unknown_denial' for item in guard})
    site = getattr(guard, 'failure_site', '')
    packages = {'torch', 'accelerate', 'transformers', 'diffusers', 'huggingface_hub',
                'numpy', 'tqdm', 'safetensors', 'toml', 'voluptuous', 'einops', 'PIL'}
    if (type(site) is not str or re.fullmatch(r'[A-Za-z_][A-Za-z0-9_.]{0,159}:[1-9][0-9]{0,7}', site) is None
            or site.split(':')[0].split('.')[0] not in packages):
        site = ''
    value = {'schema_version': 1, 'kind': 'sdxl_training_boundary_observation',
        'process_fingerprint': context.binding['process_sha256'],
        'execution_contract_sha256': context.binding['preview_sha256'],
        'denials': denials, 'failure_site': site}
    process._write_new(context.work / 'training-boundary-observation.json', process._bytes(value))


def stage_execution_patch(context, process, raw):
    """Copy the exact admitted patch into the owned native workspace.

    sd-scripts appends its module directory during LoRA namespace import. The
    app preparation directory is intentionally outside the worker read roots;
    loading these identical bytes locally keeps imports inside owned storage.
    """
    selected = context.inputs['prepared_patch']
    if (type(raw) is not bytes or len(raw) != selected['state'][2]
            or hashlib.sha256(raw).hexdigest() != selected['sha256']):
        raise WorkerError('training_execution_prepared_source_changed')
    directory = context.work / 'prepared-source'
    directory.mkdir(exist_ok=False)
    process._directory(directory)
    path = directory / 'train_network.py'
    process._write_new(path, raw)
    if process._read(path, limit=1024**2) != raw:
        raise WorkerError('training_execution_prepared_source_changed')
    return path


def native_run(context, owned_loader):
    """Import the exact selected runtime only after full worker validation."""
    if any(name in sys.modules for name in ('torch', 'numpy', 'PIL', 'transformers', 'safetensors')):
        raise WorkerError('training_execution_native_imported_early')
    hooks = owned_loader('training_continuation_hooks')
    execution = owned_loader('training_continuation_execution')
    process = owned_loader('training_process')
    hooks.prepare_environment()
    original = process._read(context.source / 'train_network.py', limit=1024**2)
    patched = process._read(context.patch, limit=1024**2)
    catalog = strict_json(process._read(context.source_catalog, limit=1024**2))
    if execution.source_identity(original, catalog, patched) != context.contract['continuation']['contract']['prepared_source']:
        raise WorkerError('training_execution_prepared_source_changed')
    model_catalog = strict_json(process._read(context.model_catalog, limit=1024**2))
    owned_loader('training_model_loading_contract').checked_catalog(model_catalog)
    previous = context.contract['continuation']['previous']
    prior = previous_checkpoint(context.contract, execution)
    old_folder = Path(previous['destination']) / 'work' / prior['name'] if previous else None
    base = Path(context.contract['resources']['base']['path'])
    # A file as a read root satisfies the existing tensor guard without granting
    # directory discovery over the selected model's containing folder.
    read_roots = (*context.inventories.values(), CORE, base,
        *(Path(row['path']) for row in context.contract['resources']['tokenizers']),
        *((old_folder, Path(previous['destination']) / 'checkpoint-events') if previous else ()))
    explicit = tuple(Path(item.path) for item in context.plan.inputs)
    probe = runpy.run_path(str(context.probe))
    guard = probe['install_guard'](context.work.parent, read_roots, read_files=explicit,
        generated_tensor_files=(context.work.parent / context.contract['result_binding']['output_relative_path'],),
        generated_state_files=state_files(context.contract, context.work),
        read_only_tensor_files=(base, *((old_folder / 'model.safetensors',) if previous else ())),
        profile='local-processing-v1', diagnostic_site_packages=str(context.site))
    sys.path[:0] = [str(context.source), str(context.site)]
    probe['check_local_connectivity']()
    def native(name, root):
        module = importlib.import_module(name)
        if not Path(module.__file__).absolute().is_relative_to(root):
            raise WorkerError('training_execution_native_module_changed')
        return module
    torch = native('torch', context.site)
    hooks.configure_torch(torch)
    verify_device(torch, context, process)
    np, Image = native('numpy', context.site), native('PIL.Image', context.site)
    runtime_module = owned_loader('training_execution_runtime')
    runtime = runtime_module.Runtime(context.work, context.binding, context.contract, context.manifest, torch, np, Image)
    loader = source_loader()
    selected_patch = context.inputs['prepared_patch']
    local_patch = stage_execution_patch(context, process, patched)
    module = loader.load_source_module('train_network', loader.SourceFile(local_patch,
        selected_patch['sha256'], selected_patch['state'][2]), expected_path=local_patch)
    module._collectigen_resume = runtime
    # Preserve the qualified bootstrap's handled library-probe fallback. Reads
    # remain guarded throughout; training denials cannot become a success.
    guard.clear()
    transformers = native('transformers', context.site)
    tokenizers, report = owned_loader('training_reference_tokenizers').load_reference_tokenizers(
        transformers.CLIPTokenizer, transformers.AddedToken, context.tokenizers_root)
    tokenizer_binding(report, context, owned_loader('training_tokenizer_files'), execution)
    safetensors = native('safetensors.torch', context.site)
    models = native('library.sdxl_model_util', context.source)
    def load_models():
        loaded, auxiliary = owned_loader('training_reference_loading').load_reference_models(
            models, torch, safetensors.load, base, context.contract['resources']['base']['id'][7:], model_catalog)
        runtime.reference_auxiliary = auxiliary
        return loaded
    finish = runtime.finish
    def guarded_finish():
        probe['check_local_connectivity']()
        if guard:
            record_boundary_observation(context, guard, process)
            raise WorkerError('training_execution_file_boundary')
        return finish()
    runtime.finish = guarded_finish
    return runtime_module.run(native('sdxl_train_network', context.source), models,
        native('library.strategy_sdxl', context.source), safetensors, native('toml', context.site),
        runtime, tokenizers, load_models)


def failure_record(exc, binding=None):
    """Stable recovery categories; never copy paths, prompts, or traceback text."""
    error_type = type(exc).__name__
    # "raise ... from None" hides display context, but retains __context__.
    # Preserve this fixed inspection category without copying selected data.
    cause = exc
    inventory_timeout = False
    for _ in range(8):
        if cause is None:
            break
        if (type(cause).__name__ == 'FileInventoryError'
                and type(cause).__module__.endswith('.training_file_inventory')
                and cause.args == ('Software inventory exceeded its bounded inspection time.',)):
            inventory_timeout = True
            break
        cause = cause.__context__
    if inventory_timeout:
        code, error_type = 'resource_unavailable', 'FileInventoryError'
    elif isinstance(exc, MemoryError) or (error_type == 'OutOfMemoryError' and type(exc).__module__.startswith('torch')):
        code = 'out_of_memory'
    elif isinstance(exc, OSError) and (exc.errno in {errno.ENOSPC, getattr(errno, 'EDQUOT', -1)} or getattr(exc, 'winerror', None) == 112):
        code = 'workspace_unavailable'
    elif isinstance(exc, (FileNotFoundError, ImportError)):
        code = 'resource_unavailable'
    elif str(exc) in {'training_execution_nonfinite_latents', 'training_execution_nonfinite_noise',
            'training_execution_nonfinite_loss', 'continuation_nonfinite_state', 'training_execution_output_nonfinite'}:
        code = 'nonfinite'
    elif isinstance(exc, (ValueError, TypeError, KeyError)):
        code = 'input_changed'
    else:
        code = 'runtime_failed'
    public_types = {'ValueError', 'TypeError', 'KeyError', 'RuntimeError', 'OSError', 'FileNotFoundError',
        'ModuleNotFoundError', 'ImportError', 'MemoryError', 'OutOfMemoryError', 'InterruptedError', 'KeyboardInterrupt',
        'WorkerError', 'TrainingExecutionPlanError', 'TrainingProcessError', 'CheckpointError', 'PermissionError', 'FileInventoryError'}
    return {'schema_version': 1, 'kind': 'sdxl_training_failure', 'code': code,
        'error_type': error_type if error_type in public_types else 'RuntimeError',
        'process_fingerprint': binding['process_sha256'] if binding else None,
        'execution_contract_sha256': binding['preview_sha256'] if binding else None}


def execute(contract_path, *, owned_loader=owned, native_runner=native_run, work=None, interpreter=None, worker=None, python_base=None):
    """Execute once. Optional injections are test seams, never CLI arguments."""
    plan = process = workspace_identity = None
    try:
        if owned_loader is owned:
            prepare_owned(contract_path, worker_path=worker, work=work)
        process = owned_loader('training_process')
        raw = process._read(Path(contract_path).absolute(), limit=MAX_CONTRACT_BYTES)
        contract = strict_json(raw)
        if process._bytes(contract) != raw:
            raise WorkerError('training_execution_contract_not_canonical')
        authority = owned_loader('training_execution_plan')
        authority.validate_contract_structure(contract)
        plan = reconstruct_plan(contract, contract_path, process, work=work, interpreter=interpreter, worker=worker)
        workspace_identity = (process._directory(plan.destination), process._directory(plan.destination / 'work'))
        if (plan.destination / 'work' / 'training-result.json').exists() or (plan.destination / 'work' / 'training-failure.json').exists():
            raise WorkerError('training_execution_result_occupied')
        authority.validate_worker_contract(contract, plan)
        manifest_path = Path(contract['package']['path']) / authority.MANIFEST_NAME
        manifest_raw = process._read(manifest_path, limit=authority.MAX_PACKAGE_MANIFEST)
        if hashlib.sha256(manifest_raw).hexdigest() != contract['package']['manifest_sha256']:
            raise WorkerError('training_execution_package_changed')
        context = locations(contract, plan, strict_json(manifest_raw), process, python_base=python_base)
        result = native_runner(context, owned_loader)
        if (type(result) is not dict or result.get('status') != 'completed'
                or result.get('process_fingerprint') != plan.fingerprint
                or result.get('execution_contract_sha256') != plan.preview_fingerprint
                or process._read(context.work / 'training-result.json', limit=MAX_RESULT_BYTES) != process._bytes(result)):
            raise WorkerError('training_execution_result_missing_or_changed')
        return 0
    except BaseException as exc:
        binding = {'process_sha256': plan.fingerprint, 'preview_sha256': plan.preview_fingerprint} if plan else None
        record = failure_record(exc, binding)
        if plan is not None and workspace_identity is not None:
            try:
                if workspace_identity == (process._directory(plan.destination), process._directory(plan.destination / 'work')):
                    process._write_new(plan.destination / 'work' / 'training-failure.json', process._bytes(record))
            except (OSError, RuntimeError): pass  # Retain occupied records or a disk-full workspace.
        print('training_execution_' + record['code'], file=sys.stderr, flush=True)
        return 2


def fixed_invocation(arguments, worker_path):
    """Windows venv launchers retain the base executable in orig_argv[0]."""
    if (len(arguments) != 2 or not all((sys.flags.isolated, sys.flags.no_site,
            sys.flags.dont_write_bytecode, sys.flags.utf8_mode))):
        return False
    original = tuple(sys.orig_argv)
    executables = {str(Path(sys.executable).absolute()),
                   str(Path(sys.base_prefix).absolute() / 'python.exe')}
    expected_tail = (*FLAGS, str(Path(worker_path).absolute()), str(Path(arguments[1]).absolute()))
    # reconstruct_plan separately binds the actual sys.executable to the reviewed plan.
    return len(original) == 8 and original[0] in executables and original[1:] == expected_tail


def main(argv=None):
    arguments = sys.argv if argv is None else argv
    if not fixed_invocation(arguments, __file__):
        print('training_execution_fixed_worker_invocation_required', file=sys.stderr)
        return 2
    return execute(arguments[1])


if __name__ == '__main__':
    raise SystemExit(main())
