"""Full generated SDXL continuation experiment; never an app launch path."""
from __future__ import annotations

import hashlib
import json
import math
import random
from pathlib import Path

from . import training_loop_contract as loop
from . import training_checkpoint as checkpoint
from .training_model_contract import canonical
from .training_unet_step_contract import state_hash
from .training_resume_adapter import PROFILE

CAPTIONS = ('generated red square', 'generated blue circle',
    'generated green triangle', 'generated yellow diamond')
COLORS = ((192, 32, 16), (16, 32, 192), (32, 192, 16), (192, 192, 16))


def parameter_layout(parameter):
    return {'dtype': str(parameter.dtype), 'device': str(parameter.device),
        'shape': list(parameter.shape), 'stride': list(parameter.stride())}


def optimizer_binding(network, optimizer):
    names = {id(parameter): name for name, parameter in network.named_parameters()}
    groups, seen = [], set()
    for group in optimizer.param_groups:
        rows = []
        for parameter in group['params']:
            identity = id(parameter)
            if identity not in names or identity in seen:
                raise ValueError('resume_optimizer_binding_invalid')
            seen.add(identity)
            rows.append({'name': names[identity], **parameter_layout(parameter)})
        groups.append(rows)
    return groups


def gradient_state(accelerator, network, torch):
    gradients, layouts = {}, {}
    for name, parameter in accelerator.unwrap_model(network).named_parameters():
        gradient = parameter.grad
        layouts[name] = None if gradient is None else parameter_layout(gradient)
        if gradient is not None:
            if not torch.isfinite(gradient).all().item():
                raise ValueError('resume_gradient_invalid')
            gradients[name] = gradient
    return {'values': state_hash(gradients, torch), 'present': len(gradients),
        'layout': hashlib.sha256(canonical(layouts)).hexdigest(),
        'sync': accelerator.sync_gradients}


def snapshot(accelerator, network, optimizer, scheduler, torch, np):
    def normalized(value):
        if torch.is_tensor(value):
            tensor = value.detach().cpu().contiguous()
            return {**parameter_layout(value),
                'sha256': hashlib.sha256(tensor.reshape(-1).view(torch.uint8).numpy().tobytes()).hexdigest()}
        if isinstance(value, np.ndarray):
            return {'dtype': str(value.dtype), 'shape': list(value.shape),
                'sha256': hashlib.sha256(value.tobytes()).hexdigest()}
        if isinstance(value, dict):
            return {str(key): normalized(item) for key, item in value.items()}
        if isinstance(value, (tuple, list)):
            return [normalized(item) for item in value]
        return value
    values = {'model': accelerator.unwrap_model(network).state_dict(),
        'optimizer_binding': optimizer_binding(accelerator.unwrap_model(network), optimizer),
        'optimizer': optimizer.state_dict(), 'scheduler': scheduler.state_dict(),
        'python_rng': random.getstate(), 'numpy_rng': np.random.get_state(),
        'torch_rng': torch.get_rng_state(), 'cuda_rng': torch.cuda.get_rng_state_all() if torch.cuda.is_available() else [],
        'accelerator_step': accelerator.step}
    return {name: hashlib.sha256(canonical(normalized(value))).hexdigest() for name, value in values.items()}


def run(entry, model_module, strategy_module, transformers, torch, np, image_module,
        toml, catalog, work, runtime, request):
    loop.checked_catalog(catalog)
    loop.resource_gate(work)
    device = loop.device_gate(torch)
    torch.set_num_threads(2)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.use_deterministic_algorithms(True)
    torch.cuda.reset_peak_memory_stats(0)
    args = loop.generated_arguments(entry, image_module, toml, work)
    # Rich hyperlink IDs consume Python's global RNG, including after the
    # checkpoint captures it. Use the trainer's own plain logging option.
    args.console_log_simple = True
    for index in (2, 3):
        image_module.new('RGB', (64, 64), COLORS[index]).save(work / 'generated-images' / f'{index}.png')
        (work / 'generated-images' / f'{index}.txt').write_text(CAPTIONS[index], encoding='utf-8')
    args.max_train_steps = 3
    args.save_state = True
    if request.get('previous'):
        args.resume = str(Path(request['previous']) / request['checkpoint'])
    tokenizers, _ = loop._write_tokenizers(transformers.CLIPTokenizer, work)
    strategy = loop._strategy(strategy_module.SdxlTokenizeStrategy, tokenizers, 75)
    models, context, saves, batches, weights = {}, {}, {}, [], []
    result = {'profile': PROFILE, 'status': 'held', 'execution_ready': False,
        'selected_training_started': False, 'learned_resource_compatibility_verified': False,
        'device': device, 'configuration': {'images': 4, 'accumulation': 2, 'maximum_steps': 3,
            'seed': 42, 'workers': 0, 'random_augmentation': False,
            'console_log_simple': True, 'deterministic_algorithms': True,
            'cublas_workspace_config': ':4096:8', 'cudnn_benchmark': torch.backends.cudnn.benchmark,
            'cudnn_deterministic': torch.backends.cudnn.deterministic}, 'batches': batches, 'saves': saves}

    def current():
        return snapshot(context['accelerator'], context['network'], context['optimizer'],
            context['scheduler'], torch, np)

    def after_load(accelerator, network, optimizer, scheduler):
        context.update(accelerator=accelerator, network=network, optimizer=optimizer, scheduler=scheduler)
        original_backward, original_step = accelerator.backward, optimizer.step
        def observed_backward(*args, **kwargs):
            value = original_backward(*args, **kwargs)
            batches[-1]['gradients'] = gradient_state(accelerator, network, torch)
            return value
        def observed_step(*args, **kwargs):
            batches[-1]['update_gradients'] = gradient_state(accelerator, network, torch)
            return original_step(*args, **kwargs)
        accelerator.backward, optimizer.step = observed_backward, observed_step
        result['base_hashes'] = {role: state_hash(model.state_dict(), torch) for role, model in models.items()}
        if request.get('previous'):
            result['restored'] = current()
            if (result['restored'] != request['expected']['state']
                    or result['base_hashes'] != request['base_hashes']):
                raise ValueError('resume_loaded_state_changed')

    def save_state(args, accelerator, step):
        name = f'generated-training-step{step:08d}-state'
        before = current()
        accelerator.save_state(str(work / name))
        after = current()
        if after != before:
            raise ValueError('resume_save_changed_' + '_'.join(key for key in before if before[key] != after[key]))
        epoch = math.ceil(step / 2)
        completion = checkpoint.record_save_completion(work, name, request['binding'], step=step, epoch=epoch)
        receipt = checkpoint.seal_checkpoint(work, name, request['binding'], step=step, epoch=epoch,
            completion_sha256=completion)
        saves[str(step)] = {'name': name, 'state': before, 'receipt_sha256': receipt,
            'completion_sha256': completion, 'step': step, 'epoch': epoch}
        if step == args.max_train_steps:
            # Already retained the exact final optimizer state; the generated
            # diagnostic does not request the trainer's duplicate final folder.
            args.save_state = False
        print(f'generated resume: retained state at step {step}', flush=True)

    runtime.after_load, runtime.save_state = after_load, save_state

    class GeneratedTrainer(entry.SdxlNetworkTrainer):
        def get_tokenize_strategy(self, args):
            return strategy

        def load_target_model(self, args, weight_dtype, accelerator):
            models.update(loop._generated_models(model_module, torch, catalog))
            self.load_stable_diffusion_format, self.logit_scale, self.ckpt_info = True, None, None
            return model_module.MODEL_VERSION_SDXL_BASE_V1_0, [models['encoder_1'], models['encoder_2']], models['vae'], models['unet']

        def on_step_start(self, args, accelerator, network, text_encoders, unet, batch, weight_dtype, is_train=True):
            super().on_step_start(args, accelerator, network, text_encoders, unet, batch, weight_dtype, is_train)
            loop.require_checkpointing(accelerator.unwrap_model(unet))
            index = CAPTIONS.index(batch['captions'][0])
            expected = torch.tensor(COLORS[index], dtype=torch.float32).div(255).sub(.5).div(.5)
            expected = expected.reshape(1, 3, 1, 1).expand(1, 3, 64, 64)
            if batch['captions'] != [CAPTIONS[index]] or not torch.equal(batch['images'].cpu(), expected):
                raise ValueError('resume_image_caption_changed')
            batches.append({'example': index, 'before': current(),
                'epoch': runtime.sampler.epoch, 'accumulator_step': accelerator.step})

        def encode_images_to_latents(self, *args):
            value = super().encode_images_to_latents(*args)
            if not torch.isfinite(value).all().item():
                raise ValueError('resume_latents_invalid')
            batches[-1]['latents'] = state_hash({'latents': value}, torch)
            return value

        def get_noise_pred_and_target(self, *args, **kwargs):
            value = super().get_noise_pred_and_target(*args, **kwargs)
            if any(not torch.isfinite(t).all().item() for t in value[:2]):
                raise ValueError('resume_noise_invalid')
            batches[-1]['noise'] = state_hash({'prediction': value[0], 'target': value[1], 'timestep': value[2]}, torch)
            return value

        def process_batch(self, *args, **kwargs):
            value = super().process_batch(*args, **kwargs)
            loss = float(value.detach().item())
            if not math.isfinite(loss) or loss <= 0:
                raise ValueError('resume_loss_invalid')
            batches[-1]['loss'] = loss
            return value

        def _save_model(self, **kwargs):
            # State restoration is the tested boundary. Existing fresh-run
            # qualification owns weights-only output serialization/metadata.
            weights.append({'step': kwargs['steps'], 'hash': state_hash(kwargs['unwrapped_nw'].state_dict(), torch)})

    GeneratedTrainer().train(args)
    result['final_state'] = current()
    result['weights'] = weights
    if result['base_hashes'] != {role: state_hash(model.state_dict(), torch) for role, model in models.items()}:
        raise ValueError('resume_base_weights_changed')
    initial = request['expected']['step'] if request.get('previous') else 0
    if len(batches) != (3 - initial) * 2 or list(saves) != [str(i) for i in range(initial + 1, 4)]:
        raise ValueError('resume_progress_changed')
    device['peak_allocated_bytes'] = torch.cuda.max_memory_allocated(0)
    result['status'] = 'generated_resume_phase_passed'
    return result


def run_cpu(torch, np, work, runtime, request):
    """Real Accelerate loader/state control without SDXL or CUDA computation."""
    from accelerate import Accelerator
    from types import SimpleNamespace
    from library.utils import setup_logging
    setup_logging(SimpleNamespace(console_log_simple=True, console_log_file=None, console_log_level='INFO'), reset=True)
    torch.set_num_threads(2)
    random.seed(42)
    np.random.seed(42)
    torch.manual_seed(42)
    accelerator = Accelerator(cpu=True, gradient_accumulation_steps=2)

    fetches = []
    class Dataset(torch.utils.data.Dataset):
        def __init__(self):
            self.values, self.epoch = list(range(4)), 0
        def __len__(self):
            return 4
        def set_current_epoch(self, epoch):
            # Reproduce the public dataset's cumulative shuffle/global seed.
            while self.epoch < epoch:
                self.epoch += 1
                random.seed(42 + self.epoch)
                random.shuffle(self.values)
        def __getitem__(self, index):
            fetches.append({'epoch': self.epoch - 1, 'index': index, 'example': self.values[index]})
            return self.values[index]

    args = SimpleNamespace(seed=42, initial_step=None, initial_epoch=None,
        skip_until_initial_step=False, max_train_epochs=None)
    loader = runtime.dataloader(torch, args, accelerator, Dataset(), batch_size=1,
        shuffle=True, num_workers=0, persistent_workers=False)
    model = torch.nn.Linear(4, 2)
    optimizer = torch.optim.AdamW(model.parameters(), lr=.001, weight_decay=0)
    scheduler = torch.optim.lr_scheduler.StepLR(optimizer, step_size=1, gamma=.9)
    model, optimizer, loader, scheduler = accelerator.prepare(model, optimizer, loader, scheduler)
    current = lambda: snapshot(accelerator, model, optimizer, scheduler, torch, np)
    result = {'status': 'held', 'profile': PROFILE, 'configuration': {'cpu_order_control': True},
        'base_hashes': {}, 'saves': {}, 'batches': [], 'fetches': fetches, 'weights': [], 'execution_ready': False,
        'selected_training_started': False, 'full_sdxl_resume_verified': False}
    step = request['expected']['step'] if request.get('previous') else 0
    if request.get('previous'):
        accelerator.load_state(str(Path(request['previous']) / request['checkpoint']))
        result['restored'] = current()
        if result['restored'] != request['expected']['state']:
            raise ValueError('resume_loaded_state_changed')
    epoch_start, skip, _ = runtime.position(step, 4, 2, 3)
    def save_position(models, weights, folder):
        (Path(folder) / 'train_state.json').write_bytes(canonical({'current_step': step, 'current_epoch': epoch + 1}))
    accelerator.register_save_state_pre_hook(save_position)
    for epoch in range(epoch_start, 2):
        runtime.begin_epoch(epoch, skip if epoch == epoch_start else 0)
        for example in loader:
            with accelerator.accumulate(model):
                row = {'example': int(example.item()), 'epoch': epoch, 'before': current(),
                    'accumulator_step': accelerator.step}
                x = torch.randn(2, 4) * random.random() + float(np.random.random()) + example.item()
                loss = model(x).square().mean()
                row.update(noise=state_hash({'x': x}, torch), loss=float(loss.detach().item()))
                result['batches'].append(row)
                accelerator.backward(loss)
                optimizer.step()
                scheduler.step()
                optimizer.zero_grad(set_to_none=True)
            if accelerator.sync_gradients:
                step += 1
                name = f'generated-training-step{step:08d}-state'
                before = current()
                accelerator.save_state(str(work / name))
                after = current()
                if after != before:
                    raise ValueError('resume_save_changed_' + '_'.join(key for key in before if before[key] != after[key]))
                completion = checkpoint.record_save_completion(work, name, request['binding'], step=step, epoch=epoch + 1)
                receipt = checkpoint.seal_checkpoint(work, name, request['binding'], step=step, epoch=epoch + 1,
                    completion_sha256=completion)
                result['saves'][str(step)] = {'name': name, 'step': step, 'epoch': epoch + 1,
                    'state': before, 'receipt_sha256': receipt, 'completion_sha256': completion}
                result['weights'].append({'step': step, 'hash': before['model']})
            if step == 3:
                break
    result.update(status='generated_resume_phase_passed', final_state=current())
    return result
