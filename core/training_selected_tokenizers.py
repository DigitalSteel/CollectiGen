"""Explicit local CLIP profile and memory-only selected-tokenizer experiment.

Standard library only. The runtime stages this module with its exact two shared
contracts; never import model/application state or use a configurable loader.
"""
from __future__ import annotations

import hashlib
import mmap
import os
from pathlib import Path
import re
from uuid import uuid4

from .training_tokenizer_files import (TOKENIZER_FILES, canonical, strict_json,
                                      read_slot, files_identity)
from .training_tokenizer_contract import BOS, EOS, WIDTH, LIMITS, cases, expected_chunks

PROFILE = 'sdxl-local-clip-scalar-v1'
MAX_REQUEST = 256 * 1024
SHA = re.compile(r'[0-9a-f]{64}\Z')
SPECIALS = {'bos_token': '<|startoftext|>', 'eos_token': '<|endoftext|>',
            'unk_token': '<|endoftext|>', 'pad_token': '<|endoftext|>'}


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def validate_selection(value):
    if type(value) is not dict or set(value) != {'binding_sha256', 'slots'} or type(value['binding_sha256']) is not str or not SHA.fullmatch(value['binding_sha256']):
        raise ValueError('invalid_tokenizer_binding')
    if type(value['slots']) is not list or len(value['slots']) != 2:
        raise ValueError('invalid_tokenizer_slots')
    for row, role in zip(value['slots'], (1, 2)):
        if type(row) is not dict or set(row) != {'role', 'path', 'identity'} or type(row['role']) is not int or row['role'] != role or type(row['identity']) is not str or not re.fullmatch(r'clip-files-v1:[0-9a-f]{64}', row['identity']):
            raise ValueError('invalid_tokenizer_slot')
        path = row['path']
        if type(path) is not str or not path or len(path) > 32767 or '\0' in path or path.startswith(('\\\\', '//')) or not Path(path).is_absolute() or str(Path(os.path.abspath(path))) != path:
            raise ValueError('invalid_tokenizer_location')
    return value


def binding(value):
    validate_selection(value)
    return {'component_binding_sha256': value['binding_sha256'],
            'slots': [{'role': row['role'], 'identity': row['identity']} for row in value['slots']]}


def input_paths(value):
    validate_selection(value)
    return tuple(Path(row['path']) / name for row in value['slots'] for name, _ in TOKENIZER_FILES)


def validate_descriptor(value):
    if type(value) is not dict or set(value) != {'mapping', 'length', 'sha256'} or type(value['mapping']) is not str or not re.fullmatch(r'Local\\CollectiGen\.Tokenizers\.[0-9a-f]{32}', value['mapping']) or type(value['length']) is not int or not 0 < value['length'] <= MAX_REQUEST or type(value['sha256']) is not str or not SHA.fullmatch(value['sha256']):
        raise ValueError('invalid_tokenizer_memory_request')
    return value


class SelectionMemory:
    def __init__(self, selection):
        raw = canonical(validate_selection(selection))
        self.descriptor = {'mapping': 'Local\\CollectiGen.Tokenizers.' + uuid4().hex,
                           'length': len(raw), 'sha256': hashlib.sha256(raw).hexdigest()}
        validate_descriptor(self.descriptor)
        self.mapping = mmap.mmap(-1, len(raw), tagname=self.descriptor['mapping'], access=mmap.ACCESS_WRITE)
        self.mapping[:] = raw

    def close(self):
        self.mapping.close()


def read_selection(descriptor):
    validate_descriptor(descriptor)
    with mmap.mmap(-1, descriptor['length'], tagname=descriptor['mapping'], access=mmap.ACCESS_READ) as memory:
        raw = memory[:]
    if hashlib.sha256(raw).hexdigest() != descriptor['sha256']:
        raise ValueError('tokenizer_memory_changed')
    value = validate_selection(strict_json(raw))
    if canonical(value) != raw:
        raise ValueError('noncanonical_tokenizer_selection')
    return value


def configuration(blobs):
    config = strict_json(blobs['tokenizer_config.json'])
    specials = strict_json(blobs['special_tokens_map.json'])
    vocab = strict_json(blobs['vocab.json'])
    allowed = {'tokenizer_class', 'model_max_length', 'errors'} | set(SPECIALS)
    if not {'tokenizer_class', 'model_max_length'} <= config.keys() <= allowed or config['tokenizer_class'] != 'CLIPTokenizer' or type(config['model_max_length']) is not int or config['model_max_length'] != WIDTH or config.get('errors', 'replace') != 'replace' or canonical(specials) != canonical(SPECIALS) or any(config[name] != value for name, value in SPECIALS.items() if name in config):
        raise ValueError('unsupported_tokenizer_configuration')
    if vocab.get(SPECIALS['bos_token']) != BOS or vocab.get(SPECIALS['eos_token']) != EOS or 0 not in vocab.values():
        raise ValueError('unsupported_tokenizer_special_ids')
    return {'model_max_length': WIDTH, 'errors': 'replace', **SPECIALS}


def inspect_selection(value):
    """Blocking worker primitive; exact four-file reads, no directory discovery."""
    validate_selection(value)
    result = []
    for row in value['slots']:
        blobs = read_slot(row['path'])
        if files_identity(blobs) != row['identity']:
            raise ValueError('selected_tokenizer_changed')
        result.append(configuration(blobs))
    return result


def validate_report(value, selected_binding):
    fields = {'schema_version', 'kind', 'profile', 'binding', 'case_set_sha256', 'checks',
              'model_compatibility_verified', 'offline_execution_verified', 'execution_ready'}
    if type(value) is not dict or set(value) != fields or type(value['schema_version']) is not int or value['schema_version'] != 1 or value['kind'] != 'sdxl_selected_tokenizer_check' or value['profile'] != PROFILE or canonical(value['binding']) != canonical(selected_binding) or value['case_set_sha256'] != digest(cases()) or any(value[key] is not False for key in ('model_compatibility_verified', 'offline_execution_verified', 'execution_ready')):
        raise ValueError('selected_tokenizer_report_invalid')
    rows = value['checks']
    if type(rows) is not list or len(rows) != 6:
        raise ValueError('selected_tokenizer_checks_invalid')
    payload_counts = {}
    for row, (role, limit) in zip(rows, ((role, limit) for role in (1, 2) for limit in LIMITS)):
        if type(row) is not dict or set(row) != {'role', 'payload_limit', 'shape', 'cases'} or type(row['role']) is not int or row['role'] != role or type(row['payload_limit']) is not int or row['payload_limit'] != limit or canonical(row['shape']) != canonical([len(cases()), limit // 75, WIDTH]) or type(row['cases']) is not list or len(row['cases']) != len(cases()):
            raise ValueError('selected_tokenizer_shape_invalid')
        for measured, (name, caption, _) in zip(row['cases'], cases()):
            if type(measured) is not dict or set(measured) != {'case', 'payload_tokens', 'truncated_tokens', 'output_sha256'} or measured['case'] != name or type(measured['payload_tokens']) is not int or not 0 <= measured['payload_tokens'] <= len(caption.encode('utf-8')) or type(measured['truncated_tokens']) is not int or measured['truncated_tokens'] != max(0, measured['payload_tokens'] - limit) or type(measured['output_sha256']) is not str or not SHA.fullmatch(measured['output_sha256']):
                raise ValueError('selected_tokenizer_case_invalid')
            key = (role, name)
            if payload_counts.setdefault(key, measured['payload_tokens']) != measured['payload_tokens']:
                raise ValueError('selected_tokenizer_count_changed')
    return value


def probe_selected_tokenizers(tokenizer_class, strategy_class, selection):
    options = inspect_selection(selection)
    tokenizers, payloads = [], []
    for row, config in zip(selection['slots'], options):
        folder = Path(row['path'])
        tokenizer = tokenizer_class(vocab_file=str(folder / 'vocab.json'),
                                    merges_file=str(folder / 'merges.txt'), **config)
        if (tokenizer.bos_token_id, tokenizer.eos_token_id, tokenizer.pad_token_id,
                tokenizer.model_max_length) != (BOS, EOS, EOS, WIDTH):
            raise ValueError('selected_tokenizer_markers_changed')
        if row['role'] == 2:
            tokenizer.pad_token_id = 0
        tokenizers.append(tokenizer)
        encoded = []
        for _, caption, _ in cases():
            ids = tokenizer.encode(caption, add_special_tokens=False, truncation=False)
            if type(ids) is not list or len(ids) > len(caption.encode('utf-8')) or any(type(n) is not int or not 0 <= n < 2**31 for n in ids):
                raise ValueError('selected_tokenizer_payload_invalid')
            encoded.append(ids)
        payloads.append(encoded)
    checks = []
    for limit in LIMITS:
        strategy = strategy_class.__new__(strategy_class)
        strategy.tokenizer1, strategy.tokenizer2 = tokenizers
        strategy.max_length = limit + 2
        actual = strategy.tokenize([text for _, text, _ in cases()])
        if type(actual) is not tuple or len(actual) != 2:
            raise ValueError('selected_tokenizer_roles_changed')
        for role, tensor in enumerate(actual, 1):
            shape = [len(cases()), limit // 75, WIDTH]
            if list(tensor.shape) != shape:
                raise ValueError('selected_tokenizer_shape_changed')
            rows = []
            for measured, ids, (name, _, _) in zip(tensor.tolist(), payloads[role - 1], cases()):
                if canonical(measured) != canonical(expected_chunks(role, ids, limit)):
                    raise ValueError('selected_tokenizer_chunking_changed')
                rows.append({'case': name, 'payload_tokens': len(ids),
                             'truncated_tokens': max(0, len(ids) - limit), 'output_sha256': digest(measured)})
            checks.append({'role': role, 'payload_limit': limit, 'shape': shape, 'cases': rows})
    inspect_selection(selection)  # Full bytes/config read-back after every use.
    report = {'schema_version': 1, 'kind': 'sdxl_selected_tokenizer_check', 'profile': PROFILE,
        'binding': binding(selection), 'case_set_sha256': digest(cases()),
        'checks': sorted(checks, key=lambda row: (row['role'], row['payload_limit'])),
        'model_compatibility_verified': False, 'offline_execution_verified': False, 'execution_ready': False}
    return validate_report(report, binding(selection))
