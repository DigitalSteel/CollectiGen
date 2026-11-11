"""Generated SDXL loading inside the explicitly staged restricted owner."""
from __future__ import annotations
import hashlib
import importlib
import json
import os
from pathlib import Path
import runpy
import sys
import time
from types import ModuleType

PHASE = 'bootstrap'
DENIALS = set()

def digest(value):
    return hashlib.sha256(json.dumps(value,ensure_ascii=False,sort_keys=True,allow_nan=False,separators=(',',':')).encode()).hexdigest()


def main():
    global PHASE, DENIALS
    request_path = Path(sys.argv[1]); request = json.loads(request_path.read_bytes())
    inputs = request_path.parent; work = Path.cwd()
    if request_path.name != 'restricted-request.json' or Path(request['code_bundle']).parent != inputs:
        raise ValueError('restricted_bootstrap_scope')
    package = ModuleType('_collectigen_model_probe')
    package.__path__ = [str(inputs/'model-loading-code.zip')+'/'+package.__name__]
    sys.modules[package.__name__] = package
    contract = importlib.import_module(package.__name__+'.training_restricted_loading_contract')
    models = importlib.import_module(package.__name__+'.training_model_loading_contract')
    contract.validate_request(request)
    if contract.read_packet(request_path) != request: raise ValueError('restricted_request_changed')
    roots = {role:Path(path) for role,path in request['runtime_roots'].items()}
    if Path(sys.executable).absolute() != roots['python_base']/'python.exe' or Path(sys.base_prefix).absolute() != roots['python_base']:
        raise ValueError('restricted_interpreter_roots')
    model_inputs = Path(request['model_inputs'])
    if model_inputs != inputs/'models' or Path(request['source']) != inputs/'source': raise ValueError('restricted_input_scope')
    # Native ACL controls, before installing Python audit defenses. Opening for
    # update performs no write even if permissions are unexpectedly permissive.
    PHASE = 'read_only_acl'
    for path in (model_inputs/models.NAMES[0], roots['python_base']/'Lib/os.py'):
        try:
            handle = path.open('r+b')
        except PermissionError: pass
        else:
            handle.close(); raise ValueError('restricted_read_only_grant_failed')
    guard = runpy.run_path(request['guard'])
    tensor_paths = tuple(model_inputs/name for name in models.NAMES)
    DENIALS = guard['install_guard'](work, (inputs,*roots.values()), read_files=tensor_paths,
                                   read_only_tensor_files=tensor_paths)
    sys.path[:0] = [request['source'],str(roots['environment']/'Lib'/'site-packages')]
    # No venv relocation, site/.pth execution, model-ID dispatcher or download.
    PHASE = 'import_torch'
    torch = importlib.import_module('torch')
    PHASE = 'import_loader'
    module = importlib.import_module('library.sdxl_model_util')
    safetensors = importlib.import_module('safetensors.torch')
    PHASE = 'resource_gate'
    models.resource_gate(work)
    catalog = json.loads(Path(request['catalog']).read_bytes())
    PHASE = 'generated_loading'
    checked = models.probe_staged_models(module,torch,safetensors,catalog,model_inputs)
    checks = contract.validate_checks({'model_loading':checked,'staged_interpreter_verified':True,
        'read_only_model_write_denied':True,'runtime_write_denied':True})
    PHASE = 'native_observation'
    barrier = contract.FileBarrier(work)
    ready = {'schema_version':1,'phase':'parser_ready','nonce':request['nonce'],
        'request_sha256':digest(request),'pid':os.getpid(),'checks_sha256':digest(checks)}
    barrier.write(0,ready)
    deadline = time.monotonic()+60
    while (ack := barrier.read(1)) is None:
        if time.monotonic() >= deadline: raise ValueError('restricted_observation_timeout')
        time.sleep(.025)
    if ack.get('nonce') != request['nonce'] or ack.get('request_sha256') != digest(request) or ack.get('ready_sha256') != digest(ready):
        raise ValueError('restricted_ack_binding')
    result = {'schema_version':1,'profile':contract.PROFILE,'nonce':request['nonce'],
        'request_sha256':digest(request),'checks':checks,'process_fingerprint':ack['process_fingerprint'],
        'native_observation_sha256':ack['observation_sha256']}
    contract.write_packet(work/'restricted-result.json',result)


if __name__ == '__main__':
    try: main()
    except BaseException as error:
        # Fixed phase/type and audit categories only; no exception messages,
        # selected paths, module tracebacks or embedded strings are exposed.
        kind = type(error).__name__
        kind = kind if kind in {'ValueError','PermissionError','ImportError','ModuleNotFoundError','OSError','RuntimeError','MemoryError','ProbeDenied','AssertionError'} else 'other'
        try:
            with (Path.cwd()/'restricted-held.json').open('x',encoding='utf-8') as handle:
                json.dump({'phase':PHASE,'error_kind':kind,'denied_operations':sorted(DENIALS)},handle)
        except BaseException: pass
        # The parent retains opaque logs and a held result, never raw errors.
        print('Restricted generated loading did not qualify.',flush=True)
        raise SystemExit(2) from None
