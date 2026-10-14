"""Explicit CPU SDXL assembly from bounded, verified local safetensor reads.

No application launch uses this internal adapter yet. Caller-provided identities
must be bound to reviewed resources before any future selected-model execution.
"""
from __future__ import annotations

from contextlib import contextmanager, ExitStack
import hashlib
import os
from pathlib import Path
import stat
import struct

from . import model_resource_registry as resource
from .training_source_io import _assert_unredirected, _identity
from .training_model_contract import PREFIXES, canonical

MAX_FILE = 8 * 1024**3
MAX_TENSOR = 128 * 1024**2
CHUNK = 1024**2


def _stable(path, handle, before):
    _assert_unredirected(path)
    if _identity(os.fstat(handle.fileno())) != before or _identity(path.stat(follow_symlinks=False)) != before:
        raise ValueError('model_loading_source_changed')


def _hash(handle):
    handle.seek(0)
    digest = hashlib.sha256()
    total = 0
    while block := handle.read(CHUNK):
        total += len(block)
        if total > MAX_FILE:
            raise ValueError('model_loading_file_grew')
        digest.update(block)
    return digest.hexdigest()


@contextmanager
def verified_file(path, expected_sha256, shapes):
    """Retain one stable handle through inspection, decoding and final recheck."""
    path = Path(path).absolute()
    if path.suffix.casefold() != '.safetensors' or not resource._ID.fullmatch('sha256:' + expected_sha256):
        raise ValueError('model_loading_identity_required')
    _assert_unredirected(path)
    fd = os.open(path, os.O_RDONLY | getattr(os, 'O_BINARY', 0) | getattr(os, 'O_NOFOLLOW', 0))
    with os.fdopen(fd, 'rb') as handle:
        state = os.fstat(handle.fileno())
        before = _identity(state)
        if not stat.S_ISREG(state.st_mode) or not 8 < state.st_size <= MAX_FILE:
            raise ValueError('model_loading_file_unsupported')
        _stable(path, handle, before)
        prefix = handle.read(8)
        size = int.from_bytes(prefix, 'little')
        if not 2 <= size <= resource.MAX_HEADER_BYTES:
            raise ValueError('model_loading_header_bound')
        raw = handle.read(size)
        resource._header(raw, state.st_size - size - 8)
        rows = resource._json(raw)
        rows.pop('__metadata__', None)
        if set(rows) != set(shapes) or any(row['shape'] != shapes[name] or row['dtype'] != 'F16' or not 0 < row['data_offsets'][1] - row['data_offsets'][0] <= MAX_TENSOR for name, row in rows.items()):
            raise ValueError('model_loading_layout_unsupported')
        if _hash(handle) != expected_sha256:
            raise ValueError('model_loading_bytes_changed')
        _stable(path, handle, before)
        yield handle, size + 8, rows, prefix + raw, expected_sha256
        if _hash(handle) != expected_sha256:
            raise ValueError('model_loading_bytes_changed')
        _stable(path, handle, before)


def decode_state(verified, decoder):
    handle, offset, rows, original_header, expected_sha256 = verified
    decoded_bytes = hashlib.sha256(original_header)
    result = {}
    for name, row in sorted(rows.items(), key=lambda item: item[1]['data_offsets'][0]):
        start, end = row['data_offsets']
        handle.seek(offset + start)
        payload = handle.read(end - start)
        if len(payload) != end - start:
            raise ValueError('model_loading_truncated_tensor')
        decoded_bytes.update(payload)
        header = canonical({'tensor': {'dtype': row['dtype'], 'shape': row['shape'], 'data_offsets': [0, len(payload)]}})
        header += b' ' * (-len(header) % 8)
        decoded = decoder(struct.pack('<Q', len(header)) + header + payload)
        if type(decoded) is not dict or set(decoded) != {'tensor'}:
            raise ValueError('model_loading_decoder_keys')
        tensor = decoded['tensor']
        if tensor.device.type != 'cpu' or str(tensor.dtype) != 'torch.float16' or list(tensor.shape) != row['shape']:
            raise ValueError('model_loading_decoder_tensor')
        result[name] = tensor
    if decoded_bytes.hexdigest() != expected_sha256:
        raise ValueError('model_loading_decoded_bytes_changed')
    return result


def text_config(module, hidden, layers, heads, activation):
    return module.CLIPTextConfig(vocab_size=49408, hidden_size=hidden,
        intermediate_size=hidden * 4, num_hidden_layers=layers, num_attention_heads=heads,
        max_position_embeddings=77, hidden_act=activation, layer_norm_eps=1e-5,
        dropout=0.0, attention_dropout=0.0, initializer_range=0.02,
        initializer_factor=1.0, pad_token_id=1, bos_token_id=0, eos_token_id=2,
        model_type='clip_text_model', projection_dim=hidden)


def assemble(module, torch, state):
    """Use only pinned constructors/conversions; no identifier/path dispatch."""
    with module.init_empty_weights():
        models = {'unet': module.sdxl_original_unet.SdxlUNet2DConditionModel(in_channels=4),
            'encoder_1': module.CLIPTextModel._from_config(text_config(module, 768, 12, 12, 'quick_gelu')),
            'encoder_2': module.CLIPTextModelWithProjection(text_config(module, 1280, 32, 20, 'gelu')),
            'vae': module.AutoencoderKL(**module.model_util.create_vae_diffusers_config())}
    def take(prefix, keep=False):
        return {name if keep else name[len(prefix):]: state.pop(name) for name in list(state) if name.startswith(prefix)}
    module._load_state_dict_on_device(models['unet'], take(PREFIXES[0]), 'cpu', dtype=torch.float16)
    module._load_state_dict_on_device(models['encoder_1'], take(PREFIXES[1]), 'cpu')
    second, logit_scale = module.convert_sdxl_text_encoder_2_checkpoint(take(PREFIXES[2], True), max_length=77)
    if logit_scale is not None:
        raise ValueError('model_loading_logit_scale_unsupported')
    module._load_state_dict_on_device(models['encoder_2'], second, 'cpu')
    vae = module.model_util.convert_ldm_vae_checkpoint(take(PREFIXES[3], True), module.model_util.create_vae_diffusers_config())
    module._load_state_dict_on_device(models['vae'], vae, 'cpu', dtype=torch.float16)
    if state:
        raise ValueError('model_loading_unconsumed_state')
    return models


def load_models(module, torch, decoder, base, base_sha256, catalog, *, vae=None, vae_sha256=None, observe=None):
    """Internal blocking primitive. Contained process owns cancellation/recovery."""
    if (vae is None) != (vae_sha256 is None):
        raise ValueError('model_loading_vae_selection_incomplete')
    shapes = catalog['tensors']
    vae_shapes = {name[len(PREFIXES[3]):]: shape for name, shape in shapes.items() if name.startswith(PREFIXES[3])}
    with ExitStack() as stack:
        # Validate every requested component before allocating model tensors.
        base_file = stack.enter_context(verified_file(base, base_sha256, shapes))
        vae_file = stack.enter_context(verified_file(vae, vae_sha256, vae_shapes)) if vae is not None else None
        models = assemble(module, torch, decode_state(base_file, decoder))
        if observe: observe(models, False)
        if vae_file is not None:
            state = {PREFIXES[3] + name: tensor for name, tensor in decode_state(vae_file, decoder).items()}
            converted = module.model_util.convert_ldm_vae_checkpoint(state, module.model_util.create_vae_diffusers_config())
            module._load_state_dict_on_device(models['vae'], converted, 'cpu', dtype=torch.float16)
            if observe: observe(models, True)
    return models
