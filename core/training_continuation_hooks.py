"""Contained local trainer hooks; application launch remains a separate gate."""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
import os
from pathlib import Path
import sys

from . import training_checkpoint as checkpoint
from . import training_continuation as continuation
from . import training_continuation_execution as execution
from .training_process import _bytes
from .training_resume_adapter import Runtime as QualifiedRuntime


def prepare_environment():
    """Call in a fresh owned worker before importing Torch or trainer modules."""
    if 'torch' in sys.modules:
        raise checkpoint.CheckpointError('continuation_torch_imported_early')
    os.environ['CUBLAS_WORKSPACE_CONFIG'] = ':4096:8'


def configure_torch(torch):
    if os.environ.get('CUBLAS_WORKSPACE_CONFIG') != ':4096:8' or torch.cuda.is_initialized():
        raise checkpoint.CheckpointError('continuation_cuda_initialized_early')
    torch.set_num_threads(2)
    torch.backends.cuda.matmul.allow_tf32 = False
    torch.backends.cudnn.allow_tf32 = False
    torch.backends.cudnn.benchmark = False
    torch.backends.cudnn.deterministic = True
    torch.use_deterministic_algorithms(True)
    check_numerical_policy(torch)


def check_numerical_policy(torch):
    if (os.environ.get('CUBLAS_WORKSPACE_CONFIG') != ':4096:8' or torch.get_num_threads() != 2
            or torch.backends.cuda.matmul.allow_tf32 is not False or torch.backends.cudnn.allow_tf32 is not False
            or torch.backends.cudnn.benchmark is not False or torch.backends.cudnn.deterministic is not True
            or torch.are_deterministic_algorithms_enabled() is not True):
        raise checkpoint.CheckpointError('continuation_numerical_policy_changed')


def snapshot(accelerator, network, optimizer, scheduler, torch, np, should_cancel=None):
    """Bounded named state fingerprints; never persist values or deserialize data."""
    budget = {'bytes': 0, 'nodes': 0}
    def normalized(value, depth=0):
        checkpoint._cancel(should_cancel)
        budget['nodes'] += 1
        if budget['nodes'] > 250_000 or depth > 32:
            raise checkpoint.CheckpointError('continuation_state_limit')
        if torch.is_tensor(value):
            size = value.numel() * value.element_size()
            budget['bytes'] += size
            if size > 256 * 1024**2 or budget['bytes'] > 2 * 1024**3:
                raise checkpoint.CheckpointError('continuation_state_limit')
            if (value.is_floating_point() or value.is_complex()) and not torch.isfinite(value).all().item():
                raise checkpoint.CheckpointError('continuation_nonfinite_state')
            raw = value.detach().cpu().contiguous().reshape(-1).view(torch.uint8).numpy().tobytes()
            return {'type': 'torch.Tensor', 'dtype': str(value.dtype), 'device': str(value.device), 'shape': list(value.shape),
                'stride': list(value.stride()), 'sha256': hashlib.sha256(raw).hexdigest()}
        if isinstance(value, np.ndarray):
            budget['bytes'] += value.nbytes
            if value.nbytes > 16 * 1024**2 or budget['bytes'] > 2 * 1024**3 or value.dtype.hasobject:
                raise checkpoint.CheckpointError('continuation_state_limit')
            return {'type': 'numpy.ndarray', 'dtype': str(value.dtype), 'shape': list(value.shape), 'sha256': hashlib.sha256(value.tobytes()).hexdigest()}
        if isinstance(value, dict):
            if any(type(key) not in (str, int) for key in value):
                raise checkpoint.CheckpointError('continuation_state_keys_invalid')
            pairs = [[normalized(key, depth + 1), normalized(item, depth + 1)] for key, item in value.items()]
            return {'type': type(value).__module__ + '.' + type(value).__qualname__, 'items': sorted(pairs, key=lambda item: _bytes(item[0]))}
        if type(value) in (tuple, list):
            return {'type': type(value).__name__, 'items': [normalized(item, depth + 1) for item in value]}
        if type(value) not in (str, int, float, bool, type(None)) or (type(value) is float and not math.isfinite(value)):
            raise checkpoint.CheckpointError('continuation_state_scalar_invalid')
        return {'type': type(value).__name__, 'value': value}
    unwrapped = accelerator.unwrap_model(network)
    names, seen, groups = {id(p): name for name, p in unwrapped.named_parameters()}, set(), []
    for group in optimizer.param_groups:
        rows = []
        for parameter in group['params']:
            identity = id(parameter)
            if identity not in names or identity in seen:
                raise checkpoint.CheckpointError('continuation_optimizer_binding_changed')
            seen.add(identity)
            rows.append({'name': names[identity], 'dtype': str(parameter.dtype), 'device': str(parameter.device),
                'shape': list(parameter.shape), 'stride': list(parameter.stride())})
        groups.append(rows)
    import random
    values = {'model': unwrapped.state_dict(), 'optimizer': optimizer.state_dict(), 'optimizer_binding': groups,
        'scheduler': scheduler.state_dict(), 'python_rng': random.getstate(), 'numpy_rng': np.random.get_state(),
        'torch_rng': torch.get_rng_state(), 'cuda_rng': torch.cuda.get_rng_state_all() if torch.cuda.is_available() else [],
        'accelerator_step': accelerator.step}
    return {key: execution.digest(normalized(value)) for key, value in values.items()}


class Runtime(QualifiedRuntime):
    """Callbacks for the unchanged qualified sd-scripts patch, inside its process."""
    def __init__(self, work, binding, contract, torch, np, *, previous=None, should_cancel=None):
        super().__init__(self.after_load, self.save_state)
        self.work = Path(work).absolute()
        checkpoint._paths(self.work, 'validation-state')
        self.binding = deepcopy(checkpoint._binding(binding))
        self.contract = deepcopy(execution.validate_contract(contract))
        self.torch, self.np, self.cancel = torch, np, should_cancel
        self.previous = deepcopy(previous)
        if previous is not None:
            if (type(previous) is not dict or set(previous) != {'destination', 'binding', 'anchor_sha256', 'sequence'}
                    or type(previous['destination']) is not str or not Path(previous['destination']).is_absolute()
                    or previous['binding']['run_id'] == binding['run_id']):
                raise checkpoint.CheckpointError('continuation_previous_run_invalid')
            checkpoint._binding(previous['binding'])
        self.journal = execution.CheckpointJournal(self.work.parent, self.binding, self.contract, create=True)
        self.sequence, self.tip = 0, execution.digest(self.journal.header)
        self.args = self.accelerator = self.context = None
        self.load_calls, self.load_checked, self.ready = 0, False, False
        self.expected = None
        self.last_step = 0
        self.next_cursor = (0, 0)

    def _arguments(self, args, accelerator):
        try:
            current = {key: getattr(args, key) for key in execution.FIXED.keys() | execution.VARIABLE}
        except AttributeError:
            raise checkpoint.CheckpointError('continuation_arguments_missing') from None
        if _bytes(current) != _bytes(self.contract['settings']):
            raise checkpoint.CheckpointError('continuation_arguments_changed')
        if getattr(args, 'save_state', None) is not True:
            raise checkpoint.CheckpointError('continuation_state_saving_disabled')
        if (type(accelerator.num_processes) is not int or accelerator.num_processes != 1
                or getattr(accelerator.distributed_type, 'value', accelerator.distributed_type) != 'NO'
                or accelerator.mixed_precision != current['mixed_precision']
                or accelerator.gradient_accumulation_steps != current['gradient_accumulation_steps']):
            raise checkpoint.CheckpointError('continuation_accelerator_changed')
        check_numerical_policy(self.torch)
        # This hook accepts only the explicitly anchored local state. Acquisition
        # is a separate operation; it never invokes an implicit remote resume.
        if getattr(args, 'resume_from_huggingface', False) is not False:
            raise checkpoint.CheckpointError('continuation_resume_selection_changed')
        selected = getattr(args, 'resume', None)
        expected = None
        if self.previous:
            row = execution.anchored_checkpoint(**self.previous, contract=self.contract)
            expected = str(Path(self.previous['destination']) / 'work' / row['name'])
        if selected != expected:
            raise checkpoint.CheckpointError('continuation_resume_selection_changed')

    def dataloader(self, torch, args, accelerator, dataset, **kwargs):
        if self.args is not None or torch is not self.torch:
            raise checkpoint.CheckpointError('continuation_loader_repeated')
        self._arguments(args, accelerator)
        loader = super().dataloader(torch, args, accelerator, dataset, **kwargs)
        if len(loader) != self.contract['epoch_batches']:
            raise checkpoint.CheckpointError('continuation_loader_length_changed')
        self.args, self.accelerator = args, accelerator
        self.load_handle = accelerator.register_load_state_pre_hook(self.before_load)
        return loader

    def before_load(self, models, folder):
        self.load_calls += 1
        if self.load_calls != 1 or not self.previous or self.ready:
            raise checkpoint.CheckpointError('continuation_unexpected_load')
        self._arguments(self.args, self.accelerator)
        row = execution.anchored_checkpoint(**self.previous, contract=self.contract)
        work = Path(self.previous['destination']) / 'work'
        if Path(folder).absolute() != work / row['name']:
            raise checkpoint.CheckpointError('continuation_load_folder_changed')
        continuation.inspect_checkpoint(work, row['name'], self.previous['binding'],
            self.contract['prerequisite'], **row['proof'], should_cancel=self.cancel)
        self.expected, self.load_checked = row, True

    def current(self):
        if self.context is None:
            raise checkpoint.CheckpointError('continuation_context_missing')
        return snapshot(*self.context, self.torch, self.np, self.cancel)

    def after_load(self, accelerator, network, optimizer, scheduler):
        if self.ready or self.args is None or accelerator is not self.accelerator:
            raise checkpoint.CheckpointError('continuation_callback_order_changed')
        self._arguments(self.args, accelerator)
        if (self.previous and (self.load_calls != 1 or not self.load_checked)) or (not self.previous and self.load_calls != 0):
            raise checkpoint.CheckpointError('continuation_load_hook_missing')
        self.context = (accelerator, network, optimizer, scheduler)
        if self.previous:
            row = execution.anchored_checkpoint(**self.previous, contract=self.contract)
            if row != self.expected or self.current() != row['state']:
                raise checkpoint.CheckpointError('continuation_restored_state_changed')
            self.last_step = row['step']
            self.next_cursor = execution.saved_position(self.contract, row['step'], row['epoch'], resume=True)[:2]
        self.ready = True

    def begin_epoch(self, epoch, skip):
        if not self.ready:
            raise checkpoint.CheckpointError('continuation_fetch_before_load_check')
        if type(epoch) is not int or type(skip) is not int or (epoch, skip) != self.next_cursor:
            raise checkpoint.CheckpointError('continuation_epoch_cursor_changed')
        result = super().begin_epoch(epoch, skip)
        self.next_cursor = (epoch + 1, 0)
        return result

    def save_state(self, args, accelerator, step):
        if not self.ready or args is not self.args or accelerator is not self.accelerator or accelerator.sync_gradients is not True:
            raise checkpoint.CheckpointError('continuation_save_boundary_invalid')
        self._arguments(args, accelerator)
        settings = self.contract['settings']
        expected_step = min((self.last_step // settings['save_every_n_steps'] + 1) * settings['save_every_n_steps'], settings['max_train_steps'])
        if type(step) is not int or step != expected_step or step <= self.last_step:
            raise checkpoint.CheckpointError('continuation_save_step_changed')
        epoch = math.ceil(step / math.ceil(self.contract['epoch_batches'] / settings['gradient_accumulation_steps']))
        execution.saved_position(self.contract, step, epoch)
        if self.next_cursor != (epoch, 0) or self.sampler.epoch + 1 != epoch:
            raise checkpoint.CheckpointError('continuation_save_epoch_changed')
        name = f'training-step{step:08d}-state'
        checkpoint._cancel(self.cancel)
        (self.work / name).mkdir(exist_ok=False)
        before = self.current()
        accelerator.save_state(str(self.work / name))
        if self.current() != before:
            raise checkpoint.CheckpointError('continuation_save_changed_state')
        checkpoint._cancel(self.cancel)
        completion = checkpoint.record_save_completion(self.work, name, self.binding, step=step, epoch=epoch)
        proof = continuation.seal_checkpoint(self.work, name, self.binding, self.contract['prerequisite'],
            step=step, epoch=epoch, completion_sha256=completion, should_cancel=self.cancel)
        self.tip = self.journal.append(name, step, epoch, proof, before, self.sequence, self.tip)
        self.sequence += 1
        self.last_step = step
        if step == settings['max_train_steps']:
            args.save_state = False
        return {'name': name, 'step': step, 'epoch': epoch, 'proof': proof, 'journal_tip': self.tip}
