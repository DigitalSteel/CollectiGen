"""Bounded, non-executable recovery evidence for the pinned SDXL state format.

Only the owned writer calls seal after its save operation returns. The process
owner must retain the returned digest independently. Inspection hashes opaque
optimizer/RNG bytes; it never imports torch, unpickles state, or grants resume.
"""
from __future__ import annotations

import hashlib
import json
import os
from pathlib import Path
import re

from .training_process import (
    TrainingProcessService, TrainingRunJournal,
    _bytes, _directory, _read, _state, _write_new,
)
from .training_windows_file_identity import descriptor_identity


PROFILE = 'sd-scripts-accelerate-state-layout-v1'
FILES = {'model.safetensors': 512 * 1024**2, 'optimizer.bin': 1024**3,
    'scheduler.bin': 1024**2, 'random_states_0.pkl': 1024**2,
    'train_state.json': 4096}
SHA = re.compile(r'[0-9a-f]{64}')
NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,95}-state')
BINDINGS = {'run_id', 'process_sha256', 'preview_sha256'}


class CheckpointError(ValueError):
    """Stable reason only; no selected paths, state or exception payloads."""


def _hash(value):
    return type(value) is str and SHA.fullmatch(value) is not None


def _binding(value):
    if (type(value) is not dict or set(value) != BINDINGS
            or type(value['run_id']) is not str or not re.fullmatch(r'[0-9a-f]{32}', value['run_id'])
            or not all(_hash(value[key]) for key in ('process_sha256', 'preview_sha256'))):
        raise CheckpointError('checkpoint_binding_invalid')
    return value


def binding_for(plan):
    plan.validate()
    return _binding({'run_id': plan.run_id, 'process_sha256': plan.fingerprint,
        'preview_sha256': plan.preview_fingerprint})


def _position(step, epoch):
    if any(type(n) is not int or not 1 <= n <= 1_000_000_000 for n in (step, epoch)):
        raise CheckpointError('checkpoint_position_invalid')


def _paths(work, name):
    if type(name) is not str or NAME.fullmatch(name) is None:
        raise CheckpointError('checkpoint_name_invalid')
    work = Path(work).absolute()
    _directory(work)
    return work / name, work / (name + '.collectigen.json')


def _cancel(callback):
    if callback and callback():
        raise InterruptedError('Checkpoint inspection cancelled.')


def _file_id(path, *, directory=False):
    """Full Windows identity, consistent across CPython 3.11/3.12+ stat APIs."""
    before = _state(path, directory=directory)
    if os.name != 'nt':
        return tuple(format(value, 'x') for value in before[:2])
    import ctypes as C
    from ctypes import wintypes as W
    class FileIdInfo(C.Structure):
        _fields_ = [('volume', C.c_ulonglong), ('file_id', C.c_ubyte * 16)]
    api = C.WinDLL('kernel32', use_last_error=True)
    api.CreateFileW.argtypes = [W.LPCWSTR, W.DWORD, W.DWORD, W.LPVOID, W.DWORD, W.DWORD, W.HANDLE]
    api.CreateFileW.restype = W.HANDLE
    api.GetFileInformationByHandleEx.argtypes = [W.HANDLE, C.c_int, W.LPVOID, W.DWORD]
    api.GetFileInformationByHandleEx.restype = W.BOOL
    api.CloseHandle.argtypes, api.CloseHandle.restype = [W.HANDLE], W.BOOL
    # Read attributes, share existing writers, open existing; do not follow a
    # final reparse point. Python path/type/stale checks bracket the native read.
    handle = api.CreateFileW(str(path), 0x80, 7, None, 3, 0x02000000 | 0x00200000, None)
    if handle == C.c_void_p(-1).value:
        raise C.WinError(C.get_last_error())
    query_failed = True
    try:
        info = FileIdInfo()
        if not api.GetFileInformationByHandleEx(handle, 18, C.byref(info), C.sizeof(info)):
            raise C.WinError(C.get_last_error())
        value = (format(info.volume, '016x'), bytes(info.file_id).hex())
        query_failed = False
    finally:
        if not api.CloseHandle(handle) and not query_failed:
            raise C.WinError(C.get_last_error())
    if _state(path, directory=directory) != before:
        raise CheckpointError('checkpoint_file_changed')
    return value


def _persisted_state(path):
    before = _state(path)
    identity = _file_id(path)
    if _state(path) != before:
        raise CheckpointError('checkpoint_file_changed')
    return [*identity, *before[2:]]


def _members(folder):
    identity = _file_id(folder, directory=True)
    names = set()
    with os.scandir(folder) as entries:
        for entry in entries:
            if entry.name not in FILES or entry.name in names:
                raise CheckpointError('checkpoint_members_changed')
            names.add(entry.name)
    if names != set(FILES) or _file_id(folder, directory=True) != identity:
        raise CheckpointError('checkpoint_members_changed')
    return identity


def _capture(path, limit, should_cancel):
    before = _state(path)
    portable = _persisted_state(path)
    if not 1 <= before[2] <= limit or path.stat(follow_symlinks=False).st_nlink != 1:
        raise CheckpointError('checkpoint_file_bound_or_alias')
    digest = hashlib.sha256()
    with path.open('rb') as handle:
        if descriptor_identity(handle.fileno()) != before:
            raise CheckpointError('checkpoint_file_changed')
        remaining = before[2]
        while remaining:
            _cancel(should_cancel)
            block = handle.read(min(1024**2, remaining))
            if not block:
                raise CheckpointError('checkpoint_file_changed')
            remaining -= len(block)
            digest.update(block)
        if handle.read(1) or descriptor_identity(handle.fileno()) != before:
            raise CheckpointError('checkpoint_file_changed')
    if _state(path) != before or _persisted_state(path) != portable:
        raise CheckpointError('checkpoint_file_changed')
    return {'identity': portable, 'bytes': before[2], 'sha256': digest.hexdigest()}


def _snapshot(folder, step, epoch, should_cancel):
    identity = _members(folder)
    rows = {name: _capture(folder / name, limit, should_cancel) for name, limit in FILES.items()}
    raw = _read(folder / 'train_state.json', limit=FILES['train_state.json'])
    try:
        state = json.loads(raw)
    except (ValueError, UnicodeError):
        raise CheckpointError('checkpoint_position_invalid') from None
    if (type(state) is not dict or set(state) != {'current_epoch', 'current_step'}
            or type(state['current_step']) is not int or state['current_step'] != step
            or type(state['current_epoch']) is not int or state['current_epoch'] != epoch):
        raise CheckpointError('checkpoint_position_changed')
    if hashlib.sha256(raw).hexdigest() != rows['train_state.json']['sha256']:
        raise CheckpointError('checkpoint_file_changed')
    _cancel(should_cancel)
    if _members(folder) != identity or any(_persisted_state(folder / name) != row['identity'] for name, row in rows.items()):
        raise CheckpointError('checkpoint_file_changed')
    return list(identity), rows


def _safe_error(operation):
    def wrapped(*args, **kwargs):
        try:
            return operation(*args, **kwargs)
        except (CheckpointError, InterruptedError):
            raise
        except (OSError, RuntimeError, ValueError, TypeError, KeyError):
            raise CheckpointError('checkpoint_unavailable_or_changed') from None
    return wrapped


@_safe_error
def record_save_completion(work, name, binding, *, step, epoch):
    """Owned writer hook: publish only AFTER its save call returned."""
    _binding(binding)
    _position(step, epoch)
    _, receipt = _paths(work, name)
    marker = receipt.with_suffix('.save-returned.json')
    raw = _bytes({'schema_version': 1, 'event': 'save_returned', 'binding': binding,
        'name': name, 'step': step, 'epoch': epoch})
    _write_new(marker, raw)
    return hashlib.sha256(raw).hexdigest()


def _completion(receipt, binding, name, step, epoch, digest):
    if not _hash(digest):
        raise CheckpointError('checkpoint_save_completion_unbound')
    expected = _bytes({'schema_version': 1, 'event': 'save_returned', 'binding': binding,
        'name': name, 'step': step, 'epoch': epoch})
    raw = _read(receipt.with_suffix('.save-returned.json'), limit=4096)
    if raw != expected or hashlib.sha256(raw).hexdigest() != digest:
        raise CheckpointError('checkpoint_save_completion_changed')


@_safe_error
def seal_checkpoint(work, name, binding, *, step, epoch, completion_sha256, should_cancel=None):
    """Publish exclusively after an owned save returned; no cleanup on failure.

    This records integrity and writer completion, not deserialization safety or
    a successful resume. The exact digest belongs in separate owner evidence.
    """
    _binding(binding)
    _position(step, epoch)
    folder, receipt = _paths(work, name)
    if os.path.lexists(receipt):
        raise CheckpointError('checkpoint_receipt_exists')
    _completion(receipt, binding, name, step, epoch, completion_sha256)
    identity, files = _snapshot(folder, step, epoch, should_cancel)
    value = {'schema_version': 1, 'profile': PROFILE, 'binding': binding,
        'name': name, 'directory_identity': identity, 'step': step, 'epoch': epoch,
        'files': files, 'save_completion_sha256': completion_sha256, 'load_verified': False}
    raw = _bytes(value)
    _cancel(should_cancel)
    _write_new(receipt, raw)
    digest = hashlib.sha256(raw).hexdigest()
    inspect_checkpoint(work, name, binding, expected_receipt_sha256=digest, should_cancel=should_cancel)
    return digest


@_safe_error
def inspect_checkpoint(work, name, binding, *, expected_receipt_sha256, should_cancel=None):
    """Read one explicitly selected state against independently retained proof."""
    _binding(binding)
    if not _hash(expected_receipt_sha256):
        raise CheckpointError('checkpoint_receipt_unbound')
    folder, receipt = _paths(work, name)
    raw = _read(receipt, limit=16 * 1024)
    if hashlib.sha256(raw).hexdigest() != expected_receipt_sha256:
        raise CheckpointError('checkpoint_receipt_changed')
    value = json.loads(raw)
    expected = {'schema_version', 'profile', 'binding', 'name', 'directory_identity',
        'step', 'epoch', 'files', 'save_completion_sha256', 'load_verified'}
    if (type(value) is not dict or set(value) != expected or _bytes(value) != raw
            or type(value['schema_version']) is not int or value['schema_version'] != 1
            or value['profile'] != PROFILE or value['binding'] != binding or value['name'] != name
            or value['load_verified'] is not False):
        raise CheckpointError('checkpoint_receipt_invalid')
    _position(value['step'], value['epoch'])
    _completion(receipt, binding, name, value['step'], value['epoch'], value['save_completion_sha256'])
    identity, files = _snapshot(folder, value['step'], value['epoch'], should_cancel)
    # Canonical comparison also prevents bool/int coercion inside nested rows.
    if _bytes((identity, files)) != _bytes((value['directory_identity'], value['files'])):
        raise CheckpointError('checkpoint_snapshot_changed')
    if _read(receipt, limit=16 * 1024) != raw:
        raise CheckpointError('checkpoint_receipt_changed')
    _completion(receipt, binding, name, value['step'], value['epoch'], value['save_completion_sha256'])
    return {'status': 'retained_state_verified', 'profile': PROFILE,
        'receipt_sha256': expected_receipt_sha256, 'step': value['step'], 'epoch': value['epoch'],
        'files': len(files), 'bytes': sum(row['bytes'] for row in files.values()),
        'resume_ready': False, 'load_verified': False, 'output_registered': False}


@_safe_error
def assess_recovery(plan, expected_tip, name, *, expected_receipt_sha256,
                    process_service=None, should_cancel=None):
    """Reconcile the exact stopped Job, then assess a single retained checkpoint.

    Reuses process recovery (including its interrupted journal publication).
    Does not choose a latest folder, restart a process, or deserialize any state.
    """
    binding = binding_for(plan)
    _paths(plan.destination / 'work', name)
    rows, tip = TrainingRunJournal(plan.destination).read()
    if (tip != expected_tip or not rows
            or _bytes(rows[0]['details']['plan']) != _bytes(TrainingProcessService._receipt(plan))):
        raise CheckpointError('checkpoint_process_changed')
    _cancel(should_cancel)
    recovered = (process_service or TrainingProcessService()).recover(plan.destination, expected_tip)
    if not recovered.tree_stopped:
        raise CheckpointError('checkpoint_process_active')
    checked = inspect_checkpoint(plan.destination / 'work', name, binding,
        expected_receipt_sha256=expected_receipt_sha256, should_cancel=should_cancel)
    checked.update(process_state=recovered.status, journal_tip=recovered.journal_tip)
    return checked
