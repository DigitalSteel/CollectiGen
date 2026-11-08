"""Contained child: selected parser and optional generated-only caption probe.

Launch through qualify_training_runtime.py, never as a training entry point.
Python audit restrictions are defense in depth, not OS network containment.
"""
from __future__ import annotations

import argparse
import hashlib
import importlib
import json
import os
from pathlib import Path
import runpy
import re
import sys
from types import ModuleType


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True,
        allow_nan=False, separators=(",", ":")).encode("utf-8")).hexdigest()


class ProbeDenied(PermissionError):
    def __init__(self, message, operation="none"):
        super().__init__(message)
        self.operation = operation


PROBE_OPERATIONS = {"none", "socket.__new__", "socket.connect", "socket.bind",
    "socket.gethostname", "socket.gethostbyname", "socket.getaddrinfo", "socket.other",
    "subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "pty.spawn"}


def failure_site(exc, site_packages):
    """Return only a selected public package code location, never exception data."""
    found = ""
    trace = exc.__traceback__
    packages = {"torch", "accelerate", "transformers", "diffusers", "huggingface_hub",
                "numpy", "tqdm", "safetensors", "toml", "voluptuous", "einops", "PIL"}
    while trace:
        frame = trace.tb_frame
        name = frame.f_globals.get("__name__", "")
        if type(name) is str and name.split(".")[0] in packages and re.fullmatch(r"[A-Za-z0-9_.]{1,160}", name) and Path(frame.f_code.co_filename).resolve().is_relative_to(Path(site_packages).resolve()):
            found = f"{name}:{trace.tb_lineno}"
        trace = trace.tb_next
    return found


class GuardDenials(set):
    """Keep one public code site for a handled denial; never store its target."""
    failure_site = ''

    def clear(self):
        super().clear()
        self.failure_site = ''


def install_guard(workspace, read_roots, *, blocked_roots=(), read_files=(), generated_tensor_files=(), read_only_tensor_files=(), generated_state_files=(), profile="diagnostic-offline-v1", diagnostic_site_packages=None):
    """Scope diagnostic file access; legacy probes also deny network/process calls.

    Native code may bypass Python audit hooks. No claim of a security sandbox or
    verified offline loading follows from this limited parser-only exercise.
    """
    if type(profile) is not str or profile not in {"diagnostic-offline-v1", "local-processing-v1"}:
        raise ValueError("unsupported_guard_profile")
    workspace = workspace.resolve()
    read_roots = tuple(Path(root).resolve() for root in read_roots) + (workspace,)
    blocked_roots = tuple(Path(root).resolve() for root in blocked_roots)
    # Lexical canonical paths only. Shared slot readers check redirection before
    # every use; adding these exact files never grants folder discovery access.
    read_files = frozenset(Path(os.path.abspath(path)) for path in read_files)
    generated_tensor_files = frozenset(Path(path).resolve() for path in generated_tensor_files)
    generated_state_files = frozenset(Path(path).resolve() for path in generated_state_files)
    read_only_tensor_files = frozenset(Path(path).resolve() for path in read_only_tensor_files)
    if any(path.suffix != '.safetensors' or path.is_relative_to(workspace) or path not in read_files
           or not any(path.is_relative_to(root) for root in read_roots) for path in read_only_tensor_files):
        raise ValueError('read_only_tensor_guard_scope')
    if any(path.parent != workspace / 'work' or path.suffix != '.safetensors' for path in generated_tensor_files):
        raise ValueError('generated_tensor_guard_scope')
    if generated_state_files and (profile != 'local-processing-v1' or any(
            path.parent.parent != workspace / 'work' or path.name != 'model.safetensors'
            or re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}-state', path.parent.name) is None
            for path in generated_state_files)):
        raise ValueError('generated_state_guard_scope')
    generated_tensor_files = generated_tensor_files | generated_state_files
    denials = GuardDenials()
    def deny(message, operation="none"):
        denials.add(operation if operation != "none" else message)
        if diagnostic_site_packages is not None:
            frame = sys._getframe(1)
            try:
                while frame:
                    name = frame.f_globals.get('__name__', '')
                    if (type(name) is str and name.split('.')[0] in {'torch', 'accelerate', 'transformers', 'diffusers', 'huggingface_hub', 'numpy', 'tqdm', 'safetensors', 'toml', 'voluptuous', 'einops', 'PIL'}
                            and re.fullmatch(r'[A-Za-z0-9_.]{1,160}', name)
                            and Path(frame.f_code.co_filename).is_relative_to(diagnostic_site_packages)):
                        denials.failure_site = f'{name}:{frame.f_lineno}'
                        break
                    frame = frame.f_back
            finally:
                del frame
        # PermissionError lets standard-library system probes use their normal
        # denied-access fallback. The attempted operation still never executes.
        raise ProbeDenied(message, operation)
    def inside(value):
        if isinstance(value, int):
            return True
        try:
            path = Path(value).resolve()
            return path.is_relative_to(workspace) and not any(path.is_relative_to(root) for root in blocked_roots)
        except (TypeError, ValueError, OSError):
            return False
    def audit(event, args):
        if event == "socket.gethostname":
            # A local OS query, with no DNS lookup or network communication.
            # Imported libraries use it during initialization; never report it.
            return
        if profile == "diagnostic-offline-v1" and (event.startswith("socket.") or event in {"subprocess.Popen", "os.system", "os.exec", "os.posix_spawn", "os.spawn", "pty.spawn"}):
            deny("network_or_process_denied", event if event in PROBE_OPERATIONS else "socket.other")
        if event == "open":
            if isinstance(args[0], str) and args[0].casefold() == os.devnull.casefold():
                # The OS null sink is not an external file. Windows resolves
                # it as a device path; allow this exact spelling, no aliases.
                return
            mode, flags = args[1], args[2]
            writing = (isinstance(mode, str) and any(c in mode for c in "wax+")) or (isinstance(flags, int) and flags & (os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_TRUNC | os.O_APPEND))
            if writing and not inside(args[0]):
                deny("external_write_denied")
            if not isinstance(args[0], int):
                path = Path(args[0]).resolve()
                if any(path.is_relative_to(root) for root in blocked_roots) or (path not in read_files and not any(path.is_relative_to(root) for root in read_roots)) or (path.suffix.lower() in {".safetensors", ".ckpt", ".pt", ".pth"} and path not in generated_tensor_files | read_only_tensor_files):
                    deny("external_or_model_read_denied")
        if event in {"os.listdir", "os.scandir", "os.chdir"}:
            path = Path(args[0] or os.curdir).resolve()
            if any(path.is_relative_to(root) for root in blocked_roots) or not any(path.is_relative_to(root) for root in read_roots):
                deny("external_directory_denied")
        if event in {"os.remove", "os.rmdir", "os.mkdir", "os.chmod", "os.utime", "os.truncate"} and not inside(args[0]):
            deny("external_write_denied")
        if event in {"os.rename", "os.link", "os.symlink"} and any(not inside(item) for item in args[:2]):
            deny("external_write_denied")
    sys.addaudithook(audit)
    return denials


def check_local_connectivity():
    """Exchange generated bytes with our own ephemeral numeric loopback socket.

    No DNS, public endpoint, download, credential, or user data is involved.
    This is a compatibility positive control, not a network security claim.
    """
    import socket
    if any(key in os.environ for key in ("HF_HUB_OFFLINE", "HF_DATASETS_OFFLINE", "TRANSFORMERS_OFFLINE")):
        raise ValueError("offline_environment_override")
    payload = os.urandom(32)
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as listener:
        listener.settimeout(5)
        listener.bind(("127.0.0.1", 0))
        listener.listen(1)
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as client:
            client.settimeout(5)
            client.connect(listener.getsockname())
            with listener.accept()[0] as peer:
                peer.settimeout(5)
                client.sendall(payload)
                received = bytearray()
                while len(received) < len(payload):
                    block = peer.recv(len(payload) - len(received))
                    if not block:
                        raise ValueError("loopback_closed")
                    received.extend(block)
                if received != payload:
                    raise ValueError("loopback_payload_changed")


def qualify_parser(module, args_module, toml_module, contract, work):
    """Pure parser boundary; never construct a trainer, dataset or tokenizer."""
    parser = module.setup_parser()
    defaults = vars(parser.parse_args([]))
    actions = {action.dest: action for action in parser._actions if action.dest != "help"}
    requested = contract["synthetic_configurations"]()
    results = []
    for index, config in enumerate(requested):
        contract["validate_configuration"](config)
        if not config.keys() <= actions.keys():
            raise ValueError("parser_field_missing")
        # Check CLI actions as well as the stricter CollectiGen contract, since
        # TOML namespaces do not reliably apply argparse's type/choice checks.
        for key, value in config.items():
            action = actions[key]
            if action.choices is not None and value not in action.choices:
                raise ValueError("parser_choice_changed")
            if action.type is not None and action.type(value) != value:
                raise ValueError("parser_type_changed")
        path = work / f"synthetic-{index}.toml"
        path.write_text(contract["configuration_toml"](config), encoding="utf-8")
        decoded = toml_module.load(str(path))
        contract["validate_configuration"](decoded)
        if decoded != config or any(type(decoded[key]) is not type(value) for key, value in config.items()):
            raise ValueError("serialization_changed")
        old_argv = sys.argv
        try:
            sys.argv = ["collectigen-parser-probe", "--config_file", str(path)]
            initial = parser.parse_args()
            effective = vars(args_module.read_config_from_file(initial, parser))
        finally:
            sys.argv = old_argv
        expected = {**defaults, **config, "config_file": str(path.with_suffix(""))}
        if effective != expected or any(type(effective[key]) is not type(value) for key, value in expected.items()):
            raise ValueError("effective_settings_changed")
        # Native config paths are owned probe details, not portable semantics.
        effective["config_file"] = f"synthetic-{index}"
        results.append({"requested_sha256": digest(config), "effective_sha256": digest(effective)})
    # A rejected CLI option must not quietly become a supported adapter field.
    try:
        parser.parse_args(["--collectigen_unknown_option", "1"])
    except SystemExit as exc:
        if exc.code != 2:
            raise ValueError("unknown_cli_exit_changed")
    else:
        raise ValueError("unknown_cli_accepted")
    return {"cases": results, "parser_destinations": len(actions),
            "defaults_sha256": digest(defaults), "unknown_cli_rejected": True}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("request")
    options = parser.parse_args()
    request_raw = Path(options.request).read_bytes()
    request = json.loads(request_raw)
    required = {"schema_version", "nonce", "source_revision", "source", "site_packages", "contract"}
    if request.get("schema_version") in {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17}:
        required.add("runtime_roots")
    if request.get("schema_version") in {3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17}:
        required.add("native_observation")
    if request.get('schema_version') in {4, 5, 15}:
        required.add('tokenizer_contract')
    if request.get('schema_version') in {5, 15}:
        required.add('selected_tokenizers')
    if request.get('schema_version') == 6:
        required.add('model_contract')
    if request.get('schema_version') == 7:
        required.add('tensor_contract')
    if request.get('schema_version') in {8, 14}:
        required.update(('model_loading_bundle', 'model_loading_catalog'))
    if request.get('schema_version') == 16:
        required.update(('unet_step_bundle', 'unet_step_catalog'))
    if request.get('schema_version') == 17:
        required.update(('training_loop_bundle', 'training_loop_catalog'))
    if request.get('schema_version') == 9:
        required.add('image_effects_contract')
    if request.get('schema_version') == 10:
        required.update(('dataset_package_bundle', 'dataset_package_catalog'))
    if request.get('schema_version') in {11, 12, 13}:
        required.add('dataset_preparation')
    if request.get('schema_version') in {12, 13}:
        required.update(('model_pairing', 'model_pairing_bundle', 'model_pairing_catalog'))
    if request.get('schema_version') == 13:
        required.add('conditioning')
        if request['conditioning'] is not True:
            raise ValueError('conditioning_request_invalid')
    if request.get('schema_version') in {14, 15, 16, 17}:
        required.add('environment_profile')
        if request['environment_profile'] != 'local-processing-v1':
            raise ValueError('unsupported_native_environment')
    if set(request) != required or type(request["schema_version"]) is not int or request["schema_version"] not in {1, 2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17}:
        raise ValueError("unsupported_probe_request")
    profile = 'local-processing-v1' if request['schema_version'] in {14, 15, 16, 17} else 'diagnostic-offline-v1'
    work = Path.cwd()
    contract = runpy.run_path(request["contract"])
    tokenizer_contract = runpy.run_path(request['tokenizer_contract']) if request['schema_version'] in {4, 5, 15} else None
    model_contract = runpy.run_path(request['model_contract']) if request['schema_version'] == 6 else None
    tensor_contract = runpy.run_path(request['tensor_contract']) if request['schema_version'] == 7 else None
    image_contract = runpy.run_path(request['image_effects_contract']) if request['schema_version'] == 9 else None
    full_model_contract, full_model_catalog = None, None
    if request['schema_version'] in {8, 14}:
        package = ModuleType('_collectigen_model_probe')
        package.__path__ = [str(Path(request['model_loading_bundle'])) + '/' + package.__name__]
        sys.modules[package.__name__] = package
        full_model_contract = importlib.import_module(package.__name__ + '.training_model_loading_contract')
        full_model_catalog = json.loads(Path(request['model_loading_catalog']).read_bytes())
    unet_contract, unet_catalog = None, None
    loop_contract, loop_catalog = None, None
    if request['schema_version'] == 16:
        package = ModuleType('_collectigen_unet_step')
        package.__path__ = [str(Path(request['unet_step_bundle'])) + '/' + package.__name__]
        sys.modules[package.__name__] = package
        unet_contract = importlib.import_module(package.__name__ + '.training_unet_step_contract')
        unet_catalog = json.loads(Path(request['unet_step_catalog']).read_bytes())
    if request['schema_version'] == 17:
        package = ModuleType('_collectigen_training_loop')
        package.__path__ = [str(Path(request['training_loop_bundle'])) + '/' + package.__name__]
        sys.modules[package.__name__] = package
        loop_contract = importlib.import_module(package.__name__ + '.training_loop_contract')
        loop_catalog = json.loads(Path(request['training_loop_catalog']).read_bytes())
    package_contract, package_catalog = None, None
    if request['schema_version'] == 10:
        package = ModuleType('_collectigen_dataset_probe')
        package.__path__ = [str(Path(request['dataset_package_bundle'])) + '/' + package.__name__]
        sys.modules[package.__name__] = package
        package_contract = importlib.import_module(package.__name__ + '.training_dataset_package_contract')
        package_catalog = json.loads(Path(request['dataset_package_catalog']).read_bytes())
    preparation_contract = None
    if request['schema_version'] in {11, 12, 13}:
        selected_request = request['dataset_preparation']
        if type(selected_request) is not dict or set(selected_request) != {'package', 'binding', 'summary', 'code_bundle'}:
            raise ValueError('dataset_preparation_request_invalid')
        package = ModuleType('_collectigen_preparation')
        package.__path__ = [str(Path(selected_request['code_bundle'])) + '/' + package.__name__]
        sys.modules[package.__name__] = package
        preparation_contract = importlib.import_module(package.__name__ + '.training_dataset_preparation')
        preparation_contract.expected_report(selected_request)
    pairing_contract, pairing_catalog = None, None
    if request['schema_version'] in {12, 13}:
        package = ModuleType('_collectigen_pairing')
        package.__path__ = [str(Path(request['model_pairing_bundle'])) + '/' + package.__name__]
        sys.modules[package.__name__] = package
        pairing_contract = importlib.import_module(package.__name__ + '.training_model_pairing_contract')
        pairing_catalog = json.loads(Path(request['model_pairing_catalog']).read_bytes())
        pairing_contract.expected_report(request['dataset_preparation'], request['model_pairing'])
    conditioning_contract = None
    if request['schema_version'] == 13:
        conditioning_contract = importlib.import_module('_collectigen_pairing.training_conditioning_contract')
    selected_contract, selected_selection, selected_files = None, None, ()
    if request['schema_version'] in {5, 15}:
        selected = request['selected_tokenizers']
        if type(selected) is not dict or set(selected) != {'code_bundle', 'memory', 'binding'}:
            raise ValueError('selected_tokenizer_request_invalid')
        package = ModuleType('_collectigen_selected_probe')
        package.__path__ = [str(Path(selected['code_bundle'])) + '/' + package.__name__]
        sys.modules[package.__name__] = package
        selected_contract = importlib.import_module(package.__name__ + '.training_selected_tokenizers')
        selected_selection = selected_contract.read_selection(selected['memory'])
        if selected_contract.binding(selected_selection) != selected['binding']:
            raise ValueError('selected_tokenizer_binding_changed')
        selected_files = selected_contract.input_paths(selected_selection)
    native = None
    if request["schema_version"] in {3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17}:
        selection = request["native_observation"]
        if type(selection) is not dict or set(selection) != {"mapping", "protocol"}:
            raise ValueError("native_request_invalid")
        native = runpy.run_path(selection["protocol"])
    if request["source_revision"] != contract["SOURCE_REVISION"]:
        raise ValueError("unsupported_source_revision")
    # -I -S avoids PYTHONPATH, user site, startup customizations and executable
    # .pth files. Append only the explicitly selected package directory.
    sys.path[:0] = [request["source"], request["site_packages"]]
    sys.dont_write_bytecode = True
    generated_files = tuple(work / name for name in tensor_contract['filenames']()) if tensor_contract else ()
    if full_model_contract is not None:
        generated_files = tuple(work / name for name in full_model_contract.filenames())
    if pairing_contract is not None:
        loading_contract = importlib.import_module('_collectigen_pairing.training_model_loading_contract')
        generated_files = tuple(work / name for name in loading_contract.filenames())
    if unet_contract is not None:
        generated_files = tuple(work / name for name in unet_contract.filenames())
        # PyTorch seed/checkpoint helpers format Python stacks lazily. Their
        # linecache loaders may reread this exact hash-bound script/code bundle.
        selected_files = (Path(__file__).absolute(), Path(request['unet_step_bundle']))
    if loop_contract is not None:
        generated_files = tuple(work / name for name in loop_contract.filenames())
        # PyTorch seed/checkpoint helpers format Python stacks lazily. Their
        # linecache loaders may reread this exact hash-bound script/code bundle.
        selected_files = (Path(__file__).absolute(), Path(request['training_loop_bundle']))
    dataset_roots = (request['dataset_preparation']['package'],) if preparation_contract is not None else ()
    denials = install_guard(work.parent, (request["source"], request["site_packages"], sys.base_prefix, *dataset_roots),
        read_files=selected_files, generated_tensor_files=generated_files, profile=profile,
        diagnostic_site_packages=request['site_packages'] if unet_contract is not None or loop_contract is not None else None)
    parser_denials, tokenizer_denials = set(), set()
    report = {"schema_version": 1, "nonce": request["nonce"],
        "request_sha256": hashlib.sha256(request_raw).hexdigest(),
        "source_revision": request["source_revision"], "status": "held",
        "reason": "parser_import_failed", "checks": None, "execution_ready": False,
        "python_version": ".".join(map(str, sys.version_info[:3])), "denied_operation": "none",
        "failure_site": "", "software_roots_checked": False}
    if native is not None:
        report["native_observation_sha256"] = ""
    if tokenizer_contract is not None:
        report['tokenizer_checks'] = None
    if selected_contract is not None:
        report['selected_tokenizer_checks'] = None
    if model_contract is not None:
        report['model_layout_checks'] = None
    if tensor_contract is not None:
        report['tensor_loading_checks'] = None
    if full_model_contract is not None:
        report['model_loading_checks'] = None
    if unet_contract is not None:
        report['unet_step_checks'] = None
    if loop_contract is not None:
        report['training_loop_checks'] = None
    if image_contract is not None:
        report['image_effects_checks'] = None
    if package_contract is not None:
        report['dataset_package_checks'] = None
    if preparation_contract is not None:
        report['dataset_preparation_checks'] = None
    if pairing_contract is not None:
        report['model_pairing_checks'] = None
    if conditioning_contract is not None:
        report['conditioning_checks'] = None
    if request['schema_version'] in {14, 15, 16, 17}:
        report.update(environment_profile=profile, local_processing_checks=None)
    try:
        if request['schema_version'] in {14, 15, 16, 17}:
            report['reason'] = 'native_connectivity_failed'
            check_local_connectivity()
        if sys.version_info[:2] not in contract["PYTHON_VERSIONS"]:
            report["reason"] = "python_version_unsupported"
            raise ValueError("python_version_unsupported")
        if request["schema_version"] in {2, 3, 4, 5, 6, 7, 8, 9, 10, 11, 12, 13, 14, 15, 16, 17}:
            report["reason"] = "runtime_roots_mismatch"
            roots = request["runtime_roots"]
            if type(roots) is not dict or set(roots) != {"environment", "python_base"} or any(type(value) is not str for value in roots.values()) or Path(sys.base_prefix).absolute() != Path(roots["python_base"]).absolute() or Path(sys.executable).absolute() != Path(roots["environment"]).absolute() / "Scripts" / "python.exe":
                raise ValueError("runtime_roots_mismatch")
            report["software_roots_checked"] = True
        report["reason"] = "parser_import_failed"
        module = importlib.import_module("sdxl_train_network")
        args_module = importlib.import_module("library.args")
        toml_module = importlib.import_module("toml")
        report["reason"] = "parser_contract_failed"
        checks = qualify_parser(module, args_module, toml_module, contract, work)
        bound_checks, tokenizer_checks = checks, None
        if tokenizer_contract is not None:
            report['reason'] = 'tokenizer_contract_failed'
            # The tokenizer phase rejects out-of-scope file/cache/model reads.
            # Native profiles keep connectivity; legacy diagnostics retain their
            # network restriction. Earlier import fallback denials stay reported.
            parser_denials = set(denials)
            denials.clear()
            tokenizer_denials = install_guard(work.parent,
                (request['source'], request['site_packages'], sys.base_prefix),
                blocked_roots=(work / 'cache', work / 'home'), read_files=selected_files, profile=profile)
            transformers = importlib.import_module('transformers')
            strategy = importlib.import_module('library.strategy_sdxl')
            tokenizer_checks = tokenizer_contract['probe_generated_tokenizers'](
                transformers.CLIPTokenizer, strategy.SdxlTokenizeStrategy, work)
            selected_checks = None
            if selected_contract is not None:
                selected_checks = selected_contract.probe_selected_tokenizers(
                    transformers.CLIPTokenizer, strategy.SdxlTokenizeStrategy, selected_selection)
                # Reject an altered memory request even if its replacement paths
                # happened to yield similar token behavior.
                if selected_contract.read_selection(request['selected_tokenizers']['memory']) != selected_selection:
                    raise ValueError('selected_tokenizer_memory_changed')
            if denials or tokenizer_denials:
                report['reason'] = 'tokenizer_boundary_failed'
                raise ValueError('tokenizer_boundary_failed')
            bound_checks = {'parser': checks, 'tokenizers': tokenizer_checks}
            if selected_contract is not None:
                bound_checks['selected_tokenizers'] = selected_checks
        if model_contract is not None:
            report['reason'] = 'model_layout_failed'
            parser_denials = set(denials)
            denials.clear()
            tokenizer_denials = install_guard(work.parent,
                (request['source'], request['site_packages'], sys.base_prefix),
                blocked_roots=(work / 'cache', work / 'home'))
            model_checks = model_contract['probe_generated_layout'](
                importlib.import_module('library.sdxl_model_util'), importlib.import_module('torch'), work)
            if denials or tokenizer_denials:
                report['reason'] = 'model_layout_boundary_failed'
                raise ValueError('model_layout_boundary_failed')
            bound_checks = {'parser': checks, 'model_layout': model_checks}
        if tensor_contract is not None:
            report['reason'] = 'tensor_loading_failed'
            parser_denials = set(denials)
            denials.clear()
            tokenizer_denials = install_guard(work.parent,
                (request['source'], request['site_packages'], sys.base_prefix),
                blocked_roots=(work / 'cache', work / 'home'), generated_tensor_files=generated_files, profile=profile)
            tensor_checks = tensor_contract['probe_generated_tensors'](
                importlib.import_module('safetensors.torch'), importlib.import_module('torch'), work,
                importlib.import_module('safetensors').SafetensorError)
            if denials or tokenizer_denials:
                report['reason'] = 'tensor_loading_boundary_failed'
                raise ValueError('tensor_loading_boundary_failed')
            bound_checks = {'parser': checks, 'tensor_loading': tensor_checks}
        if full_model_contract is not None:
            report['reason'] = 'model_loading_failed'
            parser_denials = set(denials)
            denials.clear()
            tokenizer_denials = install_guard(work.parent,
                (request['source'], request['site_packages'], sys.base_prefix),
                blocked_roots=(work / 'cache', work / 'home'), generated_tensor_files=generated_files, profile=profile)
            full_model_checks = full_model_contract.probe_generated_models(
                importlib.import_module('library.sdxl_model_util'), importlib.import_module('torch'),
                importlib.import_module('safetensors.torch'), full_model_catalog, work)
            if denials or tokenizer_denials:
                report['reason'] = 'model_loading_boundary_failed'
                raise ValueError('model_loading_boundary_failed')
            bound_checks = {'parser': checks, 'model_loading': full_model_checks}
        if unet_contract is not None:
            report['reason'] = 'unet_step_failed'
            parser_denials = set(denials)
            denials.clear()
            tokenizer_denials = install_guard(work.parent,
                (request['source'], request['site_packages'], sys.base_prefix),
                read_files=selected_files, generated_tensor_files=generated_files, profile=profile)
            torch = importlib.import_module('torch')
            try:
                unet_contract.device_gate(torch)
                unet_checks = unet_contract.probe_steps(
                    importlib.import_module('library.sdxl_original_unet'), importlib.import_module('networks.lora'),
                    module, torch, importlib.import_module('safetensors.torch'), unet_catalog, work)
            except ValueError as exc:
                if str(exc) in {'unet_step_gpu_capacity_held', 'unet_step_cuda_precision_unavailable'}:
                    report['reason'] = str(exc)
                raise
            if denials or tokenizer_denials:
                report['reason'] = 'unet_step_boundary_failed'
                report['failure_site'] = denials.failure_site
                raise ValueError('unet_step_boundary_failed')
            bound_checks = {'parser': checks, 'unet_step': unet_checks}
        if loop_contract is not None:
            report['reason'] = 'training_loop_failed'
            parser_denials = set(denials)
            denials.clear()
            tokenizer_denials = install_guard(work.parent,
                (request['source'], request['site_packages'], sys.base_prefix),
                read_files=selected_files, generated_tensor_files=generated_files, profile=profile)
            torch = importlib.import_module('torch')
            try:
                loop_contract.device_gate(torch)
                loop_checks = loop_contract.probe_loop(
                    module, importlib.import_module('library.sdxl_model_util'), importlib.import_module('library.strategy_sdxl'),
                    importlib.import_module('transformers'), torch, importlib.import_module('safetensors.torch'),
                    importlib.import_module('PIL.Image'), toml_module, loop_catalog, work)
            except ValueError as exc:
                if str(exc) in {'training_loop_resources_held', 'unet_step_gpu_capacity_held', 'unet_step_cuda_precision_unavailable'}:
                    report['reason'] = str(exc)
                raise
            if denials or tokenizer_denials:
                report['reason'] = 'training_loop_boundary_failed'
                report['failure_site'] = denials.failure_site
                raise ValueError('training_loop_boundary_failed')
            bound_checks = {'parser': checks, 'training_loop': loop_checks}
        if image_contract is not None:
            report['reason'] = 'image_effects_failed'
            parser_denials = set(denials)
            denials.clear()
            tokenizer_denials = install_guard(work.parent,
                (request['source'], request['site_packages'], sys.base_prefix),
                blocked_roots=(work / 'cache', work / 'home'))
            image_checks = image_contract['probe_generated_images'](
                importlib.import_module('library.dataset'), importlib.import_module('library.subset'),
                importlib.import_module('library.dreambooth_dataset'), importlib.import_module('torch'),
                importlib.import_module('numpy'), work)
            if denials or tokenizer_denials:
                report['reason'] = 'image_effects_boundary_failed'
                raise ValueError('image_effects_boundary_failed')
            bound_checks = {'parser': checks, 'image_effects': image_checks}
        if package_contract is not None:
            report['reason'] = 'dataset_package_failed'
            parser_denials = set(denials)
            denials.clear()
            tokenizer_denials = install_guard(work.parent,
                (request['source'], request['site_packages'], sys.base_prefix),
                blocked_roots=(work / 'cache', work / 'home'))
            package_checks = package_contract.probe_generated_package(
                importlib.import_module('library.config_util'), module, importlib.import_module('library.dataset'),
                importlib.import_module('torch'), importlib.import_module('numpy'), package_catalog, work)
            if denials or tokenizer_denials:
                report['reason'] = 'dataset_package_boundary_failed'
                raise ValueError('dataset_package_boundary_failed')
            bound_checks = {'parser': checks, 'dataset_package': package_checks}
        if preparation_contract is not None and pairing_contract is None:
            report['reason'] = 'dataset_preparation_failed'
            parser_denials = set(denials)
            denials.clear()
            tokenizer_denials = install_guard(work.parent,
                (request['source'], request['site_packages'], sys.base_prefix, *dataset_roots),
                blocked_roots=(work / 'cache', work / 'home'))
            preparation_checks = preparation_contract.probe_package(
                importlib.import_module('library.config_util'), module, importlib.import_module('library.dataset'),
                importlib.import_module('torch'), importlib.import_module('numpy'), importlib.import_module('PIL.Image'),
                request['dataset_preparation'], work)
            if denials or tokenizer_denials:
                report['reason'] = 'dataset_preparation_boundary_failed'
                raise ValueError('dataset_preparation_boundary_failed')
            bound_checks = {'parser': checks, 'dataset_preparation': preparation_checks}
        if pairing_contract is not None:
            report['reason'] = 'model_pairing_failed'
            parser_denials = set(denials)
            denials.clear()
            tokenizer_denials = install_guard(work.parent,
                (request['source'], request['site_packages'], sys.base_prefix, *dataset_roots),
                blocked_roots=(work / 'cache', work / 'home'), generated_tensor_files=generated_files, profile=profile)
            if conditioning_contract is not None:
                report['reason'] = 'conditioning_failed'
                conditioning_checks = conditioning_contract.probe_conditioning(
                    importlib.import_module('library.sdxl_model_util'), importlib.import_module('torch'),
                    importlib.import_module('safetensors.torch'), pairing_catalog,
                    importlib.import_module('transformers').CLIPTokenizer,
                    importlib.import_module('library.strategy_sdxl').SdxlTokenizeStrategy,
                    importlib.import_module('library.strategy_sdxl').SdxlTextEncodingStrategy,
                    importlib.import_module('library.config_util'), module, importlib.import_module('library.dataset'),
                    importlib.import_module('library.sdxl_train_util'), importlib.import_module('numpy'),
                    importlib.import_module('PIL.Image'), request['dataset_preparation'], request['model_pairing'], work)
                pairing_checks = conditioning_checks['model_pairing_checks']
            else:
                pairing_checks = pairing_contract.probe_pairing(
                    importlib.import_module('library.sdxl_model_util'), importlib.import_module('torch'),
                    importlib.import_module('safetensors.torch'), pairing_catalog,
                    importlib.import_module('transformers').CLIPTokenizer,
                    importlib.import_module('library.strategy_sdxl').SdxlTokenizeStrategy,
                    importlib.import_module('library.config_util'), module, importlib.import_module('library.dataset'),
                    importlib.import_module('numpy'), importlib.import_module('PIL.Image'),
                    request['dataset_preparation'], request['model_pairing'], work)
            if denials or tokenizer_denials:
                report['reason'] = 'conditioning_boundary_failed' if conditioning_contract is not None else 'model_pairing_boundary_failed'
                raise ValueError('model_pairing_boundary_failed')
            preparation_checks = pairing_checks['dataset_preparation_checks']
            bound_checks = {'parser': checks, 'dataset_preparation': preparation_checks, 'model_pairing': pairing_checks}
            if conditioning_contract is not None:
                bound_checks['conditioning'] = conditioning_checks
        if request['schema_version'] in {14, 15, 16, 17}:
            report['reason'] = 'native_connectivity_failed'
            check_local_connectivity()
            local_checks = {'environment_profile': profile, 'offline_overrides_absent': True,
                'loopback_before_import': True,
                {14: 'loopback_after_loading', 15: 'loopback_after_tokenization', 16: 'loopback_after_optimizer', 17: 'loopback_after_training'}[request['schema_version']]: True}
            bound_checks['local_processing'] = local_checks
        if native is not None:
            report["reason"] = "native_observation_failed"
            ready = {"schema_version": 1, "nonce": request["nonce"], "phase": "parser_ready",
                "request_sha256": report["request_sha256"], "pid": os.getpid(), "checks_sha256": digest(bound_checks)}
            report["native_observation_sha256"] = native["wait_for_observation"](request["native_observation"]["mapping"], ready)
        report["checks"] = checks
        report.update(status="parser_checked", reason="synthetic_parser_only")
        if tokenizer_contract is not None:
            report.update(tokenizer_checks=tokenizer_checks, reason='synthetic_parser_and_tokenizers_only')
        if selected_contract is not None:
            report.update(selected_tokenizer_checks=selected_checks, reason='selected_tokenizers_and_synthetic_captions_only')
        if model_contract is not None:
            report.update(model_layout_checks=model_checks, reason='generated_meta_model_layout_only')
        if tensor_contract is not None:
            report.update(tensor_loading_checks=tensor_checks, reason='generated_local_tensor_loading_only')
        if full_model_contract is not None:
            report.update(model_loading_checks=full_model_checks, reason='generated_full_model_loading_only')
        if unet_contract is not None:
            report['unet_step_checks'] = unet_checks
        if loop_contract is not None:
            report['training_loop_checks'] = loop_checks
        if image_contract is not None:
            report.update(image_effects_checks=image_checks, reason='generated_image_effects_only')
        if package_contract is not None:
            report.update(dataset_package_checks=package_checks, reason='generated_dataset_package_only')
        if preparation_contract is not None:
            report.update(dataset_preparation_checks=preparation_checks, reason='selected_package_preparation_only')
        if pairing_contract is not None:
            report.update(model_pairing_checks=pairing_checks, reason='generated_model_pairing_only')
        if conditioning_contract is not None:
            report.update(conditioning_checks=conditioning_checks, reason='generated_encoder_conditioning_only')
        if request['schema_version'] in {14, 15, 16, 17}:
            report.update(local_processing_checks=local_checks, reason=('generated_training_loop_only' if request['schema_version'] == 17 else 'generated_unet_lora_step_only' if request['schema_version'] == 16 else 'generated_native_model_loading_only'
                if request['schema_version'] == 14 else 'native_selected_tokenizers_and_synthetic_captions_only'))
    except ProbeDenied as exc:
        # These codes are authored by this probe; never persist an imported
        # exception's text or a denied target path.
        known = {"network_or_process_denied", "external_write_denied",
                 "external_or_model_read_denied", "external_directory_denied"}
        report["reason"] = str(exc) if str(exc) in known else "probe_boundary_denied"
        report["denied_operation"] = exc.operation if exc.operation in PROBE_OPERATIONS else "none"
        report["failure_site"] = failure_site(exc, request["site_packages"]) or report['failure_site']
    except BaseException as exc:
        # Never persist exception messages/tracebacks from imported tools.
        # The process owner separately bounds and hashes opaque console output.
        report["failure_site"] = failure_site(exc, request["site_packages"]) or report['failure_site']
    report["blocked_operations"] = sorted(denials | parser_denials | tokenizer_denials)
    raw = json.dumps(report, ensure_ascii=False, sort_keys=True, allow_nan=False).encode("utf-8")
    with (work / "parser-result.json").open("xb") as handle:
        handle.write(raw)
        handle.flush()
        os.fsync(handle.fileno())
    return 0 if report["status"] == "parser_checked" else 2


if __name__ == "__main__":
    raise SystemExit(main())
