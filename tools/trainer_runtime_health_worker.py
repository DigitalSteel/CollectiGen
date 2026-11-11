"""Fixed isolated installation probe; no training, model or tokenizer loading."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
from pathlib import Path
import runpy
import sys


ROOT = Path(__file__).resolve().parents[1]
_FAILURE_CONTEXT = None


BRIDGE_HASHES = ('68f2f83e41b449bb3ae432a9116a6dcb5dc270b7ca4473e5236e1ce63e867c88', '5386562a8dd487a813abf61b973de9a3569cc5adc66acf68f6b500fe728e6eb6', 'dc1e25f6a7f3b427ae2b862191430d2283af55863b7d14b801fa88ed673d61dc')
_BRIDGE = None


def bootstrap():
    global _BRIDGE
    if _BRIDGE is None:
        from types import ModuleType
        path = ROOT / 'tools/training_execution_worker.py'
        with path.open('rb') as handle:
            raw = handle.read(256 * 1024 + 1)
        if len(raw) > 256 * 1024 or hashlib.sha256(raw).hexdigest() not in BRIDGE_HASHES:
            raise ValueError('training_execution_bridge_changed')
        name = '_collectigen_health_bootstrap'
        if name in sys.modules:
            raise ValueError('training_execution_bridge_occupied')
        module = ModuleType(name)
        module.__file__ = str(path)
        sys.modules[name] = module
        try:
            exec(compile(raw, str(path), 'exec', dont_inherit=True), module.__dict__)
        except BaseException:
            del sys.modules[name]
            raise
        _BRIDGE = module
    return _BRIDGE


def observe(request, profile, fingerprint, health, *, importer=importlib.import_module,
            distribution_at=None, system=sys):
    """Native seam for synthetic tests; the executable exposes no override."""
    root = Path(request['software']['root'])
    site = root / 'environment/Lib/site-packages'
    if (Path(system.base_prefix).absolute() != root / 'python'
            or Path(system.executable).absolute() != root / 'python/python.exe'
            or '.'.join(map(str, system.version_info[:3])) != request['software']['python_version']):
        raise ValueError('trainer_health_interpreter_changed')
    if distribution_at is None:
        from importlib.metadata import Distribution
        distribution_at = Distribution.at
    packages = {}
    # Every location comes from the pinned file list. No ambient site discovery.
    for name, row in request['software']['packages'].items():
        distribution = distribution_at(root / row['metadata'].removesuffix('/METADATA'))
        import re
        observed_name = re.sub(r'[-_.]+', '-', str(distribution.metadata['Name'])).lower()
        if observed_name != name or distribution.version != row['version']:
            raise ValueError('trainer_health_package_version_changed')
        packages[name] = distribution.version
    modules, origins = {}, {}
    files = {row['path'] for row in profile.files}
    for name in health.MODULES:
        module = importer(name)
        origin = Path(module.__file__).absolute()
        selected_root = root / 'source' if name == 'sdxl_train_network' else site / name.split('.')[0]
        if not origin.is_relative_to(selected_root) or origin != origin.resolve():
            raise ValueError('trainer_health_native_origin_invalid')
        relative = origin.relative_to(root).as_posix()
        if relative not in files:
            raise ValueError('trainer_health_native_origin_invalid')
        modules[name], origins[name] = module, relative
    for name in ('torch', 'transformers', 'safetensors'):
        if str(modules[name].__version__) != packages[name]:
            raise ValueError('trainer_health_package_version_changed')
    # Parsing no arguments imports the trainer without selecting any data/model.
    modules['sdxl_train_network'].setup_parser().parse_args([])
    cuda = None
    if request['software']['observe_cuda']:
        torch = modules['torch']
        cuda = {'available': bool(torch.cuda.is_available())}
        if cuda['available']:
            index = torch.cuda.current_device()
            properties = torch.cuda.get_device_properties(index)
            cuda.update({'index': index, 'name': properties.name,
                'compute_capability': [properties.major, properties.minor],
                'total_memory_bytes': properties.total_memory, 'cuda_version': str(torch.version.cuda)})
    result = {'schema_version': 1, 'kind': health.RESULT_KIND, 'process_fingerprint': fingerprint,
        'request_sha256': health.digest(request), 'profile_sha256': profile.sha256,
        'python_version': '.'.join(map(str, system.version_info[:3])), 'packages': packages,
        'module_origins': origins, 'parser': {'module': 'sdxl_train_network', 'arguments': [], 'parsed': True},
        'cuda': cuda, 'denials': []}
    return health.validate_native_result(result, request, fingerprint, profile)


def run(request_path):
    bridge = bootstrap()
    bridge.prepare_owned(request_path, 'health', worker_path=__file__)
    health, process = bridge.owned('trainer_runtime_health'), bridge.owned('training_process')
    request = health.validate_request(health.strict(process._read(Path(request_path), limit=health.MAX_RECORD)))
    plan = bridge.reconstruct_plan(request, request_path, process, worker=__file__)
    global _FAILURE_CONTEXT
    _FAILURE_CONTEXT = (bridge, plan, process._directory(plan.destination / 'work'))
    if any(name in sys.modules for name in health.MODULES):
        raise ValueError('trainer_health_native_imported_early')
    # The parent already checked every selected input in a contained helper.
    # Recheck the fixed owned code and profile before importing selected code.
    for row in request['process']['inputs']:
        if row['purpose'] in {'owned_code', 'worker', 'profile'}:
            captured = process.ProcessInput.capture(row['path'])
            if captured.sha256 != row['sha256'] or list(captured.state) != row['state']:
                raise ValueError('trainer_health_owned_input_changed')
    profile_row = next(row for row in request['process']['inputs'] if row['purpose'] == 'profile')
    profile = health.TrainerProfile.from_dict(health.strict(
        process._read(Path(profile_row['path']), limit=health.MAX_MANIFEST_BYTES), limit=health.MAX_MANIFEST_BYTES))
    root = Path(request['software']['root'])
    if (profile.sha256 != request['software']['profile_sha256']
            or profile.payload()['python']['executable'] != 'python/python.exe'
            or profile.payload()['python']['version'] != request['software']['python_version']
            or health.packages_from_profile(root, profile) != request['software']['packages']):
        raise ValueError('trainer_health_profile_changed')
    for row in request['process']['inventories']:
        inventory = health.SoftwareInventoryService.load(row['root'], row['manifest_path'])
        if inventory.fingerprint != row['inventory_sha256']:
            raise ValueError('trainer_health_inventory_changed')
    if health._verify_software(root, profile, None, health.time.monotonic() + health.MAX_SECONDS) != request['software']['inventory_sha256']:
        raise ValueError('trainer_health_software_changed')
    if Path(sys.base_prefix).absolute() != root / 'python' or Path(sys.executable).absolute() != root / 'python/python.exe':
        raise ValueError('trainer_health_interpreter_changed')
    site = root / 'environment/Lib/site-packages'
    process._directory(site)
    probe = runpy.run_path(str(ROOT / 'tools/training_runtime_probe.py'))
    guard = probe['install_guard'](plan.destination, (root / 'source', root / 'python', root / 'environment', ROOT / 'core'),
        read_files=tuple(Path(item.path) for item in plan.inputs),
        profile='local-processing-v1', diagnostic_site_packages=str(site))
    sys.path[:0] = [str(site), str(root / 'source')]
    probe['check_local_connectivity']()
    result = observe(request, profile, plan.fingerprint, health)
    probe['check_local_connectivity']()
    # Handled denials are evidence too; never clear them to obtain success.
    result['denials'] = sorted(guard)
    health.validate_native_result(result, request, plan.fingerprint, profile)
    process._write_new(plan.destination / 'work/health-result.json', health.canonical(result))
    return 0


def main():
    if not bootstrap().fixed_invocation(sys.argv, __file__): return 2
    try:
        return run(sys.argv[1])
    except BaseException as exc:
        if _FAILURE_CONTEXT is not None:
            bridge, plan, identity = _FAILURE_CONTEXT
            try:
                process = bridge.owned('training_process')
                if process._directory(plan.destination / 'work') != identity: return 2
                code = ('resource_unavailable' if isinstance(exc, (ImportError, FileNotFoundError))
                        else 'cancelled' if isinstance(exc, (KeyboardInterrupt, InterruptedError)) else 'runtime_failed')
                kind = type(exc).__name__
                if kind not in {'ValueError', 'RuntimeError', 'ImportError', 'ModuleNotFoundError',
                                'FileNotFoundError', 'OSError', 'SystemExit', 'KeyboardInterrupt', 'InterruptedError'}:
                    kind = 'RuntimeError'
                value = {'schema_version': 1, 'kind': 'trainer_installation_health_failure',
                    'process_fingerprint': plan.fingerprint, 'request_sha256': plan.preview_fingerprint,
                    'code': code, 'error_type': kind}
                process._write_new(plan.destination / 'work/health-failure.json', process._bytes(value))
            except Exception:
                pass  # Retain incomplete evidence; never fabricate a success.
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
