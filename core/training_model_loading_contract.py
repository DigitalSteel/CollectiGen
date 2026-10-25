"""Deterministic full SDXL CPU weight-loading qualification, generated data only."""
from __future__ import annotations

import ctypes
import gc
import hashlib
import math
import shutil
import struct

from . import training_sdxl_loading as loader
from .training_model_contract import PREFIXES, canonical, validate_catalog

PROFILE = 'generated-full-sdxl-cpu-v1'
CATALOG_SHA256 = '3d8df5797c4fe2cde8efcd18fe4402ff2375b22a459733c5d631b0271d390c59'
FIXTURE_SHA256 = {'base': '81500c85810888999a90e77c4837d65c5c711f9521232b66d740aae289a3be68', 'vae': '25c533ede667887d42d5ded69e2786484964ad3cfe8c70470e0c14eaf973446d'}
FIXTURE_BYTES = {'base': 6938040286, 'vae': 167333134}
NAMES = ('generated-full-base.safetensors', 'generated-full-vae.safetensors',
         'generated-full-missing.safetensors', 'generated-full-invalid.safetensors')
NEGATIVES = ['missing_base', 'missing_vae', 'layout_mismatch', 'identity_mismatch', 'decoder_failure']


def filenames(): return NAMES


def checked_catalog(value):
    validate_catalog(value)
    if hashlib.sha256(canonical(value)).hexdigest() != CATALOG_SHA256:
        raise ValueError('full_model_catalog_changed')
    return value


def fixture_rows(catalog, role):
    if role not in ('base', 'vae'):
        raise ValueError('full_model_fixture_role')
    result, offset = [], 0
    for index, (name, shape) in enumerate(sorted(catalog['tensors'].items())):
        if role == 'vae' and not name.startswith(PREFIXES[3]): continue
        key = name[len(PREFIXES[3]):] if role == 'vae' else name
        size = math.prod(shape) * 2
        value = (index % 97 + 1) / 128 * (-1 if role == 'vae' else 1)
        result.append((key, shape, offset, offset + size, value))
        offset += size
    return result


def fixture_blocks(catalog, role):
    rows = fixture_rows(catalog, role)
    header = canonical({name: {'dtype': 'F16', 'shape': shape, 'data_offsets': [start, end]}
                        for name, shape, start, end, _ in rows})
    header += b' ' * (-len(header) % 8)
    yield struct.pack('<Q', len(header)) + header
    for _, _, start, end, value in rows:
        block = struct.pack('<e', value) * (loader.CHUNK // 2)
        remaining = end - start
        while remaining:
            size = min(remaining, len(block))
            yield block[:size]
            remaining -= size


def expected_report():
    return {'schema_version': 1, 'kind': 'generated_full_sdxl_loading', 'profile': PROFILE,
        'catalog_sha256': CATALOG_SHA256, 'fixture_sha256': dict(FIXTURE_SHA256),
        'fixture_bytes': dict(FIXTURE_BYTES), 'base_tensors': 2513, 'override_tensors': 248,
        'tensor_value_checks': 5026, 'reader': 'bounded_single_tensor_safetensors_bytes',
        'component_dtypes': {'unet': 'F16', 'encoder_1': 'F32', 'encoder_2': 'F32', 'vae': 'F16'},
        'negative_cases': list(NEGATIVES), 'generated_model_weights_loaded': True,
        'selected_model_weights_loaded': False, 'model_compatibility_verified': False,
        'forward_pass_verified': False, 'offline_execution_verified': False, 'execution_ready': False}


def validate_report(value):
    if canonical(value) != canonical(expected_report()):
        raise ValueError('full_model_loading_report_changed')
    return value


def resource_gate(work):
    if shutil.disk_usage(work).free < 10 * 1024**3:
        raise ValueError('full_model_disk_capacity_held')
    class Memory(ctypes.Structure):
        _fields_ = [('length', ctypes.c_ulong), ('load', ctypes.c_ulong)] + [(name, ctypes.c_ulonglong) for name in
            ('total', 'available', 'page_total', 'page_available', 'virtual_total', 'virtual_available', 'extended')]
    state = Memory()
    state.length = ctypes.sizeof(state)
    if not ctypes.windll.kernel32.GlobalMemoryStatusEx(ctypes.byref(state)) or state.available < 20 * 1024**3:
        raise ValueError('full_model_memory_capacity_held')


def check_models(module, torch, models, catalog, override):
    for name, model in models.items():
        dtype = torch.float32 if name.startswith('encoder') else torch.float16
        if any(t.device.type != 'cpu' or t.dtype != dtype for t in model.state_dict().values()):
            raise ValueError('full_model_component_dtype_changed')
    converted = (models['unet'].state_dict(), models['encoder_1'].state_dict(),
        module.convert_text_encoder_2_state_dict_to_sdxl(models['encoder_2'].state_dict(), None),
        module.model_util.convert_vae_state_dict(models['vae'].state_dict()))
    state = {prefix + name: tensor for prefix, rows in zip(PREFIXES, converted) for name, tensor in rows.items()}
    if set(state) != set(catalog['tensors']):
        raise ValueError('full_model_inverse_keys_changed')
    for index, (name, shape) in enumerate(sorted(catalog['tensors'].items())):
        tensor = state[name]
        if list(tensor.shape) != shape:
            raise ValueError('full_model_inverse_shape_changed')
        expected = (index % 97 + 1) / 128 * (-1 if override and name.startswith(PREFIXES[3]) else 1)
        for chunk in tensor.detach().reshape(-1).split(512 * 1024):
            if not torch.all(chunk == expected).item():
                raise ValueError('full_model_tensor_value_changed')


def probe_generated_models(module, torch, safetensors, catalog, work, *, inspect_models=None):
    checked_catalog(catalog)
    resource_gate(work)
    torch.set_num_threads(2)
    for role, name in zip(('base', 'vae'), NAMES):
        digest, size = hashlib.sha256(), 0
        with (work / name).open('xb') as handle:
            for block in fixture_blocks(catalog, role):
                handle.write(block)
                digest.update(block)
                size += len(block)
        if digest.hexdigest() != FIXTURE_SHA256[role] or size != FIXTURE_BYTES[role]:
            raise ValueError('full_model_generated_fixture_changed')
    (work / NAMES[3]).write_bytes(b'\x02\0\0\0\0\0\0\0{}')
    return probe_staged_models(module, torch, safetensors, catalog, work, inspect_models=inspect_models)


def probe_staged_models(module, torch, safetensors, catalog, inputs, *, inspect_models=None):
    """Same finite generated checks against pre-generated read-only inputs."""
    checked_catalog(catalog)
    torch.set_num_threads(2)
    base, vae, missing, invalid = (inputs / name for name in NAMES)
    # Missing/incompatible selections must hold before any tensor decode.
    def forbidden(_): raise AssertionError('full_model_unavailable_decoded')
    for source, source_hash, override, override_hash, error in (
        (missing, FIXTURE_SHA256['base'], None, None, FileNotFoundError),
        (base, FIXTURE_SHA256['base'], missing, FIXTURE_SHA256['vae'], FileNotFoundError),
        (invalid, hashlib.sha256(invalid.read_bytes()).hexdigest(), None, None, loader.resource.ModelResourceError)):
        try:
            loader.load_models(module, torch, forbidden, source, source_hash, catalog, vae=override, vae_sha256=override_hash)
        except error: pass
        else: raise ValueError('full_model_unavailable_accepted')
    vae_shapes = {key[len(PREFIXES[3]):]: shape for key, shape in catalog['tensors'].items() if key.startswith(PREFIXES[3])}
    try:
        with loader.verified_file(vae, '0' * 64, vae_shapes):
            raise AssertionError('full_model_identity_mismatch_accepted')
    except ValueError: pass
    calls = []
    def failing(_):
        calls.append(1)
        raise MemoryError('generated decoder failure')
    try:
        with loader.verified_file(vae, FIXTURE_SHA256['vae'], vae_shapes) as verified:
            loader.decode_state(verified, failing)
    except MemoryError: pass
    if calls != [1]: raise ValueError('full_model_decoder_retried')
    observed = []
    def observe(models, override):
        check_models(module, torch, models, catalog, override)
        observed.append(override)
    with torch.no_grad():
        models = loader.load_models(module, torch, safetensors.load, base, FIXTURE_SHA256['base'], catalog,
            vae=vae, vae_sha256=FIXTURE_SHA256['vae'], observe=observe)
    if observed != [False, True]:
        raise ValueError('full_model_override_observation_changed')
    if inspect_models is not None:
        inspect_models(models)
    del models
    gc.collect()
    return expected_report()
