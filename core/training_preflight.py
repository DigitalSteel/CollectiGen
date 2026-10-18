"""Contained trainer inspection/preparation with memory-only control messages.

Only project runtime/code and the owned control workspace are read by the
controller. Selected software I/O belongs to an independently stopped helper.
No model loading, training, downloads, or application launch authority is added.
"""
from __future__ import annotations

from dataclasses import asdict
import json
import mmap
import os
from pathlib import Path
import re
import sys
from uuid import uuid4

from . import training_runtime_qualification as runtime
from .training_file_inventory import SoftwareInventoryService
from .training_process import (ProcessInput, ProcessInventory, TrainingProcessError,
    TrainingProcessPlan, TrainingProcessService, TrainingRunJournal, _bytes, _read, _sha, _write_new)

WORKER = runtime.ROOT / 'tools/training_preflight_worker.py'
MAX_REQUEST = 256 * 1024
MAX_RESPONSE = 512 * 1024
MAX_PROGRESS = 1024
PREFLIGHT_SECONDS = 300
SHA = re.compile(r'[0-9a-f]{64}')


def _inputs(rows):
    if type(rows) is not list or len(rows) > 128:
        raise ValueError('preflight_input_bound')
    result = []
    for row in rows:
        if type(row) is not dict or set(row) != {'path', 'state', 'sha256'} or type(row['path']) is not str or not Path(row['path']).is_absolute() or len(row['path']) > 32767 or type(row['state']) is not list or len(row['state']) != 5 or any(type(n) is not int or n < 0 for n in row['state']) or type(row['sha256']) is not str or not SHA.fullmatch(row['sha256']):
            raise ValueError('preflight_input_identity')
        result.append(ProcessInput(row['path'], tuple(row['state']), row['sha256']))
    if len({item.path for item in result}) != len(result):
        raise ValueError('preflight_duplicate_input')
    return tuple(result)


def decode_result(value, request, helper_plan):
    """Validate bounded memory data without touching any selected path."""
    if type(value) is not dict or set(value) != {'inspection', 'plan', 'request'}:
        raise ValueError('preflight_result_fields')
    row = value['inspection']
    if type(row) is not dict or set(row) != set(runtime.RuntimeInspection.__dataclass_fields__):
        raise ValueError('preflight_inspection_fields')
    expected = {'source': request['source'], 'profile': request['profile'],
        'interpreter': str(Path(request['environment']) / 'Scripts/python.exe'),
        'site_packages': str(Path(request['environment']) / 'Lib/site-packages')}
    if any(row[key] != item for key, item in expected.items()) or type(row['catalog_sha256']) is not str or row['catalog_sha256'] != request['catalog_sha256']:
        raise ValueError('preflight_selection_changed')
    catalog_raw = _read(Path(request['catalog']))
    if _sha(catalog_raw) != request['catalog_sha256']:
        raise ValueError('preflight_catalog_changed')
    catalog = runtime._json(catalog_raw)
    source_names = catalog['files']
    blockers = {'interpreter_unavailable', 'package_directory_unavailable'}
    blockers.update('source_missing_or_changed:' + name for name in source_names)
    blockers.update(prefix + name for prefix in ('package_missing_or_ambiguous:',
        'package_metadata_unavailable:', 'package_metadata_invalid:', 'package_version_mismatch:') for name in runtime.PROFILES[request['profile']])
    if type(row['blockers']) is not list or len(row['blockers']) > 128 or any(type(item) is not str or item not in blockers for item in row['blockers']) or type(row['source_files']) is not list or len(row['source_files']) > 111 or any(type(item) is not str or item not in source_names for item in row['source_files']) or len(set(row['source_files'])) != len(row['source_files']):
        raise ValueError('preflight_inspection_scope')
    if type(row['versions']) is not list or len(row['versions']) > len(runtime.PROFILES[request['profile']]) or any(type(item) is not list or len(item) != 2 or type(item[0]) is not str or item[0] not in runtime.PROFILES[request['profile']] or type(item[1]) is not str or not re.fullmatch(r'[A-Za-z0-9.+!_-]{1,80}', item[1]) for item in row['versions']) or len({item[0] for item in row['versions']}) != len(row['versions']):
        raise ValueError('preflight_versions')
    inspection = runtime.RuntimeInspection(**{**row, 'inputs': _inputs(row['inputs']),
        'blockers': tuple(row['blockers']), 'source_files': tuple(row['source_files']),
        'versions': tuple(tuple(item) for item in row['versions'])})
    prepared = value['plan']
    if request['operation'] == 'inspect' or not inspection.probe_ready:
        if prepared is not None or value['request'] is not None:
            raise ValueError('preflight_unexpected_plan')
        return inspection, None, None
    if type(prepared) is not dict or set(prepared) != set(TrainingProcessPlan.__dataclass_fields__) or type(prepared['inventories']) is not list or len(prepared['inventories']) not in ({3} if request.get('selected_dataset') or request.get('selected_tokenizers') else {0, 2}):
        raise ValueError('preflight_plan_fields')
    if type(prepared['command']) is not list or type(prepared['root_identity']) is not list or len(prepared['root_identity']) != 2 or any(type(n) is not int or n < 0 for n in prepared['root_identity']):
        raise ValueError('preflight_plan_identity')
    inventories = tuple(ProcessInventory(**item) for item in prepared['inventories'])
    plan = TrainingProcessPlan(**{**prepared, 'command': tuple(prepared['command']),
        'root_identity': tuple(prepared['root_identity']), 'inputs': _inputs(prepared['inputs']), 'inventories': inventories})
    plan.validate()
    parser = value['request']
    if type(parser) is not dict or type(parser.get('nonce')) is not str or not re.fullmatch(r'[0-9a-f]{32}', parser['nonce']):
        raise ValueError('preflight_parser_request')
    inputs = Path(plan.root) / ('parser-inputs-' + parser['nonce'])
    expected_parser = {'schema_version': 1, 'nonce': parser['nonce'],
        'source_revision': runtime.SOURCE_REVISION, 'source': str(inputs / 'source'),
        'site_packages': inspection.site_packages, 'contract': str(inputs / runtime.CONTRACT.name)}
    if request['python_base']:
        expected_parser.update(schema_version=3,
            runtime_roots={'environment': request['environment'], 'python_base': request['python_base']},
            native_observation={'mapping': 'Local\\CollectiGen.Native.' + parser['nonce'], 'protocol': str(inputs / runtime.NATIVE_PROTOCOL.name)})
    if request.get('synthetic_tokenizers'):
        if not request['python_base']:
            raise ValueError('preflight_tokenizer_inventory_required')
        expected_parser.update(schema_version=4, tokenizer_contract=str(inputs / runtime.TOKENIZER_CONTRACT.name))
    if request.get('selected_tokenizers') is not None:
        selected = request['selected_tokenizers']
        selection = runtime.selected_tokens.read_selection(selected['memory'])
        if not request['python_base'] or runtime.selected_tokens.binding(selection) != selected['binding']:
            raise ValueError('preflight_selected_tokenizer_binding')
        expected_parser.update(schema_version=5, tokenizer_contract=str(inputs / runtime.TOKENIZER_CONTRACT.name),
            selected_tokenizers={**selected, 'code_bundle': str(inputs / runtime.SELECTED_TOKENIZER_BUNDLE)})
        if not set(map(str, runtime.selected_tokens.input_paths(selection))) <= {item.path for item in plan.inputs}:
            raise ValueError('preflight_selected_tokenizer_inputs')
        bundle = str(inputs / runtime.SELECTED_TOKENIZER_BUNDLE)
        expected_code = {bundle: _sha(runtime.selected_tokenizer_bundle()),
                         str(inputs / runtime.TOKENIZER_CONTRACT.name): _sha(_read(runtime.TOKENIZER_CONTRACT))}
        captured = {item.path: item.sha256 for item in plan.inputs}
        if any(captured.get(path) != digest for path, digest in expected_code.items()):
            raise ValueError('preflight_selected_tokenizer_code')
    if request.get('synthetic_model_layout'):
        if not request['python_base'] or request.get('synthetic_tokenizers') or request.get('selected_tokenizers') is not None:
            raise ValueError('preflight_model_layout_scope')
        contract = str(inputs / runtime.MODEL_CONTRACT.name)
        expected_parser.update(schema_version=6, model_contract=contract)
        if {item.path: item.sha256 for item in plan.inputs}.get(contract) != _sha(_read(runtime.MODEL_CONTRACT)):
            raise ValueError('preflight_model_layout_code')
    if request.get('synthetic_tensor_loading'):
        if not request['python_base'] or request.get('synthetic_tokenizers') or request.get('synthetic_model_layout') or request.get('selected_tokenizers') is not None:
            raise ValueError('preflight_tensor_loading_scope')
        contract = str(inputs / runtime.TENSOR_CONTRACT.name)
        expected_parser.update(schema_version=7, tensor_contract=contract)
        if {item.path: item.sha256 for item in plan.inputs}.get(contract) != _sha(_read(runtime.TENSOR_CONTRACT)):
            raise ValueError('preflight_tensor_loading_code')
    if request.get('synthetic_model_loading'):
        if not request['python_base'] or any(request.get(key) for key in ('synthetic_tokenizers', 'synthetic_model_layout', 'synthetic_tensor_loading')) or request.get('selected_tokenizers') is not None:
            raise ValueError('preflight_model_loading_scope')
        bundle = str(inputs / runtime.MODEL_LOADING_BUNDLE)
        catalog = str(inputs / runtime.MODEL_LOADING_CATALOG.name)
        expected_parser.update(schema_version=8, model_loading_bundle=bundle, model_loading_catalog=catalog)
        captured = {item.path: item.sha256 for item in plan.inputs}
        if captured.get(bundle) != _sha(runtime.model_loading_bundle()) or captured.get(catalog) != runtime.model_loading.CATALOG_SHA256 or plan.timeout_seconds != (1200 if request.get('native_model_loading') else 300):
            raise ValueError('preflight_model_loading_code')
    if request.get('synthetic_image_effects'):
        if not request['python_base'] or any(request.get(key) for key in ('synthetic_tokenizers', 'synthetic_model_layout', 'synthetic_tensor_loading', 'synthetic_model_loading')) or request.get('selected_tokenizers') is not None:
            raise ValueError('preflight_image_effects_scope')
        contract = str(inputs / runtime.IMAGE_EFFECTS_CONTRACT.name)
        expected_parser.update(schema_version=9, image_effects_contract=contract)
        if {item.path: item.sha256 for item in plan.inputs}.get(contract) != _sha(_read(runtime.IMAGE_EFFECTS_CONTRACT)):
            raise ValueError('preflight_image_effects_code')
    if request.get('synthetic_dataset_package'):
        if not request['python_base'] or any(request.get(key) for key in ('synthetic_tokenizers', 'synthetic_model_layout', 'synthetic_tensor_loading', 'synthetic_model_loading', 'synthetic_image_effects')) or request.get('selected_tokenizers') is not None:
            raise ValueError('preflight_dataset_package_scope')
        bundle = str(inputs / runtime.DATASET_PACKAGE_BUNDLE)
        catalog = str(inputs / runtime.DATASET_PACKAGE_CATALOG.name)
        expected_parser.update(schema_version=10, dataset_package_bundle=bundle, dataset_package_catalog=catalog)
        captured = {item.path: item.sha256 for item in plan.inputs}
        if captured.get(bundle) != _sha(runtime.dataset_package_bundle()) or captured.get(catalog) != runtime.dataset_package.CATALOG_SHA256:
            raise ValueError('preflight_dataset_package_code')
    if request.get('selected_dataset') is not None:
        selected = request['selected_dataset']
        selection = runtime.dataset_preparation.read_selection(selected['memory'])
        if not request['python_base'] or runtime.dataset_preparation.binding(selection) != selected['binding'] or any(request.get(key) for key in ('synthetic_tokenizers', 'selected_tokenizers', 'synthetic_model_layout', 'synthetic_tensor_loading', 'synthetic_model_loading', 'synthetic_image_effects', 'synthetic_dataset_package')):
            raise ValueError('preflight_dataset_preparation_scope')
        expected_parser.update(schema_version=11, dataset_preparation={
            'package': str(inputs / 'dataset-package'), 'binding': selected['binding'],
            'summary': selected['summary'], 'code_bundle': str(inputs / runtime.DATASET_PREPARATION_BUNDLE)})
        if {item.path: item.sha256 for item in plan.inputs}.get(str(inputs / runtime.DATASET_PREPARATION_BUNDLE)) != _sha(runtime.dataset_preparation_bundle()) or plan.timeout_seconds != 300:
            raise ValueError('preflight_dataset_preparation_code')
        inventory = plan.inventories[2]
        if inventory.root != str(inputs / 'dataset-package') or inventory.manifest_path != str(inputs / 'dataset-inventory.json'):
            raise ValueError('preflight_dataset_inventory_scope')
    if request.get('model_pairing') is not None:
        if not request.get('selected_dataset'):
            raise ValueError('preflight_model_pairing_scope')
        runtime.model_pairing_contract.expected_report(request['selected_dataset'], request['model_pairing'])
        expected_parser.update(schema_version=12, model_pairing=request['model_pairing'],
            model_pairing_bundle=str(inputs / runtime.MODEL_PAIRING_BUNDLE),
            model_pairing_catalog=str(inputs / runtime.MODEL_LOADING_CATALOG.name))
        captured = {item.path: item.sha256 for item in plan.inputs}
        if captured.get(expected_parser['model_pairing_bundle']) != _sha(runtime.model_pairing_bundle()) or captured.get(expected_parser['model_pairing_catalog']) != runtime.model_loading.CATALOG_SHA256:
            raise ValueError('preflight_model_pairing_code')
    if 'conditioning' in request:
        if request['conditioning'] is not True or request.get('model_pairing') is None:
            raise ValueError('preflight_conditioning_scope')
        expected_parser.update(schema_version=13, conditioning=True)
    expected_profile = 'diagnostic-offline-v1'
    if request.get('native_model_loading'):
        if request['native_model_loading'] is not True or request.get('synthetic_model_loading') is not True:
            raise ValueError('preflight_native_loading_scope')
        expected_profile = 'local-processing-v1'
        expected_parser.update(schema_version=14, environment_profile=expected_profile)
    if request.get('native_selected_tokenizers'):
        if request['native_selected_tokenizers'] is not True or expected_parser['schema_version'] != 5:
            raise ValueError('preflight_native_tokenizer_scope')
        expected_profile = 'local-processing-v1'
        expected_parser.update(schema_version=15, environment_profile=expected_profile)
    if request.get('synthetic_unet_step'):
        if request['synthetic_unet_step'] is not True or expected_parser['schema_version'] != 3:
            raise ValueError('preflight_unet_step_scope')
        expected_profile = 'local-processing-v1'
        bundle, catalog = str(inputs / runtime.UNET_STEP_BUNDLE), str(inputs / runtime.MODEL_LOADING_CATALOG.name)
        expected_parser.update(schema_version=16, environment_profile=expected_profile,
            unet_step_bundle=bundle, unet_step_catalog=catalog)
        captured = {item.path: item.sha256 for item in plan.inputs}
        if captured.get(bundle) != _sha(runtime.unet_step_bundle()) or captured.get(catalog) != runtime.model_loading.CATALOG_SHA256 or plan.timeout_seconds != 1200:
            raise ValueError('preflight_unet_step_code')
    if request.get('synthetic_training_loop'):
        if request['synthetic_training_loop'] is not True or expected_parser['schema_version'] != 3:
            raise ValueError('preflight_training_loop_scope')
        expected_profile = 'local-processing-v1'
        bundle, catalog = str(inputs / runtime.TRAINING_LOOP_BUNDLE), str(inputs / runtime.MODEL_LOADING_CATALOG.name)
        expected_parser.update(schema_version=17, environment_profile=expected_profile,
            training_loop_bundle=bundle, training_loop_catalog=catalog)
        captured = {item.path: item.sha256 for item in plan.inputs}
        if captured.get(bundle) != _sha(runtime.training_loop_bundle()) or captured.get(catalog) != runtime.model_loading.CATALOG_SHA256 or plan.timeout_seconds != 1200:
            raise ValueError('preflight_training_loop_code')
    if plan.environment_profile != expected_profile or (expected_profile == 'local-processing-v1' and (plan.schema_version != 3 or len(plan.inventories) != (3 if request.get('selected_tokenizers') else 2))):
        raise ValueError('preflight_environment_profile')
    expected_command = (inspection.interpreter, '-I', '-S', '-B',
        *(('-X', 'utf8') if request.get('synthetic_training_loop') else ()),
        str(inputs / runtime.PROBE.name), str(inputs / 'request.json'))
    if plan.root != str(helper_plan.destination / 'work/prepared') or plan.name != 'parser-run-' + parser['nonce'] or plan.preview_fingerprint != inspection.fingerprint or plan.command != expected_command or parser != expected_parser or bool(plan.inventories) != bool(request['python_base']):
        raise ValueError('preflight_preparation_authority')
    if request['python_base']:
        for item, role in zip(plan.inventories[:2], ('environment', 'python_base')):
            if item.root != expected_parser['runtime_roots'][role] or item.manifest_path != str(inputs / (role + '-inventory.json')):
                raise ValueError('preflight_inventory_authority')
    # The complete preparation authority is checked before reading this owned
    # manifest; untrusted child-returned paths must not choose controller reads.
    runtime.verify_selected_tokenizer_source_inventory(inspection,plan,parser)
    return inspection, plan, parser


class PreflightReply:
    receipt_key = 'preflight_receipt_sha256'
    bytes_message = 'Verifying selected software bytes'
    entries_message = 'Checking selected software directory entries'

    def decode(self, value, plan):
        return decode_result(value, self.request, plan)

    def __init__(self, request, progress=None):
        self.request = request
        self.raw = _bytes(request)
        if len(self.raw) > MAX_REQUEST:
            raise TrainingProcessError('The preflight request exceeds its bound.')
        self.name = 'Local\\CollectiGen.Preflight.' + request['nonce']
        self.progress, self.last_progress = progress, None
        self.mapping = mmap.mmap(-1, len(self.raw) + 4 + MAX_RESPONSE + 4 + MAX_PROGRESS, tagname=self.name, access=mmap.ACCESS_WRITE)
        if self.mapping[:] != b'\0' * len(self.mapping):
            self.mapping.close()
            raise TrainingProcessError('The preflight memory identity is already occupied.')
        self.mapping[:len(self.raw)] = self.raw
        self.value = self.receipt = self.decoded = None

    def poll(self, plan, owner, cancel):
        # Progress has no completion authority. Inconsistent updates are skipped,
        # and only bounded counts plus fixed controller-authored text are exposed.
        if self.progress is None:
            return
        start = len(self.raw) + 4 + MAX_RESPONSE
        first = self.mapping[start:start + 4 + MAX_PROGRESS]
        if first != self.mapping[start:start + 4 + MAX_PROGRESS]:
            return
        size = int.from_bytes(first[:4], 'little')
        if not 0 < size <= MAX_PROGRESS:
            return
        try:
            value = runtime._json(first[4:4 + size])
            if type(value) is not dict or set(value) != {'phase', 'done', 'total'} or value['phase'] not in {'entries', 'bytes'} or any(type(value[key]) is not int or not 0 <= value[key] <= 64 * 1024 ** 3 for key in ('done', 'total')):
                return
        except (ValueError, TypeError, UnicodeError):
            return
        if value != self.last_progress:
            self.last_progress = value
            self.progress(value['done'], value['total'], self.bytes_message if value['phase'] == 'bytes' else self.entries_message)

    def finish(self, plan):
        start = len(self.raw)
        size = int.from_bytes(self.mapping[start:start + 4], 'little')
        if not 0 < size <= MAX_RESPONSE or self.mapping[:start] != self.raw:
            raise TrainingProcessError('The preflight reply is missing, changed or excessive.')
        raw = self.mapping[start + 4:start + 4 + size]
        value = runtime._json(raw)
        expected = {'schema_version': 1, 'nonce': self.request['nonce'], 'request_sha256': _sha(self.raw)}
        if type(value) is not dict or _bytes(value) != raw or set(value) != set(expected) | {'status', 'result'} or any(type(value[key]) is not type(item) or value[key] != item for key, item in expected.items()) or value['status'] not in {'complete', 'held'}:
            raise TrainingProcessError('The preflight reply is invalid or stale.')
        if value['status'] == 'held':
            if value['result'] is not None:
                raise TrainingProcessError('The held preflight reply contains unexpected data.')
        else:
            self.decoded = self.decode(value['result'], plan)
        receipt = {**expected, 'operation': self.request['operation'],
            'process_fingerprint': plan.fingerprint, 'result_sha256': _sha(raw), 'status': value['status']}
        _write_new(plan.destination / 'preflight-result.json', _bytes(receipt))
        self.value, self.receipt = value, receipt
        return _sha(_bytes(receipt))

    def close(self):
        self.mapping.close()


class ContainedRuntimeQualificationService:
    def __init__(self):
        # Retain this service so an unobserved helper can be recovered through
        # the same held coordinator slot; never hide it in a temporary owner.
        self.process = TrainingProcessService()

    def preflight(self, source, environment, root, jobs, *, profile='sd-scripts-requirements',
                  prepare=False, python_base=None, should_cancel=None, progress=None, timeout_seconds=PREFLIGHT_SECONDS,
                  synthetic_tokenizers=False, selected_tokenizers=None, native_selected_tokenizers=False, synthetic_model_layout=False, synthetic_tensor_loading=False, synthetic_model_loading=False, native_model_loading=False, synthetic_image_effects=False, synthetic_dataset_package=False, selected_dataset=None, model_pairing=None, conditioning=False, synthetic_unet_step=False, synthetic_training_loop=False):
        # Collapse lexical parent segments before testing workspace containment.
        # No resolve()/stat() of a selected directory is needed on the controller.
        source, environment, root = (Path(os.path.abspath(item)) for item in (source, environment, root))
        base = Path(os.path.abspath(python_base)) if python_base is not None else None
        if profile not in runtime.PROFILES or (base is not None and not prepare):
            raise TrainingProcessError('Unsupported preflight profile or software selection.')
        if type(synthetic_tokenizers) is not bool or (synthetic_tokenizers and (not prepare or base is None)):
            raise TrainingProcessError('The generated tokenizer probe requires parser and full software selection.')
        if type(synthetic_model_layout) is not bool or (synthetic_model_layout and (not prepare or base is None or synthetic_tokenizers or selected_tokenizers is not None)):
            raise TrainingProcessError('The meta-model probe requires parser, full software selection and its own experiment.')
        if type(synthetic_tensor_loading) is not bool or (synthetic_tensor_loading and (not prepare or base is None or synthetic_tokenizers or synthetic_model_layout or selected_tokenizers is not None)):
            raise TrainingProcessError('The generated tensor probe requires parser, full software selection and its own experiment.')
        if type(synthetic_unet_step) is not bool or (synthetic_unet_step and (not prepare or base is None or any((synthetic_training_loop, synthetic_tokenizers, native_selected_tokenizers, synthetic_model_layout, synthetic_tensor_loading, synthetic_model_loading, native_model_loading, synthetic_image_effects, synthetic_dataset_package, conditioning)) or any(value is not None for value in (selected_tokenizers, selected_dataset, model_pairing)))):
            raise TrainingProcessError('The generated GPU step requires parser, full software selection and its own experiment.')
        if type(synthetic_training_loop) is not bool or (synthetic_training_loop and (not prepare or base is None or any((synthetic_unet_step, synthetic_tokenizers, native_selected_tokenizers, synthetic_model_layout, synthetic_tensor_loading, synthetic_model_loading, native_model_loading, synthetic_image_effects, synthetic_dataset_package, conditioning)) or any(value is not None for value in (selected_tokenizers, selected_dataset, model_pairing)))):
            raise TrainingProcessError('The generated GPU step requires parser, full software selection and its own experiment.')
        if type(native_selected_tokenizers) is not bool or (native_selected_tokenizers and selected_tokenizers is None):
            raise TrainingProcessError('Native tokenizer qualification requires an exact selected pair.')
        if type(native_model_loading) is not bool or (native_model_loading and not synthetic_model_loading):
            raise TrainingProcessError("Native loading requires the full generated model experiment.")
        if type(synthetic_model_loading) is not bool or (synthetic_model_loading and (not prepare or base is None or synthetic_tokenizers or synthetic_model_layout or synthetic_tensor_loading or selected_tokenizers is not None)):
            raise TrainingProcessError('The full generated model probe requires parser, full software selection and its own experiment.')
        if type(synthetic_image_effects) is not bool or (synthetic_image_effects and (not prepare or base is None or synthetic_tokenizers or synthetic_model_layout or synthetic_tensor_loading or synthetic_model_loading or selected_tokenizers is not None)):
            raise TrainingProcessError('The generated image probe requires parser, full software selection and its own experiment.')
        if type(synthetic_dataset_package) is not bool or (synthetic_dataset_package and (not prepare or synthetic_image_effects or base is None or synthetic_tokenizers or synthetic_model_layout or synthetic_tensor_loading or synthetic_model_loading or selected_tokenizers is not None)):
            raise TrainingProcessError('The generated dataset-package probe requires parser, full software selection and its own experiment.')
        if selected_dataset is not None:
            if not prepare or base is None or any((synthetic_tokenizers, synthetic_model_layout, synthetic_tensor_loading, synthetic_model_loading, synthetic_image_effects, synthetic_dataset_package)) or selected_tokenizers is not None or type(selected_dataset) is not dict or set(selected_dataset) != {'memory', 'binding', 'summary'}:
                raise TrainingProcessError('Dataset preparation requires its own parser and full-software selection.')
            dataset_selection = runtime.dataset_preparation.read_selection(selected_dataset['memory'])
            if runtime.dataset_preparation.binding(dataset_selection) != selected_dataset['binding'] or root.is_relative_to(Path(dataset_selection['root'])):
                raise TrainingProcessError('The selected dataset binding or workspace changed.')
            runtime.dataset_preparation.expected_report(selected_dataset)
        if model_pairing is not None:
            if selected_dataset is None:
                raise TrainingProcessError('Generated model pairing requires exact dataset preparation.')
            runtime.model_pairing_contract.expected_report(selected_dataset, model_pairing)
        if type(conditioning) is not bool or (conditioning and model_pairing is None):
            raise TrainingProcessError('Generated conditioning requires generated model pairing.')
        if selected_tokenizers is not None:
            if not prepare or base is None or type(selected_tokenizers) is not dict or set(selected_tokenizers) != {'memory', 'binding'}:
                raise TrainingProcessError('Selected tokenizer probing requires full software selection.')
            selection = runtime.selected_tokens.read_selection(selected_tokenizers['memory'])
            if runtime.selected_tokens.binding(selection) != selected_tokenizers['binding']:
                raise TrainingProcessError('Selected tokenizer binding changed.')
            if any(root.is_relative_to(Path(row['path'])) for row in selection['slots']):
                raise TrainingProcessError('Keep probe work outside selected tokenizer directories.')
        if any(root.is_relative_to(path) for path in (source, environment, *((base,) if base else ()))):
            raise TrainingProcessError('Keep preflight work outside selected source and software.')
        catalog = _read(runtime.CATALOG)
        request = {'schema_version': 1, 'nonce': uuid4().hex,
            'operation': 'prepare' if prepare else 'inspect', 'source': str(source),
            'environment': str(environment), 'python_base': str(base) if base else '',
            'profile': profile, 'catalog': str(runtime.CATALOG), 'catalog_sha256': _sha(catalog)}
        if synthetic_tokenizers:
            request['synthetic_tokenizers'] = True
        if selected_tokenizers is not None:
            request['selected_tokenizers'] = selected_tokenizers
        if synthetic_model_layout:
            request['synthetic_model_layout'] = True
        if synthetic_tensor_loading:
            request['synthetic_tensor_loading'] = True
        if synthetic_unet_step:
            request['synthetic_unet_step'] = True
        if synthetic_training_loop:
            request['synthetic_training_loop'] = True
        if synthetic_model_loading:
            request['synthetic_model_loading'] = True
        if native_model_loading:
            request['native_model_loading'] = True
        if native_selected_tokenizers:
            request['native_selected_tokenizers'] = True
        if synthetic_image_effects:
            request['synthetic_image_effects'] = True
        if synthetic_dataset_package:
            request['synthetic_dataset_package'] = True
        if selected_dataset is not None:
            request['selected_dataset'] = selected_dataset
        if model_pairing is not None:
            request['model_pairing'] = model_pairing
        if conditioning:
            request['conditioning'] = True
        return self._run(request, root, jobs, worker=WORKER, should_cancel=should_cancel,
            progress=progress, timeout_seconds=timeout_seconds)

    def _run(self, request, root, jobs, *, worker, extra_code=(), reply_type=PreflightReply,
             should_cancel=None, progress=None, timeout_seconds=PREFLIGHT_SECONDS):
        """Shared contained transport; callers validate their exact read scope."""
        verification_inputs = ()
        if request.get('selected_dataset') is not None or request.get('operation') == 'dataset_admission':
            from .training_dataset_binding import write_verification_bundle
            bundle = Path(root) / ('verification-code-' + request['nonce'] + '.zip')
            digest = write_verification_bundle(bundle)
            request = {**request, 'verification_bundle': {'path': str(bundle), 'sha256': digest}}
            verification_inputs = (bundle, runtime.ROOT / 'version.py')
        reply = reply_type(request, progress)
        try:
            # Bootstrap trust is the local project runtime/code, not the selected
            # trainer interpreter. No selected-source read occurs in this plan.
            code = tuple(Path(__file__).with_name(name) for name in (
                'training_preflight.py', 'training_runtime_qualification.py',
                'training_runtime_contract.py', 'training_tokenizer_contract.py', 'training_file_inventory.py',
                'training_selected_tokenizers.py', 'training_tokenizer_files.py',
                'training_model_contract.py', 'training_tensor_contract.py',
                'training_image_effects_contract.py', 'training_dataset_package_contract.py', 'training_package_effects.py', 'training_dataset_preparation.py', 'training_configuration.py', 'training_model_pairing_contract.py',
                'training_conditioning_contract.py', 'training_conditioning_reference.py', 'training_unet_step_contract.py', 'training_loop_contract.py',
                'training_model_loading_contract.py', 'training_restricted_loading_contract.py', 'training_sdxl_loading.py', 'model_resource_registry.py',
                'training_process.py', 'training_process_windows.py',
                'training_source_io.py', 'training_native_observation.py', 'training_native_protocol.py',
                'training_verification.py', 'background_jobs.py'))
            if request.get('selected_dataset') is not None:
                from .training_dataset_binding import PACKAGE_CODE
                extra_code = (*extra_code, *PACKAGE_CODE)
            code_inputs = tuple(dict.fromkeys((*code, *extra_code)))
            helper = self.process.prepare(root, 'preflight-' + request['nonce'],
                (sys.executable, '-I', '-S', '-B', str(worker), reply.name, str(len(reply.raw)), _sha(reply.raw)),
                inputs=(worker, runtime.CATALOG, runtime.PROBE, runtime.CONTRACT,
                    runtime.ROOT / 'tools/training_verification_worker.py', runtime.DATASET_PACKAGE_CATALOG, *code_inputs, *verification_inputs),
                preview_fingerprint=_sha(reply.raw), timeout_seconds=timeout_seconds, should_cancel=should_cancel)
            result = self.process.execute(helper, helper.fingerprint, jobs,
                running_observer=reply, should_cancel=should_cancel)
            if result.status != 'exited_unverified' or result.exit_code != 0 or not result.tree_stopped or reply.receipt is None:
                raise TrainingProcessError('Preflight did not complete; retain its owned workspace for recovery.')
            rows, tip = TrainingRunJournal(helper.destination).read()
            raw = _read(helper.destination / 'preflight-result.json')
            if tip != result.journal_tip or rows[-1]['details'].get(reply.receipt_key) != _sha(raw) or raw != _bytes(reply.receipt):
                raise TrainingProcessError('The preflight completion receipt changed.')
            if reply.decoded is None:
                raise TrainingProcessError('Selected software inspection or preparation could not finish.')
            return (*reply.decoded, reply.receipt)
        finally:
            reply.close()

    def qualify(self, source, environment, root, jobs, *, profile='sd-scripts-requirements',
                probe_parser=False, python_base=None, should_cancel=None, progress=None, synthetic_tokenizers=False,
                selected_tokenizers=None, native_selected_tokenizers=False, synthetic_model_layout=False, synthetic_tensor_loading=False, synthetic_model_loading=False, native_model_loading=False, synthetic_image_effects=False, synthetic_dataset_package=False, selected_dataset=None, model_pairing=None, conditioning=False, synthetic_unet_step=False, synthetic_training_loop=False):
        inspection, plan, request, initial = self.preflight(source, environment, root, jobs,
            profile=profile, prepare=probe_parser, python_base=python_base, should_cancel=should_cancel, progress=progress,
            synthetic_tokenizers=synthetic_tokenizers, selected_tokenizers=selected_tokenizers, native_selected_tokenizers=native_selected_tokenizers,
            synthetic_model_layout=synthetic_model_layout, synthetic_tensor_loading=synthetic_tensor_loading,
            synthetic_model_loading=synthetic_model_loading, native_model_loading=native_model_loading, synthetic_image_effects=synthetic_image_effects,
            synthetic_dataset_package=synthetic_dataset_package, selected_dataset=selected_dataset, model_pairing=model_pairing, conditioning=conditioning, synthetic_unet_step=synthetic_unet_step, synthetic_training_loop=synthetic_training_loop)
        report = inspection.summary()
        if native_model_loading or native_selected_tokenizers or synthetic_unet_step or synthetic_training_loop:
            flag = runtime.native_verified_flag(17 if synthetic_training_loop else 16 if synthetic_unet_step else 14 if native_model_loading else 15)
            report[flag] = False
            report.update(environment_profile='local-processing-v1', local_processing_checks=None,
                **({'selected_training_started': False} if synthetic_unet_step or synthetic_training_loop else {'training_started': False}), remaining_gates=['resolve_runtime_prerequisites', *runtime.NATIVE_REMAINING_GATES])
        if plan is None:
            report['preflight_receipt_sha256'] = [_sha(_bytes(initial))]
            return report, None
        # Only owned, hash-bound artifacts are read here. load() does not stat
        # the selected root; the actual bytes are checked by execution helpers.
        software = tuple((role, SoftwareInventoryService.load(item.root, item.manifest_path))
            for role, item in zip(('environment', 'python_base'), plan.inventories))
        if any(inventory.fingerprint != item.manifest_sha256 for (_, inventory), item in zip(software, plan.inventories)):
            raise TrainingProcessError('Prepared software inventory evidence changed.')
        receipts = [_sha(_bytes(initial))]
        def reinspect():
            current, _, _, receipt = self.preflight(source, environment, root, jobs,
                profile=profile, should_cancel=should_cancel)
            receipts.append(_sha(_bytes(receipt)))
            return current
        report, destination = runtime.TrainingRuntimeQualificationService().execute_prepared(
            inspection, plan, request, jobs, software=software, should_cancel=should_cancel,
            reinspect=reinspect, preflight_receipts=receipts, process_service=self.process)
        return report, destination
