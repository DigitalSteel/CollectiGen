"""Finite generated full-width SDXL encoder/conditioning numerical qualification.

This calibrates owned generated encoder objects after their original loading
and input-wiring checks. Calibration is distinct from checkpoint weights. No
selected learned weights, U-Net computation, training or general compatibility
claim follows. NumPy reference math is independent of the native Torch path.
"""
from __future__ import annotations

from . import training_conditioning_reference as reference
from . import training_model_pairing_contract as pairing
from .training_tokenizer_contract import canonical, sha, EOS


PROFILE = 'generated-sdxl-encoder-conditioning-v1'
ATOL, RTOL = 3e-4, 3e-4
SIZE_CASES = {
    'original_sizes_hw': [[1024, 768], [513, 769]],
    'crop_top_lefts': [[7, 11], [3, 5]],
    'target_sizes_hw': [[768, 1024], [512, 768]],
}
NEGATIVES = ('parameter_change', 'hidden_value', 'hidden_position', 'pooled_value',
             'size_order', 'nonfinite', 'shape', 'dtype')


def expected_report(dataset_request, pairing_request):
    paired = pairing.expected_report(dataset_request, pairing_request)
    summary = dataset_request['summary']
    return {'schema_version': 1, 'kind': 'generated_sdxl_encoder_conditioning', 'profile': PROFILE,
        'reference_profile_sha256': reference.PROFILE_SHA256, 'comparison': 'all_values_numpy_float64_reference',
        'absolute_tolerance': ATOL, 'relative_tolerance': RTOL,
        'model_pairing_checks': paired, 'calibration_scope': 'distinct_generated_in_memory_encoder_parameters',
        'encoder_dtype': 'F32', 'conditioning_dtype': 'F32', 'encoder_layers': [12, 32],
        'selected_hidden_states': [11, 31], 'text_width': 2048, 'vector_width': 2816,
        'sequence_length': pairing_request['caption_limit'] + 2,
        'dataset_exposures': summary['epoch_exposures'], 'dataset_batches': summary['epoch_batches'],
        'size_reference_cases': 2, 'size_case_set_sha256': sha(canonical(SIZE_CASES)),
        'reference_control_cases': list(reference.CONTROL_CASES),
        'negative_cases': list(NEGATIVES), 'generated_encoder_conditioning_verified': True,
        'selected_dataset_verified': False, 'selected_model_weights_loaded': False,
        'model_compatibility_verified': False, 'forward_pass_verified': False,
        'unet_forward_verified': False, 'offline_execution_verified': False, 'execution_ready': False}


def validate_report(value, dataset_request, pairing_request):
    if canonical(value) != canonical(expected_report(dataset_request, pairing_request)):
        raise ValueError('conditioning_report_changed')
    return value


def compare(actual, expected, torch, np):
    if (actual.device.type != 'cpu' or actual.dtype != torch.float32
            or list(actual.shape) != list(expected.shape)):
        raise ValueError('conditioning_output_layout_changed')
    values = actual.detach().numpy()
    if not np.isfinite(values).all() or not np.isfinite(expected).all() or not np.allclose(values, expected, atol=ATOL, rtol=RTOL):
        raise ValueError('conditioning_output_values_changed')


def _refuse(action):
    try:
        action()
    except ValueError:
        return
    raise ValueError('conditioning_negative_accepted')


def _verify_parameter(tensor, expected, torch):
    if (tensor.device.type != 'cpu' or tensor.dtype != torch.float32
            or tuple(tensor.shape) != tuple(expected.shape)
            or not torch.equal(tensor, torch.from_numpy(expected))):
        raise ValueError('conditioning_parameter_changed')


def _calibrate(models, torch, np):
    for role, model in models.items():
        shapes = reference.parameter_shapes(role)
        state = model.state_dict()
        if set(state) != set(shapes) or any(list(state[name].shape) != list(shape) for name, shape in shapes.items()):
            raise ValueError('conditioning_parameter_layout_changed')
        model.eval()
        with torch.no_grad():
            for name in shapes:
                expected = reference.parameter(role, name, np)
                state[name].copy_(torch.from_numpy(expected))
                _verify_parameter(state[name], expected, torch)
        # A genuine parameter mutation must be rejected before forward work.
        name = 'text_model.final_layer_norm.bias'
        expected = reference.parameter(role, name, np)
        with torch.no_grad():
            state[name][0].add_(1)
            _refuse(lambda: _verify_parameter(state[name], expected, torch))
            state[name].copy_(torch.from_numpy(expected))
        _verify_parameter(state[name], expected, torch)


def _expected_text(ids_list, np, cache):
    hidden, pools = [], []
    for role, ids in enumerate(ids_list, 1):
        rows = []
        for chunks in ids.tolist():
            windows = []
            for tokens in chunks:
                key = role, tuple(tokens)
                if key not in cache:
                    cache[key] = reference.forward(role, tokens, np)
                windows.append(cache[key])
            rows.append(np.stack([windows[window][0][position]
                                  for window, position in reference.stitch_positions(len(windows))]))
            if role == 2:
                index = chunks[0].index(EOS) if EOS in chunks[0] else 0
                pools.append(reference.projection(windows[0][1][index], np))
        hidden.append(np.stack(rows))
    return [*hidden, np.stack(pools)]


def _size(batch, np):
    return reference.size_embedding(*(batch[name].tolist() for name in SIZE_CASES), np)


def _check_assembly(entry_module, size_module, encoded, expected, batch, torch, np):
    count = encoded[0].shape[0]
    size = _size(batch, np)
    compare(size_module.get_size_embeddings(*(batch[name] for name in SIZE_CASES), 'cpu'), size, torch, np)
    text = np.concatenate(expected[:2], axis=2)
    vector = np.concatenate([expected[2], size], axis=1)
    latents, timesteps = torch.zeros((count, 4, 1, 1)), torch.arange(count)
    calls = []
    class Accelerator:
        device = 'cpu'
    def capture(actual_latents, actual_steps, actual_text, actual_vector):
        compare(actual_text, text, torch, np)
        compare(actual_vector, vector, torch, np)
        if not torch.equal(actual_latents, latents) or not torch.equal(actual_steps, timesteps):
            raise ValueError('conditioning_sample_order_changed')
        calls.append(True)
        return actual_latents
    trainer = entry_module.SdxlNetworkTrainer.__new__(entry_module.SdxlNetworkTrainer)
    returned = trainer.call_unet(None, Accelerator(), capture, latents, timesteps, encoded, batch, torch.float32)
    if calls != [True] or returned is not latents:
        raise ValueError('conditioning_capture_changed')


def _negative_outputs(encoded, expected, size_module, torch, np):
    changed = encoded[0].clone(); changed[0, 2, 1] += 1
    _refuse(lambda: compare(changed, expected[0], torch, np))
    _refuse(lambda: compare(encoded[0].flip((1,)), expected[0], torch, np))
    changed = encoded[2].clone(); changed[0, 0] += 1
    _refuse(lambda: compare(changed, expected[2], torch, np))
    changed = encoded[0].clone(); changed[0, 0, 0] = float('nan')
    _refuse(lambda: compare(changed, expected[0], torch, np))
    _refuse(lambda: compare(encoded[0][:, :-1], expected[0], torch, np))
    _refuse(lambda: compare(encoded[0].to(torch.float16), expected[0], torch, np))
    tensors = [torch.tensor(SIZE_CASES[name]) for name in SIZE_CASES]
    size = reference.size_embedding(*(SIZE_CASES[name] for name in SIZE_CASES), np)
    compare(size_module.get_size_embeddings(*tensors, 'cpu'), size, torch, np)
    _refuse(lambda: compare(size_module.get_size_embeddings(tensors[2], tensors[1], tensors[0], 'cpu'), size, torch, np))


def probe_conditioning(model_module, torch, safetensors, model_catalog, tokenizer_class, strategy_class,
                       encoding_class, config_module, entry_module, dataset_module, size_module, np,
                       Image, dataset_request, pairing_request, work):
    expected_report(dataset_request, pairing_request)
    observed = []
    def observe(models, strategy, batches):
        if reference.verify_controls(np) != list(reference.CONTROL_CASES):
            raise ValueError('conditioning_reference_controls_changed')
        _calibrate(models, torch, np)
        cache = {}
        encoding = encoding_class()
        exposures = 0
        for index, batch in enumerate(batches):
            expected = _expected_text(batch['input_ids_list'], np, cache)
            with torch.no_grad():
                encoded = encoding.encode_tokens(strategy, [models[1], models[2]], batch['input_ids_list'])
            if type(encoded) is not list or len(encoded) != 3:
                raise ValueError('conditioning_encoder_outputs_changed')
            for actual, wanted in zip(encoded, expected):
                compare(actual, wanted, torch, np)
            _check_assembly(entry_module, size_module, encoded, expected, batch, torch, np)
            if index == 0:
                _negative_outputs(encoded, expected, size_module, torch, np)
            exposures += encoded[0].shape[0]
        if exposures != dataset_request['summary']['epoch_exposures'] or len(batches) != dataset_request['summary']['epoch_batches']:
            raise ValueError('conditioning_exposures_changed')
        # Every calibrated parameter stays exact after all forwards.
        for role, model in models.items():
            for name, tensor in model.state_dict().items():
                _verify_parameter(tensor, reference.parameter(role, name, np), torch)
        observed.append(True)
    paired = pairing.probe_pairing(model_module, torch, safetensors, model_catalog, tokenizer_class, strategy_class,
        config_module, entry_module, dataset_module, np, Image, dataset_request, pairing_request, work, observe=observe)
    pairing.validate_report(paired, dataset_request, pairing_request)
    if observed != [True]:
        raise ValueError('conditioning_observation_missing')
    return expected_report(dataset_request, pairing_request)
