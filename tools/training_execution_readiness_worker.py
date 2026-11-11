"""Fixed non-training native check for original SDXL tokenizers and the GPU."""
from __future__ import annotations

import hashlib
import importlib
import importlib.util
from pathlib import Path
import runpy
import re
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
        name = '_collectigen_execution_bootstrap'
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


def run(request_path):
    bridge = bootstrap()
    bridge.prepare_owned(request_path, 'readiness', worker_path=__file__)
    readiness, process = bridge.owned('training_execution_readiness'), bridge.owned('training_process')
    request = readiness.validate_request(readiness.strict(process._read(Path(request_path), limit=readiness.MAX_RECEIPT)))
    plan = bridge.reconstruct_plan(request, request_path, process, worker=__file__)
    global _FAILURE_CONTEXT
    _FAILURE_CONTEXT = (bridge, plan, process._directory(plan.destination / 'work'))
    code = {row['slot']: row for row in request['process']['inputs'] if row['purpose'] == 'owned_code'}
    if set(code) != set(readiness.code_paths()) or any(code[name]['path'] != str(path)
            or process.ProcessInput.capture(path).sha256 != code[name]['sha256'] for name, path in readiness.code_paths().items()):
        raise ValueError('training_readiness_owned_code_changed')
    roots = {row['purpose'].removesuffix('_inventory'): Path(row['root']) for row in request['process']['inventories']}
    if Path(sys.base_prefix).absolute() != roots['python_base'] or not any(
            Path(sys.executable).absolute().is_relative_to(roots[key]) for key in ('python_base', 'environment')):
        raise ValueError('training_readiness_interpreter_changed')
    catalog = bridge.strict_json(process._read(ROOT / 'tools/training_runtime_sources.json', limit=1024**2))
    # The catalog itself is readable JSON with ordinary formatting, unlike run records.
    if catalog['source_revision'] != request['source_revision']:
        raise ValueError('training_readiness_source_revision_changed')
    for name, expected in catalog['files'].items():
        if process.ProcessInput.capture(roots['source'] / name).sha256 not in expected:
            raise ValueError('training_readiness_source_changed')
    site = roots['environment'] / 'Lib/site-packages'
    tokens = Path(request['selection']['tokenizers_root'])
    hooks = bridge.owned('training_continuation_hooks')
    hooks.prepare_environment()
    probe = runpy.run_path(str(ROOT / 'tools/training_runtime_probe.py'))
    guard = probe['install_guard'](plan.destination, (*roots.values(), ROOT / 'core', tokens / 'tokenizer', tokens / 'tokenizer_2'),
        read_files=tuple(Path(row.path) for row in plan.inputs), profile='local-processing-v1', diagnostic_site_packages=str(site))
    sys.path[:0] = [str(roots['source']), str(site)]
    def native(name, root=site):
        module = importlib.import_module(name)
        if not Path(module.__file__).absolute().is_relative_to(root):
            raise ValueError('training_readiness_native_module_changed')
        return module
    probe['check_local_connectivity']()
    torch = native('torch')
    hooks.configure_torch(torch)
    transformers = native('transformers')
    # Parser import is the qualified trainer bootstrap; retain later denials.
    entry = native('sdxl_train_network', roots['source'])
    entry.setup_parser().parse_args([])
    guard.clear()
    _, tokenizers = bridge.owned('training_reference_tokenizers').load_reference_tokenizers(
        transformers.CLIPTokenizer, transformers.AddedToken, tokens)
    if not torch.cuda.is_available() or torch.cuda.current_device() != 0:
        raise ValueError('training_readiness_cuda_unavailable')
    properties = torch.cuda.get_device_properties(0)
    device = {'device_type': 'cuda', 'cuda_index': 0, 'name': properties.name,
        'compute_capability': [properties.major, properties.minor], 'total_memory_bytes': properties.total_memory,
        'torch_version': str(torch.__version__), 'cuda_version': str(torch.version.cuda)}
    from importlib import metadata
    packages = {}
    for distribution in metadata.distributions(path=[str(site)]):
        name = str(distribution.metadata['Name']).lower().replace('_', '-').replace('.', '-')
        if name in request['expected_packages']:
            if name in packages: raise ValueError('training_readiness_duplicate_distribution')
            packages[name] = distribution.version
    probe['check_local_connectivity']()
    result = {'schema_version': 1, 'kind': readiness.RESULT_KIND, 'process_fingerprint': plan.fingerprint,
        'request_sha256': readiness.digest(request), 'device_observation': device,
        'runtime': {'python_version': '.'.join(map(str, sys.version_info[:3])), 'packages': packages},
        'tokenizers': tokenizers, 'configuration_sha256': readiness.digest(request['selection']['configuration']),
        'denials': sorted(guard)}
    readiness.validate_native_result(result, request, plan.fingerprint)
    process._write_new(plan.destination / 'work/readiness-result.json', readiness.canonical(result))
    return 0


def main():
    if not bootstrap().fixed_invocation(sys.argv, __file__):
        return 2
    try:
        return run(sys.argv[1])
    except Exception as exc:
        if _FAILURE_CONTEXT is not None:
            bridge, plan, identity = _FAILURE_CONTEXT
            try:
                process = bridge.owned('training_process')
                if process._directory(plan.destination / 'work') != identity:
                    return 2
                message = str(exc)
                code = message if re.fullmatch(r'(?:training_readiness|training_execution|reference|continuation)_[a-z_]{1,100}', message) else 'native_runtime_failed'
                kind = type(exc).__name__
                if kind not in {'ValueError', 'RuntimeError', 'TypeError', 'KeyError', 'OSError', 'FileNotFoundError', 'ImportError', 'ModuleNotFoundError', 'WorkerError'}:
                    kind = 'RuntimeError'
                value = {'schema_version': 1, 'kind': 'sdxl_readiness_failure',
                    'process_fingerprint': plan.fingerprint, 'request_sha256': plan.preview_fingerprint,
                    'code': code, 'error_type': kind}
                process._write_new(plan.destination / 'work/readiness-failure.json', process._bytes(value))
            except Exception:
                pass  # Failed observations never replace or repair evidence.
        return 2


if __name__ == '__main__':
    raise SystemExit(main())
