"""Bounded atomic training progress, separate from checkpoint/result authority.

One owned worker publishes a small latest observation. The controller combines
it with process state; a completed observation alone never verifies a result.
"""
from __future__ import annotations

from copy import deepcopy
import hashlib
import math
import os
from pathlib import Path
import re
import time
from uuid import uuid4

from .training_process import _bytes, _read, _write_new
from .training_source_io import _assert_unredirected

NAME = 'training-progress.json'
MAX_BYTES = 4096
SHA = re.compile(r'[0-9a-f]{64}\Z')
STATES = {'starting', 'running', 'checkpoint', 'completed'}
FIELDS = {'schema_version', 'kind', 'revision', 'process_fingerprint',
    'execution_contract_sha256', 'state', 'optimizer_step', 'max_train_steps',
    'epoch', 'loss', 'checkpoint_sequence', 'checkpoint_tip'}


def validate_progress(value, process_fingerprint, execution_contract_sha256):
    if (type(value) is not dict or set(value) != FIELDS
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['kind'] != 'training_progress'
            or value['process_fingerprint'] != process_fingerprint
            or value['execution_contract_sha256'] != execution_contract_sha256
            or any(type(item) is not str or SHA.fullmatch(item) is None
                for item in (process_fingerprint, execution_contract_sha256))
            or type(value['state']) is not str or value['state'] not in STATES):
        raise ValueError('training_progress_invalid')
    for field, minimum in (('revision', 1), ('optimizer_step', 0), ('max_train_steps', 1), ('epoch', 0)):
        if type(value[field]) is not int or not minimum <= value[field] <= 1_000_000_000:
            raise ValueError('training_progress_position_invalid')
    if (value['optimizer_step'] > value['max_train_steps']
            or value['state'] == 'starting' and value['optimizer_step'] != 0
            or value['state'] == 'completed' and value['optimizer_step'] != value['max_train_steps']):
        raise ValueError('training_progress_position_invalid')
    if value['loss'] is not None and (type(value['loss']) not in (int, float)
            or not math.isfinite(value['loss']) or value['loss'] < 0):
        raise ValueError('training_progress_loss_invalid')
    sequence, tip = value['checkpoint_sequence'], value['checkpoint_tip']
    if ((sequence is None) != (tip is None)
            or sequence is not None and (type(sequence) is not int or not 0 <= sequence < 1000
                or type(tip) is not str or SHA.fullmatch(tip) is None)
            or value['state'] in {'checkpoint', 'completed'} and sequence is None):
        raise ValueError('training_progress_checkpoint_invalid')
    if len(_bytes(value)) > MAX_BYTES:
        raise ValueError('training_progress_bound')
    return value


def read_progress(work, process_fingerprint, execution_contract_sha256):
    import json
    raw = _read(Path(work) / NAME, limit=MAX_BYTES)
    value = json.loads(raw)
    if _bytes(value) != raw:
        raise ValueError('training_progress_noncanonical')
    validate_progress(value, process_fingerprint, execution_contract_sha256)
    return value, hashlib.sha256(raw).hexdigest()


class TrainingProgressWriter:
    def __init__(self, work, process_fingerprint, execution_contract_sha256, maximum,
                 *, clock=time.monotonic):
        self.work = Path(work).absolute()
        self.process_fingerprint = process_fingerprint
        self.execution_contract_sha256 = execution_contract_sha256
        self.maximum, self.clock = maximum, clock
        self.last, self.last_digest, self.last_time = None, None, float('-inf')
        if (self.work / NAME).exists():
            raise ValueError('training_progress_already_exists')

    def publish(self, state, step, epoch, *, loss=None, checkpoint_sequence=None, checkpoint_tip=None):
        value = {'schema_version': 1, 'kind': 'training_progress',
            'revision': 1 if self.last is None else self.last['revision'] + 1,
            'process_fingerprint': self.process_fingerprint,
            'execution_contract_sha256': self.execution_contract_sha256,
            'state': state, 'optimizer_step': step, 'max_train_steps': self.maximum,
            'epoch': epoch, 'loss': loss, 'checkpoint_sequence': checkpoint_sequence,
            'checkpoint_tip': checkpoint_tip}
        validate_progress(value, self.process_fingerprint, self.execution_contract_sha256)
        if self.last is not None:
            if (self.last['state'] == 'completed' or step < self.last['optimizer_step']
                    or epoch < self.last['epoch'] or state == 'starting'):
                raise ValueError('training_progress_transition_invalid')
            previous_sequence = self.last['checkpoint_sequence']
            if previous_sequence is not None and (checkpoint_sequence is None
                    or checkpoint_sequence < previous_sequence
                    or checkpoint_sequence == previous_sequence and checkpoint_tip != self.last['checkpoint_tip']):
                raise ValueError('training_progress_checkpoint_changed')
            current, digest = read_progress(self.work, self.process_fingerprint, self.execution_contract_sha256)
            if current != self.last or digest != self.last_digest:
                raise ValueError('training_progress_changed')
            if state == 'running' and self.clock() - self.last_time < 1:
                return self.last_digest
        destination = self.work / NAME
        temporary = self.work / ('.training-progress-' + uuid4().hex + '.json')
        raw = _bytes(value)
        _write_new(temporary, raw)
        _assert_unredirected(destination)
        # This is an observation only; checkpoints and their anchors remain
        # immutable. Retain a failed temporary record instead of deleting it.
        if self.last is None:
            os.link(temporary, destination)
        else:
            os.replace(temporary, destination)
        observed, digest = read_progress(self.work, self.process_fingerprint, self.execution_contract_sha256)
        if observed != value:
            raise ValueError('training_progress_readback_failed')
        if self.last is None:
            temporary.unlink()
        self.last, self.last_digest, self.last_time = deepcopy(value), digest, self.clock()
        return digest
