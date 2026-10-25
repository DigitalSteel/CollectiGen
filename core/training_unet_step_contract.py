"""Generated full-shape SDXL U-Net/LoRA optimizer checks in a local GPU worker.

No learned weights, images, dataset, trainer.train(), checkpoint resume or model
registration is used. Finite numerical execution does not establish quality.
"""
from __future__ import annotations

import gc
from copy import deepcopy
import hashlib
import math
import re
from types import SimpleNamespace

from .training_model_contract import PREFIXES, canonical
from .training_model_loading_contract import checked_catalog, CATALOG_SHA256

PROFILE = 'generated-sdxl-unet-lora-step-v1'
PRECISIONS = ('fp16', 'bf16')
MIN_FREE_BYTES = 8 * 1024**3
MAX_OUTPUT_BYTES = 512 * 1024**2
SHA = re.compile(r'[0-9a-f]{64}\Z')
CONFIG = {'rank': 32, 'alpha': 16, 'learning_rate': 0.0001,
    'optimizer': 'AdamW', 'weight_decay': 0.0, 'gradient_checkpointing': True,
    'attention': 'sdpa', 'steps_per_precision': 2, 'latent_shape': [1, 4, 8, 8],
    'text_tokens': 77, 'generated_seed': 42, 'saved_precision': 'fp16'}
FALSE_FLAGS = ('selected_model_weights_loaded', 'selected_dataset_verified',
    'selected_training_started', 'model_compatibility_verified', 'execution_ready')


def filenames():
    return tuple('generated-lora-' + precision + '.safetensors' for precision in PRECISIONS)


def report(device, rows):
    return {'schema_version': 1, 'kind': 'generated_sdxl_unet_lora_step',
        'profile': PROFILE, 'catalog_sha256': CATALOG_SHA256, 'configuration': deepcopy(CONFIG),
        'device': device, 'checks': rows, 'generated_optimizer_steps': 4,
        **{name: False for name in FALSE_FLAGS}}


def _int(value, low, high):
    return type(value) is int and low <= value <= high


def validate_report(value):
    if (type(value) is not dict or set(value) != set(report(None, None))
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['kind'] != 'generated_sdxl_unet_lora_step' or value['profile'] != PROFILE
            or value['catalog_sha256'] != CATALOG_SHA256
            or canonical(value['configuration']) != canonical(CONFIG)
            or type(value['generated_optimizer_steps']) is not int or value['generated_optimizer_steps'] != 4
            or any(value[name] is not False for name in FALSE_FLAGS)):
        raise ValueError('unet_step_report_changed')
    device = value['device']
    if (type(device) is not dict or set(device) != {'index', 'capability', 'total_bytes', 'free_before_bytes'}
            or type(device['index']) is not int or device['index'] != 0
            or type(device['capability']) is not list or len(device['capability']) != 2
            or not all(_int(n, 0, 99) for n in device['capability'])
            or not _int(device['total_bytes'], MIN_FREE_BYTES, 1024**4)
            or not _int(device['free_before_bytes'], MIN_FREE_BYTES, device['total_bytes'])):
        raise ValueError('unet_step_device_changed')
    rows = value['checks']
    if type(rows) is not list or len(rows) != 2:
        raise ValueError('unet_step_precisions_changed')
    fields = {'precision', 'base_tensors', 'lora_modules', 'lora_parameters',
        'base_before_sha256', 'base_after_sha256', 'lora_before_sha256', 'lora_after_sha256',
        'lora_layout_sha256', 'output_sha256', 'output_bytes', 'saved_tensors',
        'peak_allocated_bytes', 'steps', 'initial_zero_delta_verified', 'saved_readback_verified', 'final_output_changed'}
    for row, precision in zip(rows, PRECISIONS):
        if (type(row) is not dict or set(row) != fields or row['precision'] != precision
                or not _int(row['base_tensors'], 1680, 1680)
                or not _int(row['lora_modules'], 100, 2000)
                or not _int(row['lora_parameters'], row['lora_modules'] * 2, row['lora_modules'] * 2)
                or not _int(row['saved_tensors'], row['lora_modules'] * 3, row['lora_modules'] * 3)
                or not _int(row['output_bytes'], 1, MAX_OUTPUT_BYTES)
                or not _int(row['peak_allocated_bytes'], 1, device['total_bytes'])
                or row['initial_zero_delta_verified'] is not True or row['saved_readback_verified'] is not True or row['final_output_changed'] is not True
                or any(type(row[key]) is not str or not SHA.fullmatch(row[key]) for key in fields if key.endswith('_sha256'))
                or row['base_before_sha256'] != row['base_after_sha256']
                or row['lora_before_sha256'] == row['lora_after_sha256']):
            raise ValueError('unet_step_weights_changed')
        if type(row['steps']) is not list or len(row['steps']) != 2:
            raise ValueError('unet_step_count_changed')
        for step, number in zip(row['steps'], (1, 2)):
            if (type(step) is not dict or set(step) != {'step', 'loss', 'gradient_norm', 'changed_up', 'changed_down', 'nonzero_up', 'nonzero_down'}
                    or not _int(step['step'], number, number)
                    or any(type(step[key]) is not float or not math.isfinite(step[key]) or step[key] <= 0 for key in ('loss', 'gradient_norm'))
                    or not _int(step['changed_up'], 1, row['lora_modules'])
                    or not _int(step['nonzero_up'], 1, row['lora_modules'])
                    or not _int(step['nonzero_down'], 0 if number == 1 else 1, row['lora_modules'])
                    or not _int(step['changed_down'], 0 if number == 1 else 1, row['lora_modules'])):
                raise ValueError('unet_step_optimizer_changed')
    return value


def device_gate(torch):
    if not torch.cuda.is_available() or not torch.cuda.is_bf16_supported():
        raise ValueError('unet_step_cuda_precision_unavailable')
    free, total = torch.cuda.mem_get_info(0)
    if free < MIN_FREE_BYTES:
        raise ValueError('unet_step_gpu_capacity_held')
    return {'index': 0, 'capability': list(torch.cuda.get_device_capability(0)),
        'total_bytes': total, 'free_before_bytes': free}


def state_hash(state, torch):
    digest = hashlib.sha256()
    for name, tensor in sorted(state.items()):
        if not torch.isfinite(tensor).all().item():
            raise ValueError('unet_step_nonfinite_weights')
        digest.update(canonical([name, list(tensor.shape), str(tensor.dtype)]))
        digest.update(tensor.detach().contiguous().reshape(-1).view(torch.uint8).cpu().numpy().tobytes())
    return digest.hexdigest()


def calibrate(unet, torch):
    generator = torch.Generator(device='cuda:0').manual_seed(CONFIG['generated_seed'])
    with torch.no_grad():
        for name, parameter in unet.named_parameters():
            if name.endswith('bias'):
                parameter.zero_()
            elif parameter.ndim == 1:
                parameter.fill_(1)
            else:
                parameter.normal_(0, 1 / math.sqrt(math.prod(parameter.shape[1:])), generator=generator)


def probe_steps(unet_module, lora_module, entry_module, torch, safetensors, catalog, work):
    checked_catalog(catalog)
    device = device_gate(torch)
    torch.set_num_threads(2)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    shapes = {name[len(PREFIXES[0]):]: shape for name, shape in catalog['tensors'].items() if name.startswith(PREFIXES[0])}
    rows = []
    for precision, filename in zip(PRECISIONS, filenames()):
        device_gate(torch)  # Other applications may have consumed capacity.
        dtype = torch.float16 if precision == 'fp16' else torch.bfloat16
        torch.cuda.reset_peak_memory_stats(0)
        torch.manual_seed(CONFIG['generated_seed'])
        with torch.device('meta'):
            unet = unet_module.SdxlUNet2DConditionModel(in_channels=4)
        if {name: list(t.shape) for name, t in unet.state_dict().items()} != shapes:
            raise ValueError('unet_step_architecture_changed')
        unet.to(dtype=dtype).to_empty(device='cuda:0')
        calibrate(unet, torch)
        unet.requires_grad_(False)
        unet.set_use_sdpa(True)
        unet.enable_gradient_checkpointing()
        unet.train()
        before = state_hash(unet.state_dict(), torch)
        network = lora_module.create_network(1.0, CONFIG['rank'], CONFIG['alpha'], None, [], unet)
        network.apply_to([], unet, apply_text_encoder=False, apply_unet=True)
        network.to(device='cuda:0', dtype=torch.float32)
        if network.text_encoder_loras or not network.unet_loras:
            raise ValueError('unet_step_adapter_scope_changed')
        groups, labels = network.prepare_optimizer_params(None, CONFIG['learning_rate'], CONFIG['learning_rate'])
        parameters = dict(network.named_parameters())
        grouped = [parameter for group in groups for parameter in group['params']]
        if (labels != ['unet'] or len(grouped) != len(parameters)
                or {id(p) for p in grouped} != {id(p) for p in parameters.values()}
                or {id(p) for p in grouped} & {id(p) for p in unet.parameters()}
                or any(not name.startswith('lora_unet_') or p.device.type != 'cuda' or p.dtype != torch.float32 or not p.requires_grad for name, p in parameters.items())):
            raise ValueError('unet_step_optimizer_scope_changed')
        optimizer = torch.optim.AdamW(groups, lr=CONFIG['learning_rate'], weight_decay=CONFIG['weight_decay'])
        scaler = torch.amp.GradScaler('cuda', enabled=precision == 'fp16', init_scale=128)
        start = state_hash(network.state_dict(), torch)
        layout = hashlib.sha256(canonical({name: list(t.shape) for name, t in network.state_dict().items()})).hexdigest()
        latent = torch.linspace(-.5, .5, 256, device='cuda:0').reshape(CONFIG['latent_shape']).to(dtype).requires_grad_(True)
        text = [torch.linspace(-.1, .1, 77 * width, device='cuda:0').reshape(1, 77, width).to(dtype) for width in (768, 1280)]
        text.append(torch.linspace(-.1, .1, 1280, device='cuda:0').reshape(1, 1280).to(dtype))
        batch = {name: torch.tensor([value], device='cuda:0') for name, value in (
            ('original_sizes_hw', [64, 64]), ('crop_top_lefts', [0, 0]), ('target_sizes_hw', [64, 64]))}
        timestep = torch.tensor([17], device='cuda:0')
        trainer = entry_module.SdxlNetworkTrainer.__new__(entry_module.SdxlNetworkTrainer)
        def forward():
            with torch.autocast('cuda', dtype=dtype):
                result = trainer.call_unet(None, SimpleNamespace(device='cuda:0'), unet, latent, timestep, text, batch, dtype)
            if list(result.shape) != CONFIG['latent_shape'] or result.device.type != 'cuda' or result.dtype != dtype or not torch.isfinite(result).all().item():
                raise ValueError('unet_step_forward_changed')
            return result
        with torch.no_grad():
            network.set_multiplier(0)
            baseline = forward()
            network.set_multiplier(1)
            if not torch.equal(baseline, forward()):
                raise ValueError('unet_step_initial_delta_changed')
        steps = []
        for step in (1, 2):
            prior = {name: p.detach().clone() for name, p in parameters.items()}
            optimizer.zero_grad(set_to_none=True)
            latent.grad = None
            loss = (forward().float() - .125).square().mean()
            scaler.scale(loss).backward()
            scaler.unscale_(optimizer)
            if any(p.grad is None or not torch.isfinite(p.grad).all().item() for p in parameters.values()):
                raise ValueError('unet_step_gradients_invalid')
            nonzero = {direction: sum(bool(torch.count_nonzero(p.grad).item()) for name, p in parameters.items() if 'lora_' + direction + '.weight' in name) for direction in ('up', 'down')}
            norm = torch.nn.utils.clip_grad_norm_(list(parameters.values()), 1.0, error_if_nonfinite=True)
            if not math.isfinite(float(norm)) or float(norm) <= 0 or any(p.grad is not None for p in unet.parameters()):
                raise ValueError('unet_step_frozen_base_changed')
            scaler.step(optimizer)
            scaler.update()
            changes = {direction: sum(not torch.equal(p, prior[name]) for name, p in parameters.items() if 'lora_' + direction + '.weight' in name) for direction in ('up', 'down')}
            steps.append({'step': step, 'loss': float(loss), 'gradient_norm': float(norm),
                'changed_up': changes['up'], 'changed_down': changes['down'],
                'nonzero_up': nonzero['up'], 'nonzero_down': nonzero['down']})
            del prior, loss
        with torch.no_grad():
            if torch.equal(baseline, forward()):
                raise ValueError('unet_step_output_unchanged')
        del baseline
        after = state_hash(unet.state_dict(), torch)
        final = state_hash(network.state_dict(), torch)
        saved = {name: t.detach().to(device='cpu', dtype=torch.float16).contiguous() for name, t in network.state_dict().items()}
        if any(not torch.isfinite(t).all().item() for t in saved.values()):
            raise ValueError('unet_step_saved_precision_overflow')
        raw = safetensors.save(saved, metadata={'collectigen_scope': PROFILE})
        if len(raw) > MAX_OUTPUT_BYTES:
            raise ValueError('unet_step_output_bound')
        destination = work / filename
        with destination.open('xb') as handle:
            handle.write(raw)
        reread = destination.read_bytes()
        decoded = safetensors.load(reread)
        if reread != raw or set(decoded) != set(saved) or any(decoded[name].dtype != t.dtype or decoded[name].shape != t.shape or not torch.equal(decoded[name], t) or not torch.isfinite(decoded[name]).all().item() for name, t in saved.items()):
            raise ValueError('unet_step_output_readback_changed')
        rows.append({'precision': precision, 'base_tensors': len(shapes),
            'lora_modules': len(network.unet_loras), 'lora_parameters': len(parameters),
            'base_before_sha256': before, 'base_after_sha256': after,
            'lora_before_sha256': start, 'lora_after_sha256': final, 'lora_layout_sha256': layout,
            'output_sha256': hashlib.sha256(raw).hexdigest(), 'output_bytes': len(raw),
            'saved_tensors': len(saved), 'peak_allocated_bytes': torch.cuda.max_memory_allocated(0),
            'steps': steps, 'initial_zero_delta_verified': True, 'saved_readback_verified': True, 'final_output_changed': True})
        del trainer, forward, unet, network, optimizer, scaler, parameters, grouped, groups, saved, decoded, raw, reread, latent, text, batch, timestep, norm
        gc.collect()
        torch.cuda.empty_cache()
    return validate_report(report(device, rows))
