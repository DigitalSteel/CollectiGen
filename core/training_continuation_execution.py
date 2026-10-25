"""Resolved continuation schedules and separately anchored checkpoint journals.

Standard library only. These records do not grant application launch authority.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import json
import math
import os
from pathlib import Path

from . import training_checkpoint as checkpoint
from . import training_continuation as continuation
from .training_process import TrainingProcessService, TrainingRunJournal, _bytes, _directory, _read, _write_new
from .training_resume_adapter import patch_source, position

PROFILE = 'sdxl-resolved-continuation-v1'
MAX_SAVES = 1000
STATE_ROLES = frozenset(('model', 'optimizer', 'optimizer_binding', 'scheduler',
    'python_rng', 'numpy_rng', 'torch_rng', 'cuda_rng', 'accelerator_step'))
FIXED = {'network_module': 'networks.lora', 'network_train_unet_only': True,
    'optimizer_type': 'AdamW', 'optimizer_args': ['weight_decay=0'], 'lr_scheduler': 'constant',
    'lr_scheduler_args': [], 'lr_warmup_steps': 0, 'lr_decay_steps': 0,
    'max_train_epochs': None, 'initial_step': None, 'initial_epoch': None,
    'skip_until_initial_step': False, 'max_data_loader_n_workers': 0,
    'persistent_data_loader_workers': False, 'console_log_simple': True,
    'save_state_on_train_end': False, 'save_every_n_epochs': None, 'save_n_epoch_ratio': None,
    'cache_latents': False, 'cache_latents_to_disk': False,
    'cache_text_encoder_outputs': False, 'cache_text_encoder_outputs_to_disk': False,
    'full_fp16': False, 'full_bf16': False, 'deepspeed': False}
VARIABLE = {'network_dim', 'network_alpha', 'learning_rate', 'mixed_precision',
    'gradient_checkpointing', 'max_train_steps', 'gradient_accumulation_steps',
    'seed', 'save_every_n_steps'}


def digest(value):
    return continuation.digest(value)


def implementation_identity():
    return {name: hashlib.sha256(_read(Path(__file__).with_name(name), limit=2 * 1024**2)).hexdigest()
        for name in ('training_continuation_execution.py', 'training_continuation_hooks.py',
                     'training_continuation.py', 'training_runtime_contract.py')}


def source_identity(raw, catalog, patched):
    expected = patch_source(raw, catalog).encode('utf-8')
    if patched != expected or hashlib.sha256(patched).hexdigest() != continuation.PATCH_SHA256:
        raise checkpoint.CheckpointError('continuation_patch_changed')
    return {'original_sha256': hashlib.sha256(raw).hexdigest(),
        'patched_sha256': hashlib.sha256(patched).hexdigest(), 'catalog_sha256': digest(catalog)}


def validate_settings(value):
    if type(value) is not dict or set(value) != set(FIXED) | VARIABLE:
        raise checkpoint.CheckpointError('continuation_settings_invalid')
    if _bytes({key: value[key] for key in FIXED}) != _bytes(FIXED):
        raise checkpoint.CheckpointError('continuation_settings_unsupported')
    for key, low, high in (('network_dim', 4, 128), ('max_train_steps', 1, 1_000_000_000),
            ('gradient_accumulation_steps', 1, 128), ('seed', 0, 4_294_967_295),
            ('save_every_n_steps', 1, value['max_train_steps'])):
        if type(value[key]) is not int or not low <= value[key] <= high:
            raise checkpoint.CheckpointError('continuation_settings_invalid')
    for key, low, high in (('network_alpha', 1, value['network_dim']), ('learning_rate', 0, .01)):
        if type(value[key]) not in (int, float) or not math.isfinite(value[key]) or not low <= value[key] <= high:
            raise checkpoint.CheckpointError('continuation_settings_invalid')
    if (value['learning_rate'] == 0 or value['mixed_precision'] not in ('no', 'fp16', 'bf16')
            or type(value['mixed_precision']) is not str or type(value['gradient_checkpointing']) is not bool
            or value['max_train_steps'] % value['save_every_n_steps'] != 0
            or math.ceil(value['max_train_steps'] / value['save_every_n_steps']) > MAX_SAVES):
        raise checkpoint.CheckpointError('continuation_settings_invalid')
    return value


def resolved_contract(prerequisite, settings, epoch_batches, dataset_order_sha256, prepared_source):
    continuation.validate_contract(prerequisite)
    validate_settings(settings)
    if (type(epoch_batches) is not int or not 1 <= epoch_batches <= 1_000_000
            or not checkpoint._hash(dataset_order_sha256)
            or type(prepared_source) is not dict
            or set(prepared_source) != {'original_sha256', 'patched_sha256', 'catalog_sha256'}
            or any(not checkpoint._hash(v) for v in prepared_source.values())
            or prepared_source['patched_sha256'] != continuation.PATCH_SHA256):
        raise checkpoint.CheckpointError('continuation_schedule_invalid')
    from .training_resume_adapter import SOURCE_HASHES
    if prepared_source['original_sha256'] not in SOURCE_HASHES:
        raise checkpoint.CheckpointError('continuation_source_changed')
    return deepcopy({'schema_version': 1, 'profile': PROFILE, 'prerequisite': prerequisite,
        'settings': settings, 'epoch_batches': epoch_batches,
        'dataset_order_sha256': dataset_order_sha256, 'prepared_source': prepared_source,
        'implementation': implementation_identity(), 'application_execution_verified': False})


def validate_contract(value):
    try:
        if type(value) is not dict or set(value) != {'schema_version', 'profile', 'prerequisite', 'settings',
                'epoch_batches', 'dataset_order_sha256', 'prepared_source', 'implementation', 'application_execution_verified'}:
            raise ValueError
        rebuilt = resolved_contract(value['prerequisite'], value['settings'], value['epoch_batches'],
            value['dataset_order_sha256'], value['prepared_source'])
        if _bytes(value) != _bytes(rebuilt):
            raise ValueError
    except (KeyError, TypeError, ValueError):
        raise checkpoint.CheckpointError('continuation_execution_contract_changed') from None
    return value


def saved_position(contract, step, epoch, *, resume=False):
    settings = contract['settings']
    maximum, accumulation = settings['max_train_steps'], settings['gradient_accumulation_steps']
    checkpoint._position(step, epoch)
    per_epoch = math.ceil(contract['epoch_batches'] / accumulation)
    if step > maximum or epoch != math.ceil(step / per_epoch):
        raise checkpoint.CheckpointError('continuation_saved_position_changed')
    if resume:
        return position(step, contract['epoch_batches'], accumulation, maximum)
    return (step - 1) // per_epoch, None, step


def state_hashes(value):
    if type(value) is not dict or set(value) != STATE_ROLES or any(not checkpoint._hash(v) for v in value.values()):
        raise checkpoint.CheckpointError('continuation_state_fingerprint_invalid')
    return value


class CheckpointJournal:
    """Single writer, bounded exclusive records; no process-event mutation."""
    def __init__(self, destination, binding, contract, *, create=False):
        self.destination = Path(destination).absolute()
        self.binding, self.contract = deepcopy(checkpoint._binding(binding)), deepcopy(validate_contract(contract))
        self.identity = _directory(self.destination)
        self.folder = self.destination / 'checkpoint-events'
        if create:
            self.folder.mkdir(exist_ok=False)
        self.folder_identity = _directory(self.folder)
        self.header = {'schema_version': 1, 'profile': PROFILE, 'binding': self.binding,
            'contract_sha256': digest(self.contract)}
        if create:
            _write_new(self.folder / 'header.json', _bytes(self.header))
        self._current()

    def _current(self):
        if (_directory(self.destination) != self.identity or _directory(self.folder) != self.folder_identity
                or _read(self.folder / 'header.json', limit=4096) != _bytes(self.header)):
            raise checkpoint.CheckpointError('continuation_journal_changed')

    def read(self, sequence, expected_tip):
        if type(sequence) is not int or not 0 <= sequence < MAX_SAVES or not checkpoint._hash(expected_tip):
            raise checkpoint.CheckpointError('continuation_journal_unbound')
        self._current()
        rows, previous, last_step = [], digest(self.header), 0
        for index in range(sequence + 1):
            raw = _read(self.folder / f'{index:04d}.json', limit=8192)
            value = self._row(raw, index, previous, last_step)
            rows.append(value)
            previous, last_step = hashlib.sha256(raw).hexdigest(), value['step']
        if previous != expected_tip:
            raise checkpoint.CheckpointError('continuation_journal_tip_changed')
        self._current()
        return rows

    def _row(self, raw, index, previous, last_step):
        value = json.loads(raw)
        if (type(value) is not dict or set(value) != {'sequence', 'previous', 'name', 'step', 'epoch', 'proof', 'state'}
                or _bytes(value) != raw or type(value['sequence']) is not int or value['sequence'] != index
                or value['previous'] != previous or type(value['name']) is not str
                or not checkpoint.NAME.fullmatch(value['name']) or type(value['step']) is not int or value['step'] <= last_step
                or type(value['proof']) is not dict or set(value['proof']) != {'checkpoint_receipt_sha256', 'continuation_receipt_sha256'}
                or any(not checkpoint._hash(v) for v in value['proof'].values())):
            raise checkpoint.CheckpointError('continuation_journal_invalid')
        saved_position(self.contract, value['step'], value['epoch'])
        settings = self.contract['settings']
        if value['step'] % settings['save_every_n_steps'] and value['step'] != settings['max_train_steps']:
            raise checkpoint.CheckpointError('continuation_journal_schedule_changed')
        state_hashes(value['state'])
        return value

    def append(self, name, step, epoch, proof, state, sequence, expected_tip):
        self._current()
        if type(sequence) is not int or not 0 <= sequence < MAX_SAVES:
            raise checkpoint.CheckpointError('continuation_journal_capacity')
        previous = self.read(sequence - 1, expected_tip) if sequence else []
        if not sequence and expected_tip != digest(self.header):
            raise checkpoint.CheckpointError('continuation_journal_tip_changed')
        value = {'sequence': sequence, 'previous': expected_tip, 'name': name, 'step': step,
            'epoch': epoch, 'proof': deepcopy(proof), 'state': deepcopy(state)}
        raw = _bytes(value)
        self._row(raw, sequence, expected_tip, previous[-1]['step'] if previous else 0)
        _write_new(self.folder / f'{sequence:04d}.json', raw)
        tip = hashlib.sha256(raw).hexdigest()
        self.read(sequence, tip)
        return tip


@checkpoint._safe_error
def anchor_stopped(plan, expected_process_tip, contract, *, process_service=None):
    """Retain a valid journal prefix on success, failure, cancellation or recovery.

    A torn/unpublished tail remains held. The anchor never selects a checkpoint
    to load; callers must choose an explicit anchored entry and retain its hash.
    """
    validate_contract(contract)
    rows, tip = TrainingRunJournal(plan.destination).read()
    recovered_retry = bool(rows and rows[-1]['state'] == 'interrupted' and rows[-1]['previous'] == expected_process_tip
        and rows[-1]['details'].get('tree_stopped') is True and rows[-1]['details'].get('outputs_verified') is False)
    if ((tip != expected_process_tip and not recovered_retry) or not rows
            or _bytes(rows[0]['details']['plan']) != _bytes(TrainingProcessService._receipt(plan))):
        raise checkpoint.CheckpointError('continuation_process_changed')
    recovered = (process_service or TrainingProcessService()).recover(plan.destination, tip)
    if not recovered.tree_stopped:
        raise checkpoint.CheckpointError('continuation_process_active')
    journal = CheckpointJournal(plan.destination, checkpoint.binding_for(plan), contract)
    entries, previous, tail_held = [], digest(journal.header), False
    names = set()
    with os.scandir(journal.folder) as members:
        for index, member in enumerate(members):
            if index >= MAX_SAVES + 2:
                tail_held = True
                break
            names.add(member.name)
    allowed = {'header.json', *(f'{i:04d}.json' for i in range(MAX_SAVES))}
    if not names <= allowed:
        tail_held = True
    for index in range(MAX_SAVES):
        path = journal.folder / f'{index:04d}.json'
        if not path.exists():
            tail_held |= any(name.endswith('.json') and name != 'header.json' and name > path.name for name in names)
            break
        try:
            raw = _read(path, limit=8192)
            candidate = hashlib.sha256(raw).hexdigest()
            row = journal._row(raw, index, previous, entries[-1]['step'] if entries else 0)
        except (OSError, ValueError, RuntimeError, TypeError, KeyError):
            tail_held = True
            break
        previous = candidate
        entries.append({'sequence': index, 'name': row['name'], 'step': row['step'], 'epoch': row['epoch'], 'tip': previous})
    journal._current()
    if entries:
        journal.read(entries[-1]['sequence'], previous)
    value = {'schema_version': 1, 'profile': PROFILE, 'binding': journal.binding,
        'contract_sha256': digest(contract), 'process_tip': recovered.journal_tip,
        'process_state': recovered.status, 'entries': entries, 'tail_held': tail_held,
        'resume_ready': False}
    path = plan.destination / 'checkpoint-anchor.json'
    raw = _bytes(value)
    if os.path.lexists(path):
        if _read(path, limit=1024**2) != raw:
            raise checkpoint.CheckpointError('continuation_anchor_changed')
    else:
        _write_new(path, raw)
    if _read(path, limit=1024**2) != raw:
        raise checkpoint.CheckpointError('continuation_anchor_changed')
    return digest(value)


@checkpoint._safe_error
def anchored_checkpoint(destination, binding, contract, anchor_sha256, sequence):
    validate_contract(contract)
    checkpoint._binding(binding)
    if not checkpoint._hash(anchor_sha256) or type(sequence) is not int or not 0 <= sequence < MAX_SAVES:
        raise checkpoint.CheckpointError('continuation_anchor_unbound')
    raw = _read(Path(destination) / 'checkpoint-anchor.json', limit=1024**2)
    value = json.loads(raw)
    if (hashlib.sha256(raw).hexdigest() != anchor_sha256 or _bytes(value) != raw
            or type(value) is not dict or set(value) != {'schema_version', 'profile', 'binding', 'contract_sha256',
                'process_tip', 'process_state', 'entries', 'tail_held', 'resume_ready'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1 or value['profile'] != PROFILE
            or value['binding'] != binding or value['contract_sha256'] != digest(contract)
            or value['resume_ready'] is not False or type(value['tail_held']) is not bool
            or value['process_state'] not in {'exited_unverified', 'cancelled', 'failed', 'interrupted'}
            or not checkpoint._hash(value['process_tip']) or type(value['entries']) is not list
            or not 1 <= len(value['entries']) <= MAX_SAVES or sequence >= len(value['entries'])):
        raise checkpoint.CheckpointError('continuation_anchor_changed')
    entry = value['entries'][sequence]
    if type(entry) is not dict or set(entry) != {'sequence', 'name', 'step', 'epoch', 'tip'} or type(entry['sequence']) is not int or entry['sequence'] != sequence:
        raise checkpoint.CheckpointError('continuation_anchor_changed')
    journal = CheckpointJournal(destination, binding, contract)
    row = journal.read(sequence, entry['tip'])[-1]
    if _bytes({key: row[key] for key in ('sequence', 'name', 'step', 'epoch')}) != _bytes({key: entry[key] for key in ('sequence', 'name', 'step', 'epoch')}):
        raise checkpoint.CheckpointError('continuation_anchor_changed')
    saved_position(contract, row['step'], row['epoch'], resume=True)
    return deepcopy(row)


@checkpoint._safe_error
def anchored_choices(destination, binding, contract, anchor_sha256, *, should_cancel=None):
    """Read the anchored metadata prefix in linear work; never read state payloads.

    A damaged tail does not conceal earlier retained choices. Selection still
    rechecks its exact anchor, prefix and complete checkpoint before loading.
    """
    checkpoint._cancel(should_cancel)
    validate_contract(contract)
    checkpoint._binding(binding)
    if not checkpoint._hash(anchor_sha256):
        raise checkpoint.CheckpointError('continuation_anchor_unbound')
    path = Path(destination) / 'checkpoint-anchor.json'
    raw = _read(path, limit=1024**2)
    value = json.loads(raw)
    if (hashlib.sha256(raw).hexdigest() != anchor_sha256 or _bytes(value) != raw
            or type(value) is not dict or set(value) != {'schema_version', 'profile', 'binding', 'contract_sha256',
                'process_tip', 'process_state', 'entries', 'tail_held', 'resume_ready'}
            or type(value['schema_version']) is not int or value['schema_version'] != 1 or value['profile'] != PROFILE
            or value['binding'] != binding or value['contract_sha256'] != digest(contract)
            or value['resume_ready'] is not False or type(value['tail_held']) is not bool
            or value['process_state'] not in {'exited_unverified', 'cancelled', 'failed', 'interrupted'}
            or not checkpoint._hash(value['process_tip']) or type(value['entries']) is not list
            or len(value['entries']) > MAX_SAVES):
        raise checkpoint.CheckpointError('continuation_anchor_changed')
    journal = CheckpointJournal(destination, binding, contract)
    rows, receipts, previous, last_step = [], [], digest(journal.header), 0
    for index, entry in enumerate(value['entries']):
        checkpoint._cancel(should_cancel)
        try:
            if (type(entry) is not dict or set(entry) != {'sequence', 'name', 'step', 'epoch', 'tip'}
                    or type(entry['sequence']) is not int or entry['sequence'] != index):
                break
            row_raw = _read(journal.folder / f'{index:04d}.json', limit=8192)
            row = journal._row(row_raw, index, previous, last_step)
            tip = hashlib.sha256(row_raw).hexdigest()
            if (entry['tip'] != tip or _bytes({key: row[key] for key in ('sequence', 'name', 'step', 'epoch')})
                    != _bytes({key: entry[key] for key in ('sequence', 'name', 'step', 'epoch')})):
                break
        except (OSError, ValueError, RuntimeError, TypeError, KeyError):
            break
        receipts.append((index, tip))
        previous, last_step = tip, row['step']
        if row['step'] < contract['settings']['max_train_steps']:
            saved_position(contract, row['step'], row['epoch'], resume=True)
            rows.append(row)
    # Fresh read-back catches a changed earlier prefix without repeating every
    # prefix for every displayed choice. Memory stays bounded by MAX_SAVES.
    for index, expected in receipts:
        checkpoint._cancel(should_cancel)
        if hashlib.sha256(_read(journal.folder / f'{index:04d}.json', limit=8192)).hexdigest() != expected:
            raise checkpoint.CheckpointError('continuation_journal_changed')
    journal._current()
    if _read(path, limit=1024**2) != raw:
        raise checkpoint.CheckpointError('continuation_anchor_changed')
    return tuple(deepcopy(rows))
