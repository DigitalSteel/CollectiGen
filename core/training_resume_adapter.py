"""Explicit, generated-only resume correction for one pinned sd-scripts revision.

This module is not wired to application admission. It changes only the owned
process's source copy and uses the existing trainer's compute and state loader.
"""
from __future__ import annotations

import hashlib
import math
import random

PROFILE = 'sd-scripts-6721028-generated-resume-v1'
REVISION = '6721028c79ee85a78b3a06dfd8954dae310a1cce'
SOURCE_HASHES = ('0ea59d6da63b67c84a7d3a929af1b19ffe8893629416b4b743b57b33c85d2ffe',
    'b0af28bd9b7cf28be52942fdece819579f659fcb880217fe72d1ae229cdea6fd')


def position(saved, batches, accumulation, maximum):
    if (any(type(n) is not int for n in (saved, batches, accumulation, maximum))
            or not 0 <= saved < maximum <= 1_000_000_000
            or not 1 <= batches <= 1_000_000 or not 1 <= accumulation <= 1024):
        raise ValueError('resume_position_invalid')
    per_epoch = math.ceil(batches / accumulation)
    epoch, within = divmod(saved, per_epoch)
    return epoch, within * accumulation, saved


def patch_source(raw, catalog):
    """Hash admission precedes exact single-occurrence source replacements."""
    if (catalog.get('source_revision') != REVISION or hashlib.sha256(raw).hexdigest() not in SOURCE_HASHES
            or hashlib.sha256(raw).hexdigest() not in catalog['files']['train_network.py']):
        raise ValueError('resume_source_changed')
    source = raw.decode('utf-8').replace('\r\n', '\n')
    start = source.index('        # calculate steps to skip when resuming or starting from a specific step\n')
    end = source.index('        noise_scheduler = self.get_noise_scheduler', start)
    source = source[:start] + '''        epoch_to_start, resume_batches, global_step = _collectigen_resume.position(
            steps_from_state or 0, len(train_dataloader), args.gradient_accumulation_steps, args.max_train_steps)
        initial_step = 0

''' + source[end:]
    start = source.index('        if initial_step > 0:  # only if skip_until_initial_step is specified\n')
    end = source.index('        # log device and dtype for each model', start)
    source = source[:start] + source[end:]
    replacements = (
        ('        train_dataloader = torch.utils.data.DataLoader(\n',
         '        train_dataloader = _collectigen_resume.dataloader(torch, args, accelerator,\n'),
        ('"current_step": current_step.value + 1', '"current_step": global_step'),
        ('        args_util.resume_from_local_or_hf_if_specified(accelerator, args)\n',
         '        args_util.resume_from_local_or_hf_if_specified(accelerator, args)\n'
         '        _collectigen_resume.after_load(accelerator, network, optimizer, lr_scheduler)\n'),
        ('            current_epoch.value = epoch + 1\n',
         '            current_epoch.value = epoch + 1\n'
         '            _collectigen_resume.begin_epoch(epoch, resume_batches if epoch == epoch_to_start else 0)\n'),
        ('range(args.max_train_steps - initial_step)', 'range(args.max_train_steps - global_step)'),
        ('                                checkpoint_io.save_and_remove_state_stepwise(args, accelerator, global_step)',
         '                                _collectigen_resume.save_state(args, accelerator, global_step)'),
    )
    for before, after in replacements:
        if source.count(before) != 1:
            raise ValueError('resume_source_anchor_changed')
        source = source.replace(before, after)
    return source


class EpochSampler:
    """Exact epoch permutation and batch offset; never consume training RNG."""
    def __init__(self, size, seed):
        if type(size) is not int or not 1 <= size <= 1_000_000 or type(seed) is not int:
            raise ValueError('resume_sampler_invalid')
        self.size, self.seed, self.epoch, self.skip = size, seed, 0, 0

    def __len__(self):
        # Planning always sees the full epoch, including when continuing it.
        return self.size

    def select(self, epoch, skip):
        if type(epoch) is not int or epoch < 0 or type(skip) is not int or not 0 <= skip < self.size:
            raise ValueError('resume_sampler_position_invalid')
        self.epoch, self.skip = epoch, skip

    def __iter__(self):
        indices = list(range(self.size))
        random.Random(self.seed + self.epoch).shuffle(indices)
        return iter(indices[self.skip:])


class Runtime:
    position = staticmethod(position)

    def __init__(self, after_load, save_state):
        self.after_load, self.save_state = after_load, save_state
        self.dataset = self.sampler = self.generator = None

    def dataloader(self, torch, args, accelerator, dataset, **kwargs):
        if (accelerator.num_processes != 1 or args.seed is None
                or kwargs.get('num_workers') != 0 or kwargs.get('persistent_workers')
                or kwargs.get('batch_size') != 1 or kwargs.get('shuffle') is not True
                or args.initial_step is not None or args.initial_epoch is not None
                or args.skip_until_initial_step or args.max_train_epochs is not None):
            raise ValueError('resume_runtime_scope_invalid')
        self.dataset = dataset
        self.sampler = EpochSampler(len(dataset), args.seed)
        self.generator = torch.Generator(device='cpu')
        kwargs.update(shuffle=False, sampler=self.sampler, generator=self.generator)
        return torch.utils.data.DataLoader(dataset, **kwargs)

    def begin_epoch(self, epoch, skip):
        if self.dataset is None:
            raise ValueError('resume_loader_missing')
        self.sampler.select(epoch, skip)
        # The pinned dataset reconstructs cumulative bucket permutations. Do it
        # before __getitem__, not in its late collator; preserve training RNG.
        state = random.getstate()
        try:
            self.dataset.set_current_epoch(epoch + 1)
        finally:
            random.setstate(state)
        self.generator.manual_seed(self.sampler.seed + epoch)
