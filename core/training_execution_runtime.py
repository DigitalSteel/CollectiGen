"""Owned SDXL training loop over an immutable, explicitly selected package.

This module runs only inside the qualified worker. Launch authority and result
registration belong to the parent controller. Progress is a bounded observation;
the unchanged continuation hooks retain and verify complete recovery state.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
from pathlib import Path

from . import training_continuation_execution as execution
from .training_configuration import TrainingConfiguration
from .training_continuation_hooks import Runtime as HookRuntime
from .training_execution_dataset import DatasetAudit, package_pairs
from .training_package_effects import dataset_proposal
from .training_process import _bytes, _read, _write_new
from .training_progress import TrainingProgressWriter


def previous_selection(contract):
    previous = contract['continuation']['previous']
    if previous is None:
        return None
    selection = {key: deepcopy(previous[key]) for key in ('destination', 'binding', 'anchor_sha256', 'sequence')}
    row = execution.anchored_checkpoint(**selection, contract=contract['continuation']['contract'])
    if (any(row[key] != previous[key] for key in ('name', 'step', 'epoch'))
            or any(row['proof'][key] != previous[key] for key in
                ('checkpoint_receipt_sha256', 'continuation_receipt_sha256'))):
        raise ValueError('training_execution_previous_checkpoint_changed')
    return selection


def dataset_identity(package, manifest):
    """Bind ordered package membership and the explicit dataset configuration."""
    pairs = package_pairs(package, manifest)
    configuration = TrainingConfiguration.from_dict(manifest['training_configuration'])
    return execution.digest({'profile': 'sdxl-execution-package-order-v1',
        'configuration': configuration.to_dict(),
        'items': [{'image': row['item']['image_path'],
                   'caption': row['item']['caption_path']} for row in pairs.values()]})


def prepare_arguments(entry, work, contract, manifest, toml):
    """Use the real parser defaults plus the exact resolved execution settings."""
    package = Path(contract['package']['path'])
    pairs = package_pairs(package, manifest)
    directories = {Path(name).parent for name in pairs}
    if len(directories) != 1:
        raise ValueError('training_execution_dataset_layout_unsupported')
    configuration = TrainingConfiguration.from_dict(manifest['training_configuration'])
    configuration.require_single_checkpoint_adapter()
    dataset = dataset_proposal(next(iter(directories)), configuration)
    dataset_path = Path(work) / 'training-dataset.toml'
    _write_new(dataset_path, toml.dumps(dataset).encode('utf-8'))
    args = entry.setup_parser().parse_args([])
    # Preview epochs and save_state=False are not executable settings. The
    # reviewed continuation schedule supplies both the step limit and saves.
    for key, value in contract['continuation']['contract']['settings'].items():
        setattr(args, key, deepcopy(value))
    args.dataset_config = str(dataset_path)
    args.pretrained_model_name_or_path = contract['resources']['base']['path']
    args.output_dir = str(work)
    args.output_name = contract['recipe']['configuration']['output_name']
    args.save_model_as = 'safetensors'
    args.save_precision = contract['recipe']['configuration']['save_precision']
    args.no_half_vae, args.sdpa, args.save_state = True, True, True
    previous = previous_selection(contract)
    args.resume = None
    if previous:
        row = execution.anchored_checkpoint(**previous, contract=contract['continuation']['contract'])
        args.resume = str(Path(previous['destination']) / 'work' / row['name'])
    return args


class Runtime(HookRuntime):
    def __init__(self, work, binding, contract, manifest, torch, np, Image, *, should_cancel=None):
        from .training_execution_plan import validate_contract_structure
        validate_contract_structure(contract)
        self.execution_contract = deepcopy(contract)
        self.manifest = deepcopy(manifest)
        self.package = Path(contract['package']['path'])
        resolved = contract['continuation']['contract']
        if (dataset_identity(self.package, manifest) != resolved['dataset_order_sha256']
                or execution.digest(contract) != binding['preview_sha256']):
            raise ValueError('training_execution_package_binding_changed')
        super().__init__(work, binding, resolved, torch, np,
            previous=previous_selection(contract), should_cancel=should_cancel)
        self.Image = Image
        self.progress = TrainingProgressWriter(work, binding['process_sha256'],
            binding['preview_sha256'], resolved['settings']['max_train_steps'])
        self.progress.publish('starting', 0, 0)
        self.completed_steps, self.loss, self.last_checkpoint = 0, None, None
        self.output = None
        self.models = {}

    def dataloader(self, torch, args, accelerator, dataset, **kwargs):
        audit = DatasetAudit(dataset, self.package, self.manifest, torch, self.np, self.Image)
        return super().dataloader(torch, args, accelerator, audit, **kwargs)

    def after_load(self, accelerator, network, optimizer, scheduler):
        super().after_load(accelerator, network, optimizer, scheduler)
        self.completed_steps = self.last_step
        original = optimizer.step
        def observed_step(*args, **kwargs):
            result = original(*args, **kwargs)
            if accelerator.sync_gradients:
                if accelerator.optimizer_step_was_skipped:
                    raise ValueError('training_execution_optimizer_step_skipped')
                self.completed_steps += 1
                self.publish('running')
            return result
        optimizer.step = observed_step

    def publish(self, state):
        sequence = self.sequence - 1 if self.sequence else None
        return self.progress.publish(state, self.completed_steps, self.sampler.epoch + 1,
            loss=self.loss, checkpoint_sequence=sequence,
            checkpoint_tip=self.tip if sequence is not None else None)

    def save_state(self, args, accelerator, step):
        if self.completed_steps != step:
            raise ValueError('training_execution_optimizer_position_changed')
        self.last_checkpoint = super().save_state(args, accelerator, step)
        self.publish('checkpoint')
        return self.last_checkpoint

    def finish(self):
        binding = self.execution_contract['result_binding']
        if (self.output is None or self.last_checkpoint is None
                or self.completed_steps != binding['expected_final_step']
                or self.sequence - 1 != binding['expected_checkpoint_sequence']):
            raise ValueError('training_execution_final_result_missing')
        rows = self.journal.read(self.sequence - 1, self.tip)
        row = rows[-1]
        if (self.current() != row['state'] or row['step'] != self.completed_steps
                or row['epoch'] != binding['expected_final_epoch']):
            raise ValueError('training_execution_final_state_changed')
        progress_tip = self.publish('completed')
        result = {'schema_version': 1, 'kind': 'sdxl_training_result', 'status': 'completed',
            'process_fingerprint': self.binding['process_sha256'],
            'execution_contract_sha256': self.binding['preview_sha256'],
            'output_relative_path': binding['output_relative_path'],
            'output_sha256': self.output['sha256'], 'output_bytes': self.output['bytes'],
            'final_step': row['step'], 'final_epoch': row['epoch'],
            'checkpoint_sequence': self.sequence - 1, 'checkpoint_tip': self.tip,
            'checkpoint_name': row['name'], **row['proof'],
            'progress_tip': progress_tip, 'model_state_sha256': row['state']['model']}
        _write_new(self.work / 'training-result.json', _bytes(result))
        return result


def run(entry, model_module, strategy_module, safetensors, toml, runtime,
        selected_tokenizers, load_models):
    """Load explicit verified components and train only ordinary UNet LoRA."""
    from .training_loop_contract import _strategy, require_checkpointing
    contract, torch = runtime.execution_contract, runtime.torch
    config = TrainingConfiguration.from_dict(runtime.manifest['training_configuration'])
    args = prepare_arguments(entry, runtime.work, contract, runtime.manifest, toml)
    strategy = _strategy(strategy_module.SdxlTokenizeStrategy, selected_tokenizers, config.max_caption_tokens)
    dtype = {'bf16': torch.bfloat16, 'fp16': torch.float16}[args.mixed_precision]
    save_dtype = {'bf16': torch.bfloat16, 'fp16': torch.float16}[args.save_precision]
    expected_output = contract['result_binding']['tensor_specs']

    class Trainer(entry.SdxlNetworkTrainer):
        def get_tokenize_strategy(self, args): return strategy

        def load_target_model(self, args, weight_dtype, accelerator):
            runtime.models.update(load_models())
            if set(runtime.models) != {'encoder_1', 'encoder_2', 'vae', 'unet'}:
                raise ValueError('training_execution_components_changed')
            for role, model in runtime.models.items():
                model.to(dtype=torch.float32 if role == 'vae' else dtype)
                model.requires_grad_(False)
            runtime.models['unet'].set_use_sdpa(True)
            self.load_stable_diffusion_format, self.logit_scale, self.ckpt_info = True, None, None
            return model_module.MODEL_VERSION_SDXL_BASE_V1_0, [runtime.models['encoder_1'],
                runtime.models['encoder_2']], runtime.models['vae'], runtime.models['unet']

        def on_step_start(self, args, accelerator, network, text_encoders, unet, batch, weight_dtype, is_train=True):
            super().on_step_start(args, accelerator, network, text_encoders, unet, batch, weight_dtype, is_train)
            require_checkpointing(accelerator.unwrap_model(unet))
            unwrapped = accelerator.unwrap_model(network)
            if (unwrapped.text_encoder_loras or len(unwrapped.unet_loras) != 722
                    or len(list(unwrapped.parameters())) != 1444
                    or any(p.dtype != torch.float32 or not p.requires_grad for p in unwrapped.parameters())
                    or any(p.requires_grad or p.grad is not None for model in runtime.models.values() for p in model.parameters())):
                raise ValueError('training_execution_trainable_scope_changed')

        def encode_images_to_latents(self, *args):
            value = super().encode_images_to_latents(*args)
            if not torch.isfinite(value).all().item():
                raise ValueError('training_execution_nonfinite_latents')
            return value

        def get_noise_pred_and_target(self, *args, **kwargs):
            value = super().get_noise_pred_and_target(*args, **kwargs)
            if any(not torch.isfinite(t).all().item() for t in value[:2]):
                raise ValueError('training_execution_nonfinite_noise')
            return value

        def process_batch(self, *args, **kwargs):
            value = super().process_batch(*args, **kwargs)
            runtime.loss = float(value.detach().item())
            if not math.isfinite(runtime.loss) or runtime.loss < 0:
                raise ValueError('training_execution_nonfinite_loss')
            return value

        def _save_model(self, **kwargs):
            if kwargs['ckpt_name'] != args.output_name + '.safetensors':
                return
            if runtime.output is not None:
                raise ValueError('training_execution_output_repeated')
            metadata = {**contract['result_binding']['required_metadata'],
                'collectigen_execution_contract_sha256': runtime.binding['preview_sha256']}
            self._metadata.update(metadata)
            self._minimum_metadata.update(metadata)
            super()._save_model(**kwargs)
            raw = _read(runtime.work / kwargs['ckpt_name'], limit=512 * 1024**2)
            saved = safetensors.load(raw)
            expected = kwargs['unwrapped_nw'].state_dict()
            if set(saved) != set(expected) or set(saved) != set(expected_output):
                raise ValueError('training_execution_output_layout_changed')
            for name, tensor in saved.items():
                spec = expected_output[name]
                if (tensor.dtype != save_dtype or list(tensor.shape) != spec['shape']
                        or spec['dtype'] != ('F16' if save_dtype == torch.float16 else 'BF16')
                        or not torch.isfinite(tensor).all().item()
                        or not torch.equal(tensor, expected[name].detach().to(device='cpu', dtype=save_dtype))):
                    raise ValueError('training_execution_output_readback_failed')
            runtime.output = {'sha256': hashlib.sha256(raw).hexdigest(), 'bytes': len(raw)}

    Trainer().train(args)
    return runtime.finish()
