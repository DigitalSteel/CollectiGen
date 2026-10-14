"""Independent generated SDXL text-conditioning numerical reference.

The contained qualification injects this module's generated F32 parameters into
the real pinned CLIP implementations, then compares their CPU results with the
F64 calculations here.  This module never imports a native model, torch, or a
tokenizer.  It deliberately models only finite generated calibration inputs;
it grants no selected-model, semantic, training, or execution authority.
"""
from __future__ import annotations

import hashlib
import json
import math


PROFILE = 'generated-sdxl-conditioning-reference-v1'
CONTEXT = 77
VOCAB = 49408
EPSILON = 1e-5
CONTROL_CASES = ('parameter_orientation', 'embedding_rows', 'causal_prefix',
                 'pool_projection', 'size_layout', 'invalid_inputs')


def stitch_positions(chunks):
    """Declared content-window layout, including exactly one BOS and tail."""
    if type(chunks) is not int or chunks not in (1, 2, 3):
        raise ValueError('conditioning_reference_chunks_invalid')
    return ((0, 0), *( (window, position) for window in range(chunks)
                      for position in range(1, 76)), (chunks - 1, 76))


def _canonical(value):
    return json.dumps(value, ensure_ascii=False, sort_keys=True, allow_nan=False,
                      separators=(',', ':')).encode('utf-8')


def _sha(value):
    return hashlib.sha256(_canonical(value)).hexdigest()


_CONFIGS = (
    {'role': 1, 'hidden_size': 768, 'layers': 12, 'heads': 12,
     'hidden_act': 'quick_gelu', 'vocab_size': VOCAB, 'context': CONTEXT,
     'intermediate_size': 3072, 'layer_norm_eps': EPSILON,
     'projection_dim': 768},
    {'role': 2, 'hidden_size': 1280, 'layers': 32, 'heads': 20,
     'hidden_act': 'gelu', 'vocab_size': VOCAB, 'context': CONTEXT,
     'intermediate_size': 5120, 'layer_norm_eps': EPSILON,
     'projection_dim': 1280},
)


def configs():
    """Return independent declarations for the two full generated encoders."""
    return [dict(item) for item in _CONFIGS]


def _config(role):
    if type(role) is not int or role not in (1, 2):
        raise ValueError('conditioning_reference_role_invalid')
    return _CONFIGS[role - 1]


def _layer_prefix(layer):
    return 'text_model.encoder.layers.' + str(layer) + '.'


def parameter_shapes(role):
    """Exact public CLIP state-dict keys and shapes for one assembled role."""
    spec = _config(role)
    hidden, layers = spec['hidden_size'], spec['layers']
    result = {
        'text_model.embeddings.token_embedding.weight': (VOCAB, hidden),
        'text_model.embeddings.position_embedding.weight': (CONTEXT, hidden),
    }
    for layer in range(layers):
        prefix = _layer_prefix(layer)
        result.update({
            prefix + 'self_attn.q_proj.weight': (hidden, hidden),
            prefix + 'self_attn.q_proj.bias': (hidden,),
            prefix + 'self_attn.k_proj.weight': (hidden, hidden),
            prefix + 'self_attn.k_proj.bias': (hidden,),
            prefix + 'self_attn.v_proj.weight': (hidden, hidden),
            prefix + 'self_attn.v_proj.bias': (hidden,),
            prefix + 'self_attn.out_proj.weight': (hidden, hidden),
            prefix + 'self_attn.out_proj.bias': (hidden,),
            prefix + 'layer_norm1.weight': (hidden,),
            prefix + 'layer_norm1.bias': (hidden,),
            prefix + 'mlp.fc1.weight': (hidden * 4, hidden),
            prefix + 'mlp.fc1.bias': (hidden * 4,),
            prefix + 'mlp.fc2.weight': (hidden, hidden * 4),
            prefix + 'mlp.fc2.bias': (hidden,),
            prefix + 'layer_norm2.weight': (hidden,),
            prefix + 'layer_norm2.bias': (hidden,),
        })
    result['text_model.final_layer_norm.weight'] = (hidden,)
    result['text_model.final_layer_norm.bias'] = (hidden,)
    if role == 2:
        result['text_projection.weight'] = (hidden, hidden)
    return result


def _f32(np, value):
    return np.asarray(value, dtype=np.float32)


def _embedding_rows(role, rows, np):
    """Return selected token rows without allocating the complete vocabulary."""
    spec = _config(role)
    indices = np.asarray(rows)
    if indices.ndim != 1 or not np.issubdtype(indices.dtype, np.integer) or (
            indices < 0).any() or (indices >= VOCAB).any():
        raise ValueError('conditioning_reference_token_ids_invalid')
    columns = np.arange(spec['hidden_size'], dtype=np.float32)[None, :]
    row_values = indices.astype(np.float32, copy=False)[:, None]
    return _f32(np, ((row_values % 19) - 9) * ((columns % 7) - 3) / 256
                + ((columns % 11) - 5) / 64 + role / 128)


def _position_rows(role, rows, np):
    spec = _config(role)
    indices = np.asarray(rows)
    if indices.ndim != 1 or not np.issubdtype(indices.dtype, np.integer) or (
            indices < 0).any() or (indices >= CONTEXT).any():
        raise ValueError('conditioning_reference_positions_invalid')
    columns = np.arange(spec['hidden_size'], dtype=np.float32)[None, :]
    row_values = indices.astype(np.float32, copy=False)[:, None]
    return _f32(np, ((row_values % 13) - 6) * ((columns % 5) - 2) / 1024)


def _projection_bias(role, layer, np, *, width=None):
    if width is None:
        width = _config(role)['hidden_size']
    cols = np.arange(width, dtype=np.float32)
    return _f32(np, ((cols % 7) - 3) * (layer + role) / 4096)


def _ln_affine(role, layer, np):
    """Layer-specific nonuniform affine values; layer == layers is final LN."""
    hidden = _config(role)['hidden_size']
    cols = np.arange(hidden, dtype=np.float32)
    weight = 1 + ((cols % 9) - 4) * (layer + role) / 8192
    bias = ((cols % 7) - 3) * (layer + role) / 4096
    return _f32(np, weight), _f32(np, bias)


def _mlp_bias(role, layer, rows, np):
    values = np.asarray(rows, dtype=np.float32)
    return _f32(np, ((values % 11) - 5) / 512 + (layer + role) / 2048)


def _fc2_bias(layer, cols, np):
    values = np.asarray(cols, dtype=np.float32)
    return _f32(np, ((values % 5) - 2) * (layer + 1) / 4096)


def parameter(role, name, np):
    """Generate one exact F32 CLIP parameter from the finite calibration recipe.

    This deliberately materializes only the requested parameter.  ``forward``
    below uses the same sparse formulae directly, so it never allocates dense
    attention or MLP matrices merely to calculate the independent oracle.
    """
    shapes = parameter_shapes(role)
    if name not in shapes:
        raise ValueError('conditioning_reference_parameter_unknown')
    spec, hidden = _config(role), _config(role)['hidden_size']
    shape = shapes[name]
    if name == 'text_model.embeddings.token_embedding.weight':
        result = np.empty(shape, dtype=np.float32)
        # Bound temporary memory while retaining an exact dense state parameter.
        for start in range(0, VOCAB, 256):
            stop = min(start + 256, VOCAB)
            result[start:stop] = _embedding_rows(role, np.arange(start, stop), np)
        return result
    if name == 'text_model.embeddings.position_embedding.weight':
        return _position_rows(role, np.arange(CONTEXT), np)
    if name == 'text_model.final_layer_norm.weight':
        return _ln_affine(role, spec['layers'], np)[0]
    if name == 'text_model.final_layer_norm.bias':
        return _ln_affine(role, spec['layers'], np)[1]
    if name == 'text_projection.weight':
        result = np.zeros(shape, dtype=np.float32)
        rows = np.arange(hidden)
        result[rows, rows] = np.float32(0.25)
        result[rows, (rows + 3) % hidden] = np.float32(0.5)
        return result

    marker = 'text_model.encoder.layers.'
    if not name.startswith(marker):
        raise ValueError('conditioning_reference_parameter_unknown')
    suffix = name[len(marker):]
    layer_text, suffix = suffix.split('.', 1)
    if not layer_text.isdigit() or int(layer_text) >= spec['layers']:
        raise ValueError('conditioning_reference_parameter_unknown')
    layer = int(layer_text)
    rows = np.arange(hidden)
    if suffix in ('layer_norm1.weight', 'layer_norm2.weight'):
        return _ln_affine(role, layer, np)[0]
    if suffix in ('layer_norm1.bias', 'layer_norm2.bias'):
        return _ln_affine(role, layer, np)[1]
    if suffix in ('self_attn.q_proj.bias', 'self_attn.k_proj.bias',
                  'self_attn.v_proj.bias', 'self_attn.out_proj.bias'):
        return _projection_bias(role, layer, np)
    if suffix == 'self_attn.q_proj.weight':
        result = np.zeros(shape, dtype=np.float32)
        result[rows, rows] = np.float32((1 + layer % 3) / 8)
        return result
    if suffix == 'self_attn.k_proj.weight':
        result = np.zeros(shape, dtype=np.float32)
        result[rows, (rows + 1) % hidden] = np.float32(-(1 + layer % 5) / 16)
        return result
    if suffix == 'self_attn.v_proj.weight':
        result = np.zeros(shape, dtype=np.float32)
        result[rows, rows] = np.float32(0.25)
        return result
    if suffix == 'self_attn.out_proj.weight':
        result = np.zeros(shape, dtype=np.float32)
        result[rows, (rows + 2) % hidden] = np.float32(0.125)
        return result
    if suffix == 'mlp.fc1.bias':
        return _mlp_bias(role, layer, np.arange(hidden * 4), np)
    if suffix == 'mlp.fc1.weight':
        result = np.zeros(shape, dtype=np.float32)
        result[rows, rows] = np.float32(0.25)
        result[hidden + rows, (rows + 1) % hidden] = np.float32(-0.125)
        result[2 * hidden + rows, rows] = np.float32(0.0625)
        result[3 * hidden + rows, (rows + 2) % hidden] = np.float32(-0.03125)
        return result
    if suffix == 'mlp.fc2.bias':
        return _fc2_bias(layer, np.arange(hidden), np)
    if suffix == 'mlp.fc2.weight':
        result = np.zeros(shape, dtype=np.float32)
        for block, coefficient in enumerate((0.125, -0.0625, 0.03125, 0.015625)):
            result[rows, block * hidden + rows] = np.float32(coefficient)
        return result
    raise ValueError('conditioning_reference_parameter_unknown')


def _layer_norm(value, weight, bias, np):
    mean = value.mean(axis=-1, keepdims=True)
    variance = ((value - mean) ** 2).mean(axis=-1, keepdims=True)
    return (value - mean) / np.sqrt(variance + EPSILON) * weight + bias


def _softmax(value, np):
    shifted = value - value.max(axis=-1, keepdims=True)
    numerator = np.exp(shifted)
    return numerator / numerator.sum(axis=-1, keepdims=True)


def _causal_attention(value, role, layer, heads, np):
    """Sparse Q/K/V/out implementation shared by the reference forward/tests."""
    hidden = value.shape[-1]
    if hidden % heads or value.ndim != 2:
        raise ValueError('conditioning_reference_attention_shape')
    columns = np.arange(hidden)
    bias = _projection_bias(role, layer, np, width=hidden).astype(np.float64)
    q = value * ((1 + layer % 3) / 8) + bias
    k = value[:, (columns + 1) % hidden] * (-(1 + layer % 5) / 16) + bias
    v = value * 0.25 + bias
    width = hidden // heads
    q = q.reshape(CONTEXT, heads, width).transpose(1, 0, 2)
    k = k.reshape(CONTEXT, heads, width).transpose(1, 0, 2)
    v = v.reshape(CONTEXT, heads, width).transpose(1, 0, 2)
    scores = q @ k.transpose(0, 2, 1) / math.sqrt(width)
    mask = np.triu(np.ones((CONTEXT, CONTEXT), dtype=bool), 1)
    scores = np.where(mask[None, :, :], -np.inf, scores)
    attended = _softmax(scores, np) @ v
    attended = attended.transpose(1, 0, 2).reshape(CONTEXT, hidden)
    return attended[:, (columns + 2) % hidden] * 0.125 + bias


def _activation(value, kind, np):
    if kind == 'quick_gelu':
        return value / (1 + np.exp(-1.702 * value))
    if kind == 'gelu':
        # NumPy has no guaranteed erf ufunc.  This exact stdlib fallback keeps
        # the reference independent of torch/scipy while preserving F64 math.
        erf = getattr(np, 'erf', None)
        if erf is None:
            erf = np.frompyfunc(math.erf, 1, 1)
        return 0.5 * value * (1 + np.asarray(erf(value / math.sqrt(2)), dtype=np.float64))
    raise ValueError('conditioning_reference_activation_invalid')


def _mlp(value, role, layer, kind, np):
    hidden = value.shape[-1]
    columns = np.arange(hidden)
    rows = np.arange(hidden * 4)
    first = np.empty((CONTEXT, hidden * 4), dtype=np.float64)
    first[:, :hidden] = value * 0.25
    first[:, hidden:2 * hidden] = value[:, (columns + 1) % hidden] * -0.125
    first[:, 2 * hidden:3 * hidden] = value * 0.0625
    first[:, 3 * hidden:] = value[:, (columns + 2) % hidden] * -0.03125
    first += _mlp_bias(role, layer, rows, np).astype(np.float64)
    first = _activation(first, kind, np)
    result = (first[:, :hidden] * 0.125 - first[:, hidden:2 * hidden] * 0.0625
              + first[:, 2 * hidden:3 * hidden] * 0.03125
              + first[:, 3 * hidden:] * 0.015625)
    return result + _fc2_bias(layer, columns, np).astype(np.float64)


def forward(role, ids77, np):
    """Calculate selected and final full-encoder states using F64 NumPy math."""
    spec = _config(role)
    ids = np.asarray(ids77)
    if ids.shape != (CONTEXT,) or not np.issubdtype(ids.dtype, np.integer):
        raise ValueError('conditioning_reference_ids_shape')
    hidden = (_embedding_rows(role, ids, np).astype(np.float64)
              + _position_rows(role, np.arange(CONTEXT), np).astype(np.float64))
    states = [hidden.copy()]
    for layer in range(spec['layers']):
        weight, bias = _ln_affine(role, layer, np)
        hidden = hidden + _causal_attention(
            _layer_norm(hidden, weight.astype(np.float64), bias.astype(np.float64), np),
            role, layer, spec['heads'], np)
        hidden = hidden + _mlp(
            _layer_norm(hidden, weight.astype(np.float64), bias.astype(np.float64), np),
            role, layer, spec['hidden_act'], np)
        states.append(hidden.copy())
    selected = states[11] if role == 1 else states[-2]
    weight, bias = _ln_affine(role, spec['layers'], np)
    final = _layer_norm(hidden, weight.astype(np.float64), bias.astype(np.float64), np)
    return selected, final


def projection(value, np):
    """Apply role-2's declared sparse final projection in F64."""
    array = np.asarray(value, dtype=np.float64)
    if array.ndim < 1 or array.shape[-1] != _config(2)['hidden_size']:
        raise ValueError('conditioning_reference_projection_shape')
    columns = np.arange(_config(2)['hidden_size'])
    return array[..., columns] * 0.25 + array[..., (columns + 3) % columns.size] * 0.5


def _timestep_embedding(values, np):
    half = 128
    frequencies = np.exp(-math.log(10000) * np.arange(half, dtype=np.float64) / half)
    args = values.reshape(-1, 1).astype(np.float64) * frequencies[None, :]
    return np.concatenate((np.cos(args), np.sin(args)), axis=1)


def size_embedding(orig, crop, target, np):
    """Independent F64 implementation of the pinned 3x2 SDXL size embedding."""
    values = [np.asarray(item) for item in (orig, crop, target)]
    if any(item.ndim != 2 or item.shape[1] != 2 for item in values) or (
            values[0].shape[0] < 1) or any(item.shape != values[0].shape for item in values[1:]):
        raise ValueError('conditioning_reference_size_shape')
    if any(not np.issubdtype(item.dtype, np.number) or not np.isfinite(item).all() for item in values):
        raise ValueError('conditioning_reference_size_values')
    return np.concatenate([_timestep_embedding(item, np).reshape(item.shape[0], 512)
                           for item in values], axis=1)


_PROFILE = {
    'schema_version': 1,
    'profile': PROFILE,
    'parameter_dtype': 'F32',
    'reference_math_dtype': 'F64',
    'configs': configs(),
    'token_embedding': '((row%19)-9)*((column%7)-3)/256+((column%11)-5)/64+role/128',
    'position_embedding': '((position%13)-6)*((column%5)-2)/1024',
    'layer_norm': {'weight': '1+((column%9)-4)*(layer+role)/8192',
                   'bias': '((column%7)-3)*(layer+role)/4096',
                   'epsilon': EPSILON, 'final_layer_index': 'encoder_layer_count'},
    'selected_hidden_states': [11, 31],
    'attention': {
        'causal': True, 'q_diagonal': '(1+layer%3)/8',
        'k_circular_input_plus': 1, 'k_coefficient': '-(1+layer%5)/16',
        'v_diagonal': '1/4', 'out_circular_input_plus': 2, 'out_coefficient': '1/8',
        'projection_bias': '((column%7)-3)*(layer+role)/4096',
    },
    'mlp': {
        'fc1': ('1/4*x', '-1/8*roll(x,-1)', '1/16*x', '-1/32*roll(x,-2)'),
        'fc1_bias': '((row%11)-5)/512+(layer+role)/2048',
        'fc2': ('1/8', '-1/16', '1/32', '1/64'),
        'fc2_bias': '((column%5)-2)*(layer+1)/4096',
        'activations': {'1': 'quick_gelu(x*sigmoid(1.702*x))', '2': 'gelu_erf'},
    },
    'projection': 'role2[row,row]=1/4; role2[row,row+3]=1/2',
    'size_embedding': 'F64 sin/cos, [original_hw,crop_top_left_hw,target_hw], 3x512',
}
PROFILE_SHA256 = _sha(_PROFILE)


def profile():
    """Return the immutable, path-free calibration recipe declaration."""
    return json.loads(_canonical(_PROFILE).decode('utf-8'))


def verify_controls(np):
    """Small algebra controls run inside the same contained native process.

    The controller needs no NumPy installation. These controls are separate
    from, and never substitute for, the full native encoder comparisons.
    """
    def require(value):
        if not value:
            raise ValueError('conditioning_reference_control_failed')
    def refuse(action):
        try:
            action()
        except ValueError:
            return
        raise ValueError('conditioning_reference_invalid_accepted')
    q = parameter(1, 'text_model.encoder.layers.1.self_attn.q_proj.weight', np)
    k = parameter(1, 'text_model.encoder.layers.1.self_attn.k_proj.weight', np)
    require(q.dtype == np.float32 and k.dtype == np.float32)
    require(q[4, 4] == .25 and q[4, 5] == 0 and k[4, 5] == -.125 and k[4, 4] == 0)
    rows = _embedding_rows(1, np.asarray([4, 5], dtype=np.int64), np)
    require(rows.dtype == np.float32 and not np.array_equal(rows[0], rows[1]))
    values = np.zeros((77, 4), dtype=np.float64)
    values[0, 0], values[76, 1] = 1, 3
    initial = _causal_attention(values, 1, 2, 2, np)
    values[76, 1] = 30
    later = _causal_attention(values, 1, 2, 2, np)
    require(np.array_equal(initial[:76], later[:76]) and not np.allclose(initial[76], later[76]))
    impulse = np.zeros((2, 1280), dtype=np.float64)
    impulse[:, 3] = (2, 5)
    projected = projection(impulse, np)
    require(np.array_equal(projected[:, 0], np.asarray((1, 2.5))))
    require(np.array_equal(projected[:, 3], np.asarray((.5, 1.25))))
    dense = parameter(2, 'text_projection.weight', np)
    require(np.array_equal(impulse @ dense.T, projected))
    orig, crop, target = (np.asarray(value, dtype=np.int64) for value in
                         ([[1024, 768], [768, 1024]], [[2, 5], [5, 2]], [[768, 512], [512, 768]]))
    size = size_embedding(orig, crop, target, np)
    require(size.shape == (2, 1536) and size.dtype == np.float64)
    require(abs(size[0, 0] - math.cos(1024)) < 1e-12 and abs(size[0, 128] - math.sin(1024)) < 1e-12)
    require(not np.array_equal(size[0], size[1]))
    refuse(lambda: size_embedding(orig[:, :1], crop, target, np))
    refuse(lambda: size_embedding(orig, [[float('nan'), 0], [0, 0]], target, np))
    refuse(lambda: forward(1, np.zeros((1, 77), dtype=np.int64), np))
    return list(CONTROL_CASES)
