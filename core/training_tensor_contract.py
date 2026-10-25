"""Generated local safetensors decoding experiment; no real model inputs.

Standard-library fixtures and an independent value oracle exercise the selected
native decoder from bounded immutable bytes. This is not a model loader.
"""
from __future__ import annotations

import hashlib
import json
import os
import stat
import struct

PROFILE = 'generated-safe-tensor-cpu-v1'
MAX_FILE = 4096
DTYPES = ('F16', 'BF16', 'F32')
CORRUPT = ('truncated', 'invalid_json', 'invalid_offsets', 'invalid_dtype')
VALUES = (-0.0, 1.0, -2.0, 0.5, 4.0, -8.0)
FALSE_FLAGS = ('model_weights_loaded', 'model_compatibility_verified',
               'offline_execution_verified', 'execution_ready')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def digest(raw):
    return hashlib.sha256(raw).hexdigest()


def fixture(dtype):
    if dtype not in DTYPES:
        raise ValueError('unsupported_generated_dtype')
    def encode(values):
        if dtype == 'BF16':
            return b''.join(struct.pack('<f', value)[2:] for value in values)
        return struct.pack('<' + ('e' if dtype == 'F16' else 'f') * len(values), *values)
    weight, bias = encode(VALUES), encode((1.0, -2.0))
    header = {'weight': {'dtype': dtype, 'shape': [2, 3], 'data_offsets': [0, len(weight)]},
              'bias': {'dtype': dtype, 'shape': [2], 'data_offsets': [len(weight), len(weight) + len(bias)]}}
    return container(header, weight + bias)


def container(header, payload):
    raw = canonical(header)
    raw += b' ' * (-len(raw) % 8)
    return struct.pack('<Q', len(raw)) + raw + payload


def fixtures():
    valid = fixture('F32')
    size = int.from_bytes(valid[:8], 'little')
    header = json.loads(valid[8:8 + size])
    payload = valid[8 + size:]
    offsets = json.loads(canonical(header))
    offsets['weight']['data_offsets'][1] -= 4
    dtype = json.loads(canonical(header))
    dtype['weight']['dtype'] = 'INVALID'
    return {**{name.lower(): fixture(name) for name in DTYPES},
        'truncated': valid[:-1], 'invalid_json': struct.pack('<Q', 8) + b'not json',
        'invalid_offsets': container(offsets, payload), 'invalid_dtype': container(dtype, payload)}


def filenames():
    return tuple('generated-' + name + '.safetensors' for name in (*[d.lower() for d in DTYPES], *CORRUPT, 'missing'))


def expected_report():
    return {'schema_version': 1, 'kind': 'generated_local_tensor_loading', 'profile': PROFILE,
        'reader': 'safetensors.torch.load_bytes_cpu',
        'fixtures_sha256': {name: digest(raw) for name, raw in fixtures().items()},
        'precisions': list(DTYPES), 'tensor_count': 6, 'exact_values_verified': True,
        'negative_cases': [*CORRUPT, 'missing', 'identity_mismatch'],
        'snapshot_byte_limit': MAX_FILE, **{key: False for key in FALSE_FLAGS}}


def validate_report(value):
    if canonical(value) != canonical(expected_report()):
        raise ValueError('invalid_tensor_loading_report')
    return value


def _identity(value):
    # Follow the source reader's portable path/handle identity convention.
    # Windows ctime differs between stat APIs on newer Python releases.
    return (value.st_dev, value.st_ino, value.st_size, value.st_mtime_ns,
            getattr(value, 'st_file_attributes', 0))


def snapshot(path, expected_sha256):
    """Read one bounded generated file; never discover or resolve alternatives."""
    before = path.lstat()
    if not stat.S_ISREG(before.st_mode) or getattr(before, 'st_file_attributes', 0) & 0x400 or not 0 < before.st_size <= MAX_FILE:
        raise ValueError('generated_tensor_file_unsupported')
    with path.open('rb') as handle:
        if _identity(os.fstat(handle.fileno())) != _identity(before):
            raise ValueError('generated_tensor_file_changed')
        raw = handle.read(MAX_FILE + 1)
        if _identity(os.fstat(handle.fileno())) != _identity(before):
            raise ValueError('generated_tensor_file_changed')
    if _identity(path.lstat()) != _identity(before) or len(raw) != before.st_size or digest(raw) != expected_sha256:
        raise ValueError('generated_tensor_identity_mismatch')
    return raw


def load_snapshot(path, expected_sha256, decoder):
    raw = snapshot(path, expected_sha256)
    result = decoder(raw)
    if snapshot(path, expected_sha256) != raw:
        raise ValueError('generated_tensor_file_changed')
    return result


def probe_generated_tensors(safetensors, torch, workspace, error_type):
    """Decode exact tiny generated files on CPU, including malformed inputs."""
    generated = fixtures()
    for name, raw in generated.items():
        with (workspace / ('generated-' + name + '.safetensors')).open('xb') as handle:
            handle.write(raw)
    calls = []
    def decode(raw):
        calls.append(digest(raw))
        return safetensors.load(raw)
    for name, dtype in zip(DTYPES, (torch.float16, torch.bfloat16, torch.float32)):
        raw = generated[name.lower()]
        loaded = load_snapshot(workspace / ('generated-' + name.lower() + '.safetensors'), digest(raw), decode)
        expected = {'weight': torch.tensor(VALUES, dtype=dtype, device='cpu').reshape(2, 3),
                    'bias': torch.tensor((1.0, -2.0), dtype=dtype, device='cpu')}
        if type(loaded) is not dict or set(loaded) != set(expected):
            raise ValueError('generated_tensor_keys_changed')
        for key, tensor in loaded.items():
            if tensor.device.type != 'cpu' or tensor.dtype != dtype or tensor.shape != expected[key].shape or not torch.equal(tensor, expected[key]):
                raise ValueError('generated_tensor_values_changed')
            size = int.from_bytes(raw[:8], 'little')
            start, end = json.loads(raw[8:8 + size])[key]['data_offsets']
            # Numeric equality alone does not distinguish the sign of zero.
            if bytes(tensor.contiguous().view(torch.uint8).reshape(-1).tolist()) != raw[8 + size + start:8 + size + end]:
                raise ValueError('generated_tensor_payload_changed')
    for name in CORRUPT:
        try:
            load_snapshot(workspace / ('generated-' + name + '.safetensors'), digest(generated[name]), decode)
        except error_type:
            pass
        else:
            raise ValueError('corrupt_generated_tensor_accepted')
    before = len(calls)
    for name, expected, error in (('missing', '0' * 64, FileNotFoundError), ('f32', '0' * 64, ValueError)):
        try:
            load_snapshot(workspace / ('generated-' + name + '.safetensors'), expected, decode)
        except error:
            pass
        else:
            raise ValueError('unavailable_generated_tensor_accepted')
    if len(calls) != before or before != len(DTYPES) + len(CORRUPT):
        raise ValueError('generated_tensor_decoder_calls_changed')
    return expected_report()
