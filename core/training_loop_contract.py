"""Generated SDXL fresh-run qualification through the pinned trainer loop.

Only resource suppliers and observation hooks differ from the public trainer.
No learned weights, selected images, resume state or application launch is used.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
import sys

from .training_model_contract import PREFIXES, canonical
from .training_model_loading_contract import checked_catalog, CATALOG_SHA256, resource_gate
from .training_sdxl_loading import text_config
from .training_model_pairing_contract import _write_tokenizers, _strategy
from .training_unet_step_contract import state_hash, SHA, MAX_OUTPUT_BYTES, device_gate

PROFILE = 'generated-sdxl-training-loop-v1'
CONFIG = {'images': 2, 'resolution': 64, 'batch_size': 1, 'tokens': 77,
    'rank': 32, 'alpha': 16, 'learning_rate': .0001, 'optimizer': 'AdamW',
    'weight_decay': 0.0, 'scheduler': 'constant', 'noise_scheduler': 'scaled_linear_1000_epsilon',
    'gradient_accumulation_steps': 2, 'optimizer_steps': 2, 'batches': 4,
    'gradient_checkpointing': True, 'attention': 'sdpa', 'mixed_precision': 'bf16',
    'vae_precision': 'float32', 'saved_precision': 'fp16', 'generated_seed': 42,
    'console_encoding': 'utf-8'}
FALSE_FLAGS = ('selected_model_weights_loaded', 'selected_dataset_verified',
    'selected_training_started', 'resume_verified', 'model_compatibility_verified', 'execution_ready')
ROLES = ('unet', 'encoder_1', 'encoder_2', 'vae')
BASE_TENSORS = {'unet': 1680, 'encoder_1': 196, 'encoder_2': 517, 'vae': 248}
CAPTIONS = ('generated red square', 'generated blue circle')


def filenames():
    return ('generated-training-step00000001.safetensors',
        'generated-training-step00000002.safetensors', 'generated-training.safetensors')


def report(device, bases, lora, losses, checkpoints, example_batches):
    return {'schema_version': 1, 'kind': 'generated_sdxl_training_loop', 'profile': PROFILE,
        'catalog_sha256': CATALOG_SHA256, 'configuration': deepcopy(CONFIG), 'device': device,
        'base_models': bases, 'lora': lora, 'losses': losses, 'checkpoints': checkpoints,
        'example_batches': example_batches, 'image_caption_pairing_verified': True,
        'vae_batches': 4, 'encoder_batches': [4, 4], 'noise_batches': 4,
        'accumulation_verified': True, 'completed_optimizer_steps': 2,
        **{name: False for name in FALSE_FLAGS}}


def _integer(value, low, high):
    return type(value) is int and low <= value <= high


def _hash(value):
    return type(value) is str and SHA.fullmatch(value) is not None


def require_checkpointing(unet):
    query = getattr(unet, 'is_gradient_checkpointing', None)
    if not callable(query) or query() is not True:
        raise ValueError('training_loop_checkpointing_inactive')


def validate_report(value):
    if (type(value) is not dict or set(value) != set(report(None, None, None, None, None, None))
            or canonical({key: value[key] for key in ('schema_version', 'kind', 'profile', 'catalog_sha256', 'configuration')})
            != canonical({key: report(None, None, None, None, None, None)[key] for key in ('schema_version', 'kind', 'profile', 'catalog_sha256', 'configuration')})
            or any(value[key] is not False for key in FALSE_FLAGS)
            or value['accumulation_verified'] is not True
            or value['image_caption_pairing_verified'] is not True
            or type(value['example_batches']) is not list or canonical(value['example_batches']) != canonical([2, 2])
            or not _integer(value['completed_optimizer_steps'], 2, 2)
            or not _integer(value['vae_batches'], 4, 4) or not _integer(value['noise_batches'], 4, 4)
            or type(value['encoder_batches']) is not list or canonical(value['encoder_batches']) != canonical([4, 4])):
        raise ValueError('training_loop_report_changed')
    device = value['device']
    if (type(device) is not dict or set(device) != {'index', 'capability', 'total_bytes', 'free_before_bytes', 'peak_allocated_bytes'}
            or not _integer(device['index'], 0, 0) or type(device['capability']) is not list or len(device['capability']) != 2
            or not all(_integer(n, 0, 99) for n in device['capability'])
            or not _integer(device['total_bytes'], 8 * 1024**3, 1024**4)
            or not _integer(device['free_before_bytes'], 8 * 1024**3, device['total_bytes'])
            or not _integer(device['peak_allocated_bytes'], 1, device['total_bytes'])):
        raise ValueError('training_loop_device_changed')
    bases = value['base_models']
    if type(bases) is not dict or set(bases) != set(ROLES):
        raise ValueError('training_loop_base_roles_changed')
    for role, row in bases.items():
        if (type(row) is not dict or set(row) != {'tensors', 'before_sha256', 'after_sha256'}
                or not _integer(row['tensors'], BASE_TENSORS[role], BASE_TENSORS[role])
                or not _hash(row['before_sha256']) or row['after_sha256'] != row['before_sha256']):
            raise ValueError('training_loop_base_changed')
    lora = value['lora']
    if (type(lora) is not dict or set(lora) != {'modules', 'parameters', 'before_sha256', 'after_sha256'}
            or not _integer(lora['modules'], 722, 722) or not _integer(lora['parameters'], 1444, 1444)
            or not _hash(lora['before_sha256']) or not _hash(lora['after_sha256']) or lora['before_sha256'] == lora['after_sha256']):
        raise ValueError('training_loop_lora_changed')
    if (type(value['losses']) is not list or len(value['losses']) != 4
            or any(type(n) is not float or not math.isfinite(n) or n <= 0 for n in value['losses'])):
        raise ValueError('training_loop_losses_changed')
    checkpoints = value['checkpoints']
    if type(checkpoints) is not list or len(checkpoints) != 3:
        raise ValueError('training_loop_checkpoints_changed')
    # Pinned step saves use the zero-based loop epoch; final save uses its count.
    for row, name, step, epoch in zip(checkpoints, filenames(), (1, 2, 2), (0, 1, 2)):
        if (type(row) is not dict or set(row) != {'filename', 'step', 'epoch', 'weights_sha256', 'output_sha256', 'output_bytes', 'saved_tensors', 'readback_verified'}
                or row['filename'] != name or not _integer(row['step'], step, step) or not _integer(row['epoch'], epoch, epoch)
                or not _hash(row['weights_sha256']) or not _hash(row['output_sha256'])
                or not _integer(row['output_bytes'], 1, MAX_OUTPUT_BYTES) or not _integer(row['saved_tensors'], 2166, 2166)
                or row['readback_verified'] is not True):
            raise ValueError('training_loop_checkpoint_changed')
    hashes = [row['weights_sha256'] for row in checkpoints]
    if hashes[0] == lora['before_sha256'] or hashes[0] == hashes[1] or hashes[1] != hashes[2] or hashes[2] != lora['after_sha256']:
        raise ValueError('training_loop_updates_changed')
    return value


def generated_arguments(entry, image_module, toml, work):
    """Real parser + real image/caption dataset, entirely generated here."""
    folder = work / 'generated-images'
    folder.mkdir()
    for index, caption in enumerate(CAPTIONS):
        image = image_module.new('RGB', (64, 64), (192, 32, 16) if index == 0 else (16, 32, 192))
        image.save(folder / f'{index}.png')
        (folder / f'{index}.txt').write_text(caption, encoding='utf-8')
    dataset = {'general': {'caption_extension': '.txt', 'shuffle_caption': False},
        'datasets': [{'resolution': 64, 'batch_size': 1, 'enable_bucket': False,
            'subsets': [{'image_dir': str(folder), 'num_repeats': 1}]}]}
    config = work / 'generated-dataset.toml'
    config.write_text(toml.dumps(dataset), encoding='utf-8')
    return entry.setup_parser().parse_args(['--dataset_config', str(config),
        '--output_dir', str(work), '--output_name', 'generated-training',
        '--network_module', 'networks.lora', '--network_train_unet_only',
        '--network_dim', '32', '--network_alpha', '16', '--learning_rate', '.0001',
        '--optimizer_type', 'AdamW', '--optimizer_args', 'weight_decay=0',
        '--lr_scheduler', 'constant', '--max_train_steps', '2',
        '--gradient_accumulation_steps', '2', '--max_data_loader_n_workers', '0',
        '--gradient_checkpointing', '--sdpa', '--mixed_precision', 'bf16', '--no_half_vae',
        '--save_model_as', 'safetensors', '--save_precision', 'fp16', '--save_every_n_steps', '1', '--seed', '42'])


def _generated_models(module, torch, catalog):
    with torch.device('meta'):
        models = {'unet': module.sdxl_original_unet.SdxlUNet2DConditionModel(in_channels=4),
            'encoder_1': module.CLIPTextModel(text_config(module, 768, 12, 12, 'quick_gelu')),
            'encoder_2': module.CLIPTextModelWithProjection(text_config(module, 1280, 32, 20, 'gelu')),
            'vae': module.AutoencoderKL(**module.model_util.create_vae_diffusers_config())}
    shapes = {name[len(PREFIXES[0]):]: shape for name, shape in catalog['tensors'].items() if name.startswith(PREFIXES[0])}
    if {name: list(t.shape) for name, t in models['unet'].state_dict().items()} != shapes:
        raise ValueError('training_loop_architecture_changed')
    generator = torch.Generator(device='cpu').manual_seed(CONFIG['generated_seed'])
    with torch.no_grad():
        for role, model in models.items():
            model.to(dtype=torch.float32 if role == 'vae' else torch.bfloat16).to_empty(device='cpu')
            for name, parameter in model.named_parameters():
                if name.endswith('bias'):
                    parameter.zero_()
                elif parameter.ndim == 1:
                    parameter.fill_(1)
                else:
                    parameter.normal_(0, 1 / math.sqrt(math.prod(parameter.shape[1:])), generator=generator)
            # CLIP's nonpersistent position IDs were also allocated from meta.
            for name, buffer in model.named_buffers():
                if name == 'text_model.embeddings.position_ids':
                    buffer.copy_(torch.arange(77).expand_as(buffer))
                else:
                    raise ValueError('training_loop_unhandled_model_buffer')
            model.requires_grad_(False)
    models['unet'].set_use_sdpa(True)
    return models


def probe_loop(entry, model_module, strategy_module, transformers, torch, safetensors, image_module, toml, catalog, work):
    checked_catalog(catalog)
    if sys.flags.utf8_mode != 1 or sys.stdout.encoding.lower() != 'utf-8':
        raise ValueError('training_loop_console_mode_changed')
    try:
        resource_gate(work)
    except ValueError:
        raise ValueError('training_loop_resources_held') from None
    device = device_gate(torch)
    torch.set_num_threads(2)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.cuda.reset_peak_memory_stats(0)
    args = generated_arguments(entry, image_module, toml, work)
    tokenizers, _ = _write_tokenizers(transformers.CLIPTokenizer, work)
    strategy = _strategy(strategy_module.SdxlTokenizeStrategy, tokenizers, 75)
    losses, checkpoints, models, bases, initial = [], [], {}, {}, {}
    counts = {'vae': 0, 'encoders': [0, 0], 'noise': 0, 'starts': 0}
    examples = [0, 0]

    def finite(tensor, shape):
        if list(tensor.shape) != shape or not torch.isfinite(tensor).all().item():
            raise ValueError('training_loop_tensor_changed')

    def frozen():
        if any(p.requires_grad or p.grad is not None
                or p.dtype != (torch.float32 if role == 'vae' else torch.bfloat16)
                for role, model in models.items() for p in model.parameters()):
            raise ValueError('training_loop_base_not_frozen')

    class GeneratedTrainer(entry.SdxlNetworkTrainer):
        def get_tokenize_strategy(self, args):
            return strategy

        def load_target_model(self, args, weight_dtype, accelerator):
            print('generated training: constructing full SDXL components', flush=True)
            models.update(_generated_models(model_module, torch, catalog))
            self.load_stable_diffusion_format, self.logit_scale, self.ckpt_info = True, None, None
            for index, role in enumerate(('encoder_1', 'encoder_2')):
                def observed(module, inputs, output, index=index):
                    finite(output.last_hidden_state, [1, 77, 768 if index == 0 else 1280])
                    counts['encoders'][index] += 1
                models[role].register_forward_hook(observed)
            return model_module.MODEL_VERSION_SDXL_BASE_V1_0, [models['encoder_1'], models['encoder_2']], models['vae'], models['unet']

        def encode_images_to_latents(self, args, vae, images):
            finite(images, [1, 3, 64, 64])
            latents = super().encode_images_to_latents(args, vae, images)
            # Check before the trainer's NaN-to-zero fallback can mask failure.
            finite(latents, [1, 4, 8, 8])
            counts['vae'] += 1
            return latents

        def on_step_start(self, args, accelerator, network, text_encoders, unet, batch, weight_dtype, is_train=True):
            super().on_step_start(args, accelerator, network, text_encoders, unet, batch, weight_dtype, is_train)
            caption = batch['captions']
            if type(caption) is not list or len(caption) != 1 or caption[0] not in CAPTIONS:
                raise ValueError('training_loop_example_caption_changed')
            index = CAPTIONS.index(caption[0])
            images = batch['images']
            finite(images, [1, 3, 64, 64])
            expected = torch.tensor((192, 32, 16) if index == 0 else (16, 32, 192), dtype=torch.float32)
            expected = expected.div(255).sub(.5).div(.5).reshape(1, 3, 1, 1).expand_as(images)
            if images.dtype != torch.float32 or not torch.equal(images.cpu(), expected):
                raise ValueError('training_loop_image_caption_pair_changed')
            examples[index] += 1
            frozen()
            network = accelerator.unwrap_model(network)
            current = state_hash(network.state_dict(), torch)
            if counts['starts'] == 0:
                require_checkpointing(accelerator.unwrap_model(unet))
                for role, model in models.items():
                    bases[role] = {'tensors': len(model.state_dict()), 'before_sha256': state_hash(model.state_dict(), torch)}
                initial.update(modules=len(network.unet_loras), parameters=len(list(network.parameters())), before_sha256=current)
                if network.text_encoder_loras or initial['modules'] != 722 or initial['parameters'] != 1444:
                    raise ValueError('training_loop_network_scope_changed')
                if any(p.dtype != torch.float32 or not p.requires_grad for p in network.parameters()):
                    raise ValueError('training_loop_network_precision_changed')
            expected = initial['before_sha256'] if counts['starts'] < 2 else checkpoints[0]['weights_sha256']
            if current != expected:
                raise ValueError('training_loop_accumulation_changed')
            counts['starts'] += 1

        def get_noise_pred_and_target(self, args, accelerator, noise_scheduler, latents, batch, text_encoder_conds, unet, network, weight_dtype, train_unet, is_train=True):
            finite(latents, [1, 4, 8, 8])
            for tensor, shape in zip(text_encoder_conds, ([1, 77, 768], [1, 77, 1280], [1, 1280])):
                finite(tensor, shape)
            if len(text_encoder_conds) != 3 or list(batch['captions']) not in ([CAPTIONS[0]], [CAPTIONS[1]]):
                raise ValueError('training_loop_conditioning_changed')
            if (noise_scheduler.config.num_train_timesteps, noise_scheduler.config.beta_schedule, noise_scheduler.config.prediction_type) != (1000, 'scaled_linear', 'epsilon'):
                raise ValueError('training_loop_noise_scheduler_changed')
            result = super().get_noise_pred_and_target(args, accelerator, noise_scheduler, latents, batch, text_encoder_conds, unet, network, weight_dtype, train_unet, is_train)
            for tensor in result[:2]:
                finite(tensor, [1, 4, 8, 8])
            if list(result[2].shape) != [1] or not 0 <= result[2].item() < 1000 or result[3] is not None:
                raise ValueError('training_loop_noise_target_changed')
            counts['noise'] += 1
            return result

        def process_batch(self, *args, **kwargs):
            value = super().process_batch(*args, **kwargs)
            number = float(value.detach().item())
            if not math.isfinite(number) or number <= 0:
                raise ValueError('training_loop_loss_invalid')
            losses.append(number)
            return value

        def _save_model(self, **kwargs):
            number = len(checkpoints)
            if number >= 3 or kwargs['ckpt_name'] != filenames()[number] or kwargs['steps'] != (1, 2, 2)[number]:
                raise ValueError('training_loop_checkpoint_order_changed')
            network = kwargs['unwrapped_nw']
            before = state_hash(network.state_dict(), torch)
            path = work / kwargs['ckpt_name']
            if path.exists():
                raise ValueError('training_loop_checkpoint_exists')
            super()._save_model(**kwargs)
            size = path.stat().st_size
            if not 8 < size <= MAX_OUTPUT_BYTES:
                raise ValueError('training_loop_output_bound')
            raw = path.read_bytes()
            state = safetensors.load(raw)
            expected = network.state_dict()
            if set(state) != set(expected):
                raise ValueError('training_loop_saved_keys_changed')
            for name, tensor in state.items():
                if tensor.dtype != torch.float16 or not torch.isfinite(tensor).all().item() or not torch.equal(tensor, expected[name].detach().to(device='cpu', dtype=torch.float16)):
                    raise ValueError('training_loop_readback_changed')
            import json
            header_size = int.from_bytes(raw[:8], 'little')
            metadata = json.loads(raw[8:8 + header_size])['__metadata__']
            effective = {'ss_steps': str(kwargs['steps']), 'ss_epoch': str(kwargs['epoch_no']),
                'ss_network_module': 'networks.lora', 'ss_network_dim': '32', 'ss_network_alpha': '16.0',
                'ss_gradient_accumulation_steps': '2', 'ss_mixed_precision': 'bf16',
                'ss_learning_rate': '0.0001', 'ss_optimizer': 'torch.optim.adamw.AdamW(weight_decay=0)',
                'ss_lr_scheduler': 'constant', 'ss_lr_warmup_steps': '0', 'ss_max_train_steps': '2',
                'ss_num_train_images': '2', 'ss_num_batches_per_epoch': '2', 'ss_num_epochs': '2',
                'ss_gradient_checkpointing': 'True', 'ss_cache_latents': 'False', 'ss_loss_type': 'l2'}
            if any(metadata[key] != expected for key, expected in effective.items()):
                raise ValueError('training_loop_saved_metadata_changed')
            frozen()
            checkpoints.append({'filename': path.name, 'step': kwargs['steps'], 'epoch': kwargs['epoch_no'],
                'weights_sha256': before, 'output_sha256': hashlib.sha256(raw).hexdigest(),
                'output_bytes': len(raw), 'saved_tensors': len(state), 'readback_verified': True})
            print(f'generated training: verified checkpoint {len(checkpoints)} at optimizer step {kwargs["steps"]}', flush=True)

    trainer = GeneratedTrainer()
    trainer.train(args)
    frozen()
    for role, model in models.items():
        bases[role]['after_sha256'] = state_hash(model.state_dict(), torch)
    if counts != {'vae': 4, 'encoders': [4, 4], 'noise': 4, 'starts': 4}:
        raise ValueError('training_loop_compute_count_changed')
    initial['after_sha256'] = checkpoints[-1]['weights_sha256']
    device['peak_allocated_bytes'] = torch.cuda.max_memory_allocated(0)
    return validate_report(report(device, bases, initial, losses, checkpoints, examples))
