"""Project-interpreter helper: selected software I/O, never trainer execution."""
from __future__ import annotations

from dataclasses import asdict
import hashlib
import importlib.util
import io
import json
import mmap
from pathlib import Path
import re
import sys
from types import ModuleType
import zipfile

# These limits define the shared mapping before any project imports.
MAX_REQUEST, MAX_RESPONSE, MAX_PROGRESS = 256 * 1024, 512 * 1024, 1024


def main():
    if len(sys.argv) != 4:
        return 2
    name, length, digest = sys.argv[1:]
    if not re.fullmatch(r'Local\\CollectiGen\.Preflight\.[0-9a-f]{32}', name) or not length.isascii() or not length.isdecimal() or len(length) > 6 or not 0 < int(length) <= MAX_REQUEST or not re.fullmatch(r'[0-9a-f]{64}', digest):
        return 2
    with mmap.mmap(-1, int(length) + 4 + MAX_RESPONSE + 4 + MAX_PROGRESS, tagname=name, access=mmap.ACCESS_WRITE) as mapping:
        raw = mapping[:int(length)]
        if hashlib.sha256(raw).hexdigest() != digest:
            return 2
        request = json.loads(raw)
        if (type(request) is not dict or type(request.get('schema_version')) is not int
                or request['schema_version'] != 1 or request.get('nonce') != name.rsplit('.', 1)[1]
                or request.get('operation') not in ('inspect', 'prepare')):
            return 2

        if json.dumps(request, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8') != raw:
            return 2
        owned = request.pop('owned_sources', None)
        if type(owned) is not dict:
            return 2
        core = Path(__file__).resolve().parents[1] / 'core'
        loader_path = core / 'training_source_loader.py'
        with loader_path.open('rb') as handle:
            loader_bytes = handle.read(256 * 1024 + 1)
        loader_hash = hashlib.sha256(loader_bytes).hexdigest()
        if (len(loader_bytes) > 256 * 1024
                or loader_hash not in ('b6c91ee1eaac1ea1a70800723e126452f5545efc8b6789ccc2f2a6a3f7bcc1c4',
                                       '15ebc6afbf81d5c11f8859d1f8358588f59d80e463c590e4d95373e26724fb0e')
                or owned.get('training_source_loader') != {'sha256': loader_hash, 'size': len(loader_bytes)}):
            return 2
        source_loader = ModuleType('_collectigen_preflight_bootstrap')
        source_loader.__file__ = str(loader_path)
        sys.modules[source_loader.__name__] = source_loader
        exec(compile(loader_bytes, str(loader_path), 'exec', dont_inherit=True), source_loader.__dict__)
        files = {name: source_loader.SourceFile(core / (name + '.py'), row['sha256'], row['size'])
                 for name, row in owned.items()}
        class BundleLoader(source_loader._SourceOnlyLoader):
            def __init__(self, fullname, binding, owner, payload):
                super().__init__(fullname, binding, owner=owner)
                self.payload = payload

            def exec_module(self, module):
                owner = self.owner
                owner._loaded[self.name] = module
                try:
                    owner._current()
                    module.__cached__ = None
                    exec(compile(self.payload, str(self.binding.path), 'exec', dont_inherit=True), module.__dict__)
                    if not source_loader._matches(module, self):
                        raise source_loader.SourceLoaderError('owned_source_module_changed')
                    owner._current()
                except BaseException:
                    owner._loaded.pop(self.name, None)
                    raise

        class PreflightPackage(source_loader.OwnedSourcePackage):
            archive = None

            def find_spec(self, fullname, path=None, target=None):
                prefix = self.name + '.'
                if self.archive is not None and fullname.startswith(prefix):
                    name = fullname.removeprefix(prefix)
                    if name not in self.files and re.fullmatch(r'[A-Za-z_][A-Za-z0-9_]*', name):
                        self._current()
                        if path != [str(core)] or target is not None:
                            raise source_loader.SourceLoaderError('owned_source_import_location_changed')
                        try:
                            member = self.archive.getinfo('_collectigen_package_code/' + name + '.py')
                        except KeyError:
                            pass
                        else:
                            if member.file_size > source_loader.MAX_SOURCE_BYTES:
                                raise source_loader.SourceLoaderError('owned_source_changed')
                            payload = self.archive.read(member)
                            binding = source_loader.SourceFile(core / (name + '.py'),
                                hashlib.sha256(payload).hexdigest(), len(payload))
                            loader = BundleLoader(fullname, binding, self, payload)
                            return importlib.util.spec_from_file_location(fullname, str(binding.path), loader=loader)
                return super().find_spec(fullname, path, target)

        package = PreflightPackage('_collectigen_preflight', core, files)
        runtime = package.import_module('training_runtime_qualification')
        process = package.import_module('training_process')
        preflight = package.import_module('training_preflight')
        _bytes, _read = process._bytes, process._read
        if (MAX_REQUEST, MAX_RESPONSE, MAX_PROGRESS) != (preflight.MAX_REQUEST, preflight.MAX_RESPONSE, preflight.MAX_PROGRESS):
            return 2
        fields = {'schema_version', 'nonce', 'operation', 'source', 'environment', 'python_base', 'profile', 'catalog', 'catalog_sha256'}
        if type(request) is dict and 'synthetic_tokenizers' in request:
            if request['synthetic_tokenizers'] is not True or request.get('operation') != 'prepare' or not request.get('python_base'):
                return 2
            fields.add('synthetic_tokenizers')
        if type(request) is dict and 'selected_tokenizers' in request:
            selected = request['selected_tokenizers']
            if request.get('operation') != 'prepare' or not request.get('python_base') or type(selected) is not dict or set(selected) != {'memory', 'binding'}:
                return 2
            selection = runtime.selected_tokens.read_selection(selected['memory'])
            if runtime.selected_tokens.binding(selection) != selected['binding']:
                return 2
            fields.add('selected_tokenizers')
        if type(request) is dict and 'synthetic_model_layout' in request:
            if request['synthetic_model_layout'] is not True or request.get('operation') != 'prepare' or not request.get('python_base') or request.get('synthetic_tokenizers') or 'selected_tokenizers' in request:
                return 2
            fields.add('synthetic_model_layout')
        if type(request) is dict and 'synthetic_tensor_loading' in request:
            if request['synthetic_tensor_loading'] is not True or request.get('operation') != 'prepare' or not request.get('python_base') or request.get('synthetic_tokenizers') or request.get('synthetic_model_layout') or 'selected_tokenizers' in request:
                return 2
            fields.add('synthetic_tensor_loading')
        if type(request) is dict and 'synthetic_unet_step' in request:
            if request['synthetic_unet_step'] is not True or request.get('operation') != 'prepare' or not request.get('python_base') or any(request.get(key) for key in ('synthetic_training_loop', 'synthetic_tokenizers', 'synthetic_model_layout', 'synthetic_tensor_loading', 'synthetic_model_loading', 'native_model_loading', 'native_selected_tokenizers', 'synthetic_image_effects', 'synthetic_dataset_package', 'selected_tokenizers', 'selected_dataset', 'model_pairing', 'conditioning')):
                return 2
            fields.add('synthetic_unet_step')
        if type(request) is dict and 'synthetic_training_loop' in request:
            if request['synthetic_training_loop'] is not True or request.get('operation') != 'prepare' or not request.get('python_base') or any(request.get(key) for key in ('synthetic_unet_step', 'synthetic_tokenizers', 'synthetic_model_layout', 'synthetic_tensor_loading', 'synthetic_model_loading', 'native_model_loading', 'native_selected_tokenizers', 'synthetic_image_effects', 'synthetic_dataset_package', 'selected_tokenizers', 'selected_dataset', 'model_pairing', 'conditioning')):
                return 2
            fields.add('synthetic_training_loop')
        if type(request) is dict and 'native_selected_tokenizers' in request:
            if request['native_selected_tokenizers'] is not True or 'selected_tokenizers' not in request:
                raise ValueError('preflight_native_tokenizer_scope')
            fields.add('native_selected_tokenizers')
        if type(request) is dict and 'native_model_loading' in request:
            if request['native_model_loading'] is not True or request.get('synthetic_model_loading') is not True:
                raise ValueError('native_loading_scope')
            fields.add('native_model_loading')
        if type(request) is dict and 'synthetic_model_loading' in request:
            if request['synthetic_model_loading'] is not True or request.get('operation') != 'prepare' or not request.get('python_base') or any(request.get(key) for key in ('synthetic_tokenizers', 'synthetic_model_layout', 'synthetic_tensor_loading')) or 'selected_tokenizers' in request:
                return 2
            fields.add('synthetic_model_loading')
        if type(request) is dict and 'synthetic_image_effects' in request:
            if request['synthetic_image_effects'] is not True or request.get('operation') != 'prepare' or not request.get('python_base') or any(request.get(key) for key in ('synthetic_tokenizers', 'synthetic_model_layout', 'synthetic_tensor_loading', 'synthetic_model_loading')) or 'selected_tokenizers' in request:
                return 2
            fields.add('synthetic_image_effects')
        if type(request) is dict and 'synthetic_dataset_package' in request:
            if request['synthetic_dataset_package'] is not True or request.get('operation') != 'prepare' or not request.get('python_base') or any(request.get(key) for key in ('synthetic_tokenizers', 'synthetic_model_layout', 'synthetic_tensor_loading', 'synthetic_model_loading', 'synthetic_image_effects')) or 'selected_tokenizers' in request:
                return 2
            fields.add('synthetic_dataset_package')
        if type(request) is dict and 'selected_dataset' in request:
            selected_dataset = request['selected_dataset']
            if request.get('operation') != 'prepare' or not request.get('python_base') or type(selected_dataset) is not dict or set(selected_dataset) != {'memory', 'binding', 'summary'} or any(request.get(key) for key in ('synthetic_tokenizers', 'selected_tokenizers', 'synthetic_model_layout', 'synthetic_tensor_loading', 'synthetic_model_loading', 'synthetic_image_effects', 'synthetic_dataset_package')):
                return 2
            dataset_selection = runtime.dataset_preparation.read_selection(selected_dataset['memory'])
            if runtime.dataset_preparation.binding(dataset_selection) != selected_dataset['binding']:
                return 2
            fields.add('selected_dataset')
            fields.add('verification_bundle')
        if type(request) is dict and 'model_pairing' in request:
            if request.get('selected_dataset') is None:
                return 2
            runtime.model_pairing_contract.expected_report(request['selected_dataset'], request['model_pairing'])
            fields.add('model_pairing')
        if type(request) is dict and 'conditioning' in request:
            if request['conditioning'] is not True or request.get('model_pairing') is None:
                return 2
            fields.add('conditioning')
        if type(request) is not dict or set(request) != fields or type(request['schema_version']) is not int or request['schema_version'] != 1 or request['nonce'] != name.rsplit('.', 1)[1] or request['operation'] not in {'inspect', 'prepare'} or request['profile'] not in runtime.PROFILES:
            return 2
        for field in ('source', 'environment', 'catalog', 'python_base'):
            value = request[field]
            if field == 'python_base' and value == '':
                continue
            if type(value) is not str or not Path(value).is_absolute() or '\0' in value or len(value) > 32767:
                return 2
        if request['python_base'] and request['operation'] != 'prepare':
            return 2
        if any(Path.cwd().is_relative_to(Path(request[field])) for field in ('source', 'environment', 'python_base') if request[field]):
            return 2
        report = {'schema_version': 1, 'nonce': request['nonce'], 'request_sha256': digest,
            'status': 'held', 'result': None}
        def progress(done, total, message):
            value = _bytes({'phase': 'bytes' if total else 'entries', 'done': done, 'total': total})
            offset = int(length) + 4 + MAX_RESPONSE
            mapping[offset:offset + 4] = b'\0' * 4
            mapping[offset + 4:offset + 4 + len(value)] = value
            mapping[offset:offset + 4] = len(value).to_bytes(4, 'little')
        try:
            if request.get('selected_dataset') is not None:
                if Path.cwd().is_relative_to(Path(dataset_selection['root'])):
                    return 2
                bundle = request['verification_bundle']
                expected_path = Path.cwd().parents[1] / ('verification-code-' + request['nonce'] + '.zip')
                if type(bundle) is not dict or set(bundle) != {'path', 'sha256'} or bundle['path'] != str(expected_path):
                    return 2
                with expected_path.open('rb') as handle:
                    archive_bytes = handle.read(33 * 1024 * 1024 + 1)
                if len(archive_bytes) > 33 * 1024 * 1024 or runtime._sha(archive_bytes) != bundle['sha256']:
                    return 2
                package.archive = zipfile.ZipFile(io.BytesIO(archive_bytes))
                members = package.archive.infolist()
                if (len({member.filename for member in members}) != len(members)
                        or any(not member.filename.endswith('.py') or member.compress_type != zipfile.ZIP_STORED
                               for member in members)):
                    return 2
                version_member = package.archive.getinfo('version.py')
                if version_member.file_size > source_loader.MAX_SOURCE_BYTES or 'version' in sys.modules:
                    return 2
                version_path = core.parent / 'version.py'
                version_module = ModuleType('version')
                version_module.__file__ = str(version_path)
                sys.modules['version'] = version_module
                exec(compile(package.archive.read(version_member), str(version_path), 'exec',
                             dont_inherit=True), version_module.__dict__)
                project_root = Path(__file__).resolve().parents[1]
                project_site = Path(sys.executable).parents[1] / 'Lib/site-packages'
                sys.path[:0] = [str(project_site), str(project_root / 'tools')]
                from training_runtime_probe import install_guard
                def install_dataset_guard():
                    return install_guard(Path.cwd(), (project_root / 'tools',
                        project_site, sys.base_prefix, request['source'], request['environment'], request['python_base'], dataset_selection['root']),
                        blocked_roots=(project_root / 'data/user', project_root / 'data/cache'),
                        # Python 3.13 -S can report the venv Scripts directory as base_prefix.
                        # Permit only the running base interpreter's standard-library archive.
                        read_files=((Path(sys._base_executable).resolve().parent / f'python{sys.version_info.major}{sys.version_info.minor}.zip').resolve(),
                            project_root / 'version.py', expected_path, runtime.CONTRACT, runtime.NATIVE_PROTOCOL,
                            *(binding.path for binding in files.values()),
                            *runtime.DATASET_PACKAGE_CODE, *runtime.MODEL_PAIRING_CODE, project_root / 'core/training_dataset_preparation.py',
                            project_root / 'core/training_configuration.py'))
            runtime.CATALOG = Path(request['catalog'])
            if hashlib.sha256(_read(runtime.CATALOG)).hexdigest() != request['catalog_sha256']:
                return 2
            service = runtime.TrainingRuntimeQualificationService()
            inspection = service.inspect(request['source'], request['environment'], profile=request['profile'])
            plan = parser_request = None
            if request['operation'] == 'prepare' and inspection.probe_ready:
                if request.get('selected_tokenizers') is not None:
                    runtime.selected_tokens.inspect_selection(selection)
                software = service.inspect_software(inspection, request['python_base'], progress=progress) if request['python_base'] else ()
                if request.get('selected_dataset') is not None:
                    dataset_denials = install_dataset_guard()
                prepared = Path.cwd() / 'prepared'
                prepared.mkdir()
                plan, parser_request = service.prepare_parser(inspection, prepared, software=software,
                    synthetic_tokenizers=request.get('synthetic_tokenizers', False),
                    selected_tokenizers=request.get('selected_tokenizers'),
                    native_selected_tokenizers=request.get('native_selected_tokenizers', False),
                    synthetic_model_layout=request.get('synthetic_model_layout', False),
                    synthetic_tensor_loading=request.get('synthetic_tensor_loading', False),
                    synthetic_model_loading=request.get('synthetic_model_loading', False),
                    native_model_loading=request.get('native_model_loading', False),
                    synthetic_unet_step=request.get('synthetic_unet_step', False),
                    synthetic_training_loop=request.get('synthetic_training_loop', False),
                    synthetic_image_effects=request.get('synthetic_image_effects', False),
                    synthetic_dataset_package=request.get('synthetic_dataset_package', False),
                    selected_dataset=request.get('selected_dataset'), model_pairing=request.get('model_pairing'), conditioning=request.get('conditioning', False))
                if request.get('selected_dataset') is not None and dataset_denials:
                    raise ValueError('dataset_preflight_boundary_failed')
            report.update(status='complete', result={'inspection': asdict(inspection),
                'plan': asdict(plan) if plan else None, 'request': parser_request})
        except BaseException:
            pass  # Imported exceptions and selected paths never enter evidence.
        reply = _bytes(report)
        if len(reply) > MAX_RESPONSE:
            return 2
        mapping[int(length) + 4:int(length) + 4 + len(reply)] = reply
        mapping[int(length):int(length) + 4] = len(reply).to_bytes(4, 'little')
    return 0  # A held result still requires an independently verified envelope.


if __name__ == '__main__':
    try:
        raise SystemExit(main())
    except SystemExit:
        raise
    except BaseException:
        raise SystemExit(2) from None
