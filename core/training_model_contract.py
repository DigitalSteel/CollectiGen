"""Generated SDXL meta-tensor layout experiment; never reads model weights.

Standard library at import time. Only the explicit contained runtime calls the
public model constructors, conversion functions and injected meta-only loader.
"""
from __future__ import annotations

import hashlib
import json
import re

PROFILE = 'sdxl-base-1.0-ldm-layout-v1'
SOURCE_REVISION = '6721028c79ee85a78b3a06dfd8954dae310a1cce'
CATALOG_FILE = 'generated-model-layout.json'
MAX_CATALOG = 1024 * 1024
PREFIXES = ('model.diffusion_model.', 'conditioner.embedders.0.transformer.',
            'conditioner.embedders.1.model.', 'first_stage_model.')
FALSE_FLAGS = ('model_weights_loaded', 'model_compatibility_verified',
               'offline_execution_verified', 'execution_ready')


def canonical(value):
    return json.dumps(value, sort_keys=True, ensure_ascii=False, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def digest(value):
    return hashlib.sha256(canonical(value)).hexdigest()


def validate_catalog(value):
    if type(value) is not dict or set(value) != {'schema_version', 'profile', 'source_revision', 'tensors'} or type(value['schema_version']) is not int or value['schema_version'] != 1 or value['profile'] != PROFILE or value['source_revision'] != SOURCE_REVISION:
        raise ValueError('invalid_model_layout_catalog')
    rows = value['tensors']
    if type(rows) is not dict or not 1000 <= len(rows) <= 5000 or len(canonical(value)) > MAX_CATALOG:
        raise ValueError('invalid_model_layout_size')
    counts = [0] * 4
    for name, shape in rows.items():
        if type(name) is not str or len(name) > 240 or not re.fullmatch(r'[A-Za-z0-9_.]+', name) or type(shape) is not list or not 1 <= len(shape) <= 4 or any(type(n) is not int or not 1 <= n <= 100000 for n in shape):
            raise ValueError('invalid_model_layout_tensor')
        roles = [index for index, prefix in enumerate(PREFIXES) if name.startswith(prefix)]
        if len(roles) != 1:
            raise ValueError('invalid_model_layout_group')
        counts[roles[0]] += 1
    if min(counts) < 100 or rows.get(PREFIXES[0] + 'input_blocks.0.0.weight') != [320, 4, 3, 3] or rows.get(PREFIXES[1] + 'text_model.embeddings.token_embedding.weight') != [49408, 768] or rows.get(PREFIXES[2] + 'token_embedding.weight') != [49408, 1280]:
        raise ValueError('invalid_model_layout_architecture')
    return value


def layout_report(catalog):
    validate_catalog(catalog)
    return {'schema_version': 1, 'kind': 'sdxl_generated_model_layout', 'profile': PROFILE,
        'catalog_sha256': digest(catalog), 'tensor_count': len(catalog['tensors']),
        'group_counts': [sum(name.startswith(prefix) for name in catalog['tensors']) for prefix in PREFIXES],
        'meta_roundtrip_verified': True, 'negative_cases': ['missing', 'unexpected', 'shape'],
        **{key: False for key in FALSE_FLAGS}}


def validate_report(value, catalog):
    if canonical(value) != canonical(layout_report(catalog)):
        raise ValueError('invalid_model_layout_report')
    return value


def _shapes(state):
    result = {}
    for name, tensor in state.items():
        if tensor.device.type != 'meta' or tensor.numel() < 1:
            raise ValueError('model_layout_nonmeta_tensor')
        result[name] = list(tensor.shape)
    return result


def probe_generated_layout(module, torch, workspace):
    """Exercise public constructors/conversions/load routing on storage-free tensors.

    The safetensors reader is replaced by a generated dictionary supplier;
    successful loading here proves only complete names/shapes and meta routing.
    """
    def text_config(hidden, layers, heads, activation):
        return module.CLIPTextConfig(vocab_size=49408, hidden_size=hidden,
            intermediate_size=hidden * 4, num_hidden_layers=layers,
            num_attention_heads=heads, max_position_embeddings=77,
            hidden_act=activation, layer_norm_eps=1e-5, dropout=0.0,
            attention_dropout=0.0, initializer_range=0.02, initializer_factor=1.0,
            pad_token_id=1, bos_token_id=0, eos_token_id=2,
            model_type='clip_text_model', projection_dim=hidden)

    with torch.device('meta'), module.init_empty_weights():
        unet = module.sdxl_original_unet.SdxlUNet2DConditionModel(in_channels=4)
        te1 = module.CLIPTextModel._from_config(text_config(768, 12, 12, 'quick_gelu'))
        te2 = module.CLIPTextModelWithProjection(text_config(1280, 32, 20, 'gelu'))
        vae = module.AutoencoderKL(**module.model_util.create_vae_diffusers_config())
        original = [unet.state_dict(), te1.state_dict(), te2.state_dict(), vae.state_dict()]
        expected = [_shapes(state) for state in original]
        converted = [original[0], original[1],
            module.convert_text_encoder_2_state_dict_to_sdxl(original[2], None),
            module.model_util.convert_vae_state_dict(original[3])]
        checkpoint = {prefix + name: tensor for prefix, state in zip(PREFIXES, converted) for name, tensor in state.items()}
        catalog = validate_catalog({'schema_version': 1, 'profile': PROFILE,
            'source_revision': SOURCE_REVISION, 'tensors': _shapes(checkpoint)})
        saved_reader = module.load_file
        reader_calls = []
        fixture_name = 'collectigen-generated-meta-layout.safetensors'
        current = checkpoint
        def generated_reader(path, device='cpu'):
            if path != fixture_name or device != 'meta':
                raise ValueError('model_layout_reader_scope')
            reader_calls.append(1)
            return dict(current)
        module.load_file = generated_reader
        try:
            result = module.load_models_from_sdxl_checkpoint(module.MODEL_VERSION_SDXL_BASE_V1_0,
                fixture_name, 'meta', dtype=torch.float32, disable_mmap=False)
            actual = [result[3], result[0], result[1], result[2]]
            if [_shapes(model.state_dict()) for model in actual] != expected or result[4:] != (None, None) or len(reader_calls) != 1:
                raise ValueError('model_layout_roundtrip_changed')
            key = PREFIXES[0] + 'out.2.bias'
            for fault in ('missing', 'unexpected', 'shape'):
                current = dict(checkpoint)
                if fault == 'missing':
                    del current[key]
                elif fault == 'unexpected':
                    current[PREFIXES[0] + 'generated_unknown.weight'] = torch.empty((1,), device='meta')
                else:
                    current[key] = torch.empty((5,), device='meta')
                try:
                    module.load_models_from_sdxl_checkpoint(module.MODEL_VERSION_SDXL_BASE_V1_0,
                        fixture_name, 'meta', dtype=torch.float32, disable_mmap=False)
                except (RuntimeError, ValueError):
                    pass
                else:
                    raise ValueError('model_layout_invalid_input_accepted')
            if len(reader_calls) != 4:
                raise ValueError('model_layout_reader_calls_changed')
        finally:
            module.load_file = saved_reader
    raw = canonical(catalog)
    with (workspace / CATALOG_FILE).open('xb') as handle:
        handle.write(raw)
    return layout_report(catalog)
